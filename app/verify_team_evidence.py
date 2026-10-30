#!/usr/bin/env python3
"""Read-only evidence audit for a completed Orkestryn v3 live team.

Reuses registered policy/rehearsal and legacy audit helpers from the supplied
trusted project. No API clients, model requests or worker processes are started.
Does not execute any Python from the evidence ZIP. Local consistency only.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, math, shutil, sqlite3, sys, tempfile, zipfile
from datetime import datetime, timezone
from pathlib import Path
sys.dont_write_bytecode = True
bootstrap = argparse.ArgumentParser(add_help=False)
bootstrap.add_argument("--project", type=Path, default=Path.cwd())
project = bootstrap.parse_known_args()[0].project.resolve()
sys.path.insert(0, str(project))
import agent_team as team
import challenge_factory as factory
import conflict_explorer as explorer
import conflict_lab as reference
import plan_revision as revision
import service_agent as agent
import simulator as sim
from verify_live_evidence import EvidenceError, frames, require, same
from verify_service_agent_evidence import read, digest, extract, snapshot
VERSION = "agent-rehearsal-team-evidence-audit-v2"

def audit_service(root):
    checks = {}
    check = lambda name, value: require(checks, name, value)
    report = read(root / "report.json")
    calls = read(root / "agent_calls.json")
    runtime_events = read(root / "runtime_events.json")
    challenge = factory.validate_proposal(read(root / "challenge.json"))
    check("registered_agent_version", report["version"] == agent.VERSION)
    check("completed_two_call_live_run", report["stage"] == "RECOVERED" and report["model_calls"] == 2
          and report["scripted_consent"] is False and len(calls) == 2)
    source_match = {"mode":"CURRENT_SOURCE", "controller_digest":agent.controller_digest()}
    check("controller_and_adapter_source_match", source_match["controller_digest"] == report["controller_digest"])
    check("attempt_ledger_matches_report", calls == report["attempts"])
    check("challenge_digest_matches", sim.digest(challenge) == report["source_challenge_digest"])
    check("team_challenge_copy_matches", read(root / "challenge.json") == read(root.parent / "challenge.json"))
    check("source_baseline_recomputed", factory.rehearse(challenge) == read(root / "source_baseline.json"))
    check("mapped_program_matches_source", factory.executable(challenge) == read(root / "local_program.json") == report["local_program"])

    database = root / "coordinator.sqlite3"
    con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        check("sqlite_integrity", con.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok")
        final = json.loads(con.execute("SELECT payload FROM service_state WHERE id=1").fetchone()[0])
        events = [{"sequence": x[0], **json.loads(x[1])} for x in con.execute("SELECT sequence,event FROM events ORDER BY sequence")]
        tables = {table: [dict(x) for x in con.execute("SELECT * FROM " + table)]
                  for table in ("action_receipts", "revisions", "revision_decisions", "local_binding", "local_intents", "agent_bindings")}
    finally:
        con.close()
    check("saved_state_matches_database", final == report["state"])
    check("saved_journal_matches_database", events == report["events"])
    states = frames(events)  # verifies contiguous order and every before-image
    check("journal_reconstructs_final_state", states[-1] == final)
    for index, state in enumerate(states):
        for name, ok in reference.invariant_checks(state).items():
            check("frame_%02d_%s" % (index, name), ok)
    with_engine = snapshot(final, events, tables["action_receipts"])
    try:
        journal_audit = explorer.audit(with_engine)
        for name, ok in journal_audit.items():
            check("journal_" + name, ok)
        check("report_journal_audit_matches", journal_audit == report["journal_audit"])
    finally:
        with_engine.close()

    def unique(kind):
        matches = [event for event in events if event["kind"] == kind]
        check("one_" + kind.lower(), len(matches) == 1)
        return matches[0]

    proposed, approved, consent, failure = [unique(kind) for kind in
                                          ("REVISION_PROPOSED", "REVISION_APPROVED", "REVISION_CONSENT", "SERVICE_AGENT_WORKFLOW_FAILURE")]
    check("one_revision_and_manager_decision", len(tables["revisions"]) == len(tables["revision_decisions"]) == 1)
    record = tables["revisions"][0]
    proposal = revision.validate_proposal(report["proposal"])
    grant = json.loads(record["grant_record"])
    before = states[proposed["sequence"] - 1]
    check("revision_identity_and_controller", report["revision_id"] == record["revision_id"] == proposed["revision_id"]
          == approved["revision_id"] == consent["revision_id"] and record["implementation_digest"] == revision.implementation_digest())
    check("exact_proposal_stored", proposal == json.loads(record["proposal"]) and sim.digest(proposal) == record["proposal_digest"]
          and record["base_state_digest"] == proposal["based_on_state"] == sim.digest(before))
    check("proposal_independently_eligible", revision.admission_reference(before, proposal) == "ELIGIBLE")
    parent = snapshot(before, events[:proposed["sequence"] - 1], tables["action_receipts"])
    try:
        proof = revision.rehearse(parent, proposal, record["revision_id"])
        check("rehearsal_recomputed_in_isolated_memory", proof == json.loads(record["rehearsal"]) == report["rehearsal"] and proof["passed"])
        expected_context = revision.context_for(parent)
    finally:
        parent.close()
    tokens, metadata_records, contexts = 0, [], {}
    for number, phase in enumerate(("repair", "recovery"), 1):
        directory = root / "attempts" / ("%03d-%s" % (number, phase))
        context, prompt, response = [read(directory / filename) for filename in ("context.json", "prompt.json", "response.json")]
        call, metadata = calls[number - 1], response["metadata"]
        contexts[phase] = context
        check(phase + "_ledger_and_context", call["phase"] == phase and call["status"] == "RECEIVED"
              and call["live"] is True and call["context_sha256"] == sim.digest(context) and call["metadata"] == metadata)
        check(phase + "_phase_prompt_matches_source", prompt["system"] == (revision.SYSTEM if phase == "repair" else agent.RECOVERY_SYSTEM))
        check(phase + "_settings_and_model", prompt["request_settings"] == factory.inference.inference_settings(10000, True)
              and prompt["model_requested"] == metadata["model"] == factory.inference.MODEL)
        check(phase + "_response_complete", response["content_truncated_for_storage"] is False
              and metadata["finish_reason"] == "stop" and metadata["refused"] is False and bool(metadata["response_id"]))
        check(phase + "_usage_arithmetic", all(type(metadata[k]) is int and metadata[k] >= 0
              for k in ("input_tokens", "output_tokens", "total_tokens"))
              and metadata["input_tokens"] + metadata["output_tokens"] == metadata["total_tokens"])
        tokens += metadata["total_tokens"]
        metadata_records.append(metadata)
        parsed = revision.validate_proposal(factory.decode(response["visible_content"])) if phase == "repair" else agent.parse_recovery(response["visible_content"], sim.digest(context["current_state"]))
        check(phase + "_visible_decision_matches", parsed == (proposal if phase == "repair" else report["recovery_decision"]))
    check("repair_context_recomputed", {key: contexts["repair"][key] for key in expected_context} == expected_context)
    check("repair_observation_matches", contexts["repair"]["local_observation"] == report["initial_observation"])
    check("token_totals_complete", report["usage"] == {"reported_tokens": tokens, "calls_with_usage": 2, "missing_usage_calls": 0})
    check("distinct_provider_response_ids", metadata_records[0]["response_id"] != metadata_records[1]["response_id"])
    check("approval_before_execution", proposed["sequence"] < approved["sequence"] < consent["sequence"] < failure["sequence"])
    check("manager_consent_exact_scope", consent["actor"] == "MANAGER" and consent["source"] == "EXPLICIT LOCAL MANAGER FIXTURE CONSENT"
          and consent["result"] == report["approval"] == json.loads(tables["revision_decisions"][0]["result"]))
    check("decision_receipt_fingerprint", tables["revision_decisions"][0]["decision_key"] == consent["decision_key"]
          and tables["revision_decisions"][0]["fingerprint"] == sim.digest({key: consent[key] for key in ("revision_id", "note", "actor", "source")}))
    approved_state, expected_grant = revision.authorized_state(states[approved["sequence"] - 1], proposal, record["revision_id"], consent["source"], consent["note"])
    check("approval_state_and_grant_recomputed", approved_state == states[approved["sequence"]] and grant == expected_grant == approved["grant"])
    check("approval_did_not_allocate", reference.commitments(approved_state) == reference.commitments(before))
    executions = [event for event in events if event.get("action_key") == "execute-" + record["revision_id"]]
    check("exactly_one_reserved_action", len(executions) == 1)
    execution = executions[0]
    check("approved_exact_execution", consent["sequence"] < execution["sequence"] < failure["sequence"] and execution["tool"] == grant["exact_tool"]
          and execution["args"] == grant["exact_arguments"] and execution["approval_id"] == grant["approval_id"]
          and execution["result"] == report["execution"] and execution["result"]["status"] == "SUCCEEDED")
    reserved = states[execution["sequence"]]
    check("reservation_arithmetic", reference.commitments(reserved) == reference.commitments(before) + proposal["additional_units"] == report["after_execution_units"])
    check("failure_durably_paused_before_recovery_context", "deploy-A" in states[failure["sequence"]]["paused_runs"]
          and contexts["recovery"]["current_state"] == states[failure["sequence"]]
          and contexts["recovery"]["workflow_failure_recorded"] is True)
    recovery_before = contexts["recovery"]["current_state"]
    check("recovery_scope_and_state_bound", contexts["recovery"]["based_on_state"] == sim.digest(recovery_before)
          and contexts["recovery"]["approved_recovery_scope"] == grant and report["recovery_decision"]["decision"] == "recover")
    expected = reference.reference_recovery(recovery_before)
    assessment = reference.assess(recovery_before, final, expected)
    check("independent_recovery_reference_recomputed", expected == report["independent_recovery_reference"])
    for name, ok in assessment["checks"].items():
        check("recovery_" + name, ok)
    check("eligible_cleanup_complete", assessment["eligible_units_retained"] == 0)
    check("recovery_status_and_capacity_match_reference", report["recovery"]["status"] == expected["recovery_status"]
          and reference.commitments(final) == expected["expected_committed"] == report["committed_units"] == report["recovery"]["committed_units"]
          and report["capacity_limit"] == final["capacity_limit"])
    recovery_events = [event for event in events if event["kind"] == "ACTION" and event["sequence"] > failure["sequence"]]
    check("recovery_steps_match_journal", report["recovery"]["steps"] == [{"tool": e["tool"], "result": e["result"]} for e in recovery_events])
    check("recovery_report_matches_journal", unique("RECOVERY_REPORT")["report"] == report["recovery"])

    actual = report["actual_at_outcome"]
    check("one_live_binding", len(tables["agent_bindings"]) == len(tables["local_binding"]) == 1)
    binding = json.loads(tables["agent_bindings"][0]["binding"])
    check("exact_runtime_and_controller_binding", binding == {"controller_digest": report["controller_digest"], "runtime": report["initial_observation"], "context_state_digest": proposal["based_on_state"], "approval_freshness":"post-review-probe-v1", "approval_probe":report["approval_probe"]}
          and tables["local_binding"][0]["runtime_id"] == actual["runtime_id"])
    check("physical_route_matches_confirmed_state", actual["candidate_percent"] == final["routing"]["candidate-v2"] and actual["route_owner"] == final["route_owner"])
    check("runtime_event_sequence", [event["sequence"] for event in runtime_events] == list(range(1, len(runtime_events) + 1)))
    operations = [event for event in runtime_events if event["kind"] == "OPERATION_RESULT"]
    check("all_external_intents_confirmed", len(operations) == len(tables["local_intents"]) == len(actual["receipts"])
          and all(intent["status"] == "CONFIRMED" for intent in tables["local_intents"]))
    for i, intent in enumerate(tables["local_intents"]):
        operation, request = json.loads(intent["operation"]), json.loads(intent["request"])
        raw = [event for event in operations if event["request"] == operation]
        confirmed = [event for event in events if event["kind"] == "ACTION" and event.get("action_key") == intent["action_key"]]
        started = [event for event in events if event["kind"] == "LOCAL_INTENT" and event["action_key"] == intent["action_key"]]
        check("effect_%d_intent_receipt_chain" % i, len(raw) == len(confirmed) == len(started) == 1
              and started[0]["sequence"] < confirmed[0]["sequence"] and started[0]["operation"] == operation
              and intent["fingerprint"] == sim.digest(request)
              and actual["receipts"].get(operation["operation_id"]) == raw[0]["result"]
              and raw[0]["result"]["fingerprint"] == sim.digest(operation) and raw[0]["result"]["status"] == "SUCCEEDED"
              and confirmed[0]["runtime_confirmation"]["operation_id"] == operation["operation_id"])
    measures = [event for event in events if event["kind"] == "LOCAL_MEASUREMENTS"]
    check("one_initial_raw_probe_batch", len(measures) == 1 and len(measures[0]["samples"]) == 100)
    samples = measures[0]["samples"]
    metrics = before["deployments"]["candidate-v2"]["metrics"]
    check("probe_values_recomputed", metrics["samples"] == len(samples) and metrics["correctness"] == sum(s["ok"] and s.get("body", {}).get("result") == 49 for s in samples)
          and metrics["error_rate"] == sum(not s["ok"] for s in samples)
          and metrics["p95_ms"] == sorted(s["latency_ms"] for s in samples)[94])
    check("report_observation_after_probe", 0 <= report["initial_observation"]["observed_unix"] - metrics["observed_unix"] <= 30)
    check("worker_identities_stable", all(report["initial_observation"]["runtime"]["workers"][name]["identity"] == worker["identity"]
          and report["initial_observation"]["runtime"]["workers"][name]["pid"] == worker["pid"] for name, worker in actual["workers"].items()))
    check("single_candidate_and_no_unsafe_stop", actual["candidate_starts"] == 1 and
          (expected["candidate_releasable"] or (actual["candidate_stops"] == 0 and actual["workers"]["candidate-v2"]["alive"] is True)))
    check("availability_aggregate_valid", report["availability"]["requests"] == report["availability"]["successful"] == 100
          and 0 <= report["availability"]["candidate_requests"] <= 100)
    teardown = runtime_events[-1]
    check("teardown_separate_from_policy_recovery", teardown["kind"] == "HARNESS_TEARDOWN" and report["teardown"]["all_workers_stopped"] is True
          and len(teardown["workers"]) == 2 and all(worker["stopped"] and worker["exit_code"] is not None
          and worker["pid"] == actual["workers"][worker["target"]]["pid"] for worker in teardown["workers"]))
    check("recorded_check_flags_true", report["checks_passed"] is True and all(value is True for value in report["checks"].values()))
    return {"status": "PASSED", "checks_passed": len(checks), "checks_total": len(checks), "checks": checks,
            "recorded_run_checks": len(report["checks"]), "model_calls": 2, "reported_tokens": tokens,
            "platform_recorded": report["platform"], "python_version_recorded": report["python_version"],
            "created_at_recorded": report["created_at"], "controller_digest": report["controller_digest"], "controller_source_provenance":source_match,
            "scenario": challenge["scenario"], "fault": challenge["fault"], "approved_units": proposal["additional_units"],
            "capacity_path": [reference.commitments(before), reference.commitments(reserved), reference.commitments(final)],
            "capacity_limit": final["capacity_limit"], "recovery_status": report["recovery"]["status"],
            "initial_raw_http_samples": 100, "post_outcome_availability_aggregate": report["availability"],
            "journal_events": len(events), "confirmed_runtime_effects": len(operations),
            "provider_response_metadata": metadata_records}


def audit_team(root):
    """Compare all saved claims with the journal, source and raw observations."""
    root = Path(root)
    checks = {}
    check = lambda name, value: require(checks, name, value)
    summary, handoffs, challenge, raw, baseline = [read(root/name) for name in
        ("summary.json", "coordination.json", "challenge.json", "factory_proposal.json", "factory_baseline.json")]
    service = root/"service"
    report = read(service/"report.json")
    check("current_registered_team_version", summary["version"] == team.VERSION == "agent-rehearsal-three-agent-team-v3")
    check("team_source_matches_current_bytes", summary["implementation_digest"] == team.implementation_digest())
    check("current_service_source_matches", report["controller_digest"] == agent.controller_digest())
    check("completed_live_team", summary["status"] == "RECOVERED" and summary["full_three_role_flow_completed"] is True
        and summary["mode"] == report["mode"] == "LIVE NEBIUS THREE-AGENT TEAM; EXACT TERMINAL CONSENT"
        and summary["model_calls"] == summary["role_requests"] == summary["live_call_ceiling"] == 3
        and report["scripted_consent"] is False)
    check("registered_role_order", summary["role_order"] == list(team.ROLES)
        and [c["role"] for c in summary["calls"]] == list(team.ROLES))
    check("factory_contract_version", summary["factory_contract_version"] == team.FACTORY_VERSION)
    compiled, recomputed_baseline = team.compile_factory_proposal(raw, summary["scenario"])
    check("factory_compilation_and_rehearsal_recomputed", compiled == challenge
        and baseline == recomputed_baseline == read(service/"source_baseline.json") and baseline["passed"] is True)
    check("compiled_challenge_is_the_service_input", challenge == read(service/"challenge.json")
        and sim.digest(challenge) == report["source_challenge_digest"])
    check("handoffs_identical_to_summary", handoffs == summary["handoffs"])
    expected_order = [("coordinator","factory","decision_requested"), ("factory","coordinator","decision_proposed"),
        ("coordinator","governance","validated_factory_case"), ("coordinator","governance","decision_requested"),
        ("governance","coordinator","decision_proposed"), ("governance","coordinator","revision_rehearsed"),
        ("manager","coordinator","exact_consent_recorded"), ("coordinator","recovery","approved_execution_handoff"),
        ("coordinator","recovery","decision_requested"), ("recovery","coordinator","decision_proposed"),
        ("coordinator","reviewer","outcome_recorded")]
    check("exact_handoff_order", [(h["sender"],h["recipient"],h["kind"]) for h in handoffs] == expected_order)
    previous, last_time = None, float("-inf")
    for i, h in enumerate(handoffs,1):
        timestamp = datetime.fromisoformat(h["created_at"]).timestamp()
        check("handoff_%02d_chain_and_time" % i, h["sequence"] == i and h["previous_sha256"] == previous
            and h["sha256"] == sim.digest({k:v for k,v in h.items() if k != "sha256"})
            and math.isfinite(timestamp) and timestamp >= last_time)
        previous, last_time = h["sha256"], timestamp
    check("factory_handoff_binds_raw_and_compiled_inputs", handoffs[2]["payload"] == {
        "factory_proposal_sha256":sim.digest(raw), "factory_contract_version":team.FACTORY_VERSION,
        "challenge_sha256":sim.digest(challenge), "scenario":challenge["scenario"], "independent_baseline_passed":True,
        "delivery":"Validated Factory numbers compiled by the fixed coordinator mapping into a rehearsed challenge. Fresh local runtime delivers current state to governance. Factory narrative and baseline labels are not forwarded."})
    contexts, responses, token_rows, response_ids = {}, {}, [], []
    for role, call in zip(team.ROLES, summary["calls"]):
        req, resp = [read(root/"roles"/role/name) for name in ("request.json","response.json")]
        context, meta = req["context"], resp["metadata"]
        contexts[role], responses[role] = context, resp
        check(role+"_registered_prompt_and_settings", set(req) == {"role","system","context","request_settings","model_requested"}
            and req["role"] == role and req["system"] == team.PROMPTS[role]
            and req["model_requested"] == meta["model"] == factory.inference.MODEL
            and req["request_settings"] == factory.inference.inference_settings(10000,True))
        check(role+"_complete_live_response", call["live"] is True and call["status"] == "RECEIVED"
            and call["context_sha256"] == sim.digest(context) and call["metadata"] == meta
            and resp["content_truncated_for_storage"] is False and meta["finish_reason"] == "stop"
            and meta["refused"] is False and isinstance(meta["response_id"],str) and bool(meta["response_id"])
            and meta["source"] == "LIVE NEBIUS NEMOTRON "+role.upper()+" AGENT")
        check(role+"_token_arithmetic", all(type(meta[k]) is int and meta[k] >= 0
            for k in ("input_tokens","output_tokens","total_tokens"))
            and meta["input_tokens"]+meta["output_tokens"] == meta["total_tokens"] and meta["output_tokens"] <= 10000)
        requested = next(h for h in handoffs if h["kind"] == "decision_requested" and h["recipient"] == role)
        proposed = next(h for h in handoffs if h["kind"] == "decision_proposed" and h["sender"] == role)
        check(role+"_request_and_response_handoffs", requested["payload"] == {
            "context_sha256":sim.digest(context),"input":"roles/"+role+"/request.json","problem":team.ROLES[role][0]}
            and proposed["payload"] == {"response_sha256":sim.digest(resp["visible_content"]),"response":"roles/"+role+"/response.json"})
        parsed = (factory.decode(resp["visible_content"]) if role == "factory" else
            revision.validate_proposal(factory.decode(resp["visible_content"])) if role == "governance" else
            agent.parse_recovery(resp["visible_content"],context["based_on_state"]))
        check(role+"_visible_decision_matches", parsed == summary["decisions"][role])
        token_rows.append({"role":role,**{k:meta[k] for k in ("input_tokens","output_tokens","total_tokens")}})
        response_ids.append(meta["response_id"])
    check("three_distinct_role_instructions", len(set(team.PROMPTS.values())) == 3)
    check("three_distinct_recorded_response_ids", len(set(response_ids)) == 3)
    tokens = sum(x["total_tokens"] for x in token_rows)
    check("team_token_totals_complete", summary["usage"] == {"reported_tokens":tokens,"calls_with_usage":3,"missing_usage_calls":0})
    check("factory_context_and_decision_reproduce", contexts["factory"] == team.factory_context(summary["scenario"])
        and summary["decisions"]["factory"] == raw)
    for i,(phase,role) in enumerate((("repair","governance"),("recovery","recovery")),1):
        attempt = service/"attempts"/("%03d-"%i+phase)
        req = read(root/"roles"/role/"request.json")
        check(role+"_service_and_role_records_match", read(attempt/"context.json") == contexts[role]
            and read(attempt/"response.json") == responses[role]
            and read(attempt/"prompt.json") == {k:req[k] for k in ("system","request_settings","model_requested")})
    check("original_governance_and_recovery_decisions_match", summary["decisions"]["governance"] == report["proposal"]
        and summary["decisions"]["recovery"] == report["recovery_decision"])
    # The two service phases are independently checked against all database frames,
    # policy references, exact grants, original rehearsal and runtime receipts.
    result = audit_service(service)
    for key,value in result["checks"].items():
        check("service_"+key,value)
    expected_authority = "Model proposes only. Fresh exact manager consent is a separate terminal operation."
    check("governance_has_only_registered_observation_context", contexts["governance"].get("authority") == expected_authority
        and set(contexts["governance"]) == set(revision.context_for(snapshot_context(report)))|{"local_observation","authority"})
    events = report["events"]
    states = frames(events)
    unique = lambda kind: next(e for e in events if e["kind"] == kind)
    proposed, started, validated, approved, consent, failed = [unique(k) for k in (
        "REVISION_PROPOSED","SERVICE_AGENT_APPROVAL_PROBE_STARTED","SERVICE_AGENT_APPROVAL_PROBE_VALIDATED",
        "REVISION_APPROVED","REVISION_CONSENT","SERVICE_AGENT_WORKFLOW_FAILURE")]
    execution = next(e for e in events if e.get("action_key") == "execute-"+report["revision_id"])
    before = states[proposed["sequence"]-1]
    check("governance_original_capacity_and_block", before["stable_units"] == raw["stable_units"]
        and before["deployments"]["candidate-v2"]["allocated_units"] == raw["candidate_units"]
        and before["capacity_limit"] == raw["stable_units"]+raw["candidate_units"]+raw["headroom_units"]
        and report["original_request"]["args"]["units"] == raw["extra_units"]
        and report["original_request"]["result"]["status"] == "BLOCKED"
        and any(e.get("action_key") == report["original_request"]["action_key"] and e.get("result") == report["original_request"]["result"] for e in events))
    certificate = report["approval_probe"]
    cert_hash = sim.digest(certificate)
    check("post_review_protocol_and_certificate_identity", report["approval_freshness"] == "POST_REVIEW_PROBE"
        and certificate["schema_version"] == "agent-rehearsal-approval-probe-v1"
        and certificate["revision_id"] == started["revision_id"] == validated["revision_id"] == report["revision_id"]
        and certificate["controller_digest"] == report["controller_digest"]
        and certificate["proposal_digest"] == sim.digest(report["proposal"]) == started["proposal_digest"]
        and certificate["base_state_digest"] == sim.digest(before) == report["proposal"]["based_on_state"])
    check("certificate_is_persisted_in_journal", validated["certificate"] == certificate and validated["certificate_sha256"] == cert_hash)
    check("probe_precedes_grant_and_exact_execution", proposed["sequence"] < started["sequence"] < validated["sequence"]
        < approved["sequence"] < consent["sequence"] < execution["sequence"] < failed["sequence"])
    check("probe_grants_no_authority_or_capacity", started["changes"] == validated["changes"] == {}
        and states[started["sequence"]] == states[validated["sequence"]] == before)
    check("probe_binds_original_runtime_and_exact_action", certificate["runtime"] == report["initial_observation"]["runtime"]
        and certificate["exact_action"] == revision.action_for(report["proposal"],report["revision_id"],before)
        == approved["grant"]["exact_arguments"] == execution["args"])
    samples = certificate["samples"]
    check("one_hundred_post_review_samples", len(samples) == 100)
    latencies = [s["latency_ms"] for s in samples]
    check("fresh_latency_values_valid", all(type(v) in (int,float) and math.isfinite(v) and v >= 0 for v in latencies))
    check("fresh_samples_target_the_bound_candidate", all(s["target"] == s["body"]["target"] == "candidate-v2"
        and s["body"]["identity"] == certificate["runtime"]["workers"]["candidate-v2"]["identity"]
        and s["body"]["pid"] == certificate["runtime"]["workers"]["candidate-v2"]["pid"]
        and s["body"]["healthy"] is True and s["body"]["input"] == 7 for s in samples))
    metrics = {"samples":100, "correctness":sum(s.get("ok") is True and s.get("body",{}).get("result") == 49 for s in samples),
        "error_rate":sum(s.get("ok") is not True for s in samples), "p95_ms":sorted(latencies)[94],
        "observed_at":before["clock"], "observed_unix":certificate["started_unix"],
        "source":"100 REAL POST-REVIEW LOOPBACK HTTP REQUESTS; SEPARATE FROM ORIGINAL PROPOSAL STATE"}
    check("fresh_metrics_recomputed_from_samples", certificate["metrics"] == metrics)
    check("probe_batch_within_thirty_seconds", all(type(certificate[k]) in (int,float) and math.isfinite(certificate[k]) for k in ("started_unix","completed_unix"))
        and started["started_unix"] == certificate["started_unix"]
        and 0 <= certificate["completed_unix"]-certificate["started_unix"] <= 30)
    fresh = copy.deepcopy(before)
    fresh["deployments"]["candidate-v2"]["metrics"] = metrics
    validation_proposal = {**report["proposal"],"based_on_state":sim.digest(fresh)}
    check("isolated_validation_state_recomputed", sim.digest(fresh) == certificate["validation_state_digest"]
        and certificate["rehearsal"]["before"] == fresh
        and certificate["rehearsal"]["proposal_digest"] == sim.digest(validation_proposal))
    con = sqlite3.connect((service/"coordinator.sqlite3").resolve().as_uri()+"?mode=ro&immutable=1",uri=True)
    con.row_factory = sqlite3.Row
    receipts = [dict(row) for row in con.execute("SELECT * FROM action_receipts")]
    con.close()
    branch = snapshot(before,events[:started["sequence"]],receipts)
    try:
        revision.write_state_event(branch,before,fresh,"POST_REVIEW_HYPOTHETICAL_OBSERVATIONS")
        proof = revision.rehearse(branch,validation_proposal,report["revision_id"])
        check("fresh_rehearsal_independently_recomputed", proof == certificate["rehearsal"]
            and proof["passed"] is True and proof["reference_admission"] == "ELIGIBLE")
    finally:
        branch.close()
    check("canonical_metrics_not_replaced_by_probe", report["state"]["deployments"]["candidate-v2"]["metrics"]
        == before["deployments"]["candidate-v2"]["metrics"])
    revalidation = {"certificate_sha256":cert_hash,"metrics":metrics,"rehearsal_passed":True,
        "scope":"Fresh probe after human review; original state and exact model proposal unchanged."}
    check("team_revalidation_matches_certificate", summary["approval_revalidation"] == revalidation)
    check("recovery_received_fresh_certificate_summary", contexts["recovery"]["approval_revalidation"] == {
        "certificate_sha256":cert_hash,"metrics":metrics,"same_action_rehearsal_passed":True,
        "scope":"Post-review real probe validated the same approved action; original state retains its original measurement history."})
    check("exact_manager_and_rehearsal_handoffs", handoffs[5]["payload"] == {
        "revision_id":report["revision_id"],"proposal":report["proposal"],"rehearsal_passed":True}
        and handoffs[6]["payload"] == {"approved":True,"revision_id":report["revision_id"],
        "additional_units":report["proposal"]["additional_units"],"includes_bounded_recovery":True})
    check("manager_note_has_exact_units_and_recovery_scope", consent["note"] ==
        "Approve exactly %s additional units and bounded recovery in this local service-agent session." % report["proposal"]["additional_units"])
    check("confirmed_execution_and_pause_handoff", handoffs[7]["payload"] == {
        "execution":report["execution"],"grant":approved["grant"],"based_on_state":contexts["recovery"]["based_on_state"],
        "workflow_durably_paused":True})
    consent_time = datetime.fromisoformat(handoffs[6]["created_at"]).timestamp()
    execution_handoff_time = datetime.fromisoformat(handoffs[7]["created_at"]).timestamp()
    age_bound = execution_handoff_time-certificate["started_unix"]
    check("fresh_probe_after_consent_before_execution_handoff", consent_time <= certificate["started_unix"]
        <= certificate["completed_unix"] <= execution_handoff_time and 0 <= age_bound <= 30)
    expected_outcome = {"stage":report["stage"],"recovery_status":report["recovery"]["status"],
        "committed_units":report["committed_units"],"capacity_limit":report["capacity_limit"],
        "availability":report["availability"],"workflow_paused":"deploy-A" in report["state"]["paused_runs"],
        "harness_teardown":report["teardown"],"error":report.get("error")}
    check("team_outcome_matches_service_and_handoff", summary["outcome"] == expected_outcome == handoffs[-1]["payload"])
    check("all_recorded_team_flags_true", len(summary["checks"]) == 5 and all(v is True for v in summary["checks"].values()))
    expected_files = {"challenge.json","coordination.json","factory_baseline.json","factory_proposal.json","index.html","summary.json"}
    expected_files |= {"roles/"+role+"/"+name for role in team.ROLES for name in ("request.json","response.json")}
    expected_files |= {"service/"+name for name in ("agent_calls.json","challenge.json","coordinator.sqlite3","index.html",
        "local_program.json","report.json","runtime_events.json","source_baseline.json")}
    expected_files |= {"service/attempts/"+phase+"/"+name for phase in ("001-repair","002-recovery")
        for name in ("context.json","prompt.json","response.json")}
    check("exact_evidence_file_set", {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} == expected_files)
    return {"status":"PASSED","checks_passed":len(checks),"checks_total":len(checks),"checks":checks,
        "run_id":root.name,"team_version":summary["version"],"scenario":summary["scenario"],
        "platform_recorded":summary["platform"],"python_version_recorded":summary["python_version"],
        "team_implementation_digest":summary["implementation_digest"],"controller_digest":report["controller_digest"],
        "source_match":"CURRENT_SOURCE","role_requests":3,"recorded_model_calls":3,
        "reported_tokens":tokens,"missing_usage_calls":0,"per_role_usage":token_rows,
        "handoffs":len(handoffs),"journal_events":len(events),"service_recorded_check_flags":len(report["checks"]),
        "capacity_path":result["capacity_path"],"capacity_limit":report["capacity_limit"],
        "original_requested_units":raw["extra_units"],"approved_units":report["proposal"]["additional_units"],
        "candidate_units_preserved":report["state"]["deployments"]["candidate-v2"]["allocated_units"],
        "candidate_dependencies_at_outcome":report["state"]["deployments"]["candidate-v2"]["dependencies"],
        "final_routing":report["state"]["routing"],"route_owner":report["state"]["route_owner"],
        "route_version":report["state"]["route_version"],
        "runtime_candidate_percent":report["actual_at_outcome"]["candidate_percent"],
        "runtime_route_owner":report["actual_at_outcome"]["route_owner"],
        "candidate_process_stops_before_teardown":report["actual_at_outcome"]["candidate_stops"],
        "candidate_alive_at_policy_outcome":report["actual_at_outcome"]["workers"]["candidate-v2"]["alive"],
        "recovery_steps":report["recovery"]["steps"],"recovery_issues":report["recovery"].get("issues",[]),
        "recovery_status":report["recovery"]["status"],"workflow_paused":expected_outcome["workflow_paused"],
        "initial_raw_http_samples":100,"post_review_raw_http_samples":100,"post_review_metrics":metrics,
        "fresh_probe_duration_seconds":round(certificate["completed_unix"]-certificate["started_unix"],6),
        "recorded_execution_age_upper_bound_seconds":round(age_bound,6),
        "human_review_interval_seconds":round(consent_time-datetime.fromisoformat(handoffs[5]["created_at"]).timestamp(),6),
        "final_availability_aggregate":report["availability"],"separate_harness_teardown":report["teardown"],
        "provider_response_ids":response_ids}


def snapshot_context(report):
    # Reconstruct the observation view before the revision was proposed.
    class StateView:
        def state(self):
            return report["rehearsal"]["before"]
        def events(self):
            boundary = next(e["sequence"] for e in report["events"] if e["kind"] == "REVISION_PROPOSED")
            return report["events"][:boundary-1]
    return StateView()


def negative_checks(root,work):
    tests = [
        ("changed_capacity_claim","service/report.json",lambda r:r.update(after_execution_units=r["after_execution_units"]+1)),
        ("changed_factory_parameter","factory_proposal.json",lambda r:r.update(stable_units=r["stable_units"]-1)),
        ("changed_governance_context","roles/governance/request.json",lambda r:r["context"].update(authority="Self approve")),
        ("changed_role_prompt","roles/recovery/request.json",lambda r:r.update(system="An unrelated prompt")),
        ("changed_response_usage","roles/factory/response.json",lambda r:r["metadata"].update(total_tokens=0)),
        ("changed_manager_scope","summary.json",lambda r:r["handoffs"][6]["payload"].update(additional_units=9)),
        ("changed_raw_probe_result","service/report.json",lambda r:r["approval_probe"]["samples"][0]["body"].update(result=0)),
        ("changed_probe_action","service/report.json",lambda r:r["approval_probe"]["exact_action"].update(units=3)),
        ("changed_runtime_receipt","service/report.json",lambda r:next(iter(r["actual_at_outcome"]["receipts"].values())).update(fingerprint="0"*64)),
        ("changed_recovery_context","service/attempts/002-recovery/context.json",lambda r:r.update(workflow_failure_recorded=False)),
        ("changed_handoff_hash","coordination.json",lambda r:r[0].update(sha256="0"*64)),
    ]
    results = {}
    for name,filename,change in tests:
        target = work/name
        shutil.copytree(root,target)
        path = target/filename
        value = read(path)
        change(value)
        path.write_text(json.dumps(value),encoding="utf-8")
        try:
            audit_team(target)
        except EvidenceError as exc:
            results[name] = {"rejected":True,"failed_check":str(exc)}
        else:
            raise EvidenceError("Altered evidence was accepted: "+name)
    # A database-only change must fail even when all JSON summaries still agree.
    target = work/"changed_database_state"
    shutil.copytree(root,target)
    con = sqlite3.connect(target/"service/coordinator.sqlite3")
    state = json.loads(con.execute("SELECT payload FROM service_state WHERE id=1").fetchone()[0])
    state["capacity_limit"] += 1
    con.execute("UPDATE service_state SET payload=? WHERE id=1",(sim.canonical(state),))
    con.commit(); con.close()
    try:
        audit_team(target)
    except EvidenceError as exc:
        results["changed_database_state"] = {"rejected":True,"failed_check":str(exc)}
    else:
        raise EvidenceError("Altered database was accepted")
    for name,member in (("archive_traversal","../outside.txt"),("windows_archive_traversal","..\\outside.txt")):
        archive = work/(name+".zip")
        with zipfile.ZipFile(archive,"w") as z:
            z.writestr(member,"invalid")
        try:
            extract(archive,work/name)
        except EvidenceError as exc:
            results[name] = {"rejected":True,"failed_check":str(exc)}
        else:
            raise EvidenceError("Traversal archive was accepted")
    return results


def verify_archive(archive,self_check=False):
    archive = Path(archive).resolve()
    original_sha = digest(archive)
    with tempfile.TemporaryDirectory(prefix="team-evidence-audit-") as temp:
        service,members = extract(archive,Path(temp)/"received")
        root = service.parent
        result = audit_team(root)
        negatives = negative_checks(root,Path(temp)/"negative") if self_check else {}
    if digest(archive) != original_sha:
        raise EvidenceError("Input archive changed during audit")
    return {"audit_version":VERSION,"audited_at":datetime.now(timezone.utc).isoformat(),
        "archive_name":archive.name,"archive_sha256":original_sha,"archive_bytes":archive.stat().st_size,
        "archive_files":members,"audit_source_sha256":digest(__file__),"new_model_calls":0,
        "original_archive_unchanged":True,**result,"negative_checks":negatives,
        "negative_checks_passed":sum(x["rejected"] for x in negatives.values()),
        "limits":["Local record consistency against matching source; no independent provider, manager identity or Windows process attestation.",
        "One completed live %s scenario; not a broad model accuracy benchmark or Windows full-suite result." % result["scenario"],
        "100 initial and 100 fresh candidate HTTP samples are retained; final 100-request availability is aggregate only.",
        "Execution age is bounded by recorded enclosing handoff timestamps and journal order, not an independently timestamped execution receipt.",
        "Capacity, authority, actors, failure and workload are local fixtures; HTTP workers calculate arithmetic, not GPU inference.",
        "Protected work was retained at policy outcome before separate harness teardown; this is not persistent production supervision."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive",type=Path)
    parser.add_argument("--project",type=Path,default=project)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--self-check",action="store_true")
    args = parser.parse_args()
    try:
        result = verify_archive(args.archive,args.self_check)
    except (EvidenceError,ValueError,KeyError,OSError,sqlite3.Error,zipfile.BadZipFile) as exc:
        print("Evidence audit failed:",exc,file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print("Evidence: %s | %d/%d consistency checks | %d altered-input checks | %d recorded model calls | %d reported tokens | 0 new calls" %
        (result["status"],result["checks_passed"],result["checks_total"],result["negative_checks_passed"],result["recorded_model_calls"],result["reported_tokens"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

