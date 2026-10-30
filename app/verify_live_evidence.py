"""Offline audit of a completed positive-repair Case Workflow campaign ZIP.

Reads captured responses and SQLite journals in a temporary extraction. No SDK,
network request, new consent, or action against the user's original run is used.
This narrow audit requires the matching controller source beside this script.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import stat
import tempfile
from datetime import datetime, timezone
import zipfile

import case_workflow as workflow
import challenge_factory as factory
import conflict_explorer as explorer
import conflict_lab as reference
import coverage_planner as coverage
import plan_revision as revision
import project
import simulator as sim

VERSION = "agent-rehearsal-live-evidence-audit-v1"


class EvidenceError(ValueError):
    pass


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(checks, name, condition):
    checks[name] = bool(condition)
    if not condition:
        raise EvidenceError(name)


def same(a, b):
    return sim.canonical(a) == sim.canonical(b)


def extract(archive, destination):
    """Normalize Windows ZIP names without permitting traversal or collisions."""
    members, seen = {}, set()
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 500 or sum(i.file_size for i in z.infolist()) > 64 * 1024 * 1024:
            raise EvidenceError("Archive exceeds the bounded campaign audit size.")
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            rel = PurePosixPath(name)
            if (not rel.parts or rel.is_absolute() or ".." in rel.parts or ":" in name
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise EvidenceError("Unsafe archive member.")
            normalized = str(rel)
            if normalized.casefold() in seen:
                raise EvidenceError("Duplicate normalized archive member.")
            seen.add(normalized.casefold())
            target = Path(destination).joinpath(*rel.parts)
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            data = z.read(info)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            members[normalized] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    campaigns = list(Path(destination).glob("*/campaign.json"))
    if len(campaigns) != 1:
        raise EvidenceError("Expected exactly one campaign folder.")
    return campaigns[0].parent, members


def frames(events):
    state, result = sim.initial_state(), [sim.initial_state()]
    for index, event in enumerate(events, 1):
        if event["sequence"] != index:
            raise EvidenceError("Journal sequence is not contiguous.")
        for key, change in event.get("changes", {}).items():
            if "before" in change and not same(state.get(key), change["before"]):
                raise EvidenceError("Journal before-image does not match its preceding frame.")
            state[key] = copy.deepcopy(change["after"])
        if event["kind"] == "RECOVERY_STARTED" and event["run_id"] not in state["paused_runs"]:
            state["paused_runs"].append(event["run_id"])
        result.append(copy.deepcopy(state))
    return result


def audit_case(directory, accepted_proposal, work, checks):
    name = directory.name
    local = {}
    meta = revision.session_metadata(directory)
    baseline, handoff, report = [read(directory / filename) for filename in ("baseline.json", "handoff.json", "report.json")]
    challenge = read(directory / "challenge.json")
    require(local, "challenge_matches_accepted_factory_output", same(challenge, accepted_proposal))
    reproduced = factory.rehearse(challenge)
    require(local, "entire_baseline_reproduces_exactly", same(reproduced, baseline) and reproduced["passed"])
    replay_path = work / (name + "-replay")
    workflow.prepare_case(replay_path, challenge)
    require(local, "handoff_reproduces_exactly", same(read(replay_path / "handoff.json"), handoff))
    require(local, "source_hash_bindings_match", all(digest(directory / filename) == meta["source_case"][key]
        for filename, key in (("challenge.json", "proposal_sha256"), ("baseline.json", "baseline_sha256"), ("handoff.json", "handoff_sha256"))))
    e = revision.RevisionEngine(directory / "coordinator.sqlite3", create=False)
    replay = revision.RevisionEngine(replay_path / "coordinator.sqlite3", create=False)
    usages = []
    contexts = []
    attempt_results = []
    try:
        require(local, "sqlite_integrity_ok", e.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
        events, state = e.events(), e.state()
        records = [e.proposal_record(row[0]) for row in e.db.execute("SELECT revision_id FROM revisions ORDER BY rowid")]
        require(local, "database_matches_saved_report", same(state, report["state"]) and same(events, report["events"])
                and same(records, report["revisions"]) and same(meta, report["meta"]))
        require(local, "original_prefix_preserved", same(events[:handoff["event_prefix_length"]], baseline["parent_events"])
                and sim.digest(events[:handoff["event_prefix_length"]]) == handoff["event_prefix_sha256"])
        journal = explorer.audit(e)
        require(local, "journal_and_receipts_rechecked", all(journal.values()) and journal == report["journal_audit"])
        history = frames(events)
        require(local, "baseline_frame_and_final_state_match", same(history[handoff["event_prefix_length"]], baseline["decision_state"])
                and same(history[-1], state))
        require(local, "one_positive_executed_revision", len(records) == 1 and records[0]["status"] == "EXECUTED")
        record = records[0]
        proposal, grant = record["proposal"], record["grant"]
        revision_id = record["revision_id"]
        require(local, "controller_implementation_matches", record["implementation_digest"] == revision.implementation_digest())
        require(local, "proposal_state_and_digest_match", record["base_state_digest"] == handoff["state_sha256"]
                and record["proposal_digest"] == sim.digest(proposal) and proposal["based_on_state"] == handoff["state_sha256"])
        require(local, "latest_attempt_identifies_saved_revision", meta["proposal_id"] == revision_id)
        attempts = sorted((directory / "attempts").glob("attempt-*"))
        require(local, "all_paid_revision_attempts_retained", len(attempts) == len(meta["proposal_attempts"]) == meta["model_calls"])
        for number, (attempt_path, attempt_meta) in enumerate(zip(attempts, meta["proposal_attempts"]), 1):
            context, prompt, response = [read(attempt_path / filename) for filename in ("context.json", "prompt.json", "response.json")]
            require(local, "attempt_%s_context_is_original_decision_state" % number, same(context, revision.context_for(replay)))
            require(local, "attempt_%s_registered_prompt_and_schema" % number, prompt["system"] == revision.SYSTEM
                    and same(prompt["schema"], revision.SCHEMA) and prompt["version"] == revision.PROMPT_VERSION
                    and prompt["model_requested"] == response["metadata"]["model"])
            metadata = response["metadata"]
            require(local, "attempt_%s_response_metadata_retained" % number, same(metadata, attempt_meta["generation"]["metadata"])
                    and not response["content_truncated_for_storage"] and metadata["finish_reason"] == "stop" and not metadata["refused"])
            try:
                parsed = revision.validate_proposal(factory.decode(response["visible_content"]))
            except factory.ProposalError as exc:
                require(local, "attempt_%s_invalid_response_not_admitted" % number,
                        attempt_meta["generation"]["status"] == "INVALID_PROPOSAL"
                        and attempt_meta["generation"]["reason"] == str(exc)
                        and not (attempt_path / "proposal.json").exists() and "proposal_id" not in attempt_meta)
                attempt_results.append({"attempt": number, "status": "INVALID_PROPOSAL", "reason": str(exc),
                                        "missing_fields": sorted(set(revision.SCHEMA["required"]) - set(factory.decode(response["visible_content"])))})
            else:
                require(local, "attempt_%s_valid_proposal_is_exact_response" % number, same(parsed, read(attempt_path / "proposal.json"))
                        and same(parsed, proposal) and attempt_meta["proposal_id"] == revision_id
                        and attempt_meta["proposal_status"] == "AWAITING_APPROVAL")
                attempt_results.append({"attempt": number, "status": "AWAITING_APPROVAL", "additional_units": parsed["additional_units"]})
            usages.append(metadata)
            contexts.append(context)
        require(local, "independent_rehearsal_reproduces_exactly", same(revision.rehearse(replay, proposal, revision_id), record["rehearsal"])
                and record["rehearsal"]["passed"] and record["rehearsal"]["reference_admission"] == "ELIGIBLE")
        after_prefix = events[handoff["event_prefix_length"]:]
        proposed = [x for x in after_prefix if x["kind"] == "REVISION_PROPOSED"]
        approvals = [x for x in after_prefix if x["kind"] == "REVISION_APPROVED"]
        consents = [x for x in after_prefix if x["kind"] == "REVISION_CONSENT"]
        executions = [x for x in after_prefix if x.get("action_key") == "execute-" + revision_id]
        recoveries = [x for x in after_prefix if x["kind"] == "RECOVERY_STARTED"]
        recovery_reports = [x for x in after_prefix if x["kind"] == "RECOVERY_REPORT"]
        require(local, "one_proposal_consent_execution_recovery", all(len(group) == 1 for group in (proposed, approvals, consents, executions, recoveries, recovery_reports)))
        proposed_event, approval, consent, execution, recovery_start, recovery_report = [group[0] for group in
                (proposed, approvals, consents, executions, recoveries, recovery_reports)]
        require(local, "proposal_has_no_authority_or_state_effect", proposed_event["status"] == "AWAITING_APPROVAL"
                and not proposed_event["changes"] and same(history[proposed_event["sequence"]], baseline["decision_state"]))
        require(local, "explicit_manager_consent_precedes_execution", consent["actor"] == "MANAGER"
                and consent["source"] == "EXPLICIT LOCAL MANAGER FIXTURE CONSENT" and bool(consent["note"].strip())
                and consent["result"]["status"] == "SUCCEEDED" and consent["result"]["approved_units"] == proposal["additional_units"]
                and proposed_event["sequence"] < approval["sequence"] < consent["sequence"] < execution["sequence"] < recovery_start["sequence"])
        expected_approved, expected_grant = revision.authorized_state(baseline["decision_state"], proposal, revision_id, consent["source"], consent["note"])
        require(local, "exact_grant_and_approved_state_match", same(grant, expected_grant) and same(approval["grant"], grant)
                and same(history[consent["sequence"]], expected_approved))
        decisions = e.db.execute("SELECT decision_key,fingerprint,result FROM revision_decisions").fetchall()
        consent_request = {key: consent[key] for key in ("revision_id", "note", "actor", "source")}
        require(local, "manager_receipt_matches_journal", len(decisions) == 1 and decisions[0][0] == consent["decision_key"]
                and decisions[0][1] == sim.digest(consent_request) and same(json.loads(decisions[0][2]), consent["result"]))
        before = baseline["decision_state"]
        require(local, "approval_does_not_reserve_capacity", reference.commitments(expected_approved) == reference.commitments(before)
                and expected_approved["reservations"] == before["reservations"])
        require(local, "plan_bound_not_expanded", expected_approved["plans"]["deploy-A"]["max_units"] <= before["plans"]["deploy-A"]["max_units"])
        reserved = history[execution["sequence"]]
        expected_reserved = copy.deepcopy(expected_approved)
        reservation_id = grant["exact_arguments"]["reservation_id"]
        expected_reserved["reservations"][reservation_id] = {"units": proposal["additional_units"], "owner_run": "deploy-A", "status": "PENDING"}
        require(local, "execution_is_only_exact_authorized_reservation", execution["result"]["status"] == "SUCCEEDED"
                and execution["tool"] == grant["exact_tool"] and execution["args"] == grant["exact_arguments"]
                and execution["approval_id"] == grant["approval_id"] and same(reserved, expected_reserved))
        require(local, "exact_capacity_arithmetic", reference.commitments(reserved) == reference.commitments(before) + proposal["additional_units"]
                and reference.commitments(reserved) <= before["capacity_limit"])
        pre_recovery = history[recovery_start["sequence"] - 1]
        expected_recovery = reference.reference_recovery(pre_recovery)
        assessment = reference.assess(pre_recovery, state, expected_recovery)
        for key, value in assessment["checks"].items():
            require(local, "recovery_" + key, value)
        require(local, "recovery_matches_independent_reference", recovery_report["report"]["status"] == expected_recovery["recovery_status"]
                and reference.commitments(state) == expected_recovery["expected_committed"] and assessment["eligible_units_retained"] == 0)
        require(local, "exact_reservation_released_once", state["reservations"][reservation_id]["status"] == "RELEASED"
                and len([x for x in after_prefix if x.get("tool") == "release_reservation"
                         and x.get("args", {}).get("reservation_id") == reservation_id and x["result"]["status"] == "SUCCEEDED"]) == 1)
        require(local, "saved_report_usage_matches_responses", report["usage"]["known_total_tokens"] == sum(x["total_tokens"] for x in usages)
                and report["usage"]["calls_with_usage"] == len(usages) and report["usage"]["missing_usage_calls"] == 0)
        result = {"case": name, "scenario": challenge["scenario"], "fault": challenge["fault"],
            "original_requested_units": report["original_request"]["args"]["units"], "original_denial": report["original_request"]["result"]["code"],
            "revision_id": revision_id, "revision_units": proposal["additional_units"], "committed_before": reference.commitments(before),
            "committed_after_execution": reference.commitments(reserved), "committed_after_recovery": reference.commitments(state),
            "capacity_limit": state["capacity_limit"], "route_owner": state["route_owner"],
            "recovery_status": recovery_report["report"]["status"], "recovery_issues": recovery_report["report"]["issues"],
            "coverage": coverage.classify(baseline), "model_calls": len(usages), "reported_tokens": sum(x["total_tokens"] for x in usages),
            "attempts": attempt_results, "implementation_digest": record["implementation_digest"],
            "plan_bound_before": before["plans"]["deploy-A"]["max_units"], "plan_bound_approved": expected_approved["plans"]["deploy-A"]["max_units"],
            "journal_audit": journal, "independent_recovery_reference": expected_recovery, "checks": local}
    finally:
        e.close()
        replay.close()
    checks.update({name + ":" + key: value for key, value in local.items()})
    return result, usages, contexts


def verify_archive(archive):
    archive = Path(archive).resolve()
    archive_hash = digest(archive)
    checks = {}
    with tempfile.TemporaryDirectory(prefix="agent-rehearsal-evidence-") as temporary:
        work = Path(temporary)
        run, members = extract(archive, work / "received")
        config, saved, ledger = [read(run / name) for name in ("campaign.json", "summary.json", "calls.json")]
        require(checks, "supported_model_generated_campaign", config["version"] == workflow.VERSION
                and config["source"]["kind"] == "MODEL_GENERATION" and config["status"] == "COMPLETED")
        factory_run = run / PurePosixPath(config["factory_report"]).parent
        requests, responses, rows, accepted = [lines(factory_run / name) for name in
                ("requests.jsonl", "responses.jsonl", "proposals.jsonl", "accepted_proposals.jsonl")]
        factory_summary = read(factory_run / "summary.json")
        prompt = read(factory_run / "prompt.json")
        require(checks, "registered_generation_prompt_and_schema", prompt["system"] == workflow.SYSTEM
                and same(prompt["schema"], factory.SCHEMA) and prompt["version"] == factory.PROMPT_VERSION
                and prompt["model_requested"] == factory_summary["model_requested"])
        generation_calls = [c for c in ledger["calls"] if c["stage"] == "challenge_generation"]
        require(checks, "generation_requests_responses_and_ledger_count_match", len(requests) == len(responses) == len(rows) == len(generation_calls))
        for i, (request, response, call) in enumerate(zip(requests, responses, generation_calls), 1):
            require(checks, "generation_%s_context_and_response_metadata_bound_to_ledger" % i,
                    request["attempt"] == response["attempt"] == i and sim.digest(request["context"]) == call["context_sha256"]
                    and same(response["metadata"], call["metadata"]) and same(call["request_settings"], prompt["request_settings"])
                    and not response["content_truncated_for_storage"])

        class CapturedProvider:
            live = True  # Reproduce the original bounded validation path; ask never contacts an endpoint.
            mode = factory_summary["mode"]
            request_settings = prompt["request_settings"]
            system_prompt = prompt["system"]
            index = 0

            def ask(self, context):
                index = self.index
                self.index += 1
                require(checks, "generation_%s_context_reproduces_from_prior_feedback" % (index + 1), same(context, requests[index]["context"]))
                return responses[index]["visible_content"], responses[index]["metadata"]

        provider = CapturedProvider()
        replay_dir, replay_summary = factory.run_batch(provider, work / "factory_replay", len(responses), progress=False,
                                                     policy=coverage.CoveragePlan(factory_summary["coverage"]["mode"]))
        for filename in ("proposals.jsonl", "accepted_proposals.jsonl", "challenges.jsonl", "answers.jsonl", "requests.jsonl", "responses.jsonl"):
            require(checks, "factory_" + filename + "_reproduces_exactly", same(lines(replay_dir / filename), lines(factory_run / filename)))
        for key in ("counts", "usage", "coverage", "reference_passes", "reference_checks_passed", "challenges_exported"):
            require(checks, "factory_summary_" + key + "_reproduces", same(replay_summary[key], factory_summary[key]))
        require(checks, "coverage_snapshot_matches_generation", same(read(factory_run / "coverage.json"), replay_summary["coverage"])
                and same(config["generation"], factory_summary))
        require(checks, "all_accepted_factory_cases_connected", len(config["cases"]) == len(accepted))
        cases, revision_metadata, case_contexts = [], [], []
        for item, proposal in zip(config["cases"], accepted):
            name = item["directory"]
            if Path(name).name != name or not name.startswith("case-") or "\\" in name:
                raise EvidenceError("Invalid case folder.")
            result, usages, contexts = audit_case(run / name, proposal, work, checks)
            cases.append(result)
            revision_metadata.extend(usages)
            case_contexts.append((name, contexts[0], usages[0]))
        revision_calls = [c for c in ledger["calls"] if c["stage"] == "plan_revision"]
        require(checks, "original_revision_calls_bound_to_context_and_metadata", len(revision_calls) == len(case_contexts)
                and all(call["context_sha256"] == sim.digest(context) and same(call["metadata"], metadata)
                        for call, (_, context, metadata) in zip(revision_calls, case_contexts)))
        require(checks, "original_campaign_ceiling_respected", len(ledger["calls"]) <= ledger["limit"] == config["call_limit"]
                and [c["call"] for c in ledger["calls"]] == list(range(1, len(ledger["calls"]) + 1)))
        all_usage = [response["metadata"] for response in responses] + revision_metadata
        require(checks, "all_response_ids_distinct_and_usage_consistent", len({x["response_id"] for x in all_usage}) == len(all_usage)
                and all(type(x["total_tokens"]) is int and x["total_tokens"] == x["input_tokens"] + x["output_tokens"]
                        and x["model"] == factory_summary["model_requested"] for x in all_usage))
        require(checks, "saved_campaign_refresh_matches_database", same({k:v for k,v in workflow.refresh_campaign(run).items() if k != "refreshed_at"},
                {k:v for k,v in saved.items() if k != "refreshed_at"}))
        require(checks, "all_cases_recovered_and_handoff_checks_pass", saved["counts_by_stage"] == {"RECOVERED": len(cases)}
                and saved["handoffs_verified"] == len(cases) and saved["checks_passed"] == saved["checks_total"])
        require(checks, "total_followup_calls_and_usage_accounted", saved["total_recorded_model_calls"] == len(all_usage)
                and saved["model_calls"] == len(ledger["calls"])
                and saved["followup_model_calls"] == len(all_usage) - len(ledger["calls"])
                and saved["usage"]["known_total_tokens"] == sum(x["total_tokens"] for x in all_usage)
                and saved["usage"]["missing_usage_calls"] == 0)
        require(checks, "original_archive_unchanged", digest(archive) == archive_hash)
        result = {"version": VERSION, "audited_at": datetime.now(timezone.utc).isoformat(), "status": "PASSED",
            "archive": {"filename": archive.name, "sha256": archive_hash, "bytes": archive.stat().st_size,
                        "campaign": run.name, "files": members},
            "review_environment": {"platform": platform.system(), "python_version": platform.python_version(),
                                   "core_source_sha256": project.source_hashes(), "audit_script_sha256": digest(__file__)},
            "recorded_run_environment": {"platform": factory_summary["platform"], "python_version": factory_summary["python_version"]},
            "generation": {"counts": factory_summary["counts"], "coverage": replay_summary["coverage"],
                           "model_requested": factory_summary["model_requested"]},
            "campaign_calls": len(ledger["calls"]), "followup_calls": saved["followup_model_calls"],
            "total_recorded_model_calls": len(all_usage), "reported_tokens": sum(x["total_tokens"] for x in all_usage),
            "missing_usage_calls": 0, "cases": cases, "checks": checks,
            "checks_passed": sum(checks.values()), "checks_total": len(checks), "new_model_calls_during_audit": 0,
            "scope": "Offline corroboration of user-supplied completed positive-repair development records, not independent provider attestation, held-out accuracy, production identity or production recovery.",
            "not_established": ["Full 218-test native Windows suite", "Individual Windows source hashes outside the seven controller-bound modules",
                                "Production identity and cloud/GPU actuation",
                                "Integration of this campaign with the separate HTTP/process adapter", "Held-out complete-loop evaluation"]}
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.archive.resolve() == args.output.resolve():
        parser.error("Choose a new output path; the original archive is retained.")
    try:
        result = verify_archive(args.archive)
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print("EVIDENCE AUDIT FAILED: " + str(exc))
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("PASS: %s cases, %s/%s evidence checks, 0 new model calls." % (len(result["cases"]), result["checks_passed"], result["checks_total"]))
    print("Report: " + str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
