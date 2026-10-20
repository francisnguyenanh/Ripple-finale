"""AI use is a standing, server-side permission of a member - not a grant per reply and not a second person's click.

A person who holds `ai.use` (role bundle; removed per member by the `ai_suspended` marker role) may start the AI tasks that only read,
analyse or draft for them: chat / clarification, change draft and revision, knowledge Q&A and GAP answers, simulation readings and
explanations. The job is then approved by POLICY (`approved_by = policy:ai.use`, one audit event) instead of by another person.

What stays exactly as before and is NOT decided here: RBAC and workspace isolation (the caller's own membership is read on every call),
the provider gate (armed/disarmed), the model pin and data policy, per-job/token/call limits, the cost reservation and hard cap in the
ledger, prompt-injection and citation checks, and every business decision (confirm knowledge, approve, apply).

A sandbox workspace (the public judge trial) keeps the second-person hold: the policy never auto-approves there.
"""

from __future__ import annotations

from psycopg2.extras import Json

from infra.adapters.v2_policy import permissions_for

POLICY_APPROVER = "policy:ai.use"
POLICY_ROLES = ("ai.use",)
# What a person may start for themselves. Extraction/analysis of source files stays under the import flow; nothing here approves, applies or publishes.
SELF_SERVE_KINDS = ("intent.interpret", "change.draft", "gap.answer", "explain", "sim.analysis", "impact.judge", "knowledge.qa", "change.understand", "change.impact_deps", "change.draft_guidance", "knowledge.gap_proposals", "gap.answer_interpret", "ma.specialist", "document.variant", "knowledge.mining", "gap.interview_question", "change.risk_memo", "ma.synthesis_memo", "ci.semantic_drift", "source.correspondence", "code.diff_proposal", "roi.analyst", "request.qa")


# A kind with its own scoped capability (RQA-03): `request.qa` is admitted by `request.assistant.ask` only; `ai.use` does not admit it and it admits nothing else.
KIND_CAPABILITY = {"request.qa": "request.assistant.ask"}


def capability_for(kind: str | None) -> str:
    return KIND_CAPABILITY.get(kind or "", "ai.use")


def block_reason(store, tenant: str, ws: str, authority: dict, kind: str | None = None) -> str | None:
    """Why this person may not use the AI right now (for `kind`), or None. Read from the server-side membership, never from the request."""
    roles = authority.get("roles") or ()
    if "ai_suspended" in roles:
        return "suspended"
    if capability_for(kind) not in permissions_for(roles):
        return "no_permission"
    from infra.worker.sandbox_hold import is_sandbox
    if is_sandbox(store, tenant, ws):
        return "sandbox_hold"
    return None


def admit(store, tenant: str, ws: str, job_id: str, kind: str, authority: dict) -> bool:
    """Approve a just-created PENDING_APPROVAL self-serve job by policy. False = left for a person (not allowed to self-serve here)."""
    if kind not in SELF_SERVE_KINDS or block_reason(store, tenant, ws, authority, kind) is not None:
        return False
    cap = capability_for(kind)
    store.approve_job(tenant, ws, job_id, f"policy:{cap}", (cap,))
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
                       VALUES (%s,%s,'AI_JOB_ADMITTED_BY_POLICY',%s,%s)""",
                    (tenant, ws, authority["account_id"], Json({"job_id": job_id, "kind": kind, "policy": cap})))
    return True
