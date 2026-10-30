#!/usr/bin/env python3
"""Controlled local workers, HTTP routing, and reconciliation for Orkestryn.

The workers perform a deterministic arithmetic request, not model inference.
Capacity, approvals and actor identities remain fixtures. Processes, HTTP replies,
latency, routing and exit observations are real. No cloud or model calls occur.
"""
from __future__ import annotations

import argparse
import copy
import hmac
import html
import http.client
import json
import os
import platform
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import simulator as sim
from restart_checker import audit_journal, child_env, snapshot as journal_snapshot, worker_interpreter

VERSION = "agent-rehearsal-local-service-v1"
MODE = "REAL LOCAL PROCESSES AND HTTP; FIXTURE CAPACITY AND APPROVALS; NO MODEL CALLS"
ROOT = Path(__file__).resolve().parent
TARGETS = ("stable-v1", "candidate-v2")
EFFECTS = {"start_candidate", "set_route", "restore_route", "stop_candidate"}
# Expectations are specified before execution and never derived from the engine.
CASES = {
    "workflow_failure": ("VERIFIED_RECOVERED", 40, 0, False, True, "deploy-A"),
    "routing_conflict": ("RECOVERY_REVIEW_REQUIRED", 60, 20, True, True, "route-C"),
    "dependency_conflict": ("RECOVERY_REVIEW_REQUIRED", 60, 0, True, True, "deploy-A"),
    "candidate_failure": ("VERIFIED_RECOVERED", 40, 0, False, True, "deploy-A"),
    "fallback_failure": ("RECOVERY_REVIEW_REQUIRED", 60, 10, True, False, "deploy-A"),
    "lost_start_reply": ("VERIFIED_RECOVERED", 40, 0, False, True, "deploy-A"),
    "lost_stop_reply": ("VERIFIED_RECOVERED", 40, 0, False, True, "deploy-A"),
    "stop_unconfirmed": ("RECOVERY_REVIEW_REQUIRED", 60, 0, True, True, "deploy-A"),
}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def http_json(port, path, body=None, token=None, timeout=3):
    """Fixed loopback transport; does not consult proxy environment variables."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Rehearsal-Token"] = token
    try:
        data = None if body is None else sim.canonical(body).encode()
        connection.request("GET" if body is None else "POST", path, data, headers)
        response = connection.getresponse()
        payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("Local response exceeds the size limit.")
        result = json.loads(payload)
        if response.status != 200:
            raise OSError("Local HTTP %s: %s" % (response.status, result.get("error", "request rejected")))
        return result
    finally:
        connection.close()


class JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, value, status=200):
        payload = sim.canonical(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def worker_main(name, identity):
    class Handler(JsonHandler):
        def do_GET(self):
            if self.path not in {"/health", "/infer"}:
                return self.send_json({"error": "Unknown worker endpoint"}, 404)
            self.send_json({"target": name, "identity": identity, "pid": os.getpid(),
                            "healthy": True, "input": 7, "result": 49})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # The pipe is owned only by this runner. EOF ends an orphan after parent death.
    def watch_parent():
        sys.stdin.buffer.read()
        os._exit(0)
    threading.Thread(target=watch_parent, daemon=True).start()
    print(sim.canonical({"pid": os.getpid(), "port": server.server_port,
                         "target": name, "identity": identity}), flush=True)
    server.serve_forever(poll_interval=0.05)


class Worker:
    def __init__(self, name):
        self.name, self.identity = name, uuid.uuid4().hex
        self.process = subprocess.Popen(
            [worker_interpreter(), "-u", str(Path(__file__).resolve()),
             "--worker", name, "--identity", self.identity], cwd=str(ROOT),
            env=child_env(), shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        messages = queue.Queue()
        reader = threading.Thread(target=lambda: messages.put(self.process.stdout.readline()), daemon=True)
        reader.start()
        try:
            notice = json.loads(messages.get(timeout=12))
            if (notice.get("pid") != self.process.pid or notice.get("identity") != self.identity
                    or notice.get("target") != name or type(notice.get("port")) is not int):
                raise RuntimeError("Worker readiness identity mismatch.")
            self.port = notice["port"]
            if not self.health():
                raise RuntimeError("Worker did not become healthy.")
        except BaseException:
            self.close()
            raise
        finally:
            reader.join(timeout=1)

    def request(self, path):
        if self.process.poll() is not None:
            raise OSError("The owned worker has exited.")
        value = http_json(self.port, path, timeout=1)
        if (value.get("identity") != self.identity or value.get("pid") != self.process.pid
                or value.get("target") != self.name):
            raise OSError("Worker response identity mismatch.")
        return value

    def health(self):
        try:
            return self.request("/health").get("healthy") is True
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def stop(self):
        was_alive = self.process.poll() is None
        if was_alive:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return False, False
        return self.process.poll() is not None, was_alive

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class LocalRuntime:
    """Single local supervisor. Its receipts live only for this runtime session.

    Coordinator reopening is supported while this runtime remains alive. Binding
    to a new runtime is refused; a supervisor restart is not silently reconciled.
    Only the harness has access to fault injection and final teardown methods.
    """
    def __init__(self):
        self.identity, self.token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.workers, self.receipts, self.events = {}, {}, []
        self.revision, self.percent, self.owner, self.cursor = 0, 0, None, 0
        self.in_flight = dict.fromkeys(TARGETS, 0)
        self.starts, self.stops = 0, 0
        self.drop_reply_for, self.defer_stop = None, False
        self.hold_requests = False
        self.request_started, self.request_release = threading.Event(), threading.Event()
        self.server = self.thread = None
        try:
            self.workers["stable-v1"] = Worker("stable-v1")
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           kwargs={"poll_interval": 0.05}, daemon=True)
            self.thread.start()
            self.port = self.server.server_port
            self.log("RUNTIME_STARTED", identity=self.identity)
        except BaseException:
            self.close()
            raise

    def log(self, kind, **values):
        self.events.append({"sequence": len(self.events) + 1, "kind": kind, **values})

    def snapshot(self):
        with self.lock:
            workers = {name: {"pid": w.process.pid, "identity": w.identity,
                       "alive": w.process.poll() is None, "exit_code": w.process.poll(),
                       "healthy": w.health(), "in_flight": self.in_flight[name]}
                       for name, w in self.workers.items()}
            return {"runtime_id": self.identity, "revision": self.revision,
                    "candidate_percent": self.percent, "route_owner": self.owner,
                    "workers": workers, "receipts": copy.deepcopy(self.receipts),
                    "candidate_starts": self.starts, "candidate_stops": self.stops}

    def infer(self, target=None):
        with self.lock:
            if target is None:
                target = "candidate-v2" if self.cursor % 100 < self.percent else "stable-v1"
                self.cursor += 1
            if target not in TARGETS or target not in self.workers:
                return {"ok": False, "target": target, "error": "Worker is absent"}
            worker = self.workers[target]
            self.in_flight[target] += 1
        started = time.monotonic()
        try:
            if self.hold_requests and target == "candidate-v2":
                self.request_started.set()
                if not self.request_release.wait(timeout=5):
                    raise OSError("Harness request hold expired.")
            response = worker.request("/infer")
            return {"ok": True, "target": target, "body": response,
                    "latency_ms": round((time.monotonic() - started) * 1000, 3)}
        except (OSError, ValueError, http.client.HTTPException) as exc:
            return {"ok": False, "target": target, "error": type(exc).__name__,
                    "latency_ms": round((time.monotonic() - started) * 1000, 3)}
        finally:
            with self.lock:
                self.in_flight[target] -= 1

    def operate(self, request):
        with self.lock:
            if (not isinstance(request, dict) or set(request) != {
                    "runtime_id", "operation_id", "expected_revision", "tool", "percent", "owner"}
                    or request["runtime_id"] != self.identity
                    or not isinstance(request["operation_id"], str) or not request["operation_id"]
                    or type(request["expected_revision"]) is not int
                    or request["tool"] not in EFFECTS):
                raise ValueError("Invalid runtime operation.")
            key, fingerprint = request["operation_id"], sim.digest(request)
            prior = self.receipts.get(key)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    return {"status": "BLOCKED", "code": "RUNTIME_KEY_CONFLICT"}
                return {**prior, "replayed": True}
            tool = request["tool"]
            try:
                sim.require(request["expected_revision"] == self.revision,
                            "RUNTIME_VERSION_CONFLICT", "Runtime changed after admission.")
                if tool == "start_candidate":
                    sim.require("candidate-v2" not in self.workers, "RESOURCE_EXISTS", "Worker already exists.")
                    self.workers["candidate-v2"] = Worker("candidate-v2")
                    self.starts += 1
                elif tool in {"set_route", "restore_route"}:
                    pct = request["percent"]
                    sim.integer(pct, "percent", 0, 100)
                    sim.require(self.workers["stable-v1"].health(), "FALLBACK_NOT_SAFE", "Actual fallback is unhealthy.")
                    sim.require(pct == 0 or ("candidate-v2" in self.workers and self.workers["candidate-v2"].health()),
                                "CANDIDATE_NOT_READY", "Actual candidate is unhealthy.")
                    self.percent, self.owner = pct, request["owner"]
                elif tool == "stop_candidate":
                    sim.require(self.percent == 0 and self.in_flight["candidate-v2"] == 0,
                                "RESOURCE_STILL_IN_USE", "Actual traffic or requests still need the worker.")
                    sim.require("candidate-v2" in self.workers, "RESOURCE_MISSING", "Owned worker is absent.")
                    if self.defer_stop:
                        result = {"status": "PENDING", "code": "STOP_UNCONFIRMED", "fingerprint": fingerprint}
                        self.receipts[key] = result
                        self.log("OPERATION_PENDING", request=request, result=result)
                        return result
                    stopped, changed = self.workers["candidate-v2"].stop()
                    self.stops += int(changed and stopped)
                    if not stopped:
                        result = {"status": "PENDING", "code": "STOP_UNCONFIRMED", "fingerprint": fingerprint}
                        self.receipts[key] = result
                        self.log("OPERATION_PENDING", request=request, result=result)
                        return result
                self.revision += 1
                result = {"status": "SUCCEEDED", "code": "OK", "fingerprint": fingerprint,
                          "revision": self.revision}
            except sim.Denied as exc:
                result = {"status": "BLOCKED", "code": exc.code, "fingerprint": fingerprint}
            self.receipts[key] = result
            self.log("OPERATION_RESULT", request=request, result=result)
            return result

    def handler(self):
        runtime = self
        class Handler(JsonHandler):
            def authorized(self):
                valid = (self.headers.get("Host") == "127.0.0.1:%s" % runtime.port
                         and not self.headers.get("Origin")
                         and hmac.compare_digest(self.headers.get("X-Rehearsal-Token", ""), runtime.token))
                if not valid:
                    self.send_json({"error": "Local runner authorization required"}, 403)
                return valid

            def do_GET(self):
                if not self.authorized():
                    return
                parts = urlsplit(self.path)
                if parts.path == "/state" and not parts.query:
                    return self.send_json(runtime.snapshot())
                if parts.path == "/infer" and not parts.query:
                    return self.send_json(runtime.infer())
                if parts.path == "/probe":
                    query = parse_qs(parts.query)
                    if set(query) == {"target"} and len(query["target"]) == 1 and query["target"][0] in TARGETS:
                        return self.send_json(runtime.infer(query["target"][0]))
                self.send_json({"error": "Unknown runtime endpoint"}, 404)

            def do_POST(self):
                if not self.authorized():
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if self.path != "/operate" or not 0 < size <= 16384:
                        raise ValueError("Invalid operation body.")
                    self.connection.settimeout(3)
                    request = json.loads(self.rfile.read(size))
                    result = runtime.operate(request)
                    drop = runtime.drop_reply_for == request["tool"] or result["status"] == "PENDING"
                    if drop:
                        runtime.drop_reply_for = None
                        runtime.log("HTTP_REPLY_DROPPED", operation_id=request["operation_id"])
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    self.send_json(result)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    self.send_json({"error": str(exc)}, 400)
        return Handler

    def fail_worker(self, name):
        """Harness only. Terminate only a child this runtime owns."""
        with self.lock:
            worker = self.workers[name]
            if worker.process.poll() is None:
                worker.process.kill()
                worker.process.wait(timeout=5)
            self.log("INJECTED_PROCESS_FAILURE", target=name, pid=worker.process.pid)

    def close(self):
        self.request_release.set()
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=3)
            self.server.server_close()
        cleanup = []
        for name, worker in self.workers.items():
            worker.close()
            cleanup.append({"target": name, "pid": worker.process.pid,
                            "exit_code": worker.process.returncode, "stopped": worker.process.poll() is not None})
        self.log("HARNESS_TEARDOWN", workers=cleanup,
                 note="After outcome capture; teardown is not policy recovery.")
        return cleanup


class RuntimeClient:
    def __init__(self, runtime):
        self.port, self.token, self.identity = runtime.port, runtime.token, runtime.identity

    def state(self):
        result = http_json(self.port, "/state", token=self.token)
        if result.get("runtime_id") != self.identity:
            raise OSError("Runtime session identity mismatch.")
        return result

    def operate(self, request):
        return http_json(self.port, "/operate", request, self.token)

    def infer(self, target=None):
        return http_json(self.port, "/infer" if target is None else "/probe?target=" + target, token=self.token)


class LocalEngine(sim.Engine):
    """Reuse the simulator's policy predicates, with intent/effect/confirmation.

    Only one outstanding external intent is permitted per service. While its
    outcome is unknown, later actions are blocked and commitments are retained.
    The runtime is a trusted single local supervisor, not a cloud IAM boundary.
    """
    def __init__(self, path, client):
        self.client = client
        super().__init__(path)
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS local_binding (id INTEGER PRIMARY KEY CHECK(id=1), runtime_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS local_intents (
                    action_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    request TEXT NOT NULL, changes TEXT NOT NULL,
                    operation TEXT NOT NULL, status TEXT NOT NULL);
            """)
            binding = self.db.execute("SELECT runtime_id FROM local_binding WHERE id=1").fetchone()
            if binding is None:
                if self.state() != sim.initial_state():
                    raise ValueError("Use a fresh local-service database, not a simulation database.")
                self.db.execute("INSERT INTO local_binding VALUES(1,?)", (client.identity,))
            elif binding[0] != client.identity:
                raise ValueError("The saved coordinator belongs to a different runtime session.")
        except BaseException:
            self.close()
            raise

    def fixture_event(self, *args, **kwargs):
        raise ValueError("Synthetic observations are disabled for the local service. Use measured probes.")

    def pending(self):
        return self.db.execute("SELECT action_key FROM local_intents WHERE status='PENDING'").fetchall()

    @staticmethod
    def observations(state, actual):
        view = copy.deepcopy(state)
        view["stable_healthy"] = actual["workers"].get("stable-v1", {}).get("healthy") is True
        d = view["deployments"].get("candidate-v2")
        if d is not None:
            observed = actual["workers"].get("candidate-v2", {})
            d["healthy"] = observed.get("healthy") is True
            d["in_flight"] = observed.get("in_flight", 0)
        return view

    def record_state(self, before, after, event):
        sim.Engine.check_invariants(after)
        changes = {k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]}
        self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(after),))
        self._event({**event, "changes": changes})
        return changes

    def measure(self):
        if self.pending():
            raise ValueError("Reconcile the pending operation before measuring the candidate.")
        samples = [self.client.infer("candidate-v2") for _ in range(100)]
        latencies = sorted(s.get("latency_ms", 0) for s in samples)
        actual = self.client.state()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.pending():
                raise ValueError("An operation began during probing; reconcile it before recording observations.")
            before = self.state()
            after = self.observations(before, actual)
            after["deployments"]["candidate-v2"]["metrics"] = {
                "samples": len(samples), "correctness": sum(s["ok"] and s.get("body", {}).get("result") == 49 for s in samples),
                "error_rate": sum(not s["ok"] for s in samples), "p95_ms": latencies[94],
                "observed_at": after["clock"], "observed_unix": time.time(),
                "source": "100 REAL LOOPBACK HTTP ARITHMETIC REQUESTS; NOT MODEL QUALITY OR GPU PERFORMANCE"}
            self.record_state(before, after, {"kind": "LOCAL_MEASUREMENTS", "samples": samples})
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return after["deployments"]["candidate-v2"]["metrics"]

    def _live_guard(self, state, actual, tool):
        sim.require(actual["candidate_percent"] == state["routing"]["candidate-v2"]
                    and actual["route_owner"] == state["route_owner"],
                    "RUNTIME_STATE_DRIFT", "Observed route differs from the coordinator's confirmed route.")
        if tool == "scale_candidate":
            raise sim.Denied("LOCAL_TOOL_UNSUPPORTED", "This local adapter supports one fixed worker per target.")
        if tool == "set_route":
            metrics = state["deployments"].get("candidate-v2", {}).get("metrics") or {}
            sim.require(0 <= time.time() - metrics.get("observed_unix", 0) <= 30,
                        "METRICS_STALE", "Measure the real worker within 30 seconds of routing.")
        if tool == "release_capacity":
            worker = actual["workers"].get("candidate-v2")
            sim.require(worker is not None and worker["alive"] is False and worker["exit_code"] is not None,
                        "STOP_NOT_CONFIRMED", "The owned OS process must have a confirmed exit.")

    def _receipt(self, key, fingerprint, request, result, changes=None, **extra):
        self._event({"kind": "ACTION", "action_key": key, **request, "result": result,
                     "changes": changes or {}, **extra})
        self.db.execute("INSERT INTO action_receipts VALUES(?,?,?)", (key, fingerprint, sim.canonical(result)))

    def apply(self, actor, run_id, tool, args, action_key, approval_id):
        request = {"actor": actor, "run_id": run_id, "tool": tool, "args": args, "approval_id": approval_id}
        fingerprint = sim.digest(request)
        self.db.execute("BEGIN IMMEDIATE")
        operation = None
        try:
            previous = self.db.execute("SELECT fingerprint,result FROM action_receipts WHERE action_key=?", (action_key,)).fetchone()
            intent = self.db.execute("SELECT fingerprint FROM local_intents WHERE action_key=? AND status='PENDING'", (action_key,)).fetchone()
            if previous or intent:
                prior = previous or intent
                if prior[0] != fingerprint:
                    result = {"status": "BLOCKED", "code": "IDEMPOTENCY_KEY_CONFLICT", "reason": "Action key content differs."}
                else:
                    result = {**json.loads(previous[1]), "replayed": True} if previous else {
                        "status": "OUTCOME_UNKNOWN", "code": "RECONCILIATION_REQUIRED", "reason": "Observe the pending operation before retrying."}
                self._event({"kind": "LOCAL_REPLAY", "action_key": action_key, "result": result})
                self.db.execute("COMMIT")
                return result
            before = self.state()
            try:
                sim.require(isinstance(action_key, str) and bool(action_key), "INVALID_ARGUMENT", "An action key is required.")
                sim.require(not self.pending(), "RECONCILIATION_REQUIRED", "An external operation has an unresolved outcome.")
                sim.require(tool in sim.TOOLS, "UNSUPPORTED_TOOL", "Unsupported tool.")
                sim.require(isinstance(args, dict) and set(args) == sim.TOOLS[tool] | {"service"},
                            "INVALID_ARGUMENT", "Arguments do not match the tool contract.")
                sim.require(args["service"] == sim.SERVICE, "SERVICE_SCOPE_MISMATCH", "Wrong service.")
                actual = self.client.state()
                after = self.observations(before, actual)
                plan = self._approval(after, actor, run_id, approval_id, tool)
                self._live_guard(after, actual, tool)
                reason = self._execute(after, actor, run_id, tool, args, plan)
                self.check_invariants(after)
                changes = {k: {"before": before[k], "after": after[k]} for k in after if after[k] != before[k]}
                if tool in EFFECTS:
                    operation = {"runtime_id": self.client.identity, "operation_id": uuid.uuid4().hex,
                                 "expected_revision": actual["revision"], "tool": tool,
                                 "percent": after["routing"]["candidate-v2"], "owner": after["route_owner"]}
                    self.db.execute("INSERT INTO local_intents VALUES(?,?,?,?,?, 'PENDING')",
                                    (action_key, fingerprint, sim.canonical(request), sim.canonical(changes), sim.canonical(operation)))
                    self._event({"kind": "LOCAL_INTENT", "action_key": action_key, "request": request,
                                 "operation": operation, "note": "Intent committed before external effect."})
                else:
                    self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(after),))
                    result = {"status": "SUCCEEDED", "code": "OK", "reason": reason,
                              "committed_units": sim.total_commitments(after)}
                    self._receipt(action_key, fingerprint, request, result, changes)
            except sim.Denied as exc:
                result = {"status": "BLOCKED", "code": exc.code, "reason": exc.reason}
                self._receipt(action_key, fingerprint, request, result)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                result = {"status": "BLOCKED", "code": "OBSERVATION_UNAVAILABLE", "reason": type(exc).__name__}
                self._receipt(action_key, fingerprint, request, result)
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        if operation is None:
            return result
        self._checkpoint("local_intent_committed", action_key=action_key)
        try:
            self.client.operate(operation)
            self._checkpoint("local_effect_returned", action_key=action_key)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self._event({"kind": "LOCAL_OUTCOME_UNKNOWN", "action_key": action_key, "error": type(exc).__name__})
            return {"status": "OUTCOME_UNKNOWN", "code": "RECONCILIATION_REQUIRED",
                    "reason": "No confirmed reply. Allocation or reservation remains committed."}
        return self.reconcile(action_key)

    def reconcile(self, key=None):
        pending = self.pending()
        if not pending:
            return {"status": "NOT_NEEDED", "code": "NO_PENDING_OPERATION"}
        key = key or pending[0][0]
        row = self.db.execute("SELECT fingerprint,request,changes,operation FROM local_intents WHERE action_key=? AND status='PENDING'", (key,)).fetchone()
        if row is None:
            raise ValueError("No pending operation has this key.")
        fingerprint, request, changes, operation = row[0], *[json.loads(x) for x in row[1:]]
        try:
            actual = self.client.state()
        except (OSError, ValueError, http.client.HTTPException):
            return {"status": "OUTCOME_UNKNOWN", "code": "OBSERVATION_UNAVAILABLE"}
        receipt = actual["receipts"].get(operation["operation_id"])
        confirmed = receipt is not None and receipt.get("fingerprint") == sim.digest(operation)
        if not confirmed or receipt["status"] == "PENDING":
            return {"status": "OUTCOME_UNKNOWN", "code": "RECONCILIATION_REQUIRED"}
        if receipt["status"] == "SUCCEEDED":
            confirmed = actual["revision"] == receipt["revision"]
            worker = actual["workers"].get("candidate-v2")
            if operation["tool"] == "start_candidate":
                confirmed = confirmed and worker is not None and worker["alive"] is True
            elif operation["tool"] == "stop_candidate":
                confirmed = confirmed and worker is not None and worker["alive"] is False and worker["exit_code"] is not None
            else:
                confirmed = confirmed and actual["candidate_percent"] == operation["percent"] and actual["route_owner"] == operation["owner"]
            if not confirmed:
                return {"status": "OUTCOME_UNKNOWN", "code": "RUNTIME_STATE_DRIFT"}
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Reopening or simultaneous reconciliations must not double-commit.
            previous = self.db.execute("SELECT result FROM action_receipts WHERE action_key=?", (key,)).fetchone()
            if previous:
                self.db.execute("COMMIT")
                return {**json.loads(previous[0]), "replayed": True}
            before, committed = self.state(), {}
            after = copy.deepcopy(before)
            if receipt["status"] == "SUCCEEDED":
                for field, change in changes.items():
                    if before[field] != change["before"]:
                        self.db.execute("ROLLBACK")
                        return {"status": "OUTCOME_UNKNOWN", "code": "COORDINATOR_STATE_DRIFT"}
                    after[field] = change["after"]
                self.check_invariants(after)
                self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(after),))
                committed = changes
                result = {"status": "SUCCEEDED", "code": "OK", "reason": "External operation confirmed by runtime receipt and current observation.",
                          "committed_units": sim.total_commitments(after)}
            else:
                result = {"status": "BLOCKED", "code": receipt["code"], "reason": "Runtime rejected the operation without executing its effect."}
            self._receipt(key, fingerprint, request, result, committed, runtime_confirmation={
                "runtime_id": actual["runtime_id"], "revision": actual["revision"],
                "operation_id": operation["operation_id"], "worker": actual["workers"].get("candidate-v2")})
            self.db.execute("UPDATE local_intents SET status='CONFIRMED' WHERE action_key=?", (key,))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise


def expected_case(name):
    outcome, units, pct, candidate, stable, owner = CASES[name]
    return {"recovery": outcome, "committed_units": units, "capacity_limit": 70,
            "candidate_percent": pct, "candidate_alive": candidate, "stable_alive": stable,
            "route_owner": owner, "candidate_starts": 1,
            "pending_operations": int(name == "stop_unconfirmed")}


def case_spec(name):
    return {"version": VERSION, "scenario": name, "expected": expected_case(name),
            "scope": "DEVELOPMENT REGRESSION CASE; FIXED SCRIPTED ACTORS AND FAULT; NO MODEL CALLS"}


def read_spec(path):
    raw = Path(path).read_bytes()
    if len(raw) > 16384:
        raise ValueError("Regression specification is too large.")
    spec = json.loads(raw)
    if (not isinstance(spec, dict) or not isinstance(spec.get("scenario"), str)
            or spec["scenario"] not in CASES or spec != case_spec(spec["scenario"])):
        raise ValueError("Unsupported or modified regression contract. Use an exported case_spec.json.")
    return spec


def checked(result):
    if result["status"] != "SUCCEEDED":
        raise RuntimeError("Scenario setup was denied: " + sim.canonical(result))
    return result


def run_case(directory, name):
    directory.mkdir(parents=True, exist_ok=False)
    specification = case_spec(name)
    write_json(directory / "case_spec.json", specification)  # independent contract first
    runtime, engine, report = None, None, None
    try:
        runtime = LocalRuntime()
        client = RuntimeClient(runtime)
        path = directory / "state.sqlite3"
        engine = LocalEngine(path, client)
        trace, uncertainty = [], []
        checked(sim.action(engine, "reserve_capacity", {"units": 20, "reservation_id": "local-r1", "budget_version": 1}, "A-reserve"))
        if name == "lost_start_reply":
            runtime.drop_reply_for = "start_candidate"
        start = sim.action(engine, "start_candidate", {"candidate_id": "candidate-v2", "reservation_id": "local-r1", "config": "model-config-v2"}, "A-start")
        if name == "lost_start_reply":
            uncertainty.append({"phase": "start reply lost", "result": start, "state": engine.state(), "actual": client.state()})
            engine.close()
            engine = LocalEngine(path, client)
            trace.append({"phase": "reopened coordinator reconciles start", "result": checked(engine.reconcile())})
        else:
            checked(start)
        metrics = engine.measure()
        checked(sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10, "route_version": 1}, "A-route"))
        checked(sim.cost_change(engine))
        denied = sim.action(engine, "reserve_capacity", {"units": 20, "reservation_id": "local-r2", "budget_version": 1}, "A-stale")
        trace.append({"phase": "stale deployment approval", "result": denied})
        if name == "routing_conflict":
            checked(sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 20, "route_version": 2}, "C-route", "C", "route-C"))
        if name == "dependency_conflict":
            checked(sim.action(engine, "add_dependency", {"candidate_id": "candidate-v2", "dependency_id": "C-local-job"}, "C-dependency", "C", "route-C"))
        before = client.state()
        if name == "candidate_failure":
            runtime.fail_worker("candidate-v2")
        elif name == "fallback_failure":
            runtime.fail_worker("stable-v1")
        elif name == "lost_stop_reply":
            runtime.drop_reply_for = "stop_candidate"
        elif name == "stop_unconfirmed":
            runtime.defer_stop = True
        engine._event({"kind": "LOCAL_WORKFLOW_FAILURE", "run_id": "deploy-A", "scenario": name,
                       "note": "Trusted harness trigger; candidate failure is injected only in its named case."})
        recovery = engine.recover()
        if name in {"lost_stop_reply", "stop_unconfirmed"}:
            premature = sim.action(engine, "release_capacity", {"candidate_id": "candidate-v2"},
                                   "premature-release", "RECOVERY", "deploy-A", "AP-recovery-A")
            uncertainty.append({"phase": "stop outcome unconfirmed", "result": recovery, "state": engine.state(),
                                "actual": client.state(), "premature_release": premature})
            engine.close()
            engine = LocalEngine(path, client)
            reconciliation = engine.reconcile()
            trace.append({"phase": "reopened coordinator reconciles stop", "result": reconciliation})
            if name == "lost_stop_reply":
                checked(reconciliation)
                recovery = engine.recover()
        final, actual = engine.state(), client.state()
        routed = [client.infer() for _ in range(100)]
        availability = {"requests": len(routed), "successful": sum(r["ok"] for r in routed),
                        "failed": sum(not r["ok"] for r in routed),
                        "targets": dict(Counter(r["target"] for r in routed)),
                        "scope": "100 real requests after outcome capture, before harness teardown."}
        expected = specification["expected"]
        tests = {
            "recovery outcome matches independent contract": recovery["status"] == expected["recovery"],
            "commitments match independent contract": sim.total_commitments(final) == expected["committed_units"],
            "B budget and version preserved": final["capacity_limit"] == 70 and final["budget_version"] == 2,
            "actual routing matches contract and coordinator": actual["candidate_percent"] == final["routing"]["candidate-v2"] == expected["candidate_percent"],
            "route owner preserved": actual["route_owner"] == final["route_owner"] == expected["route_owner"],
            "actual candidate process matches contract": actual["workers"]["candidate-v2"]["alive"] == expected["candidate_alive"],
            "actual fallback process matches contract": actual["workers"]["stable-v1"]["alive"] == expected["stable_alive"],
            "exactly one candidate process created": actual["candidate_starts"] == 1,
            "pending intents match contract": len(engine.pending()) == expected["pending_operations"],
            "A remains paused": "deploy-A" in final["paused_runs"],
            "stale approval denied": denied["code"] == "STALE_BUDGET_VERSION",
            "real requests follow configured weights": availability["targets"].get("candidate-v2", 0) == expected["candidate_percent"],
            "availability measured independently": availability["successful"] == (10 if name == "fallback_failure" else 100),
            "real arithmetic probes pass promotion gates": metrics["samples"] == 100 and metrics["correctness"] == 100 and metrics["error_rate"] == 0,
            "release requires observed process exit": final["deployments"]["candidate-v2"]["allocated_units"] > 0 or actual["workers"]["candidate-v2"]["exit_code"] is not None,
            "dependency preserved when present": name != "dependency_conflict" or final["deployments"]["candidate-v2"]["dependencies"] == [{"id": "C-local-job", "owner_run": "route-C"}],
            "unconfirmed stop retains allocation": all(x["state"]["deployments"].get("candidate-v2", {}).get("allocated_units") == 20 and x["premature_release"]["code"] == "RECONCILIATION_REQUIRED" for x in uncertainty if "premature_release" in x),
            "lost start retains pending reservation": name != "lost_start_reply" or (uncertainty[0]["result"]["status"] == "OUTCOME_UNKNOWN" and not uncertainty[0]["state"]["deployments"] and uncertainty[0]["state"]["reservations"]["local-r1"]["status"] == "PENDING"),
        }
        tests.update({"journal: " + key: value for key, value in audit_journal(journal_snapshot(path)).items()})
        report = {"version": VERSION, "mode": MODE, "scenario": name, "model_calls": 0,
                  "passed": all(tests.values()), "checks": tests, "expected": expected,
                  "recovery": recovery, "final_state": final, "actual_before_failure": before,
                  "actual_at_outcome": actual, "availability": availability,
                  "metrics": metrics, "trace": trace, "uncertainty": uncertainty,
                  "events": engine.events(), "sqlite_integrity": engine.db.execute("PRAGMA integrity_check").fetchone()[0]}
        report["checks"]["database integrity"] = report["sqlite_integrity"] == "ok"
        report["passed"] = all(report["checks"].values())
    except Exception as exc:
        report = {"version": VERSION, "mode": MODE, "scenario": name, "model_calls": 0,
                  "passed": False, "error": type(exc).__name__ + ": " + str(exc),
                  "expected": specification["expected"], "events": engine.events() if engine else []}
    finally:
        if engine is not None:
            engine.close()
        if runtime is not None:
            cleanup = runtime.close()
            write_json(directory / "runtime_events.json", runtime.events)
            if report is not None:
                report["teardown"] = {"workers": cleanup, "all_workers_stopped": all(x["stopped"] for x in cleanup),
                                      "scope": "Harness cleanup after captured outcome; not counted as recovery."}
        if report is not None:
            write_json(directory / "report.json", report)
    return report


def render_report(summary, reports):
    cards = "".join('<div class="card"><strong>%s</strong><span>%s</span></div>' % (value, html.escape(label)) for value, label in [
        ("%s/%s" % (summary["cases_passed"], summary["cases_total"]), "Reference cases passed"),
        (summary["outcomes"].get("VERIFIED_RECOVERED", 0), "Automatically recovered"),
        (sum(r["passed"] and r.get("recovery", {}).get("status") == "RECOVERY_REVIEW_REQUIRED" for r in reports), "Correctly retained for review"),
        (0, "Model calls")])
    rows = []
    for r in reports:
        name = r["scenario"]
        actual, availability = r.get("actual_at_outcome", {}), r.get("availability", {})
        checks = "".join('<li class="%s">%s — %s</li>' % ("ok" if passed else "bad", "PASS" if passed else "FAIL", html.escape(label)) for label, passed in r.get("checks", {}).items())
        rows.append('<section><h2>%s <small class="%s">%s</small></h2><p>%s</p><div class="facts">'
                    '<span>Committed units <b>%s/70</b></span><span>Candidate traffic <b>%s%%</b></span>'
                    '<span>Actual HTTP success <b>%s/%s</b></span></div><details><summary>Inspect checks and evidence</summary><ul>%s</ul>'
                    '<pre>%s</pre></details><p><a href="%s/case_spec.json">Replay specification</a> · '
                    '<a href="%s/report.json">Full report</a> · <a href="%s/runtime_events.json">Runtime events</a></p></section>' % (
                        html.escape(name.replace("_", " ")), "ok" if r["passed"] else "bad", "PASS" if r["passed"] else "FAIL",
                        html.escape(r.get("recovery", {}).get("status", r.get("error", "No outcome"))),
                        sim.total_commitments(r["final_state"]) if "final_state" in r else "?",
                        actual.get("candidate_percent", "?"), availability.get("successful", "?"), availability.get("requests", "?"),
                        checks, html.escape(json.dumps({k: v for k, v in r.items() if k not in {"events", "uncertainty"}}, indent=2)), name, name, name))
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Orkestryn · Local Service Lab</title><style>' + """
body{margin:0;background:#0b1120;color:#dde7f5;font:16px/1.6 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:42px 24px}h1{font-size:36px;line-height:1.2;color:white}h2{font-size:21px;text-transform:capitalize}small{font-size:12px;margin-left:12px}.muted{color:#aebed4}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:28px 0}.card,section{background:#142034;border:1px solid #2b3d55;border-radius:14px;padding:22px}.card strong{display:block;font-size:30px;color:#91e5d1}.card span{font-size:13px}.facts{display:flex;flex-wrap:wrap;gap:28px;margin:18px 0}.facts b{display:block;color:white}.ok{color:#8fe1b7}.bad{color:#ffaaaa}section{margin:18px 0}a{color:#a7cdff}summary{cursor:pointer;color:#a7cdff}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#0b1120;padding:16px;border-radius:8px}li{margin:6px 0}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:24px 14px}h1{font-size:28px}}
""" + '</style><main><p class="muted">ORKESTRYN / LOCAL SERVICE LAB</p><h1>Recovery against real local workers</h1><p>Each case starts real Python processes and sends real HTTP requests. Capacity units, approvals, operators and dependencies remain controlled fixtures. Workers calculate 7 × 7; they do not run a model.</p>' + cards + '<p class="muted">A passing review case means the coordinator retained a resource as required. It does not mean full recovery or availability. All workers are stopped by the harness after the recorded outcome. No GPU or cloud performance is measured.</p>' + ''.join(rows) + '<p><a href="summary.json">Campaign summary</a> · ' + html.escape(VERSION) + '</p></main></html>'


def campaign(output, names, replay=None):
    directory = Path(output) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    reports = []
    for name in names:
        print("Running local case: " + name, flush=True)
        report = run_case(directory / name, name)
        reports.append(report)
        print("  " + ("PASS" if report["passed"] else "FAIL") + " - " + report.get("recovery", {}).get("status", report.get("error", "No outcome")), flush=True)
    summary = {"version": VERSION, "mode": MODE, "created_at": datetime.now(timezone.utc).isoformat(),
               "platform": platform.system(), "python_version": platform.python_version(), "model_calls": 0,
               "cases_total": len(reports), "cases_passed": sum(r["passed"] for r in reports),
               "passed": all(r["passed"] for r in reports), "scenarios": names,
               "outcomes": dict(Counter(r.get("recovery", {}).get("status", "ERROR") for r in reports)),
               "checks_total": sum(len(r.get("checks", {})) for r in reports),
               "checks_passed": sum(sum(r.get("checks", {}).values()) for r in reports),
               "all_workers_stopped_after_capture": all(r.get("teardown", {}).get("all_workers_stopped", False) for r in reports),
               "replay_source": str(replay) if replay else None,
               "source_sha256": {f: __import__("hashlib").sha256((ROOT / f).read_bytes()).hexdigest() for f in ("local_service.py", "simulator.py", "restart_checker.py")},
               "scope": "Fixed development fixtures. Real processes/HTTP; synthetic capacity/identity. Runtime supervisor must remain alive for reconciliation. Not a GPU/model or distributed reliability benchmark."}
    write_json(directory / "summary.json", summary)
    (directory / "report.html").write_text(render_report(summary, reports), encoding="utf-8")
    return directory, summary


class LocalServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Orkestryn Local ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, routed=True):
        runtime = LocalRuntime()
        self.addCleanup(runtime.close)
        engine = LocalEngine(self.root / (uuid.uuid4().hex + ".sqlite3"), RuntimeClient(runtime))
        self.addCleanup(engine.close)
        checked(sim.action(engine, "reserve_capacity", {"units": 20, "reservation_id": "r", "budget_version": 1}, "reserve"))
        checked(sim.action(engine, "start_candidate", {"candidate_id": "candidate-v2", "reservation_id": "r", "config": "model-config-v2"}, "start"))
        if routed:
            engine.measure()
            checked(sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10, "route_version": 1}, "route"))
        return runtime, engine

    def test_all_real_cases_against_prior_contracts(self):
        for name in CASES:
            with self.subTest(name=name):
                report = run_case(self.root / name, name)
                self.assertTrue(report["passed"], json.dumps(report.get("checks", report.get("error"))))
                self.assertTrue(report["teardown"]["all_workers_stopped"])

    def test_denied_stale_route_has_no_external_effect(self):
        runtime, engine = self.fixture()
        before = runtime.snapshot()
        result = sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 20, "route_version": 1}, "stale-C", "C", "route-C")
        self.assertEqual(result["code"], "ROUTE_VERSION_CONFLICT")
        self.assertEqual(runtime.snapshot()["revision"], before["revision"])
        self.assertFalse(engine.pending())

    def test_real_inflight_request_blocks_stop_until_drained(self):
        runtime, engine = self.fixture()
        runtime.hold_requests = True
        result = []
        thread = threading.Thread(target=lambda: result.append(engine.client.infer("candidate-v2")))
        thread.start()
        try:
            self.assertTrue(runtime.request_started.wait(timeout=3))
            recovery = engine.recover()
            self.assertEqual(recovery["status"], "RECOVERY_REVIEW_REQUIRED")
            self.assertIn("REQUESTS_IN_FLIGHT", [x["code"] for x in recovery["issues"]])
            self.assertTrue(runtime.snapshot()["workers"]["candidate-v2"]["alive"])
        finally:
            runtime.request_release.set()
            thread.join(timeout=5)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["ok"])
        self.assertEqual(engine.recover()["status"], "VERIFIED_RECOVERED")

    def test_pending_intent_is_durable_and_precedes_side_effect(self):
        runtime, engine = self.fixture(False)
        seen = []
        original = engine.client.operate
        def inspect(request):
            other = sim.Engine(engine.path, create=False)
            try:
                seen.append(other.db.execute("SELECT COUNT(*) FROM local_intents WHERE status='PENDING'").fetchone()[0])
                self.assertEqual(other.state()["routing"]["candidate-v2"], 0)
            finally:
                other.close()
            return original(request)
        engine.measure()
        with mock.patch.object(engine.client, "operate", side_effect=inspect):
            checked(sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10, "route_version": 1}, "route-intent"))
        self.assertEqual(seen, [1])

    def test_lost_reply_replay_and_changed_key_never_duplicate_effect(self):
        runtime, engine = self.fixture(False)
        runtime.drop_reply_for = "stop_candidate"
        args = {"service": sim.SERVICE, "candidate_id": "candidate-v2"}
        first = engine.apply("RECOVERY", "deploy-A", "stop_candidate", args, "stop-key", "AP-recovery-A")
        self.assertEqual(first["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(engine.apply("RECOVERY", "deploy-A", "stop_candidate", args, "stop-key", "AP-recovery-A")["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(engine.apply("A", "deploy-A", "stop_candidate", args, "stop-key", "AP-deploy-A")["code"], "IDEMPOTENCY_KEY_CONFLICT")
        checked(engine.reconcile())
        self.assertTrue(engine.apply("RECOVERY", "deploy-A", "stop_candidate", args, "stop-key", "AP-recovery-A")["replayed"])
        self.assertEqual(runtime.stops, 1)

    def test_reconciliation_does_not_erase_durable_pause(self):
        runtime, engine = self.fixture(False)
        engine.measure()
        runtime.drop_reply_for = "set_route"
        result = sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10, "route_version": 1}, "lost-route")
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(engine.recover()["status"], "RECOVERY_REVIEW_REQUIRED")
        checked(engine.reconcile())
        self.assertIn("deploy-A", engine.state()["paused_runs"])
        self.assertEqual(engine.recover()["status"], "VERIFIED_RECOVERED")

    def test_no_stop_claim_when_runtime_observation_unavailable(self):
        runtime, engine = self.fixture(False)
        with mock.patch.object(engine.client, "state", side_effect=OSError("unreachable")):
            recovery = engine.recover()
        self.assertEqual(recovery["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(sim.total_commitments(engine.state()), 60)
        self.assertTrue(runtime.snapshot()["workers"]["candidate-v2"]["alive"])

    def test_reject_synthetic_metrics_and_expired_real_metrics(self):
        runtime, engine = self.fixture(False)
        with self.assertRaises(ValueError):
            engine.fixture_event("OBSERVATIONS")
        engine.measure()
        with mock.patch("local_service.time.time", return_value=time.time() + 31):
            result = sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10, "route_version": 1}, "expired-metrics")
        self.assertEqual(result["code"], "METRICS_STALE")
        self.assertEqual(runtime.percent, 0)

    def test_runtime_session_binding_blocks_false_resume(self):
        runtime, engine = self.fixture(False)
        client = RuntimeClient(runtime)
        client.identity = "different-session"
        with self.assertRaisesRegex(ValueError, "different runtime"):
            LocalEngine(engine.path, client)

    def test_unexpected_runtime_change_remains_unconfirmed(self):
        runtime, engine = self.fixture(False)
        runtime.drop_reply_for = "stop_candidate"
        engine.apply("RECOVERY", "deploy-A", "stop_candidate", {"service": sim.SERVICE, "candidate_id": "candidate-v2"}, "unknown-stop", "AP-recovery-A")
        runtime.revision += 1  # trusted harness emulates an untracked runtime change
        self.assertEqual(engine.reconcile()["code"], "RUNTIME_STATE_DRIFT")
        self.assertEqual(sim.total_commitments(engine.state()), 60)
        self.assertTrue(engine.pending())

    def test_real_backend_version_conflict_does_not_apply_planned_route(self):
        runtime, engine = self.fixture()
        original = engine.client.operate
        def change_revision(request):
            runtime.revision += 1  # external write between intent and effect
            return original(request)
        with mock.patch.object(engine.client, "operate", side_effect=change_revision):
            result = sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 20, "route_version": 2}, "race-C", "C", "route-C")
        self.assertEqual(result["code"], "RUNTIME_VERSION_CONFLICT")
        self.assertEqual(runtime.percent, 10)
        self.assertEqual(engine.state()["routing"]["candidate-v2"], 10)
        self.assertFalse(engine.pending())

    def test_intent_without_effect_never_becomes_an_assumed_success(self):
        runtime, engine = self.fixture(False)
        with mock.patch.object(engine.client, "operate", side_effect=OSError("request never arrived")):
            result = engine.apply("RECOVERY", "deploy-A", "stop_candidate", {"service": sim.SERVICE, "candidate_id": "candidate-v2"}, "absent-effect", "AP-recovery-A")
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertEqual(engine.reconcile()["status"], "OUTCOME_UNKNOWN")
        self.assertTrue(runtime.snapshot()["workers"]["candidate-v2"]["alive"])
        self.assertEqual(sim.total_commitments(engine.state()), 60)

    def test_control_endpoint_requires_token_and_rejects_unsupported_tools(self):
        runtime = LocalRuntime()
        self.addCleanup(runtime.close)
        with self.assertRaises(OSError):
            http_json(runtime.port, "/state")
        with self.assertRaises(OSError):
            http_json(runtime.port, "/operate", {"tool": "shell"}, runtime.token)
        self.assertEqual(runtime.revision, 0)

    def test_replay_contract_validates_and_preserves_source(self):
        path = self.root / "case_spec.json"
        write_json(path, case_spec("workflow_failure"))
        before = path.read_bytes()
        spec = read_spec(path)
        directory, summary = campaign(self.root / "replayed", [spec["scenario"]], path)
        self.assertTrue(summary["passed"])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((directory / "report.html").is_file())
        bad = case_spec("workflow_failure")
        bad["expected"]["committed_units"] = 0
        write_json(path, bad)
        with self.assertRaises(ValueError):
            read_spec(path)

    def test_report_escapes_errors_and_separates_teardown(self):
        report = {"scenario": "workflow_failure", "passed": False, "error": "<script>alert(1)</script>"}
        page = render_report({"cases_passed": 0, "cases_total": 1, "outcomes": {}}, [report])
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("after the recorded outcome", page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *CASES), default="all")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "local_service_runs")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", choices=TARGETS, help=argparse.SUPPRESS)
    parser.add_argument("--identity", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not args.identity:
            parser.error("Worker identity is required.")
        worker_main(args.worker, args.identity)
        return 0
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(LocalServiceTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args.replay and args.scenario != "all":
        parser.error("Use either --replay or --scenario.")
    names = [read_spec(args.replay)["scenario"]] if args.replay else list(CASES) if args.scenario == "all" else [args.scenario]
    directory, summary = campaign(args.output, names, args.replay)
    print("\nLocal service cases: %s/%s passed; checks: %s/%s; model calls: 0" % (
        summary["cases_passed"], summary["cases_total"], summary["checks_passed"], summary["checks_total"]))
    print("Report: " + str(directory / "report.html"))
    if args.open:
        webbrowser.open((directory / "report.html").resolve().as_uri())
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped. Owned workers have been cleaned up; saved evidence is retained.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as exc:
        print("LOCAL SERVICE FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
