#!/usr/bin/env python3
"""Connect validated challenges to explicit revision review and bounded recovery.

Default: eight free scripted integration cases. --live: a bounded Nebius
campaign that stops for human review. Infrastructure and authority are simulated.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import platform
import sys
import tempfile
import unittest
from unittest import mock
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

import challenge_factory as factory
import conflict_explorer as explorer
import conflict_lab as base
import live_evaluator as inference
import plan_revision as revision
import simulator as sim
import coverage_planner as coverage

VERSION = "agent-rehearsal-case-workflow-v1"
ROOT = Path(__file__).resolve().parent
SYSTEM = factory.SYSTEM + """

This campaign connects each challenge to revision at the original PRE-RECOVERY
decision boundary, after all seven operator steps and the injected fault.
Aim for varied cases where A's extra reservation is BLOCKED by B's changed
budget, while C has valid routing or a dependency. Include both: (1) healthy,
unpaused cases with positive but smaller remaining headroom, suitable for a
partial reservation; (2) paused, unhealthy, stale or zero-headroom cases where
revision must defer. stop_timeout, requests_in_flight and metrics_at_limit can
leave an otherwise healthy candidate. Use legal schedules and different bounded
parameters. Do not remove faults or supply revision/approval fields. Successful
original reservations are retained as not applicable, never replaced twice.
When coverage_target is supplied, aim for that requested outcome. Use the
recent observed coverage feedback to correct missed targets within the fixed
schema. An accepted case can still miss the target; do not claim success in reason.
"""


class NemotronChallenges(factory.NemotronProvider):
    system_prompt = SYSTEM


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CallBudget:
    """One shared ceiling across generation and revision; record before dispatch."""
    def __init__(self, directory, limit):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Use an integer total call limit from 1 to 10.")
        self.path, self.limit, self.rows = Path(directory) / "calls.json", limit, []
        self.save()

    @property
    def remaining(self):
        return self.limit - len(self.rows)

    def save(self):
        factory.write_json(self.path, {"limit": self.limit, "calls": self.rows})

    def wrap(self, provider, stage):
        budget = self

        class BoundedProvider:
            live = provider.live
            mode = provider.mode
            request_settings = provider.request_settings
            system_prompt = getattr(provider, "system_prompt", factory.SYSTEM)

            def ask(self, context):
                if not budget.remaining:
                    raise factory.GenerationError("The shared campaign call limit has been reached.")
                row = {"call": len(budget.rows) + 1, "stage": stage, "status": "REQUEST_STARTED",
                       "context_sha256": sim.digest(context), "request_settings": self.request_settings}
                budget.rows.append(row)
                budget.save()
                try:
                    content, metadata = provider.ask(context)
                    row.update(status="RECEIVED", metadata=metadata)
                    return content, metadata
                except BaseException as exc:
                    row["status"] = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR"
                    raise
                finally:
                    budget.save()
        return BoundedProvider()


def read_corpus(path):
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "accepted_proposals.jsonl"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Use a regular challenge JSON or accepted_proposals.jsonl file up to 1 MiB.")
    raw = path.read_bytes()
    if path.suffix.lower() == ".jsonl":
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        if len(lines) > 100:
            raise ValueError("The input corpus is limited to 100 proposals.")
        proposals = [factory.validate_proposal(factory.decode(line)) for line in lines]
    else:
        proposals = [factory.load_proposal(path)]
    if not proposals:
        raise ValueError("This source has no accepted challenge proposals.")
    return path, raw, proposals


def prepare_case(directory, proposal):
    """Recompute baseline labels, then replay exactly to the same decision state."""
    proposal = factory.validate_proposal(proposal)
    baseline = factory.rehearse(proposal)
    if not baseline["passed"]:
        raise ValueError("Independent baseline checks failed; no revision session was created.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    factory.write_json(directory / "challenge.json", proposal)
    factory.write_json(directory / "baseline.json", baseline)
    e = revision.RevisionEngine(directory / "coordinator.sqlite3")
    try:
        spec = factory.executable(proposal)
        explorer.seed(e, spec["parameters"])
        trace = explorer.execute(e, spec)
        checks = {"independent_baseline_passed": bool(baseline["passed"]),
                  "exact_decision_state": e.state() == baseline["decision_state"],
                  "exact_event_prefix": e.events() == baseline["parent_events"],
                  "exact_operator_trace": trace == baseline["trace"], **explorer.audit(e)}
        handoff = {"boundary": "AFTER_ALL_OPERATOR_STEPS_AND_FAULT_BEFORE_RECOVERY", "checks": checks,
                   "passed": all(checks.values()), "state_sha256": sim.digest(e.state()),
                   "event_prefix_sha256": sim.digest(e.events()), "event_prefix_length": len(e.events())}
        factory.write_json(directory / "handoff.json", handoff)
        if not handoff["passed"]:
            raise ValueError("Case replay did not preserve the original state and journal; revision stopped.")
        meta = {"version": revision.VERSION, "scenario": "generated_" + proposal["scenario"],
                "scripted_demo": False, "model_calls": 0, "proposal_attempts": [],
                "mode": "VALIDATED CASE REPLAY; NO REVISION PROPOSAL YET", "created_at": datetime.now(timezone.utc).isoformat(),
                "source_case": {"case_id": baseline["case_id"], "program_digest": factory.identifiers(proposal)[0],
                                "proposal_sha256": file_digest(directory / "challenge.json"),
                                "baseline_sha256": file_digest(directory / "baseline.json"),
                                "handoff_sha256": file_digest(directory / "handoff.json")}}
        factory.write_json(directory / "session.json", meta)
        report = revision.save_report(directory, e, meta)
        return report, handoff
    finally:
        e.close()


def finish_scripted_case(directory):
    """Only the explicitly labelled offline demo calls this fixture manager."""
    meta = revision.session_metadata(directory)
    if meta.get("model_calls"):
        raise ValueError("Scripted consent cannot be used for a live proposal session.")
    e = revision.RevisionEngine(directory / "coordinator.sqlite3", create=False)
    try:
        checks = {}
        if meta.get("proposal_id"):
            record = e.proposal_record(meta["proposal_id"])
            checks["proposal_accepted_or_deferred"] = record["status"] in ("AWAITING_APPROVAL", "DEFERRED")
            if record["status"] == "AWAITING_APPROVAL":
                approved = e.approve(record["revision_id"], "Offline integration fixture consents to this exact reservation and bounded recovery.",
                                     "scripted-consent", source="SCRIPTED OFFLINE DEMO MANAGER FIXTURE")
                before = base.commitments(e.state())
                executed = e.execute_revision(record["revision_id"])
                after = base.commitments(e.state())
                replay = e.execute_revision(record["revision_id"])
                checks.update(fresh_consent_recorded=approved["status"] == "SUCCEEDED",
                              exact_reservation=executed["status"] == "SUCCEEDED" and after == before + record["proposal"]["additional_units"],
                              execution_idempotent=bool(replay.get("replayed")) and base.commitments(e.state()) == after)
        recovery = revision.recovery_check(e)
        checks.update({"recovery_" + key: value for key, value in recovery["checks"].items()})
        meta.update(scripted_demo=True, demo_recovery=recovery, demo_checks=checks)
        factory.write_json(directory / "session.json", meta)
        revision.save_report(directory, e, meta)
        return checks
    finally:
        e.close()


def refresh_campaign(directory):
    """Refresh from saved databases; no model requests or service actions."""
    directory = Path(directory).resolve()
    config = revision.read_json(directory / "campaign.json")
    if config.get("version") != VERSION:
        raise ValueError("Use a saved Case Workflow campaign folder.")
    rows, session_calls, session_tokens = [], 0, []
    for item in config["cases"]:
        name = item["directory"]
        if not isinstance(name, str) or Path(name).name != name or not name.startswith("case-") or "\\" in name:
            raise ValueError("Invalid case directory in the campaign record.")
        path = directory / name
        if path.is_symlink():
            raise ValueError("Case folders must not be symbolic links.")
        meta = revision.read_json(path / "session.json")
        session_calls += meta.get("model_calls", 0)
        session_tokens.extend(a.get("generation", {}).get("metadata", {}).get("total_tokens") for a in meta.get("proposal_attempts", []) if a.get("generation"))
        if not meta.get("scripted_demo"):
            revision.session_metadata(path)
        e = revision.RevisionEngine(path / "coordinator.sqlite3", create=False)
        try:
            report = revision.save_report(path, e, meta)
            handoff = revision.read_json(path / "handoff.json")
            baseline = revision.read_json(path / "baseline.json")
            prefix_valid = sim.digest(e.events()[:handoff["event_prefix_length"]]) == handoff["event_prefix_sha256"]
            statuses = [r["status"] for r in report["revisions"]]
            if "deploy-A" in e.state()["paused_runs"] and any(event["kind"] == "RECOVERY_REPORT" for event in e.events()):
                stage = "RECOVERED"
            else:
                stage = statuses[-1] if statuses else item["stage"]
            checks = {**handoff["checks"], "source_event_prefix_preserved": prefix_valid,
                      **report["journal_audit"], **meta.get("demo_checks", {})}
            latest = report["revisions"][-1] if report["revisions"] else None
            rows.append({**item, "stage": stage, "proposal_status": statuses[-1] if statuses else None,
                         "revision_units": latest["proposal"]["additional_units"] if latest else None,
                         "revision_rehearsal_passed": latest["rehearsal"]["passed"] if latest else None,
                         "reference_admission": latest["rehearsal"]["reference_admission"] if latest else None,
                         "before_units": base.commitments(baseline["decision_state"]), "current_units": report["committed_units"],
                         "capacity_limit": report["state"]["capacity_limit"], "route_owner": report["state"]["route_owner"],
                         "model_calls": meta["model_calls"], "checks": checks, "checks_passed": all(checks.values()),
                         "coverage_observation": coverage.classify(baseline), "report": name + "/index.html"})
        finally:
            e.close()
    ledger = revision.read_json(directory / "calls.json")
    generation_rows = [row for row in ledger["calls"] if row["stage"] == "challenge_generation"]
    tokens = [row.get("metadata", {}).get("total_tokens") for row in generation_rows] + session_tokens
    known = [value for value in tokens if type(value) is int and value >= 0]
    total_calls = len(generation_rows) + session_calls
    stages = dict(Counter(row["stage"] for row in rows))
    summary = {**config, "rows": rows, "counts_by_stage": stages, "cases_total": len(rows),
               "handoffs_verified": sum(all(row["checks"].values()) for row in rows),
               "checks_passed": sum(sum(row["checks"].values()) for row in rows),
               "checks_total": sum(len(row["checks"]) for row in rows),
               "model_calls": len(ledger["calls"]), "calls_by_stage": dict(Counter(row["stage"] for row in ledger["calls"])),
               "followup_model_calls": max(0, total_calls - len(ledger["calls"])), "total_recorded_model_calls": total_calls,
               "usage": {"known_total_tokens": sum(known), "calls_with_usage": len(known), "missing_usage_calls": max(0, total_calls - len(known))},
               "refreshed_at": datetime.now(timezone.utc).isoformat(),
               "scope": "Development cases, simulated infrastructure and local manager fixtures. Baseline recovery and revised recovery are distinct branches. Not held-out accuracy or production execution."}
    factory.write_json(directory / "summary.json", summary)
    (directory / "index.html").write_text(render_report(summary, directory), encoding="utf-8")
    return summary


def render_report(summary, directory):
    esc = lambda value: html.escape(str(value), quote=True)
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Orkestryn · Connected case lab</title>", factory.STYLE,
             "<h1>Connected case lab</h1><p>" + esc(summary["mode"]) + "</p><p>" + esc(summary["scope"]) + "</p>",
             "<div class='cards'><div class='card'><strong class='big'>" + esc(summary["cases_total"]) + "</strong>Validated cases</div><div class='card'><strong class='big'>" + esc(summary["handoffs_verified"]) + "</strong>Case checks pass</div><div class='card'><strong class='big'>" + esc(summary["model_calls"]) + " / " + esc(summary["call_limit"]) + "</strong>Campaign calls</div><div class='card'><strong class='big'>" + esc(summary["counts_by_stage"].get("RECOVERED", 0)) + "</strong>Recovered sessions</div></div>",
             "<h2>One case across three problems</h2><p>6: propose and independently validate a challenge. 9: retain its state, rehearse a revision and require fresh consent for the exact action. 12: recover eligible resources while preserving other operators' work.</p>",
             "<p>Campaign status: " + esc(summary["status"]) + ". " + esc(summary.get("stop_reason") or "") + "</p>",
             "<p>Additional calls from later, separately requested session proposals: " + esc(summary["followup_model_calls"]) + ". The campaign ceiling applies to the original command only. Total recorded calls including follow-ups: " + esc(summary["total_recorded_model_calls"]) + ".</p>",
             "<p>Reported tokens: " + esc(summary["usage"]["known_total_tokens"]) + "; calls reporting usage: " + esc(summary["usage"]["calls_with_usage"]) + "; missing usage: " + esc(summary["usage"]["missing_usage_calls"]) + ". No billed cost estimate.</p>",
             "<p><a href='" + esc(summary["factory_report"]) + "'>Challenge Factory evidence</a> · <a href='summary.json'>Campaign data</a> · <a href='calls.json'>Call ledger</a></p>",
             "<h2>Review each case</h2><p>Live and imported cases stop for your separate approval. Open a case for its exact commands. Deferral is a valid proposal outcome. NOT_APPLICABLE means the original reservation already succeeded. READY_FOR_PROPOSAL means no revision model call was made. A passing handoff does not imply an approved or executed revision.</p>",
             "<div class='scroll'><table><tr><th>Case / fault</th><th>Original request</th><th>Current stage</th><th>Revision</th><th>Capacity before → now / limit</th><th>Checks</th></tr>"]
    if summary.get("generation", {}).get("coverage"):
        parts.insert(-2, coverage.report_fragment(summary["generation"]["coverage"]))
    for row in summary["rows"]:
        parts.append("<tr><td><a href='" + esc(row["report"]) + "'>" + esc(row["directory"]) + "</a><br>" + esc(row["scenario"]) + " / " + esc(row["fault"]) + "</td><td>" + esc(row["original_units"]) + " units / " + esc(row["original_result"]) + "</td><td>" + esc(row["stage"]) + "</td><td>" + esc(row["revision_units"] if row["revision_units"] is not None else "—") + "<br>" + esc(row["proposal_status"] or "No proposal") + "</td><td>" + esc(row["before_units"]) + " → " + esc(row["current_units"]) + " / " + esc(row["capacity_limit"]) + "</td><td>" + ("PASS" if row["checks_passed"] else "FAIL") + "</td></tr>")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = ".\\.venv\\Scripts\\python.exe project.py case-workflow --run " + quote(directory) + " --inspect --open"
    parts += ["</table></div><h2>Refresh after review or recovery</h2><p>Case reports update when their commands run. Refresh this campaign to read the latest saved states; this makes no model calls.</p><pre>" + esc(command) + "</pre>",
              "<details><summary>Campaign limits and provenance</summary><pre>" + esc(json.dumps({key: summary[key] for key in ("call_limit", "source", "generation", "calls_by_stage", "counts_by_stage")}, indent=2)) + "</pre></details><footer>" + esc(VERSION) + "</footer></html>"]
    return "".join(parts)


def run_campaign(output, generation_provider=None, revision_provider=None, source=None, cases=5, calls=10, scripted=False, progress=True, coverage_mode="balanced"):
    policy = coverage.CoveragePlan(coverage_mode)
    live = generation_provider is not None and generation_provider.live or revision_provider is not None and revision_provider.live
    if type(cases) is not int or not 1 <= cases <= (5 if live else 8):
        raise ValueError("Use 1–5 cases for a live campaign or 1–8 offline cases.")
    if scripted and live:
        raise ValueError("Live campaigns always require separate explicit manager consent.")
    if type(calls) is not int or not 1 <= calls <= 10:
        raise ValueError("Use 1–10 total calls for generation and revision combined.")
    captured = read_corpus(source) if source is not None else None
    if source is None and generation_provider is None:
        raise ValueError("Choose a challenge source or a generation provider.")
    directory = revision.new_directory(output)
    budget = CallBudget(directory, calls)
    origin = {"kind": "MODEL_GENERATION" if live and source is None else "OFFLINE_FIXTURES"}
    if captured:
        path, raw, proposals = captured
        (directory / "source").mkdir()
        (directory / "source" / path.name).write_bytes(raw)
        origin = {"kind": "IMPORTED_CHALLENGES", "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                  "available_proposals": len(proposals), "selected_proposals": min(cases, len(proposals)),
                  "unselected_proposals": max(0, len(proposals) - cases)}
        generation_provider = factory.FixtureProvider([base.encode(p) for p in proposals[:cases]], "OFFLINE REVALIDATION OF IMPORTED CHALLENGES")
        cases = min(cases, len(proposals))
    generation_calls = min(cases, budget.remaining) if generation_provider.live else cases
    generation = budget.wrap(generation_provider, "challenge_generation") if generation_provider.live else generation_provider
    if not generation_provider.live:
        policy = coverage.CoveragePlan("off")
    factory_directory, generated = factory.run_batch(generation, directory / "factory", generation_calls, progress=progress, policy=policy)
    config = {"version": VERSION, "mode": "OFFLINE SCRIPTED CONNECTED CASE DEMO" if scripted else "LIVE NEBIUS CAMPAIGN; EXPLICIT MANAGER REVIEW REQUIRED" if live else "OFFLINE IMPORT; EXPLICIT MANAGER REVIEW REQUIRED",
              "created_at": datetime.now(timezone.utc).isoformat(), "platform": platform.system(),
              "call_limit": calls, "source": origin, "generation": generated, "cases": [],
              "factory_report": str((factory_directory / "index.html").relative_to(directory)).replace(os.sep, "/"),
              "status": generated["run_status"], "stop_reason": generated["stop_reason"]}
    factory.write_json(directory / "campaign.json", config)
    accepted = [factory.validate_proposal(factory.decode(line)) for line in (factory_directory / "accepted_proposals.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    allow_requests = generated["run_status"] == "COMPLETED"
    try:
        for index, proposal in enumerate(accepted, 1):
            name = "case-%03d" % index
            case_directory = directory / name
            report, _ = prepare_case(case_directory, proposal)
            original = report["original_request"]
            stage = "NOT_APPLICABLE" if original["result"]["status"] == "SUCCEEDED" else "READY_FOR_PROPOSAL"
            item = {"directory": name, "scenario": proposal["scenario"], "fault": proposal["fault"],
                    "original_units": original["args"]["units"], "original_result": original["result"]["code"], "stage": stage}
            config["cases"].append(item)
            factory.write_json(directory / "campaign.json", config)
            if stage == "READY_FOR_PROPOSAL" and allow_requests:
                provider = None
                if live:
                    if revision_provider is None or not budget.remaining:
                        item["reason"] = "Shared call budget exhausted or no revision provider; no fallback proposal was invented."
                        continue
                    provider = budget.wrap(revision_provider, "plan_revision")
                _, report = revision.create_session(directory, "generated", provider=provider, existing_directory=case_directory)
                item["stage"] = report["meta"].get("proposal_status", report["meta"].get("generation", {}).get("status", "FAILED"))
                failure = report["meta"].get("generation", {}).get("status")
                if failure in ("API_ERROR", "REFUSED", "INCOMPLETE_RESPONSE", "INTERRUPTED", "INTERNAL_ERROR"):
                    allow_requests = False
                    config.update(status="STOPPED", stop_reason="Revision request stopped the paid campaign: " + failure + ". No automatic retry.")
            if scripted:
                finish_scripted_case(case_directory)
            factory.write_json(directory / "campaign.json", config)
            if progress:
                print("Connected case %s/%s: %s" % (index, len(accepted), item["stage"]), flush=True)
    except BaseException:
        config.update(status="STOPPED", stop_reason="Campaign interrupted; retained cases and call ledger can be inspected. No automatic resume.")
        raise
    finally:
        if captured:
            config["source"]["original_file_unchanged"] = captured[0].is_file() and file_digest(captured[0]) == origin["sha256"]
        config["calls_unused"] = budget.remaining
        factory.write_json(directory / "campaign.json", config)
        summary = refresh_campaign(directory)
    return directory, summary


def demo_proposals():
    order = ["A_READ", "B_READ", "B_LIMIT", "C_READ", "C_WRITE", "A_RESERVE", "A_ROUTE"]
    rows = [(35, 15, 16, 80, 55, "routing", "stop_timeout"),
            (27, 13, 12, 65, 46, "dependency", "requests_in_flight"),
            (20, 10, 8, 55, 34, "routing", "metrics_at_limit"),
            (30, 12, 15, 70, 50, "routing", "workflow_failure"),
            (22, 8, 10, 55, 35, "dependency", "candidate_failure"),
            (18, 7, 6, 45, 30, "routing", "metrics_expired"),
            (40, 20, 20, 100, 60, "dependency", "stop_timeout"),
            (15, 5, 4, 50, 40, "routing", "stop_timeout")]
    proposals = []
    for values in rows:
        p = {"schema_version": factory.PROPOSAL_VERSION, "reason": "Offline integration fixture for exact case continuity and bounded revision.",
             "parameters": dict(zip(("stable_units", "candidate_units", "extra_units", "initial_limit", "new_limit"), values[:5])),
             "scenario": values[5], "fault": values[6], "fault_after": 7, "order": order[:]}
        proposals.append(p)
    proposals[-1]["order"] = ["A_READ", "A_RESERVE", "A_ROUTE", "B_READ", "B_LIMIT", "C_READ", "C_WRITE"]
    return proposals


def run_demo(output, progress=True):
    provider = factory.FixtureProvider([base.encode(p) for p in demo_proposals()])
    return run_campaign(output, generation_provider=provider, cases=8, scripted=True, progress=progress)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def corpus(self, proposals=None):
        path = self.root / "accepted_proposals.jsonl"
        path.write_text("".join(base.encode(p) + "\n" for p in (proposals or demo_proposals()[:2])), encoding="utf-8")
        return path

    def proposer(self, units=5, usage=120):
        provider = factory.FixtureProvider([])
        provider.live, provider.mode = True, "MOCKED LIVE PROPOSER; NO NETWORK"
        def answer(context):
            return base.encode({"schema_version": revision.PROPOSAL_VERSION, "decision": "revise" if units else "defer",
                "additional_units": units, "based_on_state": context["based_on_state"], "reason": "Bounded mocked proposal."}), {"finish_reason": "stop", "total_tokens": usage}
        provider.ask = mock.Mock(side_effect=answer)
        return provider

    def generator(self, proposals=None):
        provider = factory.FixtureProvider([base.encode(p) for p in (proposals or demo_proposals()[:2])])
        provider.live = True
        provider.system_prompt = SYSTEM
        return provider

    def test_demo_connects_eight_distinct_cases_through_recovery(self):
        directory, summary = run_demo(self.root, progress=False)
        self.assertEqual(summary["generation"]["counts"]["accepted"], 8)
        self.assertEqual(summary["counts_by_stage"], {"RECOVERED": 8})
        self.assertEqual([row["revision_units"] for row in summary["rows"]], [5, 6, 4, 0, 0, 0, 0, None])
        self.assertEqual([row["proposal_status"] for row in summary["rows"]], ["EXECUTED"] * 3 + ["DEFERRED"] * 4 + [None])
        self.assertEqual(summary["checks_passed"], summary["checks_total"])
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["handoffs_verified"], 8)
        for row in summary["rows"]:
            meta = revision.read_json(directory / row["directory"] / "session.json")
            self.assertTrue(meta["scripted_demo"])

    def test_handoff_preserves_arbitrary_parameters_trace_and_faults(self):
        for index, proposal in enumerate(demo_proposals()):
            proposal["fault_after"] = index
            baseline = factory.rehearse(proposal)
            report, handoff = prepare_case(self.root / str(index), proposal)
            self.assertTrue(handoff["passed"])
            self.assertEqual(report["state"], baseline["decision_state"])
            self.assertEqual(report["events"], baseline["parent_events"])
            self.assertEqual(report["revisions"], [])

    def test_reference_mismatch_prevents_session_creation(self):
        baseline = factory.rehearse(demo_proposals()[0]); baseline["passed"] = False
        with mock.patch.object(factory, "rehearse", return_value=baseline):
            with self.assertRaises(ValueError):
                prepare_case(self.root / "case", demo_proposals()[0])
        self.assertFalse((self.root / "case").exists())

    def test_replay_mismatch_cannot_create_review_session(self):
        baseline = factory.rehearse(demo_proposals()[0]); baseline["decision_state"]["clock"] += 1
        with mock.patch.object(factory, "rehearse", return_value=baseline):
            with self.assertRaises(ValueError):
                prepare_case(self.root / "case", demo_proposals()[0])
        self.assertFalse((self.root / "case" / "session.json").exists())

    def test_import_keeps_source_bytes_and_deduplicates(self):
        proposals = demo_proposals()[:2]
        source = self.corpus(proposals + [copy.deepcopy(proposals[0])])
        before = source.read_bytes()
        directory, summary = run_campaign(self.root / "runs", source=source, cases=3, progress=False)
        self.assertEqual(summary["generation"]["counts"]["by_outcome"], {"ACCEPTED": 2, "DUPLICATE_PROGRAM": 1})
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual((directory / "source" / source.name).read_bytes(), before)
        self.assertTrue(summary["source"]["original_file_unchanged"])
        self.assertEqual(summary["counts_by_stage"], {"AWAITING_APPROVAL": 2})

    def test_import_selection_is_explicit_and_invalid_input_fails_early(self):
        source = self.corpus()
        _, summary = run_campaign(self.root / "runs", source=source, cases=1, progress=False)
        self.assertEqual(summary["source"]["unselected_proposals"], 1)
        source.write_text('{"unexpected":1}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            run_campaign(self.root / "bad", source=source, progress=False)
        self.assertFalse((self.root / "bad").exists())

    def test_explicit_consent_exact_execute_reopen_and_preservation(self):
        directory, summary = run_campaign(self.root / "runs", source=self.corpus([demo_proposals()[0]]), progress=False)
        case = directory / "case-001"
        source_hashes = {name: file_digest(case / name) for name in ("challenge.json", "baseline.json", "handoff.json")}
        report = revision.read_json(case / "report.json")
        rid = report["revisions"][0]["revision_id"]
        denied, report = revision.operate(case, "execute", rid)
        self.assertEqual(denied["code"], "REVISION_APPROVAL_REQUIRED")
        self.assertEqual(report["committed_units"], 50)
        approved, _ = revision.operate(case, "approve", rid, "Explicit test manager review of 5 units.")
        self.assertEqual(approved["status"], "SUCCEEDED")
        executed, report = revision.operate(case, "execute", rid)
        self.assertEqual(executed["status"], "SUCCEEDED")
        self.assertEqual(report["committed_units"], 55)
        repeated, report = revision.operate(case, "execute", rid)
        self.assertTrue(repeated["replayed"])
        self.assertEqual(report["committed_units"], 55)
        recovered, report = revision.operate(case, "recover")
        self.assertEqual(recovered["status"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(report["committed_units"], 50)
        self.assertEqual(report["state"]["route_owner"], "route-C")
        self.assertTrue(all(report["journal_audit"].values()))
        self.assertEqual({name: file_digest(case / name) for name in source_hashes}, source_hashes)
        refreshed = refresh_campaign(directory)
        self.assertEqual(refreshed["counts_by_stage"], {"RECOVERED": 1})
        self.assertTrue(refreshed["rows"][0]["checks"]["source_event_prefix_preserved"])

    def test_changed_source_evidence_blocks_operations(self):
        directory, _ = run_campaign(self.root / "runs", source=self.corpus([demo_proposals()[0]]), progress=False)
        case = directory / "case-001"
        rid = revision.read_json(case / "session.json")["proposal_id"]
        (case / "challenge.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            revision.operate(case, "approve", rid, "Must not approve changed evidence.")

    def test_paused_case_rejects_model_attempt_to_resume(self):
        source = self.corpus([demo_proposals()[3]])
        directory, summary = run_campaign(self.root / "runs", source=source, revision_provider=self.proposer(), cases=1, progress=False)
        self.assertEqual(summary["counts_by_stage"], {"REHEARSAL_REJECTED": 1})
        self.assertEqual(summary["rows"][0]["reference_admission"], "WORKFLOW_PAUSED")
        report = revision.read_json(directory / "case-001" / "report.json")
        self.assertIn("deploy-A", report["state"]["paused_runs"])
        self.assertIsNone(report["revisions"][0]["grant"])

    def test_original_success_is_not_replaced_or_sent_to_model(self):
        provider = self.proposer()
        directory, summary = run_campaign(self.root / "runs", source=self.corpus([demo_proposals()[-1]]), revision_provider=provider, cases=1, progress=False)
        provider.ask.assert_not_called()
        self.assertEqual(summary["counts_by_stage"], {"NOT_APPLICABLE": 1})
        self.assertEqual(summary["model_calls"], 0)
        with self.assertRaisesRegex(ValueError, "not blocked"):
            revision.create_session(self.root, "generated", provider=provider, existing_directory=directory / "case-001")
        provider.ask.assert_not_called()

    def test_model_revision_stops_for_review_without_parent_effects(self):
        directory, summary = run_campaign(self.root / "runs", source=self.corpus([demo_proposals()[0]]), revision_provider=self.proposer(), cases=1, progress=False)
        self.assertEqual(summary["counts_by_stage"], {"AWAITING_APPROVAL": 1})
        report = revision.read_json(directory / "case-001" / "report.json")
        baseline = revision.read_json(directory / "case-001" / "baseline.json")
        self.assertEqual(report["state"], baseline["decision_state"])
        self.assertFalse(any(e["kind"] == "REVISION_APPROVED" for e in report["events"]))
        self.assertEqual(summary["usage"], {"known_total_tokens": 120, "calls_with_usage": 1, "missing_usage_calls": 0})

    def test_shared_budget_covers_both_stages_without_silent_fallback(self):
        provider = self.proposer()
        directory, summary = run_campaign(self.root / "runs", self.generator(), provider, cases=2, calls=3, progress=False)
        self.assertEqual(summary["model_calls"], 3)
        self.assertEqual(summary["calls_by_stage"], {"challenge_generation": 2, "plan_revision": 1})
        self.assertEqual(summary["counts_by_stage"], {"AWAITING_APPROVAL": 1, "READY_FOR_PROPOSAL": 1})
        self.assertEqual(provider.ask.call_count, 1)
        self.assertEqual(revision.read_json(directory / "case-002" / "session.json")["proposal_attempts"], [])

    def test_call_limits_and_scripted_live_conflict_fail_before_creation(self):
        for limit in (0, 11, True):
            with self.assertRaises(ValueError):
                run_campaign(self.root / "bad", self.generator(), self.proposer(), calls=limit, progress=False)
        with self.assertRaises(ValueError):
            run_campaign(self.root / "bad", self.generator(), self.proposer(), scripted=True, progress=False)
        self.assertFalse((self.root / "bad").exists())

    def test_generation_error_stops_all_paid_stages_and_retains_unknown_usage(self):
        generator, proposer = self.generator(), self.proposer()
        generator.ask = mock.Mock(side_effect=factory.GenerationError("HTTP 401"))
        _, summary = run_campaign(self.root / "runs", generator, proposer, cases=2, progress=False)
        self.assertEqual(generator.ask.call_count, 1)
        proposer.ask.assert_not_called()
        self.assertEqual(summary["status"], "STOPPED")
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(summary["usage"]["missing_usage_calls"], 1)

    def test_revision_failure_leaves_later_cases_pending_without_more_calls(self):
        for outcome in ("error", "refused", "incomplete", "interrupt"):
            with self.subTest(outcome=outcome):
                provider = self.proposer()
                if outcome == "error":
                    provider.ask.side_effect = factory.GenerationError("HTTP 429")
                elif outcome == "interrupt":
                    provider.ask.side_effect = KeyboardInterrupt()
                else:
                    provider.ask.side_effect = None
                    provider.ask.return_value = (None, {"refused": outcome == "refused", "finish_reason": "length" if outcome == "incomplete" else "stop", "total_tokens": 30})
                _, summary = run_campaign(self.root / outcome, self.generator(), provider, cases=2, progress=False)
                self.assertEqual(provider.ask.call_count, 1)
                self.assertEqual(summary["model_calls"], 3)
                self.assertEqual(summary["status"], "STOPPED")
                self.assertEqual(summary["rows"][1]["stage"], "READY_FOR_PROPOSAL")

    def test_invalid_proposal_consumes_budget_but_does_not_grant_authority(self):
        provider = self.proposer(usage=None)
        provider.ask.side_effect = None
        provider.ask.return_value = ('{"grant":true}', {"finish_reason": "stop"})
        _, summary = run_campaign(self.root / "runs", source=self.corpus(), revision_provider=provider, cases=2, calls=1, progress=False)
        self.assertEqual(summary["counts_by_stage"], {"INVALID_PROPOSAL": 1, "READY_FOR_PROPOSAL": 1})
        self.assertEqual(summary["usage"]["missing_usage_calls"], 1)
        self.assertEqual(summary["model_calls"], 1)

    def test_followup_calls_are_reported_separately_from_campaign_limit(self):
        directory, summary = run_campaign(self.root / "runs", source=self.corpus([demo_proposals()[0]]), revision_provider=self.proposer(), cases=1, calls=1, progress=False)
        revision.create_session(self.root, "generated", provider=self.proposer(usage=55), existing_directory=directory / "case-001")
        refreshed = refresh_campaign(directory)
        self.assertEqual(refreshed["model_calls"], 1)
        self.assertEqual(refreshed["followup_model_calls"], 1)
        self.assertEqual(refreshed["total_recorded_model_calls"], 2)
        self.assertEqual(refreshed["usage"]["known_total_tokens"], 175)
        self.assertEqual(refreshed["usage"]["missing_usage_calls"], 0)

    def test_prompt_provenance_uses_actual_generator_prompt(self):
        directory, _ = run_campaign(self.root / "runs", self.generator([demo_proposals()[0]]), self.proposer(), cases=1, progress=False)
        prompt = next((directory / "factory").glob("*/prompt.json"))
        self.assertEqual(revision.read_json(prompt)["system"], SYSTEM)
        context = revision.read_json(directory / "case-001" / "attempts" / "attempt-001" / "context.json")
        self.assertEqual(context["blocked_request"]["args"]["units"], 16)
        self.assertEqual(context["goal_max_units"], 16)

    def test_repair_targets_saved_before_dispatch_and_do_not_grant_authority(self):
        generator = self.generator()
        observed = []
        original_ask = generator.ask
        def ask(context):
            request_path = next((self.root / "runs").glob("*/factory/*/requests.jsonl"))
            saved = json.loads(request_path.read_text(encoding="utf-8").splitlines()[-1])["context"]
            self.assertEqual(saved, json.loads(base.encode(context)))
            observed.append(copy.deepcopy(context))
            return original_ask(context)
        generator.ask = ask
        directory, summary = run_campaign(self.root / "runs", generator, self.proposer(), cases=2, calls=4, progress=False, coverage_mode="repair")
        self.assertEqual([c["coverage_target"]["id"] for c in observed], ["REPAIRABLE_ROUTING", "REPAIRABLE_DEPENDENCY"])
        ledger = revision.read_json(directory / "calls.json")
        self.assertEqual([r["context_sha256"] for r in ledger["calls"][:2]], [sim.digest(c) for c in observed])
        self.assertEqual(summary["generation"]["coverage"]["status"], "MET")
        self.assertEqual(summary["model_calls"], 4)
        self.assertEqual(summary["counts_by_stage"], {"AWAITING_APPROVAL": 2})
        for row in summary["rows"]:
            r = revision.read_json(directory / row["directory"] / "report.json")
            self.assertIsNone(r["revisions"][0]["grant"])
            self.assertEqual(row["before_units"], row["current_units"])

    def test_imports_observe_coverage_without_claiming_targeted_generation(self):
        _, summary = run_campaign(self.root / "runs", source=self.corpus(), revision_provider=self.proposer(), cases=2, progress=False, coverage_mode="repair")
        c = summary["generation"]["coverage"]
        self.assertEqual(c["status"], "NOT_TARGETED")
        self.assertEqual(c["targets"], [])
        self.assertEqual(c["accepted_categories"], {"REPAIRABLE_ROUTING": 1, "REPAIRABLE_DEPENDENCY": 1})

    def test_campaigns_without_coverage_fields_still_refresh(self):
        directory, _ = run_campaign(self.root / "runs", source=self.corpus([demo_proposals()[0]]), progress=False)
        config = revision.read_json(directory / "campaign.json")
        config["generation"].pop("coverage")
        factory.write_json(directory / "campaign.json", config)
        summary = refresh_campaign(directory)
        self.assertEqual(summary["cases_total"], 1)
        self.assertEqual(summary["model_calls"], 0)
        self.assertTrue(summary["rows"][0]["checks_passed"])

    def test_offline_dispatch_drops_credentials(self):
        import project
        with mock.patch.dict(os.environ, {"NEBIUS_API_KEY": "test-secret", "OPENAI_API_KEY": "test-other"}), mock.patch.object(project.subprocess, "run") as run:
            run.return_value.returncode = 0
            project.run_model_script("case_workflow.py", [], False)
            env = run.call_args.kwargs["env"]
            self.assertNotIn("NEBIUS_API_KEY", env)
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_reports_escape_model_text_and_link_local_evidence(self):
        p = demo_proposals()[0]; p["reason"] = "<script>alert('x')</script>"
        directory, summary = run_campaign(self.root / "runs", source=self.corpus([p]), progress=False)
        report = (directory / "case-001" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(p["reason"], report)
        self.assertIn("handoff.json", report)
        self.assertIn("16 extra units", report)
        self.assertIn("explicit", (directory / "index.html").read_text(encoding="utf-8").lower())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--from-factory", type=Path, help="A factory run folder, accepted_proposals.jsonl, or one challenge JSON.")
    parser.add_argument("--cases", type=int, default=5, help="Maximum generated attempts or imported proposals (1–5 live).")
    parser.add_argument("--calls", type=int, default=10, help="Total paid call ceiling across generation and revision (1–10).")
    parser.add_argument("--max-output-tokens", type=int, default=10000)
    parser.add_argument("--thinking", choices=("on", "off"), default="on")
    parser.add_argument("--coverage", choices=("balanced", "repair", "off"), default="balanced", help="Target fresh generated cases; imports remain observation-only.")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "case_runs")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if args.run or args.inspect:
        if not (args.run and args.inspect) or args.live or args.from_factory:
            parser.error("Refresh with --run FOLDER --inspect, without --live or --from-factory.")
        directory, summary = args.run.resolve(), refresh_campaign(args.run)
    elif args.live or args.from_factory:
        if not 1 <= args.cases <= 5 or not 1 <= args.calls <= 10 or not 1024 <= args.max_output_tokens <= 10000:
            parser.error("Use 1–5 cases, 1–10 total calls and 1024–10000 output tokens per call.")
        if args.from_factory:
            read_corpus(args.from_factory)
        generation = proposer = None
        if args.live:
            print("LIVE NEBIUS CAMPAIGN: at most %s TOTAL requests; output cap %s each. No automatic retries or manager approval." % (args.calls, args.max_output_tokens), flush=True)
            key = (os.environ.get("NEBIUS_API_KEY") or getpass("Paste your Nebius API key locally: ")).strip()
            if not key:
                raise factory.GenerationError("No key supplied; no requests made.")
            generation = NemotronChallenges(key, args.max_output_tokens, args.thinking == "on") if not args.from_factory else None
            proposer = revision.NemotronRevision(key, args.max_output_tokens, args.thinking == "on")
        directory, summary = run_campaign(args.output, generation, proposer, args.from_factory, args.cases, args.calls, coverage_mode=args.coverage)
    else:
        directory, summary = run_demo(args.output)
    print("Cases: %s | case checks passing: %s | checks: %s/%s | model calls: %s/%s" % (summary["cases_total"], summary["handoffs_verified"], summary["checks_passed"], summary["checks_total"], summary["model_calls"], summary["call_limit"]))
    print("Report: " + str(directory / "index.html"))
    if args.open:
        webbrowser.open((directory / "index.html").as_uri())
    return 0 if summary["status"] == "COMPLETED" and summary["checks_passed"] == summary["checks_total"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, factory.GenerationError) as exc:
        print("CASE WORKFLOW FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
