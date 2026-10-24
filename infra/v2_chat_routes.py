"""Free-text request chat: words -> (AI clarification) -> structured understanding -> the person confirms -> an ordinary intent.

What this layer guarantees (see also infra/worker/intent_interpret.py):
  * A conversation and every understanding VERSION are stored server-side; reading needs the creator OR `knowledge.confirm`.
  * Each AI reply is one `intent.interpret` job. A member who holds `ai.use` (server-side membership; revocable per member and per
    conversation) is given the conversation's bounded quota BY POLICY - no person grants a reply. A member without it, a revoked
    conversation, an exhausted quota, a sandbox workspace or a disarmed provider are refused/held; its cost is reserved per job like every other job.
  * "Confirm" here only turns the person's own checked understanding into a PARSED intent. It is not yet the confirmation of the request (the owner does that next), not a model-call approval and not a change approval; nothing is drafted, approved or applied here.
  * The structured understanding is validated by the same `normalise_brief_form` as the structured form; only the person's own
    edits or the model's server-verified fields can reach it. The expected value of the verification contract is never touched.
"""

from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
import hashlib

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.change_intelligence.v2.brief_form import normalise_brief_form
from infra import ai_admission, chat_safety, v2_chat_store as chat
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_distinct_approver, require_human_actor
from infra.adapters.v2_postgres import ConcurrencyLimitExceeded, StaleVersion, StoreConflict, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store
from infra.worker import job_quota
from infra.worker.intent_interpret import load_config

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
KIND = "intent.interpret"


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _can(authority, permission) -> bool:
    try:
        require(authority, permission)
        return True
    except PermissionDenied:
        return False


def _limits() -> dict:
    return load_config()[0]["limits"]


def _quota(store, tenant, ws):
    import os
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        raise job_quota.BudgetConfigError("live intent.interpret requires a budget config")
    config = job_quota.load_worker_budget_config()
    job_quota.check_job_admission(config, job_kind=KIND)
    return job_quota.load_concurrency_limit(), job_quota.compute_budget_reservation(config)


def _visible(authority, conv) -> bool:
    return conv["created_by"] == authority["account_id"] or _can(authority, "knowledge.confirm")


def _public(conv: dict, authority) -> dict:
    lim = _limits()
    out = dict(conv)
    # an assistant turn stored BEFORE this rule carries the model's free sentence: it is re-rendered from the structured understanding on every read (stored data untouched)
    out["turns"] = [({**t, "text": chat_safety.preview_turn_text(t["understanding"])} if t.get("role") == "assistant" and isinstance(t.get("understanding"), dict)
                     and t["understanding"].get("source") == "model" and t.get("text") else t) for t in conv["turns"]]
    out["turns"] = [({**t, "text": (chat_safety.confirmed_preview_text(conv["outcome"]) if (conv.get("outcome") and t["text"].startswith("You confirmed this preview. Nothing has been done yet"))
                      else chat_safety.confirmed_request_text(conv.get("intent_id"), "PARSED") if t["text"].startswith("You confirmed this understanding. It is now a saved request")
                      else t["text"])} if t.get("role") == "system" and isinstance(t.get("text"), str) else t) for t in out["turns"]]      # sentences stored by the older rule are re-rendered from the server's state on read
    out["limits"] = {"max_ai_turns": lim["max_ai_turns_per_conversation"], "max_allowance_grant": lim["max_allowance_grant"],
                     "max_user_turn_chars": lim["max_user_turn_chars"]}
    pending = [t for t in conv["turns"] if t["role"] == "assistant" and not t["text"] and t["job_id"]]
    last = conv["turns"][-1] if conv["turns"] else None
    out["awaiting_ai"] = bool(pending)
    usable = _usable(conv)
    # "your last message has no AI reply and none is on the way" (a held, refused or not-yet-asked reply); `ai_block` says why, if a rule does
    out["awaiting_allowance"] = bool(last and last["role"] == "user" and not pending and conv["status"] == "OPEN")
    out["authorization"] = _authorization(conv, usable)
    out["safety_notes"] = chat_safety.scan(" \n".join(t["text"] for t in conv["turns"] if t["role"] == "user"))
    out["you_are_creator"] = conv["created_by"] == authority["account_id"]
    out["can_allow"] = _can(authority, "source.import") and not out["you_are_creator"]
    store = _v2_store()
    block = ai_admission.block_reason(store, authority["tenant_id"], authority["workspace_id"], authority) if store is not None else "no_permission"
    out["can_use_ai"] = block is None
    # Why the AI is not answering, in the server's words (None = it can be used; a new message or "Ask the AI" starts a reply).
    person_window_over = bool(conv["ai_approved_by"] and not str(conv["ai_approved_by"]).startswith("policy:") and not usable and not conv["ai_revoked_at"]
                              and conv["ai_used"] < conv["ai_allowance"])
    out["ai_block"] = ("revoked" if conv["ai_revoked_at"] else block
                       or ("quota_used" if conv["ai_used"] >= lim["max_ai_turns_per_conversation"] else "window_ended" if person_window_over else None))
    return out


def _usable(conv) -> bool:
    """True while this conversation still holds a live AI permission (not used up, not revoked, not expired)."""
    from datetime import datetime, timezone
    if conv["ai_revoked_at"] or conv["ai_used"] >= conv["ai_allowance"]:
        return False
    exp = conv.get("ai_expires_at")
    return not exp or datetime.fromisoformat(exp) > datetime.now(timezone.utc)


def _authorization(conv, usable) -> dict:
    """The finite, revocable permission to call the model for THIS conversation - nothing else. Shown as is in the UI."""
    cost = None
    try:
        import os
        if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
            res = job_quota.compute_budget_reservation(job_quota.load_worker_budget_config())
            cost = str(res[0]) if res else None
    except Exception:
        cost = None
    left = max(0, conv["ai_allowance"] - conv["ai_used"]) if usable else 0
    return {"live": usable, "replies_left": left, "replies_used": conv["ai_used"], "replies_allowed": conv["ai_allowance"],
            "granted_by": conv["ai_approved_by"], "granted_at": conv["ai_approved_at"], "expires_at": conv["ai_expires_at"],
            "revoked_at": conv["ai_revoked_at"], "revoked_by": conv["ai_revoked_by"], "reserved_usd_per_reply": cost,
            "worst_case_usd_left": str((job_quota.Decimal(cost) * left)) if cost and hasattr(job_quota, "Decimal") else None,
            "covers": "Calling the AI model for this conversation only (this workspace), within the limits above.",
            "does_not_cover": ["confirming your understanding", "confirming the request", "approving a proposal", "applying a change"]}


def _policy_allowance(store, tenant, ws, cid, authority) -> bool:
    """True when this conversation holds a usable AI quota for its OWN creator, granting it by policy when the member may use the AI.

    The quota (replies per conversation, minutes) is the bound - not a person's click per reply. An explicit revoke of the conversation
    stays revoked; a member without `ai.use` (or marked `ai_suspended`), a sandbox workspace or an exhausted quota gets nothing."""
    conv = chat.get(store, tenant, ws, cid)
    if conv is None or conv["created_by"] != authority["account_id"] or conv["ai_revoked_at"]:
        return False
    if ai_admission.block_reason(store, tenant, ws, authority) is not None:
        return False
    if _usable(conv):
        return True
    # Three different things, never mixed: (1) the member's `ai.use` (role, switched per member by an administrator), (2) this conversation's
    # activity window and reply quota (renewed by the OWNER's own new message while (1) holds, up to the reply cap), (3) the operator's arm
    # and allocation (provider gate, ceilings, reservation: checked on every dispatch, never touched by a message). A window or deadline a
    # PERSON set (an earlier second-person grant) is respected: a new message does not extend it.
    if conv["ai_approved_by"] and not str(conv["ai_approved_by"]).startswith("policy:"):
        return False
    lim = _limits()
    cap = lim["max_ai_turns_per_conversation"]
    if conv["ai_used"] >= cap:
        return False
    grant = chat.grant_allowance(store, tenant, ws, cid, ai_admission.POLICY_APPROVER, cap, cap, lim["grant_minutes"])
    chat.audit(store, tenant, ws, "CHAT_AI_ALLOWED_BY_POLICY", authority["account_id"],
               {"conversation_id": cid, "replies_allowed": grant["ai_allowance"], "minutes": lim["grant_minutes"], "policy": "ai.use"})
    return True


def _start_ai(store, tenant, ws, cid, authority) -> str | None:
    """Send the last unanswered person turn to the AI under the member's standing permission. None = nothing was sent (held or refused)."""
    from infra import provider_gate
    if not provider_gate.ai_available(KIND) or not _policy_allowance(store, tenant, ws, cid, authority):
        return None                                           # disarmed, window over, or the role is not enabled: the conversation is kept, nothing is sent
    return _dispatch(store, tenant, ws, cid, ai_admission.POLICY_APPROVER, ai_admission.POLICY_ROLES)


def sync(store, tenant, ws, cid) -> dict | None:
    """Copy finished interpretation jobs into their pending assistant turns (idempotent)."""
    conv = chat.get(store, tenant, ws, cid)
    if conv is None:
        return None
    changed = False
    for t in conv["turns"]:
        if t["role"] != "assistant" or t["text"] or not t["job_id"]:
            continue
        try:
            rec = store.job_record(tenant, ws, t["job_id"])
        except TenantScopeError:
            continue
        status = rec.get("execution_status")
        if status == "COMPLETED" and isinstance(rec.get("execution_result"), dict):
            r = rec["execution_result"]
            u = {k: r.get(k) for k in ("status", "message", "questions", "fields", "assumptions", "problems", "changes", "out_of_scope",
                                       "sources", "safety", "limits_note", "purpose")}
            u["source"] = "model"
            u["ai_message"] = r.get("message")                  # the model's own sentence, kept for trace; the turn text is the server's statement of state
            u["ai_message_names_a_state"] = chat_safety.mentions_state_change(r.get("message"))     # extra layer: a flag for the audit; the turn text does not depend on it
            chat.fill_turn(store, tenant, ws, cid, t["seq"], chat_safety.preview_turn_text(u), u, t["seq"])
            changed = True
        elif status in ("FAILED", "CANCELLED"):
            reason = str(rec.get("execution_reason") or "failed")[:120]
            chat.fill_turn(store, tenant, ws, cid, t["seq"],
                           f"The AI reply could not be produced ({reason}). Nothing was changed; you can edit the fields yourself or ask again.",
                           {"status": "FAILED", "message": reason, "questions": [], "fields": None, "assumptions": [], "problems": [],
                            "source": "system"}, t["seq"])
            changed = True
    return chat.get(store, tenant, ws, cid) if changed else conv


def _dispatch(store, tenant, ws, cid, approver_id, approver_roles) -> str | None:
    """Create + approve one interpretation job for the last unanswered person turn. None when nothing to do / no allowance."""
    conv = chat.get(store, tenant, ws, cid)
    if conv is None or conv["status"] != "OPEN" or not conv["turns"] or conv["turns"][-1]["role"] != "user":
        return None
    if conv["ai_used"] >= _limits()["max_ai_turns_per_conversation"]:
        return None
    if not chat.claim_ai_turn(store, tenant, ws, cid):
        return None
    seq = conv["turns"][-1]["seq"]
    job_id = f"chatq-{cid[5:]}-{seq}"
    try:
        limit, reservation = _quota(store, tenant, ws)
        head = store.head(tenant, ws)
        store.create_job(tenant, ws, job_id, KIND, {"conversation_id": cid}, expected_version=head.version,
                         idempotency_key=f"job-create-{job_id}", max_concurrent_jobs=limit, budget_reservation=reservation,
                         requested_by=conv["created_by"])
        store.approve_job(tenant, ws, job_id, approver_id, tuple(sorted(approver_roles)))
        chat.add_turn(store, tenant, ws, cid, "assistant", "", None, job_id=job_id)
    except Exception:
        chat.release_ai_turn(store, tenant, ws, cid)
        raise
    return job_id


def _understanding_from_fields(fields: dict) -> dict:
    norm = normalise_brief_form(fields)
    ok = norm["fields"] is not None and bool(norm["fields"]["applies_to"])
    problems = list(norm["problems"]) + ([] if (norm["fields"] is None or norm["fields"]["applies_to"])
                                         else ["applies_to: say which kinds of case this applies to"])
    shown = norm["fields"] or {k: fields.get(k) for k in ("subject", "scope", "unit", "currency", "old_value", "new_value", "applies_to")}
    return {"status": "READY" if ok else "CLARIFY",
            "message": "Your edited details look complete. Check them, then confirm." if ok else "Some details are still missing or unclear.",
            "questions": problems, "fields": {k: (str(v) if k in ("old_value", "new_value") and v is not None else v) for k, v in shown.items()
                                              if k != "target_candidate_id"},
            "assumptions": [], "problems": problems, "source": "person"}


def _flag(store, tenant, ws, cid, account, text) -> None:
    """Record reason CODES only when the text tries to claim rights / skip controls / reach outside; the request itself is not blocked."""
    codes = chat_safety.codes(text)
    if codes:
        chat.audit(store, tenant, ws, "CHAT_REQUEST_FLAGGED", account, {"conversation_id": cid, "codes": codes})


def _guard(request, body_workspace):
    """-> (identity, store, error response)."""
    if not _origin_ok(request):
        return None, None, _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return None, None, _error("v2 unavailable", 503)
    return identity, store, None


def _handle(exc):
    if isinstance(exc, IdentityDenied):
        return _denied(exc)
    if isinstance(exc, PermissionDenied):
        return _error(f"permission denied: {exc}", 403)
    if isinstance(exc, chat.ChatError):
        return _error(exc.message, exc.status)
    from infra.worker.free_text_purposes import PurposeError
    if isinstance(exc, PurposeError):
        return _error(exc.message, exc.status)
    if isinstance(exc, (job_quota.JobQuotaError, job_quota.BudgetExceeded, ConcurrencyLimitExceeded)):
        from infra import provider_gate
        return provider_gate.refusal_for(exc) or _error(f"budget_exceeded: {exc}", 409)
    if isinstance(exc, job_quota.BudgetConfigError):
        return _error(f"budget config error: {exc}", 500)
    if isinstance(exc, StaleVersion):
        return _error("the workspace changed meanwhile; reload and try again", 409)
    if isinstance(exc, (TenantScopeError, StoreConflict)):
        return _error(str(exc), 409)
    raise exc


def _need_chat(authority, store, ws, cid) -> None:
    """The permission a conversation needs depends on what it is for (a change request, a question, a reply, feedback, a what-if)."""
    from infra.worker.free_text_purposes import START_PERMISSION
    conv = chat.get(store, authority["tenant_id"], ws, cid)
    purpose = (conv or {}).get("purpose") or "change_request"
    perms = ("intent.create",) if purpose == "change_request" else START_PERMISSION.get(purpose, ("intent.create",))
    if not any(_can(authority, p) for p in perms):
        raise PermissionDenied(perms[0])


def _text_ok(body, key="text"):
    text = body.get(key)
    if not isinstance(text, str) or not text.strip():
        return None, _error("text required", 400)
    text = text.strip()
    if len(text) > _limits()["max_user_turn_chars"]:
        return None, _error(f"a message is limited to {_limits()['max_user_turn_chars']} characters", 400)
    return text, None


@router.post("/v2/chat/start")
@db_route
def chat_start(request: Request):
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    if not isinstance(ws, str) or not ws:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, ws)          # sign-in and rights BEFORE the body is looked at
        require_human_actor(authority)
        purpose = body.get("purpose") or "change_request"
        from infra.worker import free_text_purposes as ftp
        context = None
        if purpose == "change_request":
            require(authority, "intent.create")
        else:
            if purpose not in ftp.PURPOSES:
                return _error("unknown purpose", 400)
            if not any(_can(authority, p) for p in ftp.START_PERMISSION[purpose]):
                raise PermissionDenied(ftp.START_PERMISSION[purpose][0])
        text, err = _text_ok(body)
        if err:
            return err
        tenant = authority["tenant_id"]
        if purpose != "change_request":
            context = ftp.validate_start(store, tenant, ws, purpose, body.get("context"))
        cid = chat.create(store, tenant, ws, authority["account_id"], text, store.head(tenant, ws), purpose=purpose, context=context)
        _flag(store, tenant, ws, cid, authority["account_id"], text)
        try:
            _start_ai(store, tenant, ws, cid, authority)      # the member's standing AI permission; held (not an error) when refused
        except (job_quota.JobQuotaError, job_quota.BudgetExceeded, ConcurrencyLimitExceeded, job_quota.BudgetConfigError):
            pass                                              # the conversation exists; "Ask the AI" shows the exact refusal
        return JSONResponse(_public(sync(store, tenant, ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.get("/v2/chat")
def chat_list(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        allv = _can(authority, "knowledge.confirm")
        return JSONResponse({"conversations": chat.list_for(store, authority["tenant_id"], workspace_id, authority["account_id"], all_visible=allv)},
                            headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.get("/v2/chat/{cid}")
def chat_get(request: Request, cid: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        conv = sync(store, authority["tenant_id"], workspace_id, cid)
        if conv is None or not _visible(authority, conv):
            return _error("conversation not found", 404)
        return JSONResponse(_public(conv, authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


def _mine(store, authority, ws, cid, *, need_open=True):
    conv = sync(store, authority["tenant_id"], ws, cid)
    if conv is None or conv["created_by"] != authority["account_id"]:
        raise chat.ChatError("not_found", "conversation not found", 404)
    if need_open and conv["status"] != "OPEN":
        raise chat.ChatError("closed", f"this request is {conv['status'].lower()}; start a new one", 409)
    return conv


@router.post("/v2/chat/{cid}/message")
@db_route
def chat_message(request: Request, cid: str):
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        _need_chat(authority, store, ws, cid)
        text, err = _text_ok(body)
        if err:
            return err
        tenant = authority["tenant_id"]
        conv = _mine(store, authority, ws, cid)
        if any(t["role"] == "assistant" and not t["text"] for t in conv["turns"]):
            return _error("the AI is still answering your previous message", 409)
        if sum(len(t["text"]) for t in conv["turns"]) + len(text) > load_config()[0]["limits"]["max_conversation_chars"]:
            return _error("this conversation is too long; start a new request", 409)
        from infra import provider_gate
        if provider_gate.ai_available(KIND):
            _policy_allowance(store, tenant, ws, cid, authority)   # standing `ai.use` permission: the quota is granted by policy, not per reply
        conv = chat.get(store, tenant, ws, cid)
        if _usable(conv):
            provider_gate.require_provider("intent.interpret")    # a quota would send this turn to the model: refuse before recording it
        chat.add_turn(store, tenant, ws, cid, "user", text, authority["account_id"])
        _flag(store, tenant, ws, cid, authority["account_id"], text)
        conv = chat.get(store, tenant, ws, cid)
        if _usable(conv):
            _dispatch(store, tenant, ws, cid, conv["ai_approved_by"], _approver_roles(store, tenant, ws, conv["ai_approved_by"]))
        return JSONResponse(_public(sync(store, tenant, ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


def _approver_roles(store, tenant, ws, account):
    """Follow-up turns run under the allowance the approver granted; the audit role marks that."""
    return ("chat_allowance",)


@router.post("/v2/chat/{cid}/allow")
@db_route
def chat_allow(request: Request, cid: str):
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    turns = body.get("turns", 1)
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        tenant = authority["tenant_id"]
        mine = chat.get(store, tenant, ws, cid)
        if mine is not None and mine["created_by"] == authority["account_id"]:
            # "Ask the AI": the owner starts the reply under their own standing permission (nothing is granted by a person).
            mine = sync(store, tenant, ws, cid)
            if mine["status"] != "OPEN":
                return _error(f"this request is {mine['status'].lower()}", 409)
            from infra import provider_gate
            provider_gate.require_provider("intent.interpret")
            if mine["ai_revoked_at"]:
                return _error("the AI permission of this conversation was withdrawn", 409)
            reason = ai_admission.block_reason(store, tenant, ws, authority)
            if reason is not None:
                return _error({"suspended": "AI use is switched off for your account in this workspace",
                               "sandbox_hold": "in this workspace a different person has to allow each AI run"}.get(reason, "permission denied: ai.use"),
                              409 if reason == "sandbox_hold" else 403)
            if mine["ai_used"] >= _limits()["max_ai_turns_per_conversation"]:
                return _error("this conversation has used all of its AI replies", 409)
            job_id = _start_ai(store, tenant, ws, cid, authority)
            out = _public(sync(store, tenant, ws, cid), authority)
            out["job_id"] = job_id
            return JSONResponse(out, headers=_NO_STORE)
        from infra.worker.sandbox_hold import is_sandbox
        if not is_sandbox(store, tenant, ws):
            return _error("AI replies are not granted one by one in this workspace: the person who made the request asks the AI under their own AI permission", 409)
        require(authority, "source.import")
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
            return _error("turns must be a positive integer", 400)
        lim = _limits()
        if turns > lim["max_allowance_grant"]:
            return _error(f"at most {lim['max_allowance_grant']} AI replies can be allowed at a time", 400)
        conv = sync(store, tenant, ws, cid)
        if conv is None or not _visible(authority, conv):
            return _error("conversation not found", 404)
        if conv["status"] != "OPEN":
            return _error(f"this request is {conv['status'].lower()}", 409)
        policy = store.workspace_policy(tenant, ws)
        require_distinct_approver(conv["created_by"], authority["account_id"], require_distinct=policy["require_distinct_approver"])
        cap = lim["max_ai_turns_per_conversation"]
        if conv["ai_used"] >= cap:
            return _error("this conversation has used all of its AI replies", 409)
        from infra import provider_gate
        provider_gate.require_provider("intent.interpret")        # before anything is granted or recorded
        grant = chat.grant_allowance(store, tenant, ws, cid, authority["account_id"], turns, cap, lim["grant_minutes"])
        chat.audit(store, tenant, ws, "CHAT_AI_ALLOWED", authority["account_id"],
                   {"conversation_id": cid, "replies_allowed": grant["ai_allowance"], "minutes": lim["grant_minutes"]})
        job_id = _dispatch(store, tenant, ws, cid, authority["account_id"], authority["roles"])
        out = _public(sync(store, tenant, ws, cid), authority)
        out["job_id"] = job_id
        out["granted"] = grant
        return JSONResponse(out, headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/chat/{cid}/edit")
@db_route
def chat_edit(request: Request, cid: str):
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    fields = body.get("fields")
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        _need_chat(authority, store, ws, cid)
        if not isinstance(fields, dict):
            return _error("fields must be an object", 400)
        if any(isinstance(v, str) and len(v) > 300 for v in fields.values()):
            return _error("a field value is too long", 400)
        tenant = authority["tenant_id"]
        _mine(store, authority, ws, cid)
        conv0 = chat.get(store, tenant, ws, cid)
        if (conv0 or {}).get("purpose", "change_request") != "change_request":
            from infra.worker import free_text_purposes as ftp
            pcfg, _sha = ftp.load_config()
            _ctx, mapping = ftp.build_context(store, tenant, ws, conv0["purpose"], conv0.get("context"), conv0.get("pinned_source_revision_id"), pcfg["limits"])
            typed = " \n".join(t["text"] for t in conv0["turns"] if t["role"] == "user") + " \n" + " ".join(str(v) for v in fields.values() if isinstance(v, (str, int, float)))
            u = ftp.understanding_from_fields(conv0["purpose"], fields, mapping, pcfg["limits"], typed)
        else:
            u = _understanding_from_fields({k: fields.get(k) for k in ("subject", "scope", "unit", "currency", "old_value", "new_value", "applies_to")})
        version = chat.next_version(store, tenant, ws, cid)
        chat.add_turn(store, tenant, ws, cid, "edit", "You edited the details.", authority["account_id"], understanding=u, version=version)
        return JSONResponse(_public(chat.get(store, tenant, ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/chat/{cid}/confirm")
@db_route
def chat_confirm(request: Request, cid: str):
    body = _json_body_sync(request)
    ws, version = body.get("workspace_id"), body.get("version")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        _need_chat(authority, store, ws, cid)
        if not isinstance(version, int) or isinstance(version, bool):
            return _error("version required (the understanding you are confirming)", 400)
        tenant = authority["tenant_id"]
        conv = _mine(store, authority, ws, cid)
        u = conv["understanding"]
        if u is None or conv["version"] != version:
            return _error("this understanding is no longer the latest; reload", 409)
        if u.get("status") != "READY":
            return _error("this understanding is not complete yet; answer the questions or edit the details", 409)
        if (u.get("assumptions") or u.get("out_of_scope")) and body.get("acknowledged") is not True:
            return _error("acknowledge_required: check what the AI assumed and what is left out, then confirm", 409)
        if conv.get("purpose", "change_request") != "change_request":
            from infra.worker import free_text_purposes as ftp
            from infra.adapters.v2_policy import permissions_for
            outcome = ftp.outcome_for(conv["purpose"], u, set(permissions_for(authority.get("roles"))))
            outcome["version"] = version
            chat.set_outcome(store, tenant, ws, cid, outcome)
            chat.set_status(store, tenant, ws, cid, "CONFIRMED")
            chat.add_turn(store, tenant, ws, cid, "system", chat_safety.confirmed_preview_text(outcome), authority["account_id"])     # written after the save, by the server
            chat.audit(store, tenant, ws, "CHAT_PREVIEW_CONFIRMED", authority["account_id"],
                       {"conversation_id": cid, "purpose": conv["purpose"], "version": version, "action": outcome["action"]})
            return JSONResponse(_public(chat.get(store, tenant, ws, cid), authority), headers=_NO_STORE)
        norm = normalise_brief_form(u["fields"])
        if norm["fields"] is None or not norm["fields"]["applies_to"]:
            return _error("the details are incomplete", 409)
        first = next(t["text"] for t in conv["turns"] if t["role"] == "user")
        form = {**u["fields"]}
        key = "chatconf-" + hashlib.sha256(f"{cid}:{version}".encode()).hexdigest()[:20]
        parsed = store.parse_intent(tenant, ws, actor_id=authority["account_id"], raw_request=first, structured_form=form, idempotency_key=key)
        old_intent = conv.get("intent_id")
        chat.set_status(store, tenant, ws, cid, "CONFIRMED", parsed.get("intent_id"))
        chat.add_turn(store, tenant, ws, cid, "system", chat_safety.confirmed_request_text(parsed.get("intent_id"), parsed.get("status")), authority["account_id"])
        if old_intent and parsed.get("intent_id") and old_intent != parsed.get("intent_id"):
            # the old request and its downstream work are not reused: write that down on the server (not in a page)
            from infra import v2_lineage
            v2_lineage.record_supersede(store, tenant, ws, cid, old_intent, parsed["intent_id"], authority["account_id"])
        chat.audit(store, tenant, ws, "CHAT_UNDERSTANDING_CONFIRMED", authority["account_id"],
                   {"conversation_id": cid, "version": version, "intent_id": parsed.get("intent_id")})
        out = _public(chat.get(store, tenant, ws, cid), authority)
        out["intent_id"], out["intent_status"] = parsed.get("intent_id"), parsed.get("status")
        return JSONResponse(out, headers=_NO_STORE)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/chat/{cid}/cancel")
@db_route
def chat_cancel(request: Request, cid: str):
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        _need_chat(authority, store, ws, cid)
        _mine(store, authority, ws, cid)
        chat.set_status(store, authority["tenant_id"], ws, cid, "CANCELLED")
        return JSONResponse(_public(chat.get(store, authority["tenant_id"], ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/chat/{cid}/revoke")
@db_route
def chat_revoke(request: Request, cid: str):
    """Withdraw the unused AI permission. The requester may stop their own conversation; a knowledge.confirm holder may stop any."""
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        require(authority, "project.read")
        tenant = authority["tenant_id"]
        conv = sync(store, tenant, ws, cid)
        if conv is None or not _visible(authority, conv):
            return _error("conversation not found", 404)
        chat.revoke(store, tenant, ws, cid, authority["account_id"])
        chat.audit(store, tenant, ws, "CHAT_AI_REVOKED", authority["account_id"], {"conversation_id": cid})
        return JSONResponse(_public(chat.get(store, tenant, ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/chat/{cid}/reopen")
@db_route
def chat_reopen(request: Request, cid: str):
    """The requester wants to change a request they already confirmed. The saved request stays as it is; confirming again makes a NEW
    request, and everything done for the old one (draft, impact, simulation, ...) is out of date and is not reused."""
    body = _json_body_sync(request)
    ws = body.get("workspace_id")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        require(authority, "intent.create")
        tenant = authority["tenant_id"]
        conv = _mine(store, authority, ws, cid, need_open=False)
        if conv["status"] != "CONFIRMED":
            return _error("only a confirmed request can be reopened", 409)
        chat.set_status(store, tenant, ws, cid, "OPEN")
        chat.add_turn(store, tenant, ws, cid, "system", "You reopened this request to change it. The saved request stays as it is until you confirm "
                      "again; the steps already done for it will be out of date after you do, and you have to confirm the new request.",
                      authority["account_id"])
        chat.audit(store, tenant, ws, "CHAT_REOPENED", authority["account_id"], {"conversation_id": cid, "intent_id": conv["intent_id"]})
        return JSONResponse(_public(chat.get(store, tenant, ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.get("/v2/intent/{intent_id}/lineage")
def intent_lineage(request: Request, intent_id: str, workspace_id: str):
    """Which request replaced this one (or which one this replaced) and the downstream work that is out of date. Read-only."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra import v2_lineage
        return JSONResponse(v2_lineage.lineage(store, authority["tenant_id"], workspace_id, intent_id), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)


@router.get("/v2/change/draft-lineage")
def draft_lineage(request: Request, workspace_id: str, draft_job_id: str):
    """Which draft this one revises, the newest draft that replaced it and the downstream work of older drafts that is out of date. Read-only."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra import v2_lineage
        return JSONResponse(v2_lineage.draft_lineage(store, authority["tenant_id"], workspace_id, draft_job_id), headers=_NO_STORE)
    except TenantScopeError:
        return _error("draft not found", 404)
    except Exception as exc:
        return _handle(exc)


@router.post("/v2/chat/{cid}/executed")
@db_route
def chat_executed(request: Request, cid: str):
    """The person pressed the existing guarded action that follows a CONFIRMED preview (an answer job, a comment, a what-if run).
    This only remembers that it was done, so a reload does not offer the same action again; it grants and executes nothing."""
    body = _json_body_sync(request)
    ws, ref = body.get("workspace_id"), body.get("ref")
    identity, store, err = _guard(request, ws)
    if err:
        return err
    try:
        authority = _authority(identity, request, ws)
        require_human_actor(authority)
        _need_chat(authority, store, ws, cid)
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 200:
            return _error("ref required", 400)
        tenant = authority["tenant_id"]
        conv = _mine(store, authority, ws, cid, need_open=False)
        if conv["status"] != "CONFIRMED" or not conv.get("outcome"):
            return _error("only a confirmed preview can be marked as done", 409)
        ref = ref.strip()
        verified = None
        if conv["outcome"].get("action") == "gap_reply" and conv["outcome"].get("kind") == "decision":
            # the server states "recorded" only for a decision it really holds: this person's confirm decision on THIS open point of THIS draft
            with store._tx() as cur:
                cur.execute("""SELECT epoch FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s AND decision_id=%s AND action='confirm'
                                 AND actor_id=%s AND target=%s""", (tenant, ws, ref, authority["account_id"], conv["outcome"].get("gap_id")))
                row = cur.fetchone()
            if row is None:
                return _error("no such recorded decision by you on this open point; nothing is marked as recorded", 409)
            verified = row[0]
        outcome = {**conv["outcome"], "executed": {"ref": ref, "by": authority["account_id"], **({"epoch": verified} if verified is not None else {})}}
        chat.set_outcome(store, tenant, ws, cid, outcome)
        if verified is not None:
            chat.add_turn(store, tenant, ws, cid, "system", f"Recorded by the server: your decision {ref} (epoch {verified}). The earlier draft is now out of date; revise it to use this decision.",
                          authority["account_id"])
        chat.audit(store, tenant, ws, "CHAT_PREVIEW_ACTION_DONE", authority["account_id"], {"conversation_id": cid, "action": outcome.get("action"), "ref": ref.strip()})
        return JSONResponse(_public(chat.get(store, tenant, ws, cid), authority), headers=_NO_STORE)
    except Exception as exc:
        return _handle(exc)
