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

from infra import chat_safety
from infra.worker import extraction
from infra.worker.budget_config import model_for_role
from infra.worker.change_draft import _assert_global_endpoint, _canon_json, _clip
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget,
    ProviderExecutionError, ProviderInvalidResponseError, ProviderRateLimitError, ProviderTimeoutError, is_timeout_exception,
)
from infra.worker.gap_answer import _model_object
from infra.worker.live_analysis import _client_for_model, _field, _token_count

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


def call_pinned_json(ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget, role: str, key_set: set, optional_keys: frozenset = frozenset()):
    """One pinned-role provider call returning a JSON object with exactly `key_set` keys (`optional_keys` of them may be absent: a key added to a role later, so a
    recorded answer from before still replays; the executor decides what an absent key means). -> (obj, pinned_model, served_model, in, out, thought)."""
    config = ctx.config
    if role not in (getattr(config, "role_model_ids", None) or {}):
        raise NonRetryableExecutionError("role_model_not_pinned", f"role_model_ids has no {role!r} entry")
    model_id = model_for_role(config, role)
    if extraction._input_token_bound(request.prompt, request.schema) > config.max_input_tokens_per_call:
        raise NonRetryableExecutionError("budget_exceeded", "explanation request exceeds input ceiling")
    _assert_global_endpoint()
    call_config = {"max_output_tokens": ctx.max_output_tokens, "temperature": 0.0, "response_mime_type": "application/json"}
    if isinstance(getattr(request, "schema", None), dict) and request.schema:
        # the pinned output schema is also sent as the provider's structured-output schema (Vertex: response_schema; unsupported keywords are ignored by the
        # provider, the server-side validators below still decide). Live finding 2026-10-17: without it the lighter model answered 3 of 7 roles with its own keys.
        call_config["response_schema"] = provider_schema(request.schema)
    from infra.worker.budget_config import thinking_level_for_role
    level = thinking_level_for_role(config, role)
    if level:                                                     # only a configured level is sent; nothing is guessed for the model
        call_config["thinking_config"] = {"thinking_level": level}
    timeout_s = getattr(config, "request_timeout_seconds", None)
    if timeout_s:
        call_config["http_options"] = {"timeout": int(timeout_s * 1000)}
    calls.consume()
    try:
        client = _client_for_model(model_id)
        response = client.models.generate_content(model=model_id, contents=request.prompt, config=call_config)
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if callable(code):
            code = code()
        if code == 429:
            raise ProviderRateLimitError("provider rate limited") from exc
        if is_timeout_exception(exc, code):
            raise ProviderTimeoutError("provider timed out") from exc
        err = ProviderExecutionError("provider request failed")
        # safe diagnostics for a refused request (live smoke LHA-SMOKE-20261018-03: an HTTP 400 left no reason on the attempt): status, class and the provider's short
        # message; the message is dropped if it echoes the prompt. No usage was reported, so the attempt stays priced fail-closed.
        msg = " ".join(str(exc).split())[:240]
        err.diagnostics = {"stage": "request", "status_code": code if isinstance(code, int) else None, "error_class": type(exc).__name__,
                           "message": None if (request.prompt or "")[:60] and (request.prompt or "")[:60] in str(exc) else msg}
        raise err from exc
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
    ctx.request_context.setdefault("provider_usage", []).append({"model_id": actual, "input_tokens": in_t, "output_tokens": out_t, "thought_tokens": th_t})
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
