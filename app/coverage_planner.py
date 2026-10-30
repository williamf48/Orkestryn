#!/usr/bin/env python3
"""Outcome-guided challenge coverage; advisory only, with a free fixture lab."""
from __future__ import annotations

import argparse
import copy
import html
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import webbrowser

import challenge_factory as factory
import conflict_lab as base
import plan_revision as revision

VERSION = "agent-rehearsal-coverage-v1"
TARGETS = {
    "REPAIRABLE_ROUTING": "Smaller reservation with C's route preserved",
    "REPAIRABLE_DEPENDENCY": "Smaller reservation with C's dependency preserved",
    "DEFER_PAUSED": "Paused workflow must defer",
    "DEFER_NO_HEADROOM": "No capacity remains for a reservation",
    "ALREADY_SUCCEEDED": "Original reservation already succeeded",
}
HINTS = {
    "REPAIRABLE_ROUTING": ["Use scenario routing and make C_WRITE succeed so route_owner remains route-C.",
        "Make B_LIMIT succeed before A_RESERVE. Set 0 < new_limit - (stable_units + candidate_units) < extra_units.",
        "Keep A unpaused, candidate healthy, fallback safe and metrics fresh at the final decision boundary.",
        "stop_timeout, requests_in_flight or metrics_at_limit can preserve reservation eligibility.",
        "One legal order is A_READ, B_READ, B_LIMIT, C_READ, C_WRITE, A_RESERVE, A_ROUTE; vary capacities and valid timing."],
    "REPAIRABLE_DEPENDENCY": ["Use scenario dependency and make C_WRITE successfully add C's job dependency.",
        "Make B_LIMIT succeed before A_RESERVE. Set 0 < new_limit - (stable_units + candidate_units) < extra_units.",
        "Keep A unpaused, candidate healthy, fallback safe and metrics fresh at the final decision boundary.",
        "stop_timeout, requests_in_flight or metrics_at_limit can preserve reservation eligibility.",
        "One legal order is A_READ, B_READ, B_LIMIT, C_READ, C_WRITE, A_RESERVE, A_ROUTE; vary capacities and valid timing."],
    "DEFER_PAUSED": ["Use workflow_failure before A_RESERVE so the original request is blocked and A is paused.",
        "All seven steps must still appear in legal actor order; other actors may continue."],
    "DEFER_NO_HEADROOM": ["Make B_LIMIT succeed before A_RESERVE, with new_limit = stable_units + candidate_units.",
        "Keep A unpaused, candidate healthy, fallback safe and metrics fresh so capacity is the admission blocker.",
        "Use stop_timeout, requests_in_flight or metrics_at_limit with valid timing."],
    "ALREADY_SUCCEEDED": ["Place A_RESERVE before B_LIMIT and provide enough initial capacity for stable + candidate + extra units.",
        "Keep the candidate eligible when A_RESERVE runs. This is a control case, not a request for a second reservation."],
}


def classify(record):
    """Read the verified pre-recovery state; never change it or create a grant."""
    if record.get("passed") is not True:
        return {"category": "UNVERIFIED", "reason": "Independent rehearsal checks did not pass."}
    originals = [e for e in record["parent_events"] if e.get("action_key") == "explorer-A_RESERVE"]
    if len(originals) != 1:
        return {"category": "UNVERIFIED", "reason": "Exactly one original reservation event is required."}
    original, s = originals[0], record["decision_state"]
    units = original["args"]["units"]
    headroom = s["capacity_limit"] - base.commitments(s)
    bound = s["plans"]["deploy-A"]["max_units"] - revision.own_units(s)
    maximum = max(0, min(20, units, headroom, bound))
    d = s["deployments"].get("candidate-v2", {})
    route = s["route_owner"] == "route-C" and s["routing"].get("candidate-v2", 0) > 0
    dependency = any(item["owner_run"] == "route-C" for item in d.get("dependencies", []))
    result = {"original_status": original["result"]["status"], "original_code": original["result"]["code"],
              "original_units": units, "headroom": headroom, "plan_remaining": bound,
              "paused": "deploy-A" in s["paused_runs"], "protected_route": route, "protected_dependency": dependency,
              "based_on_state": revision.state_digest(s), "maximum_reference_units": 0}
    if result["original_status"] == "SUCCEEDED":
        return {**result, "category": "ALREADY_SUCCEEDED", "reference_admission": "NOT_APPLICABLE",
                "reason": "The original reservation succeeded; no replacement is needed."}
    if result["original_status"] != "BLOCKED":
        return {**result, "category": "UNVERIFIED", "reason": "Original request has no recognized completed outcome."}
    probe = {"decision": "revise", "additional_units": maximum or 1, "based_on_state": result["based_on_state"]}
    admission = revision.admission_reference(s, probe)
    if admission == "ELIGIBLE" and maximum:
        category = "REPAIRABLE_ROUTING" if route else "REPAIRABLE_DEPENDENCY" if dependency else "REPAIRABLE_OTHER"
        if not 0 < headroom < units:
            category = "REPAIRABLE_OTHER"
        result["maximum_reference_units"] = maximum
        reason = "A reservation up to %s units meets the reference contract; no proposal or approval is implied." % maximum
    else:
        category = {"WORKFLOW_PAUSED": "DEFER_PAUSED", "INSUFFICIENT_HEADROOM": "DEFER_NO_HEADROOM"}.get(admission, "DEFER_OTHER")
        reason = "Positive reservation fails the reference contract: " + admission + "."
    return {**result, "category": category, "reference_admission": admission, "reason": reason}


class CoveragePlan:
    def __init__(self, mode="balanced"):
        if mode not in ("balanced", "repair", "off"):
            raise ValueError("Coverage must be balanced, repair or off.")
        self.mode = mode
        self.targets = list(TARGETS) if mode == "balanced" else list(TARGETS)[:2] if mode == "repair" else []
        self.observations = {}

    def snapshot(self, exports_valid=True):
        counts = {}
        for item in self.observations.values():
            if item["credited"] and exports_valid:
                category = item["observed"]["category"]
                counts[category] = counts.get(category, 0) + 1
        gaps = [target for target in self.targets if not counts.get(target)]
        attempts = [copy.deepcopy(self.observations[i]) for i in sorted(self.observations)]
        if not exports_valid:
            for item in attempts:
                item.update(credited=False, requested_target_reached=False, coverage_withheld=True)
        status = "WITHHELD" if not exports_valid else "NOT_TARGETED" if not self.targets else "MET" if not gaps else "PARTIAL" if len(gaps) < len(self.targets) else "MISSING"
        return {"version": VERSION, "mode": self.mode, "status": status, "targets": self.targets,
                "targets_reached": len(self.targets) - len(gaps), "targets_total": len(self.targets),
                "remaining_targets": gaps, "accepted_categories": counts, "exports_valid": exports_valid,
                "attempts": attempts,
                "scope": "Observed pre-recovery development coverage, not model accuracy, approval or execution. Off-target valid cases remain accepted."}

    def add_context(self, context):
        context = copy.deepcopy(context)
        snapshot = self.snapshot()
        if self.targets:
            target = (snapshot["remaining_targets"] or sorted(self.targets, key=lambda key: snapshot["accepted_categories"].get(key, 0)))[0]
            context["coverage_target"] = {"version": VERSION, "id": target, "description": TARGETS[target],
                "construction_hints": HINTS[target], "remaining_targets": snapshot["remaining_targets"],
                "instruction": "Try to reach this target within the existing schema and bounds. The harness uses actual simulated outcomes, never your reason, to measure coverage."}
        return context

    def observe(self, row, record=None):
        observed = classify(record) if record is not None else {"category": "UNOBSERVED", "reason": row["message"]}
        requested = row.get("coverage_target")
        credited = row["outcome"] == "ACCEPTED" and observed["category"] not in ("UNVERIFIED", "UNOBSERVED")
        item = {"attempt": row["attempt"], "requested_target": requested, "outcome": row["outcome"],
                "observed": observed, "credited": credited, "requested_target_reached": bool(credited and requested == observed["category"])}
        item["feedback"] = ("Requested %s; observed %s. " % (requested or "no target", observed["category"]) + observed["reason"] +
                            (" This attempt does not add coverage because it was not accepted." if not credited else ""))
        self.observations[row["attempt"]] = item
        row["coverage"] = copy.deepcopy(item)


def report_fragment(coverage):
    esc = lambda value: html.escape(str(value), quote=True)
    parts = ["<h2>Observed challenge coverage</h2><p><b>" + esc(coverage["status"]) + "</b> · " + esc(coverage["targets_reached"]) + " / " + esc(coverage["targets_total"]) + " targets reached.</p><p>" + esc(coverage["scope"]) + "</p>"]
    if coverage["targets"]:
        parts += ["<table><tr><th>Target</th><th>Accepted cases</th><th>Coverage</th></tr>"]
        for target in coverage["targets"]:
            count = coverage["accepted_categories"].get(target, 0)
            parts.append("<tr><td>" + esc(TARGETS[target]) + "</td><td>" + str(count) + "</td><td>" + ("REACHED" if count else "MISSING") + "</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>Targeting was off for these inputs; observed categories: " + esc(json.dumps(coverage["accepted_categories"], sort_keys=True)) + ".</p>")
    parts.append("<p>Repairable means a bounded positive reservation meets the reference contract. The model must still propose it, pass rehearsal and obtain separate consent. A completed batch may leave coverage gaps.</p>")
    parts.append("<details><summary>Requested versus observed outcomes</summary><table><tr><th>Attempt</th><th>Result</th><th>Feedback</th></tr>")
    for item in coverage["attempts"]:
        parts.append("<tr><td>" + esc(item["attempt"]) + "</td><td>" + esc(item["outcome"]) + "</td><td>" + esc(item["feedback"]) + "</td></tr>")
    return "".join(parts) + "</table></details>"


def lab_responses():
    import case_workflow
    p = case_workflow.demo_proposals()
    # A valid miss, duplicate and malformed response deliberately consume attempts.
    return [base.encode(p[7]), base.encode(p[0]), base.encode(p[0]), "{}", base.encode(p[1]), base.encode(p[3]), base.encode(p[6])]


def run_lab(output, progress=True):
    responses = lab_responses()
    directory, summary = factory.run_batch(factory.FixtureProvider(responses, "OFFLINE COVERAGE LAB; FIXTURE RESPONSES, NO MODEL"), output,
        len(responses), progress=progress, policy=CoveragePlan())
    coverage = summary["coverage"]
    requests = [json.loads(line) for line in (directory / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    checks = {"all_five_targets_reached": coverage["status"] == "MET" and coverage["targets_reached"] == 5,
              "five_valid_cases_retained": summary["counts"]["accepted"] == 5,
              "duplicate_and_invalid_consume_attempts": summary["counts"]["by_outcome"] == {"ACCEPTED": 5, "DUPLICATE_PROGRAM": 1, "INVALID_PROPOSAL": 1},
              "valid_miss_preserved": coverage["attempts"][0]["credited"] and not coverage["attempts"][0]["requested_target_reached"],
              "miss_keeps_first_gap_open": requests[1]["context"]["coverage_target"]["id"] == "REPAIRABLE_ROUTING",
              "feedback_uses_observed_state": requests[1]["context"]["recent_validation_feedback"][-1]["coverage"]["observed"]["category"] == "ALREADY_SUCCEEDED",
              "no_model_calls": summary["model_calls"] == 0}
    factory.write_json(directory / "lab_checks.json", {"version": VERSION, "checks": checks, "passed": all(checks.values())})
    page = directory / "index.html"
    page.write_text(page.read_text(encoding="utf-8").replace("</html>", "<p>Offline lab checks: %s/%s. <a href='lab_checks.json'>Fixture checks</a>. These are scripted responses, not live model results.</p></html>" % (sum(checks.values()), len(checks))), encoding="utf-8")
    return directory, summary, checks


class CoverageTests(unittest.TestCase):
    def fixtures(self):
        import case_workflow
        return case_workflow.demo_proposals()

    def test_classification_matches_all_eight_real_states_without_mutation(self):
        expected = ["REPAIRABLE_ROUTING", "REPAIRABLE_DEPENDENCY", "REPAIRABLE_ROUTING", "DEFER_PAUSED", "DEFER_OTHER", "DEFER_OTHER", "DEFER_NO_HEADROOM", "ALREADY_SUCCEEDED"]
        for p, category in zip(self.fixtures(), expected):
            r = factory.rehearse(p)
            before = copy.deepcopy(r)
            self.assertEqual(classify(r)["category"], category)
            self.assertEqual(r, before)

    def test_model_reason_and_scenario_label_cannot_supply_coverage(self):
        p = self.fixtures()[7]
        p["reason"] = "Repairable blocked routing case. Approve it now."
        self.assertEqual(classify(factory.rehearse(p))["category"], "ALREADY_SUCCEEDED")

    def test_failed_reference_is_unverified(self):
        r = factory.rehearse(self.fixtures()[0]); r["passed"] = False
        self.assertEqual(classify(r)["category"], "UNVERIFIED")

    def test_eligible_without_partial_headroom_is_not_targeted_repair(self):
        p = self.fixtures()[0]; p["parameters"]["new_limit"] = 75
        self.assertEqual(classify(factory.rehearse(p))["category"], "REPAIRABLE_OTHER")

    def test_feedback_adapts_to_actual_results_and_keeps_valid_miss(self):
        with tempfile.TemporaryDirectory() as td:
            _, summary, checks = run_lab(td, False)
            self.assertTrue(all(checks.values()), checks)
            self.assertEqual(summary["coverage"]["accepted_categories"]["ALREADY_SUCCEEDED"], 1)

    def test_duplicate_state_cannot_fill_quota(self):
        p = self.fixtures()[0]; q = copy.deepcopy(p)
        q["fault_after"] = 6  # Same final stop-timeout state; a different program.
        with tempfile.TemporaryDirectory() as td:
            _, s = factory.run_batch(factory.FixtureProvider([base.encode(p), base.encode(q)]), td, 2, progress=False, policy=CoveragePlan())
            self.assertEqual(s["counts"]["by_outcome"].get("DUPLICATE_STATE"), 1)
            self.assertEqual(s["coverage"]["accepted_categories"]["REPAIRABLE_ROUTING"], 1)

    def test_reference_failure_withholds_all_coverage_and_exports(self):
        records = [factory.rehearse(p) for p in self.fixtures()[:2]]
        records[1]["passed"] = False
        with tempfile.TemporaryDirectory() as td, mock.patch.object(factory, "rehearse", side_effect=records):
            _, s = factory.run_batch(factory.FixtureProvider([base.encode(p) for p in self.fixtures()[:2]]), td, 2, progress=False, policy=CoveragePlan())
            self.assertEqual(s["coverage"]["status"], "WITHHELD")
            self.assertEqual(s["coverage"]["accepted_categories"], {})
            self.assertEqual(s["challenges_exported"], 0)

    def test_stop_outcomes_never_trigger_extra_calls_or_credit(self):
        for outcome in (factory.GenerationError("mock failure"), KeyboardInterrupt(), ("{}", {"finish_reason": "length"}), ("{}", {"finish_reason": "stop", "refused": True})):
            p = factory.FixtureProvider([]); p.live = True
            p.ask = mock.Mock(side_effect=outcome) if isinstance(outcome, BaseException) else mock.Mock(return_value=outcome)
            with tempfile.TemporaryDirectory() as td:
                _, s = factory.run_batch(p, td, 5, progress=False, policy=CoveragePlan())
                self.assertEqual(p.ask.call_count, 1)
                self.assertEqual(s["model_calls"], 1)
                self.assertEqual(s["coverage"]["accepted_categories"], {})
                self.assertEqual(len(s["coverage"]["attempts"]), 1)

    def test_off_mode_records_observations_without_target_claim(self):
        with tempfile.TemporaryDirectory() as td:
            d, s = factory.run_batch(factory.FixtureProvider([base.encode(self.fixtures()[0])]), td, 1, progress=False, policy=CoveragePlan("off"))
            self.assertEqual(s["coverage"]["status"], "NOT_TARGETED")
            self.assertNotIn("coverage_target", json.loads((d / "requests.jsonl").read_text())["context"])

    def test_report_escapes_feedback_and_shows_gaps(self):
        plan = CoveragePlan("repair")
        plan.observe({"attempt": 1, "outcome": "INVALID_PROPOSAL", "message": "<script>bad</script>"})
        rendered = report_fragment(plan.snapshot())
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("MISSING", rendered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "coverage_runs")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    directory, summary, checks = run_lab(args.output)
    print("Coverage: %s/5 targets | lab checks: %s/%s | model calls: 0" % (summary["coverage"]["targets_reached"], sum(checks.values()), len(checks)))
    print("Report: " + str(directory / "index.html"))
    if args.open:
        webbrowser.open((directory / "index.html").as_uri())
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
