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

from infra.ai_kinds import AI_KINDS  # noqa: F401  (re-exported; the list lives in a module without imports so the config loader can validate role names)


class ProviderDisarmed(ConcurrencyLimitExceeded):
    """The provider is switched off on this server."""


class RoleNotEnabled(ProviderDisarmed):
    """This AI job kind is not in the server's role allowlist (`enabled_roles`), or the allowlist cannot be read. Nothing was created."""

    def __init__(self, code: str, kind: str):
        super().__init__(f"{code}: kind={kind!r}")
        self.code, self.kind = code, kind


class WindowEnded(ProviderDisarmed):
    """The recorded trial window has ended: no new AI action is accepted (the worker stops claiming at the same moment)."""


def is_disarmed() -> bool:
    """Off by the environment (a revision setting) OR by the runtime operator switch (`ai_switch`, database, no redeploy; fail closed when it cannot be read)."""
    if (os.environ.get("RIPPLE_PROVIDER_MODE") or "").strip().upper() == "LIVE_DISARMED":
        return True
    from infra import operator_flags
    return (not operator_flags.ai_on()) or operator_flags.window_over()


def role_refusal(kind: str | None) -> str | None:
    """The role allowlist decision for `kind` on this server (`infra/ai_kinds.py`), read from the budget configuration the worker uses.
    No configuration named = no provider call is possible at all (every AI executor refuses `budget_config_required`), so there is nothing
    to allow; a configuration that cannot be read refuses every AI kind."""
    from infra import ai_kinds
    if kind not in ai_kinds.MODEL_ROLES or not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return None
    try:
        from infra.worker.job_quota import load_worker_budget_config
        config = load_worker_budget_config()
    except Exception:                                    # noqa: BLE001 - fail closed on any unreadable or invalid configuration
        return ai_kinds.CONFIG_UNREADABLE
    return ai_kinds.refusal(config.enabled_roles, kind)


def enabled_kinds_for_page() -> str | None:
    """What the page may show as available: `all`, `none`, or a comma-separated list of enabled AI kinds; None when this server names no budget
    configuration (then no provider call is possible and the provider meta already tells the page). An unreadable configuration reads `none`."""
    from infra import ai_kinds
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return None
    try:
        from infra.worker.job_quota import load_worker_budget_config
        roles = load_worker_budget_config().enabled_roles
    except Exception:                                    # noqa: BLE001 - fail closed
        return "none"
    if roles is None:
        return "none"
    return ai_kinds.ALL if roles == ai_kinds.ALL else ",".join(sorted(roles))


def ai_available(kind: str) -> bool:
    """True when a new job of this AI kind could be started now: the provider is armed, the window is open and the kind is allowed."""
    return not is_disarmed() and role_refusal(kind) is None


def semantic_off() -> bool:
    """The gate of the external semantic retrieval layer: closed when the provider is off or `retrieval.semantic` is not enabled (the answer then
    uses the lexical layers, labelled as a fallback)."""
    return is_disarmed() or role_refusal("retrieval.semantic") is not None


def require_provider(kind: str) -> None:
    if kind not in AI_KINDS:
        return
    from infra import operator_flags
    if operator_flags.window_over():
        raise WindowEnded("trial window ended")          # before the generic refusal: the person is told WHY and WHEN
    if is_disarmed():
        raise ProviderDisarmed("provider disarmed")
    code = role_refusal(kind)
    if code:
        raise RoleNotEnabled(code, kind)


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
_ROLE = {
    "role_not_enabled": {
        "error": "This AI feature is not enabled on this server.",
        "next_action": "Nothing was started and nothing was charged. Saved results can still be read. The operator decides which AI features this server offers."},
    "ai_roles_not_configured": {
        "error": "No AI feature is enabled on this server: its configuration does not list any.",
        "next_action": "Nothing was started and nothing was charged. Saved results can still be read. The operator has to list the enabled AI features in the server configuration."},
    "ai_config_unreadable": {
        "error": "AI features are unavailable: the server's AI configuration cannot be read.",
        "next_action": "Nothing was started and nothing was charged. Saved results can still be read. The operator has to fix the server's AI configuration."},
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
    if isinstance(exc, RoleNotEnabled):
        out = {**_ROLE.get(exc.code, _ROLE["role_not_enabled"]), "code": exc.code, "kind": exc.kind}
        return JSONResponse(out, status_code=409, headers={"Cache-Control": "no-store"})
    body = (_WINDOW if isinstance(exc, WindowEnded) else _DISARMED if isinstance(exc, ProviderDisarmed) else _EXHAUSTED if isinstance(exc, JobCeilingExceeded)
            else _BUDGET if isinstance(exc, BudgetExceeded) else _QUOTA if isinstance(exc, PersonQuotaExceeded) else _CONCURRENCY if isinstance(exc, ConcurrencyLimitExceeded) else None)
    if body is None:
        return None
    out = dict(body)
    if isinstance(exc, JobCeilingExceeded):
        out["detail"] = str(exc)                      # the exact count the store enforced (kept for the audit trail and the technical view)
    return JSONResponse(out, status_code=409, headers={"Cache-Control": "no-store"})
