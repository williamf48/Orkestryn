#!/usr/bin/env python3
"""Restartable local arithmetic runtime with durable receipts and leased workers.

Workers survive supervisor death until their own fixed lease expires. Reattach
requires the saved worker nonce plus OS creation identity. No saved PID is ever
used to terminate a process. Exit codes can remain unknown after reattachment;
an independently observed OS exit is recorded separately from an exit code.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import hmac
import http.client
import json
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from http.server import HTTPServer
from types import SimpleNamespace
from unittest import mock

import local_service as local
import native_process
import restart_checker as restart
import runtime_sessions as sessions
import service_agent as service
import simulator as sim
import team_workspace as workspace

VERSION = "agent-rehearsal-durable-runtime-v1"
ROOT = Path(__file__).resolve().parent
CHILDREN = {}


def source_digest():
    return sim.digest({"version": VERSION, "file": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "service": service.controller_digest(), "native_process": hashlib.sha256((ROOT/"native_process.py").read_bytes()).hexdigest()})


def read(directory, name):
    directory=Path(directory).absolute()
    if name!='ready.json':return sessions.read_file(directory,name)
    # A Windows scanner/reader can briefly deny this newly published record.
    # Retry only the same read; every attempt repeats the existing path/size
    # checks. Invalid JSON, unrelated errors and persistent denials still fail.
    deadline=time.monotonic()+2
    while True:
        try:return sessions.read_file(directory,name)
        except PermissionError as exc:
            winerror=getattr(exc,'winerror',None)
            retryable=(platform.system()=='Windows' and
                (winerror in {5,32,33} or (winerror is None and exc.errno==errno.EACCES)))
            remaining=deadline-time.monotonic()
            if not retryable or remaining<=0:raise
            time.sleep(min(.025,remaining))


os_process = native_process.os_process


def observe(config_dir):
    config_dir = Path(config_dir)
    config = read(config_dir, "config.json")
    try:
        ready = read(config_dir, "ready.json")
    except (ValueError, FileNotFoundError):
        return {"pid": None, "identity": config["identity"], "alive": None, "healthy": False,
                "in_flight": 0, "exit_code": None, "exit_confirmed": False, "observation": "READINESS_UNKNOWN"}
    if (ready.get("identity") != config["identity"] or ready.get("target") != config["target"]
            or ready.get("runtime_id") != config["runtime_id"] or type(ready.get("port")) is not int
            or not 1 <= ready["port"] <= 65535):
        raise ValueError("Saved worker readiness identity changed.")
    if not isinstance(ready.get("os_identity"),dict):
        raise ValueError("The worker has no registered OS creation identity.")
    facts = os_process(ready["pid"],ready["os_identity"])
    same = facts["exists"] and facts["identity"] == ready["os_identity"]
    alive = same and facts["alive"]
    exited = not facts["exists"] or not same or not facts["alive"]
    response = None
    if alive:
        try:
            response = local.http_json(ready["port"], "/health", token=config["token"], timeout=1)
            if any(response.get(k) != ready[k] for k in ("pid", "identity", "target", "runtime_id")):
                response = None
        except (OSError, ValueError, http.client.HTTPException):
            pass
    return {"pid": ready["pid"], "identity": config["identity"], "alive": bool(alive), "healthy": bool(response),
        "in_flight": (response or {}).get("in_flight", 0), "exit_code": facts["exit_code"] if same else None,
        "exit_confirmed": bool(exited), "os_identity": ready["os_identity"],
        "exit_observation": facts["method"] if same or not facts["exists"] else "ORIGINAL_PROCESS_LIFETIME_ENDED_PID_REUSED"}


def worker_main(directory):
    directory = Path(directory).absolute()
    config = read(directory, "config.json")
    if config["version"] != VERSION or config["source_digest"] != source_digest():
        raise ValueError("Worker source changed.")
    remaining = max(0, min(3600, config["expires_unix"] - time.time()))
    if not remaining:
        return
    # Claim is never removed: retrying a spawn cannot create a second service
    # worker even if the first spawn's readiness reply was lost.
    with (directory / ".claim").open("x") as stream:
        stream.write(str(os.getpid()))
    deadline = time.monotonic() + remaining
    stopping = False
    class Handler(local.JsonHandler):
        def authorized(self):
            valid = (self.headers.get("Host") == "127.0.0.1:%s" % server.server_port and not self.headers.get("Origin")
                     and hmac.compare_digest(self.headers.get("X-Rehearsal-Token", ""), config["token"]))
            if not valid:
                self.send_json({"error": "Worker capability required"}, 403)
            return valid

        def do_GET(self):
            if not self.authorized():
                return
            if self.path not in {"/infer", "/health"}:
                return self.send_json({"error": "Unknown worker operation"}, 404)
            self.send_json({"pid": os.getpid(), "identity": config["identity"], "runtime_id": config["runtime_id"],
                "target": config["target"], "healthy": True, "in_flight": 0, "input": 7, "result": 49})

        def do_POST(self):
            nonlocal stopping
            if not self.authorized():
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/stop" or not 0 < size <= 1024:
                    raise ValueError("Only an exact owned worker shutdown is allowed.")
                self.connection.settimeout(2)
                body = workspace.decode(self.rfile.read(size).decode())
                if body != {"identity": config["identity"], "runtime_id": config["runtime_id"]}:
                    raise ValueError("Wrong owned worker identity.")
                stopping = True
                self.send_json({"status": "EXIT_REQUEST_ACKNOWLEDGED"})
            except (ValueError, OSError):
                self.send_json({"error": "Invalid worker shutdown"}, 400)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = .1
    workspace.write_json(directory / "ready.json", {"pid": os.getpid(), "identity": config["identity"], "target": config["target"],
        "runtime_id": config["runtime_id"], "port": server.server_port, "os_identity": os_process(os.getpid())["identity"]})
    try:
        while not stopping and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
        workspace.write_json(directory / "exit_notice.json", {"identity": config["identity"], "pid": os.getpid(),
            "reason": "EXPLICIT_SHUTDOWN" if stopping else "LEASE_EXPIRED", "scope": "Notice precedes process return; OS exit is verified separately."})


def startup_error(stage,directory,exit_code=None):
    """Include bounded child tracebacks, never runtime capability/config files."""
    directory=Path(directory);parts=[]
    for name in ['supervisor.log','worker.log']:
        path=directory/name
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-6000));parts.append(name+':\n'+stream.read(6000).decode('utf-8',errors='replace'))
    for path in sorted((directory/'workers').glob('*/worker.log'))[-2:]:
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-3000));parts.append('worker.log:\n'+stream.read(3000).decode('utf-8',errors='replace'))
    return OSError(stage+' (exit '+str(exit_code)+'). '+str(directory)+'\n'+'\n'.join(parts))


def spawn_child(command, log_path):
    with Path(log_path).open("a", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=restart.child_env(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, shell=False, close_fds=True, **sessions.launch_flags())
    CHILDREN[process.pid] = process
    def reap():
        process.wait()
        CHILDREN.pop(process.pid, None)
    threading.Thread(target=reap, daemon=True).start()
    return process


class RuntimeServer:
    def __init__(self, directory, checkpoint=None):
        self.directory = Path(directory).absolute()
        self.meta = read(self.directory, "runtime.json")
        if self.meta["version"] != VERSION or self.meta["source_digest"] != source_digest():
            raise ValueError("Durable runtime source changed; no reattachment attempted.")
        self.identity, self.token = self.meta["runtime_id"], self.meta["token"]
        self.deadline = time.monotonic() + max(0, min(3600, self.meta["expires_unix"] - time.time()))
        self.checkpoint = checkpoint or (lambda *_: None)
        self.initializing=True
        self.new_runtime=False
        self.initial_workers=[]
        self.lock_db = sqlite3.connect(self.directory / ".supervisor-lock.sqlite3", timeout=.3)
        try:
            self.lock_db.execute("BEGIN EXCLUSIVE")
            self.db = sqlite3.connect(self.directory / "runtime.sqlite3", isolation_level=None)
            self.db.executescript("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS workers (target TEXT PRIMARY KEY, identity TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS journal (sequence INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL);")
            if self.db.execute("SELECT 1 FROM config").fetchone() is None:
                self.new_runtime=True
                self.db.execute("INSERT INTO config VALUES(1,?)", (sim.canonical({"revision": 0, "candidate_percent": 0, "route_owner": None}),))
            self.cursor = 0
            self.stopping = False
            self.ensure_worker("stable-v1")
            # At most one intent was admitted before interruption. Each effect
            # is recovered from its own persisted request and worker identity.
            for key, raw, result in list(self.db.execute("SELECT id,request,result FROM operations ORDER BY rowid")):
                if workspace.decode(result).get("status") == "PENDING":
                    self.operate(workspace.decode(raw))
            self.log("SUPERVISOR_STARTED", pid=os.getpid())
            self.initializing=False
        except BaseException as startup_failure:
            try:
                # Only a new runtime before its first successful initialization
                # owns this rollback. Existing workers survive failed restart.
                if self.new_runtime:self.abort_initial_startup()
            except Exception as cleanup_failure:
                raise OSError('Initial runtime startup failed; owned-worker rollback was not confirmed: '+str(cleanup_failure)) from startup_failure
            finally:
                if getattr(self,'db',None) is not None:self.db.close()
                self.lock_db.close()
            raise

    def abort_initial_startup(self):
        """Cancel only direct Popen children from this unaccepted startup.

        No PID/worker capability read from disk can authorize cancellation.
        A persistent readiness denial may prevent graceful HTTP shutdown, so
        the original live child handle is used and native wait confirms exit.
        This startup teardown is never policy recovery or a capacity release.
        """
        cleanup=[]
        for target,child in self.initial_workers:
            try:
                if child.poll() is None:
                    try:child.terminate()
                    except OSError:
                        if child.poll() is None:raise
                code=child.wait(timeout=5)
                cleanup.append({'target':target,'pid':child.pid,'exit_code':code,'stopped':True,'exit_observation':'OWNED_CHILD_NATIVE_WAIT'})
            except (OSError,subprocess.TimeoutExpired) as exc:
                cleanup.append({'target':target,'pid':child.pid,'stopped':False,'error':type(exc).__name__})
        stopped=all(row['stopped'] for row in cleanup)
        workspace.write_json(self.directory/'teardown.json',{'workers':cleanup,'all_workers_stopped':stopped,
            'reason':'INITIAL_STARTUP_FAILED','scope':'Unaccepted new runtime only; direct owned child handles and native waits. No policy recovery or capacity release.'})
        if not stopped:raise OSError('An owned startup worker has no confirmed exit.')

    def log(self, kind, **payload):
        self.db.execute("INSERT INTO journal(event) VALUES(?)", (sim.canonical({"kind": kind, **payload}),))

    def config(self):
        return workspace.decode(self.db.execute("SELECT value FROM config WHERE id=1").fetchone()[0])

    def worker_dir(self, target):
        row = self.db.execute("SELECT identity FROM workers WHERE target=?", (target,)).fetchone()
        return self.directory / "workers" / row[0] if row else None

    def ensure_worker(self, target):
        directory = self.worker_dir(target)
        if directory is None:
            identity = uuid.uuid4().hex
            directory = self.directory / "workers" / identity
            directory.mkdir(parents=True, exist_ok=False)
            config = {"version": VERSION, "source_digest": self.meta["source_digest"], "identity": identity,
                "runtime_id": self.identity, "target": target, "expires_unix": self.meta["expires_unix"], "token": secrets.token_urlsafe(32)}
            workspace.write_json(directory / "config.json", config)
            (directory / "config.json").chmod(0o600)
            self.db.execute("INSERT INTO workers VALUES(?,?)", (target, identity))
        child=None
        if not (directory / ".claim").exists():
            child=spawn_child([restart.worker_interpreter(), "-u", str(Path(__file__).resolve()), "--worker", str(directory)], directory / "worker.log")
            if self.initializing and self.new_runtime:self.initial_workers.append((target,child))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if child is not None and child.poll() is not None:
                raise startup_error('Worker startup failed',directory,child.returncode)
            value = observe(directory)
            if value["healthy"]:
                return value
            if value["exit_confirmed"]:
                raise OSError("Registered worker has exited; no replacement was started.")
            time.sleep(.025)
        raise OSError("Worker readiness remains unknown; no duplicate worker substituted.")

    def worker_state(self, target):
        directory = self.worker_dir(target)
        return observe(directory) if directory else None

    def snapshot(self):
        workers, endpoints = {}, {}
        for target, _ in self.db.execute("SELECT target,identity FROM workers"):
            directory = self.worker_dir(target)
            workers[target] = observe(directory)
            if (directory / "ready.json").exists():
                endpoints[target] = read(directory, "ready.json")["port"]
        operations = {key: workspace.decode(result) for key, result in self.db.execute("SELECT id,result FROM operations")}
        return {"runtime_id": self.identity, **self.config(), "workers": workers, "receipts": operations,
            "worker_endpoints": endpoints, "supervisor_pid": os.getpid(),
            "candidate_starts": int("candidate-v2" in workers),
            "candidate_stops": int(workers.get("candidate-v2", {}).get("exit_confirmed") is True)}

    def infer(self, target=None):
        if target is None:
            target = "candidate-v2" if self.cursor % 100 < self.config()["candidate_percent"] else "stable-v1"
            self.cursor += 1
        if target not in local.TARGETS or self.worker_dir(target) is None:
            return {"ok": False, "target": target, "error": "Worker absent"}
        directory = self.worker_dir(target)
        began = time.monotonic()
        try:
            config, ready = read(directory, "config.json"), read(directory, "ready.json")
            facts=os_process(ready['pid'],ready['os_identity'])
            if not facts['exists'] or facts['identity']!=ready['os_identity'] or facts['alive'] is False:
                return {"ok":False,"target":target,"error":"ORIGINAL_WORKER_EXITED","latency_ms":round((time.monotonic()-began)*1000,3)}
            result = local.http_json(ready["port"], "/infer", token=config["token"], timeout=1)
            if any(result.get(k) != ready[k] for k in ("pid", "identity", "target", "runtime_id")):
                raise ValueError("Worker identity mismatch")
            return {"ok": True, "target": target, "body": result, "latency_ms": round((time.monotonic()-began)*1000, 3)}
        except (ValueError, OSError, http.client.HTTPException) as exc:
            return {"ok": False, "target": target, "error": type(exc).__name__, "latency_ms": round((time.monotonic()-began)*1000, 3)}

    def stop_worker(self, target):
        directory = self.worker_dir(target)
        if directory is None:
            raise ValueError("Worker identity was never registered.")
        view = observe(directory)
        if view["exit_confirmed"]:
            return view
        config, ready = read(directory, "config.json"), read(directory, "ready.json")
        if view["healthy"]:
            try:
                local.http_json(ready["port"], "/stop", {"identity": config["identity"], "runtime_id": self.identity}, config["token"], timeout=2)
            except (OSError, ValueError, http.client.HTTPException):
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            view = observe(directory)
            if view["exit_confirmed"]:
                return view
            time.sleep(.025)
        return view

    def operate(self, request):
        if (not isinstance(request, dict) or set(request) != {"runtime_id", "operation_id", "expected_revision", "tool", "percent", "owner"}
                or request["runtime_id"] != self.identity or request["tool"] not in local.EFFECTS
                or type(request["expected_revision"]) is not int or not isinstance(request["operation_id"], str)
                or not 0 < len(request["operation_id"]) <= 160):
            raise ValueError("Invalid registered durable runtime operation.")
        key, fingerprint = request["operation_id"], sim.digest(request)
        prior = self.db.execute("SELECT request,result FROM operations WHERE id=?", (key,)).fetchone()
        if prior:
            if sim.digest(workspace.decode(prior[0])) != fingerprint:
                return {"status": "BLOCKED", "code": "RUNTIME_KEY_CONFLICT"}
            result = workspace.decode(prior[1])
            if result["status"] != "PENDING":
                return {**result, "replayed": True}
        state = self.config()
        result = {"status": "PENDING", "code": "RECONCILIATION_REQUIRED", "fingerprint": fingerprint}
        if prior is None:
            self.db.execute("INSERT INTO operations VALUES(?,?,?)", (key, sim.canonical(request), sim.canonical(result)))
            self.log("DURABLE_OPERATION_INTENT", request=request)
        self.checkpoint("intent", request)
        try:
            sim.require(time.monotonic() < self.deadline and source_digest() == self.meta["source_digest"], "RUNTIME_LEASE_OR_SOURCE_CHANGED", "Stop for review.")
            sim.require(request["expected_revision"] == state["revision"], "RUNTIME_VERSION_CONFLICT", "Runtime revision changed.")
            if request["tool"] == "start_candidate":
                if prior is None:
                    sim.require(self.worker_dir("candidate-v2") is None, "RESOURCE_EXISTS", "Registered candidate already exists.")
                self.ensure_worker("candidate-v2")
            elif request["tool"] in {"set_route", "restore_route"}:
                sim.integer(request["percent"], "percent", 0, 100)
                sim.require(self.worker_state("stable-v1")["healthy"], "FALLBACK_NOT_SAFE", "Actual fallback is unhealthy.")
                sim.require(request["percent"] == 0 or (self.worker_state("candidate-v2") or {}).get("healthy"), "CANDIDATE_NOT_READY", "Actual candidate is unhealthy.")
                state.update(candidate_percent=request["percent"], route_owner=request["owner"])
            else:
                sim.require(state["candidate_percent"] == 0, "RESOURCE_STILL_IN_USE", "Actual route still needs the worker.")
                stopped = self.stop_worker("candidate-v2")
                sim.require(stopped["exit_confirmed"], "STOP_UNCONFIRMED", "The original OS process has no confirmed exit.")
            self.checkpoint("effect", request)
            state["revision"] += 1
            result = {"status": "SUCCEEDED", "code": "OK", "fingerprint": fingerprint, "revision": state["revision"]}
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("UPDATE config SET value=? WHERE id=1", (sim.canonical(state),))
                self.db.execute("UPDATE operations SET result=? WHERE id=?", (sim.canonical(result), key))
                self.log("DURABLE_OPERATION_RESULT", request=request, result=result)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.checkpoint("committed", request)
        except sim.Denied as exc:
            if exc.code == "STOP_UNCONFIRMED":
                return result
            result = {"status": "BLOCKED", "code": exc.code, "fingerprint": fingerprint}
            self.db.execute("UPDATE operations SET result=? WHERE id=?", (sim.canonical(result), key))
        except (OSError, ValueError, http.client.HTTPException):
            # Effect uncertainty stays pending and can be reconciled on restart.
            return result
        return result

    def close(self, teardown=False):
        cleanup = []
        try:
            if teardown:
                for target, _ in list(self.db.execute("SELECT target,identity FROM workers")):
                    value = self.stop_worker(target)
                    cleanup.append({"target": target, "pid": value["pid"], "exit_code": value["exit_code"],
                        "stopped": value["exit_confirmed"], "exit_observation": value.get("exit_observation")})
                workspace.write_json(self.directory / "teardown.json", {"workers": cleanup,
                    "all_workers_stopped": bool(cleanup) and all(v["stopped"] for v in cleanup),
                    "scope": "Runtime/lease teardown, separate from policy recovery."})
        finally:
            self.db.close()
            self.lock_db.close()
        return cleanup


def serve(directory, crash_at=None, crash_tool=None):
    from http.server import HTTPServer
    directory = Path(directory).absolute()
    def checkpoint(phase, request):
        if phase == crash_at and request["tool"] == crash_tool:
            workspace.write_json(directory / "crash_checkpoint.json", {"phase": phase, "request": request, "pid": os.getpid()})
            os._exit(73)
    runtime = RuntimeServer(directory, checkpoint)
    class Handler(local.JsonHandler):
        def authorized(self):
            valid = (self.headers.get("Host") == "127.0.0.1:%s" % server.server_port and not self.headers.get("Origin")
                     and hmac.compare_digest(self.headers.get("X-Rehearsal-Token", ""), runtime.token))
            if not valid:
                self.send_json({"error": "Durable runtime capability required"}, 403)
            return valid
        def do_GET(self):
            if not self.authorized():return
            if self.path == "/state":return self.send_json(runtime.snapshot())
            if self.path == "/events":return self.send_json({"events": [{"sequence": n, **workspace.decode(raw)} for n,raw in runtime.db.execute("SELECT sequence,event FROM journal ORDER BY sequence")]})
            if self.path == "/infer":return self.send_json(runtime.infer())
            if self.path in {"/probe?target="+target for target in local.TARGETS}:return self.send_json(runtime.infer(self.path.split("=")[1]))
            self.send_json({"error": "Unknown runtime endpoint"}, 404)
        def do_POST(self):
            if not self.authorized():return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:raise ValueError("Invalid request size")
                self.connection.settimeout(3)
                body = workspace.decode(self.rfile.read(size).decode())
                if self.path == "/operate":result = runtime.operate(body)
                elif self.path == "/teardown" and body == {"runtime_id": runtime.identity}:
                    runtime.stopping = True
                    result = {"status": "TEARDOWN_REQUESTED"}
                else:raise ValueError("Unsupported runtime operation")
                self.send_json(result)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = .1
    workspace.write_json(directory / ".access.json", {"port": server.server_port, "runtime_id": runtime.identity,
        "pid": os.getpid(), "os_identity": os_process(os.getpid())["identity"], "token": runtime.token})
    (directory / ".access.json").chmod(0o600)
    try:
        while not runtime.stopping and time.monotonic() < runtime.deadline:
            server.handle_request()
    finally:
        server.server_close()
        runtime.close(teardown=True)


class Runtime:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        meta = read(self.directory, "runtime.json")
        self.identity = meta["runtime_id"]
    @property
    def access(self):
        access = read(self.directory, ".access.json")
        if access["runtime_id"] != self.identity or type(access["port"]) is not int or not 1 <= access["port"] <= 65535:
            raise ValueError("Runtime identity or endpoint changed.")
        return access
    @property
    def port(self):return self.access["port"]
    @property
    def token(self):return self.access["token"]
    def state(self):
        a = self.access
        value = local.http_json(a["port"], "/state", token=a["token"], timeout=5)
        if value["runtime_id"] != self.identity or value["supervisor_pid"] != a["pid"]:
            raise ValueError("Supervisor response identity mismatch.")
        return value
    snapshot = state
    def operate(self, request):
        a = self.access
        return local.http_json(a["port"], "/operate", request, a["token"], timeout=20)
    def infer(self, target=None):
        a = self.access
        return local.http_json(a["port"], "/infer" if target is None else "/probe?target="+target, token=a["token"])
    @property
    def workers(self):return {k:SimpleNamespace(port=v) for k,v in self.state()["worker_endpoints"].items()}
    @property
    def events(self):
        a = self.access
        return local.http_json(a["port"], "/events", token=a["token"])["events"]
    def close(self):
        a=self.access;deadline=time.monotonic()+20;attempts=0;acknowledged=False;next_attempt=0;last_error=None
        while time.monotonic()<deadline:
            if (self.directory/'teardown.json').exists():
                result=read(self.directory,'teardown.json')
                if not result['all_workers_stopped']:raise OSError('Owned worker teardown remains unconfirmed.')
                if not native_process.wait_for_exit(a['pid'],a['os_identity'],max(0,deadline-time.monotonic()),observer=os_process):
                    raise OSError('Workers stopped, but supervisor exit/file release is not confirmed.')
                return result['workers']
            if not acknowledged and attempts<3 and time.monotonic()>=next_attempt:
                facts=os_process(a['pid'],a['os_identity'])
                if not facts['exists'] or facts['identity']!=a['os_identity'] or facts['alive'] is False:
                    raise startup_error('Original supervisor exited before teardown was recorded',self.directory)
                attempts+=1
                try:
                    local.http_json(a['port'],'/teardown',{'runtime_id':self.identity},a['token'],timeout=2)
                    acknowledged=True
                except (OSError,ValueError,http.client.HTTPException) as exc:last_error=type(exc).__name__
                next_attempt=time.monotonic()+.25
            time.sleep(.025)
        raise startup_error('Runtime teardown outcome is unconfirmed; last transport error '+str(last_error),self.directory)

    def fail_worker(self,target):
        """Trusted development harness only; never exposed by the runtime API."""
        if target not in local.TARGETS:raise ValueError("Unknown harness worker.")
        rows=sqlite3.connect((self.directory/"runtime.sqlite3").as_uri()+"?mode=ro",uri=True)
        try:row=rows.execute("SELECT identity FROM workers WHERE target=?",(target,)).fetchone()
        finally:rows.close()
        if not row:raise ValueError("No registered harness worker.")
        folder=self.directory/"workers"/row[0];config=read(folder,"config.json");ready=read(folder,"ready.json")
        local.http_json(ready["port"],"/stop",{"identity":config["identity"],"runtime_id":self.identity},config["token"])
        end=time.monotonic()+5
        while time.monotonic()<end:
            if observe(folder)["exit_confirmed"]:return
            time.sleep(.025)
        raise OSError("Harness worker exit remains unconfirmed.")


def launch(directory, ttl=900, restart_existing=False, crash_at=None, crash_tool=None):
    directory = Path(directory).absolute()
    if not restart_existing:
        directory.mkdir(parents=True, exist_ok=False)
        if type(ttl) is not int or not 1 <= ttl <= 3600:raise ValueError("Use a bounded runtime lease.")
        workspace.write_json(directory/"runtime.json", {"version":VERSION,"runtime_id":uuid.uuid4().hex,
            "source_digest":source_digest(),"expires_unix":time.time()+ttl,"token":secrets.token_urlsafe(32)})
        (directory/"runtime.json").chmod(0o600)
    else:
        meta = read(directory,"runtime.json")
        if meta["source_digest"] != source_digest() or meta["expires_unix"]<=time.time():raise ValueError("Source or lease changed; restart blocked.")
        if (directory/"teardown.json").exists():raise ValueError("A torn-down runtime cannot be restarted.")
        try:
            existing=Runtime(directory)
            existing.state()
            return existing, None
        except (OSError, ValueError, http.client.HTTPException):
            if (directory/".access.json").exists():
                a=read(directory,".access.json");facts=os_process(a["pid"],a["os_identity"])
                if facts["exists"] and facts["alive"] and facts["identity"]==a["os_identity"]:
                    raise OSError("The original supervisor is alive but unreachable; no competing supervisor started.")
    command=[restart.worker_interpreter(),"-u",str(Path(__file__).resolve()),"--serve",str(directory)]
    if crash_at:command += ["--crash-at",crash_at,"--crash-tool",crash_tool]
    process=spawn_child(command,directory/"supervisor.log")
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if process.poll() is not None:raise startup_error('Durable supervisor startup failed',directory,process.returncode)
        if (directory/".access.json").exists():
            try:
                runtime=Runtime(directory)
                if runtime.state()["supervisor_pid"]==process.pid:return runtime,process
            except (OSError, ValueError, http.client.HTTPException):pass
        time.sleep(.05)
    raise OSError("Durable runtime readiness is unknown; independent worker leases remain bounded.")


class Engine(local.LocalEngine):
    def _live_guard(self, state, actual, tool):
        if tool != "release_capacity":return super()._live_guard(state,actual,tool)
        sim.require(actual["candidate_percent"]==state["routing"]["candidate-v2"] and actual["route_owner"]==state["route_owner"], "RUNTIME_STATE_DRIFT","Physical routing changed.")
        worker=actual["workers"].get("candidate-v2",{})
        sim.require(worker.get("alive") is False and worker.get("exit_confirmed") is True,"STOP_NOT_CONFIRMED","OS lifetime exit, independently of an exit code, is required.")

    def reconcile(self,key=None):
        pending=self.pending()
        if not pending:return {"status":"NOT_NEEDED","code":"NO_PENDING_OPERATION"}
        key=key or pending[0][0]
        row=self.db.execute("SELECT fingerprint,request,changes,operation FROM local_intents WHERE action_key=? AND status='PENDING'",(key,)).fetchone()
        if row is None:raise ValueError("Unknown pending operation.")
        fingerprint,request,changes,operation=row[0],*[workspace.decode(v) for v in row[1:]]
        try:actual=self.client.state()
        except (OSError,ValueError,http.client.HTTPException):return {"status":"OUTCOME_UNKNOWN","code":"OBSERVATION_UNAVAILABLE"}
        receipt=actual["receipts"].get(operation["operation_id"])
        if not receipt or receipt.get("fingerprint")!=sim.digest(operation) or receipt["status"]=="PENDING":return {"status":"OUTCOME_UNKNOWN","code":"RECONCILIATION_REQUIRED"}
        if receipt["status"]=="SUCCEEDED":
            worker=actual["workers"].get("candidate-v2",{})
            matched=actual["revision"]==receipt["revision"]
            matched=matched and (worker.get("alive") is True if operation["tool"]=="start_candidate" else worker.get("exit_confirmed") is True and worker.get("alive") is False if operation["tool"]=="stop_candidate" else actual["candidate_percent"]==operation["percent"] and actual["route_owner"]==operation["owner"])
            if not matched:return {"status":"OUTCOME_UNKNOWN","code":"RUNTIME_STATE_DRIFT"}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous=self.db.execute("SELECT result FROM action_receipts WHERE action_key=?",(key,)).fetchone()
            if previous:self.db.execute("COMMIT");return {**workspace.decode(previous[0]),"replayed":True}
            before=self.state();after=copy.deepcopy(before);committed={}
            if receipt["status"]=="SUCCEEDED":
                for field,change in changes.items():
                    if before[field]!=change["before"]:self.db.execute("ROLLBACK");return {"status":"OUTCOME_UNKNOWN","code":"COORDINATOR_STATE_DRIFT"}
                    after[field]=change["after"]
                self.check_invariants(after)
                self.db.execute("UPDATE service_state SET payload=? WHERE id=1",(sim.canonical(after),))
                committed=changes;result={"status":"SUCCEEDED","code":"OK","reason":"Durable receipt and current OS/runtime observation agree.","committed_units":sim.total_commitments(after)}
            else:result={"status":"BLOCKED","code":receipt["code"],"reason":"Durable runtime refused this exact operation."}
            self._receipt(key,fingerprint,request,result,committed,runtime_confirmation={"runtime_id":actual["runtime_id"],"revision":actual["revision"],"operation_id":operation["operation_id"],"worker":actual["workers"].get("candidate-v2")})
            self.db.execute("UPDATE local_intents SET status='CONFIRMED' WHERE action_key=?",(key,));self.db.execute("COMMIT")
            return result
        except BaseException:
            if self.db.in_transaction:self.db.execute("ROLLBACK")
            raise


class RevisionEngine(Engine,service.LocalRevisionEngine):
    pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve",type=Path);parser.add_argument("--worker",type=Path)
    parser.add_argument("--crash-at",choices=("intent","effect","committed"));parser.add_argument("--crash-tool",choices=sorted(local.EFFECTS))
    args=parser.parse_args()
    if args.worker:worker_main(args.worker)
    elif args.serve:serve(args.serve,args.crash_at,args.crash_tool)
    else:parser.error("Use the connected team/session entry point.")


if __name__=="__main__":main()
