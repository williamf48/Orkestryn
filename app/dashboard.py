#!/usr/bin/env python3
r"""Orkestryn: local evidence dashboard and offline Scenario lab.

Save beside simulator.py, agent_runner.py, data_factory.py and live_evaluator.py.
Windows: .\.venv\Scripts\python.exe dashboard.py --open
No extra packages, API keys or model calls. Press Ctrl+C to stop.
The Scenario lab can run simulator.py and data_factory.py on button clicks.
Each run creates new evidence. Use --read-only to disable these launch controls.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import team_workspace
from team_workspace_ui import TEAM_SCRIPT, TEAM_STYLE
import team_evaluation
from team_evaluation_ui import EVALUATION_SCRIPT, EVALUATION_STYLE
import runtime_sessions
from runtime_sessions_ui import SESSION_SCRIPT, SESSION_STYLE
import connected_team
import connected_evaluation
from connected_team_ui import CONNECTED_SCRIPT
import change_coordinator
import coordination_benchmark
from coordination_ui import COORD_SCRIPT,COORD_STYLE
import mission_control
import operations_workspace
from mission_ui import MISSION_SCRIPT,MISSION_STYLE

VERSION = "agent-rehearsal-dashboard-v10"
ROOTS = {"evaluation": "evaluation_runs", "factory": "factory_runs",
         "agent": "agent_runs", "rehearsal": "rehearsal_runs", "review": "review_runs",
         **team_workspace.ROOTS, "team_evaluation": team_evaluation.ROOT_FOLDER,
         "runtime_session": runtime_sessions.FOLDER,"connected":connected_team.FOLDER,
         "connected_evaluation":connected_evaluation.FOLDER,"coordination":change_coordinator.FOLDER,
         "coordination_benchmark":coordination_benchmark.FOLDER,"mission":mission_control.FOLDER,"operations":operations_workspace.FOLDER}
SCENARIOS = {"flagship", "normal_success", "stale_before_write", "routing_conflict"}
MAX_BYTES = 32 * 1024 * 1024
RUNNER_TIMEOUT_SECONDS = 180


class EvidenceError(ValueError):
    pass


class HoldoutClosed(EvidenceError):
    pass


class JobBusy(EvidenceError):
    pass


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise EvidenceError("Invalid evidence identifier.")
    return value


def reject_constant(value):
    raise EvidenceError("Non-finite number in saved evidence.")


def read_text(path):
    if path.stat().st_size > MAX_BYTES:
        raise EvidenceError("Evidence file exceeds the dashboard's 32 MiB read limit.")
    return path.read_text(encoding="utf-8-sig")


def read_json(path):
    return json.loads(read_text(path), parse_constant=reject_constant)


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line, parse_constant=reject_constant)
            for line in read_text(path).splitlines() if line.strip()]


def state_metrics(state):
    stable = state.get("stable_units", 0)
    allocated = sum(d.get("allocated_units", 0) for d in state.get("deployments", {}).values())
    pending = sum(r.get("units", 0) for r in state.get("reservations", {}).values()
                  if r.get("status") == "PENDING")
    return {"stable": stable, "allocated": allocated, "pending": pending,
            "committed": stable + allocated + pending, "limit": state.get("capacity_limit"),
            "routing": state.get("routing", {}), "budget_version": state.get("budget_version"),
            "route_version": state.get("route_version"), "route_owner": state.get("route_owner")}


def replay(report):
    """Reconstruct only recorded changes; never execute a simulator action."""
    state = copy.deepcopy(report.get("initial_state") or {})
    frames = [{"event": {"kind": "INITIAL_STATE", "sequence": 0},
               "state": copy.deepcopy(state), "metrics": state_metrics(state)}]
    for event in report.get("events", []):
        if "initial_state" in event:
            state = copy.deepcopy(event["initial_state"])
        for name, change in event.get("changes", {}).items():
            if "after" in change:
                state[name] = copy.deepcopy(change["after"])
        if event.get("kind") == "RECOVERY_STARTED" and event.get("run_id"):
            paused = state.setdefault("paused_runs", [])
            if event["run_id"] not in paused:
                paused.append(event["run_id"])
        frames.append({"event": event, "state": copy.deepcopy(state), "metrics": state_metrics(state)})
    final = report.get("final_state")
    return {"frames": frames, "journal_matches_final": state == final if final is not None else None,
            "saved_final_state": final, "saved_final_metrics": state_metrics(final) if final else None}


class EvidenceStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, *parts):
        path = self.root.joinpath(*parts).resolve()
        if path != self.root and self.root not in path.parents:
            raise EvidenceError("Evidence path is outside this project.")
        return path

    def directory(self, kind, run):
        if kind not in ROOTS:
            raise EvidenceError("Unknown evidence category.")
        if kind in {"team_evaluation", "runtime_session", "connected", "connected_evaluation","coordination","coordination_benchmark","mission","operations"}:
            path = self.root / ROOTS[kind] / component(run)
            if path.resolve() != path:
                raise EvidenceError("Evaluation/session evidence must stay in its own folder without symbolic links.")
            return path
        return self.path(ROOTS[kind], component(run))

    def file(self, directory, *parts):
        return self.path(directory.relative_to(self.root), *parts)

    def catalog(self):
        catalog = {kind: [] for kind in ROOTS}
        warnings = []
        for kind, folder in ROOTS.items():
            try:
                base = self.path(folder)
                if not base.is_dir():
                    continue
                children = sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                                  key=lambda p: p.name, reverse=True)
                if len(children) > 200:
                    warnings.append(folder + ": showing the 200 newest folders.")
                for child in children[:200]:
                    try:
                        directory = self.directory(kind, child.name)
                        filename = "agent_report.json" if kind == "agent" else "summary.json"
                        if kind=="connected" and not (directory/filename).exists():filename="team_summary.json"
                        path = self.file(directory, filename)
                        if not path.exists():
                            continue
                        data = read_json(path)
                        if kind == "rehearsal":
                            if not isinstance(data, list):
                                raise EvidenceError("Expected a scenario summary list.")
                            summary = {"mode": "SCRIPTED SIMULATION", "scenarios": data}
                        else:
                            if not isinstance(data, dict):
                                raise EvidenceError("Expected a report object.")
                            if kind == "agent":
                                keys = ("mode", "scenario", "task_outcome", "recovery_outcome", "observations",
                                        "model_call_count", "model", "prompt_version", "error")
                                summary = {k: data.get(k) for k in keys}
                            elif kind == "team":
                                keys = ("version", "created_at", "mode", "scenario", "status", "role_requests",
                                        "model_calls", "usage", "outcome", "error", "full_three_role_flow_completed")
                                summary = {k: data.get(k) for k in keys}
                            elif kind in {"runtime_session", "connected","coordination","mission","operations"}:
                                keys = ("version", "created_at", "mode", "scenario", "status", "lifecycle", "model_calls",
                                        "heartbeat_at", "heartbeat_unix", "expires_unix", "metrics", "teardown")
                                summary = {k: data.get(k) for k in keys}
                            else:
                                summary = data
                        catalog[kind].append({"id": child.name, "kind": kind, "summary": summary})
                    except (OSError, ValueError, TypeError) as exc:
                        warnings.append(folder + "/" + child.name + ": " + type(exc).__name__ +
                                        ". Could not read this run; it has not been counted.")
            except (OSError, ValueError) as exc:
                warnings.append(folder + ": " + type(exc).__name__ + ". Could not scan this folder.")
        return {"version": VERSION, "project": str(self.root), "runs": catalog, "warnings": warnings,
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "access": "READ_ONLY", "dashboard_model_calls": 0}

    def run(self, kind, run, scenario="flagship"):
        directory = self.directory(kind, run)
        if kind in {"mission","operations"}:
            value=operations_workspace.read(directory) if kind=="operations" else mission_control.read(directory)
            if not value.get('service'):return {"kind":kind,"id":run,"summary":value,"partial_mission":True}
            snapshot=value['service']['policy_snapshot']
            report={"initial_state":change_coordinator.sim.initial_state(),"final_state":snapshot['state'],"events":snapshot['events'],
                "mode":value['mode'],"scenario":"Three-role supervised mission","task_outcome":value['status'],"assertions":[]}
            return {"kind":kind,"id":run,"summary":value,"report":report,**replay(report)}
        if kind=="coordination":
            value=change_coordinator.saved_view(directory);snapshot=value['policy_snapshot']
            report={"initial_state":change_coordinator.sim.initial_state(),"final_state":snapshot['state'],"events":snapshot['events'],
                "mode":value['mode'],"scenario":"Competing agent requests","task_outcome":value['status'],"assertions":[]}
            return {"kind":kind,"id":run,"summary":value,"report":report,**replay(report)}
        if kind=="coordination_benchmark":return {"kind":kind,"id":run,**coordination_benchmark.read(directory)}
        if kind=="connected" and not (directory/"connected_registration.json").exists():
            summary=runtime_sessions.read_file(directory,"team_summary.json")
            if summary.get("version")!=connected_team.VERSION or not isinstance(summary.get("calls"),list) or len(summary["calls"])>3:
                raise EvidenceError("Incomplete connected evidence has an unsupported format.")
            return {"kind":kind,"id":run,"partial_team":True,"summary":summary,
                "outcome":runtime_sessions.read_file(directory,"team_outcome.json") if (directory/"team_outcome.json").exists() else None,
                "scope":"Recorded partial or deferred team. No persistent owner service is inferred."}
        if kind in {"runtime_session","connected"}:
            data = connected_team.saved_view(directory) if kind=="connected" else runtime_sessions.saved_session(directory)
            snapshot = data["policy_snapshot"]
            report = {"initial_state": runtime_sessions.sim.initial_state(), "final_state": snapshot["state"],
                      "events": snapshot["events"], "mode": data["mode"], "scenario": data["scenario"],
                      "task_outcome": data["status"], "recovery_outcome": (data.get("recovery") or {}).get("status"),
                      "assertions": []}
            return {"kind": kind, "id": run, "session": data, "report": report, "calls": [], **replay(report)}
        if kind=="connected_evaluation":
            summary,rows=connected_evaluation.read_campaign(directory)
            return {"kind":kind,"id":run,"summary":summary,"rows":rows,"historical_protocol":summary["protocol_digest"]!=connected_evaluation.protocol_digest()}
        if kind == "team_evaluation":
            return {"kind": kind, "id": run, **team_evaluation.read_campaign(directory)}
        if kind == "team":
            data = team_workspace.team_view(self.root, run)
            return {**data, **replay(data["report"])}
        if kind == "team_review":
            source = team_workspace.load_source(self.root, kind, run)
            report = source["report"]
            return {"kind": kind, "id": run, "report": report, "summary": source["summary"], "calls": [],
                    "review": {"source_digest": source["source_digest"], "available_decisions": source["available_decisions"],
                               "reason": source["review_reason"]}, **replay(report)}
        if kind == "evaluation":
            summary = read_json(self.file(directory, "summary.json"))
            results = read_jsonl(self.file(directory, "results.jsonl"))
            warnings = []
            attempted = summary.get("counts", {}).get("attempted")
            if attempted != len(results):
                warnings.append("The summary and result count differ. This run may still be writing; refresh before using it.")
            return {"kind": kind, "id": run, "summary": summary, "results": results, "warnings": warnings}
        if kind == "factory":
            summary = read_json(self.file(directory, "summary.json"))
            cases = []
            for split in ("development", "validation"):
                for row in read_jsonl(self.file(directory, split, "scenarios.jsonl")):
                    if row.get("split") != split:
                        raise EvidenceError("A scenario belongs to the wrong split.")
                    cases.append({"id": row["case_id"], "family": row["family"], "split": split})
            return {"kind": kind, "id": run, "summary": summary, "cases": cases}
        if kind == "review":
            report = read_json(self.file(directory, "report.json"))
            calls = []
        elif kind == "agent":
            report = read_json(self.file(directory, "agent_report.json"))
            path = self.file(directory, "model_calls.json")
            calls = read_json(path) if path.exists() else []
            # Show final proposals and observations, not entire prompts or raw model output.
            calls = [{k: c.get(k) for k in ("call_number", "actor", "stage", "proposal", "result", "error")}
                     for c in calls]
        else:
            if scenario not in SCENARIOS:
                raise EvidenceError("Unknown scripted scenario.")
            report = read_json(self.file(directory, scenario + ".json"))
            calls = []
        return {"kind": kind, "id": run, "report": report, "calls": calls, **replay(report)}

    def evaluation_case(self, run, case_id):
        directory = self.directory("team_evaluation", run)
        data = team_evaluation.read_case(directory, component(case_id))
        report = data.get("report")
        if report is None:
            return {**data, "replay": None}
        normalized = {"initial_state": team_evaluation.sim.initial_state(), "final_state": report.get("state"),
            "events": report.get("events", []), "mode": data.get("case_summary", {}).get("mode"),
            "scenario": data["row"]["scenario"], "task_outcome": report.get("stage"),
            "recovery_outcome": report.get("recovery", {}).get("status"), "recovery": report.get("recovery"),
            "assertions": [{"assertion": k.replace("_", " "), "passed": v} for k, v in data["row"]["score"]["checks"].items()]}
        return {**data, "replay": {"report": normalized, "calls": [], **replay(normalized)}}

    def case(self, run, case_id):
        directory = self.directory("factory", run)
        data = read_json(self.file(directory, "cases", component(case_id) + ".json"))
        definition, evaluation = data["definition"], data["evaluation"]
        if definition.get("split") not in {"development", "validation"}:
            raise HoldoutClosed("Holdout case details are reserved and are not exposed by this dashboard.")
        if evaluation.get("split") != definition["split"] or definition.get("case_id") != case_id:
            raise EvidenceError("Case identifiers or splits do not match.")
        report = {"initial_state": definition["initial_state"], "final_state": evaluation["final_state"],
                  "events": evaluation["events"], "assertions": evaluation["checks"],
                  "scenario": definition["family"], "mode": evaluation["mode"],
                  "task_outcome": evaluation["verified_task_outcome"],
                  "recovery_outcome": next((t["result"]["status"] for t in reversed(evaluation["trace"])
                                             if t["kind"] == "recovery"), "NOT_REQUESTED")}
        return {"id": case_id, "kind": "case", "run": run, "split": definition["split"],
                "passed": evaluation["passed"], "preserved_fields": definition["preserved_fields"],
                "report": report, "calls": [], **replay(report)}


class ScenarioLab:
    """One offline child process at a time; only fixed offline runner entry points."""

    def __init__(self, store, enabled=True):
        self.store, self.enabled = store, enabled
        self.lock = threading.RLock()
        self.job = self.process = self.thread = None
        self.closed = False

    def status(self):
        available = {}
        for kind, filename in (("rehearsal", "simulator.py"), ("factory", "data_factory.py"), ("review", "review_workflow.py"),
                               ("team", "agent_team.py"), ("team_review", "team_workspace.py"), ("runtime_session", "runtime_sessions.py"),
                               ("connected","connected_team.py"),("connected_evaluation","connected_evaluation.py"),
                               ("coordination","change_coordinator.py"),("coordination_benchmark","coordination_benchmark.py"),("mission","mission_control.py")):
            try:
                available[kind] = self.store.path(filename).is_file()
            except EvidenceError:
                available[kind] = False
        try:
            available["team_evaluation"] = all(self.store.path(name).is_file() for name in
                ("team_evaluation.py", "team_evaluation_scoring.py", "team_evaluation_set.json"))
        except EvidenceError:
            available["team_evaluation"] = False
        with self.lock:
            return {"enabled": self.enabled and not self.closed, "available": available,
                    "job": copy.deepcopy(self.job), "model_calls": 0}

    def command(self, payload):
        if not isinstance(payload, dict):
            raise EvidenceError("Choose a supported lab action.")
        kind = payload.get("kind")
        if kind == "rehearsal":
            if (set(payload) != {"kind", "scenario"} or not isinstance(payload["scenario"], str)
                    or payload["scenario"] not in SCENARIOS):
                raise EvidenceError("Choose one of the four supported scripted scenarios.")
            filename = "simulator.py"
            args = ["--scenario", payload["scenario"]]
        elif kind == "factory":
            if set(payload) != {"kind", "seed", "variants"}:
                raise EvidenceError("Dataset generation requires a seed and a variant count.")
            if type(payload["seed"]) is not int or not 0 <= payload["seed"] <= 2147483647:
                raise EvidenceError("Seed must be a whole number from 0 to 2147483647.")
            if type(payload["variants"]) is not int or not 5 <= payload["variants"] <= 10:
                raise EvidenceError("The lab supports 5 to 10 variants per family (60 to 120 cases).")
            filename = "data_factory.py"
            args = ["--seed", str(payload["seed"]), "--variants", str(payload["variants"])]
        elif kind=="mission":
            if set(payload)!={"kind"}:raise EvidenceError("Run only the fixed free mission. Live proposals and exact effect approvals stay in the terminal.")
            self.store.directory(kind,"output-check");filename,args="mission_control.py",["demo"]
        elif kind=="coordination":
            if set(payload)!={"kind"}:raise EvidenceError("Run only the fixed free coordination demo; real approvals and live inference stay in the terminal.")
            self.store.directory(kind,"output-check");filename,args="change_coordinator.py",["demo"]
        elif kind=="coordination_benchmark":
            if set(payload)!={"kind"}:raise EvidenceError("Run only the twelve free policy-only scheduling fixtures.")
            self.store.directory(kind,"output-check");filename,args="coordination_benchmark.py",[]
        elif kind == "connected":
            if set(payload)!={"kind","scenario"} or payload.get("scenario") not in {"routing","dependency"}:
                raise EvidenceError("Start only the free five-minute connected routing or dependency team.")
            self.store.directory("connected","output-check")
            filename,args="connected_team.py",["start","--scenario",payload["scenario"],"--ttl","300"]
        elif kind == "connected_evaluation":
            if set(payload)!={"kind","split"} or payload.get("split")!="development":
                raise EvidenceError("Launch only the free ten-case connected development evaluation.")
            self.store.directory("connected_evaluation","output-check")
            filename,args="connected_evaluation.py",["--split","development"]
        elif kind == "runtime_session":
            if set(payload) != {"kind", "scenario"} or payload.get("scenario") not in {"routing", "dependency"}:
                raise EvidenceError("Start only a free five-minute routing or dependency session.")
            self.store.directory("runtime_session", "output-check")
            filename, args = "runtime_sessions.py", ["start", "--scenario", payload["scenario"], "--ttl", "300"]
        elif kind == "team_evaluation":
            if set(payload) != {"kind", "split"} or payload.get("split") != "development":
                raise EvidenceError("The dashboard launches only the free six-case development team evaluation.")
            self.store.directory("team_evaluation", "output-check")  # Reject linked output folders before launching.
            filename, args = "team_evaluation.py", ["--split", "development"]
        elif kind == "team":
            if set(payload) != {"kind", "scenario"} or payload.get("scenario") not in {"routing", "dependency"}:
                raise EvidenceError("Choose a free routing or dependency team rehearsal.")
            filename, args = "agent_team.py", ["--scenario", payload["scenario"]]
        elif kind == "team_review":
            required = {"kind", "source_kind", "source_run", "source_digest", "decision", "note", "confirm_snapshot"}
            if set(payload) != required or payload.get("source_kind") not in team_workspace.ROOTS:
                raise EvidenceError("A snapshot review requires its displayed source, digest, decision and reason.")
            run = component(payload["source_run"])
            if not isinstance(payload["source_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["source_digest"]):
                raise EvidenceError("Use the displayed snapshot digest.")
            if not isinstance(payload["decision"], str) or payload["decision"] not in team_workspace.review.DECISIONS:
                raise EvidenceError("Choose a bounded simulated C decision.")
            if not isinstance(payload["note"], str) or not payload["note"].strip() or len(payload["note"]) > 300:
                raise EvidenceError("Record a reason of 1 to 300 characters.")
            if payload["confirm_snapshot"] is not True:
                raise EvidenceError("Confirm this is a simulated snapshot review with no live effect.")
            source = team_workspace.load_source(self.store.root, payload["source_kind"], run)
            if source["source_digest"] != payload["source_digest"]:
                raise EvidenceError("The source changed after it was displayed. Refresh before reviewing.")
            if payload["decision"] not in source["available_decisions"]:
                raise EvidenceError("This decision is outside the displayed owner's current scope.")
            filename = "team_workspace.py"
            args = ["--root", str(self.store.root), "--source-kind", payload["source_kind"], "--source-run", run,
                    "--source-digest", payload["source_digest"], "--decision", payload["decision"],
                    "--note", payload["note"], "--confirm-snapshot"]
        elif kind == "review":
            filename = "review_workflow.py"
            operation = payload.get("operation")
            if operation == "create":
                if (set(payload) != {"kind", "operation", "scenario"} or not isinstance(payload["scenario"], str)
                        or payload["scenario"] not in {"routing_conflict", "dependency_conflict"}):
                    raise EvidenceError("Choose a routing or dependency review fixture.")
                args = ["--scenario", payload["scenario"]]
            elif operation == "resolve":
                if set(payload) != {"kind", "operation", "source_run", "state_digest", "decision", "note"}:
                    raise EvidenceError("A review decision requires its displayed source, digest, choice and reason.")
                run = component(payload["source_run"])
                if not isinstance(payload["state_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["state_digest"]):
                    raise EvidenceError("Use the displayed review state digest.")
                if not isinstance(payload["decision"], str) or payload["decision"] not in {"retain", "withdraw_route", "release_dependency"}:
                    raise EvidenceError("Choose a supported simulated operator decision.")
                if not isinstance(payload["note"], str) or not payload["note"].strip() or len(payload["note"]) > 300:
                    raise EvidenceError("Record a reason of 1 to 300 characters.")
                directory = self.store.directory("review", run)
                if not self.store.file(directory, "report.json").is_file() or not self.store.file(directory, "state.sqlite3").is_file():
                    raise EvidenceError("The selected review evidence is incomplete.")
                args = ["--source-run", run, "--expected-digest", payload["state_digest"],
                        "--decision", payload["decision"], "--note", payload["note"]]
            else:
                raise EvidenceError("Choose create or resolve for the local review workflow.")
        else:
            raise EvidenceError("This lab launches only offline rehearsals, dataset generation or simulated review.")
        script = self.store.path(filename)
        if not script.is_file():
            raise EvidenceError("Save " + filename + " beside dashboard.py before running this action.")
        output = self.store.path(ROOTS[kind])
        return [sys.executable, "-u", str(script), *args, *([] if kind == "team_review" else ["--output", str(output)])]

    def start(self, payload):
        with self.lock:
            if not self.enabled or self.closed:
                raise EvidenceError("This dashboard was started in read-only mode.")
            if self.job and self.job["status"] in {"QUEUED", "RUNNING"}:
                raise JobBusy("A lab action is already running. Wait for its result before starting another.")
            command = self.command(payload)
            self.job = {"id": uuid.uuid4().hex[:12], "kind": payload["kind"],
                        "parameters": copy.deepcopy(payload), "status": "QUEUED",
                        "mode": ("SCRIPTED LOCAL WORKERS/HTTP; ZERO MODEL CALLS" if payload["kind"] in {"team", "team_evaluation", "runtime_session","connected","connected_evaluation","coordination","mission"} else
                                 "POLICY-ONLY ORDERING FIXTURES; NO WORKERS OR MODEL CALLS" if payload["kind"]=="coordination_benchmark" else
                                 team_workspace.MODE if payload["kind"] == "team_review" else "OFFLINE SCRIPTED SIMULATION"), "model_calls": 0,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_seconds": None, "result": None, "error": None, "log": ""}
            self.thread = threading.Thread(target=self._work, args=(command, copy.deepcopy(payload)), daemon=True)
            self.thread.start()
            return copy.deepcopy(self.job)

    def _saved_result(self, parameters, log):
        kind = parameters["kind"]
        prefix = "Evidence (JSON reports and SQLite journals): " if kind == "rehearsal" else "Evidence: "
        candidates = [line[len(prefix):].strip() for line in log.splitlines() if line.startswith(prefix)]
        if kind in {"team","connected_evaluation"}:
            candidates = [str(Path(line[len("Report: "):].strip()).parent) for line in log.splitlines() if line.startswith("Report: ")]
        if not candidates:
            raise EvidenceError("The runner finished but did not identify its saved evidence folder. Inspect its log.")
        directory = Path(candidates[-1]).resolve()
        if directory.parent != self.store.path(ROOTS[kind]):
            raise EvidenceError("The runner reported an unexpected output location.")
        run = component(directory.name)
        data = self.store.run(kind, run, parameters.get("scenario", "flagship"))
        if kind=="mission":
            value=data['summary']
            if value['phase']!='RECOVERED_SERVICE_ACTIVE' or value['historical_source'] or value['model_calls']!=0 or value['role_requests']!=3 or not value.get('teardown',{}).get('all_workers_stopped'):
                raise EvidenceError("The free mission did not complete its three roles, explicit owner workflow and separate teardown.")
            if mission_control.verify(directory)['status']!='CONSISTENT':raise EvidenceError("Mission evidence audit did not pass.")
            return {"kind":kind,"run":run,"status":value['phase'],"roles":value['role_requests'],"model_calls":0}
        if kind=="coordination":
            value=data['summary'];plan=value['plans'][-1]
            if plan['status']!='COMPLETED' or value['historical_source'] or not value.get('teardown',{}).get('all_workers_stopped'):
                raise EvidenceError("The coordination demo did not produce a completed exact batch and separate teardown.")
            return {"kind":kind,"run":run,"selected":len(plan['choice']['accepted']),"waiting":len(plan['choice']['waiting']),"model_calls":value['model_calls']}
        if kind=="coordination_benchmark":
            s=data['summary']
            if s['status']!='COMPLETED' or s['optimal']!=s['selected'] or not s['benchmark_valid']:raise EvidenceError("The free scheduling benchmark did not complete its reference checks.")
            return {"kind":kind,"run":run,"optimal":s['optimal'],"selected":s['selected'],"model_calls":s['model_calls']}
        if kind in {"runtime_session","connected"}:
            session = data["session"]
            if session["lifecycle"] != "RUNNING" or session["historical_controller"] or session["heartbeat_stale"]:
                raise EvidenceError("The new session did not produce a current running supervisor snapshot.")
            return {"kind": kind, "run": run, "scenario": session["scenario"], "status": session["status"],
                    "expires_unix": session["expires_unix"], "model_calls": 0}
        if kind in {"team_evaluation","connected_evaluation"}:
            summary = data["summary"]
            if summary.get("status") != "COMPLETED" or summary.get("benchmark_valid") is not True or summary.get("task_completed") != summary.get("selected"):
                raise EvidenceError("The team evaluation did not finish with every selected task complete. Inspect the saved results.")
            return {"kind": kind, "run": run, "selected": summary["selected"], "task_completed": summary["task_completed"],
                    "policy_safe": summary["policy_safe"], "model_calls": summary["model_calls"], "split": summary["split"]}
        if kind == "team":
            summary = data["summary"]
            if not summary.get("full_three_role_flow_completed") or data["warnings"]:
                raise EvidenceError("The new team run did not complete consistently. Inspect its saved evidence.")
            return {"kind": kind, "run": run, "scenario": summary["scenario"], "status": summary["status"],
                    "outcome": summary["outcome"], "model_calls": summary["model_calls"]}
        if kind == "team_review":
            return {"kind": kind, "run": run, "root_team_run": data["report"]["root_team_run"],
                    "recovery_outcome": data["report"]["recovery_outcome"], "decision": data["report"]["decision"],
                    "final_metrics": data["saved_final_metrics"], "mode": team_workspace.MODE}
        if kind == "factory":
            summary = data["summary"]
            return {"kind": kind, "run": run, "counts": summary["counts"],
                    "dataset_validated": summary["dataset_validated"],
                    "dataset_digest": summary["dataset_digest"], "split_counts": summary["split_counts"]}
        report = data["report"]
        if kind == "review":
            return {"kind": kind, "run": run, "scenario": report["scenario"],
                    "recovery_outcome": report["recovery_outcome"], "review": report["review"],
                    "final_metrics": data["saved_final_metrics"], "journal_matches_final": data["journal_matches_final"]}
        checks = report.get("assertions", [])
        return {"kind": kind, "run": run, "scenario": parameters["scenario"],
                "task_outcome": report["task_outcome"], "recovery_outcome": report["recovery_outcome"],
                "checks_passed": sum(c.get("passed") is True for c in checks), "checks_total": len(checks),
                "final_metrics": data["saved_final_metrics"], "journal_matches_final": data["journal_matches_final"]}

    def _work(self, command, parameters):
        started = time.monotonic()
        proc, log, result, error, status, returncode = None, "", None, None, "FAILED", None
        try:
            env = os.environ.copy()
            for key in ("NEBIUS_API_KEY", "OPENAI_API_KEY"):
                env.pop(key, None)
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            with self.lock:
                if self.closed:
                    raise EvidenceError("Dashboard closed before the lab action started.")
                self.job["status"] = "RUNNING"
                proc = subprocess.Popen(command, cwd=str(self.store.root), env=env, shell=False,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        start_new_session=os.name != "nt",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                self.process = proc
            try:
                log, _ = proc.communicate(timeout=RUNNER_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._stop_process_tree(proc, force=True)
                log, _ = proc.communicate(timeout=5)
                raise EvidenceError("The offline action exceeded three minutes and was stopped. Partial new evidence is retained.")
            returncode = proc.returncode
            if returncode != 0:
                raise EvidenceError("The runner exited with code " + str(returncode) + ". Inspect the log; previous runs are unchanged.")
            result = self._saved_result(parameters, log)
            status = "COMPLETED"
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, RuntimeError, subprocess.SubprocessError) as exc:
            error = str(exc)
        finally:
            with self.lock:
                if self.closed and status != "COMPLETED":
                    status, error = "STOPPED", "Dashboard stopped while the action was active. Any partial new run remains on disk."
                self.job.update(status=status, result=result, error=error, log=log[-24000:],
                                elapsed_seconds=round(time.monotonic() - started, 2), returncode=returncode,
                                finished_at=datetime.now(timezone.utc).isoformat())
                self.process = None

    @staticmethod
    def _stop_process_tree(proc, force=False):
        """Stop only this dashboard's launched child and its worker descendants."""
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                # The PID comes from our live Popen handle, never saved evidence.
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                result = subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10, shell=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode and proc.poll() is None:
                    proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (OSError, subprocess.SubprocessError):
            if proc.poll() is None:
                proc.kill()

    def close(self):
        with self.lock:
            self.closed = True
            proc, thread = self.process, self.thread
        if proc and proc.poll() is None:
            self._stop_process_tree(proc)
        if thread and thread.is_alive():
            thread.join(timeout=4)
        if proc and proc.poll() is None:
            self._stop_process_tree(proc, force=True)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orkestryn · Evidence workspace</title>
<style>
:root{color-scheme:dark;--bg:#0b1017;--surface:#111a24;--surface2:#15202d;--line:#253243;--text:#eef3fa;--muted:#94a5bb;--green:#89e2ad;--teal:#74cbd5;--blue:#8baefa;--amber:#f1c57c;--red:#ff9b9f;--radius:14px;font:14px/1.55 "Segoe UI",Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text)}button,select,input{font:inherit}button,select{border:1px solid var(--line);border-radius:8px;color:var(--text);background:var(--surface2);padding:9px 13px}button{cursor:pointer}button:hover,select:hover{border-color:var(--teal)}button:disabled{opacity:.4;cursor:default}button:focus-visible,a:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--teal);outline-offset:3px}a{color:var(--teal)}.shell{display:grid;grid-template-columns:228px minmax(0,1fr);min-height:100vh}.sidebar{background:#0e1620;border-right:1px solid var(--line);padding:29px 18px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}.logo{font-size:19px;font-weight:750;letter-spacing:-.5px;padding:0 10px}.mark{color:var(--green);display:inline-block;border:1px solid #385f50;border-radius:6px;padding:1px 6px;margin-right:6px}.brandnote{font-size:10px;letter-spacing:2px;color:var(--muted);padding:9px 11px 31px}.nav{display:grid;gap:8px}.nav button{text-align:left;background:transparent;border-color:transparent;padding:12px}.nav button.active{background:#1a2c2b;border-color:#2c4943;color:var(--green)}.navnum{font-family:Consolas,monospace;margin-right:13px;opacity:.65;font-size:12px}.sidebarfoot{margin-top:auto;padding:25px 10px 0;font-size:11px;color:var(--muted)}.sidebarfoot b{color:var(--text);font-size:12px;display:block;margin-bottom:5px}main{padding:27px 36px 45px;max-width:1650px;width:100%;margin:auto}.topline{display:flex;justify-content:space-between;align-items:center;gap:16px;border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:28px}.eyebrow{letter-spacing:1.9px;text-transform:uppercase;font-size:10px;color:var(--teal);font-weight:700}.topmeta{display:flex;align-items:center;gap:13px}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:7px}.small{font-size:12px;color:var(--muted)}h1{font-size:30px;line-height:1.2;letter-spacing:-1px;margin:6px 0 12px}h2{font-size:17px;letter-spacing:-.3px;margin:0 0 7px}h3{font-size:14px;margin:0 0 7px}p{margin:0 0 12px}.lead{color:var(--muted);max-width:900px;font-size:14px}.hero{display:flex;justify-content:space-between;gap:25px;align-items:flex-start;margin-bottom:22px}.hero>div{min-width:0}.badge{display:inline-block;font-size:10px;font-weight:650;letter-spacing:.25px;border:1px solid var(--line);border-radius:6px;padding:4px 8px;color:var(--muted);white-space:normal;overflow-wrap:anywhere}.good{color:var(--green);background:#10281f;border-color:#2a5340}.warn{color:var(--amber);background:#2b2418;border-color:#665139}.bad{color:var(--red);background:#2d1c25;border-color:#603843}.info{color:var(--teal);background:#142930;border-color:#2c4e5b}.grid{display:grid;gap:15px;margin-bottom:20px}.g4{grid-template-columns:repeat(4,minmax(0,1fr))}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}.card{border:1px solid var(--line);background:var(--surface);border-radius:var(--radius);padding:21px;min-width:0}.metric .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.7px}.metric .value{font-size:31px;font-weight:700;letter-spacing:-1px;margin:7px 0 2px;font-variant-numeric:tabular-nums}.metric .sub{font-size:11px;color:var(--muted)}.accent{color:var(--green)}.panelhead{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:17px}.panelhead h2{margin:0}.stack{display:grid;gap:17px}.row{display:flex;justify-content:space-between;gap:15px;align-items:center}.divider{border-top:1px solid var(--line);margin:17px 0}.callout{border:1px solid var(--line);border-left:3px solid var(--teal);padding:13px 16px;background:#111f2b;border-radius:8px;margin:16px 0;font-size:12px;color:#b6c9da}.callout.warning{border-left-color:var(--amber)}.empty{border:1px dashed #3a4e63;border-radius:var(--radius);padding:34px;color:var(--muted);text-align:center}.empty h2{color:var(--text)}.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:18px 0}.toolbar select{max-width:100%;min-width:210px}.toolbar input[type=search]{background:var(--surface);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:9px 12px;min-width:230px}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px;text-align:left}th{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;font-weight:600;padding:12px;border-bottom:1px solid var(--line)}td{padding:13px 12px;border-bottom:1px solid #1f2b3b;vertical-align:top}tr:last-child td{border-bottom:0}tbody tr:hover{background:#142130}td .small{font-size:11px}.mono,code,pre{font-family:Consolas,"Cascadia Code",monospace}code{font-size:11px;color:#c6d4e7;overflow-wrap:anywhere}pre{font-size:11px;white-space:pre-wrap;word-break:break-word;max-height:390px;overflow:auto;margin:12px 0 0;padding:15px;background:#0b121c;border:1px solid var(--line);border-radius:8px}summary{cursor:pointer;font-size:12px;color:var(--teal);padding:4px 0}.meter{height:10px;background:#283545;border-radius:5px;overflow:hidden;display:flex;margin:11px 0}.meter span{height:100%;display:block}.stable{background:#5d99d1}.allocated{background:#7addaf}.pending{background:#ddb86f}.route-other{background:#a99be4}.legend{display:flex;gap:17px;flex-wrap:wrap;font-size:11px;color:var(--muted)}.swatch{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px}.family{margin:13px 0}.family .meter{height:5px;margin:5px 0}.tabbtn{background:transparent}.tabbtn.active{border-color:#528e7d;color:var(--green)}.linkbutton{padding:4px 0;border:0;background:transparent;color:var(--teal);text-align:left}.replaylayout{display:grid;grid-template-columns:minmax(270px,.85fr) minmax(0,1.6fr);gap:18px}.timeline{max-height:620px;overflow:auto;padding:3px 7px 3px 0}.eventbutton{display:grid;grid-template-columns:26px minmax(0,1fr);width:100%;text-align:left;margin-bottom:7px;background:#0e1721;padding:11px;gap:10px}.eventbutton.active{border-color:var(--green);background:#162a27}.eventnum{font:11px Consolas,monospace;color:var(--muted)}.eventname{font-size:12px;font-weight:650;overflow-wrap:anywhere}.eventsub{font-size:10px;color:var(--muted);margin-top:4px}.eventtag{font-size:10px;line-height:1.2;color:var(--teal);margin-top:4px;overflow-wrap:anywhere}.player{display:flex;align-items:center;gap:10px;margin:16px 0}.player input{accent-color:var(--green);flex:1;min-width:70px}.player button{padding:7px 11px}.checks{list-style:none;padding:0;margin:0}.checks li{padding:9px 0;border-bottom:1px solid var(--line);font-size:12px;display:flex;gap:10px}.checks li:last-child{border:0}.checkmark{color:var(--green)}.checkfail{color:var(--red)}.footer{border-top:1px solid var(--line);padding-top:16px;margin-top:25px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}.loading{padding:50px;text-align:center;color:var(--muted)}.routeblock{background:#13202e;border:1px solid var(--line);border-radius:8px;padding:11px 13px;margin:10px 0}.kicker{font-size:10px;letter-spacing:1px;color:var(--muted);text-transform:uppercase}.big{font-size:24px;font-weight:700}.pillrow{display:flex;gap:7px;flex-wrap:wrap}.muted{color:var(--muted)}.readmore{margin-top:15px}
@media(min-width:1500px){main{padding:35px 52px}}@media(max-width:1100px){.shell{grid-template-columns:190px minmax(0,1fr)}main{padding:22px}.logo{font-size:16px}.g4{grid-template-columns:repeat(2,minmax(0,1fr))}.replaylayout{grid-template-columns:1fr}.timeline{max-height:270px}}@media(max-width:740px){.shell{display:block}.sidebar{position:static;height:auto;padding:14px}.brandnote,.sidebarfoot{display:none}.nav{display:flex;overflow:auto;margin-top:12px;gap:3px}.nav button{white-space:nowrap;padding:8px}.navnum{display:none}.g2,.g3{grid-template-columns:1fr}.topmeta .small{display:none}.hero{display:block}.hero .badge{margin-bottom:10px}h1{font-size:25px}main{padding:18px}.card{padding:16px}.metric .value{font-size:26px}}
.labscenario{display:flex;flex-direction:column}.labscenario .primary{margin-top:auto;align-self:flex-start}.primary{background:#17382c;border-color:#457b61;color:#a4efc3;font-weight:650}.primary:hover{background:#214736}.toolbar input[type=number]{background:var(--surface2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:9px 12px;width:115px}.logo{font-size:15px;padding:0 4px;white-space:nowrap}.logo .mark{font-size:17px}
.reviewnote{display:block;width:100%;margin-top:8px;padding:11px;border:1px solid var(--line);border-radius:8px;background:var(--surface2);color:var(--text);font:inherit;resize:vertical}
</style></head><body>
<div class="shell"><aside class="sidebar"><div class="logo"><span class="mark">O</span> Orkestryn</div><div class="brandnote">VERIFY BEFORE CHANGE</div>
<nav class="nav" aria-label="Workspace"><button data-tab="operations"><span class="navnum">6·9·12</span>Operations evidence</button><button data-tab="mission"><span class="navnum">6·9·12</span>Mission control</button><button data-tab="connected" class="active"><span class="navnum">6·9·12</span>Connected team</button><button data-tab="coordination"><span class="navnum">09</span>Coordination lab</button><button data-tab="coordination_benchmark"><span class="navnum">11</span>Ordering evaluation</button><button data-tab="connected_evaluation"><span class="navnum">10</span>Connected evaluation</button><button data-tab="team"><span class="navnum">6·9·12</span>Agent team</button><button data-tab="team_evaluation"><span class="navnum">07</span>Team evaluation</button><button data-tab="runtime_session"><span class="navnum">08</span>Runtime sessions</button><button data-tab="overview"><span class="navnum">01</span>Overview</button><button data-tab="lab"><span class="navnum">02</span>Scenario lab</button><button data-tab="factory"><span class="navnum">03</span>Data factory</button><button data-tab="replay"><span class="navnum">04</span>Rehearsal replay</button><button data-tab="review"><span class="navnum">05</span>Recovery review</button><button data-tab="evaluation"><span class="navnum">06</span>Model evaluation</button></nav>
<div class="sidebarfoot"><b>NVIDIA Nemotron</b>Live inference through Nebius Token Factory.<div class="divider"></div>Saved evidence and free rehearsals.<br>Team runs use local workers and HTTP.<br>Capacity and identities are fixtures.</div></aside>
<main><header class="topline"><div class="eyebrow">Evidence workspace</div><div class="topmeta"><span class="small"><span class="dot"></span><span id="accessmode">Local workspace</span></span><button id="refresh">Refresh evidence</button></div></header><div id="notice" role="status" aria-live="polite"></div><div id="content"><div class="loading">Reading saved runs…</div></div><footer class="footer" id="footer"></footer></main></div>
<script>
"use strict";
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const human = v => String(v ?? "Not recorded").replace(/_/g," ").toLowerCase().replace(/^./,c=>c.toUpperCase());
const fmt = v => typeof v === "number" ? v.toLocaleString() : "—";
const ratio = (a,b) => typeof a === "number" && typeof b === "number" ? fmt(a)+" / "+fmt(b) : "—";
const stamp = id => /^\d{8}T\d{6}Z/.test(id) ? id.slice(0,4)+"-"+id.slice(4,6)+"-"+id.slice(6,8)+" · "+id.slice(9,11)+":"+id.slice(11,13)+":"+id.slice(13,15)+" UTC" : id;
const badge = (v,t="") => `<span class="badge ${t}">${esc(v)}</span>`;
const metric = (label,value,sub,accent=false) => `<div class="card metric"><div class="label">${esc(label)}</div><div class="value ${accent?"accent":""}">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
const evidence = (label,obj) => `<details class="readmore"><summary>${esc(label)}</summary><pre>${esc(JSON.stringify(obj,null,2))}</pre></details>`;
const live = r => String(r?.summary?.mode || "").startsWith("LIVE ");
let catalog=null, tab="connected", selected={evaluation:"",factory:"",replay:""}, detail=null, activeReplay=null, frameIndex=0, playTimer=null, generation=0;
async function api(path,options={}){const r=await fetch(path,{cache:"no-store",...options});const data=await r.json();if(!r.ok)throw new Error(data.error||"Could not read evidence.");return data;}
function stopPlay(){if(playTimer){clearInterval(playTimer);playTimer=null;}}
function notice(text){$("#notice").innerHTML=text?`<div class="callout warning">${esc(text)}</div>`:"";}
function title(kicker,name,text,tag="Recorded evidence"){return `<div class="hero"><div><div class="eyebrow">${esc(kicker)}</div><h1>${esc(name)}</h1><p class="lead">${esc(text)}</p></div>${badge(tag,"info")}</div>`;}
function empty(name,folder){return `<div class="empty"><h2>No ${esc(name)} saved here yet</h2><p>Keep dashboard.py beside your existing project scripts and ${esc(folder)} folder, then refresh.</p><p class="small">Existing runs are read directly. Opening this dashboard makes no model calls.</p></div>`;}
function runOptions(rows,current){return rows.map(r=>`<option value="${esc(r.id)}" ${r.id===current?"selected":""}>${esc(stamp(r.id))} · ${esc(r.summary.dataset?.split || (live(r)?"live model":"offline / scripted"))} · ${esc(r.summary.evaluator_version||r.summary.version||r.summary.recovery_outcome||"")}</option>`).join("");}
function choose(kind,preferred){const rows=catalog.runs[kind];if(!rows.some(r=>r.id===selected[kind]))selected[kind]=(preferred||rows[0])?.id||"";return selected[kind];}
function profile(s){const c=s.request_controls||{};const t=c.extra_body?.chat_template_kwargs?.enable_thinking;return `${s.evaluator_version||"Version not recorded"} · thinking ${t===true?"on":t===false?"off":"unspecified"} · temperature ${c.temperature??"unspecified"}`;}
function evalCard(run,label){if(!run)return `<div class="card"><h2>${esc(label)}</h2><p class="muted">No saved live model result for this split.</p></div>`;const s=run.summary,c=s.counts||{};return `<div class="card"><div class="panelhead"><h2>${esc(label)}</h2>${badge(s.run_status||"Unknown",c.errors||c.not_attempted?"warn":"info")}</div><div class="big">${esc(ratio(c.exact_correct,c.attempted))}<span class="small"> decision + code correct</span></div><p class="small">${esc(stamp(run.id))}</p><div class="pillrow">${badge(`${fmt(c.false_allows)} false allows`,c.false_allows?"bad":"")}${badge(`${fmt(c.false_blocks)} false blocks`,c.false_blocks?"bad":"")}${badge(`${fmt(c.errors)} errors`,c.errors?"bad":"")}</div><p class="small readmore">${esc(profile(s))}</p><button class="linkbutton" data-open-eval="${esc(run.id)}">Inspect this run →</button></div>`;}
function overview(){const rs=catalog.runs;const dev=rs.evaluation.find(r=>live(r)&&r.summary.dataset?.split==="development");const val=rs.evaluation.find(r=>live(r)&&r.summary.dataset?.split==="validation");const f=rs.factory[0]?.summary,fc=f?.counts||{};const a=rs.agent.find(live);const ac=a?.summary;const c=val?.summary.counts||{};
$("#content").innerHTML=title("Coordination · governance · recovery","Rehearse change. Inspect the evidence.","Explore how operators share capacity, how policy gates admit or block requests, and how recovery preserves legitimate work.")+
`<div class="grid g4">${metric("Generated scenario checks",ratio(fc.cases_passed,fc.cases_evaluated),"Latest factory · scripted reference checks")}${metric("Validation decision + code",ratio(c.exact_correct,c.attempted),"Latest live validation · sampled checkpoints",true)}${metric("Other-operator work preserved",ratio(fc.preservation_passed,fc.preservation_cases),"Latest factory · preservation checks")}${metric("Live rehearsal recovery",ac?human(ac.recovery_outcome):"No run",ac?`${ac.model_call_count??"?"} recorded model calls · simulated service`:"Run evidence appears here automatically")}</div>`+
`<div class="grid g2">${evalCard(dev,"Development batch")}${evalCard(val,"Validation batch")}</div><div class="callout">Scores refer to the saved admission-classification batches shown above. Each attempted call, including an error, stays in its accuracy denominator. Numeric scenario groups differ across splits; templates are shared.</div>`+
`<div class="grid g3"><div class="card"><div class="eyebrow">01 · Data factory</div><h2 class="readmore">Generate situations to rehearse</h2><p class="small">Bounded scenario templates cover capacity, approvals, routing, dependencies and recovery. Expected labels are checked against the coordinator.</p><button data-tab="factory" class="linkbutton">Explore the scenarios →</button></div><div class="card"><div class="eyebrow">02 · Governed changes</div><h2 class="readmore">Trace every admitted action</h2><p class="small">Follow recorded capacity reservations, candidate changes and routing updates. See the actor, policy result and exact state change.</p><button data-tab="replay" class="linkbutton">Open rehearsal replay →</button></div><div class="card"><div class="eyebrow">03 · Model evaluation</div><h2 class="readmore">Measure the model's decisions</h2><p class="small">Compare Nemotron predictions with reference decisions, including wrong policy codes, invalid responses and earlier failed runs.</p><button data-tab="evaluation" class="linkbutton">Review evaluation history →</button></div></div>`+
`<div class="card"><div class="panelhead"><h2>Evidence on disk</h2>${badge("No API calls from this dashboard","good")}</div><div class="grid g4" style="margin:0">${Object.entries(rs).map(([k,v])=>`<div><span class="big">${v.length}</span><div class="small">${esc(human(k))} run folders</div></div>`).join("")}</div></div>`;}
function evalSummary(s){const c=s.counts||{};const good=c.attempted>0&&c.exact_correct===c.attempted&&!c.errors&&!c.not_attempted;return `<div class="row"><div class="pillrow">${badge(s.mode||"Mode not recorded",String(s.mode).startsWith("LIVE ")?"info":"warn")}${badge(s.dataset?.split||"Split not recorded")}${badge(s.run_status||"Unknown",good?"good":"warn")}</div><button data-download>Export displayed evidence</button></div><p class="small readmore">${esc(profile(s))} · output-token cap ${fmt(s.max_output_tokens_per_call)}</p><div class="grid g4">${metric("Decisions correct",ratio(c.decision_correct,c.attempted),"All attempted calls")}${metric("Decision + code correct",ratio(c.exact_correct,c.attempted),"Exact reference match",good)}${metric("False allows",ratio(c.false_allows,c.expected_invalid_attempted),"Invalid requests incorrectly allowed")}${metric("False blocks",ratio(c.false_blocks,c.expected_valid_attempted),"Valid requests incorrectly blocked")}</div><p class="small">Usable predictions: ${esc(ratio(c.valid_predictions,c.attempted))} · errors: ${fmt(c.errors)} · not attempted: ${fmt(c.not_attempted)} · known total tokens: ${fmt(c.tokens?.total_tokens?.known_sum)} · mean attempt: ${c.mean_attempt_seconds==null?"—":esc(c.mean_attempt_seconds)+"s"}</p>${s.stop_reason?`<div class="callout warning">${esc(s.stop_reason)}</div>`:""}`;}
async function evaluation(ticket){const rows=catalog.runs.evaluation,id=choose("evaluation",rows.find(r=>live(r)&&r.summary.dataset?.split==="validation"));$("#content").innerHTML=title("Live model evidence","Admission decisions under test","Inspect saved Nebius / Nemotron classifications. These calls classify checkpoints; they do not execute infrastructure changes.")+(id?`<div class="toolbar"><label for="evalrun">Saved run</label><select id="evalrun">${runOptions(rows,id)}</select></div><div id="evalbody" class="loading">Reading results…</div>`:empty("evaluations","evaluation_runs"));if(!id)return;const data=await api(`/api/run?kind=evaluation&id=${encodeURIComponent(id)}`);if(ticket!==generation)return;detail=data;$("#evalbody").className="";$("#evalbody").innerHTML=evalSummary(data.summary)+data.warnings.map(w=>`<div class="callout warning">${esc(w)}</div>`).join("")+`<div class="toolbar"><input id="resultsearch" type="search" aria-label="Search results" placeholder="Search family, code or reason"><select id="resultfilter" aria-label="Result filter"><option value="all">All results</option><option value="issues">Errors and mismatches</option><option value="MATCH">Exact matches</option></select></div><div class="card tablewrap"><table><thead><tr><th>Checkpoint</th><th>Reference</th><th>Nemotron prediction</th><th>Result</th><th>Final explanation / error</th></tr></thead><tbody id="resultrows"></tbody></table></div><div class="callout">This is a stratified diagnostic sample. A small batch score is not an estimate of production reliability. Token totals reflect reported usage; billed cost is not inferred.</div>${evidence("Run settings, provenance and counts",data.summary)}`;renderResults();}
function renderResults(){const search=($("#resultsearch")?.value||"").toLowerCase(),filter=$("#resultfilter")?.value||"all";const rows=(detail.results||[]).filter(r=>(filter==="all"||filter==="issues"&&r.outcome!=="MATCH"||filter===r.outcome)&&JSON.stringify(r).toLowerCase().includes(search));$("#resultrows").innerHTML=rows.map(r=>`<tr><td><strong>${esc(human(r.family))}</strong><div class="small mono">${esc(r.row_id)}</div></td><td>${esc(r.expected?.status)}<div class="small mono">${esc(r.expected?.code)}</div></td><td>${esc(r.prediction?.status||"No usable prediction")}<div class="small mono">${esc(r.prediction?.code||"")}</div></td><td>${badge(r.outcome,r.outcome==="MATCH"?"good":"bad")}</td><td>${esc(r.prediction?.reason||r.error||"No final explanation")}</td></tr>`).join("")||'<tr><td colspan="5" class="muted">No results match this filter.</td></tr>';}
async function factory(ticket){const rows=catalog.runs.factory,id=choose("factory");$("#content").innerHTML=title("Synthetic evaluation data","A factory for difficult situations","Inspect bounded scenario templates and independently specified checks. Generation and reference validation use scripted simulation.")+(id?`<div class="toolbar"><label for="factoryrun">Dataset</label><select id="factoryrun">${runOptions(rows,id)}</select></div><div id="factorybody" class="loading">Reading dataset…</div>`:empty("datasets","factory_runs"));if(!id)return;const data=await api(`/api/run?kind=factory&id=${encodeURIComponent(id)}`);if(ticket!==generation)return;detail=data;const s=data.summary,c=s.counts||{},families=Object.entries(s.family_counts||{}),max=Math.max(1,...families.map(f=>f[1]));$("#factorybody").className="";$("#factorybody").innerHTML=`<div class="row"><div class="pillrow">${badge("Scripted reference simulation","warn")}${badge(s.dataset_validated?"Dataset checks passed":"Dataset checks failed",s.dataset_validated?"good":"bad")}</div><button data-download>Export displayed evidence</button></div><div class="grid g4 readmore">${metric("Cases passed",ratio(c.cases_passed,c.cases_evaluated),"Full reference scenarios")}${metric("Invalid actions rejected",ratio(c.invalid_actions_correctly_rejected,c.expected_invalid_actions),"Correct rejection and policy code")}${metric("Other-operator work preserved",ratio(c.preservation_passed,c.preservation_cases),"Legitimate changes kept during recovery")}${metric("Human review correctly required",ratio(c.review_correctly_required,c.expected_review_attempts),"Recovery stopped at a supported boundary")}</div><div class="grid g2"><div class="card"><h2>Scenario coverage</h2>${families.map(([k,v])=>`<div class="family"><div class="row small"><span>${esc(human(k))}</span><span>${fmt(v)} cases</span></div><div class="meter"><span class="allocated" style="width:${100*v/max}%"></span></div></div>`).join("")}</div><div class="card"><h2>Evaluation splits</h2><p class="small">Numeric groups are separated. Scenario templates are shared.</p>${Object.entries(s.split_counts||{}).map(([k,v])=>`<div class="routeblock row"><div><strong>${esc(human(k))}</strong><div class="small">${k==="holdout"?"Reserved · details closed":k==="development"?"Prompt development":"Separate validation values"}</div></div><span class="big">${fmt(v)}</span></div>`).join("")}<div class="divider"></div><p class="small">Actions reached: ${esc(ratio(c.actions_evaluated,c.expected_actions_total))}<br>Recovery outcomes matched: ${esc(ratio(c.recovery_labels_matched,c.recovery_attempts))}<br>Invalid actions admitted: ${fmt(c.invalid_actions_admitted)}<br>False blocks: ${fmt(c.false_blocks)}</p>${evidence("Dataset provenance and limitations",s)}</div></div><div class="card"><h2>Inspect a saved scenario</h2><div class="toolbar"><select id="casesplit" aria-label="Scenario split"><option>development</option><option>validation</option></select><select id="casechoice" aria-label="Scenario"></select><button id="inspectcase">Replay scenario</button></div><p class="small">Opening a saved scenario makes no model calls and does not rerun the coordinator.</p><div id="casebody"></div></div>`;caseOptions();}
function caseOptions(){const split=$("#casesplit").value;$("#casechoice").innerHTML=detail.cases.filter(c=>c.split===split).map(c=>`<option value="${esc(c.id)}">${esc(human(c.family))} · ${esc(c.id.slice(-8))}</option>`).join("");}
function replayChoices(){let rows=[];for(const r of catalog.runs.agent)rows.push({key:"agent|"+r.id,label:`${live(r)?"Live model":"Offline fixture"} · ${stamp(r.id)} · ${human(r.summary.recovery_outcome)}`});for(const r of catalog.runs.rehearsal)for(const s of r.summary.scenarios||[])if(s.scenario)rows.push({key:"rehearsal|"+r.id+"|"+s.scenario,label:`Scripted · ${human(s.scenario)} · ${stamp(r.id)}`});return rows;}
async function rehearsals(ticket){const choices=replayChoices();if(!choices.some(c=>c.key===selected.replay))selected.replay=choices.find(c=>c.label.startsWith("Live model"))?.key||choices[0]?.key||"";$("#content").innerHTML=title("Coordination and recovery","Replay the infrastructure rehearsal","Follow recorded proposals, policy decisions and state changes. Capacity and traffic represent the simulated service.")+(selected.replay?`<div class="toolbar"><label for="replayrun">Recorded run</label><select id="replayrun">${choices.map(c=>`<option value="${esc(c.key)}" ${c.key===selected.replay?"selected":""}>${esc(c.label)}</option>`).join("")}</select></div><div id="replaybody" class="loading">Reading event journal…</div>`:empty("rehearsals","agent_runs or rehearsal_runs"));if(!selected.replay)return;const [kind,id,scenario="flagship"]=selected.replay.split("|");const data=await api(`/api/run?kind=${kind}&id=${encodeURIComponent(id)}&scenario=${scenario}`);if(ticket!==generation)return;detail=data;$("#replaybody").className="";renderReplay(data,"#replaybody");}
const LAB_TOKEN = "__LAB_SESSION_TOKEN__";
let labState=null, labTimer=null, labSubmitting=false;
const LAB_SCENARIOS = [
  {id:"flagship", name:"Recover after a capacity change", kicker:"Deploy A + cost B", text:"A launches a candidate. B lowers the shared capacity limit. A's old approval becomes stale, then the candidate fails.", expected:"Restore stable traffic and release the stopped candidate while keeping B's limit of 70 units."},
  {id:"routing_conflict", name:"Preserve another operator's route", kicker:"Deploy A + cost B + route C", text:"C makes a separately approved routing change before A's candidate fails. Recovery encounters work owned by another workflow.", expected:"Keep C's 20% route and retain the candidate allocation. Require human review to resolve the conflict."},
  {id:"stale_before_write", name:"Catch stale approval before a write", kicker:"Approval version check", text:"B changes the limit before A reserves capacity. A submits a request using its earlier approval.", expected:"Block A before any candidate or reservation is created. Recovery is unnecessary."},
  {id:"normal_success", name:"Complete the approved rollout stage", kicker:"Valid workflow", text:"A reserves capacity, launches a healthy candidate and expands traffic within the approved plan.", expected:"Reach the approved 50% traffic stage with 80 of 100 capacity units committed."}
];
async function lab(ticket){
  $("#content").innerHTML=title("Interactive simulation","Choose a situation. Run a rehearsal.","Exercise the coordinator with scripted operators and injected observations. Each action creates new saved evidence, with no model calls or API credits.","Offline Scenario lab")+'<div class="loading" id="labloading">Checking local runners…</div>';
  const data=await api("/api/lab");if(ticket!==generation)return;labState=data;
  $("#labloading").outerHTML=`<div class="callout">Operator A manages the rollout. B manages the shared capacity limit. C can own a separate routing change. Recovery compensates the failed workflow while respecting other operators' work.</div>
    <div class="grid g2">${LAB_SCENARIOS.map(s=>`<div class="card labscenario"><div class="eyebrow">${esc(s.kicker)}</div><h2 class="readmore">${esc(s.name)}</h2><p class="small">${esc(s.text)}</p><div class="routeblock"><div class="kicker">Expected reference behavior</div><p class="small" style="margin:5px 0 0">${esc(s.expected)}</p></div><button class="primary" data-run-scenario="${s.id}">Run scripted rehearsal</button></div>`).join("")}</div>
    <div class="card"><div class="panelhead"><div><div class="eyebrow">Evaluation data factory</div><h2 class="readmore">Generate a fresh dataset</h2></div>${badge("12 template families","info")}</div><p class="small">A seed selects numeric fixtures reproducibly. This generates and checks scenario data; the saved Nemotron evaluation runs remain separate.</p><div class="toolbar"><label for="labseed">Seed</label><input id="labseed" type="number" min="0" max="2147483647" step="1" value="43"><label for="labvariants">Dataset size</label><select id="labvariants"><option value="5">60 cases · 5 variants per family</option><option value="10">120 cases · 10 variants per family</option></select><button class="primary" id="generate-cases">Generate dataset</button></div><p class="small">Development, validation and reserved holdout splits are saved. Holdout case details remain closed in this dashboard.</p></div><div id="labjob" class="readmore" role="status" aria-live="polite"></div>`;
  updateLabJob();if(["QUEUED","RUNNING"].includes(labState.job?.status))scheduleLabPoll(ticket);
}
function updateLabJob(){
  if(tab!=="lab"||!$("#labjob")||!labState)return;
  const j=labState.job,busy=labSubmitting||["QUEUED","RUNNING"].includes(j?.status);
  document.querySelectorAll("[data-run-scenario]").forEach(b=>b.disabled=busy||!labState.enabled||!labState.available.rehearsal);
  $("#generate-cases").disabled=busy||!labState.enabled||!labState.available.factory;
  let warnings="";
  if(!labState.enabled)warnings+='<div class="callout warning">Read-only mode is active. Restart the dashboard without --read-only to enable the Scenario lab.</div>';
  if(!labState.available.rehearsal)warnings+='<div class="callout warning">Save simulator.py beside dashboard.py to enable scripted rehearsals.</div>';
  if(!labState.available.factory)warnings+='<div class="callout warning">Save data_factory.py beside dashboard.py to enable dataset generation.</div>';
  if(!j){$("#labjob").innerHTML=warnings+'<div class="empty"><h2>Ready for a new rehearsal</h2><p>Choose a scenario above. Its observed result and replay link will appear here.</p></div>';return;}
  const r=j.result,name=j.kind==="factory"?"Dataset generation":j.kind==="review"?"Recovery review":LAB_SCENARIOS.find(s=>s.id===j.parameters.scenario)?.name||"Rehearsal";
  let result="";
  if(r?.kind==="rehearsal"){
    const m=r.final_metrics;
    result=`<div class="grid g3 readmore">${metric("Reference checks",ratio(r.checks_passed,r.checks_total),"Observed simulator assertions")}${metric("Final shared capacity",ratio(m.committed,m.limit),"Committed / limit · simulated units")}${metric("Recovery outcome",human(r.recovery_outcome),"Observed deterministic recovery")}</div><p class="small">Task outcome: ${esc(human(r.task_outcome))} · stable traffic: ${fmt(m.routing["stable-v1"])}% · candidate traffic: ${fmt(m.routing["candidate-v2"])}%</p>${r.recovery_outcome==="RECOVERY_REVIEW_REQUIRED"?'<div class="callout warning">This scenario deliberately leaves a routing conflict for human review. Preserving another operator\'s work is the expected reference behavior; the service is not claimed to be fully recovered.</div>':""}<button class="primary" id="view-lab-result">View this rehearsal →</button>`;
  }else if(r?.kind==="factory"){
    const c=r.counts;
    result=`<div class="grid g3 readmore">${metric("Cases checked",ratio(c.cases_passed,c.cases_evaluated),"Scripted reference checks")}${metric("Invalid actions rejected",ratio(c.invalid_actions_correctly_rejected,c.expected_invalid_actions),"Correct rejection and policy code")}${metric("Other-operator work preserved",ratio(c.preservation_passed,c.preservation_cases),"Observed preservation checks")}</div><p class="small">These results validate the generated reference scenarios. No Nemotron accuracy has been measured on this new dataset by this action.</p><button class="primary" id="view-lab-result">Explore this dataset →</button>`;
  }
  if(r?.kind==="review"){
    const m=r.final_metrics;
    result=`<div class="grid g2 readmore">${metric("Shared capacity",ratio(m.committed,m.limit),"Current saved review branch")}${metric("Recovery outcome",human(r.recovery_outcome),"Observed deterministic recovery")}</div><button class="primary" id="view-lab-result">Open recovery review →</button>`;
  }
  $("#labjob").innerHTML=warnings+`<div class="card"><div class="panelhead"><div><div class="eyebrow">Latest lab action this session</div><h2 class="readmore">${esc(name)}</h2></div>${badge(j.status,j.status==="COMPLETED"?"good":j.status==="FAILED"||j.status==="STOPPED"?"bad":"info")}</div><p class="small">${j.elapsed_seconds==null?"Running the local reference workflow…":esc(j.elapsed_seconds)+" seconds"} · 0 model calls · ${esc(human(j.mode))}</p>${j.error?`<div class="callout warning">${esc(j.error)}</div>`:""}${result}${r?`<p class="small readmore">Saved run: <code>${esc(r.run)}</code></p>`:""}${j.log?evidence("Runner output",j.log):""}</div>`;
}
function scheduleLabPoll(ticket){
  if(labTimer)clearTimeout(labTimer);
  labTimer=setTimeout(async()=>{try{const data=await api("/api/lab");if(ticket!==generation||tab!=="lab")return;labState=data;updateLabJob();if(["QUEUED","RUNNING"].includes(data.job?.status))scheduleLabPoll(ticket);}catch(e){if(ticket===generation&&tab==="lab")notice("Could not refresh the lab status. Use Refresh evidence to reconnect. "+e.message);}},700);
}
async function startLab(payload){
  if(labSubmitting||["QUEUED","RUNNING"].includes(labState?.job?.status))return;
  labSubmitting=true;updateLabJob();notice("");const ticket=generation;
  try{const data=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Rehearsal-Token":LAB_TOKEN},body:JSON.stringify(payload)});if(ticket!==generation||tab!=="lab")return;labState.job=data.job;updateLabJob();scheduleLabPoll(ticket);}catch(e){if(ticket===generation)notice(e.message);}finally{labSubmitting=false;updateLabJob();}
}
document.addEventListener("click",async e=>{
  const b=e.target.closest("button");if(!b)return;
  if(b.dataset.runScenario){await startLab({kind:"rehearsal",scenario:b.dataset.runScenario});}
  else if(b.id==="generate-cases"){
    const raw=$("#labseed").value,seed=Number(raw),variants=Number($("#labvariants").value);
    if(raw.trim()===""||!Number.isInteger(seed)||seed<0||seed>2147483647){notice("Enter a whole-number seed from 0 to 2147483647.");return;}
    await startLab({kind:"factory",seed,variants});
  }else if(b.id==="view-lab-result"){
    const r=labState?.job?.result;if(!r)return;
    if(r.kind==="factory"){selected.factory=r.run;tab="factory";}else if(r.kind==="review"){selected.review=r.run;tab="review";}else{selected.replay=`rehearsal|${r.run}|${r.scenario}`;tab="replay";}
    await refresh();
  }
});


let reviewTimer=null,reviewSubmitting=false,reviewData=null,reviewHandledJob="";
const REVIEW_LABELS={retain:"Keep resource and defer cleanup",withdraw_route:"Record C's route withdrawal",release_dependency:"Record C's dependency release"};
async function reviews(ticket){
  const id=choose("review"),rows=catalog.runs.review||[];
  $("#content").innerHTML=title("Owner consent and recovery","Resolve a recovery conflict","Inspect retained work, record a simulated operator C decision, and let the coordinator recheck cleanup. Each decision creates a new saved simulation branch.","Offline simulated review")+
    `<div class="callout">This is a local review rehearsal. Operator identities and approvals are fixtures. A recorded decision is simulated owner consent; it is not real manager authentication.</div><div class="grid g2"><div class="card"><h2>Later route owned by C</h2><p class="small">C's 20% route keeps the candidate allocated. Inspect the conflict before recording whether C retains or withdraws it.</p><button class="primary" data-create-review="routing_conflict">Create routing review</button></div><div class="card"><h2>Legitimate dependency owned by C</h2><p class="small">A dependency keeps the candidate allocated after traffic is restored. Review whether C still needs this work.</p><button class="primary" data-create-review="dependency_conflict">Create dependency review</button></div></div><div id="reviewjob" role="status" aria-live="polite"></div>`+
    (id?`<div class="toolbar"><label for="reviewrun">Saved review branch</label><select id="reviewrun">${runOptions(rows,id)}</select></div><div id="reviewbody" class="loading">Reading review evidence…</div>`:'<div class="empty"><h2>Create a review case above</h2><p>The current conflict, available decisions and complete journal will appear here.</p></div>');
  const status=await api("/api/lab");if(ticket!==generation)return;labState=status;updateReviewJob(ticket);
  if(!id)return;
  const data=await api(`/api/run?kind=review&id=${encodeURIComponent(id)}`);if(ticket!==generation)return;
  reviewData=detail=data;const r=data.report,v=r.review,s=r.final_state,m=data.saved_final_metrics;
  $("#reviewbody").className="";
  $("#reviewbody").innerHTML=`<div class="card"><div class="panelhead"><div><div class="eyebrow">Current review state</div><h2 class="readmore">${esc(human(r.scenario))}</h2></div>${badge(r.recovery_outcome,r.recovery_outcome==="VERIFIED_RECOVERED"?"good":"warn")}</div><div class="grid g3">${metric("Committed / limit",ratio(m.committed,m.limit),"Current saved simulation state")}${metric("Route owner",m.route_owner||"None",`Route version ${m.route_version}`)}${metric("Budget version",m.budget_version,"B's approved limit is retained")}</div><p class="small">Stable traffic: ${fmt(s.routing["stable-v1"])}% · candidate traffic: ${fmt(s.routing["candidate-v2"])}% · paused workflow: ${esc(s.paused_runs.join(", "))}</p><h3>Why cleanup stopped</h3><ul>${(r.recovery.issues||[]).map(i=>`<li><code>${esc(i.code)}</code> · ${esc(i.reason)}</li>`).join("")||'<li>Recovery postconditions are verified.</li>'}</ul>${v.source_run?`<p class="small">Parent branch: <code>${esc(v.source_run)}</code></p>`:""}${v.decision?`<div class="callout ${v.decision.result.status==="BLOCKED"?"warning":""}"><strong>${esc(REVIEW_LABELS[v.decision.choice]||human(v.decision.choice))}</strong><br>${esc(v.decision.note)}<br>${esc(v.decision.result.status)} · ${esc(v.decision.result.code)} · ${esc(v.decision.result.reason)}</div>`:""}${v.available_decisions.length?`<div class="divider"></div><h3>Record simulated C's decision</h3><label for="reviewnote" class="small">Reason for this decision (required)</label><textarea id="reviewnote" maxlength="300" rows="3" class="reviewnote" placeholder="Explain why C still needs this work, or why C can withdraw it."></textarea><div class="toolbar">${v.available_decisions.map(d=>`<button class="${d==="retain"?"":"primary"}" data-review-decision="${d}">${esc(REVIEW_LABELS[d])}</button>`).join("")}</div><p class="small">This decision applies to the displayed state. A changed state or journal is blocked for a fresh review. Cleanup still checks traffic, dependencies, current permissions and confirmed stop.</p>`:'<div class="callout">This branch is recovered. Create a new review case to rehearse another decision.</div>'}</div><div class="readmore" id="reviewreplay"></div>`;
  renderReplay(data,"#reviewreplay");updateReviewJob(ticket);
}
function updateReviewJob(ticket){
  if(tab!=="review"||!$("#reviewjob")||!labState)return;
  const j=labState.job,busy=reviewSubmitting||["QUEUED","RUNNING"].includes(j?.status),enabled=labState.enabled&&labState.available.review;
  document.querySelectorAll("[data-create-review],[data-review-decision]").forEach(b=>b.disabled=busy||!enabled);
  $("#reviewjob").innerHTML=(!enabled?'<div class="callout warning">Review controls require the complete package and a dashboard started without --read-only.</div>':"")+(!j?"":`<div class="callout ${j.status==="FAILED"?"warning":""}">${badge(j.status)} ${esc(j.error||(["RUNNING","QUEUED"].includes(j.status)?"Running the offline workflow…":"Latest offline action saved."))}${j.log?evidence("Runner output",j.log):""}</div>`);
  if(["QUEUED","RUNNING"].includes(j?.status)){
    if(reviewTimer)clearTimeout(reviewTimer);
    reviewTimer=setTimeout(async()=>{try{const status=await api("/api/lab");if(ticket!==generation||tab!=="review")return;labState=status;updateReviewJob(ticket);}catch(e){if(ticket===generation)notice(e.message);}},700);
  }else if(j?.status==="COMPLETED"&&j.result?.kind==="review"&&reviewHandledJob!==j.id){
    reviewHandledJob=j.id;selected.review=j.result.run;refresh();
  }
}
async function startReview(payload){
  if(reviewSubmitting||["QUEUED","RUNNING"].includes(labState?.job?.status))return;
  reviewSubmitting=true;const ticket=generation;updateReviewJob(ticket);notice("");
  try{const data=await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json","X-Rehearsal-Token":LAB_TOKEN},body:JSON.stringify(payload)});if(ticket!==generation||tab!=="review")return;labState.job=data.job;updateReviewJob(ticket);}catch(e){if(ticket===generation)notice(e.message);}finally{reviewSubmitting=false;updateReviewJob(ticket);}
}
document.addEventListener("click",async e=>{
  const b=e.target.closest("button");if(!b)return;
  if(b.dataset.createReview){await startReview({kind:"review",operation:"create",scenario:b.dataset.createReview});}
  else if(b.dataset.reviewDecision&&reviewData){
    const note=$("#reviewnote")?.value||"";
    if(!note.trim()||note.length>300){notice("Record a reason of 1 to 300 characters before choosing a review decision.");return;}
    await startReview({kind:"review",operation:"resolve",source_run:reviewData.id,state_digest:reviewData.report.review.state_digest,decision:b.dataset.reviewDecision,note});
  }
});

function checks(report){const items=report.assertions||Object.entries(report.observations||{}).map(([label,v])=>({label:human(label),passed:typeof v==="boolean"?v:null,value:v}));return `<ul class="checks">${items.map(c=>`<li><span class="${c.passed===true?"checkmark":c.passed===false?"checkfail":"muted"}">${c.passed===true?"✓":c.passed===false?"!":"·"}</span><span>${esc(c.assertion||c.label||c.name||c.check||"Check")}${c.passed==null?" · "+esc(c.value??""):""}</span></li>`).join("")}</ul>`;}
function eventName(e){return human(e.tool||e.fixture_type||e.kind);}
function eventWho(e){if(e.kind==="REVIEW_DECISION"||e.kind==="REVIEW_REPLAY")return "Simulated operator C review";if(e.kind==="TEST_HARNESS")return "Injected / synthetic fixture";if(e.actor==="RECOVERY"||String(e.kind).startsWith("RECOVERY"))return "Deterministic recovery";return e.actor?"Operator "+e.actor:e.kind==="INITIAL_STATE"?"Saved starting state":"Coordinator journal";}
function renderReplay(data,target){stopPlay();activeReplay=data;frameIndex=0;const r=data.report;$(target).innerHTML=`<div class="row"><div class="pillrow">${badge(r.mode||"Saved simulation",String(r.mode).startsWith("LIVE ")?"info":"warn")}${badge(r.recovery_outcome||"No recovery recorded",r.recovery_outcome==="VERIFIED_RECOVERED"?"good":r.recovery_outcome==="RECOVERY_REVIEW_REQUIRED"?"warn":"")}</div><button data-download-replay>Export replay evidence</button></div><p class="small readmore">Scenario: ${esc(human(r.scenario))} · task outcome: ${esc(human(r.task_outcome))}</p>${r.error?`<div class="callout warning">${esc(r.error)}</div>`:""}<div class="player"><button id="previous" aria-label="Previous event">←</button><button id="play">Play journal</button><input id="scrub" type="range" min="0" max="${data.frames.length-1}" value="0" aria-label="Event position"><button id="next" aria-label="Next event">→</button><span class="small" id="position"></span></div><div class="replaylayout"><div class="card"><div class="panelhead"><h2>Event journal</h2>${badge(`${data.frames.length-1} events`)}</div><div class="timeline">${data.frames.map((f,i)=>`<button class="eventbutton" data-event="${i}"><span class="eventnum">${String(i).padStart(2,"0")}</span><span><span class="eventname">${esc(eventName(f.event))}</span><div class="eventsub">${esc(eventWho(f.event))}</div><div class="eventtag">${esc(f.event.result?.code||f.event.result?.status||f.event.report?.status||"")}</div></span></button>`).join("")}</div></div><div id="stateview"></div></div><div class="callout ${data.journal_matches_final===false?"warning":""}">${data.journal_matches_final===true?"The reconstructed journal state matches the saved final state.":"The reconstructed journal does not match the saved final state. Inspect the recorded final snapshot below before using this replay as evidence."} Playback shows recorded events; it does not repeat actions or make model calls.</div><div class="grid g2"><div class="card"><h2>Recorded checks and observations</h2>${checks(r)}</div><div class="card"><h2>Saved final state</h2><p class="small">This snapshot comes directly from the run report, separately from playback.</p>${evidence("Final state and recovery report",{final_state:data.saved_final_state,recovery:r.recovery||null,preserved_fields:data.preserved_fields||null})}${data.calls?.length?evidence("Recorded final model proposals",data.calls):""}</div></div>`;renderFrame();}
function renderFrame(){if(!activeReplay)return;const f=activeReplay.frames[frameIndex],s=f.state,m=f.metrics,limit=m.limit,total=m.committed,base=Math.max(total,limit||0,1);const percent=n=>Math.max(0,Math.min(100,n/base*100));const routes=Object.entries(m.routing||{});const deps=Object.entries(s.deployments||{});$("#position").textContent=`${frameIndex} / ${activeReplay.frames.length-1}`;$("#scrub").value=frameIndex;$("#previous").disabled=frameIndex===0;$("#next").disabled=frameIndex===activeReplay.frames.length-1;document.querySelectorAll("[data-event]").forEach(b=>{b.classList.toggle("active",Number(b.dataset.event)===frameIndex);b.setAttribute("aria-pressed",Number(b.dataset.event)===frameIndex?"true":"false");});
$("#stateview").innerHTML=`<div class="stack"><div class="card"><div class="panelhead"><h2>${esc(eventName(f.event))}</h2>${badge(f.event.result?.status||eventWho(f.event),f.event.result?.status==="BLOCKED"?"warn":"info")}</div><p class="small">${esc(f.event.result?.reason||f.event.reason||f.event.label||"Recorded state at this point in the journal.")}</p><div class="row"><span class="kicker">Shared capacity</span><span><strong>${fmt(total)}</strong><span class="muted"> / ${fmt(limit)} units</span></span></div><div class="meter"><span class="stable" style="width:${percent(m.stable)}%"></span><span class="allocated" style="width:${percent(m.allocated)}%"></span><span class="pending" style="width:${percent(m.pending)}%"></span></div><div class="legend"><span><i class="swatch stable"></i>Stable ${fmt(m.stable)}</span><span><i class="swatch allocated"></i>Candidate ${fmt(m.allocated)}</span><span><i class="swatch pending"></i>Pending ${fmt(m.pending)}</span></div><div class="divider"></div><div class="row"><span class="kicker">Traffic allocation</span><span class="small">Route version ${fmt(m.route_version)}</span></div><div class="meter">${routes.map(([k,v],i)=>`<span class="${k==="stable-v1"?"stable":"route-other"}" style="width:${Math.max(0,Math.min(100,Number(v)||0))}%"></span>`).join("")}</div><div class="legend">${routes.map(([k,v],i)=>`<span><i class="swatch ${k==="stable-v1"?"stable":"route-other"}"></i>${esc(k)} ${fmt(v)}%</span>`).join("")}</div><div class="routeblock row"><div><div class="kicker">Budget version</div><strong>${fmt(m.budget_version)}</strong></div><div><div class="kicker">Route owner</div><strong>${esc(m.route_owner||"Baseline / none")}</strong></div><div><div class="kicker">Paused workflows</div><strong>${esc((s.paused_runs||[]).join(", ")||"None")}</strong></div></div>${limit!=null&&total>limit?'<div class="callout warning">Recorded commitments exceed the recorded capacity limit.</div>':""}</div><div class="card"><h2>Candidate resources</h2>${deps.length?`<div class="tablewrap"><table><thead><tr><th>Candidate / owner</th><th>Status</th><th>Units</th><th>Dependencies</th></tr></thead><tbody>${deps.map(([k,d])=>`<tr><td>${esc(k)}<div class="small">${esc(d.owner_run)}</div></td><td>${badge(d.status)}</td><td>${fmt(d.allocated_units)}</td><td>${esc((d.dependencies||[]).map(x=>typeof x==="object"?`${x.id} (${x.owner_run})`:x).join(", ")||"None")}</td></tr>`).join("")}</tbody></table></div>`:'<p class="small">No candidate has been created at this point.</p>'}${evidence("Pending and consumed reservations",s.reservations||{})}${evidence("Recorded event and exact state changes",f.event)}</div></div>`;}
function stepTo(n){frameIndex=Math.max(0,Math.min(activeReplay.frames.length-1,n));renderFrame();}
function download(value,name){const blob=new Blob([JSON.stringify(value,null,2)],{type:"application/json"});const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}

async function operationsEvidence(ticket){
 choose("operations");const rows=catalog.runs.operations||[];
 $("#content").innerHTML=title("Operations evidence","Named approvals. Safe handoff.","Inspect saved operations here. Start the separate named-user workspace to create workflows, manage accounts or approve exact effects.","Parts 6 · 9 · 12")+`<div class="card"><code>python project.py operations serve --open</code><p class="small">The named-user workspace has its own loopback address and local sign-in. This evidence view makes no model requests or service changes.</p><label>Saved operation<select id="operationsrun">${rows.map(r=>`<option value="${esc(r.id)}" ${selected.operations===r.id?"selected":""}>${esc(stamp(r.id))}</option>`).join("")||'<option>No operations yet</option>'}</select></label></div><div id="operationsbody"></div>`;
 if(!selected.operations)return;const d=await api(`/api/run?kind=operations&id=${encodeURIComponent(selected.operations)}`);if(ticket!==generation||tab!=="operations")return;
 const s=d.summary,core=s.service,e=s.operations.emergency,p=core?.plans.at(-1);
 $("#operationsbody").innerHTML=`<div class="pillrow">${badge(human(s.phase),"info")}${badge(s.mode,"info")}${badge(s.lifecycle,"info")}</div><div class="grid g3">${metric("Typed requests",core?.intents.length??0,"Up to 32 per workflow")}${metric("Committed / limit",core?`${core.metrics.committed} / ${core.metrics.limit}`:"Pending","Local fixture capacity")}${metric("Role requests",s.role_requests,`${s.model_calls} paid attempts`)}</div><div class="card"><h2>${esc(s.goal)}</h2><p>${esc(s.scope)}</p><p>${p?`${p.comparison.orders_compared} orderings compared. ${p.comparison.global_optimum_proven?'Exhaustive small-batch reference.':'Bounded search; no global optimum claim.'}`:'No plan yet.'}</p>${e?`<p>Emergency handoff: ${esc(human(e.status))}. Manager and C owner approvals: ${esc(Object.keys(e.grants||{}).join(', '))}.</p>`:''}${e?.availability?`<p>${e.availability.successful}/${e.availability.requests} actual routed HTTP requests succeeded after handoff.</p>`:''}${evidence("Scoped approvals and customer jobs",{batch:s.batch_consent,recovery:s.recovery_grant,emergency:e,jobs:s.operations.jobs})}${evidence("Role decisions and notifications",{calls:s.calls,notifications:s.operations.notifications})}${evidence("Measured health windows",s.health)}${evidence("Event ledger",s.events)}</div>`;
}
document.addEventListener('change',async e=>{if(e.target.id==='operationsrun'){selected.operations=e.target.value;await render();}});

async function render(){stopPlay();if(missionTimer){clearTimeout(missionTimer);missionTimer=null;}if(coordinationTimer){clearTimeout(coordinationTimer);coordinationTimer=null;}if(connectedTimer){clearTimeout(connectedTimer);connectedTimer=null;}if(localSessionTimer){clearTimeout(localSessionTimer);localSessionTimer=null;}if(teamEvalTimer){clearTimeout(teamEvalTimer);teamEvalTimer=null;}if(teamTimer){clearTimeout(teamTimer);teamTimer=null;}if(labTimer){clearTimeout(labTimer);labTimer=null;}if(reviewTimer){clearTimeout(reviewTimer);reviewTimer=null;}reviewData=null;activeReplay=null;detail=null;const ticket=++generation;document.querySelectorAll(".nav [data-tab]").forEach(b=>{b.classList.toggle("active",b.dataset.tab===tab);b.setAttribute("aria-current",b.dataset.tab===tab?"page":"false");});try{if(tab==="operations")await operationsEvidence(ticket);else if(tab==="mission")await missionView(ticket);else if(tab==="coordination")await coordinationView(ticket);else if(tab==="coordination_benchmark")await coordinationBenchmark(ticket);else if(tab==="connected")await connectedTeam(ticket);else if(tab==="connected_evaluation")await connectedEvaluation(ticket);else if(tab==="runtime_session")await runtimeSessions(ticket);else if(tab==="team_evaluation")await teamEvaluation(ticket);else if(tab==="team")await teamWorkspace(ticket);else if(tab==="overview")overview();else if(tab==="lab")await lab(ticket);else if(tab==="evaluation")await evaluation(ticket);else if(tab==="factory")await factory(ticket);else if(tab==="review")await reviews(ticket);else await rehearsals(ticket);}catch(e){if(ticket===generation)$("#content").innerHTML=`<div class="callout warning"><strong>Could not read this evidence.</strong><br>${esc(e.message)}<br>Refresh after any active run has finished. No model calls were made by this view.</div>`;}}
async function refresh(){stopPlay();const button=$("#refresh");button.disabled=true;try{catalog=await api("/api/catalog");$("#accessmode").textContent=catalog.lab?.enabled?"Local · free rehearsal workspace":"Local · read only";notice(catalog.warnings.join(" "));$("#footer").textContent=`${catalog.version} · ${catalog.project} · refreshed ${new Date(catalog.refreshed_at).toLocaleTimeString()} · scores and journals are loaded from saved files`;await render();}catch(e){notice(e.message);}finally{button.disabled=false;}}
document.addEventListener("click",async e=>{const b=e.target.closest("button");if(!b)return;if(b.dataset.tab){tab=b.dataset.tab;await render();}else if(b.dataset.openEval){selected.evaluation=b.dataset.openEval;tab="evaluation";await render();}else if(b.id==="refresh"){await refresh();}else if(b.hasAttribute("data-download")){download(detail,"orkestryn-evidence.json");}else if(b.hasAttribute("data-download-replay")){download(activeReplay,"orkestryn-replay.json");}else if(b.dataset.event!==undefined){stopPlay();stepTo(Number(b.dataset.event));$("#play").textContent="Play journal";}else if(b.id==="previous"||b.id==="next"){stopPlay();stepTo(frameIndex+(b.id==="next"?1:-1));$("#play").textContent="Play journal";}else if(b.id==="play"){if(playTimer){stopPlay();b.textContent="Play journal";}else{if(frameIndex===activeReplay.frames.length-1)stepTo(0);b.textContent="Pause";playTimer=setInterval(()=>{if(frameIndex<activeReplay.frames.length-1)stepTo(frameIndex+1);else{stopPlay();$("#play").textContent="Play journal";}},1200);}}else if(b.id==="inspectcase"){stopPlay();const ticket=++generation,run=selected.factory,id=$("#casechoice").value;if(!id)return;$("#casebody").innerHTML='<div class="loading">Reading scenario…</div>';try{const data=await api(`/api/case?run=${encodeURIComponent(run)}&id=${encodeURIComponent(id)}`);if(ticket===generation)renderReplay(data,"#casebody");}catch(err){if(ticket===generation)$("#casebody").innerHTML=`<div class="callout warning">${esc(err.message)}</div>`;}}});
document.addEventListener("change",async e=>{if(e.target.id==="evalrun"){selected.evaluation=e.target.value;await render();}else if(e.target.id==="factoryrun"){selected.factory=e.target.value;await render();}else if(e.target.id==="replayrun"){selected.replay=e.target.value;await render();}else if(e.target.id==="reviewrun"){selected.review=e.target.value;await render();}else if(e.target.id==="casesplit")caseOptions();else if(e.target.id==="resultfilter")renderResults();});
document.addEventListener("input",e=>{if(e.target.id==="resultsearch")renderResults();else if(e.target.id==="scrub"){stopPlay();stepTo(Number(e.target.value));$("#play").textContent="Play journal";}});
refresh();
</script></body></html>'''


# Keep the established evidence views intact; add the team UI to the same local page.
PAGE = PAGE.replace("</style>", TEAM_STYLE + EVALUATION_STYLE + SESSION_STYLE + COORD_STYLE + MISSION_STYLE + "</style>", 1).replace("refresh();\n</script>", TEAM_SCRIPT + EVALUATION_SCRIPT + SESSION_SCRIPT + CONNECTED_SCRIPT + COORD_SCRIPT + MISSION_SCRIPT + "\nrefresh();\n</script>", 1)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "Orkestryn/2"

    def log_message(self, *_):
        pass

    def respond(self, status, payload, html=False):
        body = payload.encode("utf-8") if html else json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def local_origin(self):
        port = self.server.server_port
        hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (not origin or origin in {"http://" + h for h in hosts})

    def do_GET(self):
        if not self.local_origin():
            return self.respond(403, {"error": "Only this local dashboard origin is accepted."})
        parsed = urlsplit(self.path)
        args = parse_qs(parsed.query)
        arg = lambda name, default="": args.get(name, [default])[0]
        store = self.server.evidence
        try:
            if parsed.path == "/":
                return self.respond(200, PAGE.replace("__LAB_SESSION_TOKEN__", self.server.session_token), html=True)
            if parsed.path == "/api/catalog":
                data = store.catalog()
                data["lab"] = self.server.lab.status()
                data["access"] = "SIMULATION_LAB" if data["lab"]["enabled"] else "READ_ONLY"
            elif parsed.path == "/api/lab":
                data = self.server.lab.status()
            elif parsed.path == "/api/run":
                data = store.run(arg("kind"), arg("id"), arg("scenario", "flagship"))
            elif parsed.path == "/api/case":
                data = store.case(arg("run"), arg("id"))
            elif parsed.path == "/api/team-evaluation-case":
                data = store.evaluation_case(arg("run"), arg("id"))
            else:
                return self.respond(404, {"error": "This dashboard serves only its page and saved evidence endpoints."})
            return self.respond(200, data)
        except HoldoutClosed as exc:
            self.respond(403, {"error": str(exc)})
        except FileNotFoundError:
            self.respond(404, {"error": "This saved evidence file is missing. Refresh the run list."})
        except EvidenceError as exc:
            self.respond(400, {"error": str(exc)})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, RuntimeError):
            self.respond(422, {"error": "This evidence file is incomplete or has an unsupported format. Refresh after the run finishes."})

    def do_POST(self):
        endpoint=urlsplit(self.path).path
        if endpoint not in {"/api/jobs","/api/coordination-preview"}:
            return self.respond(405, {"error": "Only the Scenario lab launch endpoint accepts POST."})
        if not self.local_origin() or not secrets.compare_digest(
                self.headers.get("X-Rehearsal-Token", ""), self.server.session_token):
            return self.respond(403, {"error": "Reload this local dashboard before launching a scenario."})
        if endpoint=="/api/jobs" and not self.server.lab.enabled:
            return self.respond(403, {"error": "The dashboard is in read-only mode."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 2048 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise EvidenceError("Send a small JSON request for a supported lab action.")
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=reject_constant)
            if endpoint=="/api/coordination-preview":
                if not isinstance(payload,dict) or set(payload)!={"run","plan_digest","intent_id","units"}:raise EvidenceError("Preview only a copied quantity from the displayed plan.")
                directory=self.server.evidence.directory('coordination',payload['run'])
                result=change_coordinator.preview(directory,payload['plan_digest'],payload['intent_id'],payload['units'])
                return self.respond(200,result)
            job = self.server.lab.start(payload)
            self.respond(202, {"job": job})
        except JobBusy as exc:
            self.respond(409, {"error": str(exc)})
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError) as exc:
            self.respond(400, {"error": str(exc)})

    def unsupported_mutation(self):
        self.respond(405, {"error": "This method is not supported."})

    do_PUT = unsupported_mutation
    do_DELETE = unsupported_mutation
    do_PATCH = unsupported_mutation


def server_for(root, port, read_only=False):
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.daemon_threads = True
    server.evidence = EvidenceStore(root)
    server.session_token = secrets.token_hex(24)
    server.lab = ScenarioLab(server.evidence, enabled=not read_only)
    return server


class DashboardTests(unittest.TestCase):
    def test_session_launch_is_fixed_free_bounded_and_has_no_owner_effect_controls(self):
        (self.root / "runtime_sessions.py").write_text("# fixture", encoding="utf-8")
        lab = ScenarioLab(self.store)
        payload = {"kind": "runtime_session", "scenario": "routing"}
        command = lab.command(payload)
        self.assertIn("start", command)
        self.assertEqual(command[command.index("--ttl") + 1], "300")
        self.assertEqual(command[-1], str(self.root / runtime_sessions.FOLDER))
        for bad in ({**payload, "ttl": 3600}, {**payload, "live": True}, {**payload, "decision": "withdraw_route"},
                    {**payload, "scenario": "other"}, {**payload, "serve": "/outside"}, {**payload, "token": "test"}):
            with self.assertRaises(EvidenceError):
                lab.command(bad)
        outside = self.root / "elsewhere"
        outside.mkdir()
        try:
            (self.root / runtime_sessions.FOLDER).symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(EvidenceError):
            lab.command(payload)

    def test_http_session_can_reconnect_after_dashboard_close_without_exposing_capabilities(self):
        import shutil
        from unittest import mock
        source = Path(__file__).resolve().parent
        for path in source.glob("*.py"):
            shutil.copyfile(path, self.root / path.name)
        server = server_for(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        directory = None
        closed = False
        def get(path):
            with urlopen(base + path, timeout=15) as response:
                return json.load(response)
        def post(payload, headers=None):
            with urlopen(Request(base + "/api/jobs", method="POST", data=json.dumps(payload).encode(), headers=headers or {
                    "Content-Type": "application/json", "Origin": base, "X-Rehearsal-Token": server.session_token}), timeout=15) as response:
                return json.load(response)
        try:
            payload = {"kind": "runtime_session", "scenario": "dependency"}
            with self.assertRaises(HTTPError) as caught:
                post(payload, {"Content-Type": "application/json"})
            self.assertEqual(caught.exception.code, 403)
            post(payload)
            job = self._wait_for_terminal_job(server.lab)
            self.assertEqual(job["status"], "COMPLETED", job)
            run = job["result"]["run"]
            directory = self.root / runtime_sessions.FOLDER / run
            client = runtime_sessions.SessionClient(directory)
            first = client.status()
            access = client.access
            with mock.patch.object(runtime_sessions, "SessionClient", side_effect=AssertionError("Dashboard read a private capability")):
                view = get("/api/run?kind=runtime_session&id=" + run)
            self.assertTrue(view["journal_matches_final"])
            self.assertEqual(view["session"]["metrics"]["committed"], 40)
            self.assertNotIn(access["token"], json.dumps(view))
            self.assertEqual(len(get("/api/catalog")["runs"]["runtime_session"]), 1)
            with self.assertRaises(HTTPError) as caught:
                get("/runtime_session_runs/" + run + "/.access.json")
            self.assertEqual(caught.exception.code, 404)
            with self.assertRaises(HTTPError) as caught:
                get("/api/run?kind=runtime_session&id=..%2Foutside")
            self.assertEqual(caught.exception.code, 400)
            server.lab.enabled = False
            with self.assertRaises(HTTPError) as caught:
                post(payload)
            self.assertEqual(caught.exception.code, 403)
            server.lab.close(); server.shutdown(); server.server_close(); thread.join(timeout=3)
            closed = True
            after = client.status()
            self.assertEqual(first["runtime_id"], after["runtime_id"])
            self.assertEqual(first["actual_at_outcome"]["workers"], after["actual_at_outcome"]["workers"])
            self.assertTrue(after["actual_at_outcome"]["workers"]["candidate-v2"]["alive"])
        finally:
            if not closed:
                server.lab.close(); server.shutdown(); server.server_close(); thread.join(timeout=3)
            if directory is not None:
                runtime_sessions.stop_owned_session(directory)

    def test_team_evaluation_command_cannot_select_live_reserved_or_arbitrary_inputs(self):
        (self.root / "team_evaluation.py").write_text("# fixture", encoding="utf-8")
        lab = ScenarioLab(self.store)
        payload = {"kind": "team_evaluation", "split": "development"}
        command = lab.command(payload)
        self.assertIn("--split", command)
        self.assertNotIn("--live", command)
        self.assertEqual(command[-1], str(self.root / "team_evaluation_runs"))
        for update in ({"split": "confirmation"}, {"split": "../outside"}, {"live": True}, {"api_key": "not-accepted"},
                       {"case": "dev-routing-low"}, {"output": "elsewhere"}, {"calls": 18}):
            with self.assertRaises(EvidenceError):
                lab.command({**payload, **update})

    def test_http_frozen_team_evaluation_scores_all_cases_and_serves_guarded_case_replay(self):
        import shutil
        source = Path(__file__).resolve().parent
        for path in source.glob("*.py"):
            shutil.copyfile(path, self.root / path.name)
        shutil.copyfile(source / "team_evaluation_set.json", self.root / "team_evaluation_set.json")
        server = server_for(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        def get(path):
            with urlopen(base + path, timeout=15) as response:
                return json.load(response)
        def post(payload, headers=None):
            with urlopen(Request(base + "/api/jobs", method="POST", data=json.dumps(payload).encode(), headers=headers or {
                    "Content-Type": "application/json", "Origin": base, "X-Rehearsal-Token": server.session_token}), timeout=15) as response:
                return json.load(response)
        payload = {"kind": "team_evaluation", "split": "development"}
        try:
            with self.assertRaises(HTTPError) as caught:
                post(payload, {"Content-Type": "application/json"})
            self.assertEqual(caught.exception.code, 403)
            post(payload)
            job = self._wait_for_terminal_job(server.lab)
            self.assertEqual(job["status"], "COMPLETED", job)
            self.assertEqual((job["result"]["task_completed"], job["result"]["selected"], job["result"]["model_calls"]), (6, 6, 0))
            run = job["result"]["run"]
            campaign = get("/api/run?kind=team_evaluation&id=" + run)
            self.assertEqual(len(campaign["results"]), 6)
            self.assertFalse(campaign["historical_protocol"])
            self.assertFalse(campaign["summary"]["confirmation_opened"])
            case = get("/api/team-evaluation-case?run=" + run + "&id=dev-routing-low")
            self.assertTrue(case["replay"]["journal_matches_final"])
            self.assertEqual(case["row"]["score"]["safety"], "SAFE")
            self.assertEqual(len(get("/api/catalog")["runs"]["team_evaluation"]), 1)
            with self.assertRaises(HTTPError) as caught:
                get("/api/team-evaluation-case?run=" + run + "&id=..%2Foutside")
            self.assertEqual(caught.exception.code, 400)
            server.lab.enabled = False
            with self.assertRaises(HTTPError) as caught:
                post(payload)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.lab.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_team_evaluation_run_and_output_links_cannot_escape_their_folder(self):
        for run in ("..", "../../private", "x/y"):
            with self.assertRaises(EvidenceError):
                self.store.directory("team_evaluation", run)
        with TemporaryDirectory() as outside:
            base = self.root / "team_evaluation_runs"
            try:
                base.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                return
            with self.assertRaises(EvidenceError):
                self.store.directory("team_evaluation", "run")
            (self.root / "team_evaluation.py").write_text("# fixture", encoding="utf-8")
            with self.assertRaises(EvidenceError):
                ScenarioLab(self.store).command({"kind": "team_evaluation", "split": "development"})

    def test_team_command_never_accepts_live_credentials_or_arbitrary_launch_arguments(self):
        (self.root / "agent_team.py").write_text("# fixture", encoding="utf-8")
        lab = ScenarioLab(self.store)
        valid = {"kind": "team", "scenario": "routing"}
        command = lab.command(valid)
        self.assertIn("agent_team.py", str(command))
        self.assertNotIn("--live", command)
        self.assertEqual(command[-1], str(self.root / "agent_team_runs"))
        for update in ({"live": True}, {"api_key": "not-accepted"}, {"scenario": "../outside"}, {"output": "outside"}):
            with self.assertRaises(EvidenceError):
                lab.command({**valid, **update})

    def test_http_team_to_owner_review_preserves_source_and_enforces_request_guards(self):
        import shutil
        source = Path(__file__).resolve().parent
        for path in source.glob("*.py"):
            shutil.copyfile(path, self.root / path.name)
        server = server_for(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        def get(path):
            with urlopen(base + path, timeout=10) as response:
                return json.load(response)
        def post(payload, headers=None):
            request = Request(base + "/api/jobs", data=json.dumps(payload).encode(), method="POST", headers=headers or {
                "Content-Type": "application/json", "Origin": base, "X-Rehearsal-Token": server.session_token})
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        try:
            post({"kind": "team", "scenario": "routing"})
            job = self._wait_for_terminal_job(server.lab)
            self.assertEqual(job["status"], "COMPLETED", job)
            self.assertEqual(job["result"]["model_calls"], 0)
            run = job["result"]["run"]
            view = get("/api/run?kind=team&id=" + run)
            self.assertTrue(view["journal_matches_final"])
            self.assertEqual(view["review"]["available_decisions"], ["retain", "withdraw_route"])
            payload = {"kind": "team_review", "source_kind": "team", "source_run": run,
                       "source_digest": view["review"]["source_digest"], "decision": "withdraw_route",
                       "note": "C no longer needs its route in this simulation.", "confirm_snapshot": True}
            for headers, status in (({"Content-Type": "application/json"}, 403),
                                    ({"Content-Type": "application/json", "Origin": "https://outside.example", "X-Rehearsal-Token": server.session_token}, 403)):
                with self.assertRaises(HTTPError) as caught:
                    post(payload, headers)
                self.assertEqual(caught.exception.code, status)
            for update in ({"confirm_snapshot": False}, {"actor": "MANAGER"}, {"source_digest": "0" * 64},
                           {"source_run": "../outside"}, {"decision": "release_dependency"}):
                with self.assertRaises(HTTPError) as caught:
                    post({**payload, **update})
                self.assertEqual(caught.exception.code, 400)
            original = self.store.file(self.store.directory("team", run), "summary.json").read_bytes()
            post(payload)
            job = self._wait_for_terminal_job(server.lab)
            self.assertEqual(job["status"], "COMPLETED", job)
            branch = get("/api/run?kind=team_review&id=" + job["result"]["run"])
            self.assertTrue(branch["journal_matches_final"])
            self.assertEqual(branch["report"]["recovery_outcome"], "VERIFIED_RECOVERED")
            self.assertFalse(branch["report"]["live_effects"])
            self.assertEqual(self.store.file(self.store.directory("team", run), "summary.json").read_bytes(), original)
            self.assertEqual(len(get("/api/catalog")["runs"]["team_review"]), 1)
            server.lab.enabled = False
            with self.assertRaises(HTTPError) as caught:
                post(payload)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.lab.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def _wait_for_terminal_job(self, lab, timeout=None):
        """Wait for the worker's result, not a machine-dependent startup delay.

        The runner already has a three-minute process deadline. Allow a small
        additional window for launch, termination and saved-evidence parsing.
        Short joins keep this wait bounded and make an unfinished job explicit.
        """
        timeout = RUNNER_TIMEOUT_SECONDS + 20 if timeout is None else timeout
        deadline = time.monotonic() + timeout
        self.assertIsNotNone(lab.thread, "The lab did not create a worker thread.")
        while lab.thread.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail("Dashboard worker did not finish within %.2fs; last job: %r" % (
                    timeout, lab.status()["job"]))
            lab.thread.join(timeout=min(0.25, remaining))
        job = lab.status()["job"]
        self.assertIsNotNone(job, "The worker exited without a recorded job.")
        self.assertIn(job["status"], {"COMPLETED", "FAILED", "STOPPED"},
                      "Worker exited without a terminal result: %r" % job)
        return job

    def test_slow_review_worker_completes_after_old_ten_second_wait(self):
        import shutil
        source = Path(__file__).resolve().parent
        shutil.copyfile(source / "simulator.py", self.root / "simulator.py")
        script = (source / "review_workflow.py").read_text(encoding="utf-8")
        marker = 'if __name__ == "__main__":'
        self.assertIn(marker, script)
        # Delay only the temporary test copy. A real child still executes the
        # complete review workflow and produces evidence checked by ScenarioLab.
        script = script.replace(marker, marker + '\n    import time\n    time.sleep(10.25)', 1)
        (self.root / "review_workflow.py").write_text(script, encoding="utf-8")
        lab = ScenarioLab(self.store)
        try:
            lab.start({"kind": "review", "operation": "create", "scenario": "routing_conflict"})
            job = self._wait_for_terminal_job(lab)
            self.assertEqual(job["status"], "COMPLETED", job)
            self.assertGreaterEqual(job["elapsed_seconds"], 10)
            self.assertEqual(job["result"]["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")
            self.assertEqual(job["result"]["final_metrics"]["committed"], 60)
            self.assertTrue(job["result"]["journal_matches_final"])
        finally:
            lab.close()

    def test_job_wait_deadline_reports_unfinished_worker(self):
        lab, release = ScenarioLab(self.store), threading.Event()
        lab.job = {"id": "blocked-test-worker", "status": "RUNNING"}
        lab.thread = threading.Thread(target=release.wait, daemon=True)
        lab.thread.start()
        try:
            with self.assertRaisesRegex(AssertionError, "did not finish.*RUNNING"):
                self._wait_for_terminal_job(lab, timeout=0.05)
            self.assertTrue(lab.thread.is_alive())
            self.assertEqual(lab.status()["job"]["status"], "RUNNING")
        finally:
            release.set()
            lab.thread.join(timeout=3)
            lab.close()

    def test_review_commands_are_bounded_and_source_confined(self):
        (self.root / "review_workflow.py").write_text("# fixture", encoding="utf-8")
        self.put("review_runs/source/report.json", {})
        (self.root / "review_runs" / "source" / "state.sqlite3").write_bytes(b"fixture")
        lab = ScenarioLab(self.store)
        valid = {"kind": "review", "operation": "resolve", "source_run": "source",
                 "state_digest": "a" * 64, "decision": "retain", "note": "C retains this fixture."}
        for update in ({"source_run": "../outside"}, {"state_digest": "guessed"},
                       {"decision": "force_delete"}, {"note": ""}, {"note": "x" * 301},
                       {"actor": "MANAGER"}):
            with self.assertRaises(EvidenceError):
                lab.command({**valid, **update})
        with self.assertRaises(EvidenceError):
            lab.command({"kind": "review", "operation": "create", "scenario": []})
        command = lab.command(valid)
        self.assertIn("review_workflow.py", command[2])
        self.assertEqual(command[-1], str(self.root / "review_runs"))
        self.assertNotIn("--live", command)

    def test_http_review_decision_branches_and_stale_state_guard(self):
        import hashlib
        import shutil
        source = Path(__file__).resolve().parent
        for name in ("simulator.py", "review_workflow.py"):
            shutil.copyfile(source / name, self.root / name)
        server = server_for(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        headers = {"Content-Type": "application/json", "X-Rehearsal-Token": server.session_token}

        def launch(payload):
            with urlopen(Request(url + "/api/jobs", data=json.dumps(payload).encode(), headers=headers), timeout=5) as response:
                self.assertEqual(response.status, 202)
            job = self._wait_for_terminal_job(server.lab)
            self.assertEqual(job["status"], "COMPLETED", job)
            result = job["result"]
            with urlopen(url + "/api/run?kind=review&id=" + result["run"], timeout=5) as response:
                return json.load(response)

        try:
            initial = launch({"kind": "review", "operation": "create", "scenario": "routing_conflict"})
            initial_dir = self.root / "review_runs" / initial["id"]
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in initial_dir.iterdir() if p.is_file()}
            base = {"kind": "review", "operation": "resolve", "source_run": initial["id"],
                    "state_digest": initial["report"]["review"]["state_digest"], "decision": "retain", "note": "C still needs the fixture route."}
            retained = launch(base)
            self.assertEqual(retained["report"]["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")
            self.assertEqual(retained["saved_final_metrics"]["committed"], 60)
            stale = launch({**base, "source_run": retained["id"], "decision": "withdraw_route"})
            self.assertEqual(stale["report"]["review"]["decision"]["result"]["code"], "STALE_REVIEW_STATE")
            resolved = launch({**base, "source_run": retained["id"],
                               "state_digest": retained["report"]["review"]["state_digest"],
                               "decision": "withdraw_route", "note": "C now withdraws the fixture route."})
            self.assertEqual(resolved["report"]["recovery_outcome"], "VERIFIED_RECOVERED")
            self.assertEqual(resolved["saved_final_metrics"]["committed"], 40)
            self.assertEqual(resolved["saved_final_metrics"]["route_owner"], "route-C")
            self.assertTrue(resolved["journal_matches_final"])
            self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in initial_dir.iterdir() if p.is_file()}, before)
            with urlopen(url + "/api/catalog", timeout=5) as response:
                self.assertEqual(len(json.load(response)["runs"]["review"]), 4)
            server.lab.enabled = False
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(url + "/api/jobs", data=json.dumps(base).encode(), headers=headers), timeout=5)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.lab.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EvidenceStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_empty_catalog_has_no_invented_scores(self):
        catalog = self.store.catalog()
        self.assertEqual(catalog["dashboard_model_calls"], 0)
        self.assertTrue(all(not rows for rows in catalog["runs"].values()))

    def test_catalog_keeps_failed_latest_runs_and_fixture_modes(self):
        for run, correct, mode in [("20261012T100000Z-a", 12, "LIVE NEBIUS NEMOTRON CLASSIFICATION"),
                                    ("20261012T110000Z-b", 5, "OFFLINE TEST FIXTURE")]:
            self.put("evaluation_runs/" + run + "/summary.json", {"mode": mode, "counts": {"exact_correct": correct}})
        rows = self.store.catalog()["runs"]["evaluation"]
        self.assertEqual([r["summary"]["counts"]["exact_correct"] for r in rows], [5, 12])
        self.assertEqual(rows[0]["summary"]["mode"], "OFFLINE TEST FIXTURE")

    def test_corrupt_runs_are_reported_without_replacing_them(self):
        path = self.put("factory_runs/bad/summary.json", {})
        path.write_text("{unfinished", encoding="utf-8")
        before = path.read_bytes()
        result = self.store.catalog()
        self.assertEqual(result["runs"]["factory"], [])
        self.assertTrue(result["warnings"])
        self.assertEqual(path.read_bytes(), before)

    def test_path_traversal_and_outside_symlinks_are_rejected(self):
        for run in ("..", "../../private", "C:\\secret", "a/b"):
            with self.assertRaises(EvidenceError):
                self.store.directory("evaluation", run)
        with TemporaryDirectory() as outside:
            link = self.root / "evaluation_runs"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                return  # Windows accounts may not have symlink privileges.
            with self.assertRaises(EvidenceError):
                self.store.directory("evaluation", "anything")

    def test_holdout_case_is_closed_at_the_api_layer(self):
        self.put("factory_runs/run/cases/case-hidden.json", {"definition": {"split": "holdout"}, "evaluation": {}})
        with self.assertRaises(HoldoutClosed):
            self.store.case("run", "case-hidden")

    def test_replay_preserves_blocks_and_detects_missing_changes(self):
        initial = {"stable_units": 40, "capacity_limit": 70, "paused_runs": [], "deployments": {},
                   "reservations": {"r": {"units": 20, "status": "CONSUMED"}}}
        report = {"initial_state": initial, "events": [
            {"kind": "ACTION", "result": {"status": "BLOCKED"}, "changes": {}},
            {"kind": "RECOVERY_STARTED", "run_id": "deploy-A"}],
            "final_state": {**initial, "paused_runs": ["deploy-A"]}}
        out = replay(report)
        self.assertEqual(out["frames"][1]["state"], initial)
        self.assertEqual(out["frames"][1]["metrics"]["committed"], 40)
        self.assertTrue(out["journal_matches_final"])
        self.assertEqual(initial["paused_runs"], [])
        report["final_state"]["capacity_limit"] = 60
        self.assertFalse(replay(report)["journal_matches_final"])

    def test_result_count_mismatch_is_visible(self):
        self.put("evaluation_runs/run/summary.json", {"counts": {"attempted": 3}})
        self.put("evaluation_runs/run/results.jsonl", {"outcome": "API_ERROR", "prediction": None})
        result = self.store.run("evaluation", "run")
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["warnings"])

    def test_http_is_local_read_only_and_does_not_serve_arbitrary_files(self):
        secret = self.put("private.json", {"private": "must-not-be-served"})
        before = secret.read_bytes()
        server = server_for(self.root, 0, read_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        try:
            with urlopen(url + "/api/catalog", timeout=3) as response:
                self.assertEqual(json.load(response)["access"], "READ_ONLY")
            with urlopen(url, timeout=3) as response:
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertIn(b"Orkestryn", response.read())
            for request, status in [(Request(url + "/private.json"), 404),
                                    (Request(url + "/api/catalog", method="POST", data=b"{}"), 405),
                                    (Request(url + "/api/catalog", headers={"Host": "external.example"}), 403),
                                    (Request(url + "/api/catalog", headers={"Origin": "https://external.example"}), 403)]:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request, timeout=3)
                self.assertEqual(caught.exception.code, status)
            self.assertEqual(secret.read_bytes(), before)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_lab_accepts_only_bounded_offline_actions(self):
        (self.root / "simulator.py").write_text("# fixture", encoding="utf-8")
        (self.root / "data_factory.py").write_text("# fixture", encoding="utf-8")
        lab = ScenarioLab(self.store)
        invalid = [None, {}, {"kind": "agent"}, {"kind": "evaluation"},
                   {"kind": "rehearsal", "scenario": "../../secret"},
                   {"kind": "rehearsal", "scenario": []},
                   {"kind": "factory", "seed": True, "variants": 5},
                   {"kind": "factory", "seed": 1, "variants": 100},
                   {"kind": "factory", "seed": 1, "variants": 5, "output": "elsewhere"}]
        for payload in invalid:
            with self.assertRaises(EvidenceError):
                lab.command(payload)
        command = lab.command({"kind": "rehearsal", "scenario": "routing_conflict"})
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[-1], str(self.root / "rehearsal_runs"))
        self.assertIn("routing_conflict", command)

    def test_lab_refuses_a_second_active_job_and_read_only_launches(self):
        lab = ScenarioLab(self.store)
        lab.job = {"status": "RUNNING"}
        with self.assertRaises(JobBusy):
            lab.start({"kind": "rehearsal", "scenario": "flagship"})
        readonly = ScenarioLab(self.store, enabled=False)
        with self.assertRaises(EvidenceError):
            readonly.start({"kind": "rehearsal", "scenario": "flagship"})
        self.assertIsNone(readonly.status()["job"])

    def test_failed_lab_process_retains_existing_evidence(self):
        previous = self.put("rehearsal_runs/previous/summary.json", [{"scenario": "flagship"}])
        before = previous.read_bytes()
        (self.root / "simulator.py").write_text("print('OFFLINE FAILURE FIXTURE')\nraise SystemExit(7)\n", encoding="utf-8")
        lab = ScenarioLab(self.store)
        try:
            lab.start({"kind": "rehearsal", "scenario": "flagship"})
            job = self._wait_for_terminal_job(lab)
            self.assertEqual(job["status"], "FAILED")
            self.assertEqual(job["returncode"], 7)
            self.assertIn("OFFLINE FAILURE FIXTURE", job["log"])
            self.assertIsNone(job["result"])
            self.assertEqual(previous.read_bytes(), before)
        finally:
            lab.close()

    def test_http_launch_requires_session_token_and_local_origin(self):
        from unittest.mock import patch
        server = server_for(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        body = json.dumps({"kind": "rehearsal", "scenario": "flagship"}).encode()
        headers = {"Content-Type": "application/json", "X-Rehearsal-Token": server.session_token}
        try:
            with patch.object(server.lab, "start", return_value={"status": "QUEUED"}) as launch:
                for extra, method, expected in [({"X-Rehearsal-Token": "wrong"}, "POST", 403),
                                                ({"Origin": "https://outside.example"}, "POST", 403),
                                                ({}, "PUT", 405)]:
                    request = Request(url + "/api/jobs", data=body, method=method, headers={**headers, **extra})
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(request, timeout=3)
                    self.assertEqual(caught.exception.code, expected)
                launch.assert_not_called()
                with urlopen(Request(url + "/api/jobs", data=body, headers=headers), timeout=3) as response:
                    self.assertEqual(response.status, 202)
                launch.assert_called_once_with({"kind": "rehearsal", "scenario": "flagship"})
                server.lab.enabled = False
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url + "/api/jobs", data=body, headers=headers), timeout=3)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(launch.call_count, 1)
        finally:
            server.lab.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="Project folder containing the saved run folders.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in your default browser.")
    parser.add_argument("--read-only", action="store_true", help="Disable Scenario lab launch controls.")
    parser.add_argument("--self-test", action="store_true", help="Run offline dashboard checks; no model calls.")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DashboardTests))
        return 0 if result.wasSuccessful() else 1
    if not args.root.is_dir():
        parser.error("--root must be your existing project folder.")
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port from 1024 to 65535.")
    try:
        server = server_for(args.root, args.port, read_only=args.read_only)
    except OSError:
        print("Could not open that local port. Try: python dashboard.py --open --port 8766", file=sys.stderr)
        return 1
    url = "http://127.0.0.1:" + str(args.port)
    print("ORKESTRYN - EVIDENCE WORKSPACE")
    print("Dashboard: " + url)
    print("Project: " + str(args.root.resolve()))
    print("Saved evidence + offline Scenario lab. No model calls or real infrastructure actions.")
    if args.read_only:
        print("Read-only mode: lab launch controls are disabled.")
    print("Leave this terminal open. Press Ctrl+C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nDashboard stopped. Existing evidence is retained.")
    finally:
        server.lab.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
