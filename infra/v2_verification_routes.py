"""Operator-supplied verification contract routes.

`POST /v2/verification/contracts` stores ONE immutable contract (`v2_verification_contracts`) for a
proposal: the expected value of one locator of one logical path, stated by an authenticated HUMAN
holding `verification.contract.set` (admin, change_approver). Authority follows the module
invariant of `infra/v2_policy_routes.py`: tenant / roles / actor kind come ONLY from the server
side session; `actor_id`, `author`, `tenant_id`, `contract_sha256` ... in the body are never read.
The same Origin + `X-Ripple-Judge` rule as every other v2 mutation applies (`_origin_ok`).

Guards (each has a negative test): human actor; permission; the proposal must exist in THIS
tenant/workspace and be a `proposal` job; the author must differ from the proposal's requester
when the workspace requires a distinct approver (the same `require_distinct_approver` the decision
route uses); the logical path must be an artifact of the proposal's source revision / current head
and agree with the contract kind; the value is a scalar; one contract per (proposal, path,
locator), immutable; an audit row is written in the same transaction.

The expected value is never prefilled from, or compared to, the proposal's own edits here: that
independence is the point (it is checked later by the review / verification stages).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import (
    PermissionDenied, require, require_distinct_approver, require_human_actor,
)
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_policy_routes import (
    _authority, _error, _identity_denied_status, _identity_store, _json_body, _origin_ok,
    _v2_store,
)
from infra import v2_contract_suggestion
from infra.worker.verification_contract import (
    CONTRACT_KINDS, clean_provenance, kind_matches_path, normalize_locator,
)

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_MAX_TEXT = 2000


def _public(contract: dict) -> dict:
    return dict(contract)


def _denied(exc: IdentityDenied):
    status = _identity_denied_status(exc)
    return _error("sign in required" if status == 401 else "permission denied", status)


def _scalar(value) -> bool:
    if isinstance(value, bool) or isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return isinstance(value, str) and len(value) <= _MAX_TEXT


@router.post("/v2/verification/contracts")
async def set_contract(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = await _json_body(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "verification.contract.set")
        # Authenticate and authorize BEFORE any field validation, so an anonymous or unauthorised
        # caller learns nothing about the request schema (401/403, never a 400).
        proposal_job_id = body.get("proposal_job_id")
        if not isinstance(proposal_job_id, str) or not proposal_job_id:
            return _error("proposal_job_id required", 400)
        kind = body.get("kind")
        if kind not in CONTRACT_KINDS:
            return _error("kind must be one of " + "|".join(sorted(CONTRACT_KINDS)), 400)
        logical_path = body.get("logical_path")
        if (not isinstance(logical_path, str) or not logical_path.strip()
                or len(logical_path) > 500):
            return _error("logical_path required", 400)
        if not kind_matches_path(kind, logical_path):
            return _error("kind does not match the logical_path file type", 400)
        locator = normalize_locator(kind, body.get("locator"))
        if locator is None:
            return _error("locator is not valid for this kind", 400)
        if "expected" not in body or not _scalar(body.get("expected")):
            return _error("expected must be a scalar (string, number or boolean)", 400)
        provenance = clean_provenance(body.get("provenance"))
        if provenance is None:
            return _error("provenance must be a small string-to-string object", 400)
        tenant_id = authority["tenant_id"]
        try:
            proposal = store.job_record(tenant_id, workspace_id, proposal_job_id)
        except TenantScopeError:
            return _error("proposal_job_id not found", 400)
        if proposal.get("kind") != "proposal":
            return _error("proposal_job_id is not a proposal job", 400)
        # A contract is for ONE proposal on ONE source revision: once the files moved on, it cannot be stated for the old proposal.
        gone = v2_contract_suggestion.staleness(store, tenant_id, workspace_id, proposal)
        if gone:
            return _error(f"this proposal is out of date: {gone}", 409)
        if v2_contract_suggestion.was_applied(store, tenant_id, workspace_id, proposal_job_id):
            return _error("this proposal was already applied: its check cannot be changed any more", 409)
        suggested = body.get("suggestion_sha256")
        if suggested is not None:
            # The person confirmed a check the SERVER built. Recompute it now: it must still be exactly what was shown (same proposal, same revision, same item).
            current = v2_contract_suggestion.build(store, tenant_id, workspace_id, proposal_job_id)
            same = (isinstance(suggested, str) and suggested == current.get("suggestion_sha256") and not current.get("stale")
                    and any(i["kind"] == kind and i["logical_path"] == logical_path and i["locator"] == locator and i["expected"] == body["expected"]
                            for i in current["items"]))
            if not same:
                return _error("the suggested check changed or no longer matches what was shown; reload and confirm again", 409)
            provenance = {**provenance, "origin": "system_suggestion_confirmed", "suggestion_sha256": suggested}
        else:
            provenance = {**provenance, "origin": provenance.get("origin", "typed_by_approver")}
        policy = store.workspace_policy(tenant_id, workspace_id)
        require_distinct_approver(proposal.get("requested_by"), authority["account_id"],
                                  require_distinct=policy["require_distinct_approver"])
        revisions = []
        pinned = ((proposal.get("execution_result") or {}).get("pinned_input") or {}
                  ).get("pinned_source_revision_id")
        if isinstance(pinned, str) and pinned:
            revisions.append(pinned)
        head = store.head(tenant_id, workspace_id)
        if head.source_revision_id:
            revisions.append(head.source_revision_id)
        known = set()
        for revision_id in revisions:
            try:
                view = store.source_revision_view(tenant_id, workspace_id, revision_id)
            except TenantScopeError:
                continue
            # `logical_path` is set by every import/refresh; a directly published artifact has
            # none, and the verification report already names it by artifact_id then.
            known |= {a["logical_path"] or a["artifact_id"] for a in view["artifacts"]}
        if logical_path not in known:
            return _error("logical_path is not an artifact of the proposal's source revision",
                          400)
        contract = store.record_verification_contract(
            tenant_id, workspace_id, proposal_job_id=proposal_job_id,
            logical_path=logical_path, kind=kind, locator=locator,
            expected=body["expected"], author_account_id=authority["account_id"],
            provenance=provenance)
        return JSONResponse(_public(contract), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict, StaleVersion) as exc:
        return _error(str(exc), 409)


@router.get("/v2/verification/contracts")
def list_contracts(request: Request, workspace_id: str, proposal_job_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        rows = store.verification_contracts_for_proposal(
            authority["tenant_id"], workspace_id, proposal_job_id)
        gone = None
        try:
            gone = v2_contract_suggestion.staleness(store, authority["tenant_id"], workspace_id, store.job_record(authority["tenant_id"], workspace_id, proposal_job_id))
        except TenantScopeError:
            gone = None
        applied = v2_contract_suggestion.applied_job_id(store, authority["tenant_id"], workspace_id, proposal_job_id)
        return JSONResponse({"proposal_job_id": proposal_job_id, "stale": bool(gone), "stale_reason": gone, "applied_job_id": applied,
                             "contracts": [{**_public(r), "stale": bool(gone)} for r in rows]}, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/verification/contracts/{contract_id}")
def read_contract(request: Request, contract_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        row = store.verification_contract(authority["tenant_id"], workspace_id, contract_id)
        if row is None:
            return _error("contract not found", 404)
        return JSONResponse(_public(row), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/verification/suggestion")
def suggested_checks(request: Request, workspace_id: str, proposal_job_id: str):
    """The check the server proposes for this proposal (see `infra/v2_contract_suggestion.py`). Read-only; nothing is saved. It carries no source text:
    only the file, the key or cell, and the value of the confirmed request."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        return JSONResponse(v2_contract_suggestion.build(store, authority["tenant_id"], workspace_id, proposal_job_id), headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
