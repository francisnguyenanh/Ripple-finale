"""Job kind "explain", executor_id "explain.v1", role "intent.explain": plain-English wording of an already verified result (one model call).

The facts in the payload were collected by the SERVER from stored records the requesting account may read. The model only phrases
them. The server then checks the text: it is safe (no link, markup, id), short, and every number in it is a number from the facts.
Nothing here judges, extracts, decides, approves or applies. The result is stored with the snapshot it describes; the page reads it,
it never asks a model on render. Role `intent.explain` must be pinned in the budget config (no fallback), the endpoint must be the
Vertex global one and the served model must equal the pinned one.
"""
from __future__ import annotations

from infra.worker.provider_response import attach_after_answer, require_normal_finish

import hashlib
import json
import pathlib
import re
import time

from infra import chat_safety
from infra.worker import extraction
from infra.worker.budget_config import model_for_role
from infra.worker.change_draft import _assert_global_endpoint, _assert_vertex_backend, _canon_json, _clip
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget,
    ProviderExecutionError, ProviderInvalidResponseError, ProviderRateLimitError, ProviderTimeoutError, is_timeout_exception,
)
from infra.worker.gap_answer import _model_object
from infra.worker.live_analysis import generate_and_close, generate_bounded, _client_for_model, _field, _token_count

EXECUTOR_ID = "explain.v1"
ROLE = "intent.explain"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "explain.json"
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_ID = re.compile(r"\b(?:art|cand|job|rev|src|km|vc|intent|chat|dec|finding)-[0-9a-z_-]{6,}\b|\b[0-9a-f]{32,}\b")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def numbers_in(value) -> set[float]:
    out: set[float] = set()
    for m in _NUM.findall(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict) or not payload["facts"]:
        return "facts_required"
    if not isinstance(payload.get("snapshot"), dict) or not isinstance(payload.get("subject"), str):
        return "snapshot_required"
    return None


def verify_text(text, facts: dict, max_chars: int) -> str:
    """The wording the server accepts. Raises NonRetryableExecutionError when it adds anything the facts do not carry."""
    def bad(why):
        raise NonRetryableExecutionError("explanation_rejected", why)
    if not isinstance(text, str) or not text.strip():
        bad("empty")
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) > max_chars:
        bad("too_long")
    if chat_safety.unsafe_output(t) or _ID.search(t):
        bad("unsafe_or_id")
    allowed = numbers_in(facts) | set(map(float, range(0, 11)))
    extra = [n for n in numbers_in(t) if n not in allowed]
    if extra:
        bad("number_not_in_facts")
    return t


PRIORITY_HEADERS = {"X-Vertex-AI-LLM-Request-Type": "shared", "X-Vertex-AI-LLM-Shared-Request-Type": "priority"}


def _traffic_type(usage) -> str | None:
    """The provider's traffic type of one response (`usage_metadata.traffic_type`, an enum or a string), as its name; None when absent."""
    t = _field(usage, "traffic_type") if usage is not None else None
    if t is None:
        return None
    name = getattr(t, "name", None) or getattr(t, "value", None) or str(t)
    return str(name).split(".")[-1].upper()


def served_service_tier(requested: str | None, traffic: str | None) -> str | None:
    """The tier a call is priced at. Requested priority: served ON_DEMAND_PRIORITY -> priority; downgraded (any other reported traffic type) -> standard;
    no traffic type reported -> priority (fail closed: never priced below what may have been served). No tier requested -> standard."""
    if requested != "priority":
        return None
    if traffic is None or traffic == "ON_DEMAND_PRIORITY":
        return "priority"
    return None


def last_service_tier(ctx) -> str | None:
    """The served tier of the last provider call of this attempt (recorded by `call_pinned_json`)."""
    rows = (getattr(ctx, "request_context", None) or {}).get("provider_usage") or []
    return rows[-1].get("service_tier") if rows else None


def call_pinned_json(ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget, role: str, key_set: set, optional_keys: frozenset = frozenset(),
                     *, deadline_left: float | None = None):
    """One pinned-role provider call returning a JSON object with exactly `key_set` keys (`optional_keys` of them may be absent: a key added to a role later, so a
    recorded answer from before still replays; the executor decides what an absent key means). -> (obj, pinned_model, served_model, in, out, thought)."""
    config = ctx.config
    if role not in (getattr(config, "role_model_ids", None) or {}):
        raise NonRetryableExecutionError("role_model_not_pinned", f"role_model_ids has no {role!r} entry")
    model_id = model_for_role(config, role)
    if extraction._input_token_bound(request.prompt, request.schema) > config.max_input_tokens_per_call:
        raise NonRetryableExecutionError("budget_exceeded", "explanation request exceeds input ceiling")
    from infra.worker.budget_config import LOCATION_ROLES, LOCATIONS, location_for_role
    location = location_for_role(config, role)
    if location is None:
        _assert_global_endpoint()                                 # every role without a configured location: the global endpoint only, as before
    else:                                                         # R21: only an allow-listed role, only a multi-region, only the Vertex backend
        if role not in LOCATION_ROLES or location not in LOCATIONS:
            raise NonRetryableExecutionError("endpoint_location_not_allowed", f"role={role!r} location={location!r}")
        _assert_vertex_backend()
    from infra.worker.budget_config import generation_cap_for_role
    call_config = {"max_output_tokens": generation_cap_for_role(config, role), "temperature": 0.0, "response_mime_type": "application/json"}   # R23: the role's own cap
    if isinstance(getattr(request, "schema", None), dict) and request.schema:
        # the pinned output schema is also sent as the provider's structured-output schema (Vertex: response_schema; unsupported keywords are ignored by the
        # provider, the server-side validators below still decide). Live finding 2026-10-17: without it the lighter model answered 3 of 7 roles with its own keys.
        call_config["response_schema"] = provider_schema(request.schema)
    from infra.worker.budget_config import thinking_level_for_role
    level = thinking_level_for_role(config, role)
    if level:                                                     # only a configured level is sent; nothing is guessed for the model
        call_config["thinking_config"] = {"thinking_level": level}
    from infra.worker.budget_config import role_request_timeout
    deadline = role_request_timeout(config, role)                 # R11: the role's own deadline (request.assistant 45 s); other roles keep the shared timeout
    if deadline_left is not None:                                 # R18: a later call of one job gets only what is left of the job's deadline
        deadline = min(deadline, deadline_left) if deadline else deadline_left
    timeout_s = deadline or getattr(config, "request_timeout_seconds", None)
    if timeout_s:
        call_config["http_options"] = {"timeout": int(timeout_s * 1000)}
    from infra.worker.budget_config import service_tier_for_role
    tier = service_tier_for_role(config, role)
    if tier == "priority":                                        # R7: Vertex Priority PayGo only (no provisioned throughput first), per the official request headers
        call_config.setdefault("http_options", {})["headers"] = dict(PRIORITY_HEADERS)
    calls.consume()
    sent = time.monotonic()
    try:
        client = _client_for_model(model_id) if location is None else _client_for_model(model_id, location=location)
        # R21: the role's deadline bounds the wall clock too (the SDK's HTTP timeout is per phase); only roles with their own deadline use the bound
        if deadline:
            response = generate_bounded(client, deadline, model=model_id, contents=request.prompt, config=call_config)
        else:
            response = generate_and_close(client, model=model_id, contents=request.prompt, config=call_config)
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if callable(code):
            code = code()
        if code == 429:                                           # R19: the typed safe diagnostics too, so a project quota and shared-capacity contention can be told apart
            from infra.worker.safe_diagnostics import safe_diagnostics
            err = ProviderRateLimitError("provider rate limited")
            err.diagnostics = safe_diagnostics({"stage": "request", "status_code": 429, "error_class": type(exc).__name__, "message": str(exc)})
            raise err from exc
        if is_timeout_exception(exc, code):
            raise ProviderTimeoutError(f"provider timed out at the {deadline:g} s deadline" if deadline else "provider timed out") from exc
        err = ProviderExecutionError("provider request failed")
        # safe diagnostics for a refused request (live smoke LHA-SMOKE-20261018-03: an HTTP 400 left no reason on the attempt): status, class and the provider's short
        # message; the message is dropped if it echoes the prompt. No usage was reported, so the attempt stays priced fail-closed.
        # AH-05: the provider's message only in its typed safe form (short, no quotes / brackets / tokens), else only its length: a prefix check on the prompt
        # did not cover an echo of a later prompt fragment
        from infra.worker.safe_diagnostics import safe_diagnostics
        err.diagnostics = safe_diagnostics({"stage": "request", "status_code": code if isinstance(code, int) else None, "error_class": type(exc).__name__,
                                            "message": str(exc)})
        raise err from exc
    if deadline and time.monotonic() - sent > deadline:
        # R11: an answer that arrives after the role's deadline is not used (the person was already told it did not come in time); the usage the provider
        # reported is booked on the attempt (never the answer), as the semantic layers do for an answer after their latency bound
        raise attach_after_answer(ProviderTimeoutError(f"provider answered after the {deadline:g} s deadline; the answer was not used"), response,
                                  model_id=model_id, requested_cap=getattr(ctx, "max_output_tokens", None), stage="after_deadline")
    actual = _field(response, "model_version") or model_id
    if not isinstance(actual, str) or actual not in config.model_prices:
        raise NonRetryableExecutionError("unpriced_model")
    if actual != model_id:
        raise NonRetryableExecutionError("served_model_differs_from_pinned_role_model", f"{actual}")
    require_normal_finish(response, model_id=model_id, requested_cap=getattr(ctx, "max_output_tokens", None))
    usage = _field(response, "usage_metadata")
    in_t, out_t = _token_count(usage, "prompt_token_count"), _token_count(usage, "candidates_token_count")
    th_t, total = _token_count(usage, "thoughts_token_count"), _token_count(usage, "total_token_count")
    if total is not None and in_t is not None and out_t is not None:
        th_t = max(th_t or 0, total - in_t - out_t)
    cap = getattr(ctx, "max_output_tokens", None)
    try:
        text = _field(response, "text")
    except (ValueError, IndexError, AttributeError) as exc:
        raise attach_after_answer(ProviderInvalidResponseError("provider returned no text"), response, model_id=model_id, requested_cap=cap, stage="text") from exc
    try:
        obj = _model_object(text)
    except ProviderInvalidResponseError as exc:
        # the provider answered (and is billed): keep its usage and a safe shape diagnostic on the error instead of pricing the attempt at the ceiling
        attach_after_answer(exc, response, model_id=model_id, requested_cap=cap, stage="json")
        exc.diagnostics["json_shape"] = _json_shape(text)
        raise
    if not (set(key_set) - set(optional_keys) <= set(obj) <= set(key_set)):
        exc = NonRetryableExecutionError("schema_violation", "keys")
        attach_after_answer(exc, response, model_id=model_id, requested_cap=cap, stage="keys")
        exc.diagnostics["keys"] = sorted(str(k) for k in obj)[:20]
        raise exc
    # the reported usage of this answered call, for the worker: a result the executor refuses AFTER this point (schema, guards) is settled at this usage, not at the ceiling
    traffic = _traffic_type(usage)
    served_tier = served_service_tier(tier, traffic)
    ctx.request_context.setdefault("provider_usage", []).append({"model_id": actual, "input_tokens": in_t, "output_tokens": out_t, "thought_tokens": th_t,
                                                                 "traffic_type": traffic, "requested_tier": tier, "service_tier": served_tier,
                                                                 "location": location or "global"})
    return obj, model_id, actual, in_t, out_t, th_t


def provider_schema(schema):
    """The pinned JSON schema in the shape the provider's structured output accepts (official supported keywords: type, properties, required, items, enum of
    strings, nullable, description, min/max): a type list with "null" becomes `nullable`, a null enum member becomes `nullable`, unsupported keywords are
    dropped here so the SDK conversion cannot refuse the pinned file. Pure; the pinned file itself is untouched and remains the server-side validator's."""
    if isinstance(schema, list):
        return [provider_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in ("additionalProperties", "$schema", "title", "examples", "default", "const", "pattern", "minLength", "maxLength"):
            continue
        if key == "type" and isinstance(value, list):
            rest = [t for t in value if t != "null"]
            if len(rest) != len(value):
                out["nullable"] = True
            out["type"] = rest[0] if len(rest) == 1 else rest
            continue
        if key == "enum" and isinstance(value, list) and any(v is None for v in value):
            out["nullable"] = True
            out["enum"] = [v for v in value if v is not None]
            continue
        if key in ("properties",) and isinstance(value, dict):
            out[key] = {k: provider_schema(v) for k, v in value.items()}
            continue
        out[key] = provider_schema(value) if isinstance(value, (dict, list)) else value
    return out


def _json_shape(text) -> dict:
    """Safe description of a JSON answer that is not an object: its type, length and (for a list) the element types. No content."""
    out = {"text_chars": len(text) if isinstance(text, str) else None}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        out["json"] = "invalid"
        return out
    out["json"] = type(value).__name__
    if isinstance(value, list):
        out["items"] = len(value)
        out["item_types"] = sorted({type(v).__name__ for v in value})[:5]
    return out


class ExplainExecutor:
    external = True
    executor_id = EXECUTOR_ID

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        err = validate_payload(ctx.job["payload"])
        if err:
            raise NonRetryableExecutionError(err)
        frozen, sha = load_config()
        facts = ctx.job["payload"]["facts"]
        blob = _canon_json(facts)
        if len(blob) > frozen["limits"]["max_facts_chars"]:
            raise NonRetryableExecutionError("budget_exceeded", "facts exceed the limit")
        prompt = frozen["prompt_template"].replace("{facts}", blob)
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "explanation request exceeds input ceiling")
        ctx.request_context.update(ex_facts=facts, ex_limits=frozen["limits"], ex_sha=sha, ex_snapshot=ctx.job["payload"]["snapshot"],
                                   ex_subject=ctx.job["payload"]["subject"])
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        obj, model_id, actual, in_t, out_t, th_t = call_pinned_json(ctx, request, calls, ROLE, {"text"})
        rc = ctx.request_context
        wording = verify_text(obj["text"], rc["ex_facts"], rc["ex_limits"]["max_text_chars"])
        result = {"text": _clip(wording, rc["ex_limits"]["max_text_chars"]), "subject": rc["ex_subject"], "snapshot": rc["ex_snapshot"],
                  "label": "AI-written wording of verified facts", "executor_id": EXECUTOR_ID, "draft_only": True, "applies_nothing": True,
                  "provider_calls": 1, "config_sha256": rc["ex_sha"],
                  "endpoint": {"backend": "vertex", "location": "global", "pinned_model": model_id, "served_model": actual}}
        result = json.loads(_canon_json(result))
        return ExecutionOutcome(result=result, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
