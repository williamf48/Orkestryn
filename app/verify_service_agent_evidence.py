#!/usr/bin/env python3
"""Offline consistency audit of a completed, imported-challenge service-agent run.

Reads a bounded ZIP, never starts workers or an API client, and compares saved
claims with source contracts, the SQLite journal and recorded runtime receipts.
This is not provider attestation or a general agent accuracy benchmark.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import challenge_factory as factory
import conflict_explorer as explorer
import conflict_lab as reference
import plan_revision as revision
import service_agent as agent
import simulator as sim
from verify_live_evidence import EvidenceError, frames, require, same

VERSION = "agent-rehearsal-service-agent-evidence-audit-v2"
HISTORICAL_SOURCE_SHA256 = "474e1ad646873934462eb73bb593da79b24adc5fcfd50a1bd4b1533ed5e21f5a"
HISTORICAL_CONTROLLER = "803e3134c1b9c0be2da21f9e97e8ab0e513a35bef25f4a1185dc324cb6a6a786"
PINNED_FACTORY_PUBLICATION = "16a944a3e3b234d44a21123a4fcf517a35d4937ff92f61160eb48307e9b7cecc"


# Exact presentation-only rebrand, with the previous contracts preserved.
REBRAND_PRESENTATION_SHA256 = {'simulator.py': {'before': 'cad80ed20445cae1b235d1626de45ad77bf19230c05adb5f0b192565af1225d5', 'after': 'd2c169160fdb649979a55e4bac7113550e18cc10305b2f366de5dfe4af1423a0'}, 'restart_checker.py': {'before': 'f3d63d0c9e09bac63de6be60493bf317fa71c189516079263b91307ac6f35ece', 'after': 'c97ed55414ef1ca336ad0e44ea75fada30e3c6f84cb60ab24f7862dfb649df59'}, 'conflict_lab.py': {'before': '571bfe5c0c3f0b7dc597f9562085a30a8cac5e7225f2b4a5f31498ae2590a4a8', 'after': '7263c52003d4112c2f905a2d2765d8bce7b4715ff6a2945046148dd0de0144ac'}, 'conflict_explorer.py': {'before': 'e50dc8c4ae82d435ee237ac8e6f5832283207592f912f0c6f8b5c8793c83d055', 'after': 'c166ef015993aa6bff725af4587290507e1b7d6070b45b451c0b4f7d1291d910'}, 'challenge_factory.py': {'before': '07453cb9e1526c4021c928437e0cd8c31c098e38945f1ecd618850e8ac12e930', 'after': '68fa835a64001b42cf357d8d2ac3be6b556b31feef2466b6f46bc03f10f01212'}, 'plan_revision.py': {'before': '992b3f59db8d3472fc9b4b61e9010b98cf48971ef96b7377d92daffd6291af90', 'after': '73ad162cda6f932fecfaa98b89e68eca393a3e5deac8bea77409b05bae590993'}, 'case_workflow.py': {'before': '1780ae29e347dacfdc0b4edcf44f05070be9e9585694af44fbe11b38ab91fc37', 'after': 'dd3a544423a182e5162d6ca9da4d09018d809adbc15d1567e4af8f18732b6bfb'}, 'local_service.py': {'before': '2e47dba8773e51d6798e6d392b9b5b9be25662ae5e6faf2571f81df23923f407', 'after': '6d186fadb9fa9e3e859871256317ca5c8da28d7b21030ac1e555e3d6c44a7e69'}}


def historical_policy_binding(value, names):
    """Accept archived bytes or exact reviewed presentation/publication edits.

    Do not normalize whitespace, arbitrary strings or executable statements. The
    complete normalized files must still match the pinned historical snapshot.
    """
    current = {name:(agent.ROOT/name).read_bytes() for name in (*names, 'local_service.py')}
    current_hashes = {name:hashlib.sha256(data).hexdigest() for name,data in current.items()}
    if sim.digest({name:current_hashes[name] for name in names}) != revision.implementation_digest():
        raise EvidenceError('Current policy source binding changed.')
    normalized = dict(current)
    edits = []
    for name, pins in REBRAND_PRESENTATION_SHA256.items():
        data = normalized[name]
        if hashlib.sha256(data).hexdigest() == pins['after']:
            prior = data.replace(b'Orkestryn', b'Agent Rehearsal').replace(b'ORKESTRYN', b'AGENT REHEARSAL')
            if name == 'plan_revision.py':
                prior = prior.replace(b'in your project folder in PowerShell', b'in your AgentRehearsal PowerShell folder')
            if hashlib.sha256(prior).hexdigest() != pins['before']:
                raise EvidenceError('Pinned presentation mapping changed.')
            normalized[name] = prior
            edits.append('Orkestryn presentation: ' + name)
    old = '</style><div class="eyebrow">AGENT REHEARSAL · CASES 6 + 9 + 12</div><h1>Conflict Lab</h1>'.encode('utf-8')
    new = '</style><div class="eyebrow">AGENT REHEARSAL · COORDINATION ANALYSIS</div><h1>Conflict Lab</h1>'.encode('utf-8')
    if normalized['conflict_lab.py'].count(new) == 1:
        normalized['conflict_lab.py'] = normalized['conflict_lab.py'].replace(new, old, 1)
        edits.append('Conflict Lab display label')
    # Pin the complete replacement function, then compare every other byte.
    # An unknown writer or any policy edit remains ineligible for this bridge.
    data = normalized['challenge_factory.py']
    start = data.find(b'def write_json(path, value):\n')
    end = data.find(b'\n\ndef append(path, value):', start)
    if start >= 0 and end > start and hashlib.sha256(data[start:end]).hexdigest() == PINNED_FACTORY_PUBLICATION:
        archived = value['sources']['challenge_factory.py'].encode('utf-8')
        a = archived.index(b'def write_json(path, value):\n')
        b = archived.index(b'\n\ndef append(path, value):', a)
        normalized['challenge_factory.py'] = data[:start] + archived[a:b] + data[end:]
        edits.append('Bounded atomic JSON publication')
    if any(hashlib.sha256(data).hexdigest() != value['source_sha256'][name] for name,data in normalized.items()):
        raise EvidenceError('Historical policy/adapter contracts differ beyond the pinned non-policy edits.')
    return {'mode':'EXACT_ARCHIVED_CONTRACTS' if normalized==current else 'PINNED_NON_POLICY_EDITS',
            'reviewed_edits':edits,
            'current_source_sha256':current_hashes,
            'scope':'Complete normalized policy/adapter bytes checked. Only exact digest-pinned presentation edits and the registered publisher function may differ; not execution attestation.'}


def controller_source_match(expected, snapshot_path=None):
    if snapshot_path is None and expected == agent.controller_digest():
        return {"mode":"CURRENT_SOURCE", "controller_digest":expected}
    path = Path(snapshot_path) if snapshot_path is not None else Path(__file__).resolve().parent/"controller_source_v1.json"
    if digest(path) != HISTORICAL_SOURCE_SHA256:
        raise EvidenceError("Historical controller source snapshot is missing or altered.")
    value = read(path)
    names = ("plan_revision.py","case_workflow.py","challenge_factory.py","simulator.py","conflict_lab.py","conflict_explorer.py","restart_checker.py")
    required = set(names)|{"local_service.py","service_agent.py"}
    if set(value["sources"]) != required:
        raise EvidenceError("Historical source snapshot has different dependencies.")
    hashes = {name:hashlib.sha256(source.encode("utf-8")).hexdigest() for name,source in value["sources"].items()}
    revision_digest = sim.digest({name:hashes[name] for name in names})
    computed = sim.digest({"revision_controller":revision_digest,"local_service.py":hashes["local_service.py"],"service_agent.py":hashes["service_agent.py"]})
    if (hashes != value["source_sha256"] or revision_digest != value["revision_controller_digest"]
            or computed != value["controller_digest"] or computed != expected or expected != HISTORICAL_CONTROLLER):
        raise EvidenceError("Recorded controller does not match the pinned historical source and unchanged policy/adapter contracts.")
    binding = historical_policy_binding(value, names)
    return {"mode":"PINNED_HISTORICAL_SOURCE", "controller_digest":computed,"snapshot":"controller_source_v1.json",
        "snapshot_sha256":HISTORICAL_SOURCE_SHA256,"source_files":len(hashes),
        "policy_binding":binding,
        "scope":"Source-consistent historical record. Not a claim that it ran on the current controller or an execution attestation."}


class EvidenceSourceTests(unittest.TestCase):
    def test_historical_controller_is_reconstructed_from_exact_source_bytes(self):
        with mock.patch.object(agent,"controller_digest",return_value="different-current"):
            matched = controller_source_match(HISTORICAL_CONTROLLER)
        self.assertEqual(matched["mode"],"PINNED_HISTORICAL_SOURCE")
        self.assertEqual(matched["source_files"],9)

    def test_altered_source_snapshot_and_unknown_controller_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"source.json"
            path.write_bytes((Path(__file__).resolve().parent/"controller_source_v1.json").read_bytes()+b" ")
            with self.assertRaises(EvidenceError):
                controller_source_match(HISTORICAL_CONTROLLER,path)
        with self.assertRaises(EvidenceError):
            controller_source_match("0"*64)

    def test_historical_snapshot_cannot_hide_changed_policy_contracts(self):
        with mock.patch.object(agent,"controller_digest",return_value="different-current"), mock.patch.object(revision,"implementation_digest",return_value="changed-policy"):
            with self.assertRaises(EvidenceError):
                controller_source_match(HISTORICAL_CONTROLLER)

    def test_display_bridge_rejects_any_other_source_edit(self):
        original=Path.read_bytes
        def changed(path):
            data=original(path)
            return data+b'\n# unexpected source edit\n' if path==agent.ROOT/'conflict_lab.py' else data
        with mock.patch.object(Path,'read_bytes',changed), mock.patch.object(agent,'controller_digest',return_value='different-current'):
            with self.assertRaisesRegex(EvidenceError,'beyond the pinned non-policy'):
                controller_source_match(HISTORICAL_CONTROLLER)

    def test_original_archived_policy_bytes_remain_accepted(self):
        original=Path.read_bytes
        sources=read(agent.ROOT/'controller_source_v1.json')['sources']
        def unchanged(path):
            if path.parent==agent.ROOT and path.name in sources:
                return sources[path.name].encode('utf-8')
            return original(path)
        with mock.patch.object(Path,'read_bytes',unchanged), mock.patch.object(agent,'controller_digest',return_value='different-current'):
            matched=controller_source_match(HISTORICAL_CONTROLLER)
        self.assertEqual(matched['policy_binding']['mode'],'EXACT_ARCHIVED_CONTRACTS')

    def test_publication_bridge_rejects_an_unregistered_writer_change(self):
        original=Path.read_bytes
        def changed(path):
            data=original(path)
            return data.replace(b'def write_json(path, value):\n',b'def write_json(path, value):\n    # unregistered edit\n',1) if path==agent.ROOT/'challenge_factory.py' else data
        with mock.patch.object(Path,'read_bytes',changed), mock.patch.object(agent,'controller_digest',return_value='different-current'):
            with self.assertRaisesRegex(EvidenceError,'beyond the pinned non-policy'):
                controller_source_match(HISTORICAL_CONTROLLER)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(archive, destination):
    members, seen = {}, set()
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 500 or sum(x.file_size for x in z.infolist()) > 64 * 1024 * 1024:
            raise EvidenceError("Archive exceeds audit bounds.")
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            rel = PurePosixPath(name)
            if (not rel.parts or rel.is_absolute() or ".." in rel.parts or ":" in name
                    or stat.S_ISLNK(info.external_attr >> 16) or str(rel).casefold() in seen):
                raise EvidenceError("Unsafe or duplicate archive member.")
            seen.add(str(rel).casefold())
            target = destination.joinpath(*rel.parts)
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                data = z.read(info)  # also checks CRC
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                members[str(rel)] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    reports = list(destination.rglob("report.json"))
    if len(reports) != 1:
        raise EvidenceError("Supply one completed live run, not a lab or several runs.")
    return reports[0].parent, members


def snapshot(state, events, receipts):
    engine = sim.Engine(":memory:")
    engine.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(state),))
    for event in events:
        engine.db.execute("INSERT INTO events(sequence,event) VALUES (?,?)", (event["sequence"], sim.canonical(event)))
    keys = {event["action_key"] for event in events if event["kind"] == "ACTION"}
    for row in receipts:
        if row["action_key"] in keys:
            engine.db.execute("INSERT INTO action_receipts VALUES (?,?,?)",
                              (row["action_key"], row["fingerprint"], row["result"]))
    return engine


def audit_directory(root):
    checks = {}
    check = lambda name, value: require(checks, name, value)
    report = read(root / "report.json")
    calls = read(root / "agent_calls.json")
    runtime_events = read(root / "runtime_events.json")
    challenge = factory.validate_proposal(read(root / "challenge.json"))
    check("registered_agent_version", report["version"] == agent.VERSION)
    check("completed_two_call_live_run", report["stage"] == "RECOVERED" and report["model_calls"] == 2
          and report["scripted_consent"] is False and len(calls) == 2)
    source_match = controller_source_match(report["controller_digest"])
    check("controller_and_adapter_source_match", source_match["controller_digest"] == report["controller_digest"])
    check("attempt_ledger_matches_report", calls == report["attempts"])
    check("challenge_digest_matches", sim.digest(challenge) == report["source_challenge_digest"])
    check("original_file_hashes_match", report["input_source"]["sha256_at_start"] == report["input_source"]["sha256_after_run"]
          == digest(root / "challenge.json"))
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
    check("exact_runtime_and_controller_binding", binding == {"controller_digest": report["controller_digest"], "runtime": report["initial_observation"], "context_state_digest": proposal["based_on_state"]}
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


def negative_checks(root, work):
    tests = [
        ("altered_capacity", "report.json", lambda r: r.update(after_execution_units=r["after_execution_units"] + 1)),
        ("altered_model_context", "attempts/002-recovery/context.json", lambda c: c.update(workflow_failure_recorded=False)),
        ("altered_manager_result", "report.json", lambda r: r["approval"].update(approved_units=19)),
        ("altered_runtime_receipt", "report.json", lambda r: next(iter(r["actual_at_outcome"]["receipts"].values())).update(fingerprint="0" * 64)),
        ("altered_phase_prompt", "attempts/002-recovery/prompt.json", lambda p: p.update(system="An unrelated prompt")),
    ]
    results = {}
    for name, filename, change in tests:
        directory = work / name
        shutil.copytree(root, directory)
        path = directory / filename
        value = read(path)
        change(value)
        path.write_text(json.dumps(value), encoding="utf-8")
        try:
            audit_directory(directory)
        except EvidenceError as exc:
            results[name] = {"rejected": True, "failed_check": str(exc)}
        else:
            raise EvidenceError("Altered input was accepted: " + name)
    archive = work / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../outside.txt", "invalid")
    try:
        extract(archive, work / "unsafe")
    except EvidenceError as exc:
        results["archive_traversal"] = {"rejected": True, "failed_check": str(exc)}
    else:
        raise EvidenceError("Traversal archive was accepted")
    return results


def verify_archive(archive, self_check=False):
    archive = Path(archive).resolve()
    before = digest(archive)
    with tempfile.TemporaryDirectory(prefix="service-agent-evidence-") as temp:
        root, members = extract(archive, Path(temp) / "received")
        result = audit_directory(root)
        negatives = negative_checks(root, Path(temp) / "negative") if self_check else None
    if digest(archive) != before:
        raise EvidenceError("Input archive changed during audit.")
    return {"audit_version": VERSION, "audited_at": datetime.now(timezone.utc).isoformat(),
            "archive_name": archive.name, "archive_sha256": before, "archive_bytes": archive.stat().st_size,
            "archive_members": members, "audit_source_sha256": digest(__file__), "new_model_calls": 0,
            "original_archive_unchanged": True, **result, "negative_checks": negatives,
            "limits": ["Recorded evidence consistency, not independent provider or Windows process attestation.",
                       "One completed imported-challenge run; no broader accuracy or all-Windows-tests claim.",
                       "100 initial HTTP samples are retained individually; final availability is an aggregate only.",
                       "No timestamped approval/execution boundary exists for an independent 30-second age calculation.",
                       "Capacity, actors, manager identity and failure injection remain local fixtures.",
                       "Harness teardown is separate; the supervisor is not persistent across sessions."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--self-check", action="store_true", help="Also require six altered-input cases to be rejected.")
    args = parser.parse_args()
    if (args.output.resolve() == args.archive.resolve()
            or (args.output.exists() and args.archive.exists() and args.output.samefile(args.archive))):
        parser.error("The audit output must be separate from the input archive.")
    try:
        result = verify_archive(args.archive, args.self_check)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print("EVIDENCE AUDIT FAILED: " + type(exc).__name__ + ": " + str(exc))
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Evidence: %s | %d/%d consistency checks | %d recorded calls | %d reported tokens | 0 new calls" %
          (result["status"], result["checks_passed"], result["checks_total"], result["model_calls"], result["reported_tokens"]))
    print("Audit record: " + str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
