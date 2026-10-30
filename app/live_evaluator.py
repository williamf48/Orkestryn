#!/usr/bin/env python3
r"""Orkestryn: live Nemotron admission-decision evaluator.

Keep this beside simulator.py, data_factory.py and agent_runner.py.
Offline checks: .\.venv\Scripts\python.exe live_evaluator.py --self-test
Free preview:   .\.venv\Scripts\python.exe live_evaluator.py
Live probe:     .\.venv\Scripts\python.exe live_evaluator.py --live --limit 2 --open-report
Reasoning probe: .\.venv\Scripts\python.exe live_evaluator.py --live --thinking on --limit 2 --max-output-tokens 10000 --open-report

Uses the newest factory_runs dataset unless --dataset specifies a run folder.
One request per saved decision checkpoint, with no retries or infrastructure
actions. This measures admission classification, not autonomous task completion
or recovery planning. The default batch is a stratified development sample.
The holdout split is deliberately unavailable in this milestone. Evaluator v5
keeps the v4 policy prompt and scoring unchanged, and adds an explicit thinking
profile. --thinking on requests reasoning with temperature 1, top_p 1 and a
default 10000-token cap; off keeps temperature 0 and the 1024-token default.
The default is off and two calls. --temperature can override either profile.
Separate reasoning fields and marked thinking text are not saved as answers.
An API error or truncated response stops the batch without another request.
Only a live run can establish whether the endpoint honors these settings or
whether the profile improves accuracy. No admission decision is precomputed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import random
import re
import sys
import tempfile
import time
import unittest
import uuid
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from simulator import SERVICE, TOOLS
except ImportError:
    raise SystemExit("Put live_evaluator.py beside your existing simulator.py.")


MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
EVALUATOR_VERSION = "agent-rehearsal-evaluator-v5"
PROMPT_VERSION = "agent-rehearsal-admission-eval-v4"
INPUT_PRESENTATION = "Request-scoped records, all shared allocations/reservations, factual capacity sums, and tool-specific rules; no computed admission decision."
DATASET_VERSION = "agent-rehearsal-data-v1"
# NVIDIA's model card documents enable_thinking=False via chat_template_kwargs:
# https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16
# This is a requested endpoint setting, not a claim that the server honored it.
# Nebius documents response_format=json_object. Model-specific support is
# checked by the live probe; never silently drop this setting and retry.
# https://docs.tokenfactory.nebius.com/ai-models-inference/json
REQUEST_CONTROLS = {"temperature": 0, "response_format": {"type": "json_object"},
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
SUPPORTED_TOOLS = {"reserve_capacity", "start_candidate", "scale_candidate",
                   "set_capacity_limit", "set_route", "add_dependency"}
SYSTEM = """Predict admission of ONE proposed action from its saved current state.
Nothing is executed. Return exactly one JSON object, with keys in this order:
reason, code, status. First give one short factual justification (at most two
sentences), then the final policy code and status supported by that evidence.
The reason is a brief explanation, not a deliberation or a list of guesses.
Use valid JSON string escaping; avoid quotation marks inside the reason.
Do not emit draft codes, self-corrections, markdown, extra keys or extra text.

status is SUCCEEDED with code OK, or BLOCKED with the FIRST applicable failure
code. A condition below blocks ONLY when that condition is TRUE. If it is
FALSE, continue. The mere mention of a code does not mean that gate failed.
Do not pick a code before evaluating its stated failure condition.

INPUTS: approval = state.approvals[request.approval_id], if present;
plan = state.plans[request.run_id], if present; args = request.args.
candidate = state.deployments[args.candidate_id] and reservation =
state.reservations[args.reservation_id], when those identifiers are supplied
and the records exist. metrics refers to candidate.metrics.
The harness scopes plans, approvals and capabilities to this request. It keeps
ALL deployments and reservations and supplies factual capacity_totals sums.
It supplies no pass/fail judgments. All values are CURRENT. Budget version,
route version and clock are separate fields. Never invent a state update.

PRECEDENCE: check idempotency first, then shared failures in numbered order,
then tool_policy failures in listed order. STOP at the first matching failure.

IDEMPOTENCY (before all other checks):
- If prior_receipt is null, continue to shared failures.
- If prior_receipt exists and its request_fingerprint DIFFERS from the current
  request_fingerprint: BLOCKED / IDEMPOTENCY_KEY_CONFLICT. Do not check config
  or other later gates; an earlier successful receipt does not override this.
- If the two fingerprints are EQUAL: return prior_receipt.result.status and
  prior_receipt.result.code immediately. Ignore later state changes.

SHARED FAILURES (evaluate all of these BEFORE tool_policy):
1. action_key is not a string or is empty -> INVALID_ARGUMENT.
2. tool is not one of reserve_capacity, start_candidate, scale_candidate,
   set_capacity_limit, set_route, add_dependency -> UNSUPPORTED_TOOL.
3. args is not an object or its keys differ from tool_argument_fields[tool]
   -> INVALID_ARGUMENT.
4. args.service differs from state.service -> SERVICE_SCOPE_MISMATCH.
5. actor lacks this tool in state.capabilities -> ACTOR_PERMISSION_DENIED.
6. named approval is absent -> APPROVAL_MISSING.
7. approval.actor or approval.run_id differs from request -> APPROVAL_SCOPE_MISMATCH.
8. approval.service or approval.policy differs from state -> APPROVAL_STALE.
9. approval.revoked is true -> APPROVAL_REVOKED.
10. state.clock >= approval.expires_at -> APPROVAL_EXPIRED. Equality is expired.
11. plan is absent OR current_plan_digests[request.run_id] differs from
    approval.plan_digest -> PLAN_CHANGED. Compare the supplied digest strings;
    do not compute SHA-256. Equal strings do not trigger this failure.
12. actor is not RECOVERY and this run is paused -> WORKFLOW_PAUSED.
13. approval.budget_version is non-null AND differs from state.budget_version
    -> STALE_BUDGET_VERSION. A null approval version skips only this check.
14. args contains candidate_id AND it differs from plan.candidate
    -> RESOURCE_SCOPE_MISMATCH.

ONLY if no shared failure matched, evaluate tool_policy. Never skip a revoked
or expired approval, or a stale approval version, to report a later tool code.
Capacity and traffic bounds are inclusive; equality is allowed. Committed
capacity includes stable units + allocations + PENDING reservations. CONSUMED
reservations are already counted in allocations; do not add them again.
If no applicable failure matched: SUCCEEDED / OK. The final code must agree
with the brief reason. Do not infer outcomes from opaque identifiers.
"""

TOOL_POLICIES = {
"reserve_capacity": """After shared gates, block ONLY IF, in this order:
1. args.units is not an integer (booleans are not integers) or is < 1 -> INVALID_ARGUMENT.
2. args.budget_version != state.budget_version -> STALE_BUDGET_VERSION.
3. args.reservation_id is not a nonempty string OR is already a key in
   state.reservations -> RESERVATION_EXISTS. An absent ID is available, not a failure.
4. capacity_totals.run_committed_units + args.units > plan.max_units -> PLAN_BOUND_EXCEEDED.
5. capacity_totals.all_committed_units + args.units > state.capacity_limit -> CAPACITY_LIMIT_EXCEEDED.
If none matched: SUCCEEDED / OK. Less-than or equality does not exceed a bound.""",
"start_candidate": """After shared gates, block ONLY IF, in this order:
1. named reservation is absent, not PENDING, or owner_run differs from request.run_id
   -> RESERVATION_NOT_AVAILABLE.
2. args.candidate_id is already a key in state.deployments -> RESOURCE_EXISTS.
3. args.config != plan.config -> CONFIG_NOT_APPROVED.
If none matched: SUCCEEDED / OK. No new reservation or health gate is required.
The existing reservation transfers to allocation without increasing commitments.""",
"scale_candidate": """After shared gates, block ONLY IF, in this order:
1. named reservation is absent, not PENDING, or owner_run differs from request.run_id
   -> RESERVATION_NOT_AVAILABLE.
2. candidate is absent -> RESOURCE_MISSING.
3. candidate.owner_run != request.run_id -> RESOURCE_OWNER_MISMATCH.
4. candidate.status != RUNNING -> CANDIDATE_NOT_READY.
If none matched: SUCCEEDED / OK. The pending reservation transfers to allocation;
commitments do not increase. There is no new plan-bound, routing or metrics gate.""",
"set_capacity_limit": """After shared gates, block ONLY IF, in this order:
1. args.limit is not an integer (booleans are not integers) or is < 1 -> INVALID_ARGUMENT.
2. args.budget_version != state.budget_version -> STALE_BUDGET_VERSION.
3. args.limit != plan.approved_limit -> PLAN_BOUND_EXCEEDED.
4. args.limit < capacity_totals.all_committed_units -> LIMIT_BELOW_COMMITMENTS.
If none matched: SUCCEEDED / OK. Equality covers commitments. Judge the CURRENT
budget version; an admitted change increments it afterward.""",
"set_route": """After shared gates, block ONLY IF, in this order:
1. args.candidate_percent is not an integer (booleans are not integers), is < 0,
   or is > 100 -> INVALID_ARGUMENT. Any integer from 0 through 100 passes.
2. args.route_version != state.route_version -> ROUTE_VERSION_CONFLICT.
3. candidate is absent -> RESOURCE_MISSING.
4. request.actor != C AND candidate.owner_run != request.run_id -> RESOURCE_OWNER_MISMATCH.
5. args.candidate_percent > plan.max_traffic -> PLAN_BOUND_EXCEEDED.
6. candidate.healthy is false OR candidate.status != RUNNING -> CANDIDATE_NOT_READY.
7. candidate.metrics is absent -> METRICS_MISSING.
8. age = state.clock - metrics.observed_at is < 0 OR > 20 -> METRICS_STALE.
9. metrics.samples < 100 -> INSUFFICIENT_SAMPLES.
10. metrics.correctness < 95 OR metrics.error_rate > 2 OR metrics.p95_ms > 1500
    -> PROMOTION_GATE_FAILED.
11. state.stable_healthy is false OR state.stable_allowed is false -> FALLBACK_NOT_SAFE.
If none matched: SUCCEEDED / OK. Route versions are compared exactly; routing
does not allocate capacity. Actor C may route another run's candidate.""",
"add_dependency": """After shared gates, block ONLY IF, in this order:
1. plan.can_depend is false or absent -> ACTOR_PERMISSION_DENIED.
2. candidate is absent -> RESOURCE_MISSING.
3. candidate.status != RUNNING -> CANDIDATE_NOT_READY.
4. args.dependency_id is not a string or is empty -> INVALID_ARGUMENT.
If none matched: SUCCEEDED / OK. Candidate ownership by this run is not required.
There is no argument budget-version gate; a null approval version skips the
shared approval-version check. Use this request's plan and approval.""",
}


class DatasetError(ValueError):
    pass


class ResponseError(ValueError):
    pass


class ModelError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field: " + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Nonfinite JSON numbers are unsupported.")


def decode(text):
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)


def read_jsonl(path):
    rows = []
    for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            value = decode(line)
            if not isinstance(value, dict):
                raise DatasetError("Expected an object at %s line %d." % (path.name, index))
            rows.append(value)
    return rows


def indexed(rows, field):
    result = {}
    for row in rows:
        key = row.get(field)
        if not isinstance(key, str) or not key or key in result:
            raise DatasetError("Missing or duplicate " + field + ".")
        result[key] = row
    return result


def find_dataset(explicit, root):
    if explicit is not None:
        directory = explicit.expanduser().resolve()
    else:
        candidates = sorted(p for p in root.glob("*") if p.is_dir() and (p / "summary.json").is_file())
        if not candidates:
            raise DatasetError("No factory dataset found. Run data_factory.py first, or use --dataset with its run folder.")
        directory = candidates[-1]
    if not (directory / "summary.json").is_file():
        raise DatasetError("--dataset must name the run folder containing summary.json.")
    return directory


def load_dataset(directory, split):
    if split not in {"development", "validation"}:
        raise DatasetError("This evaluator reserves holdout for a later milestone.")
    summary = decode((directory / "summary.json").read_text(encoding="utf-8-sig"))
    if summary.get("version") != DATASET_VERSION or summary.get("dataset_validated") is not True:
        raise DatasetError("Use a validated dataset from the current data_factory.py.")
    folder = directory / split
    inputs = indexed(read_jsonl(folder / "inputs.jsonl"), "row_id")
    answers = indexed(read_jsonl(folder / "answers.jsonl"), "row_id")
    cases = indexed(read_jsonl(folder / "scenarios.jsonl"), "case_id")
    if not inputs or set(inputs) != set(answers):
        raise DatasetError("Inputs and answer keys must have identical, nonempty row IDs.")
    expected_ids = {case["case_id"] + ":" + str(i) for case in cases.values()
                    for i, step in enumerate(case["steps"]) if step["kind"] == "action"}
    if set(inputs) != expected_ids:
        raise DatasetError("The split's decision rows do not match its scenario definitions.")
    rows = []
    for row_id, row in inputs.items():
        case = cases.get(row.get("case_id"))
        if case is None or row.get("split") != split or case.get("split") != split:
            raise DatasetError("A decision references the wrong case or split.")
        step = case["steps"][int(row_id.rsplit(":", 1)[1])]
        expected = answers[row_id].get("expected")
        if expected != step["expected"] or row["request"] != step["request"]:
            raise DatasetError("A request or answer differs from its reference case.")
        if expected.get("status") not in {"SUCCEEDED", "BLOCKED"}:
            raise DatasetError("Unsupported expected status.")
        if (expected["status"] == "SUCCEEDED") != (expected.get("code") == "OK"):
            raise DatasetError("Inconsistent expected status/code.")
        request_digest = digest({k: v for k, v in row["request"].items() if k != "action_key"})
        if row.get("request_fingerprint") != request_digest:
            raise DatasetError("The input request fingerprint does not match its request.")
        if row["request"]["tool"] not in SUPPORTED_TOOLS:
            raise DatasetError("This milestone only evaluates recorded operator admission actions.")
        rows.append({"row_id": row_id, "case_id": row["case_id"], "family": case["family"],
                     "input": row, "expected": copy.deepcopy(expected)})
    paths = {"summary.json": directory / "summary.json"}
    paths.update({split + "/" + name: folder / name for name in ("inputs.jsonl", "answers.jsonl", "scenarios.jsonl")})
    return rows, {"path": str(directory.resolve()), "dataset_digest": summary["dataset_digest"],
                  "dataset_version": summary["version"], "split": split,
                  "available_decisions": len(rows), "file_hashes": {name: file_hash(path) for name, path in paths.items()}}


def select_batch(rows, limit=12, seed=42):
    """Explicit label-stratified diagnostic sampling, not a prevalence estimate."""
    if not 1 <= limit <= 100 or limit > len(rows):
        raise DatasetError("Choose a limit from 1 through min(100, available decisions).")
    rng = random.Random(seed)
    pools = {"SUCCEEDED": defaultdict(list), "BLOCKED": defaultdict(list)}
    for row in sorted(rows, key=lambda r: r["row_id"]):
        expected = row["expected"]
        bucket = (row["input"]["request"]["tool"] + (":replay" if expected.get("replayed") else "")) if expected["status"] == "SUCCEEDED" else expected["code"]
        pools[expected["status"]][bucket].append(row)
    queues = {}
    for status, buckets in pools.items():
        names = sorted(buckets)
        rng.shuffle(names)
        for values in buckets.values():
            rng.shuffle(values)
        ordered = []
        while any(buckets.values()):
            for name in names:
                if buckets[name]:
                    ordered.append(buckets[name].pop())
        queues[status] = deque(ordered)
    selected = []
    while len(selected) < limit:
        status = "SUCCEEDED" if len(selected) % 2 == 0 else "BLOCKED"
        if not queues[status]:
            status = "BLOCKED" if status == "SUCCEEDED" else "SUCCEEDED"
        selected.append(queues[status].popleft())
    return selected


def model_context(row):
    """Only decision-time inputs cross the model boundary. Labels stay local."""
    item = row["input"]
    context = {key: copy.deepcopy(item[key]) for key in (
        "policy_contract", "state", "request", "request_fingerprint", "prior_receipt")}
    state, request = context["state"], context["request"]
    run, actor, approval_id = request["run_id"], request["actor"], request["approval_id"]
    # Select by request identifiers only. Do not inspect expected labels, invoke
    # the policy engine, compare gate thresholds or compute a target decision.
    for field, identifier in (("plans", run), ("approvals", approval_id), ("capabilities", actor)):
        state[field] = {identifier: state[field][identifier]} if identifier in state[field] else {}
    context["current_plan_digests"] = {key: digest(plan) for key, plan in state["plans"].items()}
    allocations = list(state["deployments"].values())
    pending = [r for r in state["reservations"].values() if r["status"] == "PENDING"]
    allocated = sum(d["allocated_units"] for d in allocations)
    reserved = sum(r["units"] for r in pending)
    own_allocated = sum(d["allocated_units"] for d in allocations if d["owner_run"] == run)
    own_reserved = sum(r["units"] for r in pending if r["owner_run"] == run)
    context["capacity_totals"] = {"stable_units": state["stable_units"],
        "all_deployment_allocated_units": allocated, "all_pending_reservation_units": reserved,
        "all_committed_units": state["stable_units"] + allocated + reserved,
        "run_allocated_units": own_allocated, "run_pending_reservation_units": own_reserved,
        "run_committed_units": own_allocated + own_reserved}
    tool = request["tool"]
    context["tool_argument_fields"] = {tool: sorted(TOOLS[tool] | {"service"})}
    context["tool_policy"] = TOOL_POLICIES[tool]
    context["input_presentation"] = INPUT_PRESENTATION
    context["task"] = "Check idempotency, then shared failures, then tool failures. Return JSON with reason FIRST, then code, then status; use the first true failure condition."
    return context


def parse_prediction(content):
    if not isinstance(content, str) or not content.strip() or len(content) > 20000:
        raise ResponseError("Missing or oversized response content.")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines[0].lower().strip() not in {"```", "```json"}:
            raise ResponseError("Unsupported code fence.")
        text = "\n".join(lines[1:-1])
    try:
        result = decode(text)
    except (ValueError, RecursionError):
        raise ResponseError("Response is not one valid JSON object.") from None
    if not isinstance(result, dict) or set(result) != {"status", "code", "reason"}:
        raise ResponseError("Use exactly status, code and reason.")
    if not isinstance(result["status"], str) or result["status"] not in {"SUCCEEDED", "BLOCKED"}:
        raise ResponseError("Status must be SUCCEEDED or BLOCKED.")
    if not isinstance(result["code"], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", result["code"]):
        raise ResponseError("Code must be an uppercase policy code.")
    if (result["status"] == "SUCCEEDED") != (result["code"] == "OK"):
        raise ResponseError("Status and code are inconsistent.")
    if not isinstance(result["reason"], str) or not result["reason"].strip() or len(result["reason"]) > 2000:
        raise ResponseError("Provide a brief reason of at most 2000 characters.")
    return result


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {k: redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


def inference_settings(max_output_tokens, thinking=False, temperature=None):
    """Build an explicit profile; do not use provider-specific reasoning budgets.

    NVIDIA recommends temperature 1/top_p 1 for reasoning and a large output
    cap (e.g. 10000). This remains one capped call with no automatic retries.
    """
    if type(thinking) is not bool:
        raise ValueError("thinking must be a boolean.")
    if type(max_output_tokens) is not int or not 512 <= max_output_tokens <= 16384:
        raise ValueError("--max-output-tokens must be 512 through 16384.")
    chosen_temperature = (1.0 if thinking else 0.0) if temperature is None else temperature
    if not isinstance(chosen_temperature, (int, float)) or not 0 <= chosen_temperature <= 2:
        raise ValueError("--temperature must be between 0 and 2.")
    settings = {"max_tokens": max_output_tokens, **copy.deepcopy(REQUEST_CONTROLS)}
    settings["temperature"] = chosen_temperature
    settings["extra_body"]["chat_template_kwargs"]["enable_thinking"] = thinking
    if thinking:
        settings["top_p"] = 1.0
    return settings


def visible_answer(message):
    """Exclude provider reasoning fields and an explicitly marked think prefix.

    A JSON-looking object inside a thinking block is never a model prediction.
    This does not repair malformed JSON, change labels or infer a final answer.
    """
    content = message.content or ""
    reasoning_present = bool(getattr(message, "reasoning_content", None)
                             or getattr(message, "reasoning", None))
    removed = False
    if isinstance(content, str) and content.lstrip().startswith("<think>"):
        _, marker, final = content.partition("</think>")
        content = final.strip() if marker else ""
        reasoning_present, removed = True, True
    return content, reasoning_present, removed


class NebiusEvaluator:
    mode = "LIVE NEBIUS NEMOTRON CLASSIFICATION"

    def __init__(self, api_key, max_output_tokens, thinking=False, temperature=None):
        self.request_settings = inference_settings(max_output_tokens, thinking, temperature)
        self.thinking = thinking
        self.timeout_seconds = 120 if thinking else 60
        try:
            from openai import OpenAI
        except ImportError:
            raise ModelError("The openai package is missing. Use the existing project virtual environment.") from None
        self.client = OpenAI(base_url=BASE_URL, api_key=api_key, timeout=self.timeout_seconds, max_retries=0)
        self.api_key, self.max_output_tokens = api_key, max_output_tokens

    def ask(self, context):
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=MODEL, **self.request_settings,
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": json.dumps(context, ensure_ascii=True, indent=2)}])
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            # Never log SDK exception text, request headers or credentials.
            raise ModelError(type(exc).__name__ + (" (HTTP %s)" % status if status else "")) from None
        if not response.choices:
            raise ModelError("Nebius returned no completion choices.")
        choice = response.choices[0]
        usage = response.usage
        content, reasoning_present, removed = visible_answer(choice.message)
        # Record presence/counts only; never log the reasoning text.
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        metadata = {"source": self.mode, "model": response.model, "response_id": response.id,
                    "finish_reason": choice.finish_reason,
                    "response_format_requested": "json_object",
                    "thinking_requested": self.thinking, "reasoning_content_present": reasoning_present,
                    "marked_thinking_content_removed": removed,
                    "reasoning_tokens": reasoning_tokens if type(reasoning_tokens) is int else None,
                    "visible_content_characters": len(content) if isinstance(content, str) else None,
                    "latency_seconds": round(time.monotonic() - started, 3),
                    "refused": bool(getattr(choice.message, "refusal", None)),
                    "input_tokens": getattr(usage, "prompt_tokens", None),
                    "output_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None)}
        # Keep only the visible answer. Never substitute a reasoning trace for it.
        return redact(content, self.api_key), redact(metadata, self.api_key)


def summarize(selected, results):
    attempted = len(results)
    complete = [r for r in results if r["prediction"] is not None]
    valid = [r for r in results if r["expected"]["status"] == "SUCCEEDED"]
    invalid = [r for r in results if r["expected"]["status"] == "BLOCKED"]
    false_allows = sum(r["prediction"] is not None and r["prediction"]["status"] == "SUCCEEDED" for r in invalid)
    false_blocks = sum(r["prediction"] is not None and r["prediction"]["status"] == "BLOCKED" for r in valid)
    tokens = {}
    for field in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"):
        known = [r["metadata"].get(field) for r in results if type(r["metadata"].get(field)) is int]
        tokens[field] = {"known_sum": sum(known) if known else None, "calls_with_usage": len(known),
                         "calls_with_unknown_usage": attempted - len(known)}
    latencies = [r["elapsed_seconds"] for r in results]
    return {"selected": len(selected), "attempted": attempted, "not_attempted": len(selected) - attempted,
            "valid_predictions": len(complete), "decision_correct": sum(r["decision_correct"] for r in results),
            "exact_correct": sum(r["exact_correct"] for r in results),
            "expected_valid_attempted": len(valid), "expected_invalid_attempted": len(invalid),
            "false_allows": false_allows, "false_blocks": false_blocks,
            "errors": attempted - len(complete), "outcomes": dict(Counter(r["outcome"] for r in results)),
            "reasoning_observed_calls": sum(r["metadata"].get("reasoning_content_present") is True for r in results),
            "tokens": tokens, "mean_attempt_seconds": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "observed_billed_cost": None,
            "denominator_note": "Every attempted call is in accuracy denominators, including errors; unattempted rows are reported separately."}


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def report_html(run, selected, results):
    esc = lambda x: html.escape(str(x), quote=True)
    counts = run["counts"]
    cards = [("Decisions correct", "%d / %d" % (counts["decision_correct"], counts["attempted"])),
             ("Decision + code correct", "%d / %d" % (counts["exact_correct"], counts["attempted"])),
             ("False allows", "%d / %d" % (counts["false_allows"], counts["expected_invalid_attempted"])),
             ("Calls attempted", "%d / %d" % (counts["attempted"], counts["selected"]))]
    card_html = "".join('<div class="card"><span>' + esc(k) + '</span><strong>' + esc(v) + '</strong></div>' for k, v in cards)
    lookup = {r["row_id"]: r for r in results}
    rows = []
    for selected_row in selected:
        result = lookup.get(selected_row["row_id"])
        prediction = result["prediction"] if result else None
        observed = (prediction["status"] + " / " + prediction["code"]) if prediction else "No valid prediction"
        outcome = result["outcome"] if result else "NOT_ATTEMPTED"
        reason = prediction["reason"] if prediction else (result.get("error") or "") if result else "Not called."
        expected = selected_row["expected"]
        rows.append('<tr><td>' + esc(selected_row["row_id"]) + '<br><small>' + esc(selected_row["family"]) + '</small></td><td>'
                    + esc(expected["status"] + " / " + expected["code"]) + '</td><td>' + esc(observed) + '</td><td class="'
                    + ('good' if outcome == 'MATCH' else 'bad') + '">' + esc(outcome) + '</td><td>' + esc(reason) + '</td></tr>')
    token_info = counts["tokens"]["total_tokens"]
    usage = str(token_info["known_sum"]) if token_info["known_sum"] is not None else "unknown"
    response_note = ("No usable predictions were returned. The zeros reflect failed response attempts, not observed policy decisions."
                     if counts["attempted"] and not counts["valid_predictions"] else
                     "Usable predictions: %d / %d attempts. Errors remain in the accuracy denominator." % (counts["valid_predictions"], counts["attempted"]))
    controls = run["request_controls"]
    thinking = controls.get("extra_body", {}).get("chat_template_kwargs", {}).get("enable_thinking")
    runtime_note = ("Offline test fixture; no model request was made."
                    if controls.get("mode") == "OFFLINE_TEST_FIXTURE" else
                    "%s; JSON object requested; thinking requested %s; temperature %s; output-token cap %s. Reasoning content was observed in %d returned responses."
                    % (run.get("evaluator_version", EVALUATOR_VERSION), "ON" if thinking else "OFF",
                       controls.get("temperature"), controls.get("max_tokens"), counts["reasoning_observed_calls"]))
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Orkestryn | Nemotron Evaluation</title><style>*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#19283b;font:15px/1.55 system-ui,sans-serif}main{max-width:1450px;margin:auto;padding:36px 24px}h1{font-size:34px;line-height:1.2}.eyebrow{font-weight:750;color:#315bcc;letter-spacing:.08em}.notice{padding:15px 18px;border-left:4px solid #315bcc;background:#e9efff}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:16px;margin:24px 0}.card{background:white;border:1px solid #dde5ef;border-radius:12px;padding:20px}.card span{display:block;color:#52647b}.card strong{font-size:30px}.table{overflow:auto;background:white;border-radius:12px;border:1px solid #dde5ef}table{border-collapse:collapse;width:100%;min-width:950px}th,td{text-align:left;padding:12px;vertical-align:top;border-bottom:1px solid #e8edf4;font-size:13px}th{background:#eaf0f8}td:last-child{min-width:280px}small,footer{color:#52647b}.good{color:#087346;font-weight:700}.bad{color:#a22d42;font-weight:700}footer{margin-top:24px}</style><main>
<div class="eyebrow">ORKESTRYN / NEMOTRON EVALUATION</div><h1>Admission decisions under test</h1>''' + '<p class="notice"><b>' + esc(run["mode"]) + '</b><br>' + esc(run["scope"]) + '</p><p>Split: <b>' + esc(run["dataset"]["split"]) + '</b> | Prompt: ' + esc(PROMPT_VERSION) + ' | Run: ' + esc(run["run_status"]) + '</p><p>' + esc(runtime_note) + '</p><div class="cards">' + card_html + '</div><p><b>' + esc(response_note) + '</b></p><p>False blocks: ' + esc(counts["false_blocks"]) + ' / ' + esc(counts["expected_valid_attempted"]) + ' valid requests. Errors: ' + esc(counts["errors"]) + '. Not attempted: ' + esc(counts["not_attempted"]) + '. Known total tokens: ' + esc(usage) + ' (' + esc(token_info["calls_with_usage"]) + ' calls with usage). Actual billed cost is not available from this report.</p><p>' + esc(run["stop_reason"] or 'Selected batch completed.') + '</p><div class="table"><table><thead><tr><th>Decision checkpoint</th><th>Reference</th><th>Model prediction</th><th>Result</th><th>Model explanation / error</th></tr></thead><tbody>' + ''.join(rows) + '''</tbody></table></div><footer>
The model received request-scoped approval/plan/capability records, all shared allocations and reservations, factual capacity sums, and the requested tool's rules. The harness did not compute an admission decision. Answer keys, scenario families and future events stayed local.
This is a stratified diagnostic sample from shared templates, not an estimate of production failure rates. A correct admission answer does not demonstrate full workflow completion or recovery planning.
Errors count against attempted-call accuracy. Hidden reasoning is not logged. Evidence is stored beside this report in summary.json, selection.json, requests.jsonl and results.jsonl.</footer></main></html>'''


def evaluate(selected, dataset, provider, output_root, limit, max_tokens, sample_seed, progress=True):
    if len(selected) > limit:
        raise ValueError("Selected rows exceed the hard call limit.")
    directory = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / "selection.json", [{"row_id": r["row_id"], "family": r["family"], "expected": r["expected"]} for r in selected])
    (directory / "system_prompt.txt").write_text(SYSTEM, encoding="utf-8")
    results, consecutive_errors, stop_reason = [], 0, None
    run = {"mode": provider.mode, "scope": "Admission classification of saved synthetic checkpoints; no infrastructure actions or recovery operations are executed.",
           "model_requested": MODEL, "evaluator_version": EVALUATOR_VERSION,
           "prompt_version": PROMPT_VERSION, "input_presentation": INPUT_PRESENTATION,
           "system_prompt_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
           "dataset": dataset, "sample_seed": sample_seed, "sampling": "Label-stratified, alternating valid/invalid; diversify valid tools and invalid codes. Not prevalence-weighted.",
           "max_calls": limit, "max_output_tokens_per_call": max_tokens, "automatic_retries": 0,
           "request_controls": copy.deepcopy(getattr(provider, "request_settings", {"mode": "OFFLINE_TEST_FIXTURE"})),
           "client_timeout_seconds": getattr(provider, "timeout_seconds", None),
           "evaluator_sha256": file_hash(Path(__file__)), "run_status": "RUNNING", "stop_reason": None}

    def checkpoint():
        run["counts"] = summarize(selected, results)
        atomic_json(directory / "summary.json", run)

    checkpoint()
    for number, row in enumerate(selected, 1):
        context = model_context(row)
        append_jsonl(directory / "requests.jsonl", {"call": number, "row_id": row["row_id"],
                     "evaluator_version": EVALUATOR_VERSION,
                     "prompt_version": PROMPT_VERSION, "context": context, "request_controls": run["request_controls"],
                     "status": "REQUEST_START_RECORDED"})
        if progress:
            print("\n[NEMOTRON %d/%d] %s" % (number, len(selected), row["input"]["request"]["tool"]), flush=True)
        started = time.monotonic()
        result = {"call": number, "row_id": row["row_id"], "family": row["family"], "expected": row["expected"],
                  "prediction": None, "raw_content": None, "metadata": {}, "error": None,
                  "decision_correct": False, "exact_correct": False}
        try:
            content, metadata = provider.ask(context)
            result["raw_content"], result["metadata"] = content, metadata
            if metadata.get("finish_reason") == "length":
                result.update(outcome="TRUNCATED", error="Output-token limit reached. No completed prediction; this counts as an unsuccessful attempt.")
                stop_reason = "Stopped after the first truncated response. Inspect its metadata before spending more credits."
            elif metadata.get("refused"):
                result.update(outcome="REFUSED", error="The model refused this classification.")
            else:
                prediction = parse_prediction(content)
                result["prediction"] = prediction
                result["decision_correct"] = prediction["status"] == row["expected"]["status"]
                result["exact_correct"] = result["decision_correct"] and prediction["code"] == row["expected"]["code"]
                result["outcome"] = "MATCH" if result["exact_correct"] else "WRONG_CODE" if result["decision_correct"] else "WRONG_DECISION"
        except ResponseError as exc:
            result.update(outcome="INVALID_RESPONSE", error=str(exc))
        except ModelError as exc:
            result.update(outcome="API_ERROR", error=str(exc))
            stop_reason = "Stopped after an API error; no automatic retry was made."
        except KeyboardInterrupt:
            result.update(outcome="INTERRUPTED", error="Interrupted while awaiting a result. Provider usage may be unknown.")
            stop_reason = "Interrupted by the user. Saved completed results and the uncertain attempt."
        except Exception as exc:
            result.update(outcome="RUNNER_ERROR", error="Unexpected evaluator error: " + type(exc).__name__)
            stop_reason = "Stopped after an unexpected error; inspect the saved evidence before another run."
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        results.append(result)
        append_jsonl(directory / "results.jsonl", result)
        consecutive_errors = consecutive_errors + 1 if result["prediction"] is None else 0
        if consecutive_errors >= 3 and stop_reason is None:
            stop_reason = "Stopped after three consecutive unusable responses to avoid wasting further calls."
        run["stop_reason"] = stop_reason
        checkpoint()
        if progress:
            expected = row["expected"]
            print("  Reference: %s / %s" % (expected["status"], expected["code"]))
            if result["prediction"]:
                p = result["prediction"]
                print("  Model:     %s / %s" % (p["status"], p["code"]))
            print("  Result:    " + result["outcome"], flush=True)
            meta = result["metadata"]
            if meta:
                print("  Response: finish=%s, output_tokens=%s, visible_characters=%s, reasoning_present=%s" % (
                    meta.get("finish_reason"), meta.get("output_tokens"), meta.get("visible_content_characters"),
                    meta.get("reasoning_content_present")), flush=True)
        if stop_reason:
            break
    run["run_status"] = ("STOPPED_EARLY" if len(results) < len(selected) else
                         "COMPLETED_WITH_ERRORS" if any(r["prediction"] is None for r in results) else "COMPLETED")
    checkpoint()
    (directory / "report.html").write_text(report_html(run, selected, results), encoding="utf-8")
    return directory, run, results


class EvaluatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import data_factory
        cls.temp = tempfile.TemporaryDirectory()
        cls.dataset_dir, _ = data_factory.run_factory(Path(cls.temp.name) / "datasets")
        cls.rows, cls.dataset = load_dataset(cls.dataset_dir, "development")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def provider(self, replies):
        class Fixture:
            mode = "OFFLINE TEST FIXTURES; NO LIVE CALLS"
            def __init__(self, values): self.values, self.calls = iter(values), 0
            def ask(self, context):
                self.calls += 1
                value = next(self.values)
                if isinstance(value, BaseException): raise value
                return value
        return Fixture(replies)

    def reply(self, status="SUCCEEDED", code="OK", **metadata):
        return canonical({"status": status, "code": code, "reason": "Fixture evidence only."}), {"finish_reason": "stop", **metadata}

    def run_fixture(self, selected, replies):
        provider = self.provider(replies)
        directory, run, results = evaluate(selected, self.dataset, provider,
            Path(self.temp.name) / "evaluations", len(selected), 512, 42, progress=False)
        return provider, directory, run, results

    def test_sample_is_balanced_unique_reproducible(self):
        batch = select_batch(self.rows)
        self.assertEqual(batch, select_batch(self.rows))
        self.assertEqual(len({r["row_id"] for r in batch}), 12)
        self.assertEqual(Counter(r["expected"]["status"] for r in batch), {"SUCCEEDED": 6, "BLOCKED": 6})
        self.assertNotEqual(batch, select_batch(self.rows, seed=43))

    def test_only_context_fields_cross_model_boundary(self):
        row = copy.deepcopy(self.rows[0])
        original_context = model_context(row)
        row["expected"] = {"status": "SECRET_EXPECTED_LABEL"}
        row["family"] = "SECRET_FAMILY"
        row["input"]["future"] = "SECRET_FUTURE"
        text = canonical(model_context(row))
        for marker in ("SECRET_EXPECTED_LABEL", "SECRET_FAMILY", "SECRET_FUTURE"):
            self.assertNotIn(marker, text)
        self.assertNotIn('"row_id"', text)
        self.assertIn('"current_plan_digests"', text)
        self.assertEqual(original_context, model_context(row))

    def test_scoped_records_preserve_shared_resources_and_source(self):
        row = copy.deepcopy(self.rows[0])
        original = copy.deepcopy(row)
        context = model_context(row)
        request, state = row["input"]["request"], row["input"]["state"]
        self.assertEqual(row, original)
        for field, identifier in (("plans", request["run_id"]),
                                  ("approvals", request["approval_id"]),
                                  ("capabilities", request["actor"])):
            self.assertEqual(context["state"][field], {identifier: state[field][identifier]})
        for field in ("deployments", "reservations", "clock", "budget_version", "route_version"):
            self.assertEqual(context["state"][field], state[field])
        context["state"]["plans"][request["run_id"]]["max_units"] = -999
        self.assertEqual(row, original)

    def test_capacity_totals_count_other_owners_and_pending_once(self):
        row = copy.deepcopy(self.rows[0])
        state, run = row["input"]["state"], row["input"]["request"]["run_id"]
        state["stable_units"] = 30
        state["deployments"] = {
            "own": {"owner_run": run, "allocated_units": 12},
            "foreign": {"owner_run": "OTHER", "allocated_units": 7}}
        state["reservations"] = {
            "pending-own": {"owner_run": run, "units": 4, "status": "PENDING"},
            "pending-foreign": {"owner_run": "OTHER", "units": 5, "status": "PENDING"},
            "already-allocated": {"owner_run": run, "units": 12, "status": "CONSUMED"}}
        context = model_context(row)
        self.assertEqual(context["capacity_totals"], {
            "stable_units": 30, "all_deployment_allocated_units": 19,
            "all_pending_reservation_units": 9, "all_committed_units": 58,
            "run_allocated_units": 12, "run_pending_reservation_units": 4,
            "run_committed_units": 16})
        # Only factual sums: no compared capacity thresholds or decision labels.
        self.assertTrue(all(type(v) is int for v in context["capacity_totals"].values()))
        self.assertEqual(context["state"]["deployments"]["foreign"]["allocated_units"], 7)

    def test_missing_approval_plan_and_capability_are_not_filled_in(self):
        row = copy.deepcopy(self.rows[0])
        request = row["input"]["request"]
        request.update(approval_id="MISSING", run_id="MISSING", actor="MISSING")
        context = model_context(row)
        for field in ("approvals", "plans", "capabilities"):
            self.assertEqual(context["state"][field], {})
        self.assertEqual(context["current_plan_digests"], {})
        self.assertEqual(context["capacity_totals"]["run_committed_units"], 0)

    def test_context_building_does_not_execute_or_score_policy(self):
        # Future refactors must not turn this model evaluation into engine
        # evaluation or add effects while preparing model input.
        with patch("simulator.Engine.apply", side_effect=AssertionError("Engine execution forbidden")), \
             patch("simulator.Engine._approval", side_effect=AssertionError("Gate scoring forbidden")), \
             patch("simulator.Engine._execute", side_effect=AssertionError("Gate scoring forbidden")):
            for row in self.rows:
                context = model_context(row)
                for forbidden in ("expected", "prediction", "decision", "row_id", "family"):
                    self.assertNotIn(forbidden, context)

    def test_strict_parser_and_optional_single_fence(self):
        good = self.reply()[0]
        self.assertEqual(parse_prediction("```json\n" + good + "\n```"), parse_prediction(good))
        reason_first = '{"reason":"Fixture evidence only.","code":"OK","status":"SUCCEEDED"}'
        self.assertEqual(parse_prediction(reason_first), parse_prediction(good))
        for bad in ('{"status":"BLOCKED","status":"SUCCEEDED","code":"OK","reason":"x"}',
                    '{"status":[],"code":"OK","reason":"x"}',
                    '{"status":"BLOCKED","code":"OK","reason":"x"}',
                    '{"status":"SUCCEEDED","code":"OK","reason":NaN}', good + good):
            with self.subTest(bad=bad), self.assertRaises(ResponseError): parse_prediction(bad)

    def test_unescaped_quotes_are_not_repaired_into_a_prediction(self):
        malformed = '{"status":"BLOCKED","code":"PLAN_BOUND_EXCEEDED","reason":"reservation_id "r-initial" is absent"}'
        with self.assertRaises(ResponseError):
            parse_prediction(malformed)

    def test_explanation_does_not_override_the_model_code(self):
        # The uploaded v3 response named the right gate in its explanation but
        # retained the wrong code. Score the submitted code, never repair it.
        row = next(r for r in self.rows if r["expected"]["code"] == "CAPACITY_LIMIT_EXCEEDED")
        content = json.dumps({"reason": "Shared capacity is exceeded; CAPACITY_LIMIT_EXCEEDED is the failing gate.",
                              "code": "PLAN_BOUND_EXCEEDED", "status": "BLOCKED"})
        _, _, run, results = self.run_fixture([row], [(content, {"finish_reason": "stop"})])
        self.assertEqual(results[0]["outcome"], "WRONG_CODE")
        self.assertEqual(results[0]["prediction"]["code"], "PLAN_BOUND_EXCEEDED")
        self.assertEqual(run["counts"]["decision_correct"], 1)
        self.assertEqual(run["counts"]["exact_correct"], 0)

    def test_errors_count_in_denominator_and_wrong_codes_are_distinct(self):
        valid = next(r for r in self.rows if r["expected"]["status"] == "SUCCEEDED")
        blocked = next(r for r in self.rows if r["expected"]["status"] == "BLOCKED")
        selected = [valid, blocked]
        _, _, run, results = self.run_fixture(selected, [("invalid JSON", {}), self.reply("BLOCKED", "OTHER_GATE")])
        self.assertEqual(run["counts"]["attempted"], 2)
        self.assertEqual(run["counts"]["decision_correct"], 1)
        self.assertEqual(run["counts"]["exact_correct"], 0)
        self.assertEqual(run["counts"]["errors"], 1)
        self.assertEqual(results[1]["outcome"], "WRONG_CODE")

    def test_false_allows_and_false_blocks(self):
        valid = next(r for r in self.rows if r["expected"]["status"] == "SUCCEEDED")
        blocked = next(r for r in self.rows if r["expected"]["status"] == "BLOCKED")
        _, _, run, _ = self.run_fixture([valid, blocked], [self.reply("BLOCKED", "APPROVAL_MISSING"), self.reply()])
        self.assertEqual(run["counts"]["false_allows"], 1)
        self.assertEqual(run["counts"]["false_blocks"], 1)

    def test_api_error_stops_and_saves_unattempted_count(self):
        selected = select_batch(self.rows, 4)
        provider, directory, run, _ = self.run_fixture(selected, [ModelError("HTTP 429")])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(run["counts"]["not_attempted"], 3)
        self.assertEqual(run["counts"]["tokens"]["total_tokens"]["known_sum"], None)
        self.assertEqual(run["run_status"], "STOPPED_EARLY")
        self.assertTrue((directory / "report.html").is_file())
        self.assertEqual(len(read_jsonl(directory / "results.jsonl")), 1)

    def test_truncation_stops_after_first_call(self):
        selected = select_batch(self.rows, 4)
        provider, directory, run, results = self.run_fixture(selected, [self.reply(finish_reason="length")])
        self.assertEqual(provider.calls, 1)
        self.assertEqual([r["outcome"] for r in results], ["TRUNCATED"])
        self.assertEqual(run["counts"]["decision_correct"], 0)
        self.assertEqual(run["counts"]["not_attempted"], 3)
        self.assertIn("No usable predictions were returned", (directory / "report.html").read_text())

    def test_interrupted_request_remains_an_unsuccessful_attempt(self):
        provider, _, run, results = self.run_fixture(select_batch(self.rows, 2), [KeyboardInterrupt()])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(results[0]["outcome"], "INTERRUPTED")
        self.assertEqual(run["counts"]["decision_correct"], 0)
        self.assertEqual(run["counts"]["attempted"], 1)

    def test_three_bad_outputs_stop_before_cap(self):
        provider, _, run, _ = self.run_fixture(select_batch(self.rows, 5), [("bad", {})] * 3)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(run["counts"]["not_attempted"], 2)

    def test_holdout_and_changed_answer_keys_rejected(self):
        with self.assertRaises(DatasetError): load_dataset(self.dataset_dir, "holdout")
        path = self.dataset_dir / "development" / "answers.jsonl"
        original = path.read_text()
        rows = [decode(line) for line in original.splitlines()]
        rows[0]["expected"] = {"status": "BLOCKED", "code": "APPROVAL_MISSING"}
        try:
            path.write_text("\n".join(canonical(r) for r in rows), encoding="utf-8")
            with self.assertRaises(DatasetError): load_dataset(self.dataset_dir, "development")
        finally:
            path.write_text(original, encoding="utf-8")

    def test_hard_cap_and_secret_redaction(self):
        provider = self.provider([])
        with self.assertRaises(ValueError):
            evaluate(select_batch(self.rows, 2), self.dataset, provider,
                     Path(self.temp.name) / "capped", 1, 512, 42, progress=False)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(redact({"content": "x test-secret y", "nested": ["test-secret"]}, "test-secret"),
                         {"content": "x [REDACTED] y", "nested": ["[REDACTED]"]})

    def test_sdk_contract_redacts_echo_and_uses_no_retries(self):
        captured = {}
        secret = "fake-api-secret-for-offline-test"
        def create(**kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(model=MODEL, id="test-response", usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=20, total_tokens=120,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=10)), choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=self.reply()[0] + secret,
                                                               refusal=None, reasoning_content="DO_NOT_SAVE"))])
        def construct(**kwargs):
            captured["client"] = kwargs
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=construct)}):
            provider = NebiusEvaluator(secret, 4096)
            content, metadata = provider.ask(model_context(self.rows[0]))
        self.assertEqual(captured["client"]["max_retries"], 0)
        self.assertEqual(captured["client"]["base_url"], BASE_URL)
        self.assertEqual(captured["request"]["model"], MODEL)
        self.assertEqual(captured["request"]["max_tokens"], 4096)
        self.assertEqual(captured["request"]["temperature"], 0)
        self.assertEqual(captured["request"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["request"]["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(len(captured["request"]["messages"]), 2)
        self.assertNotIn('"expected"', captured["request"]["messages"][1]["content"])
        self.assertNotIn(secret, content + canonical(metadata))
        self.assertNotIn("DO_NOT_SAVE", content + canonical(metadata))
        self.assertEqual(metadata["total_tokens"], 120)
        self.assertIs(metadata["thinking_requested"], False)
        self.assertEqual(metadata["response_format_requested"], "json_object")
        self.assertIs(metadata["reasoning_content_present"], True)
        self.assertEqual(metadata["reasoning_tokens"], 10)
        self.assertGreater(metadata["visible_content_characters"], 0)

    def test_completed_batch_saves_report_without_modifying_dataset(self):
        selected = select_batch(self.rows)
        before = {name: file_hash(self.dataset_dir / name) for name in self.dataset["file_hashes"]}
        replies = [self.reply(r["expected"]["status"], r["expected"]["code"], total_tokens=120) for r in selected]
        provider, directory, run, results = self.run_fixture(selected, replies)
        self.assertEqual(provider.calls, 12)
        self.assertEqual(run["counts"]["exact_correct"], 12)
        self.assertEqual(run["counts"]["tokens"]["total_tokens"]["known_sum"], 1440)
        self.assertEqual(run["run_status"], "COMPLETED")
        self.assertEqual(before, {name: file_hash(self.dataset_dir / name) for name in before})
        requests = read_jsonl(directory / "requests.jsonl")
        self.assertEqual(len(requests), 12)
        self.assertTrue(all("expected" not in request["context"] for request in requests))
        results[0]["prediction"]["reason"] = "<script>unsafe()</script>"
        rendered = report_html(run, selected, results)
        self.assertIn("&lt;script&gt;unsafe()&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)

    def test_thinking_profile_reaches_sdk_without_mutating_off_profile(self):
        captured = {}
        before = copy.deepcopy(REQUEST_CONTROLS)
        def create(**kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(model=MODEL, id="thinking-fixture", usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=70, total_tokens=170,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=50)), choices=[SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content=self.reply()[0],
                                                               reasoning_content="PRIVATE_TRACE", refusal=None))])
        def construct(**kwargs):
            captured["client"] = kwargs
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=construct)}):
            provider = NebiusEvaluator("fake-test-key", 10000, thinking=True)
            content, metadata = provider.ask(model_context(self.rows[0]))
        self.assertEqual(captured["client"]["timeout"], 120)
        self.assertEqual(captured["client"]["max_retries"], 0)
        self.assertEqual(captured["request"]["max_tokens"], 10000)
        self.assertEqual(captured["request"]["extra_body"], {"chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(captured["request"]["temperature"], 1.0)
        self.assertEqual(captured["request"]["top_p"], 1.0)
        self.assertEqual(captured["request"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["request"]["messages"][0]["content"], SYSTEM)
        self.assertTrue(metadata["thinking_requested"])
        self.assertTrue(metadata["reasoning_content_present"])
        self.assertEqual(metadata["reasoning_tokens"], 50)
        self.assertNotIn("PRIVATE_TRACE", content + canonical(metadata))
        self.assertEqual(REQUEST_CONTROLS, before)
        off = inference_settings(1024)
        self.assertFalse(off["extra_body"]["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(off["temperature"], 0)

    def test_marked_thinking_cannot_be_saved_or_scored_as_an_answer(self):
        final = self.reply()[0]
        for prefix in ("<think>PRIVATE_TRACE", "  <think>PRIVATE_TRACE"):
            content, present, removed = visible_answer(SimpleNamespace(content=prefix + "</think>\n" + final))
            self.assertEqual(content, final)
            self.assertTrue(present and removed)
            self.assertNotIn("PRIVATE_TRACE", content)
        for text in ("<think>" + final, "<think>" + final + "</think>"):
            content, present, removed = visible_answer(SimpleNamespace(content=text))
            self.assertEqual(content, "")
            with self.assertRaises(ResponseError): parse_prediction(content)

    def test_invalid_inference_controls_fail_before_client_creation(self):
        client = unittest.mock.Mock(side_effect=AssertionError("Client must not be created"))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=client)}):
            for tokens in (0, 511, 16385, True, 10000.0):
                with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                    NebiusEvaluator("fake-test-key", tokens, thinking=True)
            for temperature in (-1, 3, float("nan")):
                with self.subTest(temperature=temperature), self.assertRaises(ValueError):
                    NebiusEvaluator("fake-test-key", 10000, thinking=True, temperature=temperature)
        client.assert_not_called()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--live", action="store_true", help="Make paid Nebius calls; otherwise only preview the batch.")
    parser.add_argument("--dataset", type=Path, help="Factory run folder containing summary.json.")
    parser.add_argument("--split", choices=("development", "validation"), default="development")
    parser.add_argument("--limit", type=int, default=2, help="Hard request cap; 1..100 (default 2 for a small probe).")
    parser.add_argument("--sample-seed", type=int, default=42)
    parser.add_argument("--thinking", choices=("off", "on"), default="off",
                        help="Explicit inference profile (default off); on uses temperature 1/top_p 1.")
    parser.add_argument("--temperature", type=float, help="Override the profile's temperature (0..2).")
    parser.add_argument("--max-output-tokens", type=int,
                        help="Cap including reasoning; default 1024 when off, 10000 when on; maximum 16384.")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "evaluation_runs")
    parser.add_argument("--open-report", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2, buffer=True).run(unittest.defaultTestLoader.loadTestsFromTestCase(EvaluatorTests))
        return 0 if result.wasSuccessful() else 1
    thinking = args.thinking == "on"
    if args.max_output_tokens is None:
        args.max_output_tokens = 10000 if thinking else 1024
    try:
        settings = inference_settings(args.max_output_tokens, thinking, args.temperature)
    except ValueError as exc:
        parser.error(str(exc))
    directory = find_dataset(args.dataset, Path(__file__).resolve().parent / "factory_runs")
    rows, dataset = load_dataset(directory, args.split)
    selected = select_batch(rows, args.limit, args.sample_seed)
    distribution = Counter(r["expected"]["status"] for r in selected)
    print("ORKESTRYN - NEMOTRON DECISION EVALUATOR")
    print("Evaluator version: " + EVALUATOR_VERSION)
    print("Dataset: " + str(directory))
    print("Split: %s | Selected decisions: %d/%d" % (args.split, len(selected), len(rows)))
    print("Reference mix: %d valid, %d invalid (stratified diagnostic sample)" % (distribution["SUCCEEDED"], distribution["BLOCKED"]))
    print("At most %d API calls, up to %d output tokens per call, no automatic retries." % (args.limit, args.max_output_tokens))
    print("Prompt version: " + PROMPT_VERSION)
    print("Input presentation: " + INPUT_PRESENTATION)
    print("Thinking requested %s via chat_template_kwargs; temperature %s%s." % (
        args.thinking.upper(), settings["temperature"], "; top_p 1" if thinking else ""))
    if thinking:
        print("Reasoning profile: prompt and scoring are unchanged. This is not a guarantee of improved accuracy.")
    print("Response format: JSON object requested; server support is checked by the live call.")
    print("The first truncated response stops the batch. No automatic fallback or retry.")
    print("No infrastructure actions are executed. Holdout is reserved.")
    if not args.live:
        print("\nPREVIEW ONLY: no key needed, no model calls, no credits used.")
        print("To run this batch, add --live --open-report to the same command.")
        return 0
    print("\nLIVE MODE: existing Nebius credits will be used.")
    api_key = (os.environ.get("NEBIUS_API_KEY") or getpass("Paste your Nebius API key and press Enter: ")).strip()
    if not api_key:
        raise ValueError("A Nebius API key is required for --live.")
    provider = NebiusEvaluator(api_key, args.max_output_tokens, thinking=thinking, temperature=args.temperature)
    output, run, _ = evaluate(selected, dataset, provider, args.output, args.limit, args.max_output_tokens, args.sample_seed)
    counts = run["counts"]
    print("\nRun status: " + run["run_status"])
    print("Usable predictions: %d/%d attempted" % (counts["valid_predictions"], counts["attempted"]))
    print("Decision matches: %d/%d attempted" % (counts["decision_correct"], counts["attempted"]))
    print("Decision + code matches: %d/%d attempted" % (counts["exact_correct"], counts["attempted"]))
    print("False allows: %d | False blocks: %d | Errors: %d | Not attempted: %d" % (
        counts["false_allows"], counts["false_blocks"], counts["errors"], counts["not_attempted"]))
    print("Evidence: " + str(output))
    print("Report: " + str(output / "report.html"))
    if run["stop_reason"]:
        print(run["stop_reason"])
    if args.open_report:
        import webbrowser
        webbrowser.open((output / "report.html").resolve().as_uri())
    return 2 if counts["errors"] or counts["not_attempted"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ModelError, KeyError) as exc:
        print("EVALUATOR STOPPED: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nStopped before another request was started.", file=sys.stderr)
        raise SystemExit(130)
