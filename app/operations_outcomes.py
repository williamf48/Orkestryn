"""Read-only outcome reports from saved Operations evidence; no runtime/model calls."""
from __future__ import annotations

import html
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

import operations_workspace as ops
import simulator as sim
import team_workspace as ws

VERSION = "agent-rehearsal-outcomes-v1"


def _checked_digest(value, field="digest", mutable=()):
    if value.get(field) != sim.digest({k: v for k, v in value.items() if k not in (field, *mutable)}):
        raise ValueError("An outcome evidence digest does not match its recorded contents.")


def _seconds(start, end):
    """Wall-clock evidence interval, including approvals; unknown on clock anomalies."""
    if not start or not end:
        return None
    try:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if a.tzinfo is None or b.tzinfo is None:
            return None
        elapsed = (b - a).total_seconds()
        return round(elapsed, 3) if math.isfinite(elapsed) and elapsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _principal_mode(principal):
    if not principal:
        return "NOT_RECORDED"
    return "SCRIPTED" if principal.get("subject", "").startswith("fixture:") else "NAMED_LOCAL_ACCOUNT"


def from_summary(s):
    """Summarize an already validated ops.read snapshot. Never reread mid-report."""
    core = s.get("service") or {}
    snapshot = core.get("policy_snapshot")
    state = snapshot["state"] if snapshot else None
    operations = s["operations"]
    plans, intents, windows = core.get("plans", []), core.get("intents", []), s["health"]
    for plan in plans:
        _checked_digest(plan, "plan_digest", ("status", "cursor", "approval"))
    for window in windows:
        _checked_digest(window)
    emergency = operations.get("emergency") or {}
    if emergency:
        _checked_digest(emergency["plan"])
        if any(g["plan_digest"] != emergency["plan"]["digest"] for g in emergency["grants"].values()):
            raise ValueError("A handoff grant does not match the reviewed plan.")
        if emergency["phase"] == "CONFIRMED" and set(emergency["grants"]) != {"manager", "owner_C"}:
            raise ValueError("Confirmed handoff lacks both recorded role grants.")
    for job in operations["jobs"]:
        _checked_digest(job["plan"])

    metrics = []

    def add(key, label, value, unit, basis, references):
        metrics.append(dict(key=key, label=label, value=value, unit=unit, basis=basis, references=references))

    latest = plans[-1] if plans else None
    completed = [r for r in intents if r.get("status") == "COMPLETED" and (r.get("result") or {}).get("status") == "SUCCEEDED"]
    waiting = latest["choice"]["waiting"] if latest else None
    add("submitted", "Requests submitted", len(intents) if snapshot else None, "requests",
        "Unique requests in the saved mission queue.", ["service.intents"])
    add("completed", "Requests completed", len(completed) if snapshot else None, "requests",
        "Completed queue entries with a successful recorded result.", ["service.intents[].result"])
    add("waiting", "Requests waiting in latest plan", len(waiting) if waiting is not None else None, "requests",
        "Waiting is a policy/planning outcome, not a count of incidents prevented.", ["service.plans[-1].choice.waiting"])

    before = (s.get("recovery_grant") or {}).get("protected")
    protected = {(d["id"], d["owner_run"]) for d in before["dependencies"]} if before else None
    retained = {(d["id"], d["owner_run"]) for d in state["deployments"]["candidate-v2"]["dependencies"]} if state else set()
    released = set()
    release_records = []
    for job in operations["jobs"]:
        plan = job["plan"]
        confirmed = (job["phase"] == "FINISHED" and "owner_C" in job["principal"].get("roles", [])
                     and any(e.get("kind") == "OPERATIONS_C_JOB_RELEASE" and e.get("plan_digest") == plan["digest"]
                             and e.get("dependency_id") == plan["dependency_id"] and e.get("run_id") == "route-C"
                             for e in (snapshot or {}).get("events", [])))
        if confirmed:
            released.add((plan["dependency_id"], "route-C"))
        release_records.append({"dependency_id": plan["dependency_id"], "phase": job["phase"],
                                "confirmed_owner_release": confirmed, "approval_mode": _principal_mode(job["principal"])})
    missing = protected - retained - released if protected is not None else None
    add("dependencies_accounted", "Original customer dependencies accounted for", len(protected) - len(missing) if protected is not None else None,
        "dependencies", "Present in the final snapshot or removed with a completed exact owner release. These are local dependency records.",
        ["recovery_grant.protected.dependencies", "service.policy_snapshot", "operations.jobs"])
    add("dependencies_unconfirmed", "Dependencies needing reconciliation", len(missing) if missing is not None else None,
        "dependencies", "An absent dependency without a completed owner release remains unconfirmed.", ["service.policy_snapshot", "operations.jobs"])
    add("owner_releases", "Confirmed owner releases", len(released), "dependencies",
        "Matched completed release records and policy journal events; not automatically deleted customer work.", ["operations.jobs", "service.policy_snapshot.events"])

    handoffs = [e for e in s["events"] if e["kind"] == "EMERGENCY_HANDOFF_CONFIRMED"]
    handoff = handoffs[-1] if handoffs and emergency.get("phase") == "CONFIRMED" else None
    if handoff and (handoff["data"]["plan_digest"] != emergency["plan"]["digest"] or handoff["data"]["availability"] != emergency.get("availability")):
        raise ValueError("Handoff observation disagrees with its mission event.")
    incident = s.get("incident")
    interval = _seconds(incident.get("created_at") if incident else None, handoff["at"] if handoff else None)
    add("handoff_seconds", "Incident to confirmed handoff", interval, "seconds",
        "One wall-clock interval, including human/scripted approval time and post-handoff probes. Not MTTR or time saved.",
        ["incident.created_at", "events.EMERGENCY_HANDOFF_CONFIRMED.at"])
    availability = emergency["availability"] if handoff else None
    if availability and (type(availability["requests"]) is not int or type(availability["successful"]) is not int
                         or not 0 <= availability["successful"] <= availability["requests"]):
        raise ValueError("Invalid handoff sample counts.")
    add("handoff_successful", "Successful post-handoff probes", availability["successful"] if availability else None,
        "requests", (str(availability["requests"]) + " recorded local HTTP requests; not a production availability SLA.") if availability else "No confirmed handoff sample yet.",
        ["operations.emergency.availability"])

    probes = [p for w in windows for p in w["samples"]["router"]]
    successful = [p for p in probes if p.get("ok") is True and p.get("body", {}).get("result") == 49]
    candidate_probes = [p for w in windows for p in w["samples"]["candidate"]]
    candidate_failed = [p for p in candidate_probes if not (p.get("ok") is True and p.get("body", {}).get("result") == 49)]
    add("candidate_failed_probes", "Failed direct candidate probes", len(candidate_failed), "requests",
        "Direct candidate checks reveal faults that the finite router sample may not encounter.", ["health[].samples.candidate"])
    add("router_probes", "Recorded router health probes", len(probes), "requests",
        "Arithmetic HTTP probes from saved health windows only; provider inference is separate.", ["health[].samples.router"])
    add("router_successful", "Successful router health probes", len(successful), "requests",
        "Requires both HTTP success and the expected arithmetic result.", ["health[].samples.router"])
    latencies = [p.get("latency_ms") for p in probes]
    latency_complete = bool(latencies) and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in latencies)
    p95 = sorted(latencies)[math.ceil(.95 * len(latencies)) - 1] if latency_complete else None
    add("router_p95_ms", "Router probe p95 latency", p95, "ms",
        "Nearest-rank p95 across all recorded router probes, including failures; unknown if any latency is absent.", ["health[].samples.router[].latency_ms"])

    live = [c for c in s["calls"] if c["live"]]
    tokens = []
    for call in live:
        value = (call.get("metadata") or {}).get("total_tokens")
        if type(value) is int and value >= 0:
            tokens.append(value)
    missing_usage = len(live) - len(tokens)
    add("live_role_attempts", "Live proposal role attempts", len(live), "attempts",
        "Persisted attempts, including failures; excludes separate inference workload tasks.", ["calls[].live"])
    add("role_tokens", "Reported proposal tokens", sum(tokens) if not missing_usage else None, "tokens",
        "Provider-reported usage only. " + (str(missing_usage) + " live attempts lack usage; total is unknown." if missing_usage else "All live role attempts have usage, or no live roles ran."),
        ["calls[].metadata.total_tokens"])

    fixture_modes = sorted({_principal_mode(operations.get("batch_principal")), _principal_mode(operations.get("recovery_principal"))})
    faults = [e["data"].get("fault") for e in s["events"] if e["kind"] == "EXPLICIT_DEVELOPMENT_FAULT"]
    report = {
        "version": VERSION, "run": s["mission_id"], "phase": s["phase"], "lifecycle": s["lifecycle"],
        "snapshot_at": s["events"][-1]["at"], "platform": s["platform"],
        "proposal_mode": "LIVE_PROVIDER" if s["live"] else "SCRIPTED_FIXTURE",
        "evidence": {"summary_digest": s["evidence_digest"], "event_count": len(s["events"]),
                     "last_event_digest": s["events"][-1]["digest"], "operations_source": operations["source"],
                     "historical_source": s["historical_operations"], "scope": "Local digest and journal consistency, not external attestation or current service liveness."},
        "metrics": metrics,
        "waiting_reasons": dict(sorted(Counter(w["code"] for w in waiting or []).items())),
        "dependency_accounting": {"at_recovery_start": len(protected) if protected is not None else None,
                                  "retained_from_start": len(protected & retained) if protected is not None else None,
                                  "released_from_start": len(protected & released) if protected is not None else None,
                                  "unconfirmed": len(missing) if missing is not None else None, "releases": release_records},
        "recovery_status": (s.get("recovery") or {}).get("status", "NOT_RECORDED"),
        "role_usage": {"known_tokens_lower_bound": sum(tokens), "attempts_missing_usage": missing_usage, "scope": "Proposal roles only; task gateway costs are not included."},
        "comparison_basis": {"workflow_digest": operations["workflow_digest"],
                             "compiled_requests_digest": sim.digest(operations.get("compiled_requests")),
                             "settings_digest": sim.digest(s["settings"]), "platform": s["platform"],
                             "lease_seconds": s["ttl"],
                             "proposal_mode": s["live"], "faults": faults, "approval_modes": fixture_modes,
                             "handoff_approval_modes": {role: _principal_mode(g["principal"]) for role, g in emergency.get("grants", {}).items()},
                             "distinct_handoff_accounts": len({g["principal"]["subject"] for g in emergency.get("grants", {}).values()})},
        "limitations": ["Saved local evidence; stopped runs do not describe a currently running service.",
                        "Capacity and customer dependencies are local fixtures; no real GPU quota or customer revenue is measured.",
                        "No measured money saved, incidents avoided, customer adoption or production readiness is claimed.",
                        "Live proposal attempts and scripted decisions are distinct. Provider workload tasks require their own evidence.",
                        "Missing measurements stay unknown. Local hashes detect inconsistent records, not malicious full-history rewriting."],
        "effects": False, "model_calls_to_build_report": 0,
    }
    grants = emergency.get('grants', {})
    if set(grants) == {'manager', 'owner_C'} and report['comparison_basis']['distinct_handoff_accounts'] == 1:
        report['limitations'].append('Single-account evaluation: both handoff roles were approved by the same local account. This does not demonstrate approval by two different people.')
    return report


def compare(current, baseline):
    reasons = []
    if current["run"] == baseline["run"]:
        reasons.append("Select two different runs.")
    if current["lifecycle"] != "STOPPED" or baseline["lifecycle"] != "STOPPED":
        reasons.append("Both runs must be stopped before comparison.")
    for key, value in current["comparison_basis"].items():
        if value != baseline["comparison_basis"].get(key):
            reasons.append("Different " + key.replace("_", " ") + ".")
    if not current["comparison_basis"]["faults"] or not baseline["comparison_basis"]["faults"]:
        reasons.append("Both runs need a recorded development fault for this comparison.")
    previous = {m["key"]: m for m in baseline["metrics"]}
    rows = []
    if not reasons:
        for m in current["metrics"]:
            old, new = previous[m["key"]]["value"], m["value"]
            delta = round(new - old, 3) if type(old) in (int, float) and type(new) in (int, float) else None
            rows.append({"key": m["key"], "label": m["label"], "baseline": old, "current": new, "delta": delta, "unit": m["unit"]})
    return {"status": "DESCRIPTIVE_COMPARISON" if not reasons else "NOT_COMPARABLE", "baseline_run": baseline["run"],
            "baseline_summary_digest": baseline["evidence"]["summary_digest"], "reasons": reasons, "metrics": rows,
            "scope": "Single-run differences, current minus baseline. No causal improvement, statistical significance or money saved is inferred."}


def build(directory, baseline=None):
    # ops.read validates the public digest, event chain, final mission state and policy journal.
    report = from_summary(ops.read(Path(directory)))
    if baseline is not None:
        report["comparison"] = compare(report, from_summary(ops.read(Path(baseline))))
    report["report_digest"] = sim.digest(report)
    return report


def render(report):
    """Portable HTML: no JavaScript, external assets, credentials, prompts or ticket text."""
    esc = lambda v: html.escape("Unknown" if v is None else str(v), quote=True)
    rows = "".join("<tr><td><b>" + esc(m["label"]) + "</b><small>" + esc(m["basis"]) + "</small></td><td class='number'>" + esc(m["value"]) + "</td><td>" + esc(m["unit"]) + "</td></tr>" for m in report["metrics"])
    comparison = report.get("comparison")
    comparison_html = ""
    if comparison:
        comparison_html = "<section><h2>Comparison with " + esc(comparison["baseline_run"]) + "</h2><p>" + esc(comparison["scope"]) + "</p>"
        if comparison["reasons"]:
            comparison_html += "<p><strong>Not comparable</strong></p><ul>" + "".join("<li>" + esc(r) + "</li>" for r in comparison["reasons"]) + "</ul>"
        else:
            comparison_html += "<div class='scroll'><table><thead><tr><th>Metric</th><th>Baseline</th><th>Current</th><th>Difference</th><th>Unit</th></tr></thead><tbody>" + "".join("<tr>" + "".join("<td>" + esc(m[k]) + "</td>" for k in ("label", "baseline", "current", "delta", "unit")) + "</tr>" for m in comparison["metrics"]) + "</tbody></table></div>"
        comparison_html += "</section>"
    accounting = report["dependency_accounting"]
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Orkestryn outcome report</title><style>
    :root{font:16px/1.6 'Segoe UI',Arial,sans-serif;color:#172b3a;background:#f2f5f6}body{margin:0}main{max-width:1040px;margin:0 auto;padding:40px 24px}header,section{background:#fff;padding:28px;border:1px solid #d9e2e7;border-radius:12px;margin-bottom:20px}h1{font-size:36px;line-height:1.2;margin:12px 0}h2{font-size:23px}p,small{color:#4c6475}.tag{display:inline-block;padding:4px 12px;background:#e2f0ec;color:#135944;border-radius:20px;font-size:13px}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #d9e2e7;padding:12px;vertical-align:top}small{display:block;font-size:13px}.number{font-weight:700;font-size:22px;white-space:nowrap}.scroll{overflow:auto}code{overflow-wrap:anywhere;font-size:12px}.note{border-left:5px solid #bc7942}.columns{display:flex;flex-wrap:wrap;gap:24px}.columns p{flex:1;min-width:160px}.columns b{display:block;color:#172b3a;font-size:25px}@media(max-width:600px){main{padding:16px 10px}header,section{padding:18px}h1{font-size:28px}th,td{padding:8px}}@media print{body{background:#fff}main{max-width:none;padding:0}section{break-inside:auto}header{break-inside:avoid}tr{break-inside:avoid}}
    </style><main>""" + (
        "<header><p>Orkestryn · Outcome report</p><span class='tag'>" + esc(report["proposal_mode"].replace("_", " ")) + "</span><h1>What this rehearsal demonstrated</h1><p>Run " + esc(report["run"]) + " · " + esc(report["platform"]) + "</p><p>Saved " + esc(report["snapshot_at"]) + " · " + esc(report["phase"]) + " · " + esc(report["lifecycle"]) + "</p></header>"
        + ("<section class='note'>Historical source: this evidence belongs to an earlier build. It remains a record of that build.</section>" if report["evidence"]["historical_source"] else "")
        + "<section><h2>Customer dependency accounting</h2><div class='columns'><p>At recovery start<b>" + esc(accounting["at_recovery_start"]) + "</b></p><p>Retained<b>" + esc(accounting["retained_from_start"]) + "</b></p><p>Released with owner approval<b>" + esc(accounting["released_from_start"]) + "</b></p><p>Needs reconciliation<b>" + esc(accounting["unconfirmed"]) + "</b></p></div><p>These counts describe registered local dependencies. Recovery status: " + esc(report["recovery_status"]) + ".</p></section>"
        + "<section><h2>Recorded outcomes</h2><div class='scroll'><table><thead><tr><th>Measurement and meaning</th><th>Value</th><th>Unit</th></tr></thead><tbody>" + rows + "</tbody></table></div></section>"
        + comparison_html + "<section class='note'><h2>Evidence boundaries</h2><ul>" + "".join("<li>" + esc(x) + "</li>" for x in report["limitations"]) + "</ul></section>"
        + "<section><h2>Reproducible evidence</h2><p>Summary digest<br><code>" + esc(report["evidence"]["summary_digest"]) + "</code></p><p>Report digest<br><code>" + esc(report["report_digest"]) + "</code></p><p>Metric references in the companion JSON identify the source fields. Report generation made zero model calls and changed no workflow state.</p></section></main></html>"
    )


def export(directory, report):
    directory = Path(directory)
    for name in ("outcome_report.json", "outcome_report.html"):
        target = directory / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("Outcome reports must be regular files.")
    ws.write_json(directory / "outcome_report.json", report)
    # The HTML is a disposable rendering. The JSON and source evidence are authoritative.
    path = directory / "outcome_report.html"
    path.write_text(render(report), encoding="utf-8")
    return path
