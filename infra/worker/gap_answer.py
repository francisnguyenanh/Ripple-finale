"""Job kind "gap.answer" -- answer ONE question from a pinned, authorized artifact list,
or abstain. Modelled on `extraction.ExtractRulesExecutor` (same reviewed boundary: registered executor, one provider
call per attempt, `_client_for_model`, `_resolve_evidence`); nothing here writes knowledge, candidates or decisions.

Honesty rules (all deterministic, applied AFTER the provider returns):
* Scope is the payload's `artifact_ids` (validated against the job's pinned revision; no free-text scope). The WHOLE
  scope is put in the prompt; a corpus over the input ceiling, or an artifact with more than `_MAX_CONTEXT_CHUNKS`
  chunks, is `budget_exceeded` with zero provider calls -- never silently truncated.
* Every span is resolved by `_resolve_evidence` against the observations the model was shown; a fabricated or
  out-of-scope quote is dropped. `answered` needs >= 1 resolved, non-injected span containing every token of the
  answer; otherwise the result is DOWNGRADED to `abstained` (a cross-check may only downgrade, never upgrade).
* Yes/no answers need a same-span polarity match (negation cue, question words and numbers in the span).
* Value containment is normalised (numbers, separators, scale, currency, plurals) but per span and exact on numbers.
* `states_absence` survives only for an abstention whose `scope_examined` equals the full authorized list and
  whose sources carry no flagged injection text (F-11; reason recorded in `absence_downgrade_reason`).
* A malformed model object (unknown key, bad enum, abstained without question or with an answer) fails the job.
* Sources are sent as an untrusted-data block; text matching the frozen injection patterns is flagged and can never
  be the sole ground of an answer.
The frozen prompt/schema/patterns live in `config/ai10_gap_answer.json`; its sha256 is stamped into every result."""
from __future__ import annotations

from infra.worker.provider_response import require_normal_finish

import hashlib
import json
import math
import pathlib
import re
import unicodedata
from decimal import Decimal

from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import extraction
from infra.worker.execution_types import (is_timeout_exception, 
    ExecutionContext, ExecutionOutcome, ExecutionRequest, NonRetryableExecutionError,
    ProviderCallBudget, ProviderExecutionError, ProviderInvalidResponseError,
    ProviderRateLimitError, ProviderTimeoutError,
)
from infra.worker.live_analysis import generate_and_close, _client_for_model, _field, _token_count

CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "ai10_gap_answer.json"
_MODEL_KEYS = {"status", "answer", "states_absence", "evidence", "question_for_human", "scope_examined"}
_TOKEN = re.compile(r"[a-z0-9]+")


def load_frozen_config(path=None) -> tuple[dict, str]:
    raw = pathlib.Path(path or CONFIG_PATH).read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def validate_payload(payload) -> str | None:
    """Shared by the route and the executor: an error code, or None when the payload shape is valid."""
    if not isinstance(payload, dict):
        return "payload_not_an_object"
    for key in ("question", "question_id"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return f"{key}_required"
    ids = payload.get("artifact_ids")
    if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids)):
        return "artifact_ids_required"
    if payload.get("intent_id") is not None and (not isinstance(payload["intent_id"], str) or not payload["intent_id"]):
        return "intent_id_invalid"
    return None


_SYMBOLS = {"¥": " jpy ", "￥": " jpy ", "円": " jpy ", "$": " usd ", "€": " eur ", "%": " percent ",
            "％": " percent "}
_SYNONYM = {"yen": "jpy", "dollar": "usd", "euro": "eur", "pct": "percent", "per cent": "percent"}
_STOP = frozenset({"a", "an", "the", "of", "for", "in", "on", "at", "to", "by", "be", "is", "are", "was", "were", "it",
                   "its", "this", "that", "and", "or", "as", "with", "from", "per"})
_SCALE = {"k": 10 ** 3, "thousand": 10 ** 3, "m": 10 ** 6, "million": 10 ** 6, "万": 10 ** 4, "千": 10 ** 3,
          "億": 10 ** 8}
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:[,  ]\d{3})+|\d+)(?:\.(\d+))?(?:\s?(k|m|thousand|million|万|千|億)(?![a-z]))?")
_WORD = re.compile(r"[^\W\d_]+")


def _canon(text) -> tuple[frozenset, frozenset]:
    """(numbers, words) of a text, normalised: NFKC (full-width digits), case, thousand separators, scale suffixes
    (750k, 1.5 million), currency symbols and a few unit synonyms, plural stripped, stopwords dropped. Numbers are
    canonical Decimal strings, so 750,000 == 750000 == 750k but 30 != 3."""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    for k, v in _SYMBOLS.items():
        t = t.replace(k, v)
    nums = set()

    def take(m):
        whole = re.sub(r"[,  ]", "", m.group(1))
        d = Decimal(whole + ("." + m.group(2) if m.group(2) else ""))
        if m.group(3):
            d *= _SCALE[m.group(3)]
        nums.add(format(d.normalize(), "f"))
        return " "
    rest = _NUMBER.sub(take, t)
    words = set()
    for w in _WORD.findall(rest):
        w = _SYNONYM.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = _SYNONYM.get(w[:-1], w[:-1])
        if w not in _STOP:
            words.add(w)
    return frozenset(nums), frozenset(words)


def _contains_value(answer: str, quote: str) -> bool:
    """Verified containment within ONE span: every normalised number and every content word of the answer is
    supported by the quote. A missing number, a different number (30 vs 3), or an unsupported unit/word fails."""
    an, aw = _canon(answer)
    qn, qw = _canon(quote)
    return bool(an or aw) and an <= qn and aw <= qw


_NEG_ANSWER = frozenset({"no", "none", "not", "never", "nil", "nothing", "neither", "without", "false", "nope",
                         "cannot", "n"})
_POS_ANSWER = frozenset({"yes", "true", "correct", "yep", "affirmative"})
_NEG_CUE = re.compile(r"\b(no|not|none|never|nothing|neither|nor|without|cannot|can't|cant|isn't|aren't|wasn't|"
                      r"weren't|doesn't|don't|didn't|won't|shouldn't|mustn't|hasn't|haven't|no longer)\b")
_QWORDS = frozenset({"what", "which", "who", "whom", "when", "where", "how", "why", "does", "do", "did", "is", "are",
                     "was", "were", "can", "could", "there", "any", "exist", "exists", "have", "has", "had", "should",
                     "must", "will", "would", "shall", "may", "allowed", "permitted", "required", "need", "needed"})


def _polarity(answer: str):
    toks = set(re.findall(r"[a-z]+", unicodedata.normalize("NFKC", answer).lower().replace("’", "'")))
    toks |= {"n"} if "n/a" in answer.lower() else set()
    neg, pos = toks & _NEG_ANSWER, toks & _POS_ANSWER
    if neg and not pos:
        return "neg", toks - _NEG_ANSWER
    if pos and not neg:
        return "pos", toks - _POS_ANSWER
    return None, toks


def _polarity_supported(answer: str, quote: str, question: str) -> bool:
    """Yes/no-style answers have no value token to find in the span, so the SPAN must carry the polarity itself:
    the same quote covers the question's content words and numbers, and its negation cue matches the answer's
    polarity (negative answer needs an explicit cue; positive answer needs none). Residual answer words and
    numbers must also be supported. Per span, so a negation in a different span never counts."""
    pol, rest = _polarity(answer)
    if pol is None:
        return False
    qn, qw = _canon(question)
    qw = frozenset(w for w in qw if w not in _QWORDS)
    sn, sw = _canon(quote)
    if not qw or len(qw & sw) < min(2, len(qw)) or not qn <= sn:
        return False
    cue = bool(_NEG_CUE.search(unicodedata.normalize("NFKC", quote).lower().replace("’", "'")))
    if (pol == "neg") != cue:
        return False
    an, aw = _canon(" ".join(sorted(rest - _STOP)))
    return an <= sn and aw <= sw


def _model_object(text) -> dict:
    try:
        obj = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ProviderInvalidResponseError("provider returned invalid JSON") from exc
    if not isinstance(obj, dict):
        raise ProviderInvalidResponseError("provider returned invalid analysis")
    return obj


def _check_shape(obj: dict) -> None:
    def bad(why):
        raise NonRetryableExecutionError("schema_violation", why)
    if set(obj) != _MODEL_KEYS:
        bad("keys")
    if obj["status"] not in ("answered", "abstained"):
        bad("status")
    if not (obj["answer"] is None or isinstance(obj["answer"], str)):
        bad("answer")
    if not isinstance(obj["states_absence"], bool):
        bad("states_absence")
    if not (obj["question_for_human"] is None or isinstance(obj["question_for_human"], str)):
        bad("question_for_human")
    if not isinstance(obj["scope_examined"], list) or any(not isinstance(x, str) for x in obj["scope_examined"]):
        bad("scope_examined")
    if not isinstance(obj["evidence"], list):
        bad("evidence")
    for e in obj["evidence"]:
        if (not isinstance(e, dict) or set(e) != {"artifact_path", "quote"}
                or not all(isinstance(v, str) for v in e.values())):
            bad("evidence_item")
    if obj["status"] == "abstained":
        if obj["answer"] not in (None, ""):
            bad("abstained_with_answer")
        if not (obj["question_for_human"] or "").strip():
            bad("abstained_without_question")
    elif not (obj["answer"] or "").strip():
        bad("answered_without_answer")


def _clip(text, n):
    return text if not isinstance(text, str) or len(text) <= n else text[:n] + "..."


class GapAnswerExecutor:
    external = True
    executor_id = "gap.answer.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        config = ctx.config
        if config is None or not ctx.max_output_tokens:
            raise NonRetryableExecutionError("budget_config_required")
        payload = ctx.job["payload"]
        err = validate_payload(payload)
        if err:
            raise NonRetryableExecutionError(err)
        frozen, sha = load_frozen_config()
        tenant, ws, rev = ctx.job["tenant_id"], ctx.job["workspace_id"], ctx.job["pinned_source_revision_id"]
        try:
            view = ctx.store.source_revision_view(tenant, ws, rev)
            all_obs = ctx.store.source_observations(tenant, ws, rev)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("source_artifact_unavailable") from exc
        by_id = {a["artifact_id"]: a for a in view["artifacts"]}
        if any(i not in by_id for i in payload["artifact_ids"]):
            raise NonRetryableExecutionError("source_artifact_unavailable", "artifact outside the pinned revision")
        limit = extraction._MAX_CONTEXT_CHUNKS
        blocks, shown, paths, texts = [], {}, {}, []
        for aid in sorted(payload["artifact_ids"], key=lambda i: by_id[i]["logical_path"] or i):
            path = by_id[aid]["logical_path"] or aid
            obs = sorted((o for o in all_obs if o["artifact_id"] == aid
                          and extraction._observation_text(o) is not None),
                         key=lambda o: o["observation_id"])
            if len(obs) > limit:
                raise NonRetryableExecutionError("budget_exceeded", f"{path}: {len(obs)} chunks exceed {limit}")
            if obs:
                body = "\n\n".join(f"[chunk {i}] {extraction._observation_text(o)}" for i, o in enumerate(obs))
                shown[aid] = obs
            else:
                try:
                    body = ctx.store.read_blob(tenant, ws, rev, aid).decode("utf-8")
                except (KeyError, FileNotFoundError, UnicodeDecodeError, ValueError, TenantScopeError,
                        NotImplementedError) as exc:
                    raise NonRetryableExecutionError("source_artifact_unavailable") from exc
                shown[aid] = None
            paths[path] = aid
            texts.append(body)
            blocks.append(f'<artifact path="{path}">\n{body}\n</artifact>')
        if not any(t.strip() for t in texts):
            raise NonRetryableExecutionError("source_artifact_empty")
        patterns = [re.compile(p, re.I) for p in frozen["injection_patterns"]]
        from infra.worker import gap_context
        given = gap_context.build(ctx.store, tenant, ws, intent_id=payload.get("intent_id"), artifact_ids=payload["artifact_ids"],
                                  paths_by_id={a: pth for pth, a in paths.items()}, max_epoch=ctx.job.get("pinned_decision_epoch"), patterns=patterns)
        ctx.request_context["gap_decisions"] = given
        prompt = (frozen["prompt_template"].replace("{question_id}", payload["question_id"])
                  .replace("{question}", payload["question"] + given["block"]).replace("{sources}", "\n".join(blocks)))
        if math.ceil(len(prompt.encode("utf-8")) / 2) > config.max_input_tokens_per_call:
            raise NonRetryableExecutionError("budget_exceeded", "source prompt exceeds input ceiling")
        ctx.request_context.update(
            gap_shown=shown, gap_paths=paths, gap_patterns=patterns, gap_config=frozen, gap_config_sha=sha,
            gap_flagged=any(p.search(t) for t in texts for p in patterns))
        return ExecutionRequest(prompt=prompt, schema=frozen["output_schema"], provider_calls_planned=1,
                                external=True)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
                calls: ProviderCallBudget) -> ExecutionOutcome:
        config = ctx.config
        from infra.worker.budget_config import model_for_role
        model_id = model_for_role(config, "gap.answer")
        pinned = "gap.answer" in (getattr(config, "role_model_ids", None) or {})
        if pinned:                      # a pinned role: Vertex GLOBAL endpoint only, served by exactly that model, no fallback
            from infra.worker.change_draft import _assert_global_endpoint
            _assert_global_endpoint()
        calls.consume()
        try:
            client = _client_for_model(model_id)
            response = generate_and_close(client,
                model=model_id, contents=request.prompt,
                config={"max_output_tokens": ctx.max_output_tokens, "temperature": 0.0,
                        "response_mime_type": "application/json"})
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if callable(code):
                code = code()
            if code == 429:
                raise ProviderRateLimitError("provider rate limited") from exc
            if is_timeout_exception(exc, code):
                raise ProviderTimeoutError("provider timed out") from exc
            raise ProviderExecutionError("provider request failed") from exc
        actual_model = _field(response, "model_version") or model_id
        if pinned and actual_model != model_id:
            raise NonRetryableExecutionError("served_model_differs_from_pinned_role_model", f"{actual_model}")
        if not isinstance(actual_model, str) or actual_model not in config.model_prices:
            raise NonRetryableExecutionError("unpriced_model")
        require_normal_finish(response, model_id=model_id, requested_cap=getattr(ctx, "max_output_tokens", None))
        usage = _field(response, "usage_metadata")
        input_tokens = _token_count(usage, "prompt_token_count")
        output_tokens = _token_count(usage, "candidates_token_count")
        thought_tokens = _token_count(usage, "thoughts_token_count")
        total = _token_count(usage, "total_token_count")
        if total is not None and input_tokens is not None and output_tokens is not None:
            thought_tokens = max(thought_tokens or 0, total - input_tokens - output_tokens)
        try:
            text = _field(response, "text")
        except (ValueError, IndexError, AttributeError) as exc:
            raise ProviderInvalidResponseError("provider returned no text") from exc
        obj = _model_object(text)
        _check_shape(obj)
        result = self._validate(ctx, obj)
        return ExecutionOutcome(result=result, model_id=actual_model, input_tokens=input_tokens,
                                output_tokens=output_tokens, thought_tokens=thought_tokens)

    def _validate(self, ctx: ExecutionContext, obj: dict) -> dict:
        rc = ctx.request_context
        rev, payload = ctx.job["pinned_source_revision_id"], ctx.job["payload"]
        paths: dict = rc["gap_paths"]                       # authorized logical path -> artifact_id
        auth_ids = set(paths.values())
        spans, rejected, by_artifact = [], [], {}
        for e in obj["evidence"]:
            key = e["artifact_path"]
            aid = paths.get(key) or (key if key in auth_ids else None)
            if aid is None:
                rejected.append({"artifact_path": key, "reason": "outside_authorized_scope"})
                continue
            by_artifact.setdefault(aid, []).append(e["quote"])
        path_of = {v: k for k, v in paths.items()}
        for aid, quotes in by_artifact.items():
            ctx.request_context["bounded_observations"] = rc["gap_shown"].get(aid)
            resolved, bad = extraction._resolve_evidence(ctx, source_revision_id=rev, artifact_id=aid, quotes=quotes)
            rejected += [{"artifact_path": path_of[aid], "reason": "quote_not_found", "quote": q} for q in bad]
            spans += [{"artifact_id": aid, "artifact_path": path_of[aid], "source_revision_id": rev,
                       "locator": r.locator, "quote": r.quote} for r in resolved]
        flagged = [s for s in spans if any(p.search(s["quote"]) for p in rc["gap_patterns"])]
        grounds = [s for s in spans if s not in flagged]
        status, answer, absence = obj["status"], obj["answer"], obj["states_absence"]
        downgraded = None
        absence_downgrade = None
        if status == "answered":
            if absence:
                downgraded = "answered_and_states_absence"
            elif not grounds:
                downgraded = "no_verified_non_injected_span"
            elif not any(_contains_value(answer, s["quote"])
                         or _polarity_supported(answer, s["quote"], payload["question"]) for s in grounds):
                downgraded = "span_does_not_contain_value"
        examined = {paths.get(p, p) for p in obj["scope_examined"]}
        scope_ok = examined == auth_ids
        if downgraded:
            status, answer, absence, spans = "abstained", None, False, []
            question = rc["gap_config"]["downgrade_question"]
        else:
            question = obj["question_for_human"] if status == "abstained" else None
            if status == "abstained":
                # An injected source can steer an absence claim; validators only ever downgrade.
                if absence and (rc["gap_flagged"] or flagged):
                    absence_downgrade = "injection_flagged"
                if absence and absence_downgrade is None and not scope_ok:
                    absence_downgrade = "scope_incomplete"
                absence = bool(absence and absence_downgrade is None)
            else:
                spans = grounds
        failures = ([downgraded] if downgraded else []) + ([f"absence:{absence_downgrade}"] if absence_downgrade else [])
        failures += [f"span_rejected:{x['reason']}" for x in rejected]
        failures += ["span_flagged_injection"] * len(flagged)
        pre = None
        if failures:
            # Diagnostic ONLY (bounded): what the model said before validation. Never the product's answer; the
            # product answer is `answer`/`status` above, and this key is named so no reader mistakes it for one.
            pre = {"NOT_THE_ANSWER": True, "status": obj["status"], "answer": _clip(obj["answer"], 300),
                   "states_absence": obj["states_absence"],
                   "evidence": [{"artifact_path": _clip(e["artifact_path"], 200), "quote": _clip(e["quote"], 400)}
                                for e in obj["evidence"][:8]],
                   "scope_examined": [_clip(x, 200) for x in obj["scope_examined"][:50]],
                   "validator_failures": failures[:20]}
        keep = ("artifact_id", "artifact_path", "source_revision_id", "locator", "quote")
        return {"status": status, "answer": answer, "states_absence": absence,
                "evidence_spans": [{k: s[k] for k in keep} for s in spans],
                "question_for_human": question, "scope_examined": list(obj["scope_examined"]),
                "injection_flagged": bool(rc["gap_flagged"] or flagged),
                "model_output_pre_validation": pre, "downgraded_reason": downgraded, "absence_downgrade_reason": absence_downgrade, "rejected_spans": rejected,
                "decisions_given": rc["gap_decisions"]["items"], "decisions_skipped": rc["gap_decisions"]["skipped"],
                "question_id": payload["question_id"], "executor_id": self.executor_id,
                "config_sha256": rc["gap_config_sha"], "live_call_provenance": "ripple_runtime"}
