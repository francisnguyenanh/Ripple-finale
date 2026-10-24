"""What a provider response that did NOT finish normally tells us, kept instead of thrown away (live B6 failure, 2026-10-09).

Before this module every call site raised `ProviderInvalidResponseError("provider response did not finish normally")` and dropped the finish reason,
the usage and the block/safety information, so a truncated draft looked like any other failure, was retried with the same settings, and was charged
at the worst-case estimate although the provider had reported its usage.

`require_normal_finish` is the one place that decides, from the response alone:
  * STOP                                   -> returns, the caller goes on.
  * MAX_TOKENS                             -> NON-retryable `provider_output_truncated`: the same request with the same limits ends the same way
                                              (temperature 0), so a retry would only spend another call. A person retries after the limit is changed.
  * SAFETY / RECITATION / BLOCKLIST / ...  -> NON-retryable `provider_response_blocked` (deterministic for the same input).
  * prompt blocked (no candidate + a block reason) -> NON-retryable `provider_prompt_blocked`.
  * anything else (OTHER, unspecified, no candidate without a reason) -> retryable `ProviderInvalidResponseError`, bounded by `max_retries`.
Every raised error carries `.diagnostics` (safe metadata only: no prompt, no source text, no credentials) and `.usage_outcome` (the tokens the
provider reported) so the worker books the real usage and stores the diagnostics on the attempt row.

The phrase "did not finish normally" is kept inside the detail because older tooling matches on it.
"""
from __future__ import annotations

from infra.worker.execution_types import ExecutionOutcome, NonRetryableExecutionError, ProviderInvalidResponseError

REASON_TRUNCATED = "provider_output_truncated"
REASON_BLOCKED = "provider_response_blocked"
REASON_PROMPT_BLOCKED = "provider_prompt_blocked"

_BLOCKED_FINISH = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
                             "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"})
_CLIP = 200


def _get(obj, name):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _name(value) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value))[:48]


def _int(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def usage_numbers(response) -> dict:
    """The token counts the provider reported (None = not reported). Never raises."""
    u = _get(response, "usage_metadata")
    return {"input_tokens": _int(_get(u, "prompt_token_count")), "output_tokens": _int(_get(u, "candidates_token_count")),
            "thought_tokens": _int(_get(u, "thoughts_token_count")), "total_tokens": _int(_get(u, "total_token_count")),
            "cached_input_tokens": _int(_get(u, "cached_content_token_count"))}


def usage_outcome(response, model_id: str | None) -> ExecutionOutcome:
    """The usage of a response as an `ExecutionOutcome`, so a failed call is priced on what the provider reported.
    total - input is output plus thinking; a side the provider left out is derived from it rather than priced at the ceiling."""
    n = usage_numbers(response)
    in_t, out_t, th_t, total = n["input_tokens"], n["output_tokens"], n["thought_tokens"], n["total_tokens"]
    if in_t is not None and total is not None and total >= in_t:
        generated = total - in_t
        if out_t is None:
            out_t = max(generated - (th_t or 0), 0)
        th_t = max(th_t or 0, generated - out_t)
    served = _get(response, "model_version")
    return ExecutionOutcome(result={}, model_id=served if isinstance(served, str) else model_id,
                            input_tokens=in_t, output_tokens=out_t, thought_tokens=th_t)


def diagnostics(response, *, model_id: str | None = None, requested_cap: int | None = None, stage: str | None = None) -> dict:
    """Safe, bounded metadata about one response. No prompt, no response text, no source content."""
    cands = _get(response, "candidates") or []
    cand = cands[0] if cands else None
    ratings = []
    for r in (_get(cand, "safety_ratings") or [])[:12]:
        blocked, prob = _get(r, "blocked"), _name(_get(r, "probability"))
        if blocked or prob not in (None, "NEGLIGIBLE", "LOW"):
            ratings.append({"category": _name(_get(r, "category")), "probability": prob, "blocked": bool(blocked)})
    feedback = _get(response, "prompt_feedback")
    text_len = None
    try:
        t = _get(response, "text")
        text_len = len(t) if isinstance(t, str) else None
    except Exception:                                   # the SDK raises when there is no text part
        text_len = None
    msg = _get(cand, "finish_message")
    out = {"stage": stage, "finish_reason": _name(_get(cand, "finish_reason")), "candidates": len(cands),
           "finish_message": msg[:_CLIP] if isinstance(msg, str) else None,
           "block_reason": _name(_get(feedback, "block_reason")),
           "safety_flagged": ratings, "usage": {k: v for k, v in usage_numbers(response).items() if v is not None},
           "response_id": (_get(response, "response_id") if isinstance(_get(response, "response_id"), str) else None),
           "model_version": (_get(response, "model_version") if isinstance(_get(response, "model_version"), str) else None),
           "pinned_model": model_id, "requested_output_cap": requested_cap, "text_chars": text_len}
    from infra.worker.safe_diagnostics import safe_diagnostics
    kept = safe_diagnostics({k: v for k, v in out.items() if v not in (None, [], {})})     # AH-05: finish_message is model / provider text: safe form or length only
    return kept | {"finish_reason": kept.get("finish_reason")}


def _attach(exc: BaseException, response, model_id, requested_cap, stage) -> BaseException:
    exc.diagnostics = diagnostics(response, model_id=model_id, requested_cap=requested_cap, stage=stage)
    exc.usage_outcome = usage_outcome(response, model_id)
    return exc


def require_normal_finish(response, *, model_id: str | None = None, requested_cap: int | None = None) -> None:
    """Raise the right error when the first candidate did not end with STOP; return silently otherwise."""
    cands = _get(response, "candidates")
    if not cands:
        reason = _name(_get(_get(response, "prompt_feedback"), "block_reason"))
        if reason:
            raise _attach(NonRetryableExecutionError(REASON_PROMPT_BLOCKED, f"the provider blocked the request (block_reason={reason})"),
                          response, model_id, requested_cap, "no_candidate")
        raise _attach(ProviderInvalidResponseError("provider returned no candidate"), response, model_id, requested_cap, "no_candidate")
    finish = _name(_get(cands[0], "finish_reason"))
    if finish == "STOP":
        return
    base = f"provider response did not finish normally (finish_reason={finish})"
    if finish == "MAX_TOKENS":
        cap = f" of {requested_cap} tokens" if requested_cap else ""
        raise _attach(NonRetryableExecutionError(REASON_TRUNCATED, f"{base}: output plus thinking reached the request cap{cap}; the answer was cut off and was not used"),
                      response, model_id, requested_cap, "finish")
    if finish in _BLOCKED_FINISH:
        raise _attach(NonRetryableExecutionError(REASON_BLOCKED, f"{base}: the provider withheld the answer"), response, model_id, requested_cap, "finish")
    raise _attach(ProviderInvalidResponseError(base), response, model_id, requested_cap, "finish")


def attach_after_answer(exc: BaseException, response, *, model_id: str | None, requested_cap: int | None, stage: str) -> BaseException:
    """For a failure AFTER the provider answered (empty text, bad JSON, rejected by validation): keep the usage and the metadata on the error."""
    if getattr(exc, "usage_outcome", None) is None:
        _attach(exc, response, model_id, requested_cap, stage)
    return exc
