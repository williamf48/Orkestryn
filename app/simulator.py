#!/usr/bin/env python3
r"""Orkestryn, milestone 1: deterministic infrastructure simulator.

Windows: .\.venv\Scripts\python.exe simulator.py
Checks:  .\.venv\Scripts\python.exe simulator.py --self-test

Standard library only. No model calls, secrets, real GPUs or cloud changes.
All actors, approvals, metrics and faults are explicitly simulated fixtures.
Evidence is retained in a new rehearsal_runs/<timestamp>-<id> directory.
This is an initial engine milestone, not the complete hackathon submission.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


SERVICE = "inference-service-1"
POLICY = "policy-v1"
TOOLS = {
    "reserve_capacity": {"units", "reservation_id", "budget_version"},
    "start_candidate": {"candidate_id", "reservation_id", "config"},
    "scale_candidate": {"candidate_id", "reservation_id"},
    "set_route": {"candidate_id", "candidate_percent", "route_version"},
    "set_capacity_limit": {"limit", "budget_version"},
    "add_dependency": {"candidate_id", "dependency_id"},
    "restore_route": {"candidate_id", "route_version"},
    "stop_candidate": {"candidate_id"},
    "release_capacity": {"candidate_id"},
    "release_reservation": {"reservation_id"},
}
CAPABILITIES = {
    "A": {"reserve_capacity", "start_candidate", "scale_candidate", "set_route"},
    "B": {"set_capacity_limit"},
    "C": {"set_route", "add_dependency"},
    "RECOVERY": {
        "restore_route", "stop_candidate", "release_capacity", "release_reservation"
    },
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Denied(Exception):
    def __init__(self, code, reason, details=None):
        self.code, self.reason, self.details = code, reason, details or {}
        super().__init__(reason)


def require(condition, code, reason, details=None):
    if not condition:
        raise Denied(code, reason, details)


def integer(value, name, minimum=0, maximum=None):
    require(type(value) is int, "INVALID_ARGUMENT", name + " must be an integer.")
    require(value >= minimum, "INVALID_ARGUMENT", name + " is below its minimum.")
    if maximum is not None:
        require(value <= maximum, "INVALID_ARGUMENT", name + " exceeds its maximum.")
    return value


def total_commitments(state):
    return (
        state["stable_units"]
        + sum(d["allocated_units"] for d in state["deployments"].values())
        + sum(r["units"] for r in state["reservations"].values() if r["status"] == "PENDING")
    )


def initial_state():
    plans = {
        "deploy-A": {"actor": "A", "service": SERVICE, "candidate": "candidate-v2",
                     "config": "model-config-v2", "max_units": 40, "max_traffic": 50},
        "cost-B": {"actor": "B", "service": SERVICE, "approved_limit": 70},
        "route-C": {"actor": "C", "service": SERVICE, "candidate": "candidate-v2",
                    "max_traffic": 20, "can_depend": True},
    }
    approvals = {}
    for run_id, plan in plans.items():
        approvals["AP-" + run_id] = {
            "actor": plan["actor"], "run_id": run_id, "service": SERVICE,
            "policy": POLICY, "plan_digest": digest(plan), "expires_at": 100,
            "revoked": False, "budget_version": 1 if run_id == "deploy-A" else None,
            "provenance": "SIMULATED MANAGER APPROVAL FIXTURE",
        }
    approvals["AP-recovery-A"] = {
        "actor": "RECOVERY", "run_id": "deploy-A", "service": SERVICE,
        "policy": POLICY, "plan_digest": digest(plans["deploy-A"]),
        "expires_at": 200, "revoked": False, "budget_version": None,
        "provenance": "SIMULATED PREAUTHORIZED BOUNDED RECOVERY",
    }
    return {
        "service": SERVICE, "policy": POLICY, "clock": 0,
        "stable_units": 40, "stable_healthy": True, "stable_allowed": True,
        "stable_capacity_sufficient": True, "capacity_limit": 100,
        "budget_version": 1, "routing": {"stable-v1": 100, "candidate-v2": 0},
        "route_version": 1, "route_owner": None, "deployments": {},
        "reservations": {}, "plans": plans, "approvals": approvals,
        "paused_runs": [],
        "capabilities": {k: sorted(v) for k, v in CAPABILITIES.items()},
    }


class Engine:
    """Single-service coordinator; each state transition and journal entry is atomic.

    Caller identity is provided by the trusted local scenario runner. This is NOT
    an internet-facing authentication implementation. SQLite BEGIN IMMEDIATE
    serializes short admission transactions, not long observation windows.
    """

    def __init__(self, path, create=True):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        if create:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS service_state (
                    id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS action_receipts (
                    action_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL);
            """)
            self.db.execute("INSERT OR IGNORE INTO service_state VALUES (1, ?)",
                            (canonical(initial_state()),))

    def close(self):
        self.db.close()

    def state(self):
        row = self.db.execute("SELECT payload FROM service_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("This file is not an initialized rehearsal database.")
        return json.loads(row[0])

    def events(self):
        return [{"sequence": seq, **json.loads(event)} for seq, event in
                self.db.execute("SELECT sequence, event FROM events ORDER BY sequence")]

    def _event(self, value):
        self.db.execute("INSERT INTO events(event) VALUES (?)", (canonical(value),))

    def _checkpoint(self, name, **context):
        """No-op instrumentation boundary; only the offline crash harness overrides it."""
        pass

    @staticmethod
    def check_invariants(state):
        if total_commitments(state) > state["capacity_limit"]:
            raise RuntimeError("Invariant failed: shared capacity was overcommitted.")
        if sum(state["routing"].values()) != 100:
            raise RuntimeError("Invariant failed: routing must sum to 100.")
        for target, weight in state["routing"].items():
            if type(weight) is not int or not 0 <= weight <= 100:
                raise RuntimeError("Invariant failed: invalid route weight.")
            if target != "stable-v1" and weight:
                d = state["deployments"].get(target)
                if d is None or d["status"] != "RUNNING" or not d["allocated_units"]:
                    raise RuntimeError("Invariant failed: traffic targets an inactive resource.")
        for d in state["deployments"].values():
            if d["allocated_units"] < 0:
                raise RuntimeError("Invariant failed: negative allocation.")

    def _approval(self, state, actor, run_id, approval_id, tool):
        require(actor in state["capabilities"] and tool in state["capabilities"][actor],
                "ACTOR_PERMISSION_DENIED", "The fixture actor lacks this capability.")
        ap = state["approvals"].get(approval_id)
        require(ap is not None, "APPROVAL_MISSING", "No recorded approval exists.")
        require(ap["actor"] == actor and ap["run_id"] == run_id,
                "APPROVAL_SCOPE_MISMATCH", "Approval belongs to another actor or workflow.")
        require(ap["service"] == SERVICE and ap["policy"] == state["policy"],
                "APPROVAL_STALE", "The service or policy no longer matches approval.")
        require(not ap["revoked"], "APPROVAL_REVOKED", "Approval has been revoked.")
        require(state["clock"] < ap["expires_at"], "APPROVAL_EXPIRED", "Approval has expired.")
        plan = state["plans"].get(run_id)
        require(plan is not None and digest(plan) == ap["plan_digest"],
                "PLAN_CHANGED", "Approval does not authorize the edited plan.")
        if actor != "RECOVERY":
            require(run_id not in state["paused_runs"], "WORKFLOW_PAUSED",
                    "This workflow has entered recovery and cannot admit more work.")
        if ap["budget_version"] is not None:
            require(ap["budget_version"] == state["budget_version"],
                    "STALE_BUDGET_VERSION", "Current capacity limit differs from the approved plan.",
                    {"approved_version": ap["budget_version"],
                     "current_version": state["budget_version"],
                     "capacity_limit": state["capacity_limit"]})
        return plan

    def apply(self, actor, run_id, tool, args, action_key, approval_id):
        request = {"actor": actor, "run_id": run_id, "tool": tool,
                   "args": args, "approval_id": approval_id}
        fingerprint = digest(request)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute(
                "SELECT fingerprint, result FROM action_receipts WHERE action_key=?",
                (action_key,)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    result = {"status": "BLOCKED", "code": "IDEMPOTENCY_KEY_CONFLICT",
                              "reason": "An existing action key has different arguments."}
                else:
                    result = {**json.loads(previous[1]), "replayed": True}
                self._event({"kind": "IDEMPOTENT_REPLAY", **request, "result": result})
                self.db.execute("COMMIT")
                return result
            before = self.state()
            state = copy.deepcopy(before)
            try:
                require(isinstance(action_key, str) and bool(action_key),
                        "INVALID_ARGUMENT", "A nonempty action key is required.")
                require(tool in TOOLS, "UNSUPPORTED_TOOL", "This operation is unsupported.")
                require(isinstance(args, dict) and set(args) == TOOLS[tool] | {"service"},
                        "INVALID_ARGUMENT", "Arguments do not match the supported tool contract.")
                require(args["service"] == SERVICE, "SERVICE_SCOPE_MISMATCH", "Wrong service.")
                plan = self._approval(state, actor, run_id, approval_id, tool)
                note = self._execute(state, actor, run_id, tool, args, plan)
                self.check_invariants(state)
                self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (canonical(state),))
                result = {"status": "SUCCEEDED", "code": "OK", "reason": note,
                          "committed_units": total_commitments(state)}
                changes = {k: {"before": before[k], "after": state[k]}
                           for k in state if state[k] != before[k]}
            except Denied as exc:
                result = {"status": "BLOCKED", "code": exc.code,
                          "reason": exc.reason, "details": exc.details}
                changes = {}
            event = {"kind": "ACTION", "clock": before["clock"],
                     "action_key": action_key, **request, "result": result, "changes": changes}
            self._event(event)
            self.db.execute("INSERT INTO action_receipts VALUES (?, ?, ?)",
                            (action_key, fingerprint, canonical(result)))
            self._checkpoint("action_before_commit", tool=tool, action_key=action_key)
            self.db.execute("COMMIT")
            self._checkpoint("action_after_commit", tool=tool, action_key=action_key)
            return result
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _candidate(self, state, candidate_id, run_id, owned=True):
        d = state["deployments"].get(candidate_id)
        require(d is not None, "RESOURCE_MISSING", "Candidate resource does not exist.")
        if owned:
            require(d["owner_run"] == run_id, "RESOURCE_OWNER_MISMATCH",
                    "This workflow does not own the resource.")
        return d

    def _metrics(self, state, d):
        m = d.get("metrics")
        require(d["healthy"] and d["status"] == "RUNNING", "CANDIDATE_NOT_READY",
                "The candidate is not healthy and running.")
        require(m is not None, "METRICS_MISSING", "No candidate observations exist.")
        require(0 <= state["clock"] - m["observed_at"] <= 20, "METRICS_STALE",
                "Candidate observations are stale.")
        require(m["samples"] >= 100, "INSUFFICIENT_SAMPLES", "At least 100 fixture samples required.")
        require(m["correctness"] >= 95 and m["error_rate"] <= 2 and m["p95_ms"] <= 1500,
                "PROMOTION_GATE_FAILED", "Candidate observations fail the reviewed demonstration gates.")

    def _execute(self, s, actor, run, tool, a, plan):
        if "candidate_id" in a:
            require(a["candidate_id"] == plan.get("candidate"), "RESOURCE_SCOPE_MISMATCH",
                    "Candidate is outside the approved plan.")
        if tool == "reserve_capacity":
            units = integer(a["units"], "units", 1)
            require(a["budget_version"] == s["budget_version"], "STALE_BUDGET_VERSION",
                    "Reservation read an old budget version.")
            rid = a["reservation_id"]
            require(isinstance(rid, str) and rid and rid not in s["reservations"],
                    "RESERVATION_EXISTS", "A fresh reservation ID is required.")
            own = sum(d["allocated_units"] for d in s["deployments"].values() if d["owner_run"] == run)
            own += sum(r["units"] for r in s["reservations"].values()
                       if r["owner_run"] == run and r["status"] == "PENDING")
            require(own + units <= plan["max_units"], "PLAN_BOUND_EXCEEDED", "Approved capacity bound exceeded.")
            projected = total_commitments(s) + units
            require(projected <= s["capacity_limit"], "CAPACITY_LIMIT_EXCEEDED",
                    "The shared capacity limit would be exceeded.",
                    {"projected_usage": projected, "limit": s["capacity_limit"]})
            s["reservations"][rid] = {"units": units, "owner_run": run, "status": "PENDING"}
            return "Capacity reserved atomically."
        if tool in {"start_candidate", "scale_candidate"}:
            r = s["reservations"].get(a["reservation_id"])
            require(r is not None and r["owner_run"] == run and r["status"] == "PENDING",
                    "RESERVATION_NOT_AVAILABLE", "No eligible pending reservation exists.")
            cid = a["candidate_id"]
            if tool == "start_candidate":
                require(cid not in s["deployments"], "RESOURCE_EXISTS", "Candidate already exists.")
                require(a["config"] == plan["config"], "CONFIG_NOT_APPROVED", "Configuration differs from approval.")
                s["deployments"][cid] = {"owner_run": run, "config": a["config"],
                    "allocated_units": r["units"], "status": "RUNNING", "healthy": False,
                    "metrics": None, "dependencies": [], "in_flight": 0, "stop_timeout": False}
            else:
                d = self._candidate(s, cid, run)
                require(d["status"] == "RUNNING", "CANDIDATE_NOT_READY", "Candidate is not running.")
                d["allocated_units"] += r["units"]
                d["healthy"], d["metrics"] = False, None
            r["status"] = "CONSUMED"
            return "Reservation transferred to allocation; capacity is counted once."
        if tool == "set_capacity_limit":
            limit = integer(a["limit"], "limit", 1)
            require(a["budget_version"] == s["budget_version"], "STALE_BUDGET_VERSION", "Old budget version.")
            require(limit == plan["approved_limit"], "PLAN_BOUND_EXCEEDED", "Limit differs from approved value.")
            require(limit >= total_commitments(s), "LIMIT_BELOW_COMMITMENTS",
                    "Existing allocations and pending reservations must remain covered.")
            s["capacity_limit"], s["budget_version"] = limit, s["budget_version"] + 1
            return "Separately approved cost limit applied; existing commitments remain covered."
        if tool in {"set_route", "restore_route"}:
            pct = 0 if tool == "restore_route" else integer(a["candidate_percent"], "candidate_percent", 0, 100)
            require(a["route_version"] == s["route_version"], "ROUTE_VERSION_CONFLICT", "Routing has changed.")
            d = self._candidate(s, a["candidate_id"], run, owned=(actor != "C"))
            if tool == "restore_route":
                require(s["route_owner"] == run, "ROUTE_OWNER_CONFLICT", "A later actor owns the routing change.")
                require(s["stable_healthy"] and s["stable_allowed"] and s["stable_capacity_sufficient"],
                        "FALLBACK_NOT_SAFE", "Stable fallback is unhealthy, forbidden or undersized.")
            else:
                require(pct <= plan["max_traffic"], "PLAN_BOUND_EXCEEDED", "Approved traffic bound exceeded.")
                self._metrics(s, d)
                require(s["stable_healthy"] and s["stable_allowed"], "FALLBACK_NOT_SAFE", "Stable fallback unavailable.")
            s["routing"] = {"stable-v1": 100 - pct, "candidate-v2": pct}
            s["route_version"] += 1
            s["route_owner"] = run
            return "Version-checked route applied."
        if tool == "add_dependency":
            require(plan.get("can_depend"), "ACTOR_PERMISSION_DENIED", "Dependency registration not approved.")
            d = self._candidate(s, a["candidate_id"], run, owned=False)
            require(d["status"] == "RUNNING", "CANDIDATE_NOT_READY", "Cannot depend on a stopped candidate.")
            require(isinstance(a["dependency_id"], str) and bool(a["dependency_id"]),
                    "INVALID_ARGUMENT", "Dependency ID is required.")
            dependency = {"id": a["dependency_id"], "owner_run": run}
            if dependency not in d["dependencies"]:
                d["dependencies"].append(dependency)
            return "Another approved workflow's dependency registered."
        if tool == "stop_candidate":
            d = self._candidate(s, a["candidate_id"], run)
            require(s["routing"].get(a["candidate_id"], 0) == 0, "RESOURCE_STILL_ROUTED", "Candidate still receives traffic.")
            require(not d["dependencies"], "RESOURCE_STILL_IN_USE", "Another workflow depends on this resource.")
            require(d["in_flight"] == 0, "REQUESTS_IN_FLIGHT", "Candidate requests must drain first.")
            require(not d["stop_timeout"] and d["status"] != "STOP_OUTCOME_UNKNOWN",
                    "STOP_OUTCOME_UNKNOWN", "Stop is not confirmed; allocation must remain committed.")
            d["status"], d["healthy"] = "STOPPED", False
            return "Simulated stop confirmed. Allocation remains until explicit release."
        if tool == "release_capacity":
            d = self._candidate(s, a["candidate_id"], run)
            require(d["status"] == "STOPPED", "STOP_NOT_CONFIRMED", "Cannot release an unconfirmed running resource.")
            require(not d["dependencies"] and not d["in_flight"]
                    and s["routing"].get(a["candidate_id"], 0) == 0,
                    "RESOURCE_STILL_IN_USE", "Resource has routing, requests or dependencies.")
            d["allocated_units"] = 0
            return "Confirmed stopped allocation released."
        if tool == "release_reservation":
            r = s["reservations"].get(a["reservation_id"])
            require(r is not None and r["owner_run"] == run, "RESOURCE_OWNER_MISMATCH", "Reservation not owned.")
            require(r["status"] in {"PENDING", "RELEASED"}, "RESERVATION_CONSUMED",
                    "Consumed reservations must be released through confirmed resource cleanup.")
            r["status"] = "RELEASED"
            return "Unused reservation released."
        raise Denied("UNSUPPORTED_TOOL", "No registered operation handler.")

    def fixture_event(self, kind, **values):
        """Trusted TEST HARNESS operation. Never exposed as an agent tool."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            before = self.state()
            s = copy.deepcopy(before)
            if kind == "OBSERVATIONS":
                d = s["deployments"]["candidate-v2"]
                d["healthy"] = values.get("healthy", True)
                d["metrics"] = {"samples": values.get("samples", 100),
                    "correctness": values.get("correctness", 98),
                    "error_rate": values.get("error_rate", 0),
                    "p95_ms": values.get("p95_ms", 900), "observed_at": s["clock"],
                    "source": "SYNTHETIC OBSERVATION FIXTURE, NOT MEASURED GPU PERFORMANCE"}
            elif kind == "CANDIDATE_FAILURE":
                d = s["deployments"]["candidate-v2"]
                d["healthy"] = False
                d["metrics"] = {"samples": 100, "correctness": 85, "error_rate": 15,
                                    "p95_ms": 2000, "observed_at": s["clock"],
                                    "source": "EXPLICITLY INJECTED TEST FAILURE"}
            elif kind == "ADVANCE_CLOCK":
                s["clock"] += integer(values["ticks"], "ticks", 0)
            elif kind == "REVOKE_APPROVAL":
                s["approvals"][values["approval_id"]]["revoked"] = True
            elif kind == "EDIT_PLAN":
                s["plans"]["deploy-A"]["max_units"] = values["max_units"]
            elif kind == "FALLBACK_UNHEALTHY":
                s["stable_healthy"] = False
            elif kind == "STOP_TIMEOUT":
                s["deployments"]["candidate-v2"]["stop_timeout"] = True
            elif kind == "RECONCILE_STOP_CONFIRMED":
                d = s["deployments"]["candidate-v2"]
                if s["routing"]["candidate-v2"] or d["dependencies"] or d["in_flight"]:
                    raise RuntimeError("Fixture cannot confirm a safe stop with live consumers.")
                d["stop_timeout"], d["status"] = False, "STOPPED"
            elif kind == "REVOKE_RECOVERY_CAPABILITY":
                s["capabilities"]["RECOVERY"] = []
            elif kind == "SECOND_APPROVED_PLAN":
                s["plans"]["deploy-A2"] = copy.deepcopy(s["plans"]["deploy-A"])
                s["approvals"]["AP-deploy-A2"] = {
                    **copy.deepcopy(s["approvals"]["AP-deploy-A"]),
                    "run_id": "deploy-A2", "plan_digest": digest(s["plans"]["deploy-A2"]),
                }
            else:
                raise ValueError("Unknown fixture event.")
            self.check_invariants(s)
            self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (canonical(s),))
            self._event({"kind": "TEST_HARNESS", "label": "SIMULATED/INJECTED FIXTURE",
                         "fixture_type": kind, "values": values, "clock": s["clock"],
                         "changes": {k: {"before": before[k], "after": s[k]}
                                     for k in s if s[k] != before[k]}})
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def recover(self, run="deploy-A"):
        """Compensate registered effects; never restore a global old snapshot."""
        issues, steps = [], []
        # Durable pause closes admission for the failed workflow before cleanup.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            paused = self.state()
            if run not in paused["plans"]:
                raise ValueError("Recovery workflow is not a registered fixture.")
            if run not in paused["paused_runs"]:
                paused["paused_runs"].append(run)
                self.db.execute("UPDATE service_state SET payload=? WHERE id=1", (canonical(paused),))
                self._event({"kind": "RECOVERY_STARTED", "run_id": run,
                             "reason": "Stop admitting new work before compensation."})
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self._checkpoint("recovery_after_pause", run_id=run)
        attempt = sum(1 for e in self.events() if e.get("kind") == "RECOVERY_REPORT")
        s = self.state()
        original_routes = [e for e in self.events() if e.get("kind") == "ACTION"
                           and e.get("run_id") == run and e.get("tool") == "set_route"
                           and e["result"]["status"] == "SUCCEEDED"]
        cid = "candidate-v2"
        d = s["deployments"].get(cid)
        if d is not None and s["routing"].get(cid, 0):
            if not original_routes:
                issues.append({"code": "NO_OWNED_ROUTE_EFFECT", "reason": "No eligible route effect in journal."})
            else:
                owned_version = original_routes[-1]["changes"]["route_version"]["after"]
                result = self.apply("RECOVERY", run, "restore_route",
                    {"service": SERVICE, "candidate_id": cid, "route_version": owned_version},
                    "recovery-route-%s-%s" % (owned_version, attempt), "AP-recovery-A")
                steps.append({"tool": "restore_route", "result": result})
                if result["status"] != "SUCCEEDED":
                    issues.append(result)
        elif d is not None:
            steps.append({"tool": "restore_route", "result": {"status": "VERIFIED_NO_OP",
                         "reason": "Candidate already has no traffic; no route overwritten.",
                         "current_route_owner": s["route_owner"]}})
        s = self.state()
        d = s["deployments"].get(cid)
        if d is not None and d["allocated_units"]:
            # Do not attempt stop when restoration is unresolved.
            if s["routing"].get(cid, 0):
                issues.append({"code": "RESOURCE_STILL_ROUTED", "reason": "Cleanup paused while route conflict remains."})
            else:
                result = self.apply("RECOVERY", run, "stop_candidate",
                    {"service": SERVICE, "candidate_id": cid},
                    "recovery-stop-" + str(attempt), "AP-recovery-A")
                steps.append({"tool": "stop_candidate", "result": result})
                if result["status"] != "SUCCEEDED":
                    issues.append(result)
                else:
                    result = self.apply("RECOVERY", run, "release_capacity",
                        {"service": SERVICE, "candidate_id": cid}, "recovery-release-" + str(attempt), "AP-recovery-A")
                    steps.append({"tool": "release_capacity", "result": result})
                    if result["status"] != "SUCCEEDED":
                        issues.append(result)
        for rid, r in self.state()["reservations"].items():
            if r["owner_run"] == run and r["status"] == "PENDING":
                result = self.apply("RECOVERY", run, "release_reservation",
                    {"service": SERVICE, "reservation_id": rid}, "recovery-reservation-%s-%s" % (rid, attempt), "AP-recovery-A")
                steps.append({"tool": "release_reservation", "result": result})
                if result["status"] != "SUCCEEDED":
                    issues.append(result)
        final = self.state()
        d = final["deployments"].get(cid)
        cleaned = (d is None or d["allocated_units"] == 0) and final["routing"].get(cid, 0) == 0
        cleaned = cleaned and not any(r["owner_run"] == run and r["status"] == "PENDING"
                                      for r in final["reservations"].values())
        status = "VERIFIED_RECOVERED" if cleaned and not issues else "RECOVERY_REVIEW_REQUIRED"
        if d is None and not steps and not issues:
            status = "NOT_NEEDED"
        report = {"status": status, "steps": steps, "issues": issues,
                  "final_routing": final["routing"], "committed_units": total_commitments(final),
                  "capacity_limit": final["capacity_limit"], "budget_version": final["budget_version"]}
        with self.db:
            self._event({"kind": "RECOVERY_REPORT", "run_id": run, "report": report})
        return report


def action(e, tool, args, key, actor="A", run="deploy-A", approval=None):
    args = {"service": SERVICE, **args}
    return e.apply(actor, run, tool, args, key, approval or "AP-" + run)


def start_canary(e):
    results = [
        action(e, "reserve_capacity", {"units": 20, "reservation_id": "r-A-1", "budget_version": 1}, "A-reserve-1"),
        action(e, "start_candidate", {"candidate_id": "candidate-v2", "reservation_id": "r-A-1",
                                      "config": "model-config-v2"}, "A-start"),
    ]
    if any(r["status"] != "SUCCEEDED" for r in results):
        raise RuntimeError("The reviewed canary fixture failed to initialize.")
    e.fixture_event("OBSERVATIONS")
    route = action(e, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10,
                                   "route_version": 1}, "A-route-10")
    if route["status"] != "SUCCEEDED":
        raise RuntimeError("The reviewed initial route was unexpectedly blocked.")


def cost_change(e):
    return action(e, "set_capacity_limit", {"limit": 70, "budget_version": 1},
                  "B-limit-70", actor="B", run="cost-B")


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    return {"assertion": label, "passed": True}


def run_scenario(name, path):
    e = Engine(path)
    assertions, recovery = [], None
    try:
        initial = e.state()
        if name == "stale_before_write":
            changed = cost_change(e)
            before = e.state()
            blocked = action(e, "reserve_capacity", {"units": 20, "reservation_id": "r-A-1",
                                                      "budget_version": 1}, "A-first")
            assertions += [check("Authorized B update succeeds", changed["status"] == "SUCCEEDED"),
                check("Stale approval blocks A before its first write", blocked["code"] == "STALE_BUDGET_VERSION"),
                check("Blocked proposal does not mutate service state", before == e.state()),
                check("No candidate or reservation was created", not e.state()["deployments"] and not e.state()["reservations"])]
            recovery = e.recover()
            assertions.append(check("Recovery is correctly unnecessary", recovery["status"] == "NOT_NEEDED"))
            outcome = "PAUSED_STALE_APPROVAL"
        else:
            start_canary(e)
            if name == "normal_success":
                reserved = action(e, "reserve_capacity", {"units": 20, "reservation_id": "r-A-2",
                                                           "budget_version": 1}, "A-reserve-2")
                scaled = action(e, "scale_candidate", {"candidate_id": "candidate-v2",
                                                        "reservation_id": "r-A-2"}, "A-scale")
                e.fixture_event("OBSERVATIONS")
                route = action(e, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 50,
                                                "route_version": 2}, "A-route-50")
                assertions += [check("Valid expansion completes", reserved["status"] == scaled["status"] == "SUCCEEDED"),
                    check("Reviewed rollout stage reaches 50%", route["status"] == "SUCCEEDED" and e.state()["routing"]["candidate-v2"] == 50),
                    check("Reservations are not double counted", total_commitments(e.state()) == 80),
                    check("Stable fallback remains allocated", e.state()["stable_units"] == 40)]
                outcome = "COMPLETED_APPROVED_50_PERCENT_STAGE"
            else:
                changed = cost_change(e)
                before = e.state()
                blocked = action(e, "reserve_capacity", {"units": 20, "reservation_id": "r-A-2",
                                                          "budget_version": 1}, "A-reserve-2")
                assertions += [check("B lowers limit to 70 legitimately", changed["status"] == "SUCCEEDED" and e.state()["capacity_limit"] == 70),
                    check("A expansion is blocked as stale", blocked["code"] == "STALE_BUDGET_VERSION"),
                    check("Rejected expansion leaves state unchanged", e.state() == before)]
                if name == "routing_conflict":
                    c = action(e, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 20,
                                                "route_version": 2}, "C-route-20", actor="C", run="route-C")
                    assertions.append(check("C's separately approved routing update succeeds", c["status"] == "SUCCEEDED"))
                e.fixture_event("CANDIDATE_FAILURE")
                recovery = e.recover()
                final = e.state()
                assertions.append(check("B's limit and version survive recovery", final["capacity_limit"] == 70 and final["budget_version"] == 2))
                if name == "routing_conflict":
                    assertions += [check("Recovery flags the superseding routing conflict", recovery["status"] == "RECOVERY_REVIEW_REQUIRED"),
                        check("C's routing update is preserved", final["routing"]["candidate-v2"] == 20 and final["route_owner"] == "route-C"),
                        check("Resources still receiving traffic are retained", total_commitments(final) == 60)]
                    outcome = "FAILED_RECOVERY_REVIEW_REQUIRED"
                else:
                    assertions += [check("Recovery is verified", recovery["status"] == "VERIFIED_RECOVERED"),
                        check("Stable routing is restored", final["routing"] == {"stable-v1": 100, "candidate-v2": 0}),
                        check("Candidate stop is confirmed before capacity release", final["deployments"]["candidate-v2"]["status"] == "STOPPED" and total_commitments(final) == 40)]
                    repeat = e.recover()
                    assertions.append(check("Duplicate recovery does not release twice", repeat["status"] == "VERIFIED_RECOVERED" and e.state() == final))
                    outcome = "FAILED_RECOVERED"
        return {"scenario": name, "mode": "DETERMINISTIC SIMULATED INFRASTRUCTURE",
                "actor_mode": "SCRIPTED OPERATOR FIXTURES; NO LIVE MODEL CALLS",
                "policy": POLICY, "task_outcome": outcome,
                "recovery_outcome": recovery["status"] if recovery else "NOT_REQUESTED",
                "assertions": assertions, "initial_state": initial, "final_state": e.state(),
                "recovery": recovery, "events": e.events()}
    finally:
        e.close()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.e = Engine(self.path)

    def tearDown(self):
        self.e.close()
        self.directory.cleanup()

    def reserve(self, key="reserve", units=20, rid="r1", approval="AP-deploy-A"):
        return action(self.e, "reserve_capacity", {"units": units, "reservation_id": rid,
                      "budget_version": 1}, key, approval=approval)

    def test_four_reviewed_scenarios(self):
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                report = run_scenario(name, Path(self.directory.name) / (name + ".sqlite3"))
                self.assertTrue(all(a["passed"] for a in report["assertions"]))

    def test_missing_approval_no_mutation(self):
        before = self.e.state()
        self.assertEqual(self.reserve(approval="prose-manager-approved")["code"], "APPROVAL_MISSING")
        self.assertEqual(self.e.state(), before)

    def test_expired_approval(self):
        self.e.fixture_event("ADVANCE_CLOCK", ticks=100)
        self.assertEqual(self.reserve()["code"], "APPROVAL_EXPIRED")

    def test_revoked_approval(self):
        self.e.fixture_event("REVOKE_APPROVAL", approval_id="AP-deploy-A")
        self.assertEqual(self.reserve()["code"], "APPROVAL_REVOKED")

    def test_modified_plan(self):
        self.e.fixture_event("EDIT_PLAN", max_units=60)
        self.assertEqual(self.reserve()["code"], "PLAN_CHANGED")

    def test_budget_cannot_erase_reservations(self):
        self.assertEqual(self.reserve(units=40)["status"], "SUCCEEDED")
        self.assertEqual(total_commitments(self.e.state()), 80)
        self.assertEqual(cost_change(self.e)["code"], "LIMIT_BELOW_COMMITMENTS")
        self.assertEqual(self.e.state()["capacity_limit"], 100)

    def test_duplicate_start_and_key_mismatch(self):
        start_canary(self.e)
        args = {"candidate_id": "candidate-v2", "reservation_id": "r-A-1", "config": "model-config-v2"}
        replay = action(self.e, "start_candidate", args, "A-start")
        self.assertTrue(replay["replayed"])
        self.assertEqual(total_commitments(self.e.state()), 60)
        args["config"] = "unapproved-config"
        self.assertEqual(action(self.e, "start_candidate", args, "A-start")["code"], "IDEMPOTENCY_KEY_CONFLICT")

    def test_stale_route_write(self):
        start_canary(self.e)
        r = action(self.e, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 20,
                                        "route_version": 1}, "old-route")
        self.assertEqual(r["code"], "ROUTE_VERSION_CONFLICT")

    def test_dependency_prevents_cleanup(self):
        start_canary(self.e)
        r = action(self.e, "add_dependency", {"candidate_id": "candidate-v2", "dependency_id": "legitimate-job"},
                   "C-dependency", actor="C", run="route-C")
        self.assertEqual(r["status"], "SUCCEEDED")
        self.e.fixture_event("CANDIDATE_FAILURE")
        recovery = self.e.recover()
        self.assertEqual(recovery["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(total_commitments(self.e.state()), 60)
        self.assertEqual(len(self.e.state()["deployments"]["candidate-v2"]["dependencies"]), 1)

    def test_unknown_stop_retains_capacity_until_reconciled(self):
        start_canary(self.e)
        self.e.fixture_event("CANDIDATE_FAILURE")
        self.e.fixture_event("STOP_TIMEOUT")
        first = self.e.recover()
        self.assertEqual(first["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(total_commitments(self.e.state()), 60)
        self.e.fixture_event("RECONCILE_STOP_CONFIRMED")
        self.assertEqual(self.e.recover()["status"], "VERIFIED_RECOVERED")
        self.assertEqual(total_commitments(self.e.state()), 40)

    def test_unhealthy_fallback_not_restored(self):
        start_canary(self.e)
        self.e.fixture_event("FALLBACK_UNHEALTHY")
        self.e.fixture_event("CANDIDATE_FAILURE")
        self.assertEqual(self.e.recover()["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(self.e.state()["routing"]["candidate-v2"], 10)

    def test_recovery_checks_current_permission(self):
        start_canary(self.e)
        self.e.fixture_event("REVOKE_RECOVERY_CAPABILITY")
        self.e.fixture_event("CANDIDATE_FAILURE")
        self.assertEqual(self.e.recover()["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(total_commitments(self.e.state()), 60)

    def test_stale_and_bad_metrics(self):
        start_canary(self.e)
        self.e.fixture_event("OBSERVATIONS", samples=10)
        args = {"candidate_id": "candidate-v2", "candidate_percent": 20, "route_version": 2}
        self.assertEqual(action(self.e, "set_route", args, "too-few")["code"], "INSUFFICIENT_SAMPLES")
        self.e.fixture_event("OBSERVATIONS", error_rate=5)
        self.assertEqual(action(self.e, "set_route", args, "bad-quality")["code"], "PROMOTION_GATE_FAILED")
        self.e.fixture_event("OBSERVATIONS")
        self.e.fixture_event("ADVANCE_CLOCK", ticks=21)
        self.assertEqual(action(self.e, "set_route", args, "stale-metrics")["code"], "METRICS_STALE")

    def test_cross_service_and_unsupported_tool(self):
        args = {"units": 20, "reservation_id": "x", "budget_version": 1, "service": "other-service"}
        self.assertEqual(self.e.apply("A", "deploy-A", "reserve_capacity", args, "cross", "AP-deploy-A")["code"], "SERVICE_SCOPE_MISMATCH")
        self.assertEqual(self.e.apply("A", "deploy-A", "shell", {}, "shell", "AP-deploy-A")["code"], "UNSUPPORTED_TOOL")

    def test_invalid_negative_or_boolean_units(self):
        for i, value in enumerate([-1, True, "20"]):
            self.assertEqual(self.reserve(key="bad-" + str(i), units=value)["code"], "INVALID_ARGUMENT")

    def test_durable_state_and_journal(self):
        start_canary(self.e)
        original = self.e.state()
        other = Engine(self.path, create=False)
        try:
            self.assertEqual(other.state(), original)
            self.assertTrue(other.events())
            self.assertEqual(cost_change(other)["status"], "SUCCEEDED")
            self.assertEqual(self.e.state()["capacity_limit"], 70)
        finally:
            other.close()

    def test_recovered_workflow_cannot_resume(self):
        start_canary(self.e)
        self.e.fixture_event("CANDIDATE_FAILURE")
        self.assertEqual(self.e.recover()["status"], "VERIFIED_RECOVERED")
        self.assertEqual(self.reserve(key="resume", rid="resume-reservation")["code"], "WORKFLOW_PAUSED")
        self.assertEqual(total_commitments(self.e.state()), 40)

    def test_two_independent_reservations_cannot_overcommit(self):
        self.e.fixture_event("SECOND_APPROVED_PLAN")
        barrier = threading.Barrier(2)

        def competing_admission(index):
            coordinator = Engine(self.path, create=False)
            try:
                # Both writers observe the same initially feasible total.
                observed = coordinator.state()
                self.assertEqual(total_commitments(observed), 40)
                barrier.wait(timeout=5)
                run = "deploy-A" if index == 0 else "deploy-A2"
                return action(coordinator, "reserve_capacity", {
                    "units": 40, "reservation_id": "race-" + str(index),
                    "budget_version": observed["budget_version"]},
                    "race-" + str(index), run=run)
            finally:
                coordinator.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(competing_admission, [0, 1]))
        self.assertEqual(sorted(r["status"] for r in results), ["BLOCKED", "SUCCEEDED"])
        denied = next(r for r in results if r["status"] == "BLOCKED")
        self.assertEqual(denied["code"], "CAPACITY_LIMIT_EXCEEDED")
        self.assertEqual(denied["details"]["projected_usage"], 120)
        self.assertEqual(total_commitments(self.e.state()), 80)


SCENARIOS = ("flagship", "normal_success", "stale_before_write", "routing_conflict")


def summarize(report):
    s = report["final_state"]
    print("\nSCENARIO: " + report["scenario"])
    for e in report["events"]:
        if e["kind"] == "ACTION":
            print("  %s: %s -> %s (%s)" % (e["actor"], e["tool"], e["result"]["status"], e["result"]["code"]))
        elif e["kind"] == "TEST_HARNESS":
            print("  [SIMULATED FIXTURE] " + e["fixture_type"])
    for a in report["assertions"]:
        print("  PASS: " + a["assertion"])
    print("  Task outcome: " + report["task_outcome"])
    print("  Recovery outcome: " + report["recovery_outcome"])
    print("  Final: stable %d%%, candidate %d%%, commitments %d, limit %d" % (
        s["routing"]["stable-v1"], s["routing"]["candidate-v2"], total_commitments(s), s["capacity_limit"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="Run independent engine checks; no API calls.")
    parser.add_argument("--scenario", choices=("all",) + SCENARIOS, default="all")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "rehearsal_runs")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EngineTests))
        return 0 if result.wasSuccessful() else 1
    directory = args.output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    print("ORKESTRYN - FIRST ENGINE MILESTONE")
    print("SIMULATED infrastructure, SCRIPTED operators, LABELLED injected faults.")
    print("No Nebius calls, no GPU operations, no API key and no inference credit usage.")
    reports = []
    for name in SCENARIOS if args.scenario == "all" else [args.scenario]:
        report = run_scenario(name, directory / (name + ".sqlite3"))
        (directory / (name + ".json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
        reports.append({"scenario": name, "task_outcome": report["task_outcome"],
                        "recovery_outcome": report["recovery_outcome"],
                        "checks_passed": len(report["assertions"])})
        summarize(report)
    (directory / "summary.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    print("\nAll reviewed scenario assertions passed.")
    print("Evidence (JSON reports and SQLite journals): " + str(directory))
    print("Next milestone: connect Nemotron action proposals to this checked execution path.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (sqlite3.Error, OSError, RuntimeError, AssertionError) as exc:
        print("SIMULATOR FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
