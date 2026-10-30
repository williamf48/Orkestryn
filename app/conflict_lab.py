#!/usr/bin/env python3
"""Explore bounded operator interleavings and compare recovery choices offline.

The lab starts with a healthy 20-unit canary, explores every ordering of three
fixed actor programs, injects a chosen fault, and forks the same recovery state.
It never calls a model, changes a cloud resource, or edits a previous run.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import sys
import tempfile
import time
import unittest
import uuid
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import simulator as sim

VERSION = "agent-rehearsal-conflict-lab-v1"
MODE = "OFFLINE BOUNDED INTERLEAVING EXPLORATION; NO MODEL CALLS"
PROGRAMS = {
    "A": ("A_READ", "A_RESERVE", "A_ROUTE"),
    "B": ("B_READ", "B_LIMIT"),
    "C": ("C_READ", "C_WRITE"),
}
SCENARIOS = ("routing", "dependency")
FAULTS = ("workflow_failure", "candidate_failure", "stop_timeout", "fallback_unhealthy")
STRATEGIES = ("guarded", "pause_only", "snapshot_restore")
LABELS = {"guarded": "Guarded recovery", "pause_only": "Pause only",
          "snapshot_restore": "Restore old snapshot"}
RECOVERY_RULES = (
    "Pause deploy-A before compensation; current fixture approvals authorize bounded recovery.",
    "Restore A's last owned candidate route only while its owner/version still match and the stable fallback is healthy, allowed and sufficiently provisioned.",
    "Preserve B's current budget and C's current route/dependencies.",
    "Stop a candidate only after traffic is zero, dependencies and in-flight requests are absent, and the stop outcome can be confirmed.",
    "Release allocation only after confirmed stop; retain allocation for unresolved routes, consumers or unknown stop outcomes.",
    "Release A's unused pending reservations independently of candidate cleanup.",
    "Report RECOVERY_REVIEW_REQUIRED while candidate cleanup is blocked, otherwise VERIFIED_RECOVERED for this existing-candidate fixture.",
)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def schedules():
    """All 7!/(3!2!2!) = 210 serial interleavings, retaining each actor's order."""
    def visit(prefix, positions):
        if len(prefix) == 7:
            yield tuple(prefix)
            return
        for actor, program in PROGRAMS.items():
            index = positions.get(actor, 0)
            if index < len(program):
                yield from visit(prefix + [program[index]], {**positions, actor: index + 1})
    return list(visit([], {}))


def validate_schedule(order):
    if not isinstance(order, (tuple, list)) or len(order) != 7:
        raise ValueError("An ordering must contain all seven actor steps.")
    if any(not isinstance(step, str) for step in order):
        raise ValueError("Step identifiers must be strings.")
    if sorted(order) != sorted(step for program in PROGRAMS.values() for step in program):
        raise ValueError("An ordering must contain each registered step exactly once.")
    for actor, program in PROGRAMS.items():
        if tuple(step for step in order if step.startswith(actor + "_")) != program:
            raise ValueError("An ordering cannot reverse an actor's program.")


def commitments(state):
    # Independent arithmetic: do not call the coordinator's invariant checker.
    return state["stable_units"] + sum(d["allocated_units"] for d in state["deployments"].values()) + sum(
        r["units"] for r in state["reservations"].values() if r["status"] == "PENDING")


def invariant_checks(state):
    allocations = [state["stable_units"]] + [d["allocated_units"] for d in state["deployments"].values()]
    routing = state["routing"]
    return {
        "capacity_within_limit": commitments(state) <= state["capacity_limit"],
        "nonnegative_integer_allocations": all(type(n) is int and n >= 0 for n in allocations),
        "positive_pending_reservations": all(type(r["units"]) is int and r["units"] > 0
            for r in state["reservations"].values() if r["status"] == "PENDING"),
        "traffic_totals_100": sum(routing.values()) == 100,
        "valid_traffic_weights": all(type(n) is int and 0 <= n <= 100 for n in routing.values()),
        "routed_resources_running": all(weight == 0 or target == "stable-v1" or (
            target in state["deployments"] and state["deployments"][target]["status"] == "RUNNING"
            and state["deployments"][target]["allocated_units"] > 0) for target, weight in routing.items()),
    }


def summary_state(state):
    candidate = state["deployments"].get("candidate-v2")
    unhealthy_traffic = sum(weight for target, weight in state["routing"].items()
        if not (state["stable_healthy"] if target == "stable-v1"
                else state["deployments"].get(target, {}).get("healthy", False)))
    return {"committed": commitments(state), "limit": state["capacity_limit"],
            "budget_version": state["budget_version"], "route_owner": state["route_owner"],
            "route_version": state["route_version"], "routing": state["routing"],
            "candidate_status": candidate["status"] if candidate else "ABSENT",
            "candidate_units": candidate["allocated_units"] if candidate else 0,
            "dependencies": copy.deepcopy(candidate["dependencies"]) if candidate else [],
            "pending_units": sum(r["units"] for r in state["reservations"].values() if r["status"] == "PENDING"),
            "unhealthy_traffic_percent": unhealthy_traffic, "paused_runs": list(state["paused_runs"])}


def reference_recovery(before):
    """Predict this lab's recovery contract BEFORE executing Engine.recover.

    Domain: the lab's fixed healthy canary, current fixture approvals, A/B/C
    programs and four fault profiles. This is not a general recovery planner.
    """
    candidate = before["deployments"]["candidate-v2"]
    traffic = before["routing"]["candidate-v2"]
    fallback_safe = all(before[key] for key in (
        "stable_healthy", "stable_allowed", "stable_capacity_sufficient"))
    restore = traffic > 0 and before["route_owner"] == "deploy-A" and fallback_safe
    traffic_after = 0 if restore else traffic
    reasons = []
    if traffic_after:
        reasons.append("PROTECTED_OR_UNSAFE_ROUTE")
    if candidate["dependencies"]:
        reasons.append("ACTIVE_DEPENDENCY")
    if candidate["in_flight"]:
        reasons.append("IN_FLIGHT_REQUESTS")
    if candidate["stop_timeout"] or candidate["status"] == "STOP_OUTCOME_UNKNOWN":
        reasons.append("STOP_UNCONFIRMED")
    can_release = not reasons
    pending = sum(r["units"] for r in before["reservations"].values()
                  if r["status"] == "PENDING" and r["owner_run"] == "deploy-A")
    return {"recovery_status": "VERIFIED_RECOVERED" if can_release else "RECOVERY_REVIEW_REQUIRED",
            "candidate_releasable": can_release, "route_restorable": restore,
            "eligible_cleanup_units": pending + (candidate["allocated_units"] if can_release else 0),
            "expected_committed": commitments(before) - pending - (candidate["allocated_units"] if can_release else 0),
            "expected_candidate_units": 0 if can_release else candidate["allocated_units"],
            "expected_candidate_status": "STOPPED" if can_release else candidate["status"],
            "expected_candidate_traffic": traffic_after,
            "expected_route_owner": "deploy-A" if restore else before["route_owner"],
            "expected_route_version": before["route_version"] + int(restore),
            "review_reasons": reasons}


def assess(before, after, reference):
    """Check preservation and cleanup independently for every strategy."""
    candidate_before = before["deployments"]["candidate-v2"]
    candidate_after = after["deployments"].get("candidate-v2")
    checks = invariant_checks(after)
    checks["b_budget_preserved"] = all(after[key] == before[key]
        for key in ("capacity_limit", "budget_version"))
    checks["c_route_preserved"] = before["route_owner"] != "route-C" or all(
        after[key] == before[key] for key in ("routing", "route_owner", "route_version"))
    checks["c_dependencies_preserved"] = all(candidate_after is not None
        and dependency in candidate_after["dependencies"]
        and candidate_after["allocated_units"] >= candidate_before["allocated_units"]
        and candidate_after["status"] == "RUNNING"
        for dependency in candidate_before["dependencies"] if dependency["owner_run"] == "route-C")
    checks["required_allocation_preserved"] = reference["candidate_releasable"] or (
        candidate_after is not None and candidate_after["allocated_units"] >= candidate_before["allocated_units"])
    checks["external_fallback_facts_preserved"] = all(after[key] == before[key] for key in (
        "stable_healthy", "stable_allowed", "stable_capacity_sufficient"))
    checks["failed_workflow_paused"] = "deploy-A" in after["paused_runs"]
    eligible_left = sum(r["units"] for r in after["reservations"].values()
        if r["owner_run"] == "deploy-A" and r["status"] == "PENDING")
    if reference["candidate_releasable"] and candidate_after:
        eligible_left += candidate_after["allocated_units"]
    return {"checks": checks, "violations": [key for key, passed in checks.items() if not passed],
            "eligible_units_retained": eligible_left, "state": summary_state(after)}


def execute_order(engine, order, scenario):
    reads, trace = {}, []
    for step in order:
        actor = step[0]
        before = engine.state()
        if step.endswith("READ"):
            reads[actor] = {"budget_version": before["budget_version"], "route_version": before["route_version"]}
            trace.append({"step": step, "observed": reads[actor], "kind": "READ"})
            continue
        if step == "A_RESERVE":
            result = sim.action(engine, "reserve_capacity", {"units": 20, "reservation_id": "r-A-extra",
                "budget_version": reads[actor]["budget_version"]}, "lab-A-reserve")
        elif step == "A_ROUTE":
            result = sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 50,
                "route_version": reads[actor]["route_version"]}, "lab-A-route")
        elif step == "B_LIMIT":
            result = sim.action(engine, "set_capacity_limit", {"limit": 70,
                "budget_version": reads[actor]["budget_version"]}, "lab-B-limit", actor="B", run="cost-B")
        elif scenario == "routing":
            result = sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 20,
                "route_version": reads[actor]["route_version"]}, "lab-C-route", actor="C", run="route-C")
        else:
            result = sim.action(engine, "add_dependency", {"candidate_id": "candidate-v2",
                "dependency_id": "C-legitimate-job"}, "lab-C-dependency", actor="C", run="route-C")
        after = engine.state()
        checks = invariant_checks(after)
        if result["status"] == "BLOCKED":
            checks["blocked_action_has_no_state_effect"] = before == after
        trace.append({"step": step, "kind": "ACTION", "result": result, "checks": checks})
    return trace


def run_case(order, scenario="routing", fault="workflow_failure"):
    validate_schedule(order)
    if scenario not in SCENARIOS or fault not in FAULTS:
        raise ValueError("Unknown conflict scenario or fault profile.")
    engine, branch = sim.Engine(":memory:"), None
    try:
        snapshot = engine.state()
        sim.start_canary(engine)
        trace = execute_order(engine, order, scenario)
        if fault != "workflow_failure":
            engine.fixture_event({"candidate_failure": "CANDIDATE_FAILURE", "stop_timeout": "STOP_TIMEOUT",
                                  "fallback_unhealthy": "FALLBACK_UNHEALTHY"}[fault])
        before, events = engine.state(), engine.events()
        reference = reference_recovery(before)
        parent_digest = fingerprint({"state": before, "events": events})
        branch = sim.Engine(":memory:")
        engine.db.backup(branch.db)
        recovery = branch.recover()
        guarded = branch.state()
        paused = copy.deepcopy(before)
        if "deploy-A" not in paused["paused_runs"]:
            paused["paused_runs"].append("deploy-A")
        restored = copy.deepcopy(paused)
        for key in ("capacity_limit", "budget_version", "routing", "route_version", "route_owner", "deployments", "reservations"):
            restored[key] = copy.deepcopy(snapshot[key])
        # Give the naive baseline the same durable pause and current external
        # observations/approvals. Its failure must come from restored resources.
        choices = {"guarded": guarded, "pause_only": paused, "snapshot_restore": restored}
        comparison = {name: assess(before, after, reference) for name, after in choices.items()}
        actual = comparison["guarded"]["state"]
        contract_checks = {
            "recovery_status": recovery["status"] == reference["recovery_status"],
            "committed_units": actual["committed"] == reference["expected_committed"],
            "candidate_units": actual["candidate_units"] == reference["expected_candidate_units"],
            "candidate_status": actual["candidate_status"] == reference["expected_candidate_status"],
            "candidate_traffic": actual["routing"]["candidate-v2"] == reference["expected_candidate_traffic"],
            "route_owner": actual["route_owner"] == reference["expected_route_owner"],
            "route_version": actual["route_version"] == reference["expected_route_version"],
            "eligible_cleanup_complete": comparison["guarded"]["eligible_units_retained"] == 0,
            "parent_state_and_journal_unchanged": parent_digest == fingerprint({"state": engine.state(), "events": engine.events()}),
        }
        admission_ok = all(all(row.get("checks", {}).values()) for row in trace)
        passed = admission_ok and not comparison["guarded"]["violations"] and all(contract_checks.values())
        return {"case_id": "conflict-" + fingerprint([scenario, fault, order])[:16],
                "scenario": scenario, "fault": fault, "order": list(order), "trace": trace,
                "initial_state": snapshot, "decision_state": before, "reference": reference,
                "guarded_recovery": recovery, "comparison": comparison, "contract_checks": contract_checks,
                "admission_checks_passed": admission_ok, "passed": passed,
                "parent_digest": parent_digest, "parent_events": events,
                "guarded_events": branch.events()}
    finally:
        if branch is not None:
            branch.close()
        engine.close()


def challenge_pair(record):
    """Deduplicate by decision state. Answers stay outside model-facing inputs."""
    state = record["decision_state"]
    case_id = "recovery-" + fingerprint(state)[:16]
    inputs = {"schema_version": VERSION + "-recovery-input", "case_id": case_id,
              "task": "Predict the bounded recovery result for deploy-A, eligible cleanup units and protected effects.",
              "state": state, "owned_run": "deploy-A",
              "policy_contract": list(RECOVERY_RULES),
              "scope": "Development recovery challenges; synthetic single-service fixtures."}
    answer = {"case_id": case_id, "reference": record["reference"],
              "source_case_id": record["case_id"], "reference_checked_against_coordinator": record["passed"]}
    return inputs, answer


def render_report(summary, rows):
    # Text goes through HTML escaping or textContent; JSON cannot close its script.
    payload = json.dumps(rows, ensure_ascii=True).replace("<", "\\u003c")
    table = "".join("<tr><th>%s</th><td>%d / %d</td><td>%d</td><td>%d</td></tr>" % (
        html.escape(LABELS[name]), stats["constraint_passes"], summary["cases_evaluated"],
        stats["preservation_loss_cases"], stats["eligible_units_retained_total"])
        for name, stats in summary["strategies"].items())
    return r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orkestryn — Conflict Lab</title><style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0a1119;color:#e3edf5}*{box-sizing:border-box}body{max-width:1220px;margin:auto;padding:32px 24px 70px}h1{font-size:38px;margin:12px 0}h2{font-size:21px}p{line-height:1.6;color:#aebed0}.eyebrow{color:#52d6db;letter-spacing:.14em;font-size:12px;font-weight:700}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card,section{border:1px solid #283a4d;border-radius:14px;background:#101c28;padding:20px;margin:18px 0}.card b{display:block;font-size:29px;color:#f4f8fb}.card span{color:#adbed0;font-size:13px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:12px;border-bottom:1px solid #2a3a4c;vertical-align:top}th{color:#bce3e7}button,select{padding:10px 12px;background:#152a3a;color:#e7f6fb;border:1px solid #486477;border-radius:7px;font:inherit}button{cursor:pointer}button:hover{border-color:#54d4d9}a{color:#6ce2e5}.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.scroll{overflow:auto;max-height:430px}#detail{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}.note{border-left:3px solid #e3b267;padding-left:15px}.ok{color:#8de8b9}.fail{color:#ffad9b}.schedule{font-family:ui-monospace,monospace;font-size:12px}@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}body{padding:20px 12px}h1{font-size:30px}}
</style><div class="eyebrow">ORKESTRYN · COORDINATION ANALYSIS</div><h1>Conflict Lab</h1>
<p>Explore how action order changes admission and recovery. Every strategy starts from the same saved decision state. A expands a canary, B lowers its capacity limit, and C routes traffic or registers a dependency.</p>
<div class="grid"><div class="card"><b>__CASES__</b><span>orderings × conflicts × faults</span></div><div class="card"><b>__PASSED__</b><span>guarded reference checks passed</span></div><div class="card"><b>__UNIQUE__</b><span>distinct development challenges</span></div><div class="card"><b>0</b><span>model calls / inference credits</span></div></div>
<section><h2>Compare recovery choices</h2><div class="scroll"><table><thead><tr><th>Strategy</th><th>Constraints satisfied</th><th>Cases losing protected effects</th><th>Eligible units left allocated</th></tr></thead><tbody>__STRATEGIES__</tbody></table></div>
<p>Units are synthetic and summed across independent cases. Pause only preserves current work but may retain resources eligible for cleanup. The illustrative snapshot baseline restores old resource/configuration fields while keeping the current approvals, external observations and workflow pause. It may erase later resource changes. These baselines do not represent another product.</p>
<p class="note">A review-required result can be correct. It can also leave unhealthy traffic while a conflict is unresolved; the case details report availability separately. The lab explores serial orderings of seven fixed steps. The summary records whether every ordering was explored; this is not a distributed-load benchmark.</p></section>
<section><h2>Inspect a case</h2><div class="controls"><label>Conflict <select id="scenario"><option value="">All</option><option>routing</option><option>dependency</option></select></label><label>Fault <select id="fault"><option value="">All</option><option>workflow_failure</option><option>candidate_failure</option><option>stop_timeout</option><option>fallback_unhealthy</option></select></label><label>Recovery <select id="status"><option value="">All</option><option>VERIFIED_RECOVERED</option><option>RECOVERY_REVIEW_REQUIRED</option></select></label><span id="count"></span></div>
<div class="scroll"><table><thead><tr><th>Ordering</th><th>Conflict / fault</th><th>Guarded result</th><th>Evidence</th></tr></thead><tbody id="cases"></tbody></table></div><p>Showing up to 100 matching rows. Full traces and all cases are saved in campaign.jsonl.</p>
<h2>Selected case</h2><div id="detail" aria-live="polite">Select a case to inspect its reads, decisions and recovery comparison.</div></section>
<section><h2>Reusable evidence</h2><p><a href="challenge_inputs.jsonl" download>Development challenge inputs</a> · <a href="challenge_answers.jsonl" download>Separate reference answers</a> · <a href="counterexamples.jsonl" download>Baseline counterexamples</a> · <a href="summary.json" download>Campaign summary</a></p><p>Reference expectations are computed before guarded recovery. Export is withheld if any guarded reference check fails. These challenges have been inspected during development and are not an untouched test set. The admission evaluator uses its existing dataset format.</p></section>
<script id="data" type="application/json">__DATA__</script><script>
'use strict';const rows=JSON.parse(document.getElementById('data').textContent);const byId=new Map(rows.map(r=>[r.case_id,r]));
function cell(tr,value,cls){const td=document.createElement('td');td.textContent=value;if(cls)td.className=cls;tr.appendChild(td);return td;}
function show(id){document.getElementById('detail').textContent=JSON.stringify(byId.get(id),null,2);}
function draw(){const s=document.getElementById('scenario').value,f=document.getElementById('fault').value,t=document.getElementById('status').value;const found=rows.filter(r=>(!s||r.scenario===s)&&(!f||r.fault===f)&&(!t||r.outcome===t));document.getElementById('count').textContent=found.length+' matching cases';const body=document.getElementById('cases');body.replaceChildren();for(const r of found.slice(0,100)){const tr=document.createElement('tr');cell(tr,r.order.join(' → '),'schedule');cell(tr,r.scenario+' / '+r.fault);cell(tr,r.outcome+' · '+r.comparison.guarded.state.committed+'/'+r.comparison.guarded.state.limit,r.passed?'ok':'fail');const button=document.createElement('button');button.textContent='Inspect';button.addEventListener('click',()=>show(r.case_id));cell(tr,'').appendChild(button);body.appendChild(tr);}}
for(const id of ['scenario','fault','status'])document.getElementById(id).addEventListener('change',draw);draw();if(rows.length)show(rows[0].case_id);
</script></html>'''.replace("__CASES__", str(summary["cases_evaluated"])).replace("__PASSED__", str(summary["guarded_cases_passed"])).replace(
        "__UNIQUE__", str(summary["unique_development_challenges"])).replace("__STRATEGIES__", table).replace("__DATA__", payload)


def run_campaign(output, scenario="all", fault="all", max_schedules=None):
    if scenario not in (*SCENARIOS, "all") or fault not in (*FAULTS, "all"):
        raise ValueError("Unknown conflict scenario or fault profile.")
    if max_schedules is not None and (type(max_schedules) is not int or not 1 <= max_schedules <= 210):
        raise ValueError("max_schedules must be between 1 and 210.")
    orders = schedules()[:max_schedules]
    families = SCENARIOS if scenario == "all" else (scenario,)
    faults = FAULTS if fault == "all" else (fault,)
    now = datetime.now(timezone.utc)
    directory = Path(output).resolve() / (now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    rows, unique, counterexamples = [], {}, {}
    totals = {key: Counter() for key in STRATEGIES}
    outcomes, blocked = Counter(), Counter()
    passed, evaluated = 0, 0
    with (directory / "campaign.jsonl").open("w", encoding="utf-8") as stream:
        for family in families:
            for profile in faults:
                for order in orders:
                    record = run_case(order, family, profile)
                    stream.write(encode(record) + "\n")
                    evaluated += 1
                    passed += int(record["passed"])
                    outcomes[record["guarded_recovery"]["status"]] += 1
                    for entry in record["trace"]:
                        result = entry.get("result", {})
                        if result.get("status") == "BLOCKED":
                            blocked[result["code"]] += 1
                    for name, comparison in record["comparison"].items():
                        totals[name]["constraint_passes"] += int(not comparison["violations"])
                        totals[name]["preservation_loss_cases"] += int(any(key in comparison["violations"] for key in (
                            "b_budget_preserved", "c_route_preserved", "c_dependencies_preserved", "required_allocation_preserved")))
                        totals[name]["eligible_units_retained_total"] += comparison["eligible_units_retained"]
                    inputs, answer = challenge_pair(record)
                    unique.setdefault(inputs["case_id"], (inputs, answer))
                    if unique[inputs["case_id"]][1]["reference"] != answer["reference"]:
                        raise RuntimeError("Identical decision states produced inconsistent reference labels.")
                    # One witness per baseline violation signature, never a claim of minimality.
                    for strategy in ("snapshot_restore", "pause_only"):
                        comparison = record["comparison"][strategy]
                        if comparison["violations"] or comparison["eligible_units_retained"]:
                            signature = encode([family, profile, strategy, comparison["violations"], bool(comparison["eligible_units_retained"])])
                            counterexamples.setdefault(signature, {"case_id": record["case_id"], "scenario": family,
                                "fault": profile, "order": list(order), "strategy": strategy,
                                "comparison": comparison, "guarded": record["comparison"]["guarded"],
                                "note": "First observed witness for this signature; not a minimized trace."})
                    rows.append({key: record[key] for key in ("case_id", "scenario", "fault", "order", "trace", "comparison", "passed")} | {
                        "outcome": record["guarded_recovery"]["status"], "reference": record["reference"],
                        "contract_checks": record["contract_checks"]})
    export_ok = evaluated > 0 and passed == evaluated
    summary = {"version": VERSION, "mode": MODE, "created_at": now.isoformat(), "model_calls": 0,
        "source_sha256": {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                          for name in ("simulator.py", "conflict_lab.py")},
        "schedules_per_scenario_fault": len(orders), "all_orderings_explored": len(orders) == 210,
        "scenarios": list(families), "faults": list(faults), "cases_evaluated": evaluated,
        "guarded_cases_passed": passed, "guarded_cases_failed": evaluated - passed,
        "guarded_outcomes": dict(outcomes), "blocked_actions_by_code": dict(blocked),
        "strategies": {key: dict(value) for key, value in totals.items()},
        "challenge_export_status": "VALIDATED_DEVELOPMENT_ONLY" if export_ok else "WITHHELD_REFERENCE_MISMATCH",
        "unique_development_challenges": len(unique) if export_ok else 0,
        "distinct_decision_states": len(unique), "counterexample_signatures": len(counterexamples),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": "Fixed single-service fixtures; exhaustive serial interleavings of seven steps when unlimited. No real distributed races or measured GPU performance.",
        "baseline_scope": "Illustrative pause-only and old resource/configuration snapshot policies; all retain the workflow pause and current external observations/approvals. No external-product comparison.",
        "reference_scope": "Independent bounded recovery contract computed before coordinator recovery; all variants are inspected development data."}
    for filename, values in (("challenge_inputs.jsonl", [pair[0] for pair in unique.values()] if export_ok else []),
                             ("challenge_answers.jsonl", [pair[1] for pair in unique.values()] if export_ok else []),
                             ("counterexamples.jsonl", list(counterexamples.values()))):
        with (directory / filename).open("w", encoding="utf-8") as stream:
            for value in values:
                stream.write(encode(value) + "\n")
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (directory / "report.html").write_text(render_report(summary, rows), encoding="utf-8")
    return directory, summary


class ConflictTests(unittest.TestCase):
    ORDER = ("A_READ", "A_RESERVE", "A_ROUTE", "B_READ", "B_LIMIT", "C_READ", "C_WRITE")

    def test_every_order_preserves_actor_programs_and_is_unique(self):
        values = schedules()
        self.assertEqual(len(values), 210)
        self.assertEqual(len(set(values)), 210)
        for order in values:
            validate_schedule(order)

    def test_invalid_duplicate_reversed_or_unbounded_requests_rejected(self):
        for order in (self.ORDER[:-1], self.ORDER[:-1] + ("B_LIMIT",), tuple(reversed(self.ORDER)), [None] * 7):
            with self.assertRaises(ValueError):
                run_case(order)
        with tempfile.TemporaryDirectory() as folder:
            for count in (0, -1, 211, True):
                with self.assertRaises(ValueError):
                    run_campaign(folder, max_schedules=count)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_all_orderings_satisfy_independent_contract(self):
        for family in SCENARIOS:
            for order in schedules():
                record = run_case(order, family)
                self.assertTrue(record["passed"], [family, order, record["contract_checks"]])

    def test_protected_c_route_and_extra_pending_cleanup(self):
        record = run_case(self.ORDER)
        choice = record["comparison"]["guarded"]
        self.assertEqual(record["guarded_recovery"]["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(choice["state"]["route_owner"], "route-C")
        self.assertEqual(choice["state"]["candidate_units"], 20)
        self.assertEqual(choice["state"]["pending_units"], 0)
        self.assertIn("c_route_preserved", record["comparison"]["snapshot_restore"]["violations"])
        self.assertEqual(record["comparison"]["pause_only"]["eligible_units_retained"], 20)

    def test_b_limit_survives_and_stale_a_work_is_blocked(self):
        record = run_case(("A_READ", "B_READ", "B_LIMIT", "A_RESERVE", "A_ROUTE", "C_READ", "C_WRITE"))
        self.assertTrue(record["passed"])
        self.assertEqual(record["comparison"]["guarded"]["state"]["limit"], 70)
        self.assertEqual([r["result"]["code"] for r in record["trace"] if r["step"] in ("A_RESERVE", "A_ROUTE")],
                         ["STALE_BUDGET_VERSION", "STALE_BUDGET_VERSION"])
        self.assertIn("b_budget_preserved", record["comparison"]["snapshot_restore"]["violations"])

    def test_stale_c_route_blocks_and_owned_route_can_recover(self):
        record = run_case(("C_READ", "A_READ", "A_RESERVE", "A_ROUTE", "C_WRITE", "B_READ", "B_LIMIT"))
        self.assertTrue(record["passed"])
        self.assertEqual(next(r for r in record["trace"] if r["step"] == "C_WRITE")["result"]["code"], "ROUTE_VERSION_CONFLICT")
        self.assertEqual(record["guarded_recovery"]["status"], "VERIFIED_RECOVERED")
        self.assertEqual(record["comparison"]["guarded"]["state"]["committed"], 40)

    def test_dependency_survives_route_restoration(self):
        record = run_case(self.ORDER, "dependency")
        state = record["comparison"]["guarded"]["state"]
        self.assertTrue(record["passed"])
        self.assertEqual(state["routing"]["candidate-v2"], 0)
        self.assertEqual(state["candidate_units"], 20)
        self.assertEqual(state["dependencies"], [{"id": "C-legitimate-job", "owner_run": "route-C"}])

    def test_fault_profiles_preserve_uncertainty_and_report_availability(self):
        order = ("C_READ", "A_READ", "A_RESERVE", "A_ROUTE", "C_WRITE", "B_READ", "B_LIMIT")
        for fault in FAULTS:
            record = run_case(order, fault=fault)
            self.assertTrue(record["passed"], fault)
            state = record["comparison"]["guarded"]["state"]
            if fault in ("stop_timeout", "fallback_unhealthy"):
                self.assertEqual(state["candidate_units"], 20)
                self.assertEqual(record["guarded_recovery"]["status"], "RECOVERY_REVIEW_REQUIRED")
        record = run_case(self.ORDER, fault="candidate_failure")
        self.assertEqual(record["comparison"]["guarded"]["state"]["unhealthy_traffic_percent"], 20)

    def test_counterfactuals_and_repeated_runs_do_not_change_parent(self):
        one, two = run_case(self.ORDER), run_case(self.ORDER)
        self.assertEqual(one, two)
        self.assertTrue(one["contract_checks"]["parent_state_and_journal_unchanged"])
        self.assertEqual(one["guarded_events"][:len(one["parent_events"])], one["parent_events"])

    def test_reference_detects_forged_success_and_export_is_withheld(self):
        recover = sim.Engine.recover
        def wrong_status(engine, run="deploy-A"):
            return {**recover(engine, run), "status": "VERIFIED_RECOVERED"}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(sim.Engine, "recover", wrong_status):
            directory, summary = run_campaign(folder, "dependency", "workflow_failure", 1)
            self.assertEqual(summary["guarded_cases_failed"], 1)
            self.assertEqual(summary["challenge_export_status"], "WITHHELD_REFERENCE_MISMATCH")
            self.assertEqual((directory / "challenge_inputs.jsonl").read_text(), "")

    def test_challenges_deduplicate_and_never_include_reference_answers(self):
        with tempfile.TemporaryDirectory(prefix="Conflict Lab ") as folder:
            directory, summary = run_campaign(folder, max_schedules=3)
            self.assertEqual(summary["cases_evaluated"], 24)
            self.assertEqual(summary["guarded_cases_passed"], 24)
            self.assertFalse(summary["all_orderings_explored"])
            inputs = [json.loads(row) for row in (directory / "challenge_inputs.jsonl").read_text().splitlines()]
            answers = [json.loads(row) for row in (directory / "challenge_answers.jsonl").read_text().splitlines()]
            self.assertEqual(len(inputs), len({row["case_id"] for row in inputs}))
            self.assertEqual([r["case_id"] for r in inputs], [r["case_id"] for r in answers])
            self.assertTrue(all("reference" not in row and "comparison" not in row for row in inputs))
            original = (directory / "summary.json").read_bytes()
            new, _ = run_campaign(folder, "routing", "workflow_failure", 1)
            self.assertNotEqual(directory, new)
            self.assertEqual((directory / "summary.json").read_bytes(), original)

    def test_report_treats_record_values_as_text(self):
        summary = {"cases_evaluated": 1, "guarded_cases_passed": 1, "unique_development_challenges": 1,
                   "strategies": {name: {"constraint_passes": 1, "preservation_loss_cases": 0,
                   "eligible_units_retained_total": 0} for name in STRATEGIES}}
        report = render_report(summary, [{"case_id": "</script><img src=x onerror=alert(1)>"}])
        self.assertNotIn("</script><img", report)
        self.assertIn("\\u003c/script>", report)
        self.assertNotIn("innerHTML", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "conflict_runs")
    parser.add_argument("--scenario", choices=("all",) + SCENARIOS, default="all")
    parser.add_argument("--fault", choices=("all",) + FAULTS, default="all")
    parser.add_argument("--max-schedules", type=int, default=None, help="Bound the explored prefix to 1..210 orderings.")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ConflictTests))
        return 0 if result.wasSuccessful() else 1
    print(MODE, flush=True)
    directory, summary = run_campaign(args.output, args.scenario, args.fault, args.max_schedules)
    print("Guarded reference checks: %d/%d cases" % (summary["guarded_cases_passed"], summary["cases_evaluated"]))
    print("Distinct development challenges: %d" % summary["unique_development_challenges"])
    print("Evidence: " + str(directory))
    print("Report: " + str(directory / "report.html"))
    if args.open:
        webbrowser.open((directory / "report.html").as_uri())
    return 0 if summary["guarded_cases_failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print("CONFLICT LAB FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
