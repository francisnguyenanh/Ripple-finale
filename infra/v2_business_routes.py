"""Business knowledge routes (contract fkl/1): the graph, the gap queue and the deterministic analysis that proposes relations and gaps.

Reads need `project.read`. `POST /v2/business/analyse` needs a human actor with `knowledge.confirm`; it writes only a PENDING_HUMAN proposal, never an accepted
snapshot, never a decision, and makes no model call."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.change_intelligence.v2 import business_knowledge as bk
from infra.adapters import v2_business
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import StaleAuthorityError, StaleVersion, StoreConflict, TenantScopeError
from infra.v2_policy_routes import _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _read(request, workspace_id, build):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return JSONResponse(build(store, authority["tenant_id"]), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/business/graph")
def business_graph(request: Request, workspace_id: str, focus: str | None = None):
    return _read(request, workspace_id, lambda store, tenant: v2_business.graph_view(store, tenant, workspace_id, focus=focus))


@router.get("/v2/business/gaps")
def business_gaps(request: Request, workspace_id: str):
    return _read(request, workspace_id, lambda store, tenant: v2_business.gap_queue(store, tenant, workspace_id))


@router.post("/v2/business/analyse")
async def business_analyse(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        head = store.head(authority["tenant_id"], workspace_id)
        result = v2_business.analyse_workspace(store, authority["tenant_id"], workspace_id, actor_id=authority["account_id"],
                                               idempotency_key=f"analyse-{head.version}-{head.source_revision_id}-{head.decision_epoch}")
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)


async def _answer_call(request: Request, confirm: bool):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, gap_id, answer = body.get("workspace_id"), body.get("gap_id"), body.get("answer")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)       # sign-in and rights before the body is looked at
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        if not (isinstance(gap_id, str) and gap_id.strip() and isinstance(answer, dict)):
            return _error("gap_id and answer required", 400)
        tenant = authority["tenant_id"]
        if confirm:
            result = v2_business.confirm_answer(store, tenant, workspace_id, actor_id=authority["account_id"], gap_id=gap_id, answer=answer,
                                                quote=body.get("quote"), preview_hash=body.get("preview_hash"))
        else:
            result = v2_business.preview_answer(store, tenant, workspace_id, gap_id=gap_id, answer=answer)
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except bk.ContractError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=422, headers=_NO_STORE)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/business/gaps/answer-preview")
async def business_answer_preview(request: Request):
    return await _answer_call(request, confirm=False)


@router.post("/v2/business/gaps/answer")
async def business_answer(request: Request):
    return await _answer_call(request, confirm=True)


@router.post("/v2/business/gaps/propose")
async def business_gap_proposals(request: Request):
    """Ask a model to SUGGEST gaps and relations over the knowledge version people are working on (KNO-06). Suggestions only: they are shown labelled in the gap queue
    and the graph and change nothing until a person answers. Needs a human actor with `knowledge.confirm` and `ai.use`; the same version and attempt is one job."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, attempt = body.get("workspace_id"), body.get("attempt", 1)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        require(authority, "ai.use")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
            return _error("attempt (1-3) required", 400)
        tenant = authority["tenant_id"]
        _, snap = v2_business._latest_view(store, tenant, workspace_id)
        if snap is None:
            return JSONResponse({"error": "There is no business knowledge to look at yet.", "code": "no_snapshot"}, status_code=409, headers=_NO_STORE)
        from infra.worker.knowledge_gap_proposals import KIND, job_id_for
        jid = job_id_for(snap["snapshot"]["snapshot_id"], attempt)
        try:
            rec = store.job_record(tenant, workspace_id, jid)
        except TenantScopeError:
            rec = None
        if rec is None:
            from infra import ai_admission, provider_gate
            provider_gate.require_provider(KIND)
            limit, reservation = _quota_for(KIND)
            head = store.head(tenant, workspace_id)
            payload = {"snapshot_id": snap["snapshot"]["snapshot_id"], "evidence_access": "evidence.read" in permissions_for(authority.get("roles")), "attempt": attempt}
            store.create_job(tenant, workspace_id, jid, KIND, payload, expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit,
                             budget_reservation=reservation, requested_by=authority["account_id"])
            ai_admission.admit(store, tenant, workspace_id, jid, KIND, authority)
            rec = store.job_record(tenant, workspace_id, jid)
        from infra.adapters import v2_business_proposals
        return JSONResponse({"snapshot_id": snap["snapshot"]["snapshot_id"], "attempt": attempt, "job_id": jid, "status": v2_business_proposals._state(rec),
                             "reason": rec.get("execution_reason") if rec.get("execution_status") == "FAILED" else None}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _refusal(exc)


def _interpret_shell(request, workspace_id, action, *, mutating=True):
    """Origin (for a POST), sign-in, human actor, `knowledge.confirm` + `ai.use`, then `action(store, tenant, authority)`; the person who may confirm is the one who may ask for a reading."""
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        require(authority, "ai.use")
        return action(store, authority["tenant_id"], authority)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _refusal(exc)


@router.post("/v2/business/gaps/{gap_id}/interpret")
async def business_gap_interpret(request: Request, gap_id: str):
    """Ask a model to read a person's free-text answer to one open gap into the question's structured form (KNO-07). The reading is a proposal with a preview; the
    person confirms through `POST /v2/business/gaps/answer` as with the controls. The same version, gap and text is one job."""
    body = await _json_body(request)
    workspace_id, text = body.get("workspace_id"), body.get("text")

    def action(store, tenant, authority):
        from infra.worker.gap_answer_interpret import KIND, job_id_for, validate_payload
        _, snap = v2_business._latest_view(store, tenant, workspace_id)
        if snap is None:
            return JSONResponse({"error": "There is no business knowledge to answer against.", "code": "no_snapshot"}, status_code=409, headers=_NO_STORE)
        payload = {"gap_id": gap_id, "text": text if isinstance(text, str) else "", "snapshot_id": snap["snapshot"]["snapshot_id"]}
        if validate_payload(payload):
            return _error("text (1-1200 characters) required", 400)
        open_ids = {g["gap_id"] for g in v2_business.gap_queue(store, tenant, workspace_id)["gaps"]}
        if gap_id not in open_ids:
            raise TenantScopeError("open question not found")
        jid = job_id_for(payload["snapshot_id"], gap_id, payload["text"])
        try:
            rec = store.job_record(tenant, workspace_id, jid)
        except TenantScopeError:
            rec = None
        if rec is None:
            from infra import ai_admission, provider_gate
            provider_gate.require_provider(KIND)
            limit, reservation = _quota_for(KIND)
            head = store.head(tenant, workspace_id)
            store.create_job(tenant, workspace_id, jid, KIND, payload, expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit,
                             budget_reservation=reservation, requested_by=authority["account_id"])
            ai_admission.admit(store, tenant, workspace_id, jid, KIND, authority)
            rec = store.job_record(tenant, workspace_id, jid)
        return JSONResponse({"gap_id": gap_id, "job_id": jid, **_qa_state(rec)}, headers=_NO_STORE)
    return _interpret_shell(request, workspace_id, action)


@router.get("/v2/business/gaps/{gap_id}/interpret/{job_id}")
def business_gap_interpret_result(request: Request, gap_id: str, job_id: str, workspace_id: str):
    """The reading of one interpretation job: `status` (running/done/failed), and when done the proposed answer, quote, uncovered words, message and preview."""
    def action(store, tenant, authority):
        rec = store.job_record(tenant, workspace_id, job_id)
        if rec.get("kind") != "gap.answer_interpret" or (rec.get("payload") or {}).get("gap_id") != gap_id:
            raise TenantScopeError("interpretation not found")
        out = {"gap_id": gap_id, "job_id": job_id, **_qa_state(rec), "reading": None}
        if out["status"] == "done" and rec.get("execution_result"):
            res = rec["execution_result"]
            out["reading"] = {k: res[k] for k in ("status", "proposed_answer", "quote", "uncovered", "message", "preview", "text")}
            out["reading"]["statement"] = ("This is how a model read the answer; the preview shows exactly what confirming it would change. Confirming records the person's own words, "
                                           "not the model's reading, as the decision.")
        elif out["status"] == "failed":
            words = {"authority_revoked": "the person who asked no longer has permission", "snapshot_moved": "the knowledge changed after the answer was written",
                     "gap_not_open": "the question is no longer open", "schema_violation": "the answer could not be used", "budget_exceeded": "the answer was too large to send"}
            key = next((k for k in words if (out["reason"] or "").startswith(k)), None)
            out["message"] = "The reading did not finish" + (f": {words[key]}" if key else "") + ". The structured controls still work."
        return JSONResponse(out, headers=_NO_STORE)
    return _interpret_shell(request, workspace_id, action, mutating=False)


@router.get("/v2/business/knowledge")
def business_knowledge_status(request: Request, workspace_id: str):
    return _read(request, workspace_id, lambda store, tenant: v2_business.knowledge_status(store, tenant, workspace_id))


@router.get("/v2/business/diff")
def business_knowledge_diff(request: Request, workspace_id: str, from_revision: int, to_revision: int):
    def build(store, tenant):
        try:
            return v2_business.business_diff(store, tenant, workspace_id, from_revision, to_revision)
        except v2_business.BusinessKnowledgeRefused as exc:
            raise TenantScopeError(str(exc)) from exc            # an unknown revision reads as "not found", like any other scoped read
    return _read(request, workspace_id, build)


async def _decide_call(request: Request, action):
    """Shared shell of the two human decisions below: origin, sign-in, human actor, knowledge.confirm, then `action(store, tenant, account, body)`."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        return JSONResponse(action(store, authority["tenant_id"], authority["account_id"], body), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except bk.ContractError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=422, headers=_NO_STORE)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/business/accept")
async def business_accept(request: Request):
    def action(store, tenant, account, body):
        snapshot_id = body.get("snapshot_id")
        if not (isinstance(snapshot_id, str) and snapshot_id.strip()):
            raise bk.ContractError("invalid_field", "snapshot_id required")
        return v2_business.accept_snapshot(store, tenant, body["workspace_id"], actor_id=account, snapshot_id=snapshot_id,
                                           acknowledged_open_gaps=body.get("acknowledged_open_gaps") is True)
    return await _decide_call(request, action)


@router.post("/v2/business/decisions/revoke")
async def business_revoke(request: Request):
    def action(store, tenant, account, body):
        return v2_business.revoke_decision(store, tenant, body["workspace_id"], actor_id=account, decision_id=body.get("decision_id") or "", reason=body.get("reason"))
    return await _decide_call(request, action)


@router.post("/v2/business/context")
async def business_context(request: Request):
    """Context pack for a business question, pinned to one snapshot. Business items need `project.read`; source text and quotes need `evidence.read`."""
    if not _origin_ok(request):                       # a read-only POST is still a cookie-authenticated POST
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, query, view = body.get("workspace_id"), body.get("query"), body.get("view", "target")
    top_k = body.get("top_k", 40)
    if not (isinstance(workspace_id, str) and workspace_id.strip() and isinstance(query, str) and query.strip()):
        return _error("workspace_id and query required", 400)
    if type(top_k) is not int or not 1 <= top_k <= 40:
        return _error("top_k must be an integer from 1 to 40", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra.adapters import v2_business_index
        # A read route never calls a PAID provider: an external semantic provider runs only inside the knowledge.qa job, under its reservation and call budget (contract
        # section 12; the adapter refuses it here with `outside_budget_boundary`). Disarmed and synthetic replay providers cost nothing and may label the pack.
        pack = v2_business_index.context_pack(store, authority["tenant_id"], workspace_id, query, view=view, top_k=top_k, semantic=body.get("semantic", True) is not False,
                                              can_read_evidence="evidence.read" in permissions_for(authority.get("roles")))
        return JSONResponse(pack, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=422, headers=_NO_STORE)


# ---- business questions (QRY-03) -------------------------------------------------------------------------------------------------------------

def _config_fingerprint(kind: str) -> str:
    """What a stored result is bound to besides its inputs: the frozen prompt/schema of the role and the pinned model (and the semantic adapter identity for QA). The same
    inputs under a changed configuration are a NEW job, never a replay of the old answer; a retry of the same job keeps its binding (the job id does not change)."""
    import hashlib
    from infra.worker import budget_config as _bc, job_quota
    sha = {"knowledge.qa": lambda: __import__("infra.worker.knowledge_qa", fromlist=["load_config"]).load_config()[1],
           "change.understand": lambda: __import__("infra.worker.change_understand", fromlist=["load_config"]).load_config()[1]}[kind]()
    model = None
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        config = job_quota.load_worker_budget_config()
        model = {"model": _bc.model_for_role(config, kind), "thinking": _bc.thinking_level_for_role(config, kind)}
    extra = None
    if kind == "knowledge.qa":
        from infra.adapters import v2_business_index
        adapter = v2_business_index.semantic_adapter()
        extra = {"provider": adapter.provider.provider_id, "config_version": adapter.provider.config_version} if getattr(adapter.provider, "external", False) else None
    return hashlib.sha256(json.dumps({"config": sha, "model": model, "semantic": extra}, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _qa_job_id(workspace_id, snapshot_id, view, question, evidence, follow_up) -> str:
    import hashlib
    key = json.dumps([workspace_id, snapshot_id, view, " ".join(question.lower().split()), bool(evidence), follow_up or "", _config_fingerprint("knowledge.qa")], sort_keys=True)
    return "qa-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _qa_state(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st)
    if status is None:
        status = "waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running"
    return {"status": status, "reason": rec.get("execution_reason") if st == "FAILED" else None}


def _quota_for_qa():
    from infra.worker import job_quota, knowledge_qa
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        raise job_quota.BudgetConfigError("live knowledge.qa requires a budget config")
    config = job_quota.load_worker_budget_config()
    job_quota.check_job_admission(config, job_kind="knowledge.qa", real_calls_per_attempt=knowledge_qa.planned_calls())   # QA call + the semantic calls an armed adapter may make
    return job_quota.load_concurrency_limit(), job_quota.compute_budget_reservation(config)


@router.post("/v2/business/questions")
async def ask_business_question(request: Request):
    """Ask a business question about the accepted knowledge (view target) or what the sources say (view observed). Idempotent per snapshot, view, question and access scope:
    the same question again returns the stored answer and makes no model call. Needs `knowledge.answer` and `ai.use`; a model call is admitted by the member policy."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, question, view, follow_up = body.get("workspace_id"), body.get("question"), body.get("view", "target"), body.get("follow_up_of")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "knowledge.answer")
        require(authority, "ai.use")
        from infra.worker import knowledge_qa
        err = knowledge_qa.validate_payload({"question": question, "view": view, "snapshot_id": "x", "evidence_access": False})
        if not isinstance(workspace_id, str) or err:
            return _error(f"knowledge.qa: {err or 'workspace_id required'}", 400)
        if follow_up is not None and not (isinstance(follow_up, str) and follow_up.startswith("qa-")):
            return _error("knowledge.qa: follow_up_of must be an earlier question id", 400)
        tenant = authority["tenant_id"]
        working = v2_business._working(v2_business.business_view(store, tenant, workspace_id))
        if working is None or (view == "target" and working["snapshot"]["lifecycle_state"] != "ACCEPTED"):
            return JSONResponse({"error": "There is no accepted business knowledge to ask about yet.", "code": "no_knowledge"}, status_code=409, headers=_NO_STORE)
        snapshot_id = working["snapshot"]["snapshot_id"]
        evidence = "evidence.read" in permissions_for(authority.get("roles"))
        jid = _qa_job_id(workspace_id, snapshot_id, view, question, evidence, follow_up)
        try:
            rec = store.job_record(tenant, workspace_id, jid)
        except TenantScopeError:
            rec = None
        if rec is None:
            from infra import provider_gate
            provider_gate.require_provider("knowledge.qa")
            limit, reservation = _quota_for_qa()
            head = store.head(tenant, workspace_id)
            payload = {"question": question.strip(), "view": view, "snapshot_id": snapshot_id, "evidence_access": evidence}
            if follow_up:
                payload["follow_up_of"] = follow_up
            store.create_job(tenant, workspace_id, jid, "knowledge.qa", payload, expected_version=head.version, idempotency_key=f"job-create-{jid}",
                             max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
            from infra import ai_admission
            ai_admission.admit(store, tenant, workspace_id, jid, "knowledge.qa", authority)
            rec = store.job_record(tenant, workspace_id, jid)
        return JSONResponse({"question_id": jid, "snapshot_id": snapshot_id, **_qa_state(rec)}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        from infra import provider_gate
        from infra.adapters.v2_postgres import ConcurrencyLimitExceeded
        from infra.worker import job_quota
        if isinstance(exc, (job_quota.JobQuotaError, job_quota.BudgetExceeded, ConcurrencyLimitExceeded)):
            return provider_gate.refusal_for(exc) or _error(f"budget_exceeded: {exc}", 409)
        if isinstance(exc, job_quota.BudgetConfigError):
            return _error(f"budget config error: {exc}", 500)
        if isinstance(exc, (StaleVersion, StoreConflict)):
            return _error(str(exc), 409)
        raise


@router.get("/v2/business/questions/{question_id}")
def read_business_answer(request: Request, question_id: str, workspace_id: str, mode: str = "business"):
    """The stored answer as THIS reader may see it. Never calls a model. Source-text claims are withheld without `evidence.read`; Technical mode adds ids, pin and validation."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if mode not in ("business", "technical") or not question_id.startswith("qa-"):
            return _error("mode must be business or technical", 400)
        tenant = authority["tenant_id"]
        rec = store.job_record(tenant, workspace_id, question_id)
        if rec.get("kind") != "knowledge.qa":
            raise TenantScopeError("question not found")
        out = {"question_id": question_id, "question": (rec.get("payload") or {}).get("question"), **_qa_state(rec)}
        result = rec.get("execution_result") if rec.get("execution_status") == "COMPLETED" else None
        if result:
            from core.change_intelligence.v2 import business_qa
            working = v2_business._working(v2_business.business_view(store, tenant, workspace_id))
            newer = bool(working and working["snapshot"]["snapshot_id"] != result["pack"]["pin"]["snapshot_id"])
            can_evidence = "evidence.read" in permissions_for(authority.get("roles"))
            out["answer"] = business_qa.render(result, mode=mode, can_read_evidence=can_evidence, titles=result.get("pack_titles", {}), newer_knowledge=newer)
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


# ---- business change requests (CHG-01) ---------------------------------------------------------------------------------------------------------------

def _quota_for(kind: str):
    from infra.worker import job_quota
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        raise job_quota.BudgetConfigError(f"live {kind} requires a budget config")
    config = job_quota.load_worker_budget_config()
    job_quota.check_job_admission(config, job_kind=kind)
    return job_quota.load_concurrency_limit(), job_quota.compute_budget_reservation(config)


def _refusal(exc):
    from infra import provider_gate
    from infra.adapters.v2_postgres import ConcurrencyLimitExceeded
    from infra.worker import job_quota
    if isinstance(exc, (job_quota.JobQuotaError, job_quota.BudgetExceeded, ConcurrencyLimitExceeded)):
        return provider_gate.refusal_for(exc) or _error(f"budget_exceeded: {exc}", 409)
    if isinstance(exc, job_quota.BudgetConfigError):
        return _error(f"budget config error: {exc}", 500)
    if isinstance(exc, (StaleVersion, StoreConflict)):
        return _error(str(exc), 409)
    raise exc


@router.post("/v2/business/changes")
async def request_business_change(request: Request):
    """State a change to a business rule in words (an actor, a condition, an exception, a scope, a value, or several at once). The server understands it as validated operations over
    the accepted knowledge and shows what it understood; nothing changes until a person confirms, and even then only a request is recorded."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, text = body.get("workspace_id"), body.get("request")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "intent.create")
        require(authority, "ai.use")
        from infra.worker import change_understand
        err = change_understand.validate_payload({"request": text, "snapshot_id": "x"})
        if not isinstance(workspace_id, str) or err:
            return _error(f"change.understand: {err or 'workspace_id required'}", 400)
        tenant = authority["tenant_id"]
        view = v2_business.business_view(store, tenant, workspace_id)
        if view["accepted"] is None:
            return JSONResponse({"error": "There is no accepted business knowledge to change yet.", "code": "no_knowledge"}, status_code=409, headers=_NO_STORE)
        snapshot_id = view["accepted"]["snapshot"]["snapshot_id"]
        import hashlib
        jid = "chg-" + hashlib.sha256(json.dumps([workspace_id, snapshot_id, " ".join(text.lower().split()), authority["account_id"], _config_fingerprint("change.understand")]).encode("utf-8")).hexdigest()[:24]
        try:
            rec = store.job_record(tenant, workspace_id, jid)
        except TenantScopeError:
            rec = None
        if rec is None:
            from infra import provider_gate
            provider_gate.require_provider("change.understand")
            limit, reservation = _quota_for("change.understand")
            head = store.head(tenant, workspace_id)
            store.create_job(tenant, workspace_id, jid, "change.understand", {"request": text.strip(), "snapshot_id": snapshot_id}, expected_version=head.version,
                             idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
            from infra import ai_admission
            ai_admission.admit(store, tenant, workspace_id, jid, "change.understand", authority)
            rec = store.job_record(tenant, workspace_id, jid)
        return JSONResponse({"change_id": jid, "snapshot_id": snapshot_id, **_qa_state(rec)}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _refusal(exc)


@router.get("/v2/business/changes/{change_id}")
def read_business_change(request: Request, change_id: str, workspace_id: str, mode: str = "business"):
    """What the server understood of the request. Visible to the person who asked and to people who may confirm knowledge. Never calls a model."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if mode not in ("business", "technical") or not change_id.startswith("chg-"):
            return _error("mode must be business or technical", 400)
        tenant = authority["tenant_id"]
        rec = store.job_record(tenant, workspace_id, change_id)
        if rec.get("kind") != "change.understand":
            raise TenantScopeError("change request not found")
        perms = permissions_for(authority.get("roles"))
        if rec.get("requested_by") != authority["account_id"] and not {"knowledge.confirm", "change.approve"} & perms:      # the asker, a confirmer or a person who may decide the change
            raise PermissionDenied("this request belongs to someone else")
        out = {"change_id": change_id, "request": (rec.get("payload") or {}).get("request"), **_qa_state(rec)}
        result = rec.get("execution_result") if rec.get("execution_status") == "COMPLETED" else None
        if result:
            from infra.adapters import v2_business_change
            titles = result.get("titles", {})
            current = v2_business._working(v2_business.business_view(store, tenant, workspace_id))
            out["understanding"] = {"status": result["status"], "message": result["message"], "descriptions": result["descriptions"], "questions": result["questions"],
                                    "refused": [{"quote": r["quote"], "why": r["why"]} for r in result["refused"]], "uncovered_parts": result["uncovered_parts"],
                                    "already_the_case": [titles.get(n["rule_id"], n["rule_id"]) for n in result["noops"]], "assumptions": result["assumptions"],
                                    "changes": [titles.get(r, r) for r in result["write_scope"]], "also_affected_to_check": [titles.get(r, r) for r in result["read_scope"] if r not in result["write_scope"]],
                                    "preview_hash": result["preview_hash"], "can_confirm": result["status"] == "READY" and not result["questions"] and not result["refused"] and not result["uncovered_parts"],
                                    "knowledge_moved": bool(current and current["snapshot"]["snapshot_id"] != result["pin"]["snapshot_id"]),
                                    "nothing_applied": "Confirming records your request only. No rule, file or code is changed by it."}
            if mode == "technical":
                out["understanding"]["technical"] = {"pin": result["pin"], "operations": result["operations"], "rejected": result["rejected"], "offered_rule_ids": result["offered_rule_ids"],
                                                     "write_scope": result["write_scope"], "read_scope": result["read_scope"], "config_sha256": result["config_sha256"], "model": result.get("model")}
            intent = v2_business_change.read_intent(store, tenant, workspace_id, change_id)
            out["confirmed"] = bool(intent)
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/business/changes/{change_id}/confirm")
async def confirm_business_change(request: Request, change_id: str):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        perms = permissions_for(authority.get("roles"))
        if "intent.confirm_own" not in perms and "knowledge.confirm" not in perms:
            raise PermissionDenied("intent.confirm_own")
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-") or not isinstance(body.get("preview_hash"), str):
            return _error("workspace_id and preview_hash required", 400)
        from infra.adapters import v2_business_change
        result = v2_business_change.confirm_change_intent(store, authority["tenant_id"], workspace_id, actor_id=authority["account_id"], job_id=change_id, preview_hash=body["preview_hash"])
        return JSONResponse(result, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.get("/v2/business/changes/{change_id}/impact")
def read_business_change_impact(request: Request, change_id: str, workspace_id: str, mode: str = "business"):
    """What a CONFIRMED change request touches, from the knowledge it was confirmed against. Deterministic, no model. File names and code recommendations need `evidence.read`."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if mode not in ("business", "technical") or not change_id.startswith("chg-"):
            return _error("mode must be business or technical", 400)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_change
        got = v2_business_change.impact_for_intent(store, tenant, workspace_id, change_id)
        if got is None:
            return JSONResponse({"error": "Confirm the request first; impact is computed for a confirmed request.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        from core.change_intelligence.v2 import business_impact
        current = v2_business._working(v2_business.business_view(store, tenant, workspace_id))
        can_evidence = "evidence.read" in perms
        out = business_impact.render(got["report"], mode=mode, can_read_evidence=can_evidence)
        _merge_dependency_state(out, store, tenant, workspace_id, change_id, got, mode=mode, can_evidence=can_evidence)
        from infra.adapters import v2_business_simulation
        drafted = v2_business_change.draft_for_intent(store, tenant, workspace_id, change_id)
        if drafted is not None:                                                                      # F-07 (c): measured counts of the Harbor simulation, or NOT_SIMULATED with the reason
            v2_business_simulation.merge_simulation(out, store, tenant, workspace_id, change_id, drafted["draft"], can_read_evidence=can_evidence)
        out.update({"change_id": change_id, "request": got["intent"]["request"],
                    "knowledge_moved": bool(current and current["snapshot"]["snapshot_id"] != got["report"]["pin"]["snapshot_id"])})
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)


def _dep_job_id(change_id: str, attempt: int) -> str:
    import hashlib
    return "dep-" + hashlib.sha256(json.dumps([change_id, attempt]).encode("utf-8")).hexdigest()[:24]


_DEP_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the change and the rules were too large to check",
                     "schema_violation": "the answer could not be used", "intent_not_confirmed": "the request was not confirmed"}


def _merge_dependency_state(out, store, tenant, workspace_id, change_id, got, *, mode, can_evidence):
    """Add what the model-proposed dependency check did (or did not do). Not run, pending, failed or limited all leave the coverage incomplete: never read as "no dependency"."""
    from core.change_intelligence.v2 import business_impact
    report = got["report"]
    rec = None
    for attempt in (3, 2, 1):
        try:
            rec = store.job_record(tenant, workspace_id, _dep_job_id(change_id, attempt))
            break
        except TenantScopeError:
            continue
    coverage = dict(out["coverage"], deterministic_complete=out["coverage"]["complete"])
    state = {"status": "not_requested", "reason": "No model check was run: only stored relations were used.", "proposed": []}
    if rec is not None:
        st = _qa_state(rec)
        if st["status"] == "done" and rec.get("execution_result"):
            res = rec["execution_result"]
            titles = got["titles"]
            merged = business_impact.merge_dependencies(report, {"accepted": res["accepted"], "rejected": res["rejected"], "notes": res["notes"]}, rules_not_shown=res["rules_not_shown"])
            hidden = bool(res["evidence_access"]) and not can_evidence
            proposed = []
            for d in merged["accepted"]:
                refs = [r for r in d["evidence_refs"] if can_evidence or r in (d["from_rule_id"], d["to_rule_id"])]
                item = {"from": titles.get(d["from_rule_id"], "?"), "to": titles.get(d["to_rule_id"], "?"), "relation": d["relation"], "status": "PROPOSED",
                        "rationale": None if hidden else d["rationale"], "rationale_withheld": hidden, "label": "suggested by a model; not confirmed; not part of what is changed"}
                if mode == "technical":
                    item.update(dep_id=d["dep_id"], from_rule_id=d["from_rule_id"], to_rule_id=d["to_rule_id"], evidence_refs=refs, provenance=d["provenance"])
                proposed.append(item)
            state.update(status="completed", reason=None, proposed=proposed, model_status=res["model_status"], rejected_count=len(res["rejected"]), gaps=[g["text"] for g in merged["gaps"]])
            if mode == "technical":
                state["rejected"] = res["rejected"]
                state["pin"] = res["pin"]
                state["job_id"] = rec.get("job_id")
            coverage["complete"] = merged["complete"]
            coverage["ai_dependencies_status"] = "completed"
            out["ai_dependencies"] = state
            out["coverage"] = coverage
            return
        if st["status"] in ("failed", "cancelled"):
            key = next((k for k in _DEP_REASON_WORDS if (st["reason"] or "").startswith(k)), None)
            state.update(status="failed", reason="The model check did not finish" + (f": {_DEP_REASON_WORDS[key]}" if key else "") + ". Dependencies beyond the stored relations are unknown, not absent.")
        else:
            state.update(status="pending", reason="The model check is still running; dependencies beyond the stored relations are not known yet.")
    coverage["complete"] = False
    coverage["ai_dependencies_status"] = state["status"]
    out["ai_dependencies"] = state
    out["coverage"] = coverage


@router.post("/v2/business/changes/{change_id}/impact/simulate")
async def request_change_simulation(request: Request, change_id: str):
    """Measure a CONFIRMED numeric change on the public Harbor sample through the existing deterministic simulation (0 provider calls). One job per supported case, bound to the
    change, the draft's edit set and the pinned sources; the report shows `MEASURED` counts or `NOT_SIMULATED` with the reason. Needs a human actor with `change.prepare`."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "change.prepare")
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-"):
            return _error("workspace_id and a change id required", 400)
        tenant = authority["tenant_id"]
        _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_simulation
        try:
            out = v2_business_simulation.request_simulation(store, tenant, workspace_id, change_id, actor_id=authority["account_id"])
        except v2_business_simulation.NothingToSimulate as exc:
            return JSONResponse({"error": "Nothing in this change can be measured by the Harbor simulation.", "code": "nothing_to_simulate", "reasons": exc.reasons}, status_code=409, headers=_NO_STORE)
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _refusal(exc)


@router.post("/v2/business/changes/{change_id}/impact/dependencies")
async def request_change_dependencies(request: Request, change_id: str):
    """Ask a model to propose dependencies of a CONFIRMED change that the stored graph does not show. Proposals only; nothing is changed or confirmed."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, attempt = body.get("workspace_id"), body.get("attempt", 1)
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "intent.create")
        require(authority, "ai.use")
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-") or isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
            return _error("workspace_id and attempt (1-3) required", 400)
        tenant = authority["tenant_id"]
        rec0 = store.job_record(tenant, workspace_id, change_id)
        perms = permissions_for(authority.get("roles"))
        if rec0.get("kind") != "change.understand" or (rec0.get("requested_by") != authority["account_id"] and "knowledge.confirm" not in perms):
            raise TenantScopeError("change request not found")
        from infra.adapters import v2_business_change
        if v2_business_change.read_intent(store, tenant, workspace_id, change_id) is None:
            return JSONResponse({"error": "Confirm the request first.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        jid = _dep_job_id(change_id, attempt)
        try:
            rec = store.job_record(tenant, workspace_id, jid)
        except TenantScopeError:
            rec = None
        if rec is None:
            from infra import provider_gate
            provider_gate.require_provider("change.impact_deps")
            limit, reservation = _quota_for("change.impact_deps")
            head = store.head(tenant, workspace_id)
            payload = {"intent_id": change_id, "evidence_access": "evidence.read" in perms, "attempt": attempt}
            store.create_job(tenant, workspace_id, jid, "change.impact_deps", payload, expected_version=head.version,
                             idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
            from infra import ai_admission
            ai_admission.admit(store, tenant, workspace_id, jid, "change.impact_deps", authority)
            rec = store.job_record(tenant, workspace_id, jid)
        return JSONResponse({"change_id": change_id, "attempt": attempt, **_qa_state(rec)}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _refusal(exc)


# ---- structured draft of a confirmed change (CHG-03) --------------------------------------------------------------------------------------------------------

def _guidance_job_id(change_id: str, attempt: int) -> str:
    import hashlib
    return "drg-" + hashlib.sha256(json.dumps([change_id, attempt]).encode("utf-8")).hexdigest()[:24]


_GUIDE_REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "budget_exceeded": "the draft was too large to send",
                       "schema_violation": "the answer could not be used", "intent_not_confirmed": "the request was not confirmed"}


def _dependency_input(store, tenant, workspace_id, change_id, got):
    """The completed model dependency check of this change, if any, as the draft carries it: PROPOSED leads with titles, plus the merged coverage state."""
    from core.change_intelligence.v2 import business_impact
    rec = None
    for attempt in (3, 2, 1):
        try:
            rec = store.job_record(tenant, workspace_id, _dep_job_id(change_id, attempt))
            break
        except TenantScopeError:
            continue
    if rec is None:
        return [], {"status": "not_requested", "reason": "No model dependency check was run: only stored relations were used.", "complete": False}
    st = _qa_state(rec)
    if st["status"] == "done" and rec.get("execution_result"):
        res = rec["execution_result"]
        merged = business_impact.merge_dependencies(got["report"], {"accepted": res["accepted"], "rejected": res["rejected"], "notes": res["notes"]}, rules_not_shown=res["rules_not_shown"])
        titles = got["titles"]
        deps = [dict(d, from_title=titles.get(d["from_rule_id"], "?"), to_title=titles.get(d["to_rule_id"], "?")) for d in merged["accepted"]]
        return deps, {"status": "completed", "reason": None, "complete": merged["complete"], "job_id": rec.get("job_id"), "gaps": [g["text"] for g in merged["gaps"]], "evidence_access": bool(res["evidence_access"])}
    if st["status"] in ("failed", "cancelled"):
        return [], {"status": "failed", "reason": "The model dependency check did not finish; dependencies beyond the stored relations are unknown, not absent.", "complete": False}
    return [], {"status": "pending", "reason": "The model dependency check is still running.", "complete": False}


def _merge_guidance_state(draft, store, tenant, workspace_id, change_id, *, can_evidence):
    """The draft with the model guidance job's state: not requested, pending, failed, tampered or completed (merged, downgrade-only). -> (draft, written_with_evidence)."""
    from core.change_intelligence.v2 import business_draft
    from infra.worker import draft_guidance
    rec = None
    for attempt in (3, 2, 1):
        try:
            rec = store.job_record(tenant, workspace_id, _guidance_job_id(change_id, attempt))
            break
        except TenantScopeError:
            continue
    state = {"status": "not_requested", "reason": "No model guidance was requested; the draft above is the server's own."}
    if rec is not None:
        st = _qa_state(rec)
        attempt_no = (rec.get("payload") or {}).get("attempt")
        if st["status"] == "done" and rec.get("execution_result"):
            res = rec["execution_result"]
            if draft_guidance.result_hash(res) != res.get("result_hash"):
                state = {"status": "tampered", "reason": "The stored guidance does not match its own hash and was not used.", "job_id": rec.get("job_id"), "attempt": attempt_no, "integrity": "hash_mismatch"}
            elif res.get("semantic_diff_hash") != draft["semantic_diff_hash"] or res.get("report_hash") != draft["report_hash"]:
                state = {"status": "stale", "reason": "The guidance was written for a different draft state and was not used.", "job_id": rec.get("job_id"), "attempt": attempt_no, "integrity": "binding_mismatch"}
            else:
                state = {"status": "completed", "reason": None, "job_id": rec.get("job_id"), "attempt": attempt_no, "result_hash": res["result_hash"], "integrity": "ok"}
                merged = business_draft.merge_guidance(draft, {"summary": res["summary"], "accepted": res["accepted"], "rejected": res["rejected"], "questions": res["questions"],
                                                               "not_addressed": res["not_addressed"]}, state=state)
                return merged, bool(res.get("evidence_access"))
        elif st["status"] in ("failed", "cancelled"):
            key = next((k for k in _GUIDE_REASON_WORDS if (st["reason"] or "").startswith(k)), None)
            state = {"status": "failed", "reason": "The model guidance did not finish" + (f": {_GUIDE_REASON_WORDS[key]}" if key else "") + ". The server's draft stands; manual items have no model instruction.",
                     "job_id": rec.get("job_id"), "attempt": attempt_no}
        else:
            state = {"status": "pending", "reason": "The model guidance is still being written.", "job_id": rec.get("job_id"), "attempt": attempt_no}
    out = json.loads(json.dumps(draft, sort_keys=True, default=str))
    out["guidance"] = state
    out["coverage"]["guidance"] = state["status"]
    out["coverage"]["complete"] = False                                                        # no completed guidance: the draft is not complete
    from core.change_intelligence.v2.serialize import canonical_hash
    out.pop("draft_hash", None)
    out["draft_hash"] = canonical_hash(out)
    return out, False


def _change_record_for(store, tenant, workspace_id, change_id, authority):
    """The confirmed-change read surfaces (impact, draft, review) are visible to the person who asked, to people who may confirm knowledge and to people who may decide the
    change (`change.approve`); anyone else sees "not found"."""
    rec = store.job_record(tenant, workspace_id, change_id)
    perms = permissions_for(authority.get("roles"))
    if rec.get("kind") != "change.understand" or (rec.get("requested_by") != authority["account_id"] and not {"knowledge.confirm", "change.approve"} & perms):
        raise TenantScopeError("change request not found")
    return rec, perms


@router.get("/v2/business/changes/{change_id}/draft")
def read_business_change_draft(request: Request, change_id: str, workspace_id: str, mode: str = "business"):
    """The structured draft of a CONFIRMED change, built by the server from the knowledge it was confirmed against: rule before/after, relation updates, one action per mapped file
    location, an outcome per requested operation, carried coverage/unknowns/counterevidence/PROPOSED dependencies, and the state of the optional model guidance. Never calls a
    model; writes nothing. File names, locators, instructions and code recommendations need `evidence.read`; Business mode has no ids."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if mode not in ("business", "technical") or not change_id.startswith("chg-"):
            return _error("mode must be business or technical", 400)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_review
        got = v2_business_review.current_draft(store, tenant, workspace_id, change_id)
        if got is None:
            return JSONResponse({"error": "Confirm the request first; a draft is built for a confirmed request.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        draft, written_with_evidence, parts = got
        can_evidence = "evidence.read" in perms
        from core.change_intelligence.v2 import business_draft
        out = business_draft.render(draft, mode=mode, can_read_evidence=can_evidence, guidance_written_with_evidence=written_with_evidence)
        current = v2_business._working(v2_business.business_view(store, tenant, workspace_id))
        out.update({"change_id": change_id, "request": parts["intent"]["request"],
                    "knowledge_moved": bool(current and current["snapshot"]["snapshot_id"] != draft["pin"]["snapshot_id"])})
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)


# ---- review and decisions on a confirmed change (CHG-04) ------------------------------------------------------------------------------------------------------

def _review_payload(view, *, mode, can_evidence, change_id, store=None, tenant=None, workspace_id=None):
    """The review page's one response: understanding summary, impact, draft (with clarifications), decisions with their currency, what this actor may do. Business mode has no ids,
    paths or hashes except the opaque `binding` the decide call must echo back."""
    from core.change_intelligence.v2 import business_draft, business_impact
    draft = view["draft"]
    impact = business_impact.render(view["report"], mode=mode, can_read_evidence=can_evidence)
    if store is not None:
        from infra.adapters import v2_business_simulation
        v2_business_simulation.merge_simulation(impact, store, tenant, workspace_id, change_id, draft, can_read_evidence=can_evidence)
    rendered = business_draft.render(draft, mode=mode, can_read_evidence=can_evidence, guidance_written_with_evidence=view["guidance_written_with_evidence"])
    titles = view["titles"]
    decisions = []
    for d in view["decisions"]:
        row = {"kind": d["kind"], "action": d["action"], "by": d["actor_id"], "reason": d["reason"], "current": d["current"], "stale_reason": d["stale_reason"], "self_approval": d["self_approval"],
               "at": d["created_at"]}
        if d["kind"] == "clarification":
            row.update(ref=d["binding"].get("ref"), disposition=d["binding"].get("disposition"))
        if mode == "technical":
            row.update(decision_id=d["decision_id"], semantic_diff_hash=d["semantic_diff_hash"], draft_hash=d["draft_hash"], binding=d["binding"] if can_evidence else {k: v for k, v in d["binding"].items() if k != "edit_refs"})
        decisions.append(row)
    can = dict(view["can"])
    can["blockers"] = [{"code": b["code"], "text": b["text"]} for b in can["blockers"]]
    for key in ("knowledge_decision", "file_writes_decision"):
        if can.get(key):
            can[key] = {"action": can[key]["action"], "by": can[key]["actor_id"], "at": can[key]["created_at"]}
    out = {"change_id": change_id, "request": view["intent"]["request"], "confirmed_by": view["intent"]["confirmed_by"], "requested_by": view["requester_id"], "knowledge_moved": view["knowledge_moved"],
           "changes": [titles.get(r, r) for r in view["intent"]["write_scope"]], "impact": impact, "draft": rendered, "decisions": decisions, "can": can,
           "binding": {"semantic_diff_hash": draft["semantic_diff_hash"], "draft_hash": draft["draft_hash"], "edits_hash": view["edits_hash"]},
           "policy": {"require_distinct_approver": view["policy"]["require_distinct_approver"]},
           "completion": view["completion"],
           "source": view["source"],                                                   # SYN-03: the choice state (already projected by evidence rights in the adapter)
           "commit": None if view["committed"] is None else {"snapshot_id": view["committed"]["snapshot_id"], "business_revision": view["committed"]["business_revision"],
                                                             "by": view["committed"]["committed_by"], "at": view["committed"]["created_at"],
                                                             "pending_implementation": view["committed"]["pending_implementation"] if can_evidence else
                                                             {k: v for k, v in (view["committed"]["pending_implementation"] or {}).items() if k not in ("items", "unresolved")}},
           "decisions_are_separate": "Confirming the understanding, accepting the knowledge change, approving automatic file edits and accepting the result are four separate decisions; none implies another."}
    return out


@router.get("/v2/business/changes/{change_id}/review")
def read_business_change_review(request: Request, change_id: str, workspace_id: str, mode: str = "business"):
    """Everything a reviewer or approver needs for a CONFIRMED change: meaning before/after, impact, pending implementation, open questions, decisions and what this person may do.
    Computed from stored rows; never calls a model; writes nothing."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if mode not in ("business", "technical") or not change_id.startswith("chg-"):
            return _error("mode must be business or technical", 400)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_review
        view = v2_business_review.review_view(store, tenant, workspace_id, change_id, account_id=authority["account_id"], roles=authority.get("roles"))
        if view is None:
            return JSONResponse({"error": "Confirm the request first; the review starts from a confirmed request.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        return JSONResponse(_review_payload(view, mode=mode, can_evidence="evidence.read" in perms, change_id=change_id, store=store, tenant=tenant, workspace_id=workspace_id), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)


async def _decision_call(request: Request, change_id: str, *, kind_from_body: bool):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-"):
            return _error("workspace_id required", 400)
        seen = body.get("binding")
        if not isinstance(seen, dict):
            return _error("binding (semantic_diff_hash, draft_hash) required: send back what the review showed", 400)
        tenant = authority["tenant_id"]
        _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_review
        if kind_from_body:
            kind, action = body.get("kind", "knowledge"), body.get("action")
            extra = {"acknowledged_flags": body.get("acknowledged_flags") is True}
        else:
            kind, action = "clarification", "clarify"
            extra = {"ref": body.get("ref"), "disposition": body.get("disposition"), "answer": body.get("answer")}
        if not isinstance(kind, str) or not isinstance(action, str):
            return _error("kind and action required", 400)
        result = v2_business_review.record_decision(store, tenant, workspace_id, change_id, actor_id=authority["account_id"], roles=authority.get("roles"), kind=kind, action=action,
                                                    reason=body.get("reason"), seen=seen, body=extra, idempotency_key=body.get("idempotency_key") if isinstance(body.get("idempotency_key"), str) else None)
        return JSONResponse({"change_id": change_id, **result}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409 if exc.code != "invalid_field" else 400, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/business/changes/{change_id}/review/decide")
async def decide_business_change(request: Request, change_id: str):
    """Accept, reject or request changes on the knowledge change (kind `knowledge`), or approve/reject the automatic file edits (kind `file_writes`). `change.approve`, distinct
    approver by workspace policy, bound to the draft the person saw. Records a decision only; nothing is applied or committed here."""
    return await _decision_call(request, change_id, kind_from_body=True)


@router.post("/v2/business/changes/{change_id}/review/clarify")
async def clarify_business_change(request: Request, change_id: str):
    """A person's disposition or answer for one open item of the draft (requester or `knowledge.confirm`). It labels or answers; it never changes an action or applies anything."""
    return await _decision_call(request, change_id, kind_from_body=False)


# ---- code change recommendations (SYN-01): a developer's checklist, never an edit ---------------------------------------------------------------------------------

@router.get("/v2/business/changes/{change_id}/code")
def read_business_change_code(request: Request, change_id: str, workspace_id: str):
    """Code change recommendations for a CONFIRMED change, built from the draft's code locations and the pinned code observations. Locations, observed text and pseudocode need
    `evidence.read`; without it only titles, statuses and the desired logic in words are shown. Stale when the knowledge or the sources moved. No model; nothing written."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if not change_id.startswith("chg-"):
            return _error("change id required", 400)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_review
        got = v2_business_review.current_draft(store, tenant, workspace_id, change_id)
        if got is None:
            return JSONResponse({"error": "Confirm the request first.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        draft, _w, parts = got
        head = store.head(tenant, workspace_id)
        current = v2_business._working(v2_business.business_view(store, tenant, workspace_id))
        from core.change_intelligence.v2 import code_recommendation
        rec = code_recommendation.build_recommendations(draft, parts["intent"]["operations"],
                                                        head={"source_revision_id": head.source_revision_id, "snapshot_id": current["snapshot"]["snapshot_id"] if current else None})
        out = code_recommendation.render(rec, can_read_evidence="evidence.read" in perms)
        out.update({"change_id": change_id, "request": parts["intent"]["request"]})
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)


@router.post("/v2/business/changes/{change_id}/code/apply")
async def refuse_business_code_apply(request: Request, change_id: str):
    """Always refused: the product never edits, patches, runs or deploys code. The route exists so that the refusal is explicit and testable, not a 404 that looks like a missing feature."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        body = await _json_body(request)
        workspace_id = body.get("workspace_id")
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        return JSONResponse({"error": "The product does not edit, patch, run or deploy code. Use the recommendations and have a developer implement them; the changed file is read back by a source refresh.",
                             "code": "code_apply_not_supported", "change_id": change_id}, status_code=409, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)


@router.post("/v2/business/changes/{change_id}/commit")
async def commit_business_change(request: Request, change_id: str):
    """Commit an APPROVED knowledge change into the accepted knowledge (CHG-05): the decided meaning becomes TARGET rules in a new accepted business revision, atomically with
    its decision, audit and outbox marker. `knowledge.confirm`, human actor, bound to the draft seen. No file, code or document is touched; pending implementation is listed."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-") or not isinstance(body.get("binding"), dict):
            return _error("workspace_id and binding required", 400)
        tenant = authority["tenant_id"]
        _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_commit
        result = v2_business_commit.commit_change(store, tenant, workspace_id, change_id, actor_id=authority["account_id"], roles=authority.get("roles"), seen=body["binding"])
        return JSONResponse({"change_id": change_id, **result}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except bk.ContractError as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=422, headers=_NO_STORE)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)



# ---- journey per axis (SYN-04): where a change stands, who acts next, and what completion means ---------------------------------------------------------------

@router.get("/v2/business/changes/{change_id}/journey")
def read_business_change_journey(request: Request, change_id: str, workspace_id: str):
    """One read model of the change across its separate axes (understanding, confirmation, decision, commit, source files, code, read-back, behaviour, user result),
    with the next actor and action and explicit completion flags (`knowledge_only_complete`, `fully_implemented`, `behaviour_verified`). Visible to the asker, knowledge
    owners and approvers; accepted-target vs observed-current values need `evidence.read`. Nothing written, no model."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if not change_id.startswith("chg-"):
            return _error("change id required", 400)
        tenant = authority["tenant_id"]
        _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_journey
        out = v2_business_journey.journey_view(store, tenant, workspace_id, change_id, account_id=authority["account_id"], roles=authority.get("roles"))
        return JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


# ---- source files of a committed change (SYN-03): the person chooses the automatic edit or a manual edit --------------------------------------------------------

@router.get("/v2/business/changes/{change_id}/source")
def read_business_change_source(request: Request, change_id: str, workspace_id: str):
    """The state of the source files of a confirmed change: whether an automatic edit exists and is approved, the apply job if one was chosen, the manual items.
    Paths, locators, instructions and file hashes need `evidence.read`. Nothing written."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if not change_id.startswith("chg-"):
            return _error("change id required", 400)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_commit, v2_business_review, v2_business_source
        got = v2_business_review.current_draft(store, tenant, workspace_id, change_id)
        if got is None:
            return JSONResponse({"error": "Confirm the request first.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        draft, _w, _parts = got
        committed = v2_business_commit.committed_state(store, tenant, workspace_id, change_id)
        out = v2_business_source.source_state(store, tenant, workspace_id, change_id, draft, committed, perms=perms)
        return JSONResponse({"change_id": change_id, **out}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/business/changes/{change_id}/source/apply")
async def choose_business_change_source_mode(request: Request, change_id: str):
    """The person's choice for the source files of a committed change. `mode: "ai"` (`change.apply`, human actor, bound to the draft seen incl. `edits_hash`) creates one
    deterministic `business.source_apply` job that writes the APPROVED YAML scalar edits into a new source revision; `mode: "manual"` returns the instructions and writes
    nothing. Code is never edited; unsupported items stay manual."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "project.read")
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-") or not isinstance(body.get("binding"), dict) or body.get("mode") not in ("ai", "manual"):
            return _error("workspace_id, mode (ai|manual) and binding required", 400)
        tenant = authority["tenant_id"]
        _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_source
        result = v2_business_source.choose(store, tenant, workspace_id, change_id, actor_id=authority["account_id"], roles=authority.get("roles"), mode=body["mode"], seen=body["binding"])
        return JSONResponse({"change_id": change_id, **result}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409 if exc.code != "invalid_field" else 400, headers=_NO_STORE)
    except (StaleVersion, StaleAuthorityError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/business/changes/{change_id}/draft/guidance")
async def request_change_draft_guidance(request: Request, change_id: str):
    """Ask a model to write guidance over the server-built draft of a CONFIRMED change (summary, manual instructions, questions, holds). Text only; the model adds, drops or
    upgrades nothing, and nothing is applied. Needs `change.prepare` and `ai.use`; the asker must be the requester or hold `knowledge.confirm`."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id, attempt = body.get("workspace_id"), body.get("attempt", 1)
    try:
        authority = _authority(identity, request, workspace_id if isinstance(workspace_id, str) else "")
        require_human_actor(authority)
        require(authority, "change.prepare")
        require(authority, "ai.use")
        if not isinstance(workspace_id, str) or not change_id.startswith("chg-") or isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 3:
            return _error("workspace_id and attempt (1-3) required", 400)
        tenant = authority["tenant_id"]
        _rec, perms = _change_record_for(store, tenant, workspace_id, change_id, authority)
        from infra.adapters import v2_business_change
        if v2_business_change.read_intent(store, tenant, workspace_id, change_id) is None:
            return JSONResponse({"error": "Confirm the request first.", "code": "not_confirmed"}, status_code=409, headers=_NO_STORE)
        jid = _guidance_job_id(change_id, attempt)
        try:
            rec = store.job_record(tenant, workspace_id, jid)
        except TenantScopeError:
            rec = None
        if rec is None:
            from infra import provider_gate
            provider_gate.require_provider("change.draft_guidance")
            limit, reservation = _quota_for("change.draft_guidance")
            head = store.head(tenant, workspace_id)
            payload = {"intent_id": change_id, "evidence_access": "evidence.read" in perms, "attempt": attempt}
            store.create_job(tenant, workspace_id, jid, "change.draft_guidance", payload, expected_version=head.version,
                             idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
            from infra import ai_admission
            ai_admission.admit(store, tenant, workspace_id, jid, "change.draft_guidance", authority)
            rec = store.job_record(tenant, workspace_id, jid)
        return JSONResponse({"change_id": change_id, "attempt": attempt, "job_id": jid, **_qa_state(rec)}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _refusal(exc)
