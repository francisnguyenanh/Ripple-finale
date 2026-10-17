"""Server-side record of "this request replaced that request" and of the work that became out of date because of it.

When a person changes a confirmed request, the chat confirms a NEW intent. Everything done for the old one (AI draft, impact,
simulation, proposal, review, approval, apply) is not reused. That relation is written once, as an audit event, when the new
intent is saved; reading it later (a reload, the back button, another person opening the link) needs no page memory. The
downstream work is DERIVED from the jobs' own payloads (which job points at which), so the list can never name a job that does
not exist. Nothing here approves, cancels or deletes anything: an old job stays as it was, and is only reported as out of date.
"""
from __future__ import annotations

from psycopg2.extras import Json

EVENT = "INTENT_SUPERSEDED"
_CHAIN = ("change.draft", "impact", "simulation", "proposal", "review", "approval", "apply")


def downstream_of_intent(store, tenant: str, ws: str, intent_id: str) -> list[dict]:
    """Jobs that belong to `intent_id`: drafts naming it, then everything that points at them, in kind order."""
    with store._tx() as cur:
        cur.execute("SELECT job_id, kind, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind = ANY(%s) ORDER BY job_id",
                    (tenant, ws, list(_CHAIN)))
        rows = [(r[0], r[1], r[2] if isinstance(r[2], dict) else {}) for r in cur.fetchall()]
    mine: dict[str, str] = {}
    for jid, kind, p in rows:
        if kind in ("change.draft", "proposal") and p.get("intent_id") == intent_id:
            mine[jid] = kind
    changed = True
    while changed:                       # a job belongs when it points at one that belongs (bounded by the job count)
        changed = False
        for jid, kind, p in rows:
            if jid in mine:
                continue
            refs = {p.get(k) for k in ("draft_job_id", "proposal_job_id", "review_job_id", "approval_job_id", "impact_job_id", "simulation_job_id")}
            if refs & set(mine):
                mine[jid] = kind
                changed = True
    # impact / simulation have no pointer to the intent: they are the ones a belonging proposal names
    for jid, kind, p in rows:
        if kind == "proposal" and jid in mine:
            for k in ("impact_job_id", "simulation_job_id"):
                ref = p.get(k)
                if isinstance(ref, str) and ref not in mine:
                    mine[ref] = "impact" if k.startswith("impact") else "simulation"
    order = {k: i for i, k in enumerate(_CHAIN)}
    return sorted(({"kind": k, "job_id": j} for j, k in mine.items()), key=lambda x: (order.get(x["kind"], 99), x["job_id"]))


def record_supersede(store, tenant: str, ws: str, conversation_id: str, old_intent: str, new_intent: str, actor: str) -> dict:
    stale = downstream_of_intent(store, tenant, ws, old_intent)
    payload = {"conversation_id": conversation_id, "old_intent_id": old_intent, "new_intent_id": new_intent,
               "stale_jobs": stale, "applied": any(s["kind"] == "apply" for s in stale)}
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                    (tenant, ws, EVENT, actor, Json(payload)))
    return payload


def lineage(store, tenant: str, ws: str, intent_id: str) -> dict:
    """What replaced this request, what it replaced, and which downstream work is out of date. Read-only."""
    with store._tx() as cur:
        cur.execute("SELECT actor_id, payload, created_at FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type=%s ORDER BY id",
                    (tenant, ws, EVENT))
        events = [(r[0], r[1] if isinstance(r[1], dict) else {}, r[2]) for r in cur.fetchall()]
    out = {"intent_id": intent_id, "superseded_by": None, "supersedes": None, "conversation_id": None, "stale_jobs": [], "applied": False, "replaced_work": [], "replaced_applied": False}
    for actor, p, at in events:
        if p.get("old_intent_id") == intent_id:
            out.update(superseded_by=p.get("new_intent_id"), conversation_id=p.get("conversation_id"), stale_jobs=p.get("stale_jobs", []),
                       applied=bool(p.get("applied")), superseded_by_actor=actor, superseded_at=at.isoformat() if at else None)
        if p.get("new_intent_id") == intent_id:
            out.update(supersedes=p.get("old_intent_id"), conversation_id=out["conversation_id"] or p.get("conversation_id"),
                       replaced_work=p.get("stale_jobs", []), replaced_applied=bool(p.get("applied")))
    return out


def _drafts(store, tenant: str, ws: str) -> list[tuple[str, dict]]:
    with store._tx() as cur:
        cur.execute("SELECT job_id, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='change.draft' "
                    "AND COALESCE(execution_status,'') NOT IN ('FAILED','CANCELLED') ORDER BY job_id", (tenant, ws))
        return [(r[0], r[1] if isinstance(r[1], dict) else {}) for r in cur.fetchall()]


def superseding_draft(store, tenant: str, ws: str, draft_job_id: str) -> str | None:
    """The NEWER draft that revises `draft_job_id` (following the chain to the latest), or None. Read-only."""
    return _latest_child(_drafts(store, tenant, ws), draft_job_id)


def newer_draft_for_proposal(store, tenant: str, ws: str, proposal_job_id) -> str | None:
    """The draft that REPLACED the draft this proposal was built from (explicit `revises_draft_job_id` lineage; never job ids or time), or None
    (no replacement, or the proposal does not come from a draft, or it cannot be read). Read-only. Used by every step that would carry the
    proposal forward (review, approval bundle, approve, apply): a superseded draft's work stays as history and is never advanced."""
    if not isinstance(proposal_job_id, str) or not proposal_job_id:
        return None
    try:
        prop = store.job_record(tenant, ws, proposal_job_id)
    except Exception:
        return None
    draft_id = (prop.get("payload") or {}).get("draft_job_id") if prop.get("kind") == "proposal" else None
    return superseding_draft(store, tenant, ws, draft_id) if draft_id else None


SUPERSEDED_MSG = "the draft behind this proposal was revised after new confirmed feedback; this work is history. Build a new proposal from the latest draft"


class DraftSuperseded(Exception):
    """Raised INSIDE a store transaction (after the workspace row is locked) when the draft behind the work was revised: the whole transaction rolls back."""


def _latest_child(drafts: list[tuple[str, dict]], draft_id: str) -> str | None:
    child = {p.get("revises_draft_job_id"): jid for jid, p in drafts if p.get("revises_draft_job_id")}
    latest, seen = None, {draft_id}
    while child.get(latest or draft_id) and child[latest or draft_id] not in seen:
        latest = child[latest or draft_id]
        seen.add(latest)
    return latest


def newer_draft_in_tx(cur, tenant: str, ws: str, draft_id) -> str | None:
    """Same answer as `superseding_draft`, read on the CALLER's cursor. Called after `_lock_head`, so it is serialized with every job creation (a revision is a job
    insert made under the same workspace-row lock): the answer cannot change before the caller's transaction ends."""
    if not isinstance(draft_id, str) or not draft_id:
        return None
    cur.execute("SELECT job_id, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='change.draft' "
                "AND COALESCE(execution_status,'') NOT IN ('FAILED','CANCELLED') ORDER BY job_id", (tenant, ws))
    return _latest_child([(r[0], r[1] if isinstance(r[1], dict) else {}) for r in cur.fetchall()], draft_id)


def newer_draft_for_proposal_in_tx(cur, tenant: str, ws: str, proposal_job_id) -> str | None:
    if not isinstance(proposal_job_id, str) or not proposal_job_id:
        return None
    cur.execute("SELECT kind, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, ws, proposal_job_id))
    row = cur.fetchone()
    if not row or row[0] != "proposal":
        return None
    return newer_draft_in_tx(cur, tenant, ws, (row[1] if isinstance(row[1], dict) else {}).get("draft_job_id"))


def proposal_guard(tenant: str, ws: str, proposal_job_id):
    """A check to run INSIDE the transaction that would carry `proposal_job_id` forward (a publish, an approve decision, a job insert)."""
    def _check(cur, _head=None):
        newer = newer_draft_for_proposal_in_tx(cur, tenant, ws, proposal_job_id)
        if newer:
            raise DraftSuperseded(f"{SUPERSEDED_MSG} (newer draft {newer})")
    return _check


def job_guard(kind: str, payload: dict, tenant: str, ws: str):
    """The in-transaction check for creating a job of `kind`, or None when the kind does not depend on a draft."""
    if kind == "change.draft" and payload.get("revises_draft_job_id"):
        prev = payload["revises_draft_job_id"]

        def _revise(cur, _head=None):
            if newer_draft_in_tx(cur, tenant, ws, prev):
                raise DraftSuperseded("that draft was already revised; revise the latest draft")
        return _revise
    if kind == "proposal" and payload.get("draft_job_id"):
        draft = payload["draft_job_id"]

        def _proposal(cur, _head=None):
            if newer_draft_in_tx(cur, tenant, ws, draft):
                raise DraftSuperseded("this draft was revised after new confirmed feedback; build the proposal from the latest draft")
        return _proposal
    if kind in ("review", "approval") and payload.get("proposal_job_id"):
        return proposal_guard(tenant, ws, payload["proposal_job_id"])
    if kind == "apply" and payload.get("approval_job_id"):
        approval = payload["approval_job_id"]

        def _apply(cur, _head=None):
            cur.execute("SELECT payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant, ws, approval))
            row = cur.fetchone()
            pid = ((row[0] if row and isinstance(row[0], dict) else {}) or {}).get("proposal_job_id")
            proposal_guard(tenant, ws, pid)(cur)
        return _apply
    return None


def draft_lineage(store, tenant: str, ws: str, draft_job_id: str) -> dict:
    """Which draft this one revises, the newest draft that replaced it, and the work done for older drafts that is out of date."""
    drafts = dict(_drafts(store, tenant, ws))
    this = drafts.get(draft_job_id)
    if this is None:
        with store._tx() as cur:          # a failed or cancelled draft is still a draft that exists
            cur.execute("SELECT payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='change.draft' AND job_id=%s", (tenant, ws, draft_job_id))
            row = cur.fetchone()
        this = (row[0] if row and isinstance(row[0], dict) else None)
        if this is not None:
            drafts[draft_job_id] = this
    if this is None:
        from infra.adapters.v2_postgres import TenantScopeError
        raise TenantScopeError("draft not found")
    latest = superseding_draft(store, tenant, ws, draft_job_id)
    older, cur = [], this.get("revises_draft_job_id")
    while cur and cur in drafts and cur not in older:
        older.append(cur)
        cur = drafts[cur].get("revises_draft_job_id")
    stale = []
    for d in older + ([draft_job_id] if latest else []):
        stale += [x for x in downstream_of_draft(store, tenant, ws, d)]
    return {"draft_job_id": draft_job_id, "revises": this.get("revises_draft_job_id"), "revised_by": latest,
            "is_latest": latest is None, "older_drafts": older, "stale_downstream": stale}


def downstream_of_draft(store, tenant: str, ws: str, draft_job_id: str) -> list[dict]:
    with store._tx() as cur:
        cur.execute("SELECT job_id, kind, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind = ANY(%s) ORDER BY job_id",
                    (tenant, ws, list(_CHAIN[1:])))
        rows = [(r[0], r[1], r[2] if isinstance(r[2], dict) else {}) for r in cur.fetchall()]
    mine: dict[str, str] = {j: k for j, k, p in rows if k == "proposal" and p.get("draft_job_id") == draft_job_id}
    grew = True
    while grew:
        grew = False
        for jid, kind, p in rows:
            if jid not in mine and {p.get(k) for k in ("proposal_job_id", "review_job_id", "approval_job_id")} & set(mine):
                mine[jid] = kind
                grew = True
    order = {k: i for i, k in enumerate(_CHAIN)}
    return sorted(({"kind": k, "job_id": j} for j, k in mine.items()), key=lambda x: (order.get(x["kind"], 99), x["job_id"]))
