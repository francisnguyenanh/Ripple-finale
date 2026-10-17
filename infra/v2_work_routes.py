"""Hand-off between people: review notes and the work inbox. Read-mostly; nothing here approves, applies or calls a model.

* A REVIEWER (`change.review`) reads a proposal and records a note on it: a comment, "request changes" or "ready for approval".
  A note is an append-only audit event bound to the proposal job. An open "request changes" blocks the approval decision of THAT
  proposal (the approver still decides everything else); a revised proposal is a new proposal and starts without it.
* The INBOX answers "what is waiting for me, and what am I waiting for", derived on the server from the same stored jobs, decisions,
  contracts and notes the Changes page uses - so nobody has to copy a link to hand work over. What a person may do is still decided by
  the permission layer on each action; the inbox only shows where to go.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg2.extras import Json

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
NOTE_EVENT = "CHANGE_REVIEW_NOTE"
VERDICTS = ("comment", "request_changes", "ready")
MAX_NOTE_CHARS = 1000
INBOX_LIMIT = 40
REVIEWABLE = ("NEEDS_CHECK", "RUN_REVIEW", "REVIEW_RUNNING", "PREPARE_APPROVAL", "APPROVAL_PREPARING", "AWAITING_DECISION")   # a finished proposal that no decision has closed
APPROVER_STEPS = ("change.approve", "verification.contract.set")


# ----------------------------------------------------------------------------- review notes

def review_notes(store, tenant: str, ws: str, proposal_job_id: str) -> list[dict]:
    with store._tx() as cur:
        cur.execute("""SELECT id, actor_id, payload, created_at FROM v2_audit
            WHERE tenant_id=%s AND workspace_id=%s AND event_type=%s AND payload->>'proposal_job_id'=%s ORDER BY id""",
                    (tenant, ws, NOTE_EVENT, proposal_job_id))
        rows = cur.fetchall()
    out = []
    for rid, actor, payload, at in rows:
        p = payload if isinstance(payload, dict) else {}
        out.append({"note_id": rid, "reviewer": actor, "verdict": p.get("verdict"), "text": p.get("text") or "",
                    "source_revision_id": p.get("source_revision_id"), "created_at": at.isoformat()})
    return out


def open_change_requests(notes: list[dict]) -> list[str]:
    """Reviewers whose LATEST note on this proposal is 'request changes' (a later 'ready' or comment-free 'ready' from them clears it)."""
    latest: dict[str, str] = {}
    for n in notes:
        if n["verdict"] in ("request_changes", "ready"):
            latest[n["reviewer"]] = n["verdict"]
    return sorted(a for a, v in latest.items() if v == "request_changes")


@router.get("/v2/changes/{proposal_job_id}/review-notes")
def get_review_notes(request: Request, proposal_job_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        notes = review_notes(store, authority["tenant_id"], workspace_id, proposal_job_id)
        return JSONResponse({"proposal_job_id": proposal_job_id, "notes": notes, "open_change_requests": open_change_requests(notes),
                             "you_can_review": "change.review" in permissions_for(authority.get("roles"))}, headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)


@router.post("/v2/changes/review-note")
async def post_review_note(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    ws, pid, verdict, text = body.get("workspace_id"), body.get("proposal_job_id"), body.get("verdict"), body.get("text")
    try:
        authority = _authority(identity, request, ws if isinstance(ws, str) else "")      # sign-in and rights BEFORE the body is looked at
        require_human_actor(authority)
        require(authority, "change.review")
        if not isinstance(pid, str) or not pid or verdict not in VERDICTS:
            return _error("proposal_job_id and verdict (comment | request_changes | ready) required", 400)
        text = text.strip() if isinstance(text, str) else ""
        if len(text) > MAX_NOTE_CHARS:
            return _error(f"a note is limited to {MAX_NOTE_CHARS} characters", 400)
        if verdict != "ready" and not text:
            return _error("say what you found or what has to change", 400)
        tenant = authority["tenant_id"]
        job = store.job_record(tenant, ws, pid)
        if job["kind"] != "proposal" or job.get("execution_status") != "COMPLETED":
            return _error("a review note needs a finished proposal", 409)
        if job.get("requested_by") == authority["account_id"]:
            raise PermissionDenied("a proposal cannot be reviewed by the person who made it")
        notes = review_notes(store, tenant, ws, pid)
        mine = [n for n in notes if n["reviewer"] == authority["account_id"]]
        if mine and mine[-1]["verdict"] == verdict and mine[-1]["text"] == text:
            return JSONResponse({"proposal_job_id": pid, "notes": notes, "open_change_requests": open_change_requests(notes), "replayed": True},
                                headers=_NO_STORE)       # a double click or retry records nothing twice
        head = store.head(tenant, ws)
        with store._tx() as cur:
            cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)""",
                        (tenant, ws, NOTE_EVENT, authority["account_id"],
                         Json({"proposal_job_id": pid, "verdict": verdict, "text": text, "source_revision_id": head.source_revision_id})))
        notes = review_notes(store, tenant, ws, pid)
        return JSONResponse({"proposal_job_id": pid, "notes": notes, "open_change_requests": open_change_requests(notes)}, headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


# ----------------------------------------------------------------------------- inbox

def _jobs(store, tenant, ws, kind) -> list[str]:
    """Newest first (by the time the job was queued)."""
    with store._tx() as cur:
        cur.execute("""SELECT j.job_id FROM v2_jobs j LEFT JOIN LATERAL (
                SELECT min(a.created_at) AS at FROM v2_audit a WHERE a.tenant_id=j.tenant_id AND a.workspace_id=j.workspace_id
                  AND a.event_type='JOB_QUEUED' AND a.payload->>'job_id'=j.job_id) q ON true
            WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind=%s ORDER BY q.at DESC NULLS LAST, j.job_id DESC LIMIT 200""",
                    (tenant, ws, kind))
        return [r[0] for r in cur.fetchall()]


def _done(job) -> bool:
    return bool(job) and job.get("execution_status") == "COMPLETED"


def _chain(store, tenant, ws, prop: dict, by_kind: dict) -> dict:
    """The stored chain behind one proposal: its review jobs, approval bundles, latest decision, apply job and contracts."""
    pid = prop["job_id"]
    reviews = [j for j in by_kind["review"] if (j.get("payload") or {}).get("proposal_job_id") == pid]
    approvals = [j for j in by_kind["approval"] if (j.get("payload") or {}).get("proposal_job_id") == pid]
    approval = next((a for a in approvals if _done(a)), approvals[0] if approvals else None)
    decisions = store.change_decisions(tenant, ws, approval["job_id"]) if approval else []
    applies = [j for j in by_kind["apply"] if approval and (j.get("payload") or {}).get("approval_job_id") == approval["job_id"]]
    change = None
    if applies and _done(applies[0]):                    # after the apply, where the CHANGE stands (verification, knowledge, the person's result decision)
        try:
            from infra.adapters.v2_change import get_change_view
            view = get_change_view(store, tenant, ws, applies[0]["job_id"])
            change = {"state": view.get("state"), "completion": (view.get("completion") or {}).get("status")}
        except Exception:
            change = None
    return {"change": change, "reviews": reviews, "review": next((r for r in reviews if _done(r)), reviews[0] if reviews else None), "approval": approval,
            "decisions": decisions, "last": decisions[-1] if decisions else None, "apply": applies[0] if applies else None,
            "contracts": store.verification_contracts_for_proposal(tenant, ws, pid)}


def _stage(prop: dict, c: dict, notes: list[dict]) -> dict:
    """-> {stage, label, waits: {perm, who}|None (None = the system), ready}. `waits.perm` is the permission of the person who can act."""
    if prop.get("execution_status") in ("FAILED", "CANCELLED"):
        return {"stage": "PROPOSAL_FAILED", "label": "The proposal could not be prepared", "waits": {"perm": "intent.create", "who": "the requester", "owner": True}}
    if not _done(prop):
        return {"stage": "PROPOSAL_RUNNING", "label": "The proposal is being prepared", "waits": None}
    if not c["contracts"]:
        return {"stage": "NEEDS_CHECK", "label": "Needs the check that must hold after the change", "waits": {"perm": "verification.contract.set", "who": "an approver (not the requester)", "not_owner": True}}
    if c["review"] is None or not _done(c["review"]):
        return {"stage": "RUN_REVIEW", "label": "Ready for the review run", "waits": {"perm": "source.import", "who": "the requester", "owner": True}} \
            if c["review"] is None else {"stage": "REVIEW_RUNNING", "label": "The review is running", "waits": None}
    if c["approval"] is None or not _done(c["approval"]):
        return {"stage": "PREPARE_APPROVAL", "label": "Ready to prepare the approval bundle", "waits": {"perm": "source.import", "who": "the requester", "owner": True}} \
            if c["approval"] is None else {"stage": "APPROVAL_PREPARING", "label": "The approval bundle is being prepared", "waits": None}
    last = c["last"]
    if last is None:
        blocked = open_change_requests(notes)
        if blocked:
            return {"stage": "REVIEWER_CHANGES", "label": "A reviewer asked for changes", "waits": {"perm": "intent.create", "who": "the requester", "owner": True},
                    "blocked_by": blocked}
        return {"stage": "AWAITING_DECISION", "label": "Waiting for the approver's decision", "waits": {"perm": "change.approve", "who": "an approver (not the requester)", "not_owner": True}}
    if last["action"] != "approve":
        return {"stage": "SENT_BACK", "label": "Rejected" if last["action"] == "reject" else "Sent back for changes", "waits": {"perm": "intent.create", "who": "the requester", "owner": True}}
    ap = c["apply"]
    if ap is None:
        return {"stage": "READY_TO_APPLY", "label": "Approved, ready to apply", "waits": {"perm": "change.apply", "who": "the requester", "owner": True}}
    if ap.get("execution_status") in ("FAILED", "CANCELLED"):
        return {"stage": "APPLY_FAILED", "label": "Applying failed", "waits": {"perm": "change.apply", "who": "the requester", "owner": True}}
    if not _done(ap):
        return {"stage": "APPLYING", "label": "Being applied and re-checked", "waits": None}
    state = (c.get("change") or {}).get("state")
    who = "an approver or an administrator"
    if state == "COMPLETED":
        return {"stage": "COMPLETED", "label": "Completed: verified and decided by a person", "waits": None}
    if state in ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE"):
        return {"stage": "AWAITING_RESULT", "label": "Verified - waiting for the result decision (not complete yet)", "waits": {"perm": "change.accept_result", "who": who}}
    if state in ("STAGED", "SOURCE_PUBLISHED", "REANALYSING"):
        return {"stage": "VERIFYING", "label": "Applied - verification and the knowledge update are running", "waits": None}
    if state in ("FAILED", "RECOVERY_REQUIRED", "STALE"):
        return {"stage": "NEEDS_ATTENTION", "label": "The applied change needs a person's attention", "waits": {"perm": "change.accept_result", "who": who}}
    return {"stage": "APPLIED", "label": "Applied", "waits": None}


def _own_requests(store, tenant, ws, me, have_intent: set, drafts: list[dict]) -> list[dict]:
    """The caller's OWN requests that have not reached a proposal yet (chat -> confirm -> draft -> analysis): where to continue."""
    with store._tx() as cur:
        cur.execute("""SELECT conversation_id, status, intent_id, (SELECT text FROM v2_chat_turns t WHERE t.tenant_id=c.tenant_id AND t.workspace_id=c.workspace_id
                         AND t.conversation_id=c.conversation_id AND t.role='user' ORDER BY seq LIMIT 1),
                         EXISTS (SELECT 1 FROM v2_chat_turns t WHERE t.tenant_id=c.tenant_id AND t.workspace_id=c.workspace_id
                         AND t.conversation_id=c.conversation_id AND t.role='assistant' AND t.text='' AND t.job_id IS NOT NULL)
            FROM v2_chat_conversations c WHERE tenant_id=%s AND workspace_id=%s AND created_by=%s AND purpose='change_request'
              AND status IN ('OPEN','CONFIRMED') ORDER BY created_at DESC LIMIT 10""", (tenant, ws, me))
        rows = cur.fetchall()
    out = []
    for cid, status, intent_id, first, ai_pending in rows:
        if intent_id and intent_id in have_intent:
            continue                                                       # it already has a proposal: that item covers it
        base = {"proposal_job_id": cid, "title": (first or cid)[:140], "requested_by": me, "mine": True, "params": {"chat": cid}, "open_change_requests": [],
                "reviewer_notes": 0}
        if status == "OPEN":
            out.append({**base, "stage": "CHAT_OPEN", "label": "Your request is still a conversation",
                        "you_do": "the requester", "why": "The AI is replying" if ai_pending else "Answer the AI or confirm what you mean", "waits_for_system": bool(ai_pending),
                        "waiting_on": "the AI" if ai_pending else None})
            continue
        base["params"] = {"chat": cid, "intent": intent_id}
        try:
            intent = store.get_intent(tenant, ws, intent_id) if intent_id else None
        except Exception:
            intent = None
        if intent is None:
            continue
        if intent["status"] == "PARSED":
            out.append({**base, "stage": "CONFIRM_REQUEST", "label": "Saved request waiting for your confirmation", "you_do": "the requester", "why": "Confirm your request (Confirm intent)"})
            continue
        if intent["status"] != "CONFIRMED":
            continue
        mine_drafts = [d for d in drafts if (d.get("payload") or {}).get("intent_id") == intent_id]
        d = mine_drafts[0] if mine_drafts else None
        if d is None:
            out.append({**base, "stage": "ASK_DRAFT", "label": "Confirmed request, no AI draft yet", "you_do": "the requester", "why": "Ask for the AI draft"})
        elif d.get("execution_status") in ("FAILED", "CANCELLED"):
            out.append({**base, "stage": "DRAFT_FAILED", "label": "The AI draft failed", "you_do": "the requester", "why": "Ask for a new draft",
                        "params": {**base["params"], "draft": d["job_id"]}})
        elif d.get("execution_status") != "COMPLETED":
            out.append({**base, "stage": "DRAFT_RUNNING", "label": "The AI draft is being written", "waits_for_system": True, "waiting_on": "the system",
                        "params": {**base["params"], "draft": d["job_id"]}})
        else:
            out.append({**base, "stage": "CONTINUE_ANALYSIS", "label": "Draft ready", "you_do": "the requester", "why": "Continue: impact analysis, simulation, proposal",
                        "params": {**base["params"], "draft": d["job_id"]}})
    return out


@router.get("/v2/work/chain")
def work_chain(request: Request, workspace_id: str, proposal_job_id: str):
    """The ids of the stored chain behind ONE proposal (intent, draft, impact, simulation, proposal, review, approval, apply), so a page opened with only part of the link
    finds its own stages again. Read-only, same right as reading the proposal; it names job ids that the same person can already read, nothing from a source."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        prop = store.job_record(tenant, workspace_id, proposal_job_id)
        if prop.get("kind") != "proposal":
            return _error("proposal_job_id is not a proposal job", 400)
        by_kind = {k: [store.job_record(tenant, workspace_id, j) for j in _jobs(store, tenant, workspace_id, k)[:INBOX_LIMIT]] for k in ("review", "approval", "apply")}
        c = _chain(store, tenant, workspace_id, prop, by_kind)
        pl = prop.get("payload") or {}
        params = {k: v for k, v in {"intent": pl.get("intent_id"), "draft": pl.get("draft_job_id"), "impact": pl.get("impact_job_id"), "simulation": pl.get("simulation_job_id"),
                                    "proposal": prop["job_id"], "review": (c["review"] or {}).get("job_id"), "approval": (c["approval"] or {}).get("job_id"),
                                    "apply": (c["apply"] or {}).get("job_id")}.items() if v}
        return JSONResponse({"workspace_id": workspace_id, "params": params}, headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/work/inbox")
def work_inbox(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant, me = authority["tenant_id"], authority["account_id"]
        perms = permissions_for(authority.get("roles"))
        by_kind = {k: [store.job_record(tenant, workspace_id, j) for j in _jobs(store, tenant, workspace_id, k)[:INBOX_LIMIT]]
                   for k in ("proposal", "review", "approval", "apply")}
        replaced = {(p.get("payload") or {}).get("previous_proposal_job_id") for p in by_kind["proposal"]}
        todo, waiting, recent = [], [], []
        for prop in by_kind["proposal"]:
            if prop["job_id"] in replaced:
                continue                                             # a newer proposal replaced this one
            c = _chain(store, tenant, workspace_id, prop, by_kind)
            notes = review_notes(store, tenant, workspace_id, prop["job_id"])
            st = _stage(prop, c, notes)
            pl = prop.get("payload") or {}
            intent_id = pl.get("intent_id")
            title = None
            if intent_id:
                try:
                    title = (store.get_intent(tenant, workspace_id, intent_id).get("raw_request") or "")[:140] or None
                except Exception:
                    title = None
            params = {k: v for k, v in {"intent": intent_id, "draft": pl.get("draft_job_id"), "impact": pl.get("impact_job_id"),
                                        "simulation": pl.get("simulation_job_id"), "proposal": prop["job_id"],
                                        "review": (c["review"] or {}).get("job_id"), "approval": (c["approval"] or {}).get("job_id"),
                                        "apply": (c["apply"] or {}).get("job_id")}.items() if v}
            mine = prop.get("requested_by") == me
            item = {"proposal_job_id": prop["job_id"], "title": title or prop["job_id"], "requested_by": prop.get("requested_by"),
                    "stage": st["stage"], "label": st["label"], "params": params, "mine": mine,
                    "reviewer_notes": len(notes), "open_change_requests": open_change_requests(notes)}
            w = st["waits"]
            if w is None:
                if st["stage"] == "APPLIED":
                    recent.append({**item, "waiting_on": None})
                elif mine:
                    waiting.append({**item, "waiting_on": "the system"})
                continue
            can = w["perm"] in perms and (not w.get("not_owner") or not mine) and (not w.get("owner") or mine)
            if can and w["perm"] in APPROVER_STEPS and "admin" in (authority.get("roles") or ()) and "change_approver" not in (authority.get("roles") or ()):
                can = False       # an administrator is not a default approver: it may still decide (the server allows it) but is not asked to
            if can:
                todo.append({**item, "you_do": w["who"], "why": st["label"]})
            elif mine:
                waiting.append({**item, "waiting_on": w["who"]})
            # a reviewer is also offered every proposal that waits for a decision (optional review), until they leave a note
            if st["stage"] in REVIEWABLE and "change.review" in perms and not mine and not any(n["reviewer"] == me for n in notes):
                todo.append({**item, "you_do": "a reviewer (optional)", "why": "You may review this proposal before the approver decides", "optional": True})
        have_intent = {(p.get("payload") or {}).get("intent_id") for p in by_kind["proposal"]}
        if "intent.create" in perms:
            drafts = [store.job_record(tenant, workspace_id, j) for j in _jobs(store, tenant, workspace_id, "change.draft")[:INBOX_LIMIT]]
            for item in _own_requests(store, tenant, workspace_id, me, have_intent, drafts):
                (waiting if item.pop("waits_for_system", False) else todo).append(item)
        return JSONResponse({"workspace_id": workspace_id, "account_id": me, "todo": todo, "waiting": waiting, "recent": recent[:5]}, headers=_NO_STORE)
    except IdentityDenied as exc:
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
