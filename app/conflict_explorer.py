#!/usr/bin/env python3
"""Bounded, offline parameter/fault exploration with replay and witness reduction.

This extends Conflict Lab v1 without changing its fixed-fixture campaign. Actors,
capacity and time are fixtures. No model, cloud service or real worker is called.
"""
from __future__ import annotations

import argparse
import copy
import json
import platform
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

import conflict_lab as base
import restart_checker
import simulator as sim

VERSION = "agent-rehearsal-conflict-explorer-v1"
MODE = "OFFLINE DEVELOPMENT EXPLORATION; SYNTHETIC FIXTURES; NO MODEL CALLS"
PROFILE_VALUES = {
    "baseline": (40, 20, 20, 100, 70),
    "exact_fit": (40, 20, 10, 70, 70),
    "over_limit_by_one": (40, 20, 11, 70, 70),
    "cost_at_commitments": (40, 20, 20, 100, 60),
    "cost_below_commitments": (40, 20, 20, 100, 59),
    "small_canary": (20, 10, 1, 31, 30),
    "large_canary": (50, 30, 20, 100, 80),
}
PROFILES = {name: dict(zip(("stable_units", "candidate_units", "extra_units", "initial_limit", "new_limit"), values))
            for name, values in PROFILE_VALUES.items()}
FAULTS = (*base.FAULTS, "metrics_at_limit", "metrics_expired", "requests_in_flight")
POSITIONS = (0, 3, 7)
SPEC_KEYS = {"schema_version", "profile", "parameters", "scenario", "fault", "steps"}
REDUCTION_SCOPE = ("1-minimal under deleting one operator step (and its writes when deleting its read), "
                   "with the seeded canary, parameter profile and fault held fixed. Not a globally "
                   "shortest example or proof of general causation.")


def selected_schedules(count=8):
    if type(count) is not int or not 1 <= count <= 210:
        raise ValueError("max-schedules must be an integer between 1 and 210.")
    orders = base.schedules()
    indices = [round(i * 209 / (count - 1)) for i in range(count)] if count > 1 else [0]
    return [orders[i] for i in indices]


def make_spec(order, profile="baseline", scenario="routing", fault="workflow_failure", position=7):
    base.validate_schedule(order)
    if type(position) is not int or not 0 <= position <= 7:
        raise ValueError("Fault position must be between 0 and 7 operator steps.")
    if profile not in PROFILES:
        raise ValueError("Unknown parameter profile.")
    spec = {"schema_version": VERSION, "profile": profile, "parameters": copy.deepcopy(PROFILES[profile]),
            "scenario": scenario, "fault": fault,
            "steps": [*order[:position], "FAULT", *order[position:]]}
    validate_spec(spec)
    return spec


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS or spec.get("schema_version") != VERSION:
        raise ValueError("Use a complete Explorer v1 replay specification with no extra fields.")
    if not isinstance(spec["profile"], str) or spec["profile"] not in PROFILES:
        raise ValueError("Unknown parameter profile.")
    params = spec["parameters"]
    if not isinstance(params, dict) or params != PROFILES[spec["profile"]] or any(type(v) is not int for v in params.values()):
        raise ValueError("Replay parameters must exactly match the registered profile.")
    if spec["scenario"] not in base.SCENARIOS or spec["fault"] not in FAULTS:
        raise ValueError("Unknown scenario or fault.")
    steps = spec["steps"]
    allowed = {"FAULT"} | {s for p in base.PROGRAMS.values() for s in p}
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8 or any(not isinstance(s, str) for s in steps):
        raise ValueError("Replay steps must be a bounded list of registered step identifiers.")
    if len(set(steps)) != len(steps) or not set(steps) <= allowed or steps.count("FAULT") != 1:
        raise ValueError("Replay needs exactly one fault and no duplicate or unknown steps.")
    for actor, program in base.PROGRAMS.items():
        actual = [s for s in steps if s.startswith(actor + "_")]
        if actual != [s for s in program if s in actual]:
            raise ValueError("Replay cannot reverse an actor's program.")
        if actual and actual[0] != actor + "_READ":
            raise ValueError("Every actor write requires its preceding read.")


def load_spec(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("Replay specification exceeds 64 KiB.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON fields are not allowed in replay specifications.")
            result[key] = value
        return result
    spec = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    validate_spec(spec)
    return spec


def harness_change(engine, kind, mutate, **extra):
    """Trusted, labelled fixture updates; never an agent-facing tool."""
    engine.db.execute("BEGIN IMMEDIATE")
    try:
        before = engine.state()
        after = copy.deepcopy(before)
        mutate(after)
        engine.check_invariants(after)
        engine.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(after),))
        engine._event({"kind": kind, "label": "SIMULATED/INJECTED EXPLORER FIXTURE", **extra,
                       "changes": {k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]}})
        engine.db.execute("COMMIT")
    except BaseException:
        engine.db.execute("ROLLBACK")
        raise


def seed(engine, params):
    def configure(state):
        state["stable_units"] = params["stable_units"]
        state["capacity_limit"] = params["initial_limit"]
        state["plans"]["deploy-A"]["max_units"] = params["candidate_units"] + params["extra_units"]
        state["plans"]["cost-B"]["approved_limit"] = params["new_limit"]
        for approval in state["approvals"].values():
            approval["plan_digest"] = sim.digest(state["plans"][approval["run_id"]])
    harness_change(engine, "EXPLORER_SETUP", configure, parameters=params, initial_fixture_state=sim.initial_state())
    snapshot = engine.state()
    results = [sim.action(engine, "reserve_capacity", {"units": params["candidate_units"],
        "reservation_id": "r-A-1", "budget_version": 1}, "explorer-seed-reserve"),
        sim.action(engine, "start_candidate", {"candidate_id": "candidate-v2", "reservation_id": "r-A-1",
        "config": "model-config-v2"}, "explorer-seed-start")]
    engine.fixture_event("OBSERVATIONS")
    results.append(sim.action(engine, "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 10,
                        "route_version": 1}, "explorer-seed-route"))
    if any(result["status"] != "SUCCEEDED" for result in results):
        raise RuntimeError("The registered canary fixture failed to initialize.")
    return snapshot


def inject_fault(engine, fault):
    if fault == "workflow_failure":
        harness_change(engine, "RECOVERY_STARTED", lambda state: state["paused_runs"].append("deploy-A"),
                       run_id="deploy-A", reason="Injected workflow failure: pause A at this schedule position.")
    elif fault == "requests_in_flight":
        harness_change(engine, "EXPLORER_FAULT", lambda state: state["deployments"]["candidate-v2"].update(in_flight=1),
                       fixture_type="REQUESTS_IN_FLIGHT")
    elif fault in ("metrics_at_limit", "metrics_expired"):
        engine.fixture_event("ADVANCE_CLOCK", ticks=20 if fault == "metrics_at_limit" else 21)
    else:
        engine.fixture_event({"candidate_failure": "CANDIDATE_FAILURE", "stop_timeout": "STOP_TIMEOUT",
                              "fallback_unhealthy": "FALLBACK_UNHEALTHY"}[fault])


def execute(engine, spec):
    reads, trace = {}, []
    for step in spec["steps"]:
        before = engine.state()
        seq = len(engine.events())
        if step == "FAULT":
            inject_fault(engine, spec["fault"])
            trace.append({"step": step, "kind": "FAULT", "fault": spec["fault"],
                          "event_sequences": [e["sequence"] for e in engine.events()[seq:]],
                          "checks": base.invariant_checks(engine.state())})
            continue
        actor = step[0]
        if step.endswith("READ"):
            reads[actor] = {"budget_version": before["budget_version"], "route_version": before["route_version"]}
            trace.append({"step": step, "kind": "READ", "observed": reads[actor], "after_event_sequence": seq})
            continue
        if step == "A_RESERVE":
            tool, args, run = "reserve_capacity", {"units": spec["parameters"]["extra_units"],
                "reservation_id": "r-A-extra", "budget_version": reads[actor]["budget_version"]}, "deploy-A"
        elif step == "B_LIMIT":
            tool, args, run = "set_capacity_limit", {"limit": spec["parameters"]["new_limit"],
                "budget_version": reads[actor]["budget_version"]}, "cost-B"
        elif step == "C_WRITE" and spec["scenario"] == "dependency":
            tool, args, run = "add_dependency", {"candidate_id": "candidate-v2", "dependency_id": "C-legitimate-job"}, "route-C"
        else:
            tool, args, run = "set_route", {"candidate_id": "candidate-v2", "candidate_percent": 50 if actor == "A" else 20,
                "route_version": reads[actor]["route_version"]}, "deploy-A" if actor == "A" else "route-C"
        result = sim.action(engine, tool, args, "explorer-" + step, actor=actor, run=run)
        after = engine.state()
        checks = base.invariant_checks(after)
        if result["status"] == "BLOCKED":
            checks["blocked_action_has_no_state_effect"] = before == after
        trace.append({"step": step, "kind": "ACTION", "result": result, "checks": checks,
                      "event_sequences": [e["sequence"] for e in engine.events()[seq:]]})
    return trace


def audit(engine):
    receipts = {key: {"fingerprint": digest, "result": json.loads(result)} for key, digest, result in
                engine.db.execute("SELECT action_key, fingerprint, result FROM action_receipts")}
    return restart_checker.audit_journal({"state": engine.state(), "events": engine.events(), "receipts": receipts})


def explain(record):
    """Evidence-linked predicates, not model-generated explanations or causal proof."""
    state, events = record["decision_state"], record["parent_events"]
    candidate = state["deployments"]["candidate-v2"]
    recovery_events = record["guarded_events"][len(events):]

    def refs(fields):
        # Match the exact nested value where possible, rather than attributing
        # an old ownership fact to a later health update on the same resource.
        def value_at(value, parts):
            for part in parts:
                value = value.get(part) if isinstance(value, dict) else None
            return value
        def changed(event, field):
            top, *parts = field.split(".")
            change = event.get("changes", {}).get(top)
            return change is not None and value_at(change["before"], parts) != value_at(change["after"], parts)
        # Event 1 explicitly records the initial fixture state for facts that
        # have never changed, including the original fixture approvals.
        return sorted({next((e["sequence"] for e in reversed(events) if changed(e, field)), events[0]["sequence"])
                       for field in fields})

    def evidence(tool):
        return [e["sequence"] for e in recovery_events if e.get("tool") == tool]

    route_reasons = []
    if state["route_owner"] != "deploy-A":
        route_reasons.append("The current route belongs to " + str(state["route_owner"]) + "; A cannot overwrite it during recovery.")
    for field in ("stable_healthy", "stable_allowed", "stable_capacity_sufficient"):
        if not state[field]:
            route_reasons.append(field + " is false, so moving traffic to the fallback is blocked.")
    if not route_reasons:
        route_reasons.append("A still owns the recorded route version, and the fallback checks permit restoration.")
    stop_reasons = []
    ref = record["reference"]
    if ref["expected_candidate_traffic"]:
        stop_reasons.append("Candidate traffic remains; stop is not attempted.")
    if candidate["dependencies"]:
        stop_reasons.append("An active dependency still needs the candidate: " + ", ".join(d["owner_run"] for d in candidate["dependencies"]) + ".")
    if candidate["in_flight"]:
        stop_reasons.append(str(candidate["in_flight"]) + " in-flight request remains; stopping is blocked.")
    if candidate["stop_timeout"] or candidate["status"] == "STOP_OUTCOME_UNKNOWN":
        stop_reasons.append("The stop outcome cannot be confirmed; allocation must remain committed.")
    if not stop_reasons:
        stop_reasons.append("After route restoration there is no traffic, dependency or in-flight request; stop can be confirmed.")
    actions = []
    reasons = {"restore_route": route_reasons, "stop_candidate": stop_reasons,
        "release_capacity": ["Allocation is released only after a successful confirmed stop." if ref["candidate_releasable"] else
                              "Allocation is retained because candidate cleanup is unresolved."],
        "release_reservation": ["Only unused pending reservations owned by deploy-A are eligible for independent release."
            if any(r["owner_run"] == "deploy-A" and r["status"] == "PENDING" for r in state["reservations"].values()) else
            "No unused pending reservation owned by deploy-A remains; this action is not needed."]}
    paths = {"restore_route": ["route_owner", "route_version", "routing", "stable_healthy", "stable_allowed", "stable_capacity_sufficient"],
             "stop_candidate": ["routing", "deployments.candidate-v2.dependencies", "deployments.candidate-v2.in_flight", "deployments.candidate-v2.stop_timeout"],
             "release_capacity": ["deployments.candidate-v2.status", "deployments.candidate-v2.allocated_units"],
             "release_reservation": ["reservations"]}
    for tool in reasons:
        performed = [s["result"] for s in record["guarded_recovery"]["steps"] if s["tool"] == tool]
        actions.append({"tool": tool, "results": performed or [{"status": "NOT_ATTEMPTED"}],
                        "reasons": reasons[tool], "state_paths": paths[tool],
                        "decision_event_sequences": refs(paths[tool]),
                        "recovery_event_sequences": evidence(tool)})
    relations = [
        {"from": "A", "to": "candidate", "label": "owns allocation", "events": refs(["deployments.candidate-v2.owner_run"])},
        {"from": "B", "to": "budget", "label": "approved limit " + str(state["plans"]["cost-B"]["approved_limit"]), "events": refs(["plans"])},
        {"from": "budget", "to": "candidate", "label": "%s/%s units committed; v%s" % (base.commitments(state), state["capacity_limit"], state["budget_version"]), "events": refs(["capacity_limit", "budget_version"])},
        {"from": "route", "to": "candidate", "label": "%s%% traffic; v%s" % (state["routing"]["candidate-v2"], state["route_version"]), "events": refs(["routing", "route_version"])},
        {"from": "A", "to": "approval", "label": "fixture recovery approval", "events": refs(["approvals"])},
        {"from": "approval", "to": "candidate", "label": "AP-recovery-A; plan-bound", "events": refs(["approvals"])},
    ]
    if state["route_owner"] in ("deploy-A", "route-C"):
        relations.append({"from": "A" if state["route_owner"] == "deploy-A" else "C", "to": "route", "label": "owns current route", "events": refs(["route_owner", "route_version"])})
    for dep in candidate["dependencies"]:
        relations.append({"from": "C", "to": "candidate", "label": "active dependency: " + dep["id"], "events": refs(["deployments.candidate-v2.dependencies"])})
    return {"basis": "Recorded fixture state, successful effects and actual recovery results. Not a causal proof.",
            "actions": actions, "relations": relations,
            "approval": {"id": "AP-recovery-A", "record": state["approvals"]["AP-recovery-A"],
                         "plan_digest_matches": state["approvals"]["AP-recovery-A"]["plan_digest"] == sim.digest(state["plans"]["deploy-A"])}}


def run_case(spec):
    validate_spec(spec)
    return _rehearse_validated(spec)


def _rehearse_validated(spec):
    """Internal execution shared by independently validated bounded front ends.

    Explorer replay still requires its registered profiles. Challenge Factory
    validates its own integer bounds and grammar before constructing this spec.
    This is not an unvalidated CLI or an agent-facing tool.
    """
    engine, branch = sim.Engine(":memory:"), None
    try:
        snapshot = seed(engine, spec["parameters"])
        trace = execute(engine, spec)
        before, events = engine.state(), engine.events()
        # This state-driven formula is separate from Engine.recover. The explorer
        # keeps the same approval/ownership domain and adds parameter/timing faults.
        reference = base.reference_recovery(before)
        parent_digest = base.fingerprint({"state": before, "events": events})
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
        comparison = {name: base.assess(before, after, reference) for name, after in
                      {"guarded": guarded, "pause_only": paused, "snapshot_restore": restored}.items()}
        actual = comparison["guarded"]["state"]
        checks = {"recovery_status": recovery["status"] == reference["recovery_status"],
                  "committed_units": actual["committed"] == reference["expected_committed"],
                  "candidate_units": actual["candidate_units"] == reference["expected_candidate_units"],
                  "candidate_status": actual["candidate_status"] == reference["expected_candidate_status"],
                  "candidate_traffic": actual["routing"]["candidate-v2"] == reference["expected_candidate_traffic"],
                  "route_owner": actual["route_owner"] == reference["expected_route_owner"],
                  "route_version": actual["route_version"] == reference["expected_route_version"],
                  "eligible_cleanup_complete": comparison["guarded"]["eligible_units_retained"] == 0,
                  "parent_unchanged": parent_digest == base.fingerprint({"state": engine.state(), "events": engine.events()})}
        checks.update({"parent_" + k: v for k, v in audit(engine).items()})
        checks.update({"recovered_" + k: v for k, v in audit(branch).items()})
        checks["admission_checks"] = all(all(row.get("checks", {}).values()) for row in trace)
        record = {"case_id": "explorer-" + base.fingerprint(spec)[:16], "spec": copy.deepcopy(spec),
                  "scenario": spec["scenario"], "fault": spec["fault"], "profile": spec["profile"],
                  "fault_position": spec["steps"].index("FAULT"), "trace": trace,
                  "decision_state": before, "reference": reference, "guarded_recovery": recovery,
                  "comparison": comparison, "contract_checks": checks,
                  "passed": all(checks.values()) and not comparison["guarded"]["violations"],
                  "parent_digest": parent_digest, "parent_events": events, "guarded_events": branch.events()}
        record["explanation"] = explain(record)
        return record
    finally:
        if branch is not None:
            branch.close()
        engine.close()


def failure_signature(record, strategy):
    if strategy not in ("pause_only", "snapshot_restore"):
        raise ValueError("Only the illustrative baselines have reduced counterexamples.")
    comparison = record["comparison"][strategy]
    if not comparison["violations"] and not comparison["eligible_units_retained"]:
        return None
    return {"strategy": strategy, "scenario": record["scenario"],
            "violations": sorted(comparison["violations"]),
            "eligible_units_retained": bool(comparison["eligible_units_retained"])}


def deletion_candidates(spec):
    for step in spec["steps"]:
        if step == "FAULT":
            continue
        removed = {s for s in spec["steps"] if s.startswith(step[0] + "_")} if step.endswith("READ") else {step}
        candidate = copy.deepcopy(spec)
        candidate["steps"] = [s for s in spec["steps"] if s not in removed]
        validate_spec(candidate)
        yield step, candidate


def reduce_witness(record, strategy):
    signature = failure_signature(record, strategy)
    if not record["passed"] or signature is None:
        raise ValueError("Reduction needs a baseline counterexample with passing guarded checks.")
    original, current, trials = record, record, []
    while True:
        changed = False
        for removed, spec in deletion_candidates(current["spec"]):
            candidate = run_case(spec)
            kept = candidate["passed"] and failure_signature(candidate, strategy) == signature
            trials.append({"removed": removed, "steps": spec["steps"], "guarded_passed": candidate["passed"],
                           "same_signature": failure_signature(candidate, strategy) == signature, "accepted": kept})
            if kept:
                current, changed = candidate, True
                break
        if not changed:
            break
    return {"signature": signature, "scope": REDUCTION_SCOPE, "source_case_id": original["case_id"],
            "original_spec": original["spec"], "reduced_spec": current["spec"],
            "original_operator_steps": len(original["spec"]["steps"]) - 1,
            "reduced_operator_steps": len(current["spec"]["steps"]) - 1,
            "replay_case_id": current["case_id"], "guarded_passed": current["passed"],
            "signature_preserved": failure_signature(current, strategy) == signature,
            "one_minimal_checked": True, "trials": trials, "reduced_record": current}


def lightweight(record):
    return {key: record[key] for key in ("case_id", "spec", "scenario", "fault", "profile", "fault_position",
            "passed", "trace", "reference", "comparison", "guarded_recovery", "explanation", "guarded_events")}


def overview_row(record):
    row = {key: record[key] for key in ("case_id", "scenario", "fault", "profile", "fault_position", "passed")}
    row["guarded_recovery"] = {"status": record["guarded_recovery"]["status"]}
    row["comparison"] = {"guarded": {"state": {key: record["comparison"]["guarded"]["state"][key] for key in ("committed", "limit")}}}
    row["detail_file"] = "cases/" + record["case_id"] + ".html"
    return row


REPORT = r'''<!doctype html><html lang="en"><meta charset="utf-8"><base href="__BASE__"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orkestryn · Conflict Explorer</title>
<style>:root{color-scheme:dark;font:16px system-ui;background:#0b1220;color:#e4ecf7}body{margin:auto;max-width:1250px;padding:30px}h1{font-size:32px;margin-bottom:8px}h2{font-size:22px}p{line-height:1.6;color:#b8c9dd}a{color:#66d8cf}button,select{background:#17283e;color:#e4ecf7;border:1px solid #4f6a87;border-radius:7px;padding:9px;cursor:pointer}button:focus,select:focus,a:focus{outline:3px solid #66d8cf}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card,article{background:#122137;border:1px solid #2d435e;border-radius:12px;padding:18px;margin:12px 0}.big{display:block;font-size:29px;font-weight:700;color:#72e0bc}.filters{display:flex;flex-wrap:wrap;gap:14px}label{display:grid;gap:5px}table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;border-bottom:1px solid #2d435e;padding:11px;vertical-align:top}.scroll{overflow:auto;max-height:520px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#091321;padding:14px;border-radius:8px;font-size:13px}details{margin:10px 0}svg{width:100%;max-height:390px;background:#0b1728;border-radius:10px}.muted{font-size:13px;color:#aebed1}.status{color:#72e0bc}#detail{scroll-margin-top:20px}li{line-height:1.6}footer{margin:24px 0;color:#aebed1}</style>
<a href="index.html">Campaign overview</a><h1>Conflict Explorer</h1><p>Capacity boundaries, fault timing and recovery evidence for Orkestryn.</p>
<div class="cards" id="cards"></div><p id="scope"></p>
<p><a href="summary.json">Summary</a> · <a href="campaign.jsonl">Full traces</a> · <a href="challenges.jsonl">Development inputs</a> · <a href="answers.jsonl">Separate answers</a> · <a href="witnesses.json">Reduced counterexamples</a></p>
<h2>Explore cases</h2><div class="filters" id="filters"></div><p class="muted" id="count"></p>
<div class="scroll"><table><thead><tr><th>Profile / family</th><th>Fault / position</th><th>Guarded recovery</th><th>Capacity</th><th>Inspect</th></tr></thead><tbody id="rows"></tbody></table></div>
<article id="detail" aria-live="polite" hidden><h2 id="caseTitle">Select a case</h2><p id="caseSummary"></p><button id="download" type="button">Save replay specification</button>
<h3>Recovery choices from the same state</h3><div class="scroll"><table><thead><tr><th>Choice</th><th>Capacity</th><th>Candidate</th><th>Violations</th><th>Eligible units retained</th></tr></thead><tbody id="comparison"></tbody></table></div>
<h3>Recorded relationships before recovery</h3><svg id="graph" viewBox="0 0 960 370" role="img" aria-label="Actors, approvals, budget, route and candidate relationships"></svg>
<p class="muted">The graph shows recorded relationships. The table links each relationship to journal evidence; it is not a proof of causation.</p><div class="scroll"><table><thead><tr><th>Relationship</th><th>Recorded fact</th><th>Events</th></tr></thead><tbody id="relations"></tbody></table></div>
<h3>Why cleanup proceeded or stopped</h3><div id="actions"></div><details><summary>Read/write order and injected fault</summary><pre id="trace"></pre></details>
<h3>Journal evidence</h3><pre id="event">Select an event link above.</pre></article>
<h2>Reduced baseline counterexamples</h2><p id="reductionScope"></p><div id="witnesses"></div>
<footer>Development fixtures only. No model calls, cloud changes or real worker execution. Existing model holdouts are untouched.</footer>
<script id="data" type="application/json">__DATA__</script><script>
"use strict";
const data=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const el=(tag,text,parent)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.append(n);return n;};
const s=data.summary;[[s.cases_passed+'/'+s.cases_total,'Guarded reference passes'],[s.distinct_decision_states,'Distinct development states'],[s.reduced_witnesses,'Reduced baseline witnesses'],[0,'Model calls']].forEach(([v,t])=>{const c=el('div',undefined,$('cards'));c.className='card';el('strong',v,c).className='big';el('span',t,c);});
$('scope').textContent=s.scope+' Profile coverage: '+s.profiles.length+'; fault types: '+s.faults.length+'.';
$('reductionScope').textContent=s.reduction_scope;
let current=null;const selects={};
for(const [key,label] of [['profile','Capacity profile'],['scenario','Conflict family'],['fault','Fault'],['fault_position','Fault after steps']]){const l=el('label',label,$('filters')),sel=el('select',undefined,l);el('option','All',sel).value='';[...new Set(data.rows.map(r=>String(r[key])))].sort().forEach(v=>el('option',v,sel).value=v);selects[key]=sel;sel.addEventListener('change',filter);}
function refs(ids,parent){ids.forEach(id=>{const b=el('button','#'+id,parent);b.type='button';b.addEventListener('click',()=>{$('event').textContent=JSON.stringify(current.guarded_events.find(e=>e.sequence===id),null,2);$('event').scrollIntoView({block:'nearest'});});});}
function graph(r){const root=$('graph');root.replaceChildren();const points={A:[120,45],B:[360,45],C:[720,45],approval:[120,165],budget:[360,165],route:[720,165],candidate:[480,320]};const ns='http://www.w3.org/2000/svg';function shape(name,attrs,text){const n=document.createElementNS(ns,name);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text)n.textContent=text;root.append(n);}
r.explanation.relations.forEach(edge=>{const a=points[edge.from],b=points[edge.to];shape('line',{x1:a[0],y1:a[1]+16,x2:b[0],y2:b[1]-16,stroke:edge.from==='C'?'#ffb366':'#527994','stroke-width':2});});Object.entries(points).forEach(([id,[x,y]])=>{shape('rect',{x:x-77,y:y-20,width:154,height:40,rx:9,fill:'#18354a',stroke:'#66d8cf'});shape('text',{x,y:y+5,'text-anchor':'middle',fill:'#eef7ff','font-size':15},id==='candidate'?'candidate-v2':id==='approval'?'Recovery approval':id==='A'?'A · deployment':id==='B'?'B · capacity':id==='C'?'C · consumer':id);});}
function show(r){if(!r.explanation){window.location.href=r.detail_file;return;}current=r;$('detail').hidden=false;$('caseTitle').textContent=r.case_id;$('caseSummary').textContent=r.profile+' · '+r.scenario+' · '+r.fault+' after '+r.fault_position+' steps · '+r.guarded_recovery.status;$('trace').textContent=JSON.stringify(r.trace,null,2);$('event').textContent='Select an event link above.';graph(r);$('relations').replaceChildren();r.explanation.relations.forEach(e=>{const tr=el('tr',undefined,$('relations'));el('td',e.from+' → '+e.to,tr);el('td',e.label,tr);refs(e.events,el('td',undefined,tr));});$('actions').replaceChildren();r.explanation.actions.forEach(a=>{const box=el('div',undefined,$('actions'));box.className='card';el('strong',a.tool+' — '+a.results.map(x=>x.status+(x.code?' / '+x.code:'')).join(', '),box);a.reasons.forEach(reason=>el('p',reason,box));el('p','State: '+a.state_paths.join(', '),box).className='muted';el('span','Decision evidence: ',box);refs(a.decision_event_sequences,box);el('span',' Recovery evidence: ',box);refs(a.recovery_event_sequences,box);});$('comparison').replaceChildren();Object.entries(r.comparison).forEach(([name,c])=>{const tr=el('tr',undefined,$('comparison'));[name,c.state.committed+'/'+c.state.limit,c.state.candidate_status,c.violations.join(', ')||'none',c.eligible_units_retained].forEach(v=>el('td',String(v),tr));});}
function filter(){const rows=data.rows.filter(r=>Object.entries(selects).every(([k,v])=>!v.value||String(r[k])===v.value));$('rows').replaceChildren();$('count').textContent=rows.length+' matching cases; showing up to 100. Full results are in the trace file.';rows.slice(0,100).forEach(r=>{const tr=el('tr',undefined,$('rows'));el('td',r.profile+' / '+r.scenario,tr);el('td',r.fault+' / '+r.fault_position,tr);el('td',(r.passed?'PASS · ':'FAIL · ')+r.guarded_recovery.status,tr);const v=r.comparison.guarded.state;el('td',v.committed+'/'+v.limit,tr);const b=el('button','Inspect',el('td',undefined,tr));b.addEventListener('click',()=>{show(r);$('detail').scrollIntoView();});});if(rows.length&&rows[0].explanation)show(rows[0]);}
$('download').addEventListener('click',()=>{if(!current)return;const url=URL.createObjectURL(new Blob([JSON.stringify(current.spec,null,2)+'\n'],{type:'application/json'})),a=el('a');a.href=url;a.download=current.case_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
data.witnesses.forEach(w=>{const box=el('div',undefined,$('witnesses'));box.className='card';el('strong',w.signature.strategy+' · '+w.original_operator_steps+' → '+w.reduced_operator_steps+' operator steps',box);el('p','Violations: '+(w.signature.violations.join(', ')||'none')+'; eligible cleanup retained: '+w.signature.eligible_units_retained,box);el('p',w.reduced_spec.profile+' · '+w.reduced_spec.fault+' · '+w.reduced_spec.steps.join(' → '),box);const a=el('a','Replay specification',box);a.href=w.replay_file;const b=el('button','Inspect reduced witness',box);b.addEventListener('click',()=>{show(w.row);$('detail').scrollIntoView();});});filter();
</script></html>'''


def render_report(summary, rows, witnesses, detail=False):
    payload = json.dumps({"summary": summary, "rows": rows, "witnesses": witnesses}, ensure_ascii=True).replace("<", "\\u003c")
    return REPORT.replace("__DATA__", payload).replace("__BASE__", "../" if detail else "./")


def run_campaign(output, scenario="all", profile="all", fault="all", max_schedules=8, replay=None):
    for value, allowed, name in ((scenario, base.SCENARIOS, "scenario"), (profile, PROFILES, "profile"), (fault, FAULTS, "fault")):
        if value != "all" and value not in allowed:
            raise ValueError("Unknown " + name + ".")
    orders = selected_schedules(max_schedules)
    if replay is not None:
        validate_spec(replay)
        specs = [copy.deepcopy(replay)]
    else:
        specs = (make_spec(order, p, family, f, pos)
                 for p in (PROFILES if profile == "all" else (profile,))
                 for family in (base.SCENARIOS if scenario == "all" else (scenario,))
                 for f in (FAULTS if fault == "all" else (fault,)) for pos in POSITIONS for order in orders)
    now, started = datetime.now(timezone.utc), time.monotonic()
    directory = Path(output).resolve() / (now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    rows, unique, seeds, witnesses = [], {}, {}, []
    outcomes, blocks, coverage = Counter(), Counter(), {key: set() for key in ("profile", "scenario", "fault", "fault_position")}
    totals = {name: Counter() for name in base.STRATEGIES}
    passed, checked, checks_passed, checks_total = 0, 0, 0, 0
    with (directory / "campaign.jsonl").open("w", encoding="utf-8") as stream:
        for spec in specs:
            record = run_case(spec)
            stream.write(base.encode(record) + "\n")
            rows.append(overview_row(record))
            passed += int(record["passed"])
            checked += 1
            checks_passed += sum(record["contract_checks"].values())
            checks_total += len(record["contract_checks"])
            outcomes[record["guarded_recovery"]["status"]] += 1
            for key in coverage:
                coverage[key].add(record[key])
            for item in record["trace"]:
                if item.get("result", {}).get("status") == "BLOCKED":
                    blocks[item["result"]["code"]] += 1
            for strategy, result in record["comparison"].items():
                totals[strategy]["constraint_passes"] += int(not result["violations"])
                totals[strategy]["eligible_units_retained_total"] += result["eligible_units_retained"]
            inputs, answer = base.challenge_pair(record)
            inputs["schema_version"] = VERSION + "-recovery-input"
            inputs["scope"] = "Expanded development recovery fixtures; not held-out evaluation."
            if inputs["case_id"] in unique and unique[inputs["case_id"]][1]["reference"] != answer["reference"]:
                raise RuntimeError("Identical decision states produced inconsistent reference labels.")
            unique.setdefault(inputs["case_id"], (inputs, answer))
            for strategy in ("snapshot_restore", "pause_only"):
                signature = failure_signature(record, strategy)
                if record["passed"] and signature:
                    # First witness per fault/family/baseline signature, not per
                    # parameter profile; coverage remains in the full campaign.
                    seeds.setdefault(base.encode([record["fault"], signature]), (record, strategy))
            if checked % 250 == 0:
                print("Explorer: %s/%s reference passes so far" % (passed, checked), flush=True)
    (directory / "replays").mkdir()
    web_witnesses = []
    for index, (record, strategy) in enumerate(seeds.values(), 1):
        witness = reduce_witness(record, strategy)
        witness["replay_file"] = "replays/witness-%03d.json" % index
        (directory / witness["replay_file"]).write_text(json.dumps(witness["reduced_spec"], indent=2) + "\n", encoding="utf-8")
        web_witnesses.append({k: v for k, v in witness.items() if k not in ("trials", "reduced_record")})
        web_witnesses[-1]["row"] = lightweight(witness["reduced_record"])
        witnesses.append(witness)
    publish = passed == checked
    for filename, side in (("challenges.jsonl", 0), ("answers.jsonl", 1)):
        (directory / filename).write_text("".join(base.encode(pair[side]) + "\n" for pair in unique.values()) if publish else "", encoding="utf-8")
    (directory / "witnesses.json").write_text(json.dumps(witnesses, indent=2) + "\n", encoding="utf-8")
    if replay is not None:
        (directory / "case_spec.json").write_text(json.dumps(replay, indent=2) + "\n", encoding="utf-8")
    summary = {"version": VERSION, "mode": MODE, "passed": publish,
        "cases_total": checked, "cases_passed": passed, "contract_checks_passed": checks_passed, "contract_checks_total": checks_total,
        "distinct_decision_states": len(unique), "challenges_exported": len(unique) if publish else 0,
        "reduced_witnesses": len(witnesses), "all_reductions_validated": all(w["guarded_passed"] and w["signature_preserved"] and w["one_minimal_checked"] for w in witnesses),
        "reduction_scope": REDUCTION_SCOPE, "profiles": sorted(coverage["profile"]), "faults": sorted(coverage["fault"]),
        "families": sorted(coverage["scenario"]), "fault_positions": sorted(coverage["fault_position"]),
        "schedules_per_combination": None if replay is not None else len(orders), "legal_full_schedules": 210,
        "scope": ("Replay of one validated fixed-format program." if replay is not None else
                  "%s of 210 legal schedules sampled at evenly spaced indices for each profile/family/fault/position combination." % len(orders)) +
                  " Fixed canary setup and operator programs; serial interleavings, not distributed concurrency or unseen-task accuracy.",
        "outcomes": dict(outcomes), "blocked_action_codes": dict(blocks),
        "strategy_totals": {name: dict(total) for name, total in totals.items()},
        "model_calls": 0, "created_at": now.isoformat(), "elapsed_seconds": round(time.monotonic() - started, 3),
        "platform": platform.system(), "python_version": platform.python_version(),
        "replay": replay is not None, "report_browser_visual_qa": "NOT_AUTOMATICALLY_PERFORMED"}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    # Separate detail pages keep the overview small and work from file:// on
    # Windows without a server, fetch permissions or third-party JavaScript.
    (directory / "cases").mkdir()
    with (directory / "campaign.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            page = render_report(summary, [lightweight(record)], [], detail=True)
            (directory / "cases" / (record["case_id"] + ".html")).write_text(page, encoding="utf-8")
    (directory / "index.html").write_text(render_report(summary, rows, web_witnesses), encoding="utf-8")
    return directory, summary


class ExplorerTests(unittest.TestCase):
    def spec(self, **kwargs):
        return make_spec(base.schedules()[0], **kwargs)

    def test_even_sample_and_bounds(self):
        self.assertEqual(len(set(selected_schedules())), 8)
        self.assertEqual(selected_schedules()[0], base.schedules()[0])
        self.assertEqual(selected_schedules()[-1], base.schedules()[-1])
        self.assertEqual(selected_schedules(210), base.schedules())
        for invalid in (0, 211, True, 2.5):
            with self.assertRaises(ValueError):
                selected_schedules(invalid)

    def test_all_parameter_fault_position_combinations(self):
        # One schedule at every profile/fault/position/family boundary. Campaign
        # adds the other sampled orderings; this isn't a second full campaign.
        for profile in PROFILES:
            for fault in FAULTS:
                for scenario in base.SCENARIOS:
                    for position in POSITIONS:
                        with self.subTest(profile=profile, fault=fault, scenario=scenario, position=position):
                            record = run_case(self.spec(profile=profile, fault=fault, scenario=scenario, position=position))
                            self.assertTrue(record["passed"], record["contract_checks"])

    def test_capacity_exact_fit_and_one_over(self):
        for profile, expected in (("exact_fit", "SUCCEEDED"), ("over_limit_by_one", "BLOCKED")):
            record = run_case(self.spec(profile=profile))
            result = next(x["result"] for x in record["trace"] if x["step"] == "A_RESERVE")
            self.assertEqual(result["status"], expected)

    def test_cost_limit_below_commitment_cannot_apply(self):
        record = run_case(self.spec(profile="cost_below_commitments"))
        result = next(x["result"] for x in record["trace"] if x["step"] == "B_LIMIT")
        self.assertEqual(result["code"], "LIMIT_BELOW_COMMITMENTS")
        self.assertEqual(record["decision_state"]["capacity_limit"], 100)

    def test_metric_age_boundary(self):
        for fault, expected in (("metrics_at_limit", "SUCCEEDED"), ("metrics_expired", "BLOCKED")):
            record = run_case(self.spec(fault=fault, position=0))
            result = next(x["result"] for x in record["trace"] if x["step"] == "A_ROUTE")
            self.assertEqual(result["status"], expected)
            if expected == "BLOCKED":
                self.assertEqual(result["code"], "METRICS_STALE")

    def test_early_workflow_failure_durably_closes_admission(self):
        early = run_case(self.spec(position=0))
        for row in early["trace"]:
            if row["step"] in ("A_RESERVE", "A_ROUTE"):
                self.assertEqual(row["result"]["code"], "WORKFLOW_PAUSED")
        self.assertTrue(early["contract_checks"]["parent_journal_matches_state"])
        self.assertNotEqual(early["parent_digest"], run_case(self.spec(position=7))["parent_digest"])

    def test_in_flight_keeps_allocation_even_with_zero_traffic(self):
        spec = self.spec(fault="requests_in_flight")
        spec["steps"] = ["FAULT"]
        record = run_case(spec)
        self.assertEqual(record["comparison"]["guarded"]["state"]["routing"]["candidate-v2"], 0)
        self.assertEqual(record["comparison"]["guarded"]["state"]["candidate_units"], 20)
        self.assertIn("IN_FLIGHT_REQUESTS", record["reference"]["review_reasons"])

    def test_replay_validation_rejects_mutation_and_unregistered_actions(self):
        cases = []
        bad = self.spec(); bad["parameters"]["extra_units"] = 999; cases.append(bad)
        bad = self.spec(); bad["execute"] = "shell"; cases.append(bad)
        bad = self.spec(); bad["steps"] = ["A_RESERVE", "FAULT"]; cases.append(bad)
        bad = self.spec(); bad["steps"] = ["A_READ", "A_ROUTE", "A_RESERVE", "FAULT"]; cases.append(bad)
        bad = self.spec(); bad["steps"] = ["FAULT", "FAULT"]; cases.append(bad)
        bad = self.spec(); bad["steps"] = ["FAULT", "shell"]; cases.append(bad)
        bad = self.spec(); bad["profile"] = []; cases.append(bad)
        for spec in cases:
            with self.assertRaises(ValueError):
                validate_spec(spec)

    def test_duplicate_json_fields_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "case.json"
            raw = json.dumps(self.spec()).replace('"profile": "baseline"', '"profile": "baseline", "profile": "baseline"')
            path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_spec(path)

    def test_replay_is_deterministic_and_source_unchanged(self):
        spec = self.spec(fault="stop_timeout", scenario="dependency")
        original = copy.deepcopy(spec)
        self.assertEqual(run_case(spec), run_case(spec))
        self.assertEqual(spec, original)

    def test_reduction_preserves_signature_and_is_one_minimal(self):
        record = run_case(self.spec(scenario="dependency"))
        witness = reduce_witness(record, "snapshot_restore")
        self.assertLess(witness["reduced_operator_steps"], 7)
        reduced = witness["reduced_record"]
        self.assertEqual(failure_signature(record, "snapshot_restore"), failure_signature(reduced, "snapshot_restore"))
        for _, candidate in deletion_candidates(witness["reduced_spec"]):
            checked = run_case(candidate)
            self.assertFalse(checked["passed"] and failure_signature(checked, "snapshot_restore") == witness["signature"])
        self.assertEqual(witness["original_spec"], record["spec"])

    def test_explanations_reference_real_events_and_preserved_dependency(self):
        record = run_case(self.spec(scenario="dependency"))
        sequences = {e["sequence"] for e in record["guarded_events"]}
        explanation = record["explanation"]
        for row in explanation["actions"]:
            self.assertTrue(set(row["decision_event_sequences"] + row["recovery_event_sequences"]) <= sequences)
        for relation in explanation["relations"]:
            self.assertTrue(set(relation["events"]) <= sequences)
            if relation["label"].startswith("active dependency:"):
                source = next(e for e in record["parent_events"] if e["sequence"] == relation["events"][0])
                self.assertEqual(source["tool"], "add_dependency")
        stop = next(a for a in explanation["actions"] if a["tool"] == "stop_candidate")
        self.assertTrue(any("dependency" in reason for reason in stop["reasons"]))
        self.assertEqual(stop["results"][0]["status"], "BLOCKED")
        self.assertTrue(explanation["approval"]["plan_digest_matches"])

    def test_reference_disagreement_withholds_challenges(self):
        real = run_case(self.spec())
        real["passed"] = False
        with tempfile.TemporaryDirectory() as td, mock.patch(__name__ + ".run_case", return_value=real):
            directory, summary = run_campaign(td, replay=self.spec())
            self.assertFalse(summary["passed"])
            self.assertEqual((directory / "challenges.jsonl").read_text(), "")
            self.assertEqual((directory / "answers.jsonl").read_text(), "")
            self.assertTrue((directory / "campaign.jsonl").stat().st_size > 0)

    def test_campaign_exports_separate_answers_and_replays(self):
        with tempfile.TemporaryDirectory() as td:
            directory, summary = run_campaign(td, replay=self.spec(scenario="dependency"))
            self.assertTrue(summary["passed"])
            self.assertEqual(summary["cases_total"], 1)
            inputs = json.loads((directory / "challenges.jsonl").read_text())
            answers = json.loads((directory / "answers.jsonl").read_text())
            self.assertNotIn("reference", inputs)
            self.assertNotIn("expected", inputs)
            self.assertEqual(inputs["case_id"], answers["case_id"])
            detail = next((directory / "cases").glob("*.html"))
            self.assertIn('<base href="../">', detail.read_text(encoding="utf-8"))
            self.assertIn('cases/' + detail.name, (directory / "index.html").read_text(encoding="utf-8"))
            for path in (directory / "replays").glob("*.json"):
                self.assertTrue(run_case(load_spec(path))["passed"])

    def test_report_escapes_script_breakouts(self):
        rendered = render_report({"scope": "</script><script>alert(1)</script>"}, [], [])
        self.assertNotIn("</script><script>alert(1)", rendered)
        self.assertIn("\\u003c/script>", rendered)
        self.assertNotIn("innerHTML", rendered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *base.SCENARIOS), default="all")
    parser.add_argument("--profile", choices=("all", *PROFILES), default="all")
    parser.add_argument("--fault", choices=("all", *FAULTS), default="all")
    parser.add_argument("--max-schedules", type=int, default=8)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "explorer_runs")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    print(MODE, flush=True)
    directory, summary = run_campaign(args.output, args.scenario, args.profile, args.fault, args.max_schedules,
                                      load_spec(args.replay) if args.replay else None)
    print("Reference passes: %s/%s | distinct development states: %s | reduced witnesses: %s | model calls: 0" % (
        summary["cases_passed"], summary["cases_total"], summary["distinct_decision_states"], summary["reduced_witnesses"]))
    print("Report: " + str(directory / "index.html"))
    if args.open:
        webbrowser.open((directory / "index.html").as_uri())
    return 0 if summary["passed"] and summary["all_reductions_validated"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print("EXPLORER FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
