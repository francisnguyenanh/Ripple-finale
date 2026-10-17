"""Early, explained refusals for provider-backed actions.

A button that starts a model call must not look usable and then fail later inside a worker with a raw reason. Two situations are known
BEFORE anything is created, so the route refuses at once (HTTP 409, nothing created, nothing charged) with a code the page can turn into
plain English and a next action:

* `provider_disarmed`   - the server operator declared `RIPPLE_PROVIDER_MODE=LIVE_DISARMED` (live-local without an approved allocation).
* `allocation_exhausted` - the durable job ceiling (`v2_job_ceilings`) for this kind of job is used up.

Reading a stored result never goes through here.
"""
from __future__ import annotations

import os

from fastapi.responses import JSONResponse

from infra.adapters.v2_postgres import ConcurrencyLimitExceeded, JobCeilingExceeded, PersonQuotaExceeded

# Job kinds that call a model. (apply/review/simulation/impact/proposal/relation/finding are deterministic.)
AI_KINDS = frozenset({"extraction", "analysis", "change.draft", "gap.answer", "sim.analysis", "explain", "intent.interpret", "impact.judge", "knowledge.qa", "change.understand", "change.impact_deps", "change.draft_guidance", "knowledge.gap_proposals", "gap.answer_interpret", "ma.specialist", "document.variant", "knowledge.mining", "gap.interview_question"})


class ProviderDisarmed(ConcurrencyLimitExceeded):
    """The provider is switched off on this server."""


class WindowEnded(ProviderDisarmed):
    """The recorded trial window has ended: no new AI action is accepted (the worker stops claiming at the same moment)."""


def is_disarmed() -> bool:
    """Off by the environment (a revision setting) OR by the runtime operator switch (`ai_switch`, database, no redeploy; fail closed when it cannot be read)."""
    if (os.environ.get("RIPPLE_PROVIDER_MODE") or "").strip().upper() == "LIVE_DISARMED":
        return True
    from infra import operator_flags
    return (not operator_flags.ai_on()) or operator_flags.window_over()


def require_provider(kind: str) -> None:
    if kind not in AI_KINDS:
        return
    from infra import operator_flags
    if operator_flags.window_over():
        raise WindowEnded("trial window ended")          # before the generic refusal: the person is told WHY and WHEN
    if is_disarmed():
        raise ProviderDisarmed("provider disarmed")


_DISARMED = {
    "error": "AI actions are switched off on this server (by the operator).",
    "code": "provider_disarmed",
    "next_action": "Nothing was started and nothing was charged. You can keep reading saved results. To run a new AI action, the operator must "
                   "switch AI on (Support & Access) or arm the provider under an approved allocation.",
}
_WINDOW = {
    "error": "The trial window has ended: new AI actions are no longer accepted.",
    "code": "window_ended",
    "next_action": "Nothing was started and nothing was charged. You can keep reading everything saved and moving between roles. Work already running was finished or settled; "
                   "a new AI action needs a new, approved window.",
}
_EXHAUSTED = {
    "error": "The approved allocation for this kind of AI action is used up.",
    "code": "allocation_exhausted",
    "next_action": "Nothing was started and nothing was charged. A new allocation (jobs, calls, retries and cost) has to be approved before "
                   "another one can run. Saved results can still be read.",
}


_BUDGET = {
    "error": "The AI budget for this period is used up.",
    "code": "budget_exceeded",
    "next_action": "Nothing was started and nothing was charged for this request. Saved results can still be read. The operator can approve more budget; until then no new AI action can start.",
}
_QUOTA = {
    "error": "This account used its AI jobs for today.",
    "code": "person_quota",
    "next_action": "Nothing was started and nothing was charged. The allowance is per account (people who share an account share it) and resets at midnight UTC. Saved results can still be read.",
}
_CONCURRENCY = {
    "error": "Jobs are already running or waiting in this workspace up to its limit.",
    "code": "concurrency_limit",
    "next_action": "Nothing was lost. Wait for a running job to finish (the page updates by itself) and start yours again.",
}


def refusal_for(exc: Exception) -> JSONResponse | None:
    """The explained 409 for the known cases (switched off, allocation used up, budget used up, concurrency limit); None for anything else (the caller keeps its own mapping)."""
    from infra.worker.budget_config import BudgetExceeded
    body = (_WINDOW if isinstance(exc, WindowEnded) else _DISARMED if isinstance(exc, ProviderDisarmed) else _EXHAUSTED if isinstance(exc, JobCeilingExceeded)
            else _BUDGET if isinstance(exc, BudgetExceeded) else _QUOTA if isinstance(exc, PersonQuotaExceeded) else _CONCURRENCY if isinstance(exc, ConcurrencyLimitExceeded) else None)
    if body is None:
        return None
    out = dict(body)
    if isinstance(exc, JobCeilingExceeded):
        out["detail"] = str(exc)                      # the exact count the store enforced (kept for the audit trail and the technical view)
    return JSONResponse(out, status_code=409, headers={"Cache-Control": "no-store"})
