#!/usr/bin/env python3
r"""Kill a local simulation worker, then resume its database in a new process.

Windows: .\.venv\Scripts\python.exe project.py restart-check --open
Standard library only. Scripted infrastructure; no model calls or cloud changes.
Workers are killed only after reporting the exact requested checkpoint. A timeout
or missing checkpoint is a failed check, never an inferred interruption success.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import os
import platform
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import simulator as sim

VERSION = "agent-rehearsal-restart-v2"
MODE = "OFFLINE PROCESS INTERRUPTION; SCRIPTED SIMULATED INFRASTRUCTURE"
ROOT = Path(__file__).resolve().parent
CASES = {
    "reservation_before_commit": ("action_before_commit", "restart-reserve", "reserve"),
    "reservation_reply_lost": ("action_after_commit", "restart-reserve", "reserve"),
    "blocked_before_commit": ("action_before_commit", "restart-reserve", "blocked"),
    "blocked_reply_lost": ("action_after_commit", "restart-reserve", "blocked"),
    "recovery_after_pause": ("recovery_after_pause", None, "recovery"),
    "recovery_after_route": ("action_after_commit", "recovery-route-2-0", "recovery"),
    "recovery_after_stop": ("action_after_commit", "recovery-stop-0", "recovery"),
    "recovery_after_release": ("action_after_commit", "recovery-release-0", "recovery"),
    "pending_reservation_after_pause": ("recovery_after_pause", None, "pending"),
    "routing_conflict_after_pause": ("recovery_after_pause", None, "routing_conflict"),
    "dependency_after_route": ("action_after_commit", "recovery-route-2-0", "dependency"),
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def snapshot(path):
    engine = sim.Engine(path, create=False)
    try:
        receipts = {key: {"fingerprint": fingerprint, "result": json.loads(result)}
                    for key, fingerprint, result in engine.db.execute(
                        "SELECT action_key, fingerprint, result FROM action_receipts ORDER BY action_key")}
        return {"state": engine.state(), "events": engine.events(), "receipts": receipts,
                "integrity_check": [row[0] for row in engine.db.execute("PRAGMA integrity_check")]}
    finally:
        engine.close()


def reserve(engine, units=20, key="restart-reserve", rid="restart-reservation"):
    return sim.action(engine, "reserve_capacity", {
        "units": units, "reservation_id": rid, "budget_version": 1}, key)


def prepare(path, case):
    engine = sim.Engine(path)
    try:
        operation = CASES[case][2]
        if operation == "blocked":
            sim.cost_change(engine)
        elif operation == "pending":
            reserve(engine, key="pending-before-recovery")
            sim.cost_change(engine)
        elif operation not in {"reserve", "blocked"}:
            sim.start_canary(engine)
            sim.cost_change(engine)
            if operation == "routing_conflict":
                sim.action(engine, "set_route", {"candidate_id": "candidate-v2",
                    "candidate_percent": 20, "route_version": 2}, "C-route-20",
                    actor="C", run="route-C")
            elif operation == "dependency":
                sim.action(engine, "add_dependency", {"candidate_id": "candidate-v2",
                    "dependency_id": "C-legitimate-job"}, "C-dependency",
                    actor="C", run="route-C")
            engine.fixture_event("CANDIDATE_FAILURE")
        failures = [event for event in engine.events()
                    if event.get("kind") == "ACTION" and event["result"]["status"] != "SUCCEEDED"]
        if failures:
            raise RuntimeError("The crash fixture did not initialize as specified.")
    finally:
        engine.close()


class InterruptibleEngine(sim.Engine):
    """Harness-only implementation. Normal Engine never blocks at checkpoints."""
    def __init__(self, path, case):
        self.target, self.key, _ = CASES[case]
        super().__init__(path, create=False)

    def _checkpoint(self, name, **context):
        if name != self.target or (self.key is not None and context.get("action_key") != self.key):
            return
        # For pre-commit checks this proves state, journal AND receipt were staged.
        notice = {"kind": "CHECKPOINT_READY", "checkpoint": name, "context": context,
                  "pid": os.getpid(), "transaction_open": self.db.in_transaction,
                  "worker_state": self.state(), "worker_event_count": len(self.events()),
                  "worker_receipt_count": self.db.execute(
                      "SELECT COUNT(*) FROM action_receipts").fetchone()[0]}
        print(sim.canonical(notice), flush=True)
        sys.stdin.buffer.read(1)  # Parent keeps this pipe open until it kills us.
        raise RuntimeError("Checkpoint resumed without the requested process termination.")


def worker(path, case, mode, result_path=None):
    if not path.is_file():
        raise RuntimeError("The worker requires an existing initialized fixture database.")
    operation = CASES[case][2]
    engine = InterruptibleEngine(path, case) if mode == "interrupt" else sim.Engine(path, create=False)
    try:
        if mode == "interrupt":
            if operation in {"reserve", "blocked"}:
                reserve(engine)
            else:
                engine.recover()
            raise RuntimeError("The requested interruption checkpoint was not reached.")
        outcome = {"pid": os.getpid(), "resumed_in_new_process": True}
        if operation in {"reserve", "blocked"}:
            outcome["result"] = reserve(engine)
            stable = engine.state()
            outcome["duplicate_result"] = reserve(engine)
            outcome["duplicate_state_unchanged"] = engine.state() == stable
            outcome["changed_key_result"] = reserve(engine, units=21)
            outcome["changed_key_state_unchanged"] = engine.state() == stable
        else:
            outcome["recovery"] = engine.recover()
            stable = engine.state()
            outcome["new_admission_result"] = reserve(
                engine, key="new-work-after-restart", rid="new-work-after-restart")
            outcome["paused_admission_state_unchanged"] = engine.state() == stable
            outcome["duplicate_recovery"] = engine.recover()
            outcome["duplicate_state_unchanged"] = engine.state() == stable
        write_json(result_path, outcome)
    finally:
        engine.close()


def child_env():
    env = os.environ.copy()
    for name in ("NEBIUS_API_KEY", "OPENAI_API_KEY", "__PYVENV_LAUNCHER__"):
        env.pop(name, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


def worker_interpreter():
    """Launch the actual interpreter, not the Windows venv redirector.

    CPython's venv module itself uses sys._base_executable to find its runtime.
    These workers need only stdlib and local source. The launcher may create a
    second process, breaking ownership checks and making launcher termination an
    unsuitable substitute for terminating the worker at its checkpoint.
    """
    if platform.system() != "Windows":
        return sys.executable
    base = getattr(sys, "_base_executable", None)
    candidate = Path(base) if base else Path(sys.base_prefix) / "python.exe"
    if not candidate.is_file():
        raise RuntimeError("The underlying Python executable for the Windows worker is missing: " + str(candidate))
    return str(candidate.resolve())


def child_command(path, case, mode, result_path=None):
    flags = [worker_interpreter(), "-u", str(Path(__file__).resolve()), "--worker", mode,
             "--database", str(path), "--case", case]
    if result_path is not None:
        flags += ["--worker-result", str(result_path)]
    return flags


def interrupt_worker(path, case, timeout=30):
    process = subprocess.Popen(child_command(path, case, "interrupt"), cwd=str(ROOT),
        env=child_env(), shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")
    messages = queue.Queue()

    def read_notice():
        try:
            messages.put(process.stdout.readline())
        except (OSError, ValueError) as exc:
            messages.put(exc)

    reader = threading.Thread(target=read_notice, daemon=True)
    reader.start()
    try:
        try:
            line = messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError("Worker did not report its checkpoint before the timeout.") from exc
        if not isinstance(line, str) or not line:
            raise RuntimeError("Worker exited before reporting its checkpoint.")
        notice = json.loads(line)
        if not isinstance(notice, dict):
            raise RuntimeError("The worker checkpoint must be a JSON object.")
        checkpoint, key, _ = CASES[case]
        if notice.get("pid") != process.pid:
            raise RuntimeError("Worker process ID mismatch: started PID %s, reported PID %s. "
                               "The checkpoint was not accepted." % (process.pid, notice.get("pid")))
        if (notice.get("kind") != "CHECKPOINT_READY" or notice.get("checkpoint") != checkpoint
                or (key is not None and notice.get("context", {}).get("action_key") != key)):
            raise RuntimeError("Worker reported a different interruption boundary: expected %s / %s, "
                               "received %s / %s." % (checkpoint, key, notice.get("checkpoint"),
                                notice.get("context", {}).get("action_key")))
        process.kill()  # SIGKILL on POSIX, TerminateProcess on Windows; no graceful close.
        stdout, stderr = process.communicate(timeout=10)
        notice.update(termination_requested=True, child_exit_code=process.returncode,
                      termination_method="Popen.kill", started_worker_pid=process.pid,
                      worker_interpreter=process.args[0], remaining_stdout=stdout, stderr=stderr)
        if process.returncode == 0:
            raise RuntimeError("The interrupted worker unexpectedly exited successfully.")
        return notice
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def audit_journal(record):
    """Cross-check the committed state and receipts against the recorded journal."""
    state = sim.initial_state()
    committed = {}
    invariant_results = []
    for event in record["events"]:
        for field, change in event.get("changes", {}).items():
            state[field] = copy.deepcopy(change["after"])
        if event["kind"] == "RECOVERY_STARTED":
            if event["run_id"] not in state["paused_runs"]:
                state["paused_runs"].append(event["run_id"])
        if event["kind"] == "ACTION":
            request = {name: event[name] for name in ("actor", "run_id", "tool", "args", "approval_id")}
            committed[event["action_key"]] = {"fingerprint": sim.digest(request), "result": event["result"]}
        # Independent predicates over every recorded committed frame.
        units = state["stable_units"] + sum(d["allocated_units"] for d in state["deployments"].values())
        units += sum(r["units"] for r in state["reservations"].values() if r["status"] == "PENDING")
        invariant_results.append(units <= state["capacity_limit"] and
            all(d["allocated_units"] >= 0 for d in state["deployments"].values()) and
            sum(state["routing"].values()) == 100 and
            all(type(pct) is int and 0 <= pct <= 100 for pct in state["routing"].values()) and
            all(target == "stable-v1" or pct == 0 or
                (state["deployments"].get(target, {}).get("status") == "RUNNING" and
                 state["deployments"][target]["allocated_units"] > 0)
                for target, pct in state["routing"].items()))
    return {"journal_matches_state": state == record["state"],
            "receipts_match_committed_actions": committed == record["receipts"],
            "every_committed_frame_respects_invariants": all(invariant_results)}


def evaluate(case, baseline, interrupted, checkpoint, resumed, final):
    checks = []

    def check(label, passed):
        checks.append({"assertion": label, "passed": bool(passed)})

    operation = CASES[case][2]
    check("Parent terminated the worker at the requested checkpoint", checkpoint["termination_requested"]
          and checkpoint["child_exit_code"] != 0)
    check("A separate worker resumed the saved database", resumed["resumed_in_new_process"]
          and resumed["pid"] != checkpoint["pid"])
    check("SQLite integrity holds after termination and restart",
          interrupted["integrity_check"] == final["integrity_check"] == ["ok"])
    for label, record in (("After termination", interrupted), ("After restart", final)):
        for name, passed in audit_journal(record).items():
            check(label + ": " + name.replace("_", " "), passed)
    if operation in {"reserve", "blocked"}:
        before_commit = CASES[case][0] == "action_before_commit"
        key = "restart-reserve"
        if before_commit:
            check("State, action journal and receipt were all staged in the open transaction",
                  checkpoint["transaction_open"] and
                  checkpoint["worker_event_count"] == len(baseline["events"]) + 1 and
                  checkpoint["worker_receipt_count"] == len(baseline["receipts"]) + 1)
            check("Uncommitted state, journal and receipt rolled back together", interrupted == baseline)
        else:
            check("The commit completed before the lost reply", not checkpoint["transaction_open"]
                  and key in interrupted["receipts"] and
                  len(interrupted["events"]) == len(baseline["events"]) + 1)
            check("Committed state matches the worker checkpoint", interrupted["state"] == checkpoint["worker_state"])
        result = resumed["result"]
        check("Retry replays exactly when a receipt survived", bool(result.get("replayed")) == (not before_commit))
        if operation == "reserve":
            check("Reservation is admitted exactly once", result["status"] == "SUCCEEDED" and
                  final["state"]["reservations"] == {
                      "restart-reservation": {"owner_run": "deploy-A", "units": 20, "status": "PENDING"}})
            check("Capacity remains 60 of 100", sim.total_commitments(final["state"]) == 60 and
                  final["state"]["capacity_limit"] == 100)
        else:
            check("A stale request remains blocked after restart", result["code"] == "STALE_BUDGET_VERSION")
            check("Blocked request has no state effect", final["state"] == baseline["state"])
        check("The next identical retry is a receipt replay without state mutation",
              resumed["duplicate_result"].get("replayed") and resumed["duplicate_state_unchanged"])
        check("Changed arguments using the same key remain blocked",
              resumed["changed_key_result"]["code"] == "IDEMPOTENCY_KEY_CONFLICT" and
              resumed["changed_key_state_unchanged"])
        check("Only one original action and receipt exist for the key",
              sum(e["kind"] == "ACTION" and e.get("action_key") == key for e in final["events"]) == 1)
    else:
        state, at_crash = final["state"], interrupted["state"]
        check("The workflow pause survived interruption", "deploy-A" in at_crash["paused_runs"])
        check("B's independently approved capacity change survives recovery",
              state["capacity_limit"] == 70 and state["budget_version"] == 2)
        check("New admission remains closed for the paused workflow",
              resumed["new_admission_result"]["code"] == "WORKFLOW_PAUSED" and
              resumed["paused_admission_state_unchanged"])
        check("Repeating recovery does not mutate the recovered state", resumed["duplicate_state_unchanged"])
        check("The durable pause is recorded only once", sum(e["kind"] == "RECOVERY_STARTED"
              for e in final["events"]) == 1)
        if operation == "routing_conflict":
            check("C's later route is preserved", state["routing"] == {"stable-v1": 80, "candidate-v2": 20}
                  and state["route_owner"] == "route-C" and state["route_version"] == 3)
            check("Traffic-dependent capacity remains allocated", sim.total_commitments(state) == 60 and
                  state["deployments"]["candidate-v2"]["status"] == "RUNNING")
            check("Route conflict produces review rather than a recovery claim",
                  resumed["recovery"]["status"] == "RECOVERY_REVIEW_REQUIRED" and
                  {i["code"] for i in resumed["recovery"]["issues"]} ==
                  {"ROUTE_VERSION_CONFLICT", "RESOURCE_STILL_ROUTED"})
        elif operation == "dependency":
            check("C's legitimate dependency survives restart", state["deployments"]["candidate-v2"]["dependencies"] ==
                  [{"id": "C-legitimate-job", "owner_run": "route-C"}])
            check("Dependent candidate is retained after route restoration", sim.total_commitments(state) == 60 and
                  state["routing"] == {"stable-v1": 100, "candidate-v2": 0} and
                  state["deployments"]["candidate-v2"]["status"] == "RUNNING")
            check("Unresolved dependency produces review", resumed["recovery"]["status"] == "RECOVERY_REVIEW_REQUIRED"
                  and any(i["code"] == "RESOURCE_STILL_IN_USE" for i in resumed["recovery"]["issues"]))
        else:
            expected_repeat = "NOT_NEEDED" if operation == "pending" else "VERIFIED_RECOVERED"
            check("Restarted recovery completes and its retry reports the expected outcome",
                  resumed["recovery"]["status"] == "VERIFIED_RECOVERED" and
                  resumed["duplicate_recovery"]["status"] == expected_repeat)
            check("Final commitments and routing are safe", sim.total_commitments(state) == 40 and
                  state["routing"] == {"stable-v1": 100, "candidate-v2": 0})
            if operation == "pending":
                check("Unused reservation is released without fabricating a candidate", not state["deployments"] and
                      state["reservations"]["restart-reservation"]["status"] == "RELEASED")
            else:
                check("Candidate is confirmed stopped before its allocation is released",
                      state["deployments"]["candidate-v2"]["status"] == "STOPPED" and
                      state["deployments"]["candidate-v2"]["allocated_units"] == 0)
                for tool in ("restore_route", "stop_candidate", "release_capacity"):
                    check(tool.replace("_", " ") + " commits once across interruption and retry",
                          sum(e["kind"] == "ACTION" and e.get("tool") == tool and
                              e["result"]["status"] == "SUCCEEDED" for e in final["events"]) == 1)
        if case in {"recovery_after_route", "dependency_after_route"}:
            check("Route was restored but allocation was still retained at interruption",
                  at_crash["routing"]["candidate-v2"] == 0 and
                  at_crash["deployments"]["candidate-v2"]["allocated_units"] == 20)
        elif case == "recovery_after_stop":
            check("Stop was committed while allocation was still retained at interruption",
                  at_crash["deployments"]["candidate-v2"]["status"] == "STOPPED" and
                  at_crash["deployments"]["candidate-v2"]["allocated_units"] == 20)
        elif case == "recovery_after_release":
            check("Release committed before the recovery report existed", sim.total_commitments(at_crash) == 40 and
                  not any(e["kind"] == "RECOVERY_REPORT" for e in interrupted["events"]))
    return checks


def run_case(case, directory):
    directory.mkdir(parents=True, exist_ok=False)
    database = directory / "state.sqlite3"
    prepare(database, case)
    baseline = snapshot(database)
    write_json(directory / "baseline.json", baseline)
    checkpoint = interrupt_worker(database, case)
    write_json(directory / "checkpoint.json", checkpoint)
    interrupted = snapshot(database)
    write_json(directory / "after_termination.json", interrupted)
    result_path = directory / "resume_result.json"
    process = subprocess.run(child_command(database, case, "resume", result_path),
        cwd=str(ROOT), env=child_env(), shell=False, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    if process.returncode != 0:
        raise RuntimeError("Restart worker failed: " + (process.stderr or process.stdout)[-3000:])
    resumed = json.loads(result_path.read_text(encoding="utf-8"))
    final = snapshot(database)
    write_json(directory / "after_restart.json", final)
    checks = evaluate(case, baseline, interrupted, checkpoint, resumed, final)
    report = {"case": case, "mode": MODE, "version": VERSION, "model_calls": 0,
              "passed": all(c["passed"] for c in checks), "checks": checks,
              "checkpoint": checkpoint["checkpoint"], "interrupted_worker_pid": checkpoint["pid"],
              "resumed_worker_pid": resumed["pid"], "child_exit_code": checkpoint["child_exit_code"],
              "result": resumed.get("result"), "recovery": resumed.get("recovery"),
              "initial_state": sim.initial_state(), "final_state": final["state"], "events": final["events"]}
    write_json(directory / "report.json", report)
    return report


def render_report(summary, reports):
    rows = []
    for report in reports:
        checks = report.get("checks", [])
        state = report.get("final_state")
        outcome = (report.get("recovery") or report.get("result") or {}).get("status", "ERROR")
        details = "".join("<li>" + ("PASS: " if c["passed"] else "FAIL: ") +
                          html.escape(c["assertion"]) + "</li>" for c in checks)
        if report.get("error"):
            details += "<li>ERROR: " + html.escape(report["error"]) + "</li>"
        capacity = "%s / %s" % (sim.total_commitments(state), state["capacity_limit"]) if state else "—"
        rows.append('<tr><td>' + html.escape(report["case"]) + '</td><td>' +
                    ("PASS" if report["passed"] else "FAIL") + '</td><td>' +
                    html.escape(outcome) + '</td><td>' + capacity + '</td><td><details><summary>' +
                    str(sum(c["passed"] for c in checks)) + '/' + str(len(checks)) +
                    ' assertions</summary><ul>' + details + '</ul></details></td></tr>')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orkestryn · Restart verification</title><style>
body{font:16px/1.6 system-ui,sans-serif;background:#10151d;color:#e5edf5;margin:0;padding:36px;max-width:1200px}
h1{line-height:1.2}p{max-width:950px}.card{border:1px solid #344256;padding:20px;border-radius:12px;margin:24px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:12px;border-bottom:1px solid #344256;vertical-align:top}
summary{cursor:pointer;color:#98d9bc}code{color:#98d9bc}li{margin:5px 0}small{color:#b3c2d4}@media(max-width:700px){body{padding:16px}.table{overflow:auto}}
</style><h1>Process interruption and restart checks</h1><p>Orkestryn · ''' + html.escape(VERSION) + '''</p>
<div class="card"><strong>''' + str(summary["cases_passed"]) + " / " + str(summary["cases_total"]) + ''' interruption cases passed</strong>
<p>Each case terminates a worker at an acknowledged checkpoint, then starts a separate process against the same SQLite database.
Saved checks compare state, committed events, idempotency receipts and preservation of other operators' work.</p></div>
<p>Offline scripted infrastructure. Zero model calls and no GPU or cloud operations. These results test local process termination;
they do not establish power-loss durability, disk-failure handling or distributed recovery. Review-required outcomes count as correct
only when the specified route or dependency is preserved.</p><div class="table"><table><thead><tr><th>Interruption case</th><th>Check</th>
<th>Resumed outcome</th><th>Capacity / limit</th><th>Evidence checks</th></tr></thead><tbody>''' + "".join(rows) + '''</tbody></table></div>
<p><small>Evidence is saved beside this report, including checkpoint acknowledgements, pre/post snapshots, resumed results and SQLite journals.
Observed platform: ''' + html.escape(summary["platform"]) + " · Python " + html.escape(summary["python_version"]) + '''.
Results apply to this recorded run.</small></p></html>'''


def run_suite(output):
    directory = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    reports = []
    for case in CASES:
        try:
            report = run_case(case, directory / case)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            report = {"case": case, "passed": False, "error": str(exc), "checks": []}
            write_json(directory / case / "report.json", report)
        reports.append(report)
        print(("PASS: " if report["passed"] else "FAIL: ") + case, flush=True)
    summary = {"version": VERSION, "mode": MODE, "model_calls": 0, "passed": all(r["passed"] for r in reports),
               "cases_passed": sum(r["passed"] for r in reports), "cases_total": len(reports),
               "failed_cases": [r["case"] for r in reports if not r["passed"]],
               "assertions_passed": sum(c["passed"] for r in reports for c in r["checks"]),
               "assertions_total": sum(len(r["checks"]) for r in reports),
               "platform": platform.system(), "python_version": platform.python_version(),
               "created_at": datetime.now(timezone.utc).isoformat(),
               "scope": "Local abrupt process termination at fixed boundaries; not power loss, disk failure or distributed infrastructure."}
    write_json(directory / "summary.json", summary)
    (directory / "report.html").write_text(render_report(summary, reports), encoding="utf-8")
    return directory, summary


class RestartTests(unittest.TestCase):
    def test_windows_worker_bypasses_the_virtual_environment_launcher(self):
        real_runtime = getattr(sys, "_base_executable", None) or sys.executable
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # This launcher cannot run a worker. Only direct base-runtime selection
            # can pass the real lost-reply termination/resumption checks below.
            launcher = root / "virtual environment" / "Scripts" / "python.exe"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("Not an interpreter: simulated venv redirector.", encoding="utf-8")
            with mock.patch.object(platform, "system", return_value="Windows"), \
                    mock.patch.object(sys, "executable", str(launcher)), \
                    mock.patch.object(sys, "_base_executable", real_runtime, create=True):
                report = run_case("reservation_reply_lost", root / "run with spaces")
            self.assertTrue(report["passed"], [c for c in report["checks"] if not c["passed"]])
            checkpoint = json.loads((root / "run with spaces" / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["started_worker_pid"], checkpoint["pid"])
            self.assertEqual(Path(checkpoint["worker_interpreter"]).resolve(), Path(real_runtime).resolve())

    def test_missing_windows_base_runtime_fails_without_launcher_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing-python.exe")
            with mock.patch.object(platform, "system", return_value="Windows"), \
                    mock.patch.object(sys, "_base_executable", missing, create=True):
                with self.assertRaisesRegex(RuntimeError, "underlying Python executable.*missing"):
                    child_command(Path(directory) / "state.sqlite3", "reservation_reply_lost", "interrupt")

    def check_case(self, name):
        with tempfile.TemporaryDirectory() as directory:
            report = run_case(name, Path(directory) / name)
            self.assertTrue(report["passed"], [c["assertion"] for c in report["checks"] if not c["passed"]])

    def test_reservation_before_commit(self):
        self.check_case("reservation_before_commit")

    def test_reservation_reply_lost(self):
        self.check_case("reservation_reply_lost")

    def test_blocked_before_commit(self):
        self.check_case("blocked_before_commit")

    def test_blocked_reply_lost(self):
        self.check_case("blocked_reply_lost")

    def test_recovery_after_pause(self):
        self.check_case("recovery_after_pause")

    def test_recovery_after_route(self):
        self.check_case("recovery_after_route")

    def test_recovery_after_stop(self):
        self.check_case("recovery_after_stop")

    def test_recovery_after_release(self):
        self.check_case("recovery_after_release")

    def test_pending_reservation_after_pause(self):
        self.check_case("pending_reservation_after_pause")

    def test_routing_conflict_after_pause(self):
        self.check_case("routing_conflict_after_pause")

    def test_dependency_after_route(self):
        self.check_case("dependency_after_route")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "restart_runs")
    parser.add_argument("--open", action="store_true", help="Open the saved HTML verification report.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", choices=("interrupt", "resume"), help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=tuple(CASES), help=argparse.SUPPRESS)
    parser.add_argument("--worker-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if args.database is None or args.case is None or (args.worker == "resume" and args.worker_result is None):
            parser.error("Worker arguments are incomplete.")
        worker(args.database, args.case, args.worker, args.worker_result)
        return 0
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RestartTests))
        return 0 if result.wasSuccessful() else 1
    print("ORKESTRYN - PROCESS INTERRUPTION AND RESTART CHECKS", flush=True)
    print("Only dedicated simulation workers are terminated. No API calls or credits needed.", flush=True)
    directory, summary = run_suite(args.output)
    print("\nInterruption cases: %d/%d passed" % (summary["cases_passed"], summary["cases_total"]))
    print("Assertions: %d/%d passed" % (summary["assertions_passed"], summary["assertions_total"]))
    print("Evidence: " + str(directory))
    print("Report: " + str(directory / "report.html"))
    if args.open:
        webbrowser.open((directory / "report.html").resolve().as_uri())
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped. Completed evidence remains saved.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print("RESTART CHECK FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
