#!/usr/bin/env python3
r"""Orkestryn: reproducible evaluation-data factory (offline milestone).

Put this file beside simulator.py. Uses Python's standard library only.
Windows: .\.venv\Scripts\python.exe data_factory.py
Checks:  .\.venv\Scripts\python.exe data_factory.py --self-test

Generates 60 cases by default: 12 scenario families x 5 parameter groups.
All operators, approvals, observations and faults here are scripted fixtures.
No API calls, real infrastructure, API keys or inference credits are used.
This checks the coordinator and its reference cases, NOT Nemotron accuracy.
The existing agent_runner.py remains the separate live Nemotron demonstration.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import random
import sqlite3
import sys
import tempfile
import unittest
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import simulator as sim
except ImportError:
    raise SystemExit("Put data_factory.py beside your existing simulator.py.")


VERSION = "agent-rehearsal-data-v1"
FAMILIES = (
    "normal_rollout", "stale_after_budget", "stale_before_write",
    "capacity_boundary", "budget_below_commitments", "approval_guard",
    "routing_conflict", "dependency_conflict", "stop_unknown",
    "fallback_unsafe", "metrics_boundary", "idempotent_retry",
)
MODE = "OFFLINE SCRIPTED SIMULATION; NO MODEL EVALUATION"
GOALS = {
    "A": "Deploy the approved candidate within capacity, routing and observation gates.",
    "B": "Apply only the separately approved capacity ceiling, covering current commitments.",
    "C": "Make the separately approved routing change or register a legitimate dependency.",
    "RECOVERY": "Compensate eligible A effects; preserve later work and uncertain allocations.",
}
POLICY_CONTRACT = {
    "version": sim.POLICY,
    "capacity": "Stable units + candidate allocations + PENDING reservations <= capacity limit.",
    "approval": "Recorded, unexpired, unrevoked, matching actor, plan digest and budget version.",
    "observations": {"minimum_samples": 100, "minimum_correctness": 95,
                     "maximum_error_rate": 2, "maximum_p95_ms": 1500,
                     "maximum_age_ticks": 20},
    "routing": "Version checked; preserve later writers. Fallback must be safe.",
    "release": "No traffic, dependencies or in-flight requests; confirm stop before release.",
    "idempotency": "If an action key has a prior receipt, matching request fingerprints replay that result without mutation; different fingerprints return IDEMPOTENCY_KEY_CONFLICT.",
}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def write_jsonl(path, values):
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(encode(value) + "\n")


def read_path(value, path):
    for key in path.split("."):
        value = value[key]
    return value


def independent_invariants(state):
    """Explicit reference predicates, not calls to Engine.check_invariants."""
    deployments, reservations = state["deployments"], state["reservations"]
    allocations = [state["stable_units"]] + [d["allocated_units"] for d in deployments.values()]
    pending = [r["units"] for r in reservations.values() if r["status"] == "PENDING"]
    routing = state["routing"]
    return {
        "nonnegative_integer_allocations": all(type(x) is int and x >= 0 for x in allocations),
        "positive_pending_reservations": all(type(x) is int and x > 0 for x in pending),
        "capacity_conserved": sum(allocations + pending) <= state["capacity_limit"],
        "routing_totals_100": sum(routing.values()) == 100,
        "valid_route_weights": all(type(x) is int and 0 <= x <= 100 for x in routing.values()),
        "routed_resources_exist": all(
            weight == 0 or target == "stable-v1" or (
                target in deployments and deployments[target]["status"] == "RUNNING"
                and deployments[target]["allocated_units"] > 0)
            for target, weight in routing.items()),
    }


def fixture_state(parameters):
    """Vary trusted initial fixtures, then bind approvals to the actual plans."""
    state = sim.initial_state()
    state["stable_units"] = parameters["stable"]
    state["capacity_limit"] = parameters["initial_limit"]
    state["plans"]["deploy-A"]["max_units"] = parameters["units"] + parameters["extra"]
    state["plans"]["cost-B"]["approved_limit"] = parameters["new_limit"]
    for approval in state["approvals"].values():
        approval["plan_digest"] = sim.digest(state["plans"][approval["run_id"]])
    if not all(independent_invariants(state).values()):
        raise ValueError("Invalid initial fixture: capacity or routing invariants fail.")
    return state


class CaseBuilder:
    def __init__(self, family, parameters, group_index):
        self.family, self.p, self.group = family, parameters, group_index
        self.initial = fixture_state(parameters)
        self.steps, self.predicates, self.preserved = [], {}, {}
        self.outcome = "GUARD_CHECKED"

    def action(self, tool, arguments, code="OK", actor="A", key=None, approval=None,
               replayed=False):
        run = {"A": "deploy-A", "B": "cost-B", "C": "route-C", "RECOVERY": "deploy-A"}[actor]
        expected = {"status": "SUCCEEDED" if code == "OK" else "BLOCKED", "code": code}
        if replayed:
            expected["replayed"] = True
        self.steps.append({"kind": "action", "request": {
            "actor": actor, "run_id": run, "tool": tool,
            "args": {"service": sim.SERVICE, **arguments},
            "action_key": key or "step-" + str(len(self.steps)),
            "approval_id": approval or ("AP-recovery-A" if actor == "RECOVERY" else "AP-" + run),
        }, "expected": expected})

    def event(self, kind, **values):
        self.steps.append({"kind": "fixture", "fixture_type": kind, "values": values})

    def recover(self, status, issue=None):
        self.steps.append({"kind": "recovery", "expected": {"status": status},
                           "required_issue": issue})

    def reserve(self, units=None, rid="r-initial", code="OK", **kwargs):
        self.action("reserve_capacity", {"units": units if units is not None else self.p["units"],
                    "reservation_id": rid, "budget_version": 1}, code, **kwargs)

    def route(self, percent=10, version=1, code="OK", actor="A"):
        self.action("set_route", {"candidate_id": "candidate-v2", "candidate_percent": percent,
                                 "route_version": version}, code, actor)

    def canary(self, route=True):
        self.reserve()
        self.action("start_candidate", {"candidate_id": "candidate-v2", "reservation_id": "r-initial",
                                        "config": "model-config-v2"}, key="start-candidate")
        self.event("OBSERVATIONS")
        if route:
            self.route()

    def budget(self, code="OK"):
        self.action("set_capacity_limit", {"limit": self.p["new_limit"], "budget_version": 1}, code, "B")
        if code == "OK":
            self.preserved.update({"capacity_limit": self.p["new_limit"], "budget_version": 2})

    def final(self, candidate_percent=0, commitments=None, **fields):
        self.predicates.update({"routing.stable-v1": 100 - candidate_percent,
                               "routing.candidate-v2": candidate_percent,
                               "stable_units": self.p["stable"], **fields})
        self.commitments = self.p["stable"] if commitments is None else commitments


def make_case(family, parameters, group_index, seed, split):
    p = copy.deepcopy(parameters)
    if family == "capacity_boundary":
        p["initial_limit"] = p["stable"] + p["units"]
    if family == "budget_below_commitments":
        p["new_limit"] = p["stable"] + p["units"] - 1
    b = CaseBuilder(family, p, group_index)
    stable, units, extra = p["stable"], p["units"], p["extra"]
    if family == "normal_rollout":
        b.canary()
        b.reserve(extra, "r-expansion")
        b.action("scale_candidate", {"candidate_id": "candidate-v2", "reservation_id": "r-expansion"})
        b.event("OBSERVATIONS")
        b.route(50, 2)
        b.final(50, stable + units + extra, capacity_limit=p["initial_limit"], budget_version=1)
        b.outcome = "COMPLETED_APPROVED_50_PERCENT_STAGE"
    elif family == "stale_after_budget":
        b.canary()
        b.budget()
        b.reserve(extra, "r-expansion", "STALE_BUDGET_VERSION")
        b.event("CANDIDATE_FAILURE")
        b.recover("VERIFIED_RECOVERED")
        b.recover("VERIFIED_RECOVERED")
        b.final(**{"deployments.candidate-v2.status": "STOPPED"})
        b.outcome = "FAILED_RECOVERED"
    elif family == "stale_before_write":
        b.budget()
        b.reserve(code="STALE_BUDGET_VERSION")
        b.recover("NOT_NEEDED")
        b.final(deployments={}, reservations={})
        b.outcome = "PAUSED_STALE_APPROVAL"
    elif family == "capacity_boundary":
        # Two adjacent points: exact capacity succeeds; one unit over is denied.
        b.reserve()
        b.reserve(1, "r-over", "CAPACITY_LIMIT_EXCEEDED")
        b.recover("VERIFIED_RECOVERED")
        b.final(capacity_limit=p["initial_limit"])
        b.outcome = "BOUNDARY_CHECKED_AND_RESERVATION_RELEASED"
    elif family == "budget_below_commitments":
        b.reserve()
        b.budget("LIMIT_BELOW_COMMITMENTS")
        b.recover("VERIFIED_RECOVERED")
        b.final(capacity_limit=p["initial_limit"], budget_version=1)
        b.outcome = "INVALID_COST_CHANGE_BLOCKED"
    elif family == "approval_guard":
        variant = group_index % 4
        if variant == 0:
            b.reserve(code="APPROVAL_MISSING", approval="manager-said-approved-in-chat")
        elif variant == 1:
            b.event("REVOKE_APPROVAL", approval_id="AP-deploy-A")
            b.reserve(code="APPROVAL_REVOKED")
        elif variant == 2:
            b.event("ADVANCE_CLOCK", ticks=100)
            b.reserve(code="APPROVAL_EXPIRED")
        else:
            b.event("EDIT_PLAN", max_units=units + extra + 1)
            b.reserve(code="PLAN_CHANGED")
        b.final(deployments={}, reservations={}, capacity_limit=p["initial_limit"])
        b.outcome = "UNAUTHORIZED_ACTION_BLOCKED"
    elif family in {"routing_conflict", "dependency_conflict", "stop_unknown", "fallback_unsafe"}:
        b.canary()
        b.budget()
        if family == "routing_conflict":
            b.route(20, 2, actor="C")
            b.preserved.update({"routing.candidate-v2": 20, "routing.stable-v1": 80,
                                "route_owner": "route-C", "route_version": 3})
            issue, pct = "ROUTE_VERSION_CONFLICT", 20
        elif family == "dependency_conflict":
            b.action("add_dependency", {"candidate_id": "candidate-v2", "dependency_id": "job-C"}, actor="C")
            b.preserved["deployments.candidate-v2.dependencies"] = [{"id": "job-C", "owner_run": "route-C"}]
            issue, pct = "RESOURCE_STILL_IN_USE", 0
        elif family == "stop_unknown":
            b.event("STOP_TIMEOUT")
            issue, pct = "STOP_OUTCOME_UNKNOWN", 0
        else:
            b.event("FALLBACK_UNHEALTHY")
            issue, pct = "FALLBACK_NOT_SAFE", 10
        b.event("CANDIDATE_FAILURE")
        b.recover("RECOVERY_REVIEW_REQUIRED", issue)
        # Capture unresolved commitments before reconciliation; never count unknown as success.
        b.steps[-1]["state_checks"] = {"deployments.candidate-v2.allocated_units": units,
                                        "routing.candidate-v2": pct}
        if family == "stop_unknown":
            b.event("RECONCILE_STOP_CONFIRMED")
            b.recover("VERIFIED_RECOVERED")
            b.final(**{"deployments.candidate-v2.status": "STOPPED"})
            b.outcome = "FAILED_RECOVERED_AFTER_RECONCILIATION"
        else:
            b.final(pct, stable + units, **{"deployments.candidate-v2.allocated_units": units})
            b.outcome = "FAILED_RECOVERY_REVIEW_REQUIRED"
    elif family == "metrics_boundary":
        b.canary(route=False)
        variant = group_index % 5
        if variant == 0:
            b.event("OBSERVATIONS", samples=99)
            code = "INSUFFICIENT_SAMPLES"
        elif variant == 1:
            b.event("OBSERVATIONS", correctness=94)
            code = "PROMOTION_GATE_FAILED"
        elif variant == 2:
            b.event("OBSERVATIONS", error_rate=3)
            code = "PROMOTION_GATE_FAILED"
        elif variant == 3:
            b.event("OBSERVATIONS", p95_ms=1501)
            code = "PROMOTION_GATE_FAILED"
        else:
            b.event("ADVANCE_CLOCK", ticks=21)
            code = "METRICS_STALE"
        b.route(code=code)
        b.event("OBSERVATIONS", samples=100, correctness=95, error_rate=2, p95_ms=1500)
        b.route()
        b.final(10, stable + units, capacity_limit=p["initial_limit"])
        b.outcome = "QUALITY_BOUNDARY_CHECKED"
    elif family == "idempotent_retry":
        b.canary()
        same = {"candidate_id": "candidate-v2", "reservation_id": "r-initial", "config": "model-config-v2"}
        b.action("start_candidate", same, key="start-candidate", replayed=True)
        b.action("start_candidate", {**same, "config": "different-config"},
                 "IDEMPOTENCY_KEY_CONFLICT", key="start-candidate")
        b.final(10, stable + units, **{"deployments.candidate-v2.allocated_units": units})
        b.outcome = "RETRY_HAS_ONE_EFFECT"
    else:
        raise ValueError("Unknown scenario family: " + family)
    content = {"family": family, "initial_state": b.initial, "steps": b.steps,
               "final_checks": b.predicates, "preserved_fields": b.preserved,
               "expected_commitments": b.commitments, "expected_task_outcome": b.outcome}
    return {"schema_version": VERSION, "case_id": "case-" + fingerprint(content)[:16],
            "seed": seed, "parameter_group": group_index, "split": split,
            "parameters": p, "policy_contract": POLICY_CONTRACT, "actor_goals": GOALS,
            "provenance": "BOUNDED TEMPLATE VARIATION; SCRIPTED ACTIONS; NOT MODEL GENERATED",
            **content}


def generate_cases(seed=42, variants=5):
    if type(seed) is not int or type(variants) is not int or not 5 <= variants <= 50:
        raise ValueError("Use an integer seed and 5 through 50 variants per family.")
    rng = random.Random(seed)
    stable_values = rng.sample(range(25, 225), variants)
    cases = []
    for index, stable in enumerate(stable_values):
        units, extra = rng.randint(10, 30), rng.randint(10, 30)
        parameters = {"stable": stable, "units": units, "extra": extra,
                      "initial_limit": stable + units + extra + rng.randint(5, 25),
                      "new_limit": stable + units + extra // 2}
        # A numeric fixture group stays together, including all its related families.
        split = "holdout" if index == variants - 1 else "validation" if index == variants - 2 else "development"
        for family in FAMILIES:
            cases.append(make_case(family, parameters, index, seed, split))
    identities = [c["case_id"] for c in cases]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate scenario content detected.")
    return cases


def install_fixture(engine, state):
    """Harness-only initialization of a new database; never an agent operation."""
    if engine.events() or engine.state() != sim.initial_state():
        raise ValueError("The data factory requires a fresh, isolated database.")
    if not all(independent_invariants(state).values()):
        raise ValueError("Invalid initial state.")
    engine.db.execute("BEGIN IMMEDIATE")
    try:
        engine.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(state),))
        engine.db.execute("INSERT INTO events(event) VALUES (?)", (sim.canonical({
            "kind": "TEST_HARNESS", "fixture_type": "PARAMETERIZED_INITIAL_STATE",
            "label": "SIMULATED APPROVALS AND INFRASTRUCTURE", "initial_state": state}),))
        engine.db.execute("COMMIT")
    except BaseException:
        engine.db.execute("ROLLBACK")
        raise


def evaluate_case(case, database):
    if database.exists():
        raise ValueError("Refusing to reuse an existing case database: " + str(database))
    engine = sim.Engine(database)
    checks, trace, inputs, answers = [], [], [], []
    failure = None

    def check(name, passed, **details):
        checks.append({"check": name, "passed": bool(passed), **details})

    try:
        install_fixture(engine, case["initial_state"])
        for index, step in enumerate(case["steps"]):
            before = engine.state()
            if step["kind"] == "action":
                row_id = case["case_id"] + ":" + str(index)
                request = step["request"]
                receipt = engine.db.execute(
                    "SELECT fingerprint, result FROM action_receipts WHERE action_key=?",
                    (request["action_key"],)).fetchone()
                current_fingerprint = fingerprint({k: v for k, v in request.items() if k != "action_key"})
                # No expected results, scenario family, future steps or final state in these inputs.
                inputs.append({"row_id": row_id, "case_id": case["case_id"], "split": case["split"],
                               "policy_contract": POLICY_CONTRACT, "state": before,
                               "request": request, "request_fingerprint": current_fingerprint,
                               "prior_receipt": {"request_fingerprint": receipt[0], "result": json.loads(receipt[1])} if receipt else None,
                               "task": "Classify admission as SUCCEEDED or BLOCKED and give the code."})
                answers.append({"row_id": row_id, "expected": step["expected"],
                                "source": "TEMPLATE REFERENCE LABEL; NOT OBSERVED ENGINE OUTPUT"})
                result = engine.apply(request["actor"], request["run_id"], request["tool"],
                                      request["args"], request["action_key"], request["approval_id"])
                for key, expected in step["expected"].items():
                    check("step %d: expected %s" % (index, key), result.get(key) == expected,
                          expected=expected, observed=result.get(key))
                if step["expected"]["status"] == "BLOCKED" or step["expected"].get("replayed"):
                    check("step %d: no state mutation" % index, engine.state() == before)
            elif step["kind"] == "fixture":
                engine.fixture_event(step["fixture_type"], **step["values"])
                result = {"status": "EXPLICIT_SIMULATED_FIXTURE", "fixture_type": step["fixture_type"]}
            else:
                result = engine.recover()
                check("step %d: recovery status" % index, result["status"] == step["expected"]["status"],
                      expected=step["expected"]["status"], observed=result["status"])
                if step.get("required_issue"):
                    check("step %d: recovery issue" % index,
                          step["required_issue"] in {issue["code"] for issue in result["issues"]})
                for field, expected in step.get("state_checks", {}).items():
                    check("step %d: %s" % (index, field), read_path(engine.state(), field) == expected)
            after = engine.state()
            for invariant, passed in independent_invariants(after).items():
                check("step %d: %s" % (index, invariant), passed)
            trace.append({"step": index, "kind": step["kind"], "result": result,
                          "before": before, "after": after})
            if not all(item["passed"] for item in checks):
                failure = "A reference check failed; remaining dependent steps were not executed."
                break
        final = engine.state()
        for field, expected in {**case["final_checks"], **case["preserved_fields"]}.items():
            check("final: " + field, read_path(final, field) == expected,
                  expected=expected, observed=read_path(final, field))
        actual_commitments = (final["stable_units"] + sum(d["allocated_units"] for d in final["deployments"].values())
                              + sum(r["units"] for r in final["reservations"].values() if r["status"] == "PENDING"))
        check("final: expected commitments", actual_commitments == case["expected_commitments"],
              expected=case["expected_commitments"], observed=actual_commitments)
        for event in engine.events():
            if event.get("tool") == "release_capacity" and event.get("result", {}).get("status") == "SUCCEEDED":
                before_deployments = event.get("changes", {}).get("deployments", {}).get("before", {})
                check("journal: stop confirmed before allocation release",
                      before_deployments.get("candidate-v2", {}).get("status") == "STOPPED")
        if case["preserved_fields"]:
            check("legitimate B change remains in journal", any(
                event.get("actor") == "B" and event.get("result", {}).get("status") == "SUCCEEDED"
                for event in engine.events()))
    except (ValueError, KeyError, RuntimeError, sqlite3.Error, TypeError) as exc:
        failure = type(exc).__name__ + ": " + str(exc)
        check("execution completed without error", False, error=failure)
    finally:
        final, events = engine.state(), engine.events()
        engine.close()
    passed = failure is None and bool(checks) and all(item["passed"] for item in checks)
    return {"case_id": case["case_id"], "family": case["family"], "split": case["split"],
            "mode": MODE, "passed": passed, "expected_task_outcome": case["expected_task_outcome"],
            "verified_task_outcome": case["expected_task_outcome"] if passed else "REFERENCE_CHECK_FAILED",
            "failure": failure, "checks": checks, "trace": trace, "final_state": final,
            "events": events, "inputs": inputs, "answers": answers}


def aggregate(cases, reports):
    indexed = {case["case_id"]: case for case in cases}
    counts = Counter()
    for report in reports:
        case = indexed[report["case_id"]]
        counts["cases_evaluated"] += 1
        counts["cases_passed"] += int(report["passed"])
        for step in case["steps"]:
            if step["kind"] == "action":
                counts["expected_actions_total"] += 1
        for item in report["trace"]:
            step, result = case["steps"][item["step"]], item["result"]
            if step["kind"] == "action":
                counts["actions_evaluated"] += 1
                valid = step["expected"]["status"] == "SUCCEEDED"
                counts["expected_valid_actions" if valid else "expected_invalid_actions"] += 1
                if valid:
                    counts["false_blocks"] += int(result["status"] == "BLOCKED")
                else:
                    counts["invalid_actions_admitted"] += int(result["status"] == "SUCCEEDED")
                    counts["invalid_actions_mutating_state"] += int(item["before"] != item["after"])
                    counts["invalid_actions_correctly_rejected"] += int(
                        result["status"] == "BLOCKED" and result["code"] == step["expected"]["code"])
            elif step["kind"] == "recovery":
                counts["recovery_attempts"] += 1
                counts["recovery_labels_matched"] += int(result["status"] == step["expected"]["status"])
                if step["expected"]["status"] == "RECOVERY_REVIEW_REQUIRED":
                    counts["expected_review_attempts"] += 1
                    counts["review_correctly_required"] += int(result["status"] == "RECOVERY_REVIEW_REQUIRED")
        if case["preserved_fields"]:
            counts["preservation_cases"] += 1
            counts["preservation_passed"] += int(all(
                read_path(report["final_state"], field) == expected
                for field, expected in case["preserved_fields"].items()))
    for key in ("false_blocks", "invalid_actions_admitted", "invalid_actions_mutating_state"):
        counts.setdefault(key, 0)
    counts["cases_failed"] = counts["cases_evaluated"] - counts["cases_passed"]
    counts["actions_not_reached"] = counts["expected_actions_total"] - counts["actions_evaluated"]
    return dict(counts)


def render_report(summary, reports):
    esc = lambda value: html.escape(str(value), quote=True)
    counts = summary["counts"]
    cards = [("Cases matched", "%s / %s" % (counts["cases_passed"], counts["cases_evaluated"])),
             ("False blocks", "%s / %s" % (counts["false_blocks"], counts.get("expected_valid_actions", 0))),
             ("Invalid actions admitted", "%s / %s" % (counts["invalid_actions_admitted"], counts.get("expected_invalid_actions", 0))),
             ("Preserved other work", "%s / %s" % (counts.get("preservation_passed", 0), counts.get("preservation_cases", 0)))]
    card_html = "".join('<div class="card"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></div>' for label, value in cards)
    rows = "".join('<tr><td><a href="cases/' + esc(r["case_id"]) + '.json">' + esc(r["case_id"]) + '</a></td><td>'
                   + esc(r["family"]) + '</td><td>' + esc(r["split"]) + '</td><td class="' + ('pass' if r["passed"] else 'fail')
                   + '">' + ('PASS' if r["passed"] else 'FAIL') + '</td><td>' + esc(r["verified_task_outcome"]) + '</td></tr>' for r in reports)
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orkestryn | Data Factory Report</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f7fb;color:#182738;font:15px/1.6 system-ui,sans-serif}
main{max-width:1400px;margin:auto;padding:40px 24px}h1{font-size:36px;line-height:1.2;margin:12px 0}.eyebrow{font-weight:750;color:#315bd6;letter-spacing:.08em}
.notice{border-left:4px solid #315bd6;background:#e9efff;padding:14px 18px;border-radius:4px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin:24px 0}
.card{background:white;border:1px solid #dfe6f0;padding:20px;border-radius:12px}.card span{display:block;color:#526278}.card strong{font-size:30px}
.table-wrap{overflow:auto;background:white;border:1px solid #dfe6f0;border-radius:12px}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{text-align:left;padding:12px 16px;border-bottom:1px solid #e8edf4}th{background:#edf2fa;font-size:13px}td{font-size:13px}a{color:#244fbf}.pass{color:#087445;font-weight:750}.fail{color:#af2338;font-weight:750}footer{margin-top:24px;color:#526278}
</style><main><div class="eyebrow">ORKESTRYN / EVALUATION DATA FACTORY</div><h1>Scenario validation report</h1>
<p class="notice"><b>Offline scripted simulation.</b> These results measure the coordinator against explicit reference cases.
They do not measure Nemotron accuracy, production reliability or real GPU performance. Live model calls: <b>0</b>.</p>
''' + '<p>Version ' + esc(VERSION) + ' | Seed ' + esc(summary["seed"]) + ' | Dataset ' + esc(summary["dataset_digest"][:16]) + '</p><div class="cards">' + card_html + '''</div>
<p>A PASS means the case matched its reference checks. Requiring human review can be correct; it does not mean the service recovered.</p>
<div class="table-wrap"><table><thead><tr><th>Case evidence</th><th>Scenario family</th><th>Split</th><th>Reference checks</th><th>Verified scenario outcome</th></tr></thead><tbody>''' + rows + '''</tbody></table></div>
<footer>Inputs and answer keys are saved separately in each split folder. Cases and traces include expected results and must not be used as model prompts.
All splits share scenario templates: holdout values are reserved for later model evaluation, not evidence of generalization to new scenario families.
Changing the seed creates a new dataset; keep the same dataset when comparing models. Inspect failed cases before using their labels.</footer></main></html>'''


def run_factory(output, seed=42, variants=5):
    cases = generate_cases(seed, variants)
    directory = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "cases").mkdir()
    (directory / "databases").mkdir()
    reports = []
    for case in cases:
        report = evaluate_case(case, directory / "databases" / (case["case_id"] + ".sqlite3"))
        write_json(directory / "cases" / (case["case_id"] + ".json"), {"definition": case, "evaluation": report})
        reports.append(report)
    for split in ("development", "validation", "holdout"):
        folder = directory / split
        folder.mkdir()
        selected = [r for r in reports if r["split"] == split]
        write_jsonl(folder / "scenarios.jsonl", [c for c in cases if c["split"] == split])
        write_jsonl(folder / "inputs.jsonl", [row for report in selected for row in report["inputs"]])
        write_jsonl(folder / "answers.jsonl", [row for report in selected for row in report["answers"]])
    summary = {"version": VERSION, "mode": MODE, "seed": seed, "variants_per_family": variants,
               "dataset_digest": fingerprint(cases), "split_counts": dict(Counter(c["split"] for c in cases)),
               "family_counts": dict(Counter(c["family"] for c in cases)), "counts": aggregate(cases, reports),
               "source_hashes": {"simulator.py": hashlib.sha256(Path(sim.__file__).read_bytes()).hexdigest(),
                                 "data_factory.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
               "live_model_calls": 0, "model_accuracy": None,
               "limitations": ["Scripted bounded template cases, not AI-generated or measured infrastructure.",
                               "Default splits: 36 development, 12 validation, 12 holdout cases.",
                               "Numeric parameter groups are disjoint within this dataset; templates are shared.",
                               "Holdout is checked against the engine, not used to tune or evaluate model prompts.",
                               "Decision inputs include recorded prior receipts where present, never future outcomes.",
                               "No real concurrent-writer, cloud adapter or crash-restart benchmark in this factory."],
               "dataset_validated": all(r["passed"] for r in reports)}
    write_json(directory / "summary.json", summary)
    (directory / "report.html").write_text(render_report(summary, reports), encoding="utf-8")
    (directory / "README.txt").write_text(
        "ORKESTRYN - OFFLINE DATA FACTORY\n\n"
        "Open report.html in your browser. summary.json contains raw metric counts.\n"
        "cases/ contains definitions, reference checks and full evidence. databases/ holds SQLite journals.\n"
        "Split folders contain scenarios.jsonl (full reference cases), inputs.jsonl (decision-time inputs),\n"
        "and answers.jsonl (separate reference labels). Do not put answer keys or future events in model prompts.\n"
        "Do not use a failed dataset as validated evaluation data. Check dataset_validated in summary.json.\n"
        "The default 60 cases cover 12 template families with five numeric fixture groups.\n"
        "Holdout is reserved for later model evaluation; templates overlap, so this is not an unseen-family test.\n"
        "This milestone makes no model calls. Existing agent_runner.py is the separate live Nebius path.\n"
        "No foundation-model training, human certification, real GPUs or cloud operations are claimed.\n",
        encoding="utf-8")
    return directory, summary


class FactoryTests(unittest.TestCase):
    def test_reproducible_and_seed_changes_content(self):
        self.assertEqual(generate_cases(42), generate_cases(42))
        self.assertNotEqual(generate_cases(42), generate_cases(43))

    def test_split_groups_disjoint_and_all_families_present(self):
        cases = generate_cases()
        groups = {}
        for case in cases:
            groups.setdefault(case["parameter_group"], set()).add(case["split"])
        self.assertTrue(all(len(splits) == 1 for splits in groups.values()))
        self.assertEqual(Counter(c["split"] for c in cases), {"development": 36, "validation": 12, "holdout": 12})
        for split in ("development", "validation", "holdout"):
            self.assertEqual({c["family"] for c in cases if c["split"] == split}, set(FAMILIES))

    def test_all_reference_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            for case in generate_cases():
                with self.subTest(case=case["case_id"], family=case["family"]):
                    report = evaluate_case(case, Path(temp) / (case["case_id"] + ".sqlite3"))
                    self.assertTrue(report["passed"], [c for c in report["checks"] if not c["passed"]])

    def test_wrong_expected_label_fails_instead_of_learning_from_engine(self):
        case = next(c for c in generate_cases() if c["family"] == "approval_guard")
        case["steps"][0]["expected"] = {"status": "SUCCEEDED", "code": "OK"}
        with tempfile.TemporaryDirectory() as temp:
            report = evaluate_case(case, Path(temp) / "wrong.sqlite3")
        self.assertFalse(report["passed"])

    def test_inputs_do_not_contain_answers_or_future_faults(self):
        case = next(c for c in generate_cases() if c["family"] == "stale_after_budget")
        with tempfile.TemporaryDirectory() as temp:
            report = evaluate_case(case, Path(temp) / "inputs.sqlite3")
        encoded = encode(report["inputs"])
        for hidden in ("expected_task_outcome", "final_checks", "CANDIDATE_FAILURE", "FAILED_RECOVERED"):
            self.assertNotIn(hidden, encoded)
        self.assertEqual({r["row_id"] for r in report["inputs"]}, {r["row_id"] for r in report["answers"]})

    def test_invalid_initial_state_rejected(self):
        with self.assertRaises(ValueError):
            fixture_state({"stable": 100, "initial_limit": 90, "units": 20, "extra": 20, "new_limit": 110})

    def test_retry_input_has_prior_receipt_and_changed_request_has_new_fingerprint(self):
        case = next(c for c in generate_cases() if c["family"] == "idempotent_retry")
        with tempfile.TemporaryDirectory() as temp:
            report = evaluate_case(case, Path(temp) / "retry.sqlite3")
        retry, conflict = report["inputs"][-2:]
        self.assertEqual(retry["request_fingerprint"], retry["prior_receipt"]["request_fingerprint"])
        self.assertNotEqual(conflict["request_fingerprint"], conflict["prior_receipt"]["request_fingerprint"])
        self.assertEqual(retry["prior_receipt"]["result"]["status"], "SUCCEEDED")

    def test_report_bundle_and_nonoverwriting_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            one, summary = run_factory(Path(temp))
            two, second = run_factory(Path(temp))
            self.assertNotEqual(one, two)
            self.assertEqual(summary["dataset_digest"], second["dataset_digest"])
            self.assertTrue(summary["dataset_validated"])
            self.assertEqual(summary["counts"]["cases_passed"], 60)
            self.assertEqual(summary["counts"]["invalid_actions_admitted"], 0)
            self.assertEqual(summary["counts"]["false_blocks"], 0)
            self.assertEqual(summary["live_model_calls"], 0)
            self.assertTrue((one / "report.html").is_file())
            self.assertEqual(len(list((one / "cases").glob("*.json"))), 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="Run offline factory checks.")
    parser.add_argument("--seed", type=int, default=42, help="Reproducible dataset seed (default: 42).")
    parser.add_argument("--variants", type=int, default=5, choices=range(5, 51), metavar="5..50")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "factory_runs")
    parser.add_argument("--open-report", action="store_true", help="Open the local HTML report in your default browser.")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2, buffer=True).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(FactoryTests))
        return 0 if result.wasSuccessful() else 1
    print("ORKESTRYN - EVALUATION DATA FACTORY")
    print(MODE)
    print("No API key needed. No Nebius credits used.")
    directory, summary = run_factory(args.output, args.seed, args.variants)
    counts = summary["counts"]
    print("\nCases matched: %d/%d | Families: %d" % (counts["cases_passed"], counts["cases_evaluated"], len(FAMILIES)))
    print("Splits: " + ", ".join("%s %d" % pair for pair in summary["split_counts"].items()))
    print("False blocks: %d | Invalid actions admitted: %d" % (counts["false_blocks"], counts["invalid_actions_admitted"]))
    print("Preserved legitimate work: %d/%d" % (counts.get("preservation_passed", 0), counts.get("preservation_cases", 0)))
    print("Dataset validated: " + str(summary["dataset_validated"]))
    print("Evidence: " + str(directory))
    print("Open this report in your browser: " + str(directory / "report.html"))
    print("These are scripted coordinator results, not a live-model accuracy score.")
    if args.open_report:
        import webbrowser
        webbrowser.open((directory / "report.html").resolve().as_uri())
    return 0 if summary["dataset_validated"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print("DATA FACTORY FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
