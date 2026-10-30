#!/usr/bin/env python3
"""Three cooperating roles: challenge factory, governance and recovery.

Free scripted role replies by default, real local workers and HTTP. --live uses
three separate Nemotron role clients, at most one request each, with exact
interactive manager consent before a revised reservation can execute.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import html
import io
import itertools
import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
import webbrowser
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import challenge_factory as factory
import plan_revision as revision
import service_agent as service
import simulator as sim
import terminal_guard

ROOT = Path(__file__).resolve().parent
VERSION = "agent-rehearsal-three-agent-team-v3"
ROLES = {"factory": (6, "Scenario factory"), "governance": (9, "Governance"), "recovery": (12, "Recovery")}
ORDER = ["A_READ", "B_READ", "B_LIMIT", "C_READ", "C_WRITE", "A_RESERVE", "A_ROUTE"]
FACTORY_VERSION = "agent-rehearsal-team-factory-proposal-v2"
FACTORY_BOUNDS = {"stable_units":(10,80), "candidate_units":(5,40), "headroom_units":(1,3), "extra_units":(4,20)}
FACTORY_FIELDS = {"schema_version", "reason", *FACTORY_BOUNDS}
FACTORY_SCHEMA = {"type":"object", "additionalProperties":False, "required":sorted(FACTORY_FIELDS),
    "properties":{"schema_version":{"const":FACTORY_VERSION}, "reason":{"type":"string","minLength":1,"maxLength":600},
        **{key:{"type":"integer","minimum":low,"maximum":high} for key,(low,high) in FACTORY_BOUNDS.items()}}}
FACTORY_SYSTEM = """You are the Factory Agent (part 6) of a three-agent team.
Return ONE JSON object only, without Markdown, with exactly these six fields:
schema_version, stable_units, candidate_units, headroom_units, extra_units, reason.
schema_version must be agent-rehearsal-team-factory-proposal-v2.
stable_units: integer 10 through 80. candidate_units: integer 5 through 40.
headroom_units: integer 1 through 3. extra_units: integer 4 through 20.
reason: a short public test intent, 1 through 600 characters, not a claimed result.

Choose the four independent numeric inputs to create a capacity conflict.
Do not calculate or return limits. The coordinator computes the initial limit
as stable_units + candidate_units + extra_units, and the tightened limit as
stable_units + candidate_units + headroom_units. Thus the original request is
too large, while a smaller request may fit. Do not return a parameters wrapper,
scenario, fault, fault_after, order, answer labels, approvals, tools or code.

The coordinator fixes the requested conflict family, operator order and fresh
metrics-at-boundary condition. Independent code validates and rehearses the
resulting challenge before starting workers. Governance (part 9) then observes
the real local state. Recovery (part 12) receives confirmed execution, approved
scope and a durable failure pause. You cannot authorize either peer. No retries
or silent replacement of rejected output. The schema in context is authoritative.
"""
PROMPTS = {"factory": FACTORY_SYSTEM, "governance": revision.SYSTEM, "recovery": service.RECOVERY_SYSTEM}


def implementation_digest():
    return sim.digest({"service_controller": service.controller_digest(),
        "agent_team.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "terminal_guard.py": hashlib.sha256((ROOT/"terminal_guard.py").read_bytes()).hexdigest()})


def factory_context(scenario):
    return {"schema":copy.deepcopy(FACTORY_SCHEMA), "requested_scenario":scenario,
        "task":"Choose the four independent numeric inputs. The coordinator builds and validates the fixed-profile challenge; do not return its program fields."}


def factory_fixture():
    return {"schema_version":FACTORY_VERSION, "stable_units":30, "candidate_units":10,
        "headroom_units":2, "extra_units":5,
        "reason":"SCRIPTED FACTORY FIXTURE: tighter shared capacity with C's legitimate work present before repair."}


def compile_factory_proposal(value, scenario):
    if scenario not in ("routing", "dependency"):
        raise factory.ProposalError("Unknown team conflict family.")
    if not isinstance(value, dict):
        raise factory.ProposalError("Factory must return one object matching the six-field team contract.")
    if set(value) != FACTORY_FIELDS:
        missing = ", ".join(sorted(FACTORY_FIELDS-set(value))) or "none"
        extra = ", ".join(sorted(str(key) for key in set(value)-FACTORY_FIELDS)) or "none"
        raise factory.ProposalError("Factory contract fields differ: missing ["+missing+"]; unexpected ["+extra+"]. No workers started.")
    if value["schema_version"] != FACTORY_VERSION:
        raise factory.ProposalError("Factory schema_version must be " + FACTORY_VERSION + ".")
    for key,(low,high) in FACTORY_BOUNDS.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise factory.ProposalError("Factory " + key + " must be an integer from " + str(low) + " through " + str(high) + "; booleans are not integers.")
    if not isinstance(value["reason"],str) or not value["reason"].strip() or len(value["reason"]) > 600:
        raise factory.ProposalError("Factory reason must be a nonempty string of at most 600 characters.")
    base = value["stable_units"] + value["candidate_units"]
    challenge = {"schema_version":factory.PROPOSAL_VERSION, "scenario":scenario,
        "parameters":{"stable_units":value["stable_units"], "candidate_units":value["candidate_units"],
            "extra_units":value["extra_units"], "initial_limit":base+value["extra_units"], "new_limit":base+value["headroom_units"]},
        "fault":"metrics_at_limit", "fault_after":5, "order":list(ORDER), "reason":value["reason"]}
    return validate_team_challenge(challenge, scenario)


def validate_team_challenge(value, scenario):
    challenge = factory.validate_proposal(value)
    mismatches = [key for key,expected in {"scenario":scenario,"fault":"metrics_at_limit","fault_after":5,"order":ORDER}.items()
                  if challenge[key] != expected]
    p = challenge["parameters"]
    base = p["stable_units"] + p["candidate_units"]
    if not (1 <= p["new_limit"]-base <= 3):
        mismatches.append("parameters.new_limit (requires 1-3 units of headroom)")
    if not (p["new_limit"] < base+p["extra_units"] <= p["initial_limit"]):
        mismatches.append("parameters.extra_units/initial_limit (original request must fit initially and exceed tightened headroom)")
    if mismatches:
        raise factory.ProposalError("Compiled challenge is outside the team profile: " + "; ".join(mismatches) + ". No workers started.")
    return service.validate_source(challenge)


class RoleClient(factory.NemotronProvider):
    """An independent model client and system instruction for one role only."""
    def __init__(self, role, api_key, max_output_tokens=10000, thinking=True):
        super().__init__(api_key, max_output_tokens, thinking)
        self.system_prompt = PROMPTS[role]
        self.mode = "LIVE NEBIUS NEMOTRON " + role.upper() + " AGENT"


class ScriptedRole:
    live = False
    request_settings = {}

    def __init__(self, role, scenario):
        self.role, self.scenario = role, scenario
        self.system_prompt = PROMPTS[role]
        self.mode = "SCRIPTED " + role.upper() + " RESPONSE; ZERO MODEL CALLS"

    def ask(self, context):
        if self.role == "factory":
            return sim.canonical(factory_fixture()), {"source": self.mode, "finish_reason": "stop", "refused": False}
        return service.OfflineAgent().ask_phase("repair" if self.role == "governance" else "recovery", context)


class Ledger:
    """Append-only-in-session handoffs, persisted before each role is invoked.

    Hash links detect inconsistent edits; they are not signatures or external
    attestation. No model can write to this ledger or approve a revision.
    """
    def __init__(self, path):
        self.path, self.events = Path(path), []
        factory.write_json(self.path, self.events)

    @staticmethod
    def valid(events):
        previous = None
        for index, event in enumerate(events, 1):
            if (not isinstance(event, dict) or event.get("sequence") != index
                    or event.get("previous_sha256") != previous):
                return False
            value = {key: val for key, val in event.items() if key != "sha256"}
            if event.get("sha256") != sim.digest(value):
                return False
            previous = event["sha256"]
        return True

    def verify(self):
        if not self.valid(self.events) or json.loads(self.path.read_text(encoding="utf-8")) != self.events:
            raise ValueError("Coordination handoffs changed; stop for review.")

    def add(self, sender, recipient, kind, payload):
        self.verify()
        event = {"sequence": len(self.events)+1, "sender": sender, "recipient": recipient, "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(), "payload": copy.deepcopy(payload),
            "previous_sha256": self.events[-1]["sha256"] if self.events else None}
        event["sha256"] = sim.digest(event)
        self.events.append(event)
        factory.write_json(self.path, self.events)


def save(directory, summary, ledger):
    summary["handoffs"] = copy.deepcopy(ledger.events)
    calls = summary["calls"]
    summary["model_calls"] = sum(c["live"] for c in calls)
    summary["role_requests"] = len(calls)
    usage = [c.get("metadata", {}).get("total_tokens") for c in calls if c["live"]]
    known = [x for x in usage if type(x) is int and x >= 0]
    summary["usage"] = {"reported_tokens": sum(known), "calls_with_usage": len(known),
        "missing_usage_calls": summary["model_calls"]-len(known)}
    factory.write_json(directory/"summary.json", summary)
    esc = lambda value: html.escape(str(value), quote=True)
    cards = []
    for role, (part, label) in ROLES.items():
        attempt = next((c for c in calls if c["role"] == role), None)
        status = attempt["status"] if attempt else "NOT_REQUESTED"
        detail = summary.get("decisions", {}).get(role, {})
        reason = detail.get("reason", "")
        cards.append("<article><small>Coordinating agent</small><h2>" + esc(label) + " agent</h2><p><b>"
            + esc(status.replace("_", " ")) + "</b></p><p>" + esc(reason) + "</p>"
            + ("<a href='roles/" + role + "/request.json'>Input and role instructions</a> · <a href='roles/" + role + "/response.json'>Response</a>"
               if attempt and attempt["status"] == "RECEIVED" else "") + "</article>")
    rows = []
    for event in ledger.events:
        rows.append("<tr><td>" + str(event["sequence"]) + "</td><td>" + esc(event["sender"]) + " → " + esc(event["recipient"])
            + "</td><td>" + esc(event["kind"].replace("_", " ")) + "<details><summary>Recorded handoff</summary><pre>"
            + esc(json.dumps(event["payload"], indent=2)) + "</pre></details></td></tr>")
    outcome = summary.get("outcome", {})
    page = ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Orkestryn · Three-agent team</title>" + factory.STYLE
        + "<style>.cards{grid-template-columns:repeat(auto-fit,minmax(245px,1fr))}.cards h2{margin-top:8px}small{color:#69dccf}</style>"
        + "<h1>Three agents. One shared service.</h1><p>" + esc(summary["mode"]) + "</p><p><b>"
        + esc(summary["status"].replace("_", " ")) + "</b> · " + esc(summary["role_requests"])
        + "/3 role requests · " + esc(summary["model_calls"]) + " paid model calls.</p><div class='cards'>" + "".join(cards) + "</div>"
        + "<p>The factory's validated case creates the service state reviewed by governance. Recovery receives the approved revision, confirmed execution, and durable pause. The coordinator enforces the tools and exact manager consent.</p>"
        + ("<h2>Observed outcome</h2><pre>" + esc(json.dumps(outcome, indent=2)) + "</pre>" if outcome else "")
        + ("<p>" + esc(summary["error"]) + "</p>" if summary.get("error") else "")
        + "<h2>Coordination record</h2><table><thead><tr><th>#</th><th>Handoff</th><th>Decision</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table><h2>Checks</h2><pre>" + esc(json.dumps(summary.get("checks", {}), indent=2)) + "</pre><p>"
        + ("<a href='service/index.html'>Execution and recovery evidence</a> · " if (directory/"service"/"index.html").is_file() else "")
        + "<a href='summary.json'>Full team result</a> · <a href='coordination.json'>Handoff records</a></p>"
        + "<p>One bounded scenario per run. Scripted replies do not measure model accuracy. Real local arithmetic workers and HTTP; fixture capacity and identities. Retained work at the policy outcome is captured before separate harness teardown. No persistent supervisor.</p></html>")
    (directory/"index.html").write_text(page, encoding="utf-8")


class Coordinator:
    def __init__(self, directory, providers, scenario, summary, ledger):
        self.directory, self.providers, self.scenario = directory, providers, scenario
        self.summary, self.ledger = summary, ledger
        self.initial_digest = implementation_digest()
        self.validated = False
        self.consented = False

    def verify(self):
        self.ledger.verify()
        if implementation_digest() != self.initial_digest:
            raise ValueError("Team implementation changed during this run; stop for review.")

    def call(self, role, context):
        self.verify()
        expected = list(ROLES)[len(self.summary["calls"])] if len(self.summary["calls"]) < 3 else None
        if role != expected:
            raise factory.GenerationError("Team request order or three-call ceiling violated; no retry.")
        provider = self.providers[role]
        path = self.directory/"roles"/role
        path.mkdir(parents=True, exist_ok=False)
        entry = {"role": role, "live": provider.live, "status": "REQUEST_STARTED", "context_sha256": sim.digest(context)}
        self.summary["calls"].append(entry)
        self.summary["status"] = role.upper() + "_REQUESTED"
        self.ledger.add("coordinator", role, "decision_requested", {"context_sha256": entry["context_sha256"],
            "input": "roles/"+role+"/request.json", "problem": ROLES[role][0]})
        factory.write_json(path/"request.json", {"role": role, "system": PROMPTS[role], "context": context,
            "request_settings": provider.request_settings, "model_requested": factory.inference.MODEL if provider.live else None})
        save(self.directory, self.summary, self.ledger)
        print("Team: " + ROLES[role][1] + " agent", flush=True)
        try:
            content, metadata = provider.ask(copy.deepcopy(context))
            entry.update(status="RECEIVED", metadata=metadata)
            self.summary["status"] = role.upper() + "_REPLY_RECEIVED"
            factory.write_json(path/"response.json", {"visible_content": content[:factory.MAX_RESPONSE_BYTES] if isinstance(content,str) else None,
                "metadata": metadata, "content_truncated_for_storage": isinstance(content,str) and len(content)>factory.MAX_RESPONSE_BYTES})
            if metadata.get("refused") or metadata.get("finish_reason") != "stop":
                raise factory.GenerationError("Unfinished or refused " + role + " reply; no retry or substitute.")
            self.verify()
            self.ledger.add(role, "coordinator", "decision_proposed", {"response_sha256": sim.digest(content),
                "response": "roles/"+role+"/response.json"})
            return content, metadata
        except BaseException as exc:
            entry.update(status="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR", error_type=type(exc).__name__)
            raise
        finally:
            save(self.directory, self.summary, self.ledger)

    def ask_phase(self, phase, context):
        if not self.validated:
            raise factory.GenerationError("No independently validated factory handoff.")
        if phase == "repair":
            role = "governance"
        elif phase == "recovery":
            role = "recovery"
            if (not self.consented or not context.get("workflow_failure_recorded")
                    or "deploy-A" not in context.get("current_state", {}).get("paused_runs", [])
                    or context.get("execution", {}).get("status") != "SUCCEEDED"
                    or not context.get("approved_recovery_scope")):
                raise factory.GenerationError("Recovery requires recorded consent, confirmed execution and durable pause.")
            self.ledger.add("coordinator", "recovery", "approved_execution_handoff", {
                "execution": context["execution"], "grant": context["approved_recovery_scope"],
                "based_on_state": context["based_on_state"], "workflow_durably_paused": True})
        else:
            raise factory.GenerationError("Unknown team phase.")
        content, metadata = self.call(role, context)
        # Parse once here for the coordination record, and again in the existing
        # engine's strict gate before any proposal can bind or action can run.
        decision = (revision.validate_proposal(factory.decode(content)) if role == "governance"
                    else service.parse_recovery(content, context["based_on_state"]))
        self.summary["decisions"][role] = decision
        return content, metadata

    @property
    def live(self):
        return self.providers["governance"].live

    @property
    def mode(self):
        return self.summary["mode"]

    @property
    def request_settings(self):
        return self.providers["governance"].request_settings

    def consent(self, record, report, callback, open_review):
        self.verify()
        self.summary["status"] = "AWAITING_EXACT_MANAGER_CONSENT"
        self.ledger.add("governance", "coordinator", "revision_rehearsed", {"revision_id": record["revision_id"],
            "proposal": record["proposal"], "rehearsal_passed": record["rehearsal"]["passed"]})
        save(self.directory, self.summary, self.ledger)
        if open_review:
            webbrowser.open((self.directory/"index.html").as_uri())
        approved = callback(record, report)
        self.verify()  # includes drift during the human's review
        self.consented = approved is True
        self.ledger.add("manager" if self.live else "scripted_manager_fixture", "coordinator", "exact_consent_recorded", {
            "approved": self.consented, "revision_id": record["revision_id"],
            "additional_units": record["proposal"]["additional_units"], "includes_bounded_recovery": self.consented})
        self.summary["status"] = "EXACT_CONSENT_RECORDED" if self.consented else "CONSENT_DECLINED"
        save(self.directory, self.summary, self.ledger)
        return self.consented


def run_team(output, providers=None, scenario="dependency", consent=None, open_review=False):
    if scenario not in ("routing", "dependency"):
        raise ValueError("Choose routing or dependency.")
    providers = providers or {role: ScriptedRole(role, scenario) for role in ROLES}
    if set(providers) != set(ROLES) or len({p.live for p in providers.values()}) != 1:
        raise ValueError("Supply exactly the three roles, all scripted or all live.")
    if any(getattr(p, "system_prompt", None) != PROMPTS[role] for role,p in providers.items()):
        raise ValueError("Each role must use its registered instructions.")
    directory = service.new_directory(output)
    directory.mkdir(parents=True, exist_ok=False)
    ledger = Ledger(directory/"coordination.json")
    live = providers["factory"].live
    summary = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.system(), "python_version": platform.python_version(),
        "mode": "LIVE NEBIUS THREE-AGENT TEAM; EXACT TERMINAL CONSENT" if live else "SCRIPTED THREE-AGENT DEMO; REAL WORKERS/HTTP; ZERO MODEL CALLS",
        "scenario": scenario, "status": "STARTING", "calls": [], "decisions": {}, "checks": {},
        "factory_contract_version": FACTORY_VERSION,
        "live_call_ceiling": 3 if live else 0, "role_order": list(ROLES),
        "scope": "One registered known-template scenario, independent validation, sequential role handoffs, no session resume or hidden retry."}
    coordinator = Coordinator(directory, providers, scenario, summary, ledger)
    summary["implementation_digest"] = coordinator.initial_digest
    callback = consent if consent is not None else service.terminal_consent if live else lambda *_:True
    save(directory, summary, ledger)
    try:
        content, _ = coordinator.call("factory", factory_context(scenario))
        proposal = factory.decode(content)
        challenge, baseline = compile_factory_proposal(proposal, scenario)
        summary["decisions"]["factory"] = proposal
        factory.write_json(directory/"factory_proposal.json", proposal)
        factory.write_json(directory/"challenge.json", challenge)
        factory.write_json(directory/"factory_baseline.json", baseline)
        coordinator.validated = True
        ledger.add("coordinator", "governance", "validated_factory_case", {"factory_proposal_sha256": sim.digest(proposal),
            "factory_contract_version": FACTORY_VERSION, "challenge_sha256": sim.digest(challenge),
            "scenario": challenge["scenario"], "independent_baseline_passed": baseline["passed"],
            "delivery": "Validated Factory numbers compiled by the fixed coordinator mapping into a rehearsed challenge. Fresh local runtime delivers current state to governance. Factory narrative and baseline labels are not forwarded."})
        summary["status"] = "FACTORY_VALIDATED"
        save(directory, summary, ledger)
        report = service.run_agent(directory/"service", coordinator, challenge,
            lambda record, current: coordinator.consent(record, current, callback, open_review), review_refresh=True)
        summary["status"] = report["stage"]
        if report.get("error"):
            summary["error"] = report["error"]
        if report.get("approval_probe"):
            certificate = report["approval_probe"]
            summary["approval_revalidation"] = {"certificate_sha256":sim.digest(certificate),
                "metrics":certificate["metrics"], "rehearsal_passed":certificate["rehearsal"]["passed"],
                "scope":"Fresh probe after human review; original state and exact model proposal unchanged."}
        summary["outcome"] = {"stage": report["stage"], "recovery_status": report.get("recovery", {}).get("status"),
            "committed_units": report.get("committed_units"), "capacity_limit": report.get("capacity_limit"),
            "availability": report.get("availability"), "workflow_paused": "deploy-A" in report.get("state", {}).get("paused_runs", []),
            "harness_teardown": report.get("teardown"), "error": report.get("error")}
        summary["checks"]["service_checks_passed"] = report.get("checks_passed") is True
        summary["checks"]["all_workers_stopped_after_outcome"] = report.get("teardown", {}).get("all_workers_stopped") is True
        coordinator.verify()
        ledger.add("coordinator", "reviewer", "outcome_recorded", summary["outcome"])
    except (factory.GenerationError, factory.ProposalError, ValueError, OSError) as exc:
        summary.update(status="STOPPED_FOR_REVIEW", error=type(exc).__name__ + ": " + str(exc))
    except KeyboardInterrupt:
        summary.update(status="INTERRUPTED", error="Interrupted; inspect saved state and the separate harness teardown record.")
    finally:
        try:
            ledger.verify()
            ledger_valid = True
        except (OSError, ValueError):
            ledger_valid = False
        unchanged = implementation_digest() == coordinator.initial_digest
        summary["checks"].update(ledger_consistent=ledger_valid, implementation_unchanged=unchanged,
            role_order_and_ceiling=[c["role"] for c in summary["calls"]] == list(ROLES)[:len(summary["calls"])] and len(summary["calls"])<=3)
        if not ledger_valid or not unchanged:
            summary["status"] = "STOPPED_FOR_REVIEW"
        summary["full_three_role_flow_completed"] = (summary["status"] == "RECOVERED" and len(summary["decisions"])==3
            and all(summary["checks"].values()))
        save(directory, summary, ledger)
    return directory, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--scenario", choices=("routing", "dependency"), default="dependency")
    parser.add_argument("--max-output-tokens", type=int, default=10000)
    parser.add_argument("--thinking", choices=("on", "off"), default="on")
    parser.add_argument("--output", type=Path, default=ROOT/"agent_team_runs")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.max_output_tokens <= 10000:
        parser.error("Use 1024-10000 output tokens per request.")
    providers = None
    if args.live:
        if not terminal_guard.interactive_input():
            parser.error("Live team mode needs an interactive terminal for exact consent.")
        print("LIVE team: at most 3 paid requests, one per role, up to %s output tokens each. No retries." % args.max_output_tokens, flush=True)
        key = (os.environ.get("NEBIUS_API_KEY") or getpass("Paste the Nebius API key locally: ")).strip()
        if not key:
            parser.error("No API key supplied; no request made.")
        providers = {role:RoleClient(role, key, args.max_output_tokens, args.thinking=="on") for role in ROLES}
    directory, result = run_team(args.output, providers, args.scenario, open_review=args.open)
    print("Team: %s | role requests: %s/3 | model calls: %s" % (result["status"], result["role_requests"], result["model_calls"]))
    if result.get("error"):
        print("Reason: " + result["error"], file=sys.stderr)
    print("Report: " + str(directory/"index.html"))
    if args.open:
        webbrowser.open((directory/"index.html").as_uri())
    return 0 if result["full_three_role_flow_completed"] else 1


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.providers = {role:ScriptedRole(role, "dependency") for role in ROLES}

    def tearDown(self):
        self.temp.cleanup()

    def run_case(self, consent=None):
        return run_team(self.root, self.providers, consent=consent)

    def make_mock_live(self):
        for provider in self.providers.values():
            provider.live = True

    def test_complete_three_role_flow_preserves_shared_work_in_both_families(self):
        for scenario in ("routing", "dependency"):
            with self.subTest(scenario=scenario):
                path, result = run_team(self.root, scenario=scenario)
                report = json.loads((path/"service"/"report.json").read_text())
                self.assertTrue(result["full_three_role_flow_completed"])
                self.assertEqual((result["role_requests"], result["model_calls"]), (3,0))
                self.assertEqual(result["outcome"]["recovery_status"], "RECOVERY_REVIEW_REQUIRED")
                self.assertEqual(report["committed_units"], 40)
                self.assertTrue(result["outcome"]["workflow_paused"])
                self.assertTrue(report["actual_at_outcome"]["workers"]["candidate-v2"]["alive"])
                self.assertTrue(report["teardown"]["all_workers_stopped"])
                if scenario == "dependency":
                    self.assertIn({"id":"C-legitimate-job", "owner_run":"route-C"}, report["state"]["deployments"]["candidate-v2"]["dependencies"])
                else:
                    self.assertEqual(report["state"]["route_owner"], "route-C")

    def test_distinct_clients_receive_real_handoffs_and_no_scoring_labels(self):
        for role, provider in self.providers.items():
            provider.ask = mock.Mock(wraps=provider.ask)
        path, result = self.run_case()
        self.assertTrue(result["full_three_role_flow_completed"])
        for role, provider in self.providers.items():
            provider.ask.assert_called_once()
            context = provider.ask.call_args.args[0]
            self.assertNotIn("expected", context)
            self.assertNotIn("expected_task", context)
            self.assertEqual(json.loads((path/"roles"/role/"request.json").read_text())["context"], context)
        review = self.providers["governance"].ask.call_args.args[0]
        recovery = self.providers["recovery"].ask.call_args.args[0]
        self.assertEqual(review["current_state"]["capacity_limit"], 42)
        self.assertEqual(recovery["execution"]["status"], "SUCCEEDED")
        self.assertTrue(recovery["workflow_failure_recorded"])
        self.assertTrue(recovery["approved_recovery_scope"])
        self.assertIn("deploy-A", recovery["current_state"]["paused_runs"])

    def test_mock_live_budget_is_one_request_per_role(self):
        self.make_mock_live()
        for provider in self.providers.values():
            provider.ask = mock.Mock(wraps=provider.ask)
        _, result = self.run_case(lambda *_:True)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["usage"]["missing_usage_calls"], 3)
        self.assertTrue(result["full_three_role_flow_completed"])
        for provider in self.providers.values():
            provider.ask.assert_called_once()

    def test_three_sdk_clients_send_distinct_prompts_and_account_for_usage(self):
        secret, requests, clients = "team-private-fixture-key", [], []
        def construct(**settings):
            self.assertEqual(settings["max_retries"], 0)
            self.assertEqual(settings["base_url"], factory.inference.BASE_URL)
            client = mock.Mock()
            def create(**request):
                role = list(ROLES)[len(requests)]
                requests.append(request)
                self.assertEqual(request["messages"][0]["content"], PROMPTS[role])
                content, _ = ScriptedRole(role, "dependency").ask(json.loads(request["messages"][1]["content"]))
                return SimpleNamespace(model=factory.inference.MODEL, id="mock-"+role,
                    usage=SimpleNamespace(prompt_tokens=50,completion_tokens=70,total_tokens=120),
                    choices=[SimpleNamespace(finish_reason="stop",message=SimpleNamespace(content=content,refusal=None))])
            client.chat.completions.create.side_effect = create
            clients.append(client)
            return client
        with mock.patch.dict(sys.modules, {"openai":SimpleNamespace(OpenAI=construct)}):
            providers = {role:RoleClient(role,secret) for role in ROLES}
            path, result = run_team(self.root, providers, consent=lambda *_:True)
        self.assertEqual(len({id(c) for c in clients}), 3)
        self.assertEqual(len(requests), 3)
        self.assertEqual((result["model_calls"],result["usage"]["reported_tokens"],result["usage"]["missing_usage_calls"]), (3,360,0))
        self.assertTrue(result["full_three_role_flow_completed"])
        self.assertNotIn(secret, "".join(p.read_text(encoding="utf-8") for p in path.rglob("*.json")))

    def test_factory_invalid_authority_fields_never_start_workers(self):
        proposal = factory_fixture()
        proposal["approval"] = True
        self.providers["factory"].ask = mock.Mock(return_value=(sim.canonical(proposal), {"finish_reason":"stop"}))
        with mock.patch.object(service.local, "LocalRuntime") as runtime:
            path, result = self.run_case()
        runtime.assert_not_called()
        self.assertEqual(result["status"], "STOPPED_FOR_REVIEW")
        self.assertEqual(result["role_requests"], 1)
        self.assertFalse((path/"service").exists())

    def test_legacy_full_challenge_reply_is_rejected_without_workers(self):
        proposal = service.fixture_challenge("dependency")
        proposal["fault"] = "requests_in_flight"
        self.providers["factory"].ask = mock.Mock(return_value=(sim.canonical(proposal), {"finish_reason":"stop"}))
        with mock.patch.object(service.local, "LocalRuntime") as runtime:
            _, result = self.run_case()
        runtime.assert_not_called()
        self.assertEqual(result["status"], "STOPPED_FOR_REVIEW")

    def test_factory_api_error_is_one_paid_attempt_no_retry(self):
        self.make_mock_live()
        self.providers["factory"].ask = mock.Mock(side_effect=factory.GenerationError("AuthenticationError (HTTP 401)"))
        with mock.patch.object(service.local, "LocalRuntime") as runtime:
            _, result = self.run_case()
        runtime.assert_not_called()
        self.providers["factory"].ask.assert_called_once()
        self.assertEqual((result["role_requests"],result["model_calls"],result["usage"]["missing_usage_calls"]), (1,1,1))

    def test_refused_or_unfinished_factory_reply_has_no_substitute(self):
        for metadata in ({"finish_reason":"length"}, {"finish_reason":"stop","refused":True}):
            with self.subTest(metadata=metadata):
                self.providers["factory"].ask = mock.Mock(return_value=(sim.canonical(factory_fixture()), metadata))
                with mock.patch.object(service.local, "LocalRuntime") as runtime:
                    _, result = self.run_case()
                runtime.assert_not_called()
                self.assertEqual(result["role_requests"], 1)
                self.assertFalse(result["full_three_role_flow_completed"])

    def test_declined_consent_does_not_invoke_recovery(self):
        self.providers["recovery"].ask = mock.Mock(wraps=self.providers["recovery"].ask)
        path, result = self.run_case(lambda *_:False)
        self.providers["recovery"].ask.assert_not_called()
        self.assertEqual(result["status"], "AWAITING_APPROVAL_NO_EXECUTION")
        self.assertEqual(result["role_requests"], 2)
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertNotIn("approval", report)
        self.assertNotIn("execution", report)
        self.assertEqual(report["committed_units"], 40)

    def test_stale_governance_proposal_cannot_reach_consent_or_execution(self):
        original = self.providers["governance"].ask
        def stale(context):
            content, metadata = original(context)
            proposal = json.loads(content)
            proposal["based_on_state"] = "0"*64
            return sim.canonical(proposal), metadata
        self.providers["governance"].ask = stale
        consent = mock.Mock(return_value=True)
        path, result = self.run_case(consent)
        consent.assert_not_called()
        self.assertEqual(result["status"], "STALE_PROPOSAL")
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertNotIn("approval", report)
        self.assertNotIn("execution", report)

    def test_governance_deferral_finishes_safely_without_recovery(self):
        def defer(context):
            return sim.canonical({"schema_version":revision.PROPOSAL_VERSION,"decision":"defer","additional_units":0,
                "based_on_state":context["based_on_state"],"reason":"Defer within the registered contract."}), {"finish_reason":"stop"}
        self.providers["governance"].ask = defer
        consent = mock.Mock(return_value=True)
        _, result = self.run_case(consent)
        consent.assert_not_called()
        self.assertEqual(result["status"], "DEFERRED")
        self.assertEqual(result["role_requests"], 2)
        self.assertFalse(result["full_three_role_flow_completed"])

    def test_recovery_defer_retains_capacity_and_durable_pause(self):
        def defer(context):
            return sim.canonical({"schema_version":service.RECOVERY_VERSION,"decision":"defer",
                "based_on_state":context["based_on_state"],"reason":"Retain for review."}), {"finish_reason":"stop"}
        self.providers["recovery"].ask = defer
        path, result = self.run_case()
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertEqual(result["status"], "RECOVERY_DEFERRED")
        self.assertEqual(report["committed_units"], 42)
        self.assertIn("deploy-A", report["state"]["paused_runs"])
        self.assertNotIn("recovery", report)
        self.assertTrue(report["teardown"]["all_workers_stopped"])

    def test_recovery_api_failure_retains_recorded_execution_and_tears_down_workers(self):
        self.make_mock_live()
        self.providers["recovery"].ask = mock.Mock(side_effect=factory.GenerationError("Timeout"))
        path, result = self.run_case(lambda *_:True)
        self.providers["recovery"].ask.assert_called_once()
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertEqual((result["model_calls"],result["status"]), (3,"STOPPED_FOR_REVIEW"))
        self.assertEqual(report["execution"]["status"], "SUCCEEDED")
        self.assertIn("deploy-A", report["state"]["paused_runs"])
        self.assertTrue(report["teardown"]["all_workers_stopped"])

    def test_recovery_cannot_supply_arbitrary_tool_arguments(self):
        original = self.providers["recovery"].ask
        def forged(context):
            content, metadata = original(context)
            proposal = json.loads(content)
            proposal["tool"] = "stop_candidate"
            return sim.canonical(proposal), metadata
        self.providers["recovery"].ask = forged
        path, result = self.run_case()
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertEqual(result["status"], "STOPPED_FOR_REVIEW")
        self.assertNotIn("recovery", report)
        self.assertEqual(report["committed_units"], 42)
        self.assertIn("deploy-A", report["state"]["paused_runs"])
        self.assertTrue(report["actual_at_outcome"]["workers"]["candidate-v2"]["alive"])

    def test_interrupted_recovery_leaves_pause_and_separate_teardown_evidence(self):
        self.providers["recovery"].ask = mock.Mock(side_effect=KeyboardInterrupt)
        path, result = self.run_case()
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertEqual(result["status"], "INTERRUPTED")
        self.assertEqual(result["calls"][-1]["status"], "INTERRUPTED")
        self.assertIn("deploy-A", report["state"]["paused_runs"])
        self.assertEqual(report["committed_units"], 42)
        self.assertTrue(report["teardown"]["all_workers_stopped"])

    def test_handoff_file_tampering_is_detected_before_governance(self):
        original = self.providers["factory"].ask
        def tamper(context):
            path = next(self.root.glob("*/coordination.json"))
            path.write_text("[]", encoding="utf-8")
            return original(context)
        self.providers["factory"].ask = tamper
        with mock.patch.object(service.local, "LocalRuntime") as runtime:
            _, result = self.run_case()
        runtime.assert_not_called()
        self.assertFalse(result["checks"]["ledger_consistent"])
        self.assertEqual(result["status"], "STOPPED_FOR_REVIEW")

    def test_source_drift_during_consent_blocks_approval(self):
        digest = implementation_digest()
        changed = [False]
        def consent(*_):
            changed[0] = True
            return True
        with mock.patch(__name__+".implementation_digest", side_effect=lambda:"changed" if changed[0] else digest):
            path, result = self.run_case(consent)
        report = json.loads((path/"service"/"report.json").read_text())
        self.assertEqual(result["status"], "STOPPED_FOR_REVIEW")
        self.assertFalse(result["checks"]["implementation_unchanged"])
        self.assertNotIn("approval", report)
        self.assertNotIn("execution", report)

    def test_ledger_hashes_detect_changed_or_reordered_events(self):
        ledger = Ledger(self.root/"ledger.json")
        ledger.add("factory", "coordinator", "proposal", {"units":2})
        ledger.add("coordinator", "governance", "validated", {"passed":True})
        self.assertTrue(Ledger.valid(ledger.events))
        altered = copy.deepcopy(ledger.events)
        altered[0]["payload"]["units"] = 200
        self.assertFalse(Ledger.valid(altered))
        self.assertFalse(Ledger.valid(list(reversed(ledger.events))))

    def test_mixed_live_and_scripted_roles_are_rejected_before_effects(self):
        self.providers["factory"].live = True
        with mock.patch.object(service.local, "LocalRuntime") as runtime, self.assertRaises(ValueError):
            self.run_case()
        runtime.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_noninteractive_live_cli_stops_before_sdk_credentials_and_workers(self):
        for stdin in (subprocess.DEVNULL, subprocess.PIPE):
            with self.subTest(stdin=stdin):
                out = self.root/("not-created-"+str(stdin))
                env = dict(os.environ, NEBIUS_API_KEY="private-test-key-never-print")
                input_args = {"input":""} if stdin == subprocess.PIPE else {"stdin":stdin}
                result = subprocess.run([sys.executable,"-B","-S",str(Path(__file__).resolve()),"--live","--output",str(out)],
                    **input_args, capture_output=True, text=True, env=env, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("interactive terminal", result.stderr)
                self.assertNotIn(env["NEBIUS_API_KEY"], result.stdout+result.stderr)
                self.assertFalse(out.exists())

    def test_factory_boundary_combinations_all_produce_rehearsable_conflicts(self):
        for values in itertools.product(*FACTORY_BOUNDS.values()):
            proposal = {**factory_fixture(), **dict(zip(FACTORY_BOUNDS,values))}
            before = copy.deepcopy(proposal)
            for scenario in ("routing","dependency"):
                with self.subTest(values=values,scenario=scenario):
                    challenge, baseline = compile_factory_proposal(proposal,scenario)
                    p = challenge["parameters"]
                    base = p["stable_units"]+p["candidate_units"]
                    self.assertTrue(baseline["passed"])
                    self.assertGreater(base+p["extra_units"],p["new_limit"])
                    self.assertLessEqual(base+p["extra_units"],p["initial_limit"])
                    self.assertGreater(p["new_limit"],base)
                    self.assertEqual(proposal,before)

    def test_factory_invalid_numeric_types_and_ranges_are_not_coerced(self):
        for key,(low,high) in FACTORY_BOUNDS.items():
            for invalid in (True,False,None,str(low),float(low),low-1,high+1):
                with self.subTest(key=key,value=invalid):
                    proposal = {**factory_fixture(),key:invalid}
                    with self.assertRaisesRegex(factory.ProposalError,key):
                        compile_factory_proposal(proposal,"dependency")

    def test_factory_missing_extra_and_wrong_contract_fields_have_specific_errors(self):
        for key in FACTORY_FIELDS:
            proposal = factory_fixture()
            del proposal[key]
            with self.subTest(missing=key), self.assertRaisesRegex(factory.ProposalError,key):
                compile_factory_proposal(proposal,"dependency")
        for field in ("approval","parameters","scenario","fault","order"):
            with self.subTest(extra=field), self.assertRaisesRegex(factory.ProposalError,field):
                compile_factory_proposal({**factory_fixture(),field:"untrusted"},"dependency")
        with self.assertRaisesRegex(factory.ProposalError,"schema_version"):
            compile_factory_proposal({**factory_fixture(),"schema_version":factory.PROPOSAL_VERSION},"dependency")

    def test_generated_low_and_high_capacity_cases_complete_with_real_workers(self):
        for stable,candidate,headroom,extra in ((10,5,1,4),(80,40,3,20)):
            with self.subTest(stable=stable):
                proposal = {**factory_fixture(),"stable_units":stable,"candidate_units":candidate,
                    "headroom_units":headroom,"extra_units":extra}
                self.providers["factory"].ask = mock.Mock(return_value=(sim.canonical(proposal),{"finish_reason":"stop"}))
                path, result = self.run_case()
                report = json.loads((path/"service"/"report.json").read_text())
                self.assertTrue(result["full_three_role_flow_completed"])
                self.assertEqual(report["proposal"]["additional_units"],headroom)
                self.assertEqual(report["committed_units"],stable+candidate)
                self.assertTrue(report["teardown"]["all_workers_stopped"])
                self.assertEqual(report["recovery"]["status"],"RECOVERY_REVIEW_REQUIRED")

    def test_factory_raw_proposal_and_compiled_challenge_have_separate_provenance(self):
        path, result = self.run_case()
        raw = json.loads((path/"factory_proposal.json").read_text())
        challenge = json.loads((path/"challenge.json").read_text())
        handoff = next(e["payload"] for e in result["handoffs"] if e["kind"]=="validated_factory_case")
        self.assertEqual(raw["schema_version"],FACTORY_VERSION)
        self.assertEqual(challenge["schema_version"],factory.PROPOSAL_VERSION)
        self.assertEqual(handoff["factory_proposal_sha256"],sim.digest(raw))
        self.assertEqual(handoff["challenge_sha256"],sim.digest(challenge))
        self.assertEqual(result["decisions"]["factory"],raw)
        self.assertNotIn("order",raw)

    def test_cli_prints_factory_rejection_reason_without_another_request(self):
        self.providers["factory"].ask = mock.Mock(return_value=(sim.canonical({**factory_fixture(),"headroom_units":0}),{"finish_reason":"stop"}))
        directory, result = self.run_case()
        stderr = io.StringIO()
        with mock.patch.object(sys,"argv",["agent_team.py"]), mock.patch(__name__+".run_team",return_value=(directory,result)) as run, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(),1)
        run.assert_called_once()
        self.providers["factory"].ask.assert_called_once()
        self.assertIn("Reason: ProposalError: Factory headroom_units",stderr.getvalue())

    def test_recovery_progress_no_longer_displays_waiting_for_manager_consent(self):
        original = self.providers["recovery"].ask
        def observed(context):
            path = next(self.root.glob("*/summary.json"))
            current = json.loads(path.read_text())
            self.assertEqual(current["status"],"RECOVERY_REQUESTED")
            consent = next(e for e in current["handoffs"] if e["kind"]=="exact_consent_recorded")
            self.assertTrue(consent["payload"]["approved"])
            return original(context)
        self.providers["recovery"].ask = mock.Mock(side_effect=observed)
        _, result = self.run_case()
        self.assertTrue(result["full_three_role_flow_completed"])
        self.providers["recovery"].ask.assert_called_once()

    def test_compiled_profile_gate_still_names_and_rejects_incompatible_fields(self):
        challenge,_ = compile_factory_proposal(factory_fixture(),"dependency")
        for field,bad in (("scenario","routing"),("fault","stop_timeout"),("fault_after",4)):
            with self.subTest(field=field), self.assertRaisesRegex(factory.ProposalError,field):
                validate_team_challenge({**challenge,field:bad},"dependency")
        changed = copy.deepcopy(challenge)
        changed["parameters"]["new_limit"] = 40
        with self.assertRaisesRegex(factory.ProposalError,"parameters.new_limit"):
            validate_team_challenge(changed,"dependency")

    def test_slow_model_and_human_review_get_a_fresh_probe_without_new_calls(self):
        offset = [0]
        real_time = service.time.time
        original = self.providers["governance"].ask
        def slow(context):
            result = original(context)
            offset[0] += 60
            return result
        def consent(*_):
            offset[0] += 120
            return True
        self.providers["governance"].ask = slow
        self.make_mock_live()
        with mock.patch.object(service.time,"time",side_effect=lambda:real_time()+offset[0]):
            path,result = self.run_case(consent)
        report = json.loads((path/"service/report.json").read_text())
        self.assertTrue(result["full_three_role_flow_completed"])
        self.assertEqual((result["role_requests"],result["model_calls"]),(3,3))
        certificate = report["approval_probe"]
        old = report["rehearsal"]["before"]["deployments"]["candidate-v2"]["metrics"]["observed_unix"]
        self.assertGreater(certificate["started_unix"]-old,170)
        self.assertEqual(certificate["base_state_digest"],report["proposal"]["based_on_state"])
        self.assertEqual(report["state"]["deployments"]["candidate-v2"]["metrics"]["observed_unix"],old)
        self.assertEqual(len(certificate["samples"]),100)
        self.assertTrue(certificate["rehearsal"]["passed"])
        events = report["events"]
        probe = next(e["sequence"] for e in events if e["kind"]=="SERVICE_AGENT_APPROVAL_PROBE_VALIDATED")
        grant = next(e["sequence"] for e in events if e["kind"]=="REVISION_APPROVED")
        execution = next(e["sequence"] for e in events if e.get("action_key","").startswith("execute-rev-"))
        self.assertLess(probe,grant)
        self.assertLess(grant,execution)

    def test_failed_current_probe_stops_after_two_roles_without_a_grant(self):
        original = service.LocalRevisionEngine.refresh_review
        def rejected(engine, revision_id):
            infer = engine.client.infer
            def wrong(target):
                sample = infer(target)
                sample["body"]["result"] = 0
                return sample
            with mock.patch.object(engine.client,"infer",side_effect=wrong):
                return original(engine,revision_id)
        self.make_mock_live()
        with mock.patch.object(service.LocalRevisionEngine,"refresh_review",rejected):
            path,result = self.run_case(lambda *_:True)
        report = json.loads((path/"service/report.json").read_text())
        self.assertEqual((result["status"],result["model_calls"]),("STOPPED_FOR_REVIEW",2))
        self.assertIn("FRESH_OBSERVATIONS_REJECTED",result["error"])
        self.assertNotIn("approval",report)
        self.assertNotIn("execution",report)
        self.assertEqual(report["committed_units"],40)
        self.assertTrue(report["teardown"]["all_workers_stopped"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (factory.GenerationError, ValueError, OSError) as exc:
        print("TEAM STOPPED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Team interrupted; saved evidence retained.", file=sys.stderr)
        raise SystemExit(130)
