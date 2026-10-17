"""GAP clarification routes (see infra/v2_gaps.py for the model): read the GAPs of a draft, confirm an answer.

Comments reuse `POST /v2/hitl/answer`; model suggestions reuse `gap.answer` jobs. Only `confirm` is added here, and it is a
durable human decision: `knowledge.confirm`, human actor, bound to the draft/intent/epoch, appended to the decision ledger
(who/what/why). It edits no source file, closes no finding and is not a model call.
"""

from __future__ import annotations

import hashlib
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, require, require_human_actor
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_gaps import MAX_ANSWER, SCOPE_KIND, build_gaps, gap_id
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _question_of(item: dict) -> str:
    """What the decision remembers of the open point. For an `unaddressed` point the text ended with a passage of the SOURCE: that passage is not copied into the decision."""
    text = str(item.get("text") or "")
    mark = ": not classified by the model"
    return text.split(mark, 1)[0] + mark if item.get("kind") == "unaddressed" and mark in text else text


@router.get("/v2/change/gaps")
def change_gaps(request: Request, workspace_id: str, draft_job_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        result = build_gaps(store, authority["tenant_id"], workspace_id, draft_job_id)
        if result is None:
            return _error("not a change.draft job of this workspace", 404)
        from infra.v2_redaction import redact_gaps
        return JSONResponse(redact_gaps(result, authority), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/change/gap/confirm")
async def gap_confirm(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, draft_job_id, key = body.get("workspace_id"), body.get("draft_job_id"), body.get("gap_key")
    answer, rationale = body.get("answer"), body.get("rationale")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)       # sign-in and rights before the body is validated
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        if not all(isinstance(x, str) and x.strip() for x in ((draft_job_id, key) if body.get("chat_id") is not None else (draft_job_id, key, answer))):
            return _error("workspace_id, draft_job_id, gap_key and answer required", 400)
        answer = answer.strip() if isinstance(answer, str) else ""
        extra: dict = {"provenance": "typed directly into the decision form by the person", "typed_text": answer}
        chat_id = body.get("chat_id")
        if chat_id is not None:
            # the words come from the CONFIRMED preview the server holds, never from the client: the person's verbatim text is the decision's text,
            # the AI wording and the files they named travel beside it
            from infra import v2_chat_store as chat
            conv = chat.get(store, authority["tenant_id"], workspace_id, chat_id) if isinstance(chat_id, str) else None
            out = (conv or {}).get("outcome") or {}
            if (conv is None or conv.get("created_by") != authority["account_id"] or conv.get("purpose") != "gap_reply" or conv.get("status") != "CONFIRMED"
                    or out.get("action") != "gap_reply" or out.get("kind") != "decision" or out.get("draft_job_id") != draft_job_id or out.get("gap_key") != key):
                return _error("chat_id is not a confirmed decision preview of yours for this open point", 409)
            answer = str(out.get("typed_text") or out.get("answer") or "").strip()
            extra = {"provenance": "typed by the person in a chat and confirmed in the preview; AI wording is supplementary", "chat_id": chat_id,
                     "chat_version": out.get("version"), "typed_text": answer, "ai_wording": out.get("ai_wording") or out.get("answer"),
                     "files": out.get("files") or [], "source_revision_id": out.get("source_revision_id")}
        if len(answer) > MAX_ANSWER:
            return _error(f"answer is limited to {MAX_ANSWER} characters", 400)
        if rationale is not None and not isinstance(rationale, str):
            return _error("rationale must be text", 400)
        from infra.worker.gap_answer import load_frozen_config
        if any(re.search(p, answer, re.I) for p in load_frozen_config()[0]["injection_patterns"]):
            return _error("the answer reads like an instruction to the model; state the decision as plain text", 400)
        tenant = authority["tenant_id"]
        gaps = build_gaps(store, tenant, workspace_id, draft_job_id)
        if gaps is None or not gaps["completed"]:
            return _error("not a completed change.draft job of this workspace", 404)
        item = next((g for g in gaps["gaps"] if g["key"] == key), None)
        if item is None:
            return _error("this draft has no such open point", 404)
        head = store.head(tenant, workspace_id)
        if any("source revision moved" in r for r in gaps.get("stale_reasons") or []):
            return _error("the source moved after this draft; generate a new draft first", 409)
        gid = gap_id(draft_job_id, key)
        digest = hashlib.sha256(answer.encode()).hexdigest()[:12]
        result = store.record_human_decision(
            tenant, workspace_id, actor_id=authority["account_id"], current_permission="knowledge.confirm",
            target=gid, action="confirm", expected_version=head.version,
            idempotency_key=f"gapconfirm-{gid}-{authority['account_id']}-{digest}",
            rationale=(rationale or "").strip() or None,
            scope={"kind": SCOPE_KIND, "intent_id": gaps["intent_id"], "draft_job_id": draft_job_id, "key": key,
                   "question": _question_of(item)[:300], "answer": answer, "pinned_source_revision_id": head.source_revision_id, **extra})
        return JSONResponse({"decision_id": result["decision_id"], "decision_epoch": result["decision_epoch"],
                             "gap_id": gid, "note": "Recorded as a human decision. Earlier drafts are now stale; the next "
                                                    "draft will use it as a stated assumption. No source file was changed."},
                            headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except StaleVersion:
        return _error("the workspace changed while you were deciding; reload and try again", 409)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)
