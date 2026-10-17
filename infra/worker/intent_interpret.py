"""Job kind "intent.interpret", executor_id "intent.interpret.v1": free-text request -> structured UNDERSTANDING (one model call).

The model only PROPOSES {status, message, questions, fields, assumptions}. The server then verifies it:
  * NEW value must be a number the person typed in the conversation (the model never invents it);
  * OLD value must be typed by the person or equal a stored current value of a known rule (else it is asked, not assumed);
  * fields must pass the same `normalise_brief_form` the structured form uses; READY is downgraded to CLARIFY otherwise.
The result is an understanding, not an intent: nothing is parsed, confirmed, drafted or applied here. Role `intent.interpret`
must be pinned in the budget config (no fallback to another model), the endpoint must be the Vertex global one, and the served
model must equal the pinned one. Business text in, business text out; ids are never shown to the model."""
from __future__ import annotations

from infra.worker.provider_response import require_normal_finish

import hashlib
import json
import pathlib
import re

from core.change_intelligence.v2.brief_form import normalise_brief_form
from infra import chat_safety, v2_chat_store as chat
from infra.chat_numbers import numbers_with_quotes, quote_for
from infra.worker import extraction
from infra.worker.budget_config import model_for_role
from infra.worker.change_brief import _num, _numbers_in
from infra.worker.change_draft import _assert_global_endpoint, _canon_json, _clip
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError, ProviderCallBudget,
    ProviderExecutionError, ProviderInvalidResponseError, ProviderRateLimitError, ProviderTimeoutError,
    is_timeout_exception,
)
from infra.worker.gap_answer import _model_object, load_frozen_config as _gap_config
from infra.worker.live_analysis import _client_for_model, _field, _token_count

EXECUTOR_ID = "intent.interpret.v1"
ROLE = "intent.interpret"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "intent_interpret.json"
_REQUIRED = {"status", "message", "questions", "fields", "assumptions"}
_KEYS = _REQUIRED | {"changes", "out_of_scope"}
CHANGE_KINDS = ("value", "scope", "approver", "condition", "exception", "other")
LIMITS_TEXT = ("Ripple prepares and applies changes to the NUMBER of an existing rule (a limit, a threshold, a tolerance). Changes of scope, "
               "approvers, conditions or exceptions are recorded here only: nothing is started and nothing is applied for them.")
_FIELD_KEYS = ("subject", "scope", "unit", "currency", "old_value", "new_value", "applies_to")
STATUSES = ("READY", "CLARIFY", "UNSUPPORTED")


def load_config() -> tuple[dict, str]:
    raw = CONFIG_PATH.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("conversation_id"), str) or not payload["conversation_id"]:
        return "conversation_id_required"
    return None


def _known_rules(ctx, rev, limit) -> list:
    """Stored current values the model may use as the OLD value: {subject, value, unit, scope} from completed extractions."""
    from infra.worker.verification import _completed_jobs
    out, seen = [], set()
    for j in _completed_jobs(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], "extraction"):
        if rev and j["pinned_source_revision_id"] != rev:
            continue
        for c in (j["result"] or {}).get("candidates") or []:
            if not isinstance(c, dict) or not isinstance(c.get("value"), (int, float, str)):
                continue
            row = {"subject": _clip(str(c.get("subject") or c.get("predicate") or ""), 80), "predicate": _clip(str(c.get("predicate") or ""), 80),
                   "value": c.get("value"),
                   "unit": c.get("unit"), "scope": c.get("scope") if isinstance(c.get("scope"), dict) else None}
            key = _canon_json(row)
            if row["subject"] and key not in seen:
                seen.add(key)
                out.append(row)
    return out[:limit]


def _check_shape(obj: dict, max_q: int) -> None:
    def bad(why):
        raise NonRetryableExecutionError("schema_violation", why)
    if not _REQUIRED <= set(obj) <= _KEYS:
        bad("keys")
    if obj["status"] not in STATUSES:
        bad("status")
    if not isinstance(obj["message"], str):
        bad("message")
    for key in ("questions", "assumptions"):
        if not isinstance(obj[key], list) or any(not isinstance(x, str) for x in obj[key]):
            bad(key)
    if not isinstance(obj["fields"], dict):
        bad("fields")
    if not isinstance(obj.get("changes", []), list) or not all(isinstance(x, dict) for x in obj.get("changes", [])):
        bad("changes")
    if not isinstance(obj.get("out_of_scope", []), list) or not all(isinstance(x, str) for x in obj.get("out_of_scope", [])):
        bad("out_of_scope")


def _norm_text(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().casefold()


def resolve(obj: dict, user_text: str, rules: list, patterns, limits: dict) -> dict:
    """Server-side verification of a model understanding. Pure: shared by the executor and unit tests.
    Every value must be traceable to the person's words (`sources`); a guess is an assumption the person has to acknowledge."""
    typed_vals = {v for v, _ in numbers_with_quotes(user_text)}
    stored = {_num(r["value"]) for r in rules if _num(r.get("value")) is not None}
    f_in = obj["fields"]
    fields = {k: f_in.get(k) for k in _FIELD_KEYS}
    problems: list = []
    notes: list = []
    sources: dict = {}
    questions = [_clip(q.strip(), 200) for q in obj["questions"] if q.strip()][: limits["max_questions"]]
    status = obj["status"]
    # output hygiene: a link / command / markup in a value or a message is refused (it is only ever shown as text anyway)
    unsafe = False
    for k in ("subject", "unit", "currency"):
        if chat_safety.unsafe_output(fields.get(k)):
            fields[k], unsafe = None, True
    if isinstance(fields.get("scope"), dict):
        fields["scope"] = {str(a): str(b) for a, b in fields["scope"].items()
                           if not (chat_safety.unsafe_output(str(a)) or chat_safety.unsafe_output(str(b)))}
    if isinstance(fields.get("applies_to"), list) and any(chat_safety.unsafe_output(x) for x in fields["applies_to"]):
        fields["applies_to"], unsafe = [x for x in fields["applies_to"] if not chat_safety.unsafe_output(x)], True
    message = obj["message"].strip()
    if chat_safety.unsafe_output(message) or any(chat_safety.unsafe_output(q) for q in questions):
        message, questions, unsafe = "I could not produce a safe reply. Please say again what you want to change.", [], True
        status = "CLARIFY"
    if chat_safety.claims_completion(message):
        # the chat only understands: whatever the model wrote, a "done" claim is replaced by the server's own, state-accurate sentence
        message = "I understood your request as shown below. " + chat_safety.NOT_DONE_NOTE
        notes.append({"code": "MODEL_CLAIMED_COMPLETION", "business": "The AI wrote as if the change had been made. It has not: this is only what was understood, and the sentence was replaced."})
    if unsafe:
        notes.append({"code": "MODEL_OUTPUT_UNSAFE", "business": "Part of the AI reply contained a link, command or markup and was removed."})
    if not isinstance(fields.get("scope"), dict):
        fields["scope"] = {}
    if not isinstance(fields.get("applies_to"), list):
        fields["applies_to"] = []
    new, old = _num(fields.get("new_value")), _num(fields.get("old_value"))
    if new is not None and new not in typed_vals:
        problems.append("The new value is not a number you wrote; please state it.")
        fields["new_value"], new = None, None
    if new is not None:
        sources["new_value"] = {"from": "your words", "quote": quote_for(user_text, new)}
    if old is not None and old not in typed_vals and old not in stored:
        problems.append("The current value is not one you wrote or one stored in the sources; please confirm it.")
        fields["old_value"], old = None, None
    if old is not None:
        sources["old_value"] = ({"from": "your words", "quote": quote_for(user_text, old)} if old in typed_vals
                                else {"from": "stored rule", "quote": None})
    # every understood change must be traceable to the person's words; the SERVER (not the model) decides what is supported
    haystack = _norm_text(user_text)
    changes, out_of_scope = [], [_clip(x, 200) for x in obj.get("out_of_scope", [])[:6]]
    assumptions = [_clip(a, 200) for a in obj["assumptions"][:6]]
    for ch in obj.get("changes", [])[:8]:
        kind = ch.get("kind") if ch.get("kind") in CHANGE_KINDS else "other"
        quote = _clip(str(ch.get("quote") or "").strip(), 200)
        traced = bool(quote) and _norm_text(quote) in haystack
        inferred = bool(ch.get("inferred")) or not traced
        summary = _clip(str(ch.get("summary") or ch.get("target") or "").strip(), 200)
        if chat_safety.unsafe_output(summary) or chat_safety.unsafe_output(quote):
            summary, quote, traced, inferred = "(removed: it contained a link or command)", "", False, True
        changes.append({"target": _clip(str(ch.get("target") or ""), 120), "kind": kind, "summary": summary,
                        "quote": quote if traced else "", "traced": traced, "inferred": inferred, "supported": kind == "value"})
        if kind != "value" and summary:
            out_of_scope.append(summary)
        if inferred and summary:
            assumptions.append("I assumed: " + summary + ("" if traced else " (not in your words)"))
    supported = [c for c in changes if c["supported"]]
    if len(supported) > 1:
        problems.append("You asked for more than one value change. Which one first? One request covers one change; the others need their own request.")
        if status == "READY":
            status = "CLARIFY"
    if status == "READY":
        norm = normalise_brief_form(fields)
        if norm["fields"] is None:
            problems += [f"Still needed: {p}" for p in norm["problems"]]
        elif not norm["fields"]["applies_to"]:
            problems.append("Still needed: which kinds of case this applies to.")
        if problems:
            status = "CLARIFY"
    if status == "CLARIFY" and not (questions or problems):
        problems.append("Please add the missing detail.")
    # nothing numeric to do: explain and state the limit instead of forcing a number into the form
    if status == "UNSUPPORTED" or (changes and not supported and fields.get("new_value") is None):
        status, problems = "UNSUPPORTED", []
    flags = chat_safety.scan(user_text)
    notes += flags
    out = {"status": status, "message": _clip(message, 400),
           "questions": questions + problems[: max(0, limits["max_questions"] - len(questions))],
           "fields": {k: fields[k] for k in _FIELD_KEYS}, "assumptions": list(dict.fromkeys(assumptions))[:8], "problems": problems,
           "changes": changes, "out_of_scope": list(dict.fromkeys(out_of_scope))[:8], "sources": sources,
           "safety": [{"code": n["code"], "business": n["business"]} for n in notes], "injection_flagged": bool(flags)}
    if status == "UNSUPPORTED" or out["out_of_scope"]:
        out["limits_note"] = LIMITS_TEXT
    return out


class IntentInterpretExecutor:
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
        patterns = [re.compile(p, re.I) for p in _gap_config()[0]["injection_patterns"]]
        tenant, ws = ctx.job["tenant_id"], ctx.job["workspace_id"]
        conv = chat.get(ctx.store, tenant, ws, ctx.job["payload"]["conversation_id"])
        if conv is None:
            raise NonRetryableExecutionError("conversation_not_found")
        users = [t for t in conv["turns"] if t["role"] == "user"]
        if not users:
            raise NonRetryableExecutionError("conversation_empty")
        purpose = conv.get("purpose") or "change_request"
        lim = frozen["limits"]
        shown = [{"from": "person" if t["role"] == "user" else "assistant", "text": _clip(t["text"], lim["max_user_turn_chars"])}
                 for t in conv["turns"] if t["role"] in ("user", "assistant") and t["text"]]
        convo = _canon_json(shown)
        if len(convo) > lim["max_conversation_chars"]:
            raise NonRetryableExecutionError("budget_exceeded", "conversation too long; start a new request")
        if purpose != "change_request":
            from infra.worker import free_text_purposes as ftp
            pcfg, psha = ftp.load_config()
            plim = pcfg["limits"]
            pctx, mapping = ftp.build_context(ctx.store, tenant, ws, purpose, conv.get("context"), ctx.job.get("pinned_source_revision_id"), plim)
            prompt, schema, psha = ftp.build_prompt(purpose, pctx, convo)
            if extraction._input_token_bound(prompt, schema) > config.max_input_tokens_per_call:
                raise NonRetryableExecutionError("budget_exceeded", "interpretation request exceeds input ceiling")
            ctx.request_context.update(ii_user_text=" \n".join(t["text"] for t in users), ii_purpose=purpose, ii_mapping=mapping,
                                       ii_limits=lim, ii_plimits=plim, ii_sha=psha)
            return ExecutionRequest(prompt=prompt, schema=schema, provider_calls_planned=1, external=True)
        rules = _known_rules(ctx, ctx.job.get("pinned_source_revision_id"), lim["max_known_rules"])
        prompt = (frozen["prompt_template"].replace("{rules}", "\n".join(_canon_json(r) for r in rules) or "(none stored)")
                  .replace("{conversation}", convo))
        if extraction._input_token_bound(prompt, frozen["output_schema"]) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "interpretation request exceeds input ceiling")
        ctx.request_context.update(ii_user_text=" \n".join(t["text"] for t in users), ii_rules=rules, ii_patterns=patterns,
                                   ii_limits=lim, ii_sha=sha)
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1, external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest, calls: ProviderCallBudget) -> ExecutionOutcome:
        config = ctx.config
        if ROLE not in (getattr(config, "role_model_ids", None) or {}):
            raise NonRetryableExecutionError("role_model_not_pinned", f"role_model_ids has no {ROLE!r} entry")
        model_id = model_for_role(config, ROLE)
        if extraction._input_token_bound(request.prompt, request.schema) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "interpretation request exceeds input ceiling")
        _assert_global_endpoint()
        call_config = {"max_output_tokens": ctx.max_output_tokens, "temperature": 0.0, "response_mime_type": "application/json"}
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
            raise ProviderExecutionError("provider request failed") from exc
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
        try:
            text = _field(response, "text")
        except (ValueError, IndexError, AttributeError) as exc:
            raise ProviderInvalidResponseError("provider returned no text") from exc
        try:
            obj = _model_object(text)
            rc = ctx.request_context
            if rc.get("ii_purpose"):
                from infra.worker import free_text_purposes as ftp
                ftp.check_shape(obj)
                result = ftp.resolve(rc["ii_purpose"], obj, rc["ii_user_text"], rc["ii_mapping"], rc["ii_plimits"], rc["ii_limits"]["max_questions"])
                result.update(executor_id=EXECUTOR_ID, draft_only=True, applies_nothing=True, provider_calls=1, config_sha256=rc["ii_sha"],
                              endpoint={"backend": "vertex", "location": "global", "pinned_model": model_id, "served_model": actual},
                              claim="AI interpretation of the person's words as a preview. Values were checked against what the person typed or "
                                    "what this account may read. Confirming it executes nothing.")
                result = json.loads(_canon_json(result))
                return ExecutionOutcome(result=result, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
            _check_shape(obj, rc["ii_limits"]["max_questions"])
            result = resolve(obj, rc["ii_user_text"], rc["ii_rules"], rc["ii_patterns"], rc["ii_limits"])
            result.update(executor_id=EXECUTOR_ID, draft_only=True, applies_nothing=True, provider_calls=1, config_sha256=rc["ii_sha"],
                          endpoint={"backend": "vertex", "location": "global", "pinned_model": model_id, "served_model": actual},
                          claim="AI interpretation of the person's words. Numbers were checked against what the person typed or the stored "
                                "sources. It is not a confirmed request and changes nothing.")
            result = json.loads(_canon_json(result))
            return ExecutionOutcome(result=result, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
        except NonRetryableExecutionError as exc:
            # the provider ANSWERED and the server refused the answer: the tokens it reported are what was spent, not the worst-case ceiling (found on the cloud: a refused
            # feedback reply was booked at 0.22 USD instead of about 0.01)
            if getattr(exc, "usage_outcome", None) is None:
                exc.usage_outcome = ExecutionOutcome(result={}, model_id=actual, input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)
            raise
