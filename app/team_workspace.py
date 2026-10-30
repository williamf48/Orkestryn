#!/usr/bin/env python3
"""Saved three-agent evidence and bounded, simulated owner-review branches.

No provider requests or worker operations are exposed here. A completed team
run has already torn down its workers. A branch copies only the saved policy
state, journal and receipts into a fresh simulator database. It never resumes
the old runtime, treats its measurements as fresh, or grants live authority.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

import agent_team as team
import restart_checker
import review_workflow as review
import simulator as sim

VERSION = "agent-rehearsal-team-workspace-v1"
MODE = "SIMULATED SNAPSHOT RECOVERY REHEARSAL; ZERO MODEL CALLS; NO LIVE EFFECTS"
ROOTS = {"team": "agent_team_runs", "team_review": "team_review_runs"}
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 5000
TERMINAL = {"RECOVERED", "STOPPED_FOR_REVIEW", "INTERRUPTED", "CONSENT_DECLINED",
            "EXECUTION_BLOCKED", "RECOVERY_DEFERRED", "RECONCILIATION_REQUIRED",
            "NOT_APPLICABLE_ORIGINAL_SUCCEEDED"}


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Invalid team evidence identifier.")
    return value


def path_for(root, kind, run=None, *parts):
    root = Path(root).resolve()
    if kind not in ROOTS:
        raise ValueError("Choose team evidence or a snapshot review branch.")
    path = root / ROOTS[kind]
    if run is not None:
        path = path / component(run)
    for part in parts:
        path = path / part
    if path.resolve() != path or root not in path.parents:
        raise ValueError("Team evidence must stay in its own folder without symbolic links.")
    return path


def reject_constant(_):
    raise ValueError("Non-finite values are not supported in team evidence.")


def decode(value):
    return json.loads(value, parse_constant=reject_constant)


def read_bytes(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Team evidence exceeds the 32 MiB read limit.")
    return path.read_bytes()


def read_json(path):
    return decode(read_bytes(path).decode("utf-8-sig"))


def write_json(path, value):
    """Publish one complete version without sharing a writer's pending file.

    Windows can briefly deny atomic replacement while a reader or scanner has
    the destination open without delete sharing. Retry only that same file
    replacement, within two seconds. Persistent and unrelated errors propagate;
    never truncate/delete the last good destination or repeat a policy action.
    """
    path = Path(path)
    raw = json.dumps(value, indent=2, allow_nan=False) + "\n"
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 2
        while True:
            try:
                pending.replace(path)
                return
            except PermissionError as exc:
                remaining = deadline - time.monotonic()
                if getattr(exc, "winerror", None) not in {5, 32, 33} or remaining <= 0:
                    raise
                time.sleep(min(.025, remaining))
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def metrics(state):
    allocated = sum(d["allocated_units"] for d in state.get("deployments", {}).values())
    pending = sum(r["units"] for r in state.get("reservations", {}).values() if r["status"] == "PENDING")
    return {"stable": state.get("stable_units", 0), "allocated": allocated, "pending": pending,
            "committed": state.get("stable_units", 0) + allocated + pending,
            "limit": state.get("capacity_limit"), "routing": state.get("routing", {}),
            "route_owner": state.get("route_owner"), "route_version": state.get("route_version")}


def validate_snapshot(snapshot):
    """Validate every recorded before image as well as final state and receipts."""
    current = sim.initial_state()
    for index, event in enumerate(snapshot["events"], 1):
        if event.get("sequence") != index:
            raise ValueError("The team journal has a missing or reordered event.")
        for field, change in event.get("changes", {}).items():
            if not isinstance(change, dict) or set(change) != {"before", "after"} or current.get(field) != change["before"]:
                raise ValueError("The team journal has an inconsistent before image.")
            current[field] = copy.deepcopy(change["after"])
        if event.get("kind") == "RECOVERY_STARTED" and event.get("run_id") not in current["paused_runs"]:
            current["paused_runs"].append(event["run_id"])
        sim.Engine.check_invariants(current)
    checks = restart_checker.audit_journal(snapshot)
    if current != snapshot["state"] or not all(checks.values()):
        raise ValueError("The team database, journal or receipts disagree.")
    return checks


def snapshot_from_db(path):
    read_bytes(path)  # Enforce the size bound before SQLite opens the file.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"service_state", "events", "action_receipts"}.issubset(tables):
            raise ValueError("The selected file is not a team policy database.")
        state = decode(db.execute("SELECT payload FROM service_state WHERE id=1").fetchone()[0])
        events = list(db.execute("SELECT sequence,event FROM events ORDER BY sequence LIMIT ?", (MAX_ROWS + 1,)))
        receipts = list(db.execute("SELECT action_key,fingerprint,result FROM action_receipts LIMIT ?", (MAX_ROWS + 1,)))
        if max(len(events), len(receipts)) > MAX_ROWS:
            raise ValueError("This journal exceeds the bounded workspace review limit.")
        snapshot = {"state": state, "events": [{"sequence": seq, **decode(event)} for seq, event in events],
                    "receipts": {key: {"fingerprint": fingerprint, "result": decode(result)} for key, fingerprint, result in receipts}}
        validate_snapshot(snapshot)
        return snapshot
    finally:
        db.close()


def options(state, scenario, recovery):
    if recovery.get("status") != "RECOVERY_REVIEW_REQUIRED" or "deploy-A" not in state.get("paused_runs", []):
        return [], "There is no unresolved paused recovery in this snapshot."
    candidate = state.get("deployments", {}).get("candidate-v2", {})
    if candidate.get("owner_run") != "deploy-A" or candidate.get("allocated_units", 0) <= 0:
        return [], "No retained A-owned candidate remains for this review."
    policy = sim.Engine(":memory:")
    try:
        tool = "set_route" if scenario == "routing" else "add_dependency"
        plan = policy._approval(state, "C", "route-C", "AP-route-C", tool)
        if plan.get("candidate") != "candidate-v2" or (scenario == "dependency" and not plan.get("can_depend")):
            return [], "C's recorded approval does not cover this candidate."
    except sim.Denied as exc:
        return [], exc.code + ": " + exc.reason
    finally:
        policy.close()
    decisions = ["retain"]
    if scenario == "routing" and state.get("route_owner") == "route-C" and state["routing"].get("candidate-v2", 0) > 0:
        decisions.append("withdraw_route")
    if scenario == "dependency" and {"id": review.DEPENDENCY, "owner_run": "route-C"} in candidate.get("dependencies", []):
        decisions.append("release_dependency")
    return decisions, "All ownership, approval, fallback and cleanup gates are checked again in the new simulation branch."


def load_source(root, kind, run):
    """Return a closed, internally consistent source; no source writes occur."""
    files = {}
    names = ("summary.json", "coordination.json", "service/report.json") if kind == "team" else ("summary.json", "report.json")
    documents = {}
    for name in names:
        path = path_for(root, kind, run, *name.split("/"))
        raw = read_bytes(path)
        files[name] = hashlib.sha256(raw).hexdigest()
        documents[name] = decode(raw.decode("utf-8-sig"))
    summary = documents["summary.json"]
    if kind == "team":
        report = documents["service/report.json"]
        ledger = documents["coordination.json"]
        if (summary.get("version") != team.VERSION or summary.get("status") != "RECOVERED"
                or summary.get("full_three_role_flow_completed") is not True
                or not summary.get("checks") or not all(v is True for v in summary["checks"].values())
                or summary.get("handoffs") != ledger or not team.Ledger.valid(ledger)
                or report.get("teardown", {}).get("all_workers_stopped") is not True
                or report.get("checks_passed") is not True):
            raise ValueError("Finish a consistent three-role team run before creating a snapshot review.")
        db_path = path_for(root, kind, run, "service", "coordinator.sqlite3")
        state = report["state"]
        outcome = summary.get("outcome", {})
        if (report.get("stage") != summary["status"] or outcome.get("stage") != summary["status"]
                or outcome.get("recovery_status") != report.get("recovery", {}).get("status")
                or outcome.get("committed_units") != sim.total_commitments(state)
                or outcome.get("capacity_limit") != state["capacity_limit"]
                or outcome.get("harness_teardown") != report.get("teardown")):
            raise ValueError("The team outcome and saved service state disagree.")
        root_run = run
    else:
        report = documents["report.json"]
        if (report.get("version") != VERSION or report.get("mode") != MODE
                or summary != branch_summary(report) or not all(c["passed"] is True for c in report["assertions"])):
            raise ValueError("The saved snapshot branch is incomplete or inconsistent.")
        db_path = path_for(root, kind, run, "state.sqlite3")
        state = report["final_state"]
        root_run = component(report["root_team_run"])
    snapshot = snapshot_from_db(db_path)
    if state != snapshot["state"] or report["events"] != snapshot["events"]:
        raise ValueError("The saved report does not match its policy database.")
    scenario = summary.get("scenario")
    if scenario not in {"routing", "dependency"}:
        raise ValueError("Unsupported team scenario for snapshot review.")
    recovery = report.get("recovery", {})
    decisions, reason = options(state, scenario, recovery)
    digest = sim.digest({"kind": kind, "run": run, "files": files, "snapshot": snapshot})
    return {"kind": kind, "id": run, "scenario": scenario, "summary": summary, "report": report,
            "snapshot": snapshot, "source_digest": digest, "file_sha256": files,
            "available_decisions": decisions, "review_reason": reason, "root_team_run": root_run}


def team_view(root, run):
    """Tolerate partial snapshots while a separate CLI is writing a run."""
    summary = read_json(path_for(root, "team", run, "summary.json"))
    if not isinstance(summary, dict):
        raise ValueError("Expected a saved team summary object.")
    warnings, report, ledger = [], None, []
    for label, parts in (("coordination", ("coordination.json",)), ("service", ("service", "report.json"))):
        try:
            value = read_json(path_for(root, "team", run, *parts))
            if label == "coordination":
                if not isinstance(value, list) or not team.Ledger.valid(value):
                    raise ValueError("Coordination hash links do not agree.")
                ledger = value
                if summary.get("handoffs") != ledger:
                    warnings.append("The handoff and summary snapshots differ; refresh after the current write.")
            elif not isinstance(value, dict):
                raise ValueError("Expected a service report object.")
            else:
                report = value
        except (OSError, ValueError, TypeError, KeyError) as exc:
            warnings.append(label.capitalize() + " evidence is not ready or consistent (" + type(exc).__name__ + ").")
    review_info = {"source_digest": None, "available_decisions": [], "reason": "Complete the run before reviewing a saved snapshot."}
    if summary.get("status") == "RECOVERED":
        try:
            source = load_source(root, "team", run)
            review_info = {"source_digest": source["source_digest"], "available_decisions": source["available_decisions"],
                           "reason": source["review_reason"]}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError) as exc:
            warnings.append("Snapshot review is disabled: " + str(exc))
    state = report.get("state", {}) if report else {}
    initial = report.get("rehearsal", {}).get("before", {}) if report else {}
    normalized = {"initial_state": sim.initial_state(), "final_state": state or None,
                  "events": report.get("events", []) if report else [], "mode": summary.get("mode"),
                  "scenario": summary.get("scenario"), "task_outcome": summary.get("status"),
                  "recovery_outcome": (report or {}).get("recovery", {}).get("status"),
                  "recovery": (report or {}).get("recovery"),
                  "assertions": [{"assertion": key.replace("_", " "), "passed": value}
                                 for key, value in (report or {}).get("checks", {}).items()]}
    return {"kind": "team", "id": run, "summary": summary, "service": report, "handoffs": ledger,
            "warnings": warnings, "active": summary.get("status") not in TERMINAL and summary.get("full_three_role_flow_completed") is not False,
            "metrics": metrics(state), "before_metrics": metrics(initial), "report": normalized,
            "calls": [], "review": review_info,
            "scope": "Saved local worker/HTTP evidence with fixture capacity and identities. This view does not certify provider accuracy or resume the runtime."}


def branch_summary(report):
    return {key: report[key] for key in ("version", "mode", "created_at", "scenario", "root_team_run",
            "parent", "task_outcome", "recovery_outcome", "model_calls", "review")}


def seed_branch(path, snapshot):
    """Copy known policy data only; no runtime bindings or live effect intents."""
    engine = review.ReviewEngine(path, create=True)
    try:
        engine.db.execute("BEGIN IMMEDIATE")
        engine.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(snapshot["state"]),))
        for event in snapshot["events"]:
            engine.db.execute("INSERT INTO events(sequence,event) VALUES (?,?)",
                              (event["sequence"], sim.canonical({k: v for k, v in event.items() if k != "sequence"})))
        for key, receipt in snapshot["receipts"].items():
            engine.db.execute("INSERT INTO action_receipts VALUES (?,?,?)", (key, receipt["fingerprint"], sim.canonical(receipt["result"])))
        engine.db.execute("COMMIT")
        return engine
    except BaseException:
        if engine.db.in_transaction:
            engine.db.execute("ROLLBACK")
        engine.close()
        raise


def resolve(root, kind, run, source_digest, decision, note, confirm_snapshot=False):
    if confirm_snapshot is not True:
        raise ValueError("Confirm that this is a simulation of the saved snapshot, with no live effect.")
    if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise ValueError("Use the displayed snapshot digest.")
    if not isinstance(decision, str) or decision not in review.DECISIONS or not isinstance(note, str) or not note.strip() or len(note) > 300:
        raise ValueError("Choose a bounded owner decision and record a reason of 1 to 300 characters.")
    source = load_source(root, kind, component(run))
    if source["source_digest"] != source_digest:
        raise ValueError("The source changed after it was displayed. Refresh and review the current snapshot.")
    if decision not in source["available_decisions"]:
        raise ValueError("This decision is outside the current owner's available review scope. " + source["review_reason"])
    base = path_for(root, "team_review")
    base.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = path_for(root, "team_review", name)
    directory.mkdir(exist_ok=False)
    engine = seed_branch(directory / "state.sqlite3", source["snapshot"])
    try:
        before = engine.state()
        cutoff = len(engine.events())
        parent = {"kind": kind, "run": run, "source_digest": source_digest, "file_sha256": source["file_sha256"]}
        engine._event({"kind": "WORKSPACE_SNAPSHOT_FORKED", "mode": MODE, "parent": parent, "changes": {},
                       "reason": "Fresh simulator policy branch. Original HTTP measurements and worker receipts are historical; no runtime is resumed."})
        result = engine.decide(source["scenario"] + "_conflict", decision, note, review.state_digest(engine),
                               "workspace-" + sim.digest({"parent": parent, "decision": decision, "note": note})[:24])
        recovery = source["report"]["recovery"]
        if result["status"] == "SUCCEEDED" and decision != "retain":
            recovery = engine.recover()
        final = engine.state()
        # A concurrent source update invalidates the result rather than rebasing it.
        if load_source(root, kind, run)["source_digest"] != source_digest:
            raise ValueError("The source changed during review. This unfinished branch has no accepted report; refresh.")
        remaining, reason = options(final, source["scenario"], recovery)
        expected_deps = copy.deepcopy(before["deployments"]["candidate-v2"]["dependencies"])
        if result["status"] == "SUCCEEDED" and decision == "release_dependency":
            expected_deps.remove({"id": review.DEPENDENCY, "owner_run": "route-C"})
        current = {"state": final, "events": engine.events(), "receipts": {key: {"fingerprint": fp, "result": decode(value)}
                   for key, fp, value in engine.db.execute("SELECT action_key,fingerprint,result FROM action_receipts")}}
        audit = validate_snapshot(current)
        assertions = [("B's capacity, budget version, plan and approval stay unchanged", all(final[k] == before[k] for k in ("capacity_limit", "budget_version"))
                       and final["plans"]["cost-B"] == before["plans"]["cost-B"] and final["approvals"]["AP-cost-B"] == before["approvals"]["AP-cost-B"]),
                      ("Stable allocation stays unchanged", final["stable_units"] == before["stable_units"]),
                      ("The failed workflow stays paused", "deploy-A" in final["paused_runs"]),
                      ("Every unselected dependency is preserved", final["deployments"]["candidate-v2"]["dependencies"] == expected_deps),
                      ("A retained decision makes no policy state change", decision != "retain" or final == before),
                      ("The source journal prefix stays unchanged", current["events"][:cutoff] == source["snapshot"]["events"]),
                      *[(key.replace("_", " "), value) for key, value in audit.items()]]
        report = {"version": VERSION, "mode": MODE, "created_at": datetime.now(timezone.utc).isoformat(),
                  "scenario": source["scenario"], "root_team_run": source["root_team_run"], "parent": parent,
                  "source_state": before, "branch_event_start": cutoff + 1, "initial_state": sim.initial_state(),
                  "final_state": final, "events": current["events"], "recovery": recovery,
                  "recovery_outcome": recovery["status"], "task_outcome": "SIMULATED_OWNER_DECISION_RECORDED",
                  "model_calls": 0, "worker_processes_started": 0, "live_effects": False,
                  "physical_stop_confirmed_by_this_branch": False,
                  "decision": {"choice": decision, "note": note, "result": result, "actor": "SIMULATED C / route-C"},
                  "assertions": [{"assertion": label, "passed": bool(passed)} for label, passed in assertions],
                  "review": {"available_decisions": remaining, "reason": reason, "state_digest": review.state_digest(engine)},
                  "scope": "New simulated policy branch from saved evidence. Newly recorded stops/releases are simulated, with no fresh HTTP evidence or live authority. The source team run stays unchanged."}
        if not all(c["passed"] for c in report["assertions"]):
            raise ValueError("The review branch failed a preservation or journal check; no accepted report was saved.")
        write_json(directory / "report.json", report)
        write_json(directory / "summary.json", branch_summary(report))
        return directory, report
    finally:
        engine.close()


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import contextlib
        import io
        cls.fixture = tempfile.TemporaryDirectory()
        cls.sources = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for scenario in ("dependency", "routing"):
                directory, summary = team.run_team(Path(cls.fixture.name) / ROOTS["team"], scenario=scenario)
                if not summary["full_three_role_flow_completed"]:
                    raise AssertionError("Workspace source fixture did not complete: " + str(summary))
                cls.sources[scenario] = directory

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        import shutil
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.runs = {}
        for scenario, source in self.sources.items():
            destination = self.root / ROOTS["team"] / source.name
            shutil.copytree(source, destination)
            self.runs[scenario] = source.name

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, scenario="dependency"):
        return load_source(self.root, "team", self.runs[scenario])

    def decide(self, decision, scenario="dependency", source=None):
        source = source or self.load(scenario)
        return resolve(self.root, source["kind"], source["id"], source["source_digest"], decision,
                       "Explicit local snapshot rehearsal decision.", True)

    def amend_fixture(self, mutate, scenario="dependency"):
        directory = self.root / ROOTS["team"] / self.runs[scenario]
        engine = sim.Engine(directory / "service" / "coordinator.sqlite3", create=False)
        try:
            before, after = engine.state(), engine.state()
            mutate(after)
            engine.db.execute("UPDATE service_state SET payload=? WHERE id=1", (sim.canonical(after),))
            engine._event({"kind": "WORKSPACE_TEST_FIXTURE_CHANGE", "changes": {
                k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]}})
            report = read_json(directory / "service" / "report.json")
            report.update(state=after, events=engine.events(), committed_units=sim.total_commitments(after))
            write_json(directory / "service" / "report.json", report)
            summary = read_json(directory / "summary.json")
            summary["outcome"].update(committed_units=report["committed_units"], capacity_limit=after["capacity_limit"])
            write_json(directory / "summary.json", summary)
        finally:
            engine.close()

    def test_team_view_separates_real_outcome_teardown_and_recovery(self):
        view = team_view(self.root, self.runs["routing"])
        self.assertFalse(view["active"])
        self.assertFalse(view["warnings"])
        self.assertEqual(view["metrics"]["routing"]["candidate-v2"], 20)
        self.assertEqual(view["before_metrics"]["committed"], 40)
        self.assertTrue(view["service"]["actual_at_outcome"]["workers"]["candidate-v2"]["alive"])
        self.assertTrue(view["service"]["teardown"]["all_workers_stopped"])
        self.assertEqual(view["report"]["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(view["review"]["available_decisions"], ["retain", "withdraw_route"])

    def test_both_owner_choices_recover_in_simulation_without_changing_any_source_bytes(self):
        for scenario, decision in (("dependency", "release_dependency"), ("routing", "withdraw_route")):
            with self.subTest(scenario=scenario):
                directory = self.root / ROOTS["team"] / self.runs[scenario]
                before = {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob("*") if p.is_file()}
                branch, report = self.decide(decision, scenario)
                after = {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob("*") if p.is_file()}
                self.assertEqual(before, after)
                self.assertEqual(report["recovery_outcome"], "VERIFIED_RECOVERED")
                self.assertEqual(metrics(report["final_state"])["committed"], 30)
                self.assertEqual((report["model_calls"], report["worker_processes_started"], report["live_effects"]), (0, 0, False))
                self.assertFalse(report["physical_stop_confirmed_by_this_branch"])
                self.assertTrue(all(c["passed"] for c in report["assertions"]))
                source = load_source(self.root, "team_review", branch.name)
                self.assertEqual(source["available_decisions"], [])
                db = sqlite3.connect(branch / "state.sqlite3")
                try:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertFalse(tables & {"local_binding", "local_intents", "revisions"})
                finally:
                    db.close()

    def test_retention_and_followup_keep_parent_branches_immutable(self):
        branch, retained = self.decide("retain")
        self.assertEqual(retained["source_state"], retained["final_state"])
        original = (branch / "report.json").read_bytes()
        source = load_source(self.root, "team_review", branch.name)
        child, released = self.decide("release_dependency", source=source)
        self.assertNotEqual(branch, child)
        self.assertEqual((branch / "report.json").read_bytes(), original)
        self.assertEqual(released["parent"]["run"], branch.name)
        self.assertEqual(released["root_team_run"], self.runs["dependency"])
        self.assertEqual(released["recovery_outcome"], "VERIFIED_RECOVERED")

    def test_changed_source_digest_and_unconfirmed_or_out_of_scope_decisions_create_no_branch(self):
        source = self.load()
        path = self.root / ROOTS["team"] / source["id"] / "summary.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.decide("release_dependency", source=source)
        current = self.load()
        with self.assertRaisesRegex(ValueError, "Confirm"):
            resolve(self.root, "team", current["id"], current["source_digest"], "retain", "Reason", False)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.decide("withdraw_route", source=current)
        self.assertFalse((self.root / ROOTS["team_review"]).exists())

    def test_revoked_or_expired_owner_approval_disables_decisions(self):
        for mutate in (lambda s: s["approvals"]["AP-route-C"].update(revoked=True),
                       lambda s: s["approvals"]["AP-route-C"].update(expires_at=0)):
            self.amend_fixture(mutate)
            self.assertEqual(self.load()["available_decisions"], [])
            with self.assertRaisesRegex(ValueError, "outside"):
                self.decide("release_dependency")

    def test_wrong_owner_and_different_dependency_cannot_be_released(self):
        self.amend_fixture(lambda s: s["deployments"]["candidate-v2"].update(dependencies=[{"id": review.DEPENDENCY, "owner_run": "other-C"}]))
        self.assertEqual(self.load()["available_decisions"], ["retain"])
        with self.assertRaisesRegex(ValueError, "outside"):
            self.decide("release_dependency")
        self.amend_fixture(lambda s: s["deployments"]["candidate-v2"].update(dependencies=[{"id": "different-job", "owner_run": "route-C"}]))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.decide("release_dependency")

    def test_unselected_consumer_still_blocks_stop_after_c_releases_its_own_dependency(self):
        other = {"id": "unselected-job", "owner_run": "other-workflow"}
        self.amend_fixture(lambda s: s["deployments"]["candidate-v2"]["dependencies"].append(other))
        _, report = self.decide("release_dependency")
        self.assertEqual(report["decision"]["result"]["status"], "SUCCEEDED")
        self.assertEqual(report["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(report["final_state"]["deployments"]["candidate-v2"]["dependencies"], [other])
        self.assertEqual(metrics(report["final_state"])["committed"], 40)

    def test_unsafe_fallback_blocks_owner_withdrawal_without_policy_changes(self):
        self.amend_fixture(lambda s: s.update(stable_healthy=False), "routing")
        _, report = self.decide("withdraw_route", "routing")
        self.assertEqual(report["decision"]["result"]["code"], "FALLBACK_NOT_SAFE")
        self.assertEqual(report["source_state"], report["final_state"])
        self.assertEqual(report["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")

    def test_revoked_recovery_authority_cannot_be_replaced_by_owner_consent(self):
        self.amend_fixture(lambda s: s["approvals"]["AP-recovery-A"].update(revoked=True))
        _, report = self.decide("release_dependency")
        self.assertEqual(report["decision"]["result"]["status"], "SUCCEEDED")
        self.assertEqual(report["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(report["recovery"]["steps"][1]["result"]["code"], "APPROVAL_REVOKED")
        self.assertEqual(metrics(report["final_state"])["committed"], 40)

    def test_unknown_simulated_stop_outcome_retains_commitments(self):
        self.amend_fixture(lambda s: s["deployments"]["candidate-v2"].update(stop_timeout=True))
        _, report = self.decide("release_dependency")
        self.assertEqual(report["recovery_outcome"], "RECOVERY_REVIEW_REQUIRED")
        self.assertEqual(metrics(report["final_state"])["committed"], 40)

    def test_report_database_mismatch_and_changed_journal_before_images_are_rejected(self):
        source = self.root / ROOTS["team"] / self.runs["dependency"]
        report = read_json(source / "service" / "report.json")
        report["state"]["stable_healthy"] = False
        write_json(source / "service" / "report.json", report)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.load()
        path = self.root / ROOTS["team"] / self.runs["routing"] / "service" / "coordinator.sqlite3"
        db = sqlite3.connect(path)
        try:
            event = decode(db.execute("SELECT event FROM events WHERE sequence=1").fetchone()[0])
            event["changes"]["stable_units"]["before"] = 999
            db.execute("UPDATE events SET event=? WHERE sequence=1", (sim.canonical(event),))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "before image"):
            self.load("routing")

    def test_path_traversal_symlinks_and_nonfinite_json_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "identifier"):
            load_source(self.root, "team", "../outside")
        linked = self.root / ROOTS["team"] / "linked"
        try:
            linked.symlink_to(self.sources["dependency"], target_is_directory=True)
        except (OSError, NotImplementedError):
            pass  # Native Windows may disallow symlink creation without elevation.
        else:
            with self.assertRaisesRegex(ValueError, "symbolic"):
                load_source(self.root, "team", "linked")
        path = self.root / ROOTS["team"] / self.runs["dependency"] / "summary.json"
        path.write_text('{"status": NaN}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            team_view(self.root, self.runs["dependency"])

    def test_incomplete_role_run_is_viewable_but_not_reviewable(self):
        run = self.runs["dependency"]
        summary_path = self.root / ROOTS["team"] / run / "summary.json"
        summary = read_json(summary_path)
        summary.update(status="AWAITING_EXACT_MANAGER_CONSENT")
        summary.pop("full_three_role_flow_completed", None)  # The runner writes this field only when it finishes.
        write_json(summary_path, summary)
        report_path = self.root / ROOTS["team"] / run / "service" / "report.json"
        report_path.write_text('{"still":', encoding="utf-8")
        view = team_view(self.root, run)
        self.assertTrue(view["active"])
        self.assertIsNone(view["service"])
        self.assertTrue(view["warnings"])
        self.assertEqual(view["review"]["available_decisions"], [])
        with self.assertRaisesRegex(ValueError, "Finish|Expecting"):
            self.load()
        summary.update(status="AWAITING_APPROVAL_NO_EXECUTION", full_three_role_flow_completed=False)
        write_json(summary_path, summary)
        self.assertFalse(team_view(self.root, run)["active"], "A completed declined run must not look active.")

    def test_source_drift_during_branch_creation_leaves_no_accepted_result(self):
        from unittest import mock
        original = load_source
        calls = []
        def drifting(*args):
            value = original(*args)
            if calls:
                value["source_digest"] = "0" * 64
            calls.append(True)
            return value
        source = self.load()
        with mock.patch(__name__ + ".load_source", side_effect=drifting):
            with self.assertRaisesRegex(ValueError, "during review"):
                self.decide("release_dependency", source=source)
        self.assertEqual(list((self.root / ROOTS["team_review"]).glob("*/summary.json")), [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--source-kind", choices=tuple(ROOTS), required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--source-digest", required=True)
    parser.add_argument("--decision", choices=sorted(review.DECISIONS), required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--confirm-snapshot", action="store_true")
    args = parser.parse_args()
    directory, report = resolve(args.root, args.source_kind, args.source_run, args.source_digest,
                                args.decision, args.note, args.confirm_snapshot)
    print(MODE)
    print("Result: " + report["decision"]["result"]["status"] + " | " + report["recovery_outcome"])
    print("Evidence: " + str(directory))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, RuntimeError) as exc:
        raise SystemExit("SNAPSHOT REVIEW STOPPED: " + str(exc))
