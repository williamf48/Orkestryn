#!/usr/bin/env python3
"""Install the complete local bundle, verify it and open the team workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def payload(source):
    """Read and verify the complete package before changing installed files."""
    source = Path(source).resolve()
    raw = (source / "bundle_manifest.json").read_bytes()
    manifest = json.loads(raw)
    hashes = manifest["file_sha256"]
    # Exact clean-release source and fixture inventory; historical reports are not runtime dependencies.
    required = {'agent_runner.py', 'coordination_tests.py', 'hosted_server.py', 'start_dashboard.cmd', 'release_archive.py', 'windows_compat_tests.py', 'inference_gateway.py', 'coordination_reference.py', 'safety_inspect.py', 'browser_proposals.py', 'operations_ui.py', 'coordination_ui.py', 'durable_runtime.py', 'agent_team.py', 'relay_demo.py', 'conflict_explorer.py', 'desktop_entry.py', 'judge_relay_tests.py', 'coordination_benchmark_tests.py', 'operations_server_tests.py', 'live_readiness_tests.py', 'connected_team_tests.py', 'process_check.py', 'safety_variants.py', 'team_workspace_ui.py', 'coordination_broker_tests.py', 'release_check_tests.py', 'workflow_dependency_chain.json', 'operations_factory_tests.py', 'coordination_set.json', 'start_operations.cmd', 'team_evaluation_set.json', 'service_agent.py', 'safety_ui.py', 'operations_contract_tests.py', 'operations_gateway_tests.py', 'safety_reliability_tests.py', 'operations_proposals.py', 'desktop_launcher.py', 'change_coordinator.py', 'operations_provider_tests.py', 'judge_setup_tests.py', 'conflict_lab.py', 'connected_evaluation.py', 'project.py', 'verify_service_agent_evidence.py', 'safety_lab.py', 'connected_evaluation_tests.py', 'campaign_check.py', 'operations_ui_checks.py', 'judge_relay.py', 'mission_ui.py', 'CHECK_WINDOWS_PROCESS.cmd', 'simulator.py', 'operations_supervisor.py', 'mission_control.py', 'workflow_balanced.json', 'runtime_sessions_ui.py', 'release_check.py', 'hosted_tests.py', 'safety_http.py', 'controller_source_v1.json', 'operations_outcomes.py', 'safety_environment.py', 'acceptance_tests.py', 'downloadable_demo.py', 'live_readiness.py', 'start_live_operations.cmd', 'factory_reply_regression.json', 'operations_evidence_tests.py', 'connected_evaluation_set.json', 'workflow_planner.py', 'named_access.py', 'safety_tests.py', 'team_workspace.py', 'CHECK_DOWNLOAD.cmd', 'coordination_benchmark.py', 'operations_workspace.py', 'team_evaluation.py', 'team_evaluation_scoring.py', 'downloadable_demo_tests.py', 'team_evaluation_ui.py', 'agent_evaluation.py', 'safety_http_tests.py', 'workflow_deployment_heavy.json', 'dashboard.py', 'terminal_guard.py', 'verify_team_evidence.py', 'data_factory.py', 'safety_live.py', 'operations_planner_tests.py', 'operations_access_tests.py', 'operations_tests.py', 'durable_runtime_tests.py', 'judge_relay_worker.mjs', 'proposal_reply_regressions.json', 'START_JUDGE_LIVE.cmd', 'restart_checker.py', 'case_workflow.py', 'install_update.py', 'local_service.py', 'acceptance.py', 'operations_evidence.py', 'test_paths.py', 'challenge_factory.py', 'native_process_tests.py', 'workflow_bulk.json', 'mission_dashboard_tests.py', 'live_evaluator.py', 'operations_recovery_contract_tests.py', 'connected_team.py', 'agent_evaluation_set.json', 'judge_relay_schema.sql', 'OPERATIONS_DEMO.json', 'mission_tests.py', 'verify_live_evidence.py', 'CHECK_WINDOWS_UPDATE.cmd', 'browser_proposals_tests.py', 'coordination_dashboard_tests.py', 'judge_evaluation.py', 'coverage_planner.py', 'judge_setup.py', 'RUN_WINDOWS_ACCEPTANCE.cmd', 'connected_team_ui.py', 'judge_relay_worker_tests.mjs', 'operations_outcomes_tests.py', 'plan_revision.py', 'safety_resume_reply_regression.json', 'start_live_judge_rehearsal.cmd', 'windows_preflight.py', 'safety_suite_v1.json', 'native_process.py', 'connected_dashboard_tests.py', 'start_judge_rehearsal.cmd', 'factory_publication_tests.py', 'operations_proposals_tests.py', 'safety_ui_checks.py', 'build_desktop.py', 'START_DEMO.cmd', 'safety_inspect_check.py', 'desktop_tests.py', 'recovery_deferral_regression.json', 'operations_server.py', 'governance_deferral_regression.json', 'INSTALL_OR_UPDATE.cmd', 'runtime_sessions.py', 'judge_evaluation_tests.py', 'review_workflow.py'}
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ValueError("This is not the complete Orkestryn package. Extract the whole ZIP first.")
    files = {}
    for name, expected in hashes.items():
        if (not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name
                or name in (".", "..", "bundle_manifest.json")
                or (name not in (".gitignore", "LICENSE") and Path(name).suffix.lower() not in
                    (".py", ".md", ".json", ".html", ".txt", ".cmd", ".mjs", ".sql"))):
            raise ValueError("Unsupported package filename.")
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing package file: " + name + ". Extract the whole ZIP first.")
        data = path.read_bytes()
        if digest(data) != expected:
            raise ValueError("Package verification failed for " + name + ". Extract a fresh copy of the ZIP.")
        files[name] = data
    files["bundle_manifest.json"] = raw
    return files, manifest["package_version"]


def install(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    files, version = payload(source)
    if source == destination:
        return destination, None, version
    # Fail before creating a backup or overwriting anything if a target is not a regular file.
    for name in files:
        path = destination / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("Cannot replace a directory or link: " + str(path))
    destination.mkdir(parents=True, exist_ok=True)
    existing = [name for name in files if (destination / name).is_file()]
    backup = None
    if existing:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        backup = destination / "update_backups" / stamp
        backup.mkdir(parents=True, exist_ok=False)
        old_hashes = {}
        for name in existing:
            old = (destination / name).read_bytes()
            (backup / name).write_bytes(old)
            old_hashes[name] = digest(old)
        (backup / "backup_record.json").write_text(json.dumps({"previous_files": old_hashes,
            "new_package_version": version}, indent=2) + "\n", encoding="utf-8")
        print("Previous package files backed up to: " + str(backup), flush=True)
    # Only verified flat package files are written. Environment and run folders are outside this list.
    for name, data in files.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, prefix=".package-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            temporary.replace(destination / name)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    for name, data in files.items():
        if (destination / name).read_bytes() != data:
            raise OSError("Installed file verification failed: " + name)
    return destination, backup, version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / "AgentRehearsal")
    parser.add_argument("--install-only", action="store_true", help="Copy and verify files without running checks or opening a report.")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 or newer is required.")
    print("ORKESTRYN - COMPLETE LOCAL UPDATE", flush=True)
    destination, _, version = install(Path(__file__).resolve().parent, args.target)
    print("Installed: " + version, flush=True)
    print("Project: " + str(destination), flush=True)
    if args.install_only:
        return 0
    python = destination / ".venv" / "Scripts" / "python.exe"
    executable = str(python) if os.name == "nt" and python.is_file() else sys.executable
    environment = os.environ.copy()
    for name in ("NEBIUS_API_KEY", "OPENAI_API_KEY"):
        environment.pop(name, None)
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    print("Running setup, short native preflight, then native acceptance: complete suite, original/frozen/connected cases, 12 policy-only ordering cases, real coordination demo, complete mission, operations emergency failover and supervisor restart walkthrough.", flush=True)
    for command in (("doctor",), ("preflight",), ("acceptance",)):
        result = subprocess.run([executable, "-u", str(destination / "project.py"), *command],
                                cwd=destination, env=environment, shell=False)
        if result.returncode:
            print("Stopped at " + command[0] + ". The error is shown above; installed files are retained.", flush=True)
            return result.returncode
    print("Update complete. Native acceptance and a public evidence ZIP are in acceptance_runs. Expected: 14/14 original, 6/6 frozen-team, 10/10 connected tasks and 12/12 policy-only ordering fixtures; real coordination and restart demos with separate teardown. Zero model calls. The Operations workspace adds local accounts, browser approvals, background monitoring and emergency handoff. Older dashboards remain available with start_dashboard.cmd.", flush=True)
    if os.name == "nt":
        subprocess.Popen([executable, "-u", str(destination / "project.py"), "operations", "serve", "--open"],
                         cwd=destination, env=environment, shell=False,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        print("Opening the named-user operations workspace in a new console. Keep that console open while using the dashboard.", flush=True)
    else:
        print("Open the workspace with: python project.py operations serve --open", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print("UPDATE FAILED: " + str(exc), file=sys.stderr)
        print("If a backup path was printed, previous package files remain there. Re-extract the full ZIP before retrying.", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Update interrupted. Re-run from the complete extracted ZIP to finish.", file=sys.stderr)
        raise SystemExit(130)
