"""The job kinds that call a model, and the server's role allowlist decision (no I/O, no imports from the rest of `infra`).

`enabled_roles` in the budget configuration is the explicit, server-side list of AI job kinds this server may run. It is fail-closed:
a configuration without the key enables no AI kind, an unknown kind makes the configuration invalid, and only the literal `all` enables
every kind (meant for development, tests and evaluation; a production configuration lists its kinds). The list is checked when a person
asks (the route refuses before anything is created) and again when the worker is about to run the job (refused before any provider
reservation or call), so a kind removed between the two never reaches the provider. The global AI switch, the trial window, permissions,
the model-data policy, quotas and caps are unchanged and still apply; the allowlist can only remove capability, never add it.
"""
from __future__ import annotations

# Job kinds that call a model. (apply/review/simulation/impact/proposal/relation/finding are deterministic.)
AI_KINDS = frozenset({"extraction", "analysis", "change.draft", "gap.answer", "sim.analysis", "explain", "intent.interpret", "impact.judge", "knowledge.qa",
                      "change.understand", "change.impact_deps", "change.draft_guidance", "knowledge.gap_proposals", "gap.answer_interpret", "ma.specialist",
                      "document.variant", "knowledge.mining", "gap.interview_question", "change.risk_memo", "ma.synthesis_memo", "ci.semantic_drift",
                      "source.correspondence", "code.diff_proposal", "roi.analyst", "request.qa"})
# Model-calling roles that are not job kinds of their own: the external semantic layer of retrieval runs inside a `knowledge.qa` job.
NON_JOB_ROLES = frozenset({"retrieval.semantic"})
MODEL_ROLES = AI_KINDS | NON_JOB_ROLES

ALL = "all"
NOT_CONFIGURED = "ai_roles_not_configured"     # the budget configuration has no `enabled_roles`
NOT_ENABLED = "role_not_enabled"               # the kind is not in `enabled_roles`
CONFIG_UNREADABLE = "ai_config_unreadable"     # the budget configuration named by the server cannot be read or is invalid


def refusal(enabled_roles, kind: str | None) -> str | None:
    """None when `kind` may run under `enabled_roles` (None = key absent, `ALL`, or a frozenset of kinds); otherwise the reason code.
    A kind that does not call a model is never refused here."""
    if kind not in MODEL_ROLES:
        return None
    if enabled_roles is None:
        return NOT_CONFIGURED
    if enabled_roles == ALL or kind in enabled_roles:
        return None
    return NOT_ENABLED


def validate(raw, errors: list):
    """The `enabled_roles` value of a raw configuration -> None (absent), `ALL`, or a frozenset; problems are appended to `errors`."""
    if raw is None:
        return None
    if raw == ALL:
        return ALL
    if not isinstance(raw, list) or not raw or not all(isinstance(k, str) and k for k in raw):
        errors.append(f"enabled_roles: must be the string 'all' or a non-empty list of AI job kinds, got {raw!r}")
        return None
    if len(set(raw)) != len(raw):
        errors.append("enabled_roles: a kind is listed twice")
    unknown = sorted(set(raw) - MODEL_ROLES)
    if unknown:
        errors.append(f"enabled_roles: not AI job kinds or model roles: {unknown}")
    return frozenset(raw)
