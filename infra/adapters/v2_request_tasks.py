"""Read-only task view of one request (RUX-01, contract `ripple-request-task/1`, docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md).

Two request families keep their own state machines: `intent_flow` (conversation -> intent -> draft -> proposal -> review -> approval -> apply -> verification -> result,
the stored chain of `infra/v2_work_routes.py`) and `business_change` (`chg-…`, the review of `infra/adapters/v2_business_review.py`). This module only projects what those
workflows already decide into one shape: who holds the current obligation, who is waiting, what blocks, what the reader may do. Permission, assignment and obligation stay
apart: a permission makes a person `eligible_me`; only the workflow binding to this account (the requester of their own request) makes `assigned_me`; `obligation_me` is
true when the obligation is currently this person's to act on (assigned, or a member of the role pool that is asked; an administrator who is not an approver is eligible to
decide but not asked). Nothing is written, no job runs, no model is called; every action route rechecks its own predicates.
"""
from __future__ import annotations

import datetime

from core.change_intelligence.v2 import business_draft as bd
from infra.adapters import v2_business_review
from infra.adapters.v2_policy import permissions_for
from infra.adapters.v2_postgres import TenantScopeError

SCHEMA = "ripple-request-task/1"
FAMILIES = ("intent_flow", "business_change")
NOT_FOUND = "request not found"

INTENT_STEPS = (("conversation", "Request stated in a conversation"), ("intent", "Request confirmed"), ("draft", "AI draft"), ("proposal", "Proposal prepared"),
                ("check", "Check that must hold after the change"), ("review_run", "Review run"), ("approval_bundle", "Approval bundle"), ("decision", "Approver's decision"),
                ("apply", "Applied"), ("verification", "Verification and knowledge update"), ("result", "Result decided by a person"))
BUSINESS_STEPS = (("understand", "Request understood and confirmed"), ("draft", "Draft of the knowledge change"), ("clarify", "Open questions answered"),
                  ("knowledge_decision", "Knowledge decision"), ("commit", "Knowledge committed"), ("file_decision", "File edits decided"),
                  ("source_apply", "Source edits applied"), ("readback", "Source read back"))
# the canonical step each obligation / wait belongs to (RUX-03 map: who holds what at which step; two obligations at one step are parallel, never serialised)
STEP_OF = {"answer_or_confirm": "conversation", "ai_reply": "conversation", "confirm_request": "intent", "ask_draft": "draft", "draft_failed": "draft",
           "draft_running": "draft", "continue_analysis": "proposal", "review_optional": "decision",
           "confirm_understanding": "understand", "understanding": "understand", "restate_request": "understand", "clarify": "clarify",
           "respond_to_changes": "knowledge_decision", "decide_knowledge": "knowledge_decision", "commit_knowledge": "commit", "decide_file_writes": "file_decision",
           "apply_source_edits": "source_apply"}
PARALLEL = {"business_change": {"commit": "after_knowledge_decision", "file_decision": "after_knowledge_decision"}}   # done by different people, in either order


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _obligation(code, label, *, required=True, holder, holder_label, eligible_me, assigned_me=False, obligation_me=None, state="current", reason=None) -> dict:
    return {"code": code, "label": label, "required": required, "holder": holder, "holder_label": holder_label, "eligible_me": bool(eligible_me), "assigned_me": bool(assigned_me),
            "obligation_me": bool(eligible_me and (assigned_me if obligation_me is None else obligation_me)), "state": state, **({"reason": reason} if reason else {})}


def _steps(spec, states: dict, phases: dict | None = None) -> list:
    return [{"code": c, "label": l, "state": states.get(c, "future"), **({"phase": phases[c]} if phases and c in phases else {})} for c, l in spec]


# ---- intent_flow ---------------------------------------------------------------------------------------------------------------------------------------------------

def _jobs_where(store, tenant, ws, kind, key, values) -> list:
    values = [v for v in values if v]
    if not values:
        return []
    assert key in ("intent_id", "proposal_job_id", "approval_job_id")                    # fixed keys only: never a client string in the SQL
    with store._tx() as cur:                                                            # newest first by the time the job was queued (the inbox's order)
        cur.execute(f"""SELECT j.job_id FROM v2_jobs j LEFT JOIN LATERAL (
                SELECT min(a.created_at) AS at FROM v2_audit a WHERE a.tenant_id=j.tenant_id AND a.workspace_id=j.workspace_id
                  AND a.event_type='JOB_QUEUED' AND a.payload->>'job_id'=j.job_id) q ON true
            WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind=%s AND j.payload->>'{key}' = ANY(%s) ORDER BY q.at DESC NULLS LAST, j.job_id DESC LIMIT 50""",
                    (tenant, ws, kind, values))
        ids = [r[0] for r in cur.fetchall()]
    return [store.job_record(tenant, ws, j) for j in ids]


def _resolve_intent_flow(store, tenant, ws, rid, authority) -> dict:
    from infra import v2_chat_store
    from infra.v2_chat_routes import _visible
    conv = None
    if rid.startswith("chat-"):
        conv = v2_chat_store.get(store, tenant, ws, rid)
        if conv is None or conv.get("purpose") != "change_request" or not _visible(authority, conv):
            raise TenantScopeError(NOT_FOUND)                                          # the conversation rule is unchanged: creator or knowledge.confirm
        if not conv.get("intent_id"):
            return {"conversation": conv, "intent": None, "canonical": None}
        rid = conv["intent_id"]
    if not rid.startswith("intent-") or "project.read" not in permissions_for(authority.get("roles")):
        raise TenantScopeError(NOT_FOUND)
    try:
        intent = store.get_intent(tenant, ws, rid)
    except Exception as exc:                                                            # noqa: BLE001 - IntentNotFound / TenantScopeError: the same generic answer
        raise TenantScopeError(NOT_FOUND) from exc
    return {"conversation": conv, "intent": intent, "canonical": rid if conv is not None else None}


def _intent_view(store, tenant, ws, rid, authority) -> dict:
    from infra import v2_work_routes as w
    me, roles = authority["account_id"], authority.get("roles") or ()
    perms = permissions_for(roles)
    r = _resolve_intent_flow(store, tenant, ws, rid, authority)
    conv, intent = r["conversation"], r["intent"]
    states, obligations, waiting, blockers, actions, inputs, deliverables = {}, [], [], [], [], [], []
    title = None
    if intent is None:                                                                   # conversation only (creator / knowledge confirmer)
        states["conversation"] = "current"
        mine = conv["created_by"] == me
        pending = any(t["role"] == "assistant" and not t["text"] and t["job_id"] for t in conv["turns"])
        title = next((t["text"] for t in conv["turns"] if t["role"] == "user"), None)
        if pending:
            waiting.append({"holder": "system", "holder_label": "the AI", "code": "ai_reply"})
        else:
            obligations.append(_obligation("answer_or_confirm", "Answer the AI or confirm what you mean", holder="requester", holder_label="the requester",
                                           eligible_me=mine and "intent.create" in perms, assigned_me=mine))
        stage = {"code": "CHAT_OPEN" if conv["status"] == "OPEN" else conv["status"], "label": "The request is still a conversation", "family_state": conv["status"]}
        return _dto("intent_flow", rid, r["canonical"], title, stage, obligations, waiting, blockers, actions, _steps(INTENT_STEPS, states), {}, inputs, deliverables)
    title = (intent.get("raw_request") or "")[:200] or None
    requester = intent.get("created_by")
    mine = requester == me
    states.update(conversation="done", intent="done" if intent["status"] == "CONFIRMED" else "current")
    inputs.append({"kind": "intent", "ref": intent["intent_id"], "label": "The confirmed request"})
    props = [p for p in _jobs_where(store, tenant, ws, "proposal", "intent_id", [intent["intent_id"]])]
    replaced = {(p.get("payload") or {}).get("previous_proposal_job_id") for p in props}
    props = [p for p in props if p["job_id"] not in replaced]
    if intent["status"] != "CONFIRMED":
        # /v2/intent/confirm: the creator with intent.confirm_own, or a holder of knowledge.confirm (eligible, not the one asked)
        may = (mine and "intent.confirm_own" in perms) or "knowledge.confirm" in perms
        reason = (None if may and mine else "the requester is asked; a knowledge owner may also confirm" if may else "only the requester or a knowledge owner confirms the request")
        obligations.append(_obligation("confirm_request", "Confirm the request", holder="requester", holder_label="the requester", eligible_me=may, assigned_me=mine, reason=reason))
        actions.append({"action": "intent.confirm_own", "label": "Confirm the request", "allowed": may, "serves": ["confirm_request"], **({} if may else {"reason": reason})})
        if not mine:
            waiting.append({"holder": "requester", "holder_label": "the requester", "code": "confirm_request"})
        deliverables.append({"code": "confirmed_request", "label": "A confirmed request the AI draft can start from"})
        stage = {"code": "CONFIRM_REQUEST", "label": "Saved request waiting for the requester's confirmation", "family_state": intent["status"]}
        return _dto("intent_flow", intent["intent_id"], r["canonical"], title, stage, obligations, waiting, blockers, actions, _steps(INTENT_STEPS, states), {}, inputs, deliverables)
    if not props:
        drafts = _jobs_where(store, tenant, ws, "change.draft", "intent_id", [intent["intent_id"]])
        d = drafts[0] if drafts else None
        # stage label (where the request is) and step label (what has to be done) are separate; the step is the requester's (the workflow's owner); the routes
        # (POST /v2/jobs change.draft / impact / simulation / proposal) need change.prepare and, for the model steps, ai.use: a permission makes a person eligible,
        # never the one asked
        if d is None:
            code, label, step, need_ai = "ASK_DRAFT", "Confirmed request, no AI draft yet", "Ask for the AI draft of the change", True
        elif d.get("execution_status") in ("FAILED", "CANCELLED"):
            code, label, step, need_ai = "DRAFT_FAILED", "The AI draft failed", "Ask for a new AI draft", True
            blockers.append({"code": "draft_failed", "text": "The AI draft failed; ask for a new draft.", "source": "job"})
        elif d.get("execution_status") != "COMPLETED":
            code, label, step, need_ai = "DRAFT_RUNNING", "The AI draft is being written", None, False
        else:
            code, label, step, need_ai = "CONTINUE_ANALYSIS", "Draft ready: impact analysis, simulation, proposal", "Run the impact analysis and simulation, then prepare the proposal", False
        states["draft"] = "failed" if code == "DRAFT_FAILED" else "current"
        if code == "CONTINUE_ANALYSIS":
            states.update(draft="done", proposal="current")
        if step is None:
            waiting.append({"holder": "system", "holder_label": "the system", "code": "draft_running"})
        else:
            may = "change.prepare" in perms and (not need_ai or "ai.use" in perms)
            reason = (None if may and mine else "the requester is asked; your role may also do it" if may
                      else "AI use is switched off for you" if "change.prepare" in perms else "your role cannot do this step")
            obligations.append(_obligation(code.lower(), step, holder="requester", holder_label="the requester", eligible_me=may, assigned_me=mine, reason=reason))
            actions.append({"action": "change.prepare", "label": step, "allowed": may, "serves": [code.lower()], **({} if may else {"reason": reason})})
            if not mine:
                waiting.append({"holder": "requester", "holder_label": "the requester", "code": code.lower()})
        deliverables.append({"code": "proposal", "label": "A proposal ready for review (impact, simulation, proposed edit)"} if code == "CONTINUE_ANALYSIS"
                            else {"code": "draft", "label": "An AI draft of the change with checked citations"})
        stage = {"code": code, "label": label, "family_state": "CONFIRMED"}
        return _dto("intent_flow", intent["intent_id"], r["canonical"], title, stage, obligations, waiting, blockers, actions, _steps(INTENT_STEPS, states), {}, inputs, deliverables)
    prop = props[0]
    pid = prop["job_id"]
    mine = prop.get("requested_by") == me                                                 # the inbox's owner: the person who requested the proposal
    reviews = _jobs_where(store, tenant, ws, "review", "proposal_job_id", [pid])
    approvals = _jobs_where(store, tenant, ws, "approval", "proposal_job_id", [pid])
    applies = _jobs_where(store, tenant, ws, "apply", "approval_job_id", [a["job_id"] for a in approvals])
    c = w._chain(store, tenant, ws, prop, {"review": reviews, "approval": approvals, "apply": applies})
    notes = w.review_notes(store, tenant, ws, pid)
    st = w._stage(prop, c, notes)
    inputs.append({"kind": "proposal", "ref": pid, "label": "The proposal"})
    order = [s[0] for s in INTENT_STEPS]
    at = {"PROPOSAL_FAILED": "proposal", "PROPOSAL_RUNNING": "proposal", "NEEDS_CHECK": "check", "RUN_REVIEW": "review_run", "REVIEW_RUNNING": "review_run",
          "PREPARE_APPROVAL": "approval_bundle", "APPROVAL_PREPARING": "approval_bundle", "REVIEWER_CHANGES": "decision", "AWAITING_DECISION": "decision", "SENT_BACK": "decision",
          "READY_TO_APPLY": "apply", "APPLY_FAILED": "apply", "APPLYING": "apply", "VERIFYING": "verification", "AWAITING_RESULT": "result", "NEEDS_ATTENTION": "result",
          "APPLIED": "result", "COMPLETED": "result"}.get(st["stage"], "proposal")
    for s in order[2:order.index(at)]:
        states[s] = "done"
    states["draft"] = "done"
    states[at] = "failed" if st["stage"] in ("PROPOSAL_FAILED", "APPLY_FAILED") else ("done" if st["stage"] == "COMPLETED" else "current")
    wt = st["waits"]
    if st["stage"] in ("PROPOSAL_FAILED", "APPLY_FAILED", "NEEDS_ATTENTION"):
        blockers.append({"code": st["stage"].lower(), "text": st["label"], "source": "job"})
    for reviewer in st.get("blocked_by") or []:
        blockers.append({"code": "reviewer_changes", "text": "A reviewer asked for changes; the approval decision is blocked until the requester revises the proposal.",
                         "source": "review_note"})
    if wt is None:
        if st["stage"] not in ("COMPLETED", "APPLIED"):
            waiting.append({"holder": "system", "holder_label": "the system", "code": st["stage"].lower()})
    else:
        is_admin_only = "admin" in roles and "change_approver" not in roles
        if wt.get("owner"):
            holder, assigned = "requester", mine
            eligible = wt["perm"] in perms and mine
            asked = assigned
        else:
            holder, assigned = "role_pool", False
            eligible = wt["perm"] in perms and not (wt.get("not_owner") and mine)
            asked = eligible and not (wt["perm"] in w.APPROVER_STEPS and is_admin_only)
        reason = None
        if not eligible:
            reason = ("the person who asked cannot do this step" if wt.get("not_owner") and mine and wt["perm"] in perms
                      else "only the requester does this step" if wt.get("owner") and not mine else "your role cannot do this step")
        elif not asked:
            reason = "an administrator may do this but is not asked by default"
        obligations.append(_obligation(st["stage"].lower(), st["label"], holder=holder, holder_label=wt["who"], eligible_me=eligible, assigned_me=assigned,
                                       obligation_me=asked, reason=reason))
        if not asked:
            waiting.append({"holder": holder, "holder_label": wt["who"], "code": st["stage"].lower()})
        actions.append({"action": wt["perm"], "label": INTENT_STEP_LABEL.get(st["stage"], st["label"]), "allowed": bool(eligible), "serves": [st["stage"].lower()],
                        **({"reason": reason} if reason and not eligible else {})})
    if st["stage"] in w.REVIEWABLE:                                                      # a reviewer may review before the decision: optional, never required
        done_me = any(n["reviewer"] == me for n in notes)
        can_review = "change.review" in perms and not mine
        obligations.append(_obligation("review_optional", "Review the proposal before the approver decides (optional)", required=False, holder="role_pool",
                                       holder_label="a reviewer", eligible_me=can_review, obligation_me=can_review and not done_me,
                                       state="done" if done_me else "current", reason=None if can_review else ("the person who asked cannot review it" if mine else "your role cannot review")))
    axes = {}
    if c.get("change"):
        axes = {"change_state": c["change"].get("state"), "completion": c["change"].get("completion")}
    deliverables.append({"code": st["stage"].lower(), "label": INTENT_DELIVERABLE.get(st["stage"], st["label"])})
    if st["stage"] in ("AWAITING_DECISION", "REVIEWER_CHANGES", "NEEDS_CHECK", "RUN_REVIEW", "PREPARE_APPROVAL"):
        deliverables.append({"code": "later", "label": "Later, by other steps: the change applied, verified, and a result decided by a person", "later": True})
    stage = {"code": st["stage"], "label": st["label"], "family_state": prop.get("execution_status")}
    return _dto("intent_flow", intent["intent_id"], r["canonical"], title, stage, obligations, waiting, blockers, actions, _steps(INTENT_STEPS, states), axes, inputs, deliverables,
                coverage={"proposal_job_id": pid, "review_notes": len(notes)}, at=at)


# ---- business_change ------------------------------------------------------------------------------------------------------------------------------------------------

def _business_view(store, tenant, ws, rid, authority) -> dict:
    from infra.v2_business_routes import _change_record_for
    if not rid.startswith("chg-"):
        raise TenantScopeError(NOT_FOUND)
    try:
        rec, perms = _change_record_for(store, tenant, ws, rid, authority)
    except TenantScopeError as exc:
        raise TenantScopeError(NOT_FOUND) from exc
    me, roles = authority["account_id"], authority.get("roles") or ()
    requester = rec.get("requested_by")
    mine = requester == me
    title = ((rec.get("payload") or {}).get("request") or "")[:200] or None
    states, obligations, waiting, blockers, actions, inputs, deliverables = {}, [], [], [], [], [], []
    view = v2_business_review.review_view(store, tenant, ws, rid, account_id=me, roles=roles)
    if view is None:
        states["understand"] = "current"
        if rec.get("execution_status") not in ("COMPLETED", "FAILED", "CANCELLED"):
            waiting.append({"holder": "system", "holder_label": "the AI", "code": "understanding"})
        else:
            obligations.append(_obligation("confirm_understanding", "Check the understanding and confirm the request", holder="requester", holder_label="the requester",
                                           eligible_me=mine, assigned_me=mine))
        stage = {"code": "NOT_CONFIRMED", "label": "The request is not confirmed yet", "family_state": rec.get("execution_status")}
        return _dto("business_change", rid, None, title, stage, obligations, waiting, blockers, actions, _steps(BUSINESS_STEPS, states), {}, inputs, deliverables)
    can, draft = view["can"], view["draft"]
    phases = {}
    states.update(understand="done", draft="done")
    inputs += [{"kind": "draft", "ref": rid, "label": "Rule before / after and the proposed relation updates"}]
    is_admin_only = "admin" in roles and "change_approver" not in roles
    perms = permissions_for(roles)
    k, f, committed = can["knowledge_decision"], can["file_writes_decision"], view["committed"]
    moved = view["knowledge_moved"] and committed is None                                 # the commit itself moves the knowledge: not a blocker once committed
    # the routes' own predicates: a clarification must name an open item of the draft as it is now (record_decision); a new request needs intent.create + ai.use
    # (POST /v2/business/changes); a confirmed change pinned to moved knowledge cannot be confirmed again, so a new request is the only way forward
    open_items = bd.open_refs(view["draft"])
    clarify_ok = bool(can["clarify"] and open_items and not moved)
    restate_ok = "intent.create" in perms and "ai.use" in perms
    if moved:
        blockers.append({"code": "knowledge_moved", "text": "The business knowledge changed after this request was confirmed; it has to be stated and confirmed again.", "source": "pin"})
        obligations.append(_obligation("restate_request", "State the request again as a new change request (this request cannot continue)", holder="requester",
                                       holder_label="the requester", eligible_me=restate_ok, assigned_me=mine,
                                       reason=None if restate_ok and mine else "the requester is asked; your role may also state a new request" if restate_ok
                                       else "AI use is switched off for you" if "intent.create" in perms else "your role cannot state a change request"))
        if not mine:
            waiting.append({"holder": "requester", "holder_label": "the requester", "code": "restate_request"})
    hard = [b for b in can["blockers"] if b["code"] != "flag_not_acknowledged"]
    for b in hard:
        blockers.append({"code": b["code"], "text": b.get("text") or b["code"], "source": "draft"})
    if hard:
        states["clarify"] = "blocked" if moved else "current"
        obligations.append(_obligation("clarify", "Answer the open questions of the draft", holder="requester", holder_label="the requester or a knowledge owner",
                                       eligible_me=clarify_ok, assigned_me=mine, obligation_me=mine or "knowledge.confirm" in perms, state="blocked" if moved else "current",
                                       reason=can["reasons"].get("clarify") or ("the knowledge moved; the request has to be stated again" if moved else None)))
        if not moved and not (mine or "knowledge.confirm" in perms):
            waiting.append({"holder": "requester", "holder_label": "the requester or a knowledge owner", "code": "clarify"})
    else:
        states["clarify"] = "done"
    changes_requested = k is not None and k["action"] == "request_changes"
    if changes_requested and not moved:                                                   # the approver sent it back: the requester answers first
        responder = mine or "knowledge.confirm" in perms
        obligations.append(_obligation("respond_to_changes", "Respond to the changes the approver asked for (answer an open item, or state a revised request)",
                                       holder="requester", holder_label="the requester", eligible_me=clarify_ok or (mine and restate_ok), assigned_me=mine,
                                       obligation_me=responder, reason=None if responder else "the requester answers the requested changes"))
        if not mine:
            waiting.append({"holder": "requester", "holder_label": "the requester", "code": "respond_to_changes"})
    if k is None or changes_requested:
        d_state = "blocked" if hard or moved else "waiting" if changes_requested else "current"
        states["knowledge_decision"] = d_state
        asked = can["approve_knowledge"] and not is_admin_only and d_state == "current"
        reason = can["reasons"].get("approve_knowledge") if not can["approve_knowledge"] else (
            "changes were requested; the requester answers first (you may still decide)" if changes_requested
            else None if asked else "an administrator may decide but is not asked by default")
        obligations.append(_obligation("decide_knowledge", "Decide the knowledge change (approve, reject or request changes)", holder="role_pool",
                                       holder_label="an approver (not the person who asked)", eligible_me=can["approve_knowledge"], obligation_me=asked,
                                       state=d_state, reason=reason))
        if d_state == "current" and not asked:
            waiting.append({"holder": "role_pool", "holder_label": "an approver", "code": "decide_knowledge"})
    else:
        states["knowledge_decision"] = "done" if k["action"] == "approve" else "failed"
        if k["action"] == "reject":
            blockers.append({"code": "knowledge_rejected", "text": "The knowledge change was rejected.", "source": "decision"})
    if k is not None and k["action"] == "approve":
        if committed is None:
            states["commit"] = "blocked" if moved else "current"
            obligations.append(_obligation("commit_knowledge", "Commit the approved knowledge change", holder="role_pool", holder_label="a knowledge owner",
                                           eligible_me=can.get("commit_knowledge"), obligation_me=can.get("commit_knowledge"), state=states["commit"],
                                           reason=can["reasons"].get("commit_knowledge")))
            if not can.get("commit_knowledge") and not moved:
                waiting.append({"holder": "role_pool", "holder_label": "a knowledge owner", "code": "commit_knowledge"})
        else:
            states["commit"] = "done"
    if can["has_proposed_edits"]:
        if f is None:
            states["file_decision"] = "blocked" if moved else "current" if k is not None and k["action"] == "approve" else "future"
            obligations.append(_obligation("decide_file_writes", "Decide the proposed file edits", holder="role_pool", holder_label="an approver", eligible_me=can["approve_file_writes"],
                                           obligation_me=can["approve_file_writes"] and not is_admin_only and states["file_decision"] == "current", state=states["file_decision"],
                                           reason=can["reasons"].get("approve_file_writes")))
        else:
            states["file_decision"] = "done" if f["action"] == "approve" else "failed"
        src = view["source"]
        if src["job"] is not None:
            states["source_apply"] = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "failed"}.get(src["job"].get("status"), "current")
            if states["source_apply"] == "current":
                phases["source_apply"] = "running"                                        # the apply job exists and has not finished
        elif f is not None and f["action"] == "approve" and committed is not None:
            states["source_apply"] = "current"
            obligations.append(_obligation("apply_source_edits", "Apply the approved source edits", holder="role_pool", holder_label="a person who may apply changes",
                                           eligible_me=can.get("apply_source_edits"), obligation_me=can.get("apply_source_edits"), reason=can["reasons"].get("apply_source_edits")))
    else:
        states["file_decision"] = states["source_apply"] = "not_applicable"
    axes = dict(view["completion"])
    if axes.get("source_sync") in ("applied_pending_readback",):
        states["readback"] = "current"
    elif committed is not None and not can["has_proposed_edits"]:
        states["readback"] = "not_applicable"
    deliverables += _business_deliverables(moved, hard, changes_requested, k, committed, can, f)
    code = ("KNOWLEDGE_MOVED" if moved else "CLARIFY" if hard else "CHANGES_REQUESTED" if changes_requested else "AWAITING_DECISION" if k is None
            else "REJECTED" if k["action"] == "reject" else "AWAITING_COMMIT" if committed is None else "COMMITTED")
    stage = {"code": code, "label": {"KNOWLEDGE_MOVED": "Blocked: the knowledge moved after confirmation", "CLARIFY": "Open questions must be answered first",
                                     "CHANGES_REQUESTED": "Changes requested by the approver; waiting for the requester's response",
                                     "AWAITING_DECISION": "Waiting for the knowledge decision", "REJECTED": "The knowledge change was rejected",
                                     "AWAITING_COMMIT": "Approved, waiting for the commit", "COMMITTED": "Knowledge committed"}[code], "family_state": draft.get("status")}
    for key, allowed in (("approve_knowledge", can["approve_knowledge"]), ("commit_knowledge", can.get("commit_knowledge")), ("approve_file_writes", can["approve_file_writes"]),
                         ("apply_source_edits", can.get("apply_source_edits")), ("clarify", clarify_ok)):
        why = can["reasons"].get(key) or ({"clarify": "the knowledge moved; the request has to be stated again" if moved else "there is no open item to answer"}.get(key))
        actions.append({"action": key, "label": BUSINESS_ACTION_LABEL[key], "allowed": bool(allowed), "serves": BUSINESS_SERVES[key], **({"reason": why} if not allowed and why else {})})
    if moved or changes_requested:                                                        # the way forward the projection names is also an action with its own predicate
        actions.append({"action": "restate_request", "label": BUSINESS_ACTION_LABEL["restate_request"], "allowed": restate_ok, "serves": BUSINESS_SERVES["restate_request"],
                        **({} if restate_ok else {"reason": "AI use is switched off for you" if "intent.create" in perms else "your role cannot state a change request"})})
    return _dto("business_change", rid, None, title, stage, obligations, waiting, blockers, actions, _steps(BUSINESS_STEPS, states, phases), axes, inputs, deliverables)


# what each business action is called for a reader, and which obligation it serves (an action is a recommended next step only for an obligation the reader is asked to do now)
BUSINESS_ACTION_LABEL = {"approve_knowledge": "Decide the knowledge change (approve, reject or request changes)", "commit_knowledge": "Commit the approved knowledge change",
                         "approve_file_writes": "Decide the proposed file edits", "apply_source_edits": "Apply the approved source edits",
                         "clarify": "Answer an open question of the draft", "restate_request": "State a new change request"}
BUSINESS_SERVES = {"approve_knowledge": ["decide_knowledge"], "commit_knowledge": ["commit_knowledge"], "approve_file_writes": ["decide_file_writes"],
                   "apply_source_edits": ["apply_source_edits"], "clarify": ["clarify", "respond_to_changes"], "restate_request": ["restate_request", "respond_to_changes"]}


def _business_deliverables(moved, hard, changes_requested, k, committed, can, f) -> list:
    """What the CURRENT step hands over (approval is not a commit; a commit is not a file decision or a source sync), then what comes later, marked `later`."""
    later = {"code": "later", "later": True}
    if moved:
        return [{"code": "new_request", "label": "A new change request stated against the current knowledge (this one cannot continue)"}]
    if hard:
        return [{"code": "answers", "label": "Answers to the open questions of the draft"}, {**later, "label": "Later: the knowledge decision"}]
    if changes_requested:
        return [{"code": "response", "label": "A response to the changes the approver asked for"}, {**later, "label": "Later: the knowledge decision on the revised version"}]
    if k is None:
        return [{"code": "knowledge_decision", "label": "A recorded knowledge decision (approve, reject or request changes) with a reason; approving does not commit anything"},
                {**later, "label": "Later, by other steps: the commit by a knowledge owner" + ("; the decision on the proposed file edits" if can["has_proposed_edits"] else "")}]
    if k["action"] == "reject":
        return [{"code": "none", "label": "Nothing more: the knowledge change was rejected"}]
    if committed is None:
        out = [{"code": "commit", "label": "The approved knowledge change committed as a new accepted revision"}]
        if can["has_proposed_edits"] and f is None:
            out.append({"code": "file_decision", "label": "A decision on the proposed file edits (by an approver, in either order with the commit)"})
        return out
    if can["has_proposed_edits"] and f is None:
        return [{"code": "file_decision", "label": "A decision on the proposed file edits"}, {**later, "label": "Later: the approved edits applied and the source read back"}]
    return [{"code": "sync", "label": "Source and code synchronisation, each read back separately (the knowledge change is already committed)"}]


INTENT_STEP_LABEL = {"NEEDS_CHECK": "State the check that must hold after the change", "RUN_REVIEW": "Run the review", "PREPARE_APPROVAL": "Prepare the approval bundle",
                     "AWAITING_DECISION": "Approve or reject the change", "REVIEWER_CHANGES": "Revise the proposal", "SENT_BACK": "Prepare a new proposal",
                     "READY_TO_APPLY": "Apply the approved change", "APPLY_FAILED": "Apply the approved change again", "PROPOSAL_FAILED": "Prepare the proposal again",
                     "AWAITING_RESULT": "Decide the result (accept, request changes or reject)", "NEEDS_ATTENTION": "Decide what happens to the applied change"}
INTENT_DELIVERABLE = {"NEEDS_CHECK": "The check that must hold after the change, stated from the confirmed request", "RUN_REVIEW": "A finished review run against the check",
                      "PREPARE_APPROVAL": "An approval bundle the approver can decide on", "AWAITING_DECISION": "A recorded approval decision with a reason (approving applies nothing by itself)",
                      "REVIEWER_CHANGES": "A revised proposal", "SENT_BACK": "A new proposal, if the change is still wanted", "READY_TO_APPLY": "The approved change applied as a new source revision",
                      "AWAITING_RESULT": "A result decision by a person (accept, request changes or reject)", "NEEDS_ATTENTION": "A person's decision about the applied change"}


# ---- shared --------------------------------------------------------------------------------------------------------------------------------------------------------
# Step phase (D4, 2026-10-21): where a step stands, derived from what the record holds for it (a system wait or a running job, the current obligation's kind, the
# step state), so "current" is never read as "running". A current step with no recorded wait, obligation or running work is `unknown`, said as such.
PHASE_LABEL = {"done": "done", "failed": "failed or stopped", "blocked": "blocked", "future": "later", "not_applicable": "not needed", "partial": "partly done",
               "running": "in progress (the system is working on it)", "not_started": "current step, not started yet",
               "awaiting_decision": "current step, waiting for a decision", "awaiting_action": "current step, waiting for an action",
               "awaiting_other_step": "waiting until another step is done", "unknown": "current step; the record does not show what it is waiting for"}
START_CODES = frozenset({"ask_draft", "continue_analysis", "needs_check", "run_review", "prepare_approval", "restate_request", "ready_to_apply", "apply_source_edits"})
DECISION_CODES = frozenset({"decide_knowledge", "decide_file_writes", "awaiting_decision", "awaiting_result", "needs_attention"})


def _phase(stage: dict, obligations: list, waiting: list) -> str:
    if stage.get("phase"):                                                                 # set by the family view from a job it read (e.g. a running source apply)
        return stage["phase"]
    st, code = stage["state"], stage["code"]
    if st not in ("current", "waiting"):
        return st if st in PHASE_LABEL else "unknown"
    if any(w.get("step") == code and w.get("holder") == "system" for w in waiting):
        return "running"
    here = [o for o in obligations if o.get("step") == code and o.get("state") == "current"]
    here = [o for o in here if o.get("required")] or here
    if here:
        c = here[0]["code"]
        return "not_started" if c in START_CODES else "awaiting_decision" if c in DECISION_CODES else "awaiting_action"
    return "awaiting_other_step" if st == "waiting" else "unknown"


def _dto(family, rid, canonical, title, stage, obligations, waiting, blockers, actions, stages, axes, inputs, deliverables, coverage=None, at=None) -> dict:
    asked_now = {o["code"] for o in obligations if o["obligation_me"] and o["state"] == "current"}
    for a in actions:                                                                       # recommended now = allowed AND serving a step the reader is asked to do now
        a["current"] = bool(a.get("allowed")) and any(c in asked_now for c in a.pop("serves", []))
    known = {s["code"] for s in stages}
    for x in list(obligations) + list(waiting):                                          # a step the map cannot place stays without one (unknown), never guessed
        st = STEP_OF.get(x["code"]) or at
        if st in known:
            x["step"] = st
    par = PARALLEL.get(family, {})
    if all(s["state"] != "not_applicable" for s in stages if s["code"] in par):
        for s in stages:
            if s["code"] in par:
                s["parallel_group"] = par[s["code"]]
    for s in stages:                                                                        # what the record shows for each step, never guessed from the word "current"
        s["phase"] = _phase(s, obligations, waiting)
        s["phase_label"] = PHASE_LABEL[s["phase"]]
    return {"schema": SCHEMA, "request_ref": {"family": family, "id": rid, **({"canonical_ref": {"family": family, "id": canonical}} if canonical else {})},
            "title": title, "as_of": _now(), "availability": "available", "stage": stage, "obligations": obligations, "waiting_on": waiting, "blockers": blockers,
            "input_refs": inputs, "expected_deliverables": deliverables, "permitted_actions": actions, "stages": stages, "completion_axes": axes, "coverage": coverage or {},
            "statement": "A read-only view of the existing workflow: it shows who holds the next step and what blocks it; every action is checked again by its own route."}


def task_view(store, tenant, ws, family: str, rid: str, authority: dict) -> dict:
    if family not in FAMILIES or not isinstance(rid, str) or len(rid) > 120:
        raise TenantScopeError(NOT_FOUND)
    if family == "intent_flow":
        return _intent_view(store, tenant, ws, rid, authority)
    return _business_view(store, tenant, ws, rid, authority)


# ---- the work list (RUX-04): where one request belongs for THIS reader, read from the projection only ------------------------------------------------------------
DONE_STAGES = ("APPLIED", "COMPLETED", "COMMITTED")
SECTIONS = ("mine", "optional", "blocked", "waiting_human", "waiting_system", "recent")


def work_entry(tv: dict, *, mine: bool) -> dict | None:
    """-> {section, ...} or None when the request is not this reader's concern. `mine` = the reader owns the request (the one who waits for it). A step is "mine"
    only when the projection says the reader is the one asked (obligation_me) and it is current; a permission alone never puts work here."""
    cur = [o for o in tv["obligations"] if o["obligation_me"] and o["state"] == "current"]
    req, opt = [o for o in cur if o["required"]], [o for o in cur if not o["required"]]
    waits, code = tv["waiting_on"], tv["stage"]["code"]
    base = {"stage": code, "label": tv["stage"]["label"], "blocked": bool(tv["blockers"]), "blockers": [b["text"] for b in tv["blockers"]]}
    if req or opt:
        o = (req or opt)[0]
        return {**base, "section": "mine" if req else "optional", "you_do": o["holder_label"], "why": o["label"], "task": o["code"],
                **({} if req else {"optional": True}), **({"step": o["step"]} if o.get("step") else {})}
    if code in DONE_STAGES and not waits:
        return {**base, "section": "recent", "waiting_on": None}
    if not mine:
        return None
    if waits:
        system = all(w["holder"] == "system" for w in waits)
        return {**base, "section": "waiting_system" if system else "waiting_human", "waiting_on": ", ".join(dict.fromkeys(w["holder_label"] for w in waits)),
                **({"step": waits[0]["step"]} if waits[0].get("step") else {})}
    if tv["blockers"]:
        return {**base, "section": "blocked", "waiting_on": None}                         # nobody can move it on its current path: said, not hidden
    return None
