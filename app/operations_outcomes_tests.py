"""Outcome evidence: unknowns, attribution, integrity, comparison and local integration."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import operations_outcomes as reports
import operations_workspace as ops
import simulator as sim

ROOT = Path(__file__).resolve().parent


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.summary = json.loads((ROOT / "OPERATIONS_DEMO.json").read_text(encoding="utf-8"))["summary"]
        self.summary["historical_operations"] = self.summary["operations"]["source"] != ops.source_digest()

    def values(self, summary=None):
        return {m["key"]: m["value"] for m in reports.from_summary(summary or self.summary)["metrics"]}

    def test_saved_completed_demo_outcomes_have_specific_independent_expectations(self):
        values = self.values()
        self.assertEqual({k: values[k] for k in ("submitted", "completed", "waiting")},
                         {"submitted": 5, "completed": 4, "waiting": 1})
        self.assertEqual(values["dependencies_accounted"], 1)
        self.assertEqual(values["dependencies_unconfirmed"], 0)
        self.assertEqual(values["owner_releases"], 1)
        self.assertEqual(values["handoff_successful"], 20)
        self.assertEqual(values["router_probes"], 60)
        self.assertEqual(values["router_successful"], 60)
        self.assertEqual(values["candidate_failed_probes"], 20)
        self.assertEqual(values["live_role_attempts"], 0)
        self.assertEqual(values["role_tokens"], 0)

    def test_no_model_calls_no_runtime_effects_and_original_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / self.summary["mission_id"]; path.mkdir()
            raw = json.loads((ROOT / "OPERATIONS_DEMO.json").read_text(encoding="utf-8"))["summary"]
            raw.pop("historical_operations", None); raw.pop("lease_elapsed", None)
            (path / "summary.json").write_text(json.dumps(raw), encoding="utf-8")
            before = (path / "summary.json").read_bytes()
            with mock.patch.object(ops, "opened", side_effect=AssertionError("must not open runtime")):
                first, second = reports.build(path), reports.build(path)
            self.assertEqual(first, second)
            self.assertEqual((path / "summary.json").read_bytes(), before)
            self.assertFalse(first["effects"])
            self.assertEqual(first["model_calls_to_build_report"], 0)

    def test_tampered_summary_rejected_before_reporting(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / self.summary["mission_id"]; path.mkdir()
            self.summary["phase"] = "fabricated"
            (path / "summary.json").write_text(json.dumps(self.summary), encoding="utf-8")
            with self.assertRaises(ValueError): reports.build(path)

    def test_changed_health_digest_is_rejected(self):
        self.summary["health"][0]["samples"]["router"][0]["ok"] = False
        with self.assertRaises(ValueError): reports.from_summary(self.summary)

    def test_changed_plan_digest_is_rejected(self):
        self.summary["service"]["plans"][-1]["reason"] = "changed"
        with self.assertRaises(ValueError): reports.from_summary(self.summary)

    def test_handoff_without_both_grants_is_rejected(self):
        del self.summary["operations"]["emergency"]["grants"]["owner_C"]
        with self.assertRaises(ValueError): reports.from_summary(self.summary)

    def test_handoff_counts_must_match_recorded_event(self):
        self.summary["operations"]["emergency"]["availability"]["successful"] = 999
        with self.assertRaises(ValueError): reports.from_summary(self.summary)

    def test_pending_release_is_not_counted_as_confirmed_customer_work(self):
        self.summary["operations"]["jobs"][0]["phase"] = "PENDING"
        values = self.values()
        self.assertEqual(values["owner_releases"], 0)
        self.assertEqual(values["dependencies_accounted"], 0)
        self.assertEqual(values["dependencies_unconfirmed"], 1)

    def test_unrelated_owner_release_cannot_account_for_missing_dependency(self):
        job = self.summary["operations"]["jobs"][0]
        job["principal"]["roles"] = ["manager"]
        self.assertEqual(self.values()["dependencies_unconfirmed"], 1)

    def test_missing_release_receipt_remains_unconfirmed(self):
        events = self.summary["service"]["policy_snapshot"]["events"]
        events[:] = [e for e in events if e.get("kind") != "OPERATIONS_C_JOB_RELEASE"]
        self.assertEqual(self.values()["dependencies_unconfirmed"], 1)

    def test_retained_dependency_counts_even_without_owner_release(self):
        self.summary["operations"]["jobs"] = []
        self.summary["service"]["policy_snapshot"]["state"]["deployments"]["candidate-v2"]["dependencies"] = [{"id": "C-mission-job", "owner_run": "route-C"}]
        result = reports.from_summary(self.summary)
        self.assertEqual(result["dependency_accounting"]["retained_from_start"], 1)
        self.assertEqual(result["dependency_accounting"]["unconfirmed"], 0)

    def test_pending_mission_does_not_claim_zero_failures_or_measured_recovery(self):
        self.summary.update(service=None, recovery_grant=None, recovery=None, incident=None, health=[], calls=[])
        self.summary["operations"].update(emergency=None, jobs=[])
        values = self.values()
        for key in ("submitted", "completed", "waiting", "dependencies_accounted", "dependencies_unconfirmed", "handoff_seconds", "handoff_successful", "router_p95_ms"):
            self.assertIsNone(values[key], key)

    def test_actual_role_metadata_tokens_are_counted_and_gateway_costs_excluded(self):
        for i, call in enumerate(self.summary["calls"]):
            call["live"] = True; call["metadata"] = {"total_tokens": [100, 200, 300][i]}
        self.assertEqual(self.values()["role_tokens"], 600)
        self.assertEqual(self.values()["live_role_attempts"], 3)

    def test_missing_live_usage_is_unknown_not_free_or_zero(self):
        for call in self.summary["calls"]: call["live"] = True
        self.summary["calls"][0]["metadata"] = {"total_tokens": 120}
        self.summary["calls"][1]["metadata"] = {"total_tokens": True}
        self.summary["calls"][2]["metadata"] = {"total_tokens": -1}
        result = reports.from_summary(self.summary)
        self.assertIsNone(self.values()["role_tokens"])
        self.assertEqual(result["role_usage"]["known_tokens_lower_bound"], 120)
        self.assertEqual(result["role_usage"]["attempts_missing_usage"], 2)

    def test_missing_latency_makes_p95_unknown_instead_of_dropping_slow_probe(self):
        w = self.summary["health"][0]; w["samples"]["router"][0].pop("latency_ms", None)
        w["digest"] = sim.digest({k: v for k, v in w.items() if k != "digest"})
        self.assertIsNone(self.values()["router_p95_ms"])

    def test_clock_anomalies_or_missing_event_are_unknown(self):
        self.summary["incident"]["created_at"] = "2100-01-01T00:00:00+00:00"
        self.assertIsNone(self.values()["handoff_seconds"])
        self.summary["incident"]["created_at"] = "2020-01-01T00:00:00"
        self.assertIsNone(self.values()["handoff_seconds"])
        self.summary["events"] = [e for e in self.summary["events"] if e["kind"] != "EMERGENCY_HANDOFF_CONFIRMED"]
        self.assertIsNone(self.values()["handoff_seconds"])

    def test_comparison_requires_distinct_stopped_runs_with_same_basis(self):
        one = reports.from_summary(self.summary); two = copy.deepcopy(one); two["run"] = "other"
        self.assertEqual(reports.compare(one, two)["status"], "DESCRIPTIVE_COMPARISON")
        self.assertEqual(reports.compare(one, one)["status"], "NOT_COMPARABLE")
        for key, value in (("platform", "different"), ("proposal_mode", True), ("workflow_digest", "other"),
                           ("compiled_requests_digest", "different"), ("settings_digest", "different"),
                           ("approval_modes", ["NAMED_LOCAL_ACCOUNT"]), ("faults", ["other"])):
            changed = copy.deepcopy(two); changed["comparison_basis"][key] = value
            result = reports.compare(one, changed)
            self.assertEqual(result["status"], "NOT_COMPARABLE", key); self.assertEqual(result["metrics"], [])
        two["lifecycle"] = "LEASED"
        self.assertEqual(reports.compare(one, two)["status"], "NOT_COMPARABLE")

    def test_comparison_requires_fault_and_preserves_unknown_deltas(self):
        one = reports.from_summary(self.summary); two = copy.deepcopy(one); two["run"] = "other"
        next(m for m in two["metrics"] if m["key"] == "role_tokens")["value"] = None
        self.assertIsNone(next(m for m in reports.compare(one, two)["metrics"] if m["key"] == "role_tokens")["delta"])
        one["comparison_basis"]["faults"] = []; two["comparison_basis"]["faults"] = []
        self.assertEqual(reports.compare(one, two)["status"], "NOT_COMPARABLE")

    def test_portable_report_escapes_untrusted_values_and_excludes_prompts_and_credentials(self):
        report = reports.from_summary(self.summary); report["report_digest"] = sim.digest(report)
        report["run"] = '<img src=x onerror="alert(1)">'
        rendered = reports.render(report)
        self.assertIn("&lt;img", rendered); self.assertNotIn("<img", rendered)
        self.assertNotIn("<script", rendered)
        self.assertNotIn(self.summary["calls"][0]["system"], rendered)
        self.assertNotIn("principal", json.dumps(report))
        with tempfile.TemporaryDirectory() as root:
            path = reports.export(root, report)
            self.assertEqual(path.read_text(encoding="utf-8"), rendered)
            self.assertEqual(json.loads(path.with_suffix(".json").read_text()), report)

    def test_real_demo_report_cli_comparison_and_read_only_source_evidence(self):
        with tempfile.TemporaryDirectory(prefix="outcome evidence ") as root:
            first, _ = ops.demo(Path(root)); second, _ = ops.demo(Path(root))
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for path in (first, second) for p in path.rglob("*") if p.is_file()}
            report = reports.build(second, first)
            self.assertEqual(report["comparison"]["status"], "DESCRIPTIVE_COMPARISON")
            self.assertFalse(report["evidence"]["historical_source"])
            self.assertEqual(ops.main(["outcome", "--output", root, "--run", second.name, "--baseline", first.name]), 0)
            self.assertTrue((second / "outcome_report.html").is_file())
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_export_rejects_nonfile_destination_before_writing_json(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "outcome_report.html").mkdir()
            with self.assertRaises(ValueError): reports.export(root, {})
            self.assertFalse((Path(root) / "outcome_report.json").exists())
