#!/usr/bin/env python3
"""Offline setup, verification, and launch commands for Orkestryn.

Examples: python project.py doctor
          python project.py check
          python project.py demo
          python project.py dashboard --open

Commands make no paid request at startup. The browser can use owner-configured
NVIDIA access, with one review per paid attempt. Explicit --live commands on
supported proposal tools use Nebius credits. No command installs packages.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = {
    "desktop_tests": "DesktopTests",
    "operations_recovery_contract_tests": "RecoveryContractTests",
    "operations_contract_tests": "ContractTests",
    "operations_factory_tests": "FactoryContractTests",
    "safety_reliability_tests": "ReliabilityTests", "safety_tests": "SafetyTests", "safety_http_tests": "SafetyHTTPTests",
    "simulator": "EngineTests", "agent_runner": "RunnerTests", "data_factory": "FactoryTests",
    "live_evaluator": "EvaluatorTests", "dashboard": "DashboardTests",
    "restart_checker": "RestartTests",
    "review_workflow": "ReviewTests",
    "conflict_lab": "ConflictTests",
    "local_service": "LocalServiceTests",
    "conflict_explorer": "ExplorerTests",
    "challenge_factory": "ChallengeTests",
    "plan_revision": "RevisionTests",
    "case_workflow": "WorkflowTests",
    "coverage_planner": "CoverageTests",
    "service_agent": "ServiceAgentTests",
    "agent_evaluation": "EvaluationTests",
    "terminal_guard": "TerminalTests",
    "agent_team": "TeamTests",
    "verify_service_agent_evidence": "EvidenceSourceTests",
    "team_workspace": "WorkspaceTests",
    "team_evaluation": "EvaluationTests",
    "team_evaluation_scoring": "ScoringTests",
    "runtime_sessions": "SessionTests",
    "durable_runtime_tests":"RuntimeTests",
    "connected_team_tests":"ConnectedTests",
    "connected_evaluation_tests":"EvaluationTests",
    "acceptance_tests":"AcceptanceTests",
    "connected_dashboard_tests":"DashboardTests",
    "coordination_tests":"PlannerTests",
    "coordination_broker_tests":"BrokerTests",
    "coordination_benchmark_tests":"BenchmarkTests",
    "coordination_dashboard_tests":"DashboardTests",
    "mission_tests":"MissionTests",
    "mission_dashboard_tests":"DashboardTests",
    "operations_access_tests":"AccessTests",
    "operations_planner_tests":"PlannerTests",
    "operations_gateway_tests":"GatewayTests",
    "operations_tests":"OperationsTests",
    "operations_server_tests":"ServerTests",
    "operations_outcomes_tests":"OutcomeTests",
    "operations_evidence_tests":"EvidenceTests",
    "operations_proposals_tests":"ProposalTests",
    "operations_provider_tests":"ProviderSetupTests",
    "hosted_tests":"HostedTests",
    "judge_setup_tests":"JudgeSetupTests",
    "downloadable_demo_tests":"DownloadableTests",
    "judge_evaluation_tests":"JudgeEvaluationTests",
    "judge_relay_tests":"RelayTests",
    "browser_proposals_tests":"BrowserKeyTests",
    "live_readiness_tests":"LiveEvidenceTests",
    "release_check_tests":"ReleaseTests",
    "factory_publication_tests":"PublicationTests",
    "windows_compat_tests":"CompatibilityTests",
    "native_process_tests":"ProcessTests",
}


def source_hashes():
    return {name + ".py": hashlib.sha256((ROOT / (name + ".py")).read_bytes()).hexdigest()
            for name in (*MODULES, "desktop_entry", "desktop_launcher", "build_desktop", "safety_environment", "safety_variants", "safety_inspect", "safety_inspect_check", "safety_lab", "safety_live", "safety_http", "safety_ui", "judge_setup", "judge_evaluation", "downloadable_demo", "judge_relay", "relay_demo", "browser_proposals", "team_workspace_ui", "team_evaluation_ui", "runtime_sessions_ui", "connected_team_ui",
                         "durable_runtime","connected_team","connected_evaluation","acceptance","change_coordinator","coordination_reference","coordination_benchmark","coordination_ui","mission_control","mission_ui","operations_workspace","workflow_planner","named_access","inference_gateway","operations_supervisor","operations_server","operations_ui","operations_outcomes","operations_evidence","operations_proposals","hosted_server","live_readiness","release_archive","release_check","campaign_check","process_check","native_process","test_paths","windows_preflight","project") if (ROOT / (name + ".py")).is_file()}


def sdk_version():
    try:
        return importlib.metadata.version("openai")
    except importlib.metadata.PackageNotFoundError:
        return None


def doctor():
    print("ORKESTRYN - SETUP CHECK")
    print("Python: " + platform.python_version())
    print("Project: " + str(ROOT))
    missing = []
    for module in MODULES:
        name = module + ".py"
        ready = (ROOT / name).is_file()
        print(("READY   " if ready else "MISSING ") + name)
        if not ready:
            missing.append(name)
    version = sdk_version()
    print("Live SDK: openai " + version if version else "Live SDK: not installed (optional for offline work)")
    manifest = ROOT / "bundle_manifest.json"
    if manifest.is_file():
        try:
            recorded = json.loads(manifest.read_text(encoding="utf-8"))["source_sha256"]
            changed = [name for name, value in source_hashes().items() if recorded.get(name) != value]
            print("Bundle source hashes: " + ("match" if not changed else "edited files: " + ", ".join(changed)))
        except (ValueError, KeyError, OSError):
            print("Bundle source hashes: manifest could not be read")
    print("No API request was made. Credentials were not inspected.")
    if missing:
        print("Copy the complete package into this folder before continuing.")
        return 1
    return 0


def check():
    print("ORKESTRYN - OFFLINE VERIFICATION", flush=True)
    print("Fixtures, local worker/HTTP integration and mocked SDK calls; no API key or credits needed.", flush=True)
    loader, suite, module_counts = unittest.TestLoader(), unittest.TestSuite(), {}
    for name, class_name in MODULES.items():
        module = importlib.import_module(name)
        tests = loader.loadTestsFromTestCase(getattr(module, class_name))
        module_counts[name] = tests.countTestCases()
        suite.addTests(tests)
        print("  %s: %d checks" % (name, module_counts[name]), flush=True)
    import test_paths
    test_paths.OBSERVATIONS.clear()
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, buffer=True).run(suite)
    now = datetime.now(timezone.utc)
    directory = ROOT / "check_runs" / (now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    report = {
        "mode": "OFFLINE UNIT AND INTEGRATION CHECKS; MOCKED MODEL CALLS",
        "model_calls": 0, "passed": result.wasSuccessful(), "tests_run": result.testsRun,
        "failure_count": len(result.failures), "error_count": len(result.errors),
        "skipped_count": len(result.skipped), "module_counts": module_counts,
        "failed_tests": [test.id() for test, _ in result.failures],
        "error_tests": [test.id() for test, _ in result.errors],
        "failure_details": [{"test":test.id(),"traceback":detail} for test,detail in result.failures],
        "error_details": [{"test":test.id(),"traceback":detail} for test,detail in result.errors],
        "python_version": platform.python_version(), "platform": platform.system(),
        "source_sha256": source_hashes(), "sdk_version": sdk_version(),
        "path_guard_fixture_boundaries":list(test_paths.OBSERVATIONS),
        "created_at": now.isoformat(), "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": "Checks coordinator behavior, bounded operator interleavings, process interruption/restart, real local workers/HTTP, proposal validation, fresh revision approval and exact execution, reference scenarios, strict scoring and dashboard controls. This is not live model accuracy.",
    }
    path = directory / "summary.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\nChecks: %d | Failures: %d | Errors: %d | Skipped: %d" % (
        result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
    print("Verification record: " + str(path))
    return 0 if result.wasSuccessful() else 1


def run_offline(filename, arguments):
    env = os.environ.copy()
    for key in ("NEBIUS_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-u", str(ROOT / filename), *arguments],
                          cwd=str(ROOT), env=env, shell=False).returncode


def run_model_script(filename, arguments, live=False):
    if not live:
        return run_offline(filename, arguments)
    # Only this explicit live command inherits the Nebius credential. The
    # child can also prompt locally; credentials never enter command arguments.
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-u", str(ROOT / filename), *arguments],
                          cwd=str(ROOT), env=env, shell=False).returncode


def run_challenge(arguments, live=False):
    return run_model_script("challenge_factory.py", arguments, live)


def main():
    if len(sys.argv)>1 and sys.argv[1]=="safety":
        import safety_lab
        return safety_lab.main(sys.argv[2:])
    if len(sys.argv)>1 and sys.argv[1]=="process-check":
        import process_check
        return process_check.main(sys.argv[2:])
    if len(sys.argv)>1 and sys.argv[1]=="campaign-check":
        import campaign_check
        return campaign_check.main(sys.argv[2:])
    if len(sys.argv)>1 and sys.argv[1]=="preflight":
        import windows_preflight
        return windows_preflight.main(sys.argv[2:])
    if len(sys.argv)>1 and sys.argv[1]=="operations":
        from operations_workspace import main as operations_main
        return operations_main(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("safety",help="Bounded AI-control evaluations, independent scoring and portable research evidence; no automatic model requests.")
    commands.add_parser("operations",help="Authenticated operations workspace, configurable workflows, emergency failover and inference gateway.")
    commands.add_parser("preflight",help="Short native shutdown/restart and Windows-regression preflight; no model calls.")
    commands.add_parser("doctor", help="Check the local files, Python and optional live SDK.")
    commands.add_parser("check", help="Run all offline checks and save their result.")
    commands.add_parser("campaign-check",help="Focused native check of evidence publication and the six-case dashboard campaign; no model calls.")
    commands.add_parser("process-check",help="Focused native check of Windows process observation and connected restart cleanup; no model calls.")
    restart = commands.add_parser("restart-check", help="Terminate dedicated simulation workers and verify recovery in new processes.")
    restart.add_argument("--open", action="store_true", help="Open the saved restart verification report.")
    conflict = commands.add_parser("conflict-lab", help="Explore operator orderings and compare recovery choices offline.")
    conflict.add_argument("--open", action="store_true")
    conflict.add_argument("--scenario", choices=("all", "routing", "dependency"), default="all")
    conflict.add_argument("--fault", choices=("all", "workflow_failure", "candidate_failure", "stop_timeout", "fallback_unhealthy"), default="all")
    conflict.add_argument("--max-schedules", type=int, default=None)
    explorer = commands.add_parser("conflict-explorer", help="Vary capacity, fault timing and observations; reduce baseline counterexamples.")
    explorer.add_argument("--open", action="store_true")
    explorer.add_argument("--scenario", choices=("all", "routing", "dependency"), default="all")
    explorer.add_argument("--profile", default="all")
    explorer.add_argument("--fault", default="all")
    explorer.add_argument("--max-schedules", type=int, default=8)
    explorer.add_argument("--replay", type=Path, help="Replay an Explorer case or reduced witness in a fresh folder.")
    challenge = commands.add_parser("challenge-factory", help="Validate proposed challenge programs; offline demo by default.")
    challenge.add_argument("--open", action="store_true")
    challenge.add_argument("--live", action="store_true", help="Use Nebius credits for up to the requested number of model calls.")
    challenge.add_argument("--calls", type=int, default=2)
    challenge.add_argument("--max-output-tokens", type=int, default=10000)
    challenge.add_argument("--thinking", choices=("on", "off"), default="on")
    challenge.add_argument("--replay", type=Path, help="Validate and replay a saved proposal offline.")
    challenge.add_argument("--against", type=Path, help="Deduplicate against a previous accepted_proposals.jsonl file.")
    revision = commands.add_parser("plan-revision", help="Rehearse a smaller plan, record fresh local manager consent and execute exactly the approved reservation.")
    revision_modes = revision.add_mutually_exclusive_group()
    revision_modes.add_argument("--live", action="store_true", help="Request one paid Nemotron proposal; stop for review.")
    revision_modes.add_argument("--prepare", action="store_true")
    revision_modes.add_argument("--approve", metavar="REVISION_ID")
    revision_modes.add_argument("--execute", metavar="REVISION_ID")
    revision_modes.add_argument("--recover", action="store_true")
    revision_modes.add_argument("--inspect", action="store_true")
    revision.add_argument("--run", type=Path)
    revision.add_argument("--note")
    revision.add_argument("--proposal", type=Path)
    revision.add_argument("--scenario", choices=("budget_conflict", "protected_route", "protected_dependency", "no_headroom"), default="budget_conflict")
    revision.add_argument("--max-output-tokens", type=int, default=10000)
    revision.add_argument("--thinking", choices=("on", "off"), default="on")
    revision.add_argument("--open", action="store_true")
    workflow = commands.add_parser("case-workflow", help="Connect validated challenges to revision review and recovery; eight-case offline demo by default.")
    workflow.add_argument("--live", action="store_true")
    workflow.add_argument("--from-factory", type=Path)
    workflow.add_argument("--cases", type=int, default=5)
    workflow.add_argument("--calls", type=int, default=10)
    workflow.add_argument("--max-output-tokens", type=int, default=10000)
    workflow.add_argument("--thinking", choices=("on", "off"), default="on")
    workflow.add_argument("--coverage", choices=("balanced", "repair", "off"), default="balanced")
    workflow.add_argument("--run", type=Path)
    workflow.add_argument("--inspect", action="store_true")
    workflow.add_argument("--open", action="store_true")
    coverage_lab = commands.add_parser("coverage-lab", help="Exercise observed coverage targets and adaptive feedback with free offline fixtures.")
    coverage_lab.add_argument("--open", action="store_true")
    service_agent = commands.add_parser("service-agent", help="Run the free four-case real-worker agent lab, or explicitly request an interactive two-call Nemotron agent.")
    service_agent.add_argument("--live", action="store_true")
    service_agent.add_argument("--challenge", type=Path)
    service_agent.add_argument("--scenario", choices=("routing", "dependency", "unprotected"), default="routing")
    service_agent.add_argument("--max-output-tokens", type=int, default=10000)
    service_agent.add_argument("--thinking", choices=("on", "off"), default="on")
    service_agent.add_argument("--open", action="store_true")
    evaluation = commands.add_parser("agent-evaluation", help="Run the fixed 14-case free agent regression, or an explicitly bounded interactive live evaluation.")
    evaluation.add_argument("--live", action="store_true")
    evaluation.add_argument("--case", action="append", dest="case_ids")
    evaluation.add_argument("--calls", type=int, default=24)
    evaluation.add_argument("--max-output-tokens", type=int, default=10000)
    evaluation.add_argument("--thinking", choices=("on", "off"), default="on")
    evaluation.add_argument("--open", action="store_true")
    team_evaluation = commands.add_parser("team-evaluation", help="Run the frozen free three-role evaluation or explicitly bounded live requests; verify saved scores without effects.")
    team_evaluation.add_argument("--split", choices=("development", "confirmation"), default="development")
    team_evaluation.add_argument("--case", action="append", dest="case_ids")
    team_evaluation.add_argument("--live", action="store_true")
    team_evaluation.add_argument("--calls", type=int, default=3)
    team_evaluation.add_argument("--max-output-tokens", type=int, default=4096)
    team_evaluation.add_argument("--thinking", choices=("on", "off"), default="on")
    team_evaluation.add_argument("--verify-run", type=Path)
    team_evaluation.add_argument("--open", action="store_true")
    connected = commands.add_parser("connected-team",help="Original three-role service continues through owner review; restart the same durable runtime.")
    connected.add_argument("action",nargs="?",choices=("start","status","review","resume","restart","stop","probe"),default="start")
    connected.add_argument("--scenario",choices=("routing","dependency"),default="dependency")
    connected.add_argument("--ttl",type=int,default=900)
    connected.add_argument("--session")
    connected.add_argument("--decision",choices=("retain","withdraw_route","release_dependency"))
    connected.add_argument("--request",dest="request_key")
    connected.add_argument("--note",default="C records its exact local decision.")
    connected.add_argument("--live",action="store_true")
    connected.add_argument("--max-output-tokens",type=int,default=4096)
    connected.add_argument("--thinking",choices=("on","off"),default="on")
    connected.add_argument("--open",action="store_true")
    combined = commands.add_parser("connected-evaluation",help="Ten development cases or six explicit reserved combinations; read-only score audit and matched comparison.")
    combined.add_argument("--split",choices=("development","confirmation"),default="development")
    combined.add_argument("--case",action="append",dest="case_ids")
    combined.add_argument("--live",action="store_true")
    combined.add_argument("--calls",type=int,default=30)
    combined.add_argument("--max-output-tokens",type=int,default=4096)
    combined.add_argument("--thinking",choices=("on","off"),default="on")
    combined.add_argument("--verify-run",type=Path)
    combined.add_argument("--compare",nargs=2,type=Path)
    acceptance = commands.add_parser("acceptance",help="Native platform acceptance, real supervisor restart walkthrough and optional bounded live benchmarks.")
    acceptance.add_argument("--live",action="store_true")
    acceptance.add_argument("--live-only",action="store_true")
    acceptance.add_argument("--walkthrough-only",action="store_true")
    acceptance.add_argument("--split",choices=("development","confirmation"),default="development")
    acceptance.add_argument("--max-output-tokens",type=int,default=4096)
    acceptance.add_argument("--thinking",choices=("on","off"),default="on")
    mission = commands.add_parser("mission",help="Persistent Factory/Governance/Recovery mission with measured monitoring, exact approvals and C owner continuation.")
    mission.add_argument("action",nargs="?",choices=("demo","start","advance","approve","observe","watch","recover","owner","status","restart","stop","verify","simulate-failure"),default="demo")
    mission.add_argument("--mission")
    mission.add_argument("--ttl",type=int,default=900)
    mission.add_argument("--goal",default="Coordinate a deployment, the approved cost limit and customer traffic safely.")
    mission.add_argument("--live",action="store_true")
    mission.add_argument("--max-output-tokens",type=int,default=8192)
    mission.add_argument("--thinking",choices=("on","off"),default="on")
    mission.add_argument("--decision",choices=("retain","withdraw_route","release_dependency"))
    mission.add_argument("--fault",choices=("workflow","candidate"),default="workflow")
    mission.add_argument("--seconds",type=int,default=20)
    mission.add_argument("--interval",type=int,default=2)
    coordination = commands.add_parser("coordination",help="Competing intent queue, exact batch consent, deterministic or live order proposal, real local execution and read-only audit.")
    coordination.add_argument("action",nargs="?",choices=("demo","start","submit","cancel","plan","approve","execute","status","restart","recover","stop","verify"),default="demo")
    coordination.add_argument("--session")
    coordination.add_argument("--plan",dest="plan_id")
    coordination.add_argument("--ttl",type=int,default=900)
    coordination.add_argument("--actor",choices=("A","B","C"))
    coordination.add_argument("--id",dest="intent_id")
    coordination.add_argument("--tool",choices=("reserve_capacity","set_capacity_limit","set_route","add_dependency"))
    coordination.add_argument("--parameters",default="{}")
    coordination.add_argument("--priority",type=int,default=3)
    coordination.add_argument("--seed-requests",action="store_true")
    coordination.add_argument("--live",action="store_true")
    coordination.add_argument("--max-output-tokens",type=int,default=8192)
    coordination.add_argument("--thinking",choices=("on","off"),default="on")
    ordering = commands.add_parser("coordination-evaluation",help="Twelve policy-only scheduling fixtures; optional at most twelve paid order proposals, no workers or real effects.")
    ordering.add_argument("--case",action="append",dest="case_ids")
    ordering.add_argument("--live",action="store_true")
    ordering.add_argument("--calls",type=int,default=12)
    ordering.add_argument("--max-output-tokens",type=int,default=8192)
    ordering.add_argument("--thinking",choices=("on","off"),default="on")
    ordering.add_argument("--verify-run",type=Path)
    team = commands.add_parser("agent-team", help="Coordinate scenario planning, governance and recovery; free demo or at most three explicit live requests.")
    team.add_argument("--live", action="store_true")
    team.add_argument("--scenario", choices=("routing", "dependency"), default="dependency")
    team.add_argument("--max-output-tokens", type=int, default=10000)
    team.add_argument("--thinking", choices=("on", "off"), default="on")
    team.add_argument("--open", action="store_true")
    sessions = commands.add_parser("runtime-sessions", help="Leased real local workers, reconnect and exact terminal owner review; no model calls.")
    sessions.add_argument("action", nargs="?", choices=("demo", "start", "status", "review", "resume", "probe", "stop"), default="demo")
    sessions.add_argument("--scenario", choices=("routing", "dependency"), default="routing")
    sessions.add_argument("--ttl", type=int, default=900)
    sessions.add_argument("--session")
    sessions.add_argument("--decision", choices=("retain", "withdraw_route", "release_dependency"))
    sessions.add_argument("--request", dest="request_key")
    sessions.add_argument("--note", default="C records its bounded local decision.")
    sessions.add_argument("--open", action="store_true")
    local = commands.add_parser("local-service", help="Run real local workers and HTTP recovery cases; no model calls.")
    local.add_argument("--open", action="store_true")
    local.add_argument("--scenario", choices=("all", "workflow_failure", "routing_conflict", "dependency_conflict",
                       "candidate_failure", "fallback_failure", "lost_start_reply", "lost_stop_reply", "stop_unconfirmed"), default="all")
    local.add_argument("--replay", type=Path, help="Replay a previously exported case_spec.json in a fresh run.")
    demo = commands.add_parser("demo", help="Create four scripted rehearsal runs and a reference dataset.")
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--variants", type=int, choices=range(5, 11), default=5, metavar="5..10")
    demo.add_argument("--open", action="store_true", help="Open the dashboard after generating the demo evidence.")
    dashboard = commands.add_parser("dashboard", help="Open the local dashboard; model runs remain separate commands.")
    dashboard.add_argument("--open", action="store_true")
    dashboard.add_argument("--read-only", action="store_true")
    dashboard.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "doctor":
        return doctor()
    if any(not (ROOT / (name + ".py")).is_file() for name in MODULES):
        return doctor()
    if args.command == "check":
        return check()
    if args.command == "restart-check":
        return run_offline("restart_checker.py", ["--open"] if args.open else [])
    if args.command == "mission":
        flags=[args.action,"--ttl",str(args.ttl),"--goal",args.goal,"--max-output-tokens",str(args.max_output_tokens),"--thinking",args.thinking,"--fault",args.fault,"--seconds",str(args.seconds),"--interval",str(args.interval)]
        if args.mission:flags += ["--mission",args.mission]
        if args.decision:flags += ["--decision",args.decision]
        if args.live:flags.append("--live")
        return run_model_script("mission_control.py",flags,args.live)
    if args.command == "coordination":
        flags=[args.action,"--ttl",str(args.ttl),"--parameters",args.parameters,"--priority",str(args.priority),"--max-output-tokens",str(args.max_output_tokens),"--thinking",args.thinking]
        for name,option in (("session","--session"),("plan_id","--plan"),("actor","--actor"),("intent_id","--id"),("tool","--tool")):
            if getattr(args,name) is not None:flags += [option,getattr(args,name)]
        for name in ("seed_requests","live"):
            if getattr(args,name):flags.append("--"+name.replace("_","-"))
        return run_model_script("change_coordinator.py",flags,args.live)
    if args.command == "coordination-evaluation":
        flags=["--calls",str(args.calls),"--max-output-tokens",str(args.max_output_tokens),"--thinking",args.thinking]
        for case in args.case_ids or []:flags += ["--case",case]
        if args.verify_run:flags += ["--verify-run",str(args.verify_run.resolve())]
        if args.live:flags.append("--live")
        return run_model_script("coordination_benchmark.py",flags,args.live)
    if args.command == "connected-team":
        flags=[args.action,"--scenario",args.scenario,"--ttl",str(args.ttl),"--note",args.note,
               "--max-output-tokens",str(args.max_output_tokens),"--thinking",args.thinking]
        for name,option in (("session","--session"),("decision","--decision"),("request_key","--request")):
            if getattr(args,name) is not None:flags += [option,getattr(args,name)]
        if args.open:flags.append("--open")
        if args.live:flags.append("--live")
        return run_model_script("connected_team.py",flags,args.live)
    if args.command == "connected-evaluation":
        flags=["--split",args.split,"--calls",str(args.calls),"--max-output-tokens",str(args.max_output_tokens),"--thinking",args.thinking]
        if args.live:flags.append("--live")
        for case in args.case_ids or []:flags += ["--case",case]
        if args.verify_run:flags += ["--verify-run",str(args.verify_run.resolve())]
        if args.compare:flags += ["--compare",*[str(path.resolve()) for path in args.compare]]
        return run_model_script("connected_evaluation.py",flags,args.live)
    if args.command == "acceptance":
        flags=["--split",args.split,"--max-output-tokens",str(args.max_output_tokens),"--thinking",args.thinking]
        for name in ("live","live_only","walkthrough_only"):
            if getattr(args,name):flags.append("--"+name.replace("_","-"))
        return run_model_script("acceptance.py",flags,args.live or args.live_only)
    if args.command == "runtime-sessions":
        flags = [args.action, "--scenario", args.scenario, "--ttl", str(args.ttl), "--note", args.note]
        for name, option in (("session", "--session"), ("decision", "--decision"), ("request_key", "--request")):
            if getattr(args, name) is not None:
                flags += [option, getattr(args, name)]
        if args.open:
            flags.append("--open")
        return run_offline("runtime_sessions.py", flags)
    if args.command == "case-workflow":
        flags = ["--cases", str(args.cases), "--calls", str(args.calls), "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking, "--coverage", args.coverage]
        for name in ("live", "inspect", "open"):
            if getattr(args, name):
                flags.append("--" + name)
        for name in ("from_factory", "run"):
            value = getattr(args, name)
            if value is not None:
                flags += ["--" + name.replace("_", "-"), str(value.resolve())]
        return run_model_script("case_workflow.py", flags, args.live)
    if args.command == "coverage-lab":
        return run_offline("coverage_planner.py", ["--open"] if args.open else [])
    if args.command == "agent-evaluation":
        flags = ["--calls", str(args.calls), "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking]
        for name in ("live", "open"):
            if getattr(args, name): flags.append("--" + name)
        for case_id in args.case_ids or []: flags += ["--case", case_id]
        return run_model_script("agent_evaluation.py", flags, args.live)
    if args.command == "team-evaluation":
        flags = ["--split", args.split, "--calls", str(args.calls), "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking]
        for name in ("live", "open"):
            if getattr(args, name): flags.append("--" + name)
        for case_id in args.case_ids or []: flags += ["--case", case_id]
        if args.verify_run is not None: flags += ["--verify-run", str(args.verify_run.resolve())]
        return run_model_script("team_evaluation.py", flags, args.live)
    if args.command == "agent-team":
        flags = ["--scenario", args.scenario, "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking]
        for name in ("live", "open"):
            if getattr(args, name): flags.append("--" + name)
        return run_model_script("agent_team.py", flags, args.live)
    if args.command == "service-agent":
        flags = ["--scenario", args.scenario, "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking]
        if args.challenge:
            flags += ["--challenge", str(args.challenge.resolve())]
        if args.live:
            flags.append("--live")
        if args.open:
            flags.append("--open")
        return run_model_script("service_agent.py", flags, args.live)
    if args.command == "plan-revision":
        flags = ["--scenario", args.scenario, "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking]
        for name in ("live", "prepare", "recover", "inspect", "open"):
            if getattr(args, name):
                flags.append("--" + name)
        for name in ("approve", "execute", "note", "run", "proposal"):
            value = getattr(args, name)
            if value is not None:
                flags += ["--" + name, str(value.resolve()) if isinstance(value, Path) else value]
        return run_model_script("plan_revision.py", flags, args.live)
    if args.command == "challenge-factory":
        if args.live and args.replay:
            parser.error("--replay is offline and cannot be combined with --live.")
        flags = ["--calls", str(args.calls), "--max-output-tokens", str(args.max_output_tokens), "--thinking", args.thinking]
        for name in ("replay", "against"):
            if getattr(args, name):
                flags += ["--" + name, str(getattr(args, name).resolve())]
        if args.live:
            flags.append("--live")
        if args.open:
            flags.append("--open")
        return run_challenge(flags, live=args.live)
    if args.command == "conflict-explorer":
        flags = ["--scenario", args.scenario, "--profile", args.profile,
                 "--fault", args.fault, "--max-schedules", str(args.max_schedules)]
        if args.replay:
            flags += ["--replay", str(args.replay.resolve())]
        if args.open:
            flags.append("--open")
        return run_offline("conflict_explorer.py", flags)
    if args.command == "local-service":
        flags = ["--scenario", args.scenario]
        if args.replay:
            flags += ["--replay", str(args.replay.resolve())]
        if args.open:
            flags.append("--open")
        return run_offline("local_service.py", flags)
    if args.command == "conflict-lab":
        flags = ["--scenario", args.scenario, "--fault", args.fault]
        if args.max_schedules is not None:
            flags += ["--max-schedules", str(args.max_schedules)]
        if args.open:
            flags.append("--open")
        return run_offline("conflict_lab.py", flags)
    if args.command == "demo":
        print("OFFLINE DEMO SETUP: scripted operators and synthetic infrastructure; no model calls.", flush=True)
        for filename, flags in (("simulator.py", ["--scenario", "all"]),
                                ("data_factory.py", ["--seed", str(args.seed), "--variants", str(args.variants)])):
            code = run_offline(filename, flags)
            if code:
                print("Demo setup stopped. Inspect the runner output above.")
                return code
        print("\nDemo evidence saved. Live model scorecards are populated only by separate live runs.", flush=True)
        if not args.open:
            print("Open with: python project.py dashboard --open")
            return 0
    flags = ["--port", str(getattr(args, "port", 8765))]
    if args.open:
        flags.append("--open")
    if getattr(args, "read_only", False):
        flags.append("--read-only")
    return run_offline("dashboard.py", flags)


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        print("Orkestryn requires Python 3.10 or newer.", file=sys.stderr)
        raise SystemExit(1)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped. Saved evidence is retained.", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        print("PROJECT COMMAND FAILED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
