"""Centralized permission enforcement on top of the v2 identity layer.

Every route below resolves the caller's tenant/project/workspace/roles fresh,
server-side, through `PostgresIdentityStore.resolve_context`, which re-joins
the session, account-liveness and membership rows on every call -- from the
session cookie and a path `workspace_id` only. Nothing in the JSON body or
headers (actor_id, role, tenant_id, ...) is ever treated as authority; those
fields, if present, are ignored. `infra/adapters/v2_policy.py` is the single
place permissions are evaluated, so no route hand-rolls its own role check.
"""

from __future__ import annotations

from infra.db_offload import db_route, request_json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.change_intelligence.v2.lifecycle import AuthorizationError, LifecycleError
from core.change_intelligence.v2.serialize import canonical_hash
from infra import v2_lineage as _v2_lineage
from infra.v2_lineage import DraftSuperseded as v2_lineage_DraftSuperseded
from infra.adapters.v2_identity import (
    IdentityDenied, MembershipNotFound, PostgresIdentityStore,
)
from infra.adapters.v2_policy import (
    APPLY_CREATE_SUBJECT_PERMISSION, GRANTABLE_ROLES, JOB_APPROVE_PERMISSION,
    JOB_CREATE_PERMISSION,
    PermissionDenied, permissions_for, require, require_distinct_approver,
    require_human_actor,
)
from infra.adapters.v2_postgres import (
    ConcurrencyLimitExceeded, HitlVersionConflict, IntentNotFound, IntentStale, JobAttemptsExhausted,
    PostgresV2Store, StaleVersion, StoreConflict, TenantScopeError,
)
from infra.adapters.v2_restore import RestoreRefused, create_restore_apply_job
from infra.worker import job_quota


router = APIRouter()
_COOKIE = "ripple_v2"


class _NullBlobs:
    """Job/approval/membership routes never touch blob bytes directly."""

    def put_immutable(self, digest, content):
        raise NotImplementedError

    def read(self, digest):
        raise NotImplementedError


def _identity_store():
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    return PostgresIdentityStore(dsn) if dsn else None


def _v2_store():
    dsn = os.environ.get("RIPPLE_V2_DSN", "")
    return PostgresV2Store(dsn, _NullBlobs()) if dsn else None


def _origin_ok(request: Request) -> bool:
    expected = os.environ.get("RIPPLE_JUDGE_ORIGIN", str(request.base_url).rstrip("/"))
    return (request.headers.get("origin") == expected
            and request.headers.get("x-ripple-judge") == "1")


def _error(message: str, status: int):
    return JSONResponse({"error": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _token(request: Request) -> str:
    return request.cookies.get(_COOKIE, "")


def _authority(identity: PostgresIdentityStore, request: Request, workspace_id: str) -> dict:
    """The one server-side authority resolution every route below uses.

    A revoked/disabled/expired account, or one with no current membership in
    this exact workspace_id, gets `IdentityDenied` -- regardless of anything
    the request body claims about actor, role or tenant.
    """
    return identity.resolve_context(_token(request), workspace_id)


def _identity_denied_status(exc: IdentityDenied) -> int:
    """`/v2/hitl/*` routes only: an invalid/missing session
    ("sign in required") is 401 like every other route; an authenticated
    account with no CURRENT membership in the requested workspace_id --
    whether it never had one (cross-tenant probing) or had it revoked after
    the fact (`test_revoked_permission_is_rechecked_live`,
    `test_cross_tenant_queue_access_is_denied`) -- is treated as an
    authorization refusal (403), not an authentication failure, since the
    caller IS a valid signed-in human and the queue/answer surface must not
    reveal via a 401/404 split whether a workspace it cannot join exists."""
    return 401 if "sign in required" in str(exc) else 403


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _json_body_sync(request: Request) -> dict:
    """`_json_body` for a `db_route` function (the body was read on the event loop)."""
    try:
        body = request_json(request)
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _enforce_job_quota(store: PostgresV2Store, tenant_id: str, workspace_id: str,
                       kind: str) -> int | None:
    """Quota admission and project budget enforcement: actually CALL the quota
    admission helpers before a job is created, so a job whose kind would
    fail admission, or whose project has already spent up to its total cap,
    is refused here -- never silently created and left for a worker to
    discover later (or never).

    Opt-in: enforcement only runs when a worker budget config is configured
    via `RIPPLE_BUDGET_CONFIG_PATH` -- absent means no quota is configured
    yet, so no ceiling is enforced, identical in shape to every other
    env-gated override.

    Returns `(concurrency_limit, budget_reservation)` for the caller to pass
    straight into `store.create_job`'s OWN atomic count-and-insert (and
    reserve-and-insert) check. Reading `count_active_jobs` HERE, in a
    separate transaction from `store.create_job`'s insert, would allow a
    "count-then-create race" (two concurrent submissions could both pass a
    one-job ceiling), and the project's total cost cap has the identical
    race shape ("reservation ... is a separate transaction from
    admission"). Neither the concurrency count nor the spend total is read
    here at all; both are read and compared inside `store.create_job`'s own
    transaction, after that
    transaction has already taken `FOR UPDATE` on the workspace row, so
    concurrent creates for the same workspace serialize on that one lock and
    cannot both observe a stale count or a stale spend total.
    """
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        if kind in ("analysis", "extraction", "gap.answer", "impact.judge", "change.draft"):
            raise job_quota.BudgetConfigError(f"live {kind} requires a budget config")
        return None, None
    config = job_quota.load_worker_budget_config()
    job_quota.check_job_admission(config, job_kind=kind)
    return job_quota.load_concurrency_limit(), job_quota.compute_budget_reservation(config)


@router.post("/v2/jobs")
@db_route
def create_job(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id, kind, job_id = body.get("workspace_id"), body.get("kind"), body.get("job_id")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if not all(isinstance(x, str) and x for x in (workspace_id, kind, job_id)):
        return _error("workspace_id, kind and job_id required", 400)
    return submit_job(store, workspace_id, kind, job_id, payload, lambda: _authority(identity, request, workspace_id))


def submit_job(store, workspace_id: str, kind: str, job_id: str, payload: dict, get_authority):
    """The whole job-creation contract of `POST /v2/jobs` (permission per kind, payload checks, epoch and lineage guards, quota, provider gate, admission), for
    the route and for server-side callers acting AS a person (EUI-08 preparation run: `get_authority` resolves that person's current membership). Returns the
    route's response object; nothing here is relaxed for a server-side caller."""
    permission = JOB_CREATE_PERMISSION.get(kind)
    if permission is None:
        return _error("unsupported job kind", 400)
    from infra import operator_flags as _flags
    from infra.worker import wake as _wake
    if _wake.enabled() and _wake.kick_budget_exhausted(_v2_store()):
        return JSONResponse({"error": "The processing capacity set aside for this evaluation has been used up: new work is no longer accepted. Nothing was started and nothing was charged. Everything already stored can still be read.",
                             "code": "capacity_reached"}, status_code=409, headers={"Cache-Control": "no-store"})
    if _flags.window_creation_closed():          # after the evaluation window nothing new is accepted: a job created now would wait with nobody to run it
        return JSONResponse({"error": "This evaluation window has ended (or is about to): new work is no longer accepted. Nothing was started and nothing was charged. Everything already stored can still be read.",
                             "code": "window_ended"}, status_code=409, headers={"Cache-Control": "no-store"})
    from infra.worker.verification import RESERVED_JOB_ID_PREFIXES
    if job_id.startswith(RESERVED_JOB_ID_PREFIXES):
        return _error("job_id uses a prefix reserved for system-derived jobs", 400)
    if kind in ("analysis", "extraction") and not isinstance(payload.get("artifact_id"), str):
        return _error(f"{kind} requires artifact_id", 400)
    if kind == "gap.answer":
        from infra.worker.gap_answer import validate_payload as _gap_payload_error
        gap_error = _gap_payload_error(payload)
        if gap_error:
            return _error(f"gap.answer: {gap_error}", 400)
    if kind == "impact.judge":
        from infra.worker.impact_judge import validate_payload as _judge_payload_error
        judge_error = _judge_payload_error(payload)
        if judge_error:
            return _error(f"impact.judge: {judge_error}", 400)
    if kind == "change.draft":
        from infra.worker.change_draft import validate_payload as _draft_payload_error
        draft_error = _draft_payload_error(payload)
        if draft_error:
            return _error(f"change.draft: {draft_error}", 400)
    if kind == "change.brief" and (not isinstance(payload.get("intent_id"), str) or not payload.get("intent_id")
                                   or not isinstance(payload.get("extraction_job_ids"), list)
                                   or not payload.get("extraction_job_ids")):
        return _error("change.brief requires intent_id and extraction_job_ids", 400)
    if kind == "impact" and not isinstance(payload.get("candidate_ids"), list):
        return _error("impact requires candidate_ids", 400)
    if (kind == "impact" and payload.get("expected_decision_epoch") is not None
            and not isinstance(payload.get("expected_decision_epoch"), int)):
        return _error("expected_decision_epoch must be an integer", 400)
    if kind == "simulation" and payload.get("corpus") is not None \
            and not isinstance(payload.get("corpus"), list):
        return _error("simulation payload.corpus must be a list", 400)
    if kind == "simulation" and payload.get("adapter") == "harbor_cli" and payload.get("from_draft_job_id") is not None:
        from infra.worker.harbor_sim import validate_payload as _harbor_payload_error
        harbor_error = _harbor_payload_error(payload)
        if harbor_error:
            return _error(f"simulation: {harbor_error}", 400)
    if kind == "whatif":
        for key in ("impact_job_id", "simulation_job_id"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                return _error(f"whatif requires {key}", 400)
    if (kind == "whatif" and payload.get("expected_decision_epoch") is not None
            and not isinstance(payload.get("expected_decision_epoch"), int)):
        return _error("expected_decision_epoch must be an integer", 400)
    if kind == "review":
        # Payload-shape rejection lives HERE, the same layer every
        # other job kind's own payload-shape checks already live
        # `evaluated_hash` is OPTIONAL and, even when
        # present, is never trusted as the real evaluated hash -- the
        # executor independently recomputes it from the resolved proposal's
        # own real result (see infra/worker/review.py's module docstring);
        # this route only validates its TYPE, not its truth.
        if not isinstance(payload.get("proposal_job_id"), str) or not payload.get("proposal_job_id"):
            return _error("review requires proposal_job_id", 400)
        evaluated_hash = payload.get("evaluated_hash")
        if evaluated_hash is not None and (
                not isinstance(evaluated_hash, str) or not evaluated_hash):
            return _error("review evaluated_hash must be a non-empty string when present", 400)
        # Optional operator-supplied verification contract; existence in THIS
        # tenant/workspace is checked below, its truth by the executor.
        contract_id = payload.get("verification_contract_id")
        if contract_id is not None and (not isinstance(contract_id, str) or not contract_id):
            return _error("review verification_contract_id must be a non-empty string when present", 400)
    if (kind == "review" and payload.get("expected_decision_epoch") is not None
            and not isinstance(payload.get("expected_decision_epoch"), int)):
        return _error("expected_decision_epoch must be an integer", 400)
    if kind == "proposal":
        # Payload-shape/protected-path/oracle-substitution
        # rejection lives HERE, the same layer every other job kind's own
        # payload-shape checks already live -- this is the
        # PRIMARY submission-time rejection surface for a malformed/
        # injection/protected-path/oracle-substitution proposal attempt;
        # `infra/worker/proposal.py`'s executor re-checks a subset of these
        # in `prepare()` as defense in depth, but a caller of the real
        # public route is rejected here, before any job is even created.
        from infra.worker.proposal_config import load_proposal_support_matrix
        for key in ("intent_id", "impact_job_id", "simulation_job_id",
                   "target_artifact_id", "edit_kind"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                return _error(f"proposal requires {key}", 400)
        matrix = load_proposal_support_matrix()
        if payload.get("edit_kind") not in matrix.allowed_edit_kinds:
            return _error(f"unsupported edit_kind: {payload.get('edit_kind')!r}", 400)
        if matrix.is_protected_path(payload.get("target_artifact_id")):
            return _error(
                "protected_config_target: this path may never be a patch target", 400)
        generated_tests = payload.get("generated_tests")
        if isinstance(generated_tests, list) and any(
                isinstance(item, dict) and item.get("replaces_oracle") for item in generated_tests):
            return _error(
                "generated test must not replace the independent oracle", 400)
        if payload.get("draft_job_id") is not None:
            if not isinstance(payload.get("draft_job_id"), str) or not payload["draft_job_id"]:
                return _error("draft_job_id must be a non-empty string", 400)
            if payload.get("edits") is not None:
                return _error("edits must not be supplied with draft_job_id (they come from the validated draft)", 400)
            if payload.get("edit_kind") != "yaml_typed_edit":
                return _error("draft_job_id supports edit_kind yaml_typed_edit only", 400)
            acc = payload.get("accepted_refs")
            if acc is not None and not (isinstance(acc, list) and acc and all(isinstance(x, str) and x for x in acc)):
                return _error("accepted_refs must be a non-empty list of draft refs", 400)
        if payload.get("action") == "revise":
            prev_id = payload.get("previous_proposal_job_id")
            if not isinstance(prev_id, str) or not prev_id:
                return _error("revise requires previous_proposal_job_id", 400)
    if (kind == "proposal" and payload.get("expected_decision_epoch") is not None
            and not isinstance(payload.get("expected_decision_epoch"), int)):
        return _error("expected_decision_epoch must be an integer", 400)
    if kind == "approval":
        # Payload-shape rejection lives HERE, the same layer every
        # other job kind's own payload-shape checks already live
        # Existence/kind/completion resolution of BOTH
        # `proposal_job_id` and `review_job_id` is deferred to the executor
        # (`infra/worker/approval.py::_resolve_job`), NOT checked here --
        # unlike "review"'s own route-level `proposal_job_id` existence
        # check, a bad `review_job_id` here must surface as a real, durable
        # FAILED job (an unsupported executor/exception referenced must be
        # rejected, never fabricated), not a submission-time 400.
        for key in ("proposal_job_id", "review_job_id"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                return _error(f"approval requires {key}", 400)
    if (kind == "approval" and payload.get("expected_decision_epoch") is not None
            and not isinstance(payload.get("expected_decision_epoch"), int)):
        return _error("expected_decision_epoch must be an integer", 400)
    if kind == "apply":
        # Payload-shape rejection lives HERE, the same layer every
        # other job kind's own payload-shape checks already live
        # Existence/kind/completion/readiness/digest
        # resolution of `approval_job_id` is deferred to the executor
        # (`infra/worker/apply.py::_resolve_job`), NOT checked here --
        # same posture as "approval"'s own deferred `proposal_job_id`/
        # `review_job_id` resolution: a bad/BLOCKED/digest-mismatched
        # `approval_job_id` must surface as a real, durable FAILED job, not
        # a submission-time 400.
        # The same `apply` kind carries two approval SUBJECTS -- a
        # CHANGE (`approval_job_id`) or a RESTORE (`restore_approval_id`) --
        # never both (ambiguous authority). The creation permission is mapped
        # per subject; the executor re-checks the approver per subject.
        if (payload.get("approval_job_id") is not None
                and payload.get("restore_approval_id") is not None):
            return _error("apply payload names two approval subjects", 400)
        apply_subject = "RESTORE" if payload.get("restore_approval_id") is not None else "CHANGE"
        subject_key = "restore_approval_id" if apply_subject == "RESTORE" else "approval_job_id"
        for key in (subject_key, "expected_approval_digest"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                return _error(f"apply requires {key}", 400)
        permission = APPLY_CREATE_SUBJECT_PERMISSION[apply_subject]
    try:
        authority = get_authority()
        try:
            require(authority, permission)
        except PermissionDenied:
            # A reviewer/approver may ask a knowledge or GAP question about what they may read: `ai.use` is enough for that one kind
            # (the table above keeps `source.import`, the contract for importing members). Still needs a signed-in member, `ai.use`
            # (checked next) and the same scope pinning below.
            if not (kind == "gap.answer" and "ai.use" in permissions_for(authority.get("roles"))):
                raise
        from infra import ai_admission
        if kind in ai_admission.SELF_SERVE_KINDS:
            require(authority, "ai.use")      # a standing member permission, read from the server-side membership
        if kind in ("analysis", "extraction", "gap.answer", "impact.judge", "change.draft") and not os.environ.get("RIPPLE_V2_BLOB_BUCKET"):
            return _error(f"{kind} source storage unavailable", 503)
        concurrency_limit, budget_reservation = _enforce_job_quota(
            store, authority["tenant_id"], workspace_id, kind)
        head = store.head(authority["tenant_id"], workspace_id)
        if kind == "gap.answer":
            # Scope pinning at admission: every artifact id must exist in THIS workspace's head revision.
            known = {a["artifact_id"] for a in store.source_revision_view(
                authority["tenant_id"], workspace_id, head.source_revision_id)["artifacts"]}                 if head.source_revision_id else set()
            if any(i not in known for i in payload["artifact_ids"]):
                return _error("gap.answer: artifact_ids not in the workspace head revision", 400)
        if kind == "impact.judge":
            # Scope pinning at admission: target artifact and every scoped artifact must be in THIS workspace's head.
            known = {a["artifact_id"] for a in store.source_revision_view(
                authority["tenant_id"], workspace_id, head.source_revision_id)["artifacts"]} if head.source_revision_id else set()
            if any(i not in known for i in payload["scope_artifact_ids"]):
                return _error("impact.judge: scope_artifact_ids not in the workspace head revision", 400)
            from infra import v2_judge_bounds
            refused = v2_judge_bounds.refusal(store, authority["tenant_id"], workspace_id, head.source_revision_id, payload)
            if refused:
                return _error(refused, 409)
        # An "impact" job carrying an already-stale
        # `expected_decision_epoch` (below the workspace's real current
        # head) must be denied BEFORE any traversal/investigation logic
        # ever runs -- checked here, at job-admission time, against the
        # SAME `head` every job of every kind is about to be pinned to
        # (`PostgresV2Store._create_job_core` pins `head.decision_epoch`
        # unconditionally already; this adds the explicit staleness DENY a
        # caller-declared expectation lets a client detect before submitting
        # work against knowledge it no longer believes is current).
        # "whatif" reuses this EXACT SAME check (same code path,
        # not a second staleness mechanism) -- a whatif
        # preview request carrying a stale `expected_decision_epoch` is
        # denied here too, before the executor ever runs.
        # "proposal" reuses the identical check.
        # "review" reuses the identical check too (the SAME tuple is
        # extended, not a second mechanism).
        # "approval" reuses the identical check too -- one shared
        # staleness mechanism across the whole impact/whatif/proposal/
        # review/approval family, not a parallel one.
        expected_epoch = (payload.get("expected_decision_epoch")
                          if kind in ("impact", "whatif", "proposal", "review", "approval", "change.brief", "change.draft")
                          else None)
        if expected_epoch is not None and expected_epoch != head.decision_epoch:
            return _error(
                f"stale decision epoch: expected {expected_epoch}, "
                f"current {head.decision_epoch}", 409)
        if kind == "proposal":
            # Allowlist enforcement (C2-a): the target must be a REAL
            # artifact that already exists in the CURRENT published source
            # -- an injection attempt naming a file that was never
            # published at all (e.g. "unrelated-secret-file.py") is
            # rejected here. This is a real, provable scope proxy (no
            # support-matrix "impacted files" list exists anywhere in this
            # repo yet -- a possible refinement is to narrow this further to
            # the REVIEWED impact scope specifically, not merely "exists in
            # the current source"); it is not a guessed/looser check, since
            # it denies exactly the injection shape of a named unpublished file.
            try:
                view = store.source_revision_view(
                    authority["tenant_id"], workspace_id, head.source_revision_id)
            except TenantScopeError:
                return _error("current source revision not found", 400)
            known_artifact_ids = {a["artifact_id"] for a in view["artifacts"]}
            if payload.get("target_artifact_id") not in known_artifact_ids:
                return _error(
                    "target_artifact_id not found in the current published "
                    "source (outside allowlist)", 400)
            if payload.get("action") == "revise":
                prev_id = payload.get("previous_proposal_job_id")
                try:
                    prev_job = store.job_record(authority["tenant_id"], workspace_id, prev_id)
                except TenantScopeError:
                    return _error("previous_proposal_job_id not found", 400)
                if prev_job.get("kind") != "proposal":
                    return _error("previous_proposal_job_id is not a proposal job", 400)
        if kind == "review":
            # The real candidate under review must already exist,
            # be a real "proposal" job, in THIS tenant/workspace -- the same
            # existence/kind check "revise" already applies to
            # `previous_proposal_job_id` above (one shared
            # pattern, not a second one). `evaluated_hash` truth (not just
            # its type) is verified later by the executor itself, never
            # trusted from the caller (see infra/worker/review.py).
            proposal_job_id = payload.get("proposal_job_id")
            try:
                proposal_job = store.job_record(
                    authority["tenant_id"], workspace_id, proposal_job_id)
            except TenantScopeError:
                return _error("proposal_job_id not found", 400)
            if proposal_job.get("kind") != "proposal":
                return _error("proposal_job_id is not a proposal job", 400)
            from infra import v2_lineage
            if v2_lineage.newer_draft_for_proposal(store, authority["tenant_id"], workspace_id, proposal_job_id):
                return _error(v2_lineage.SUPERSEDED_MSG, 409)
            if payload.get("verification_contract_id") is not None and store.verification_contract(
                    authority["tenant_id"], workspace_id,
                    payload["verification_contract_id"]) is None:
                return _error("verification_contract_id not found", 400)
        if kind == "approval":
            from infra import v2_lineage
            if v2_lineage.newer_draft_for_proposal(store, authority["tenant_id"], workspace_id, payload.get("proposal_job_id")):
                return _error(v2_lineage.SUPERSEDED_MSG, 409)
        if kind == "change.draft" and payload.get("revises_draft_job_id"):
            from infra import v2_lineage
            try:
                prev = store.job_record(authority["tenant_id"], workspace_id, payload["revises_draft_job_id"])
            except TenantScopeError:
                return _error("revises_draft_job_id not found", 400)
            if prev.get("kind") != "change.draft" or prev.get("execution_status") != "COMPLETED":
                return _error("revises_draft_job_id must be a completed draft", 400)
            if (prev.get("payload") or {}).get("intent_id") != payload.get("intent_id"):
                return _error("a revision must belong to the same request as the draft it revises", 400)
            if v2_lineage.superseding_draft(store, authority["tenant_id"], workspace_id, payload["revises_draft_job_id"]):
                return _error("that draft was already revised; revise the latest draft", 409)
        if kind == "proposal" and payload.get("draft_job_id"):
            from infra import v2_lineage
            newer = v2_lineage.superseding_draft(store, authority["tenant_id"], workspace_id, payload["draft_job_id"])
            if newer:
                return _error("this draft was revised after new confirmed feedback; build the proposal from the latest draft", 409)
        if kind == "apply" and payload.get("approval_job_id"):
            from infra import v2_lineage
            try:
                appr = store.job_record(authority["tenant_id"], workspace_id, payload["approval_job_id"])
                prop = store.job_record(authority["tenant_id"], workspace_id, (appr.get("payload") or {}).get("proposal_job_id") or "")
                src_draft = (prop.get("payload") or {}).get("draft_job_id")
            except TenantScopeError:
                src_draft = None
            if src_draft and v2_lineage.superseding_draft(store, authority["tenant_id"], workspace_id, src_draft):
                return _error("the draft behind this approval was revised; a fresh exact approval is required before applying", 409)
        if kind == "proposal" and payload.get("draft_job_id") and payload.get("simulation_job_id"):
            # a simulation the server built from ANOTHER draft cannot stand in for this change's test
            try:
                sim_job = store.job_record(authority["tenant_id"], workspace_id, payload["simulation_job_id"])
            except TenantScopeError:
                sim_job = None
            built_from = (((sim_job or {}).get("execution_result") or {}).get("built_from") or {})
            if built_from.get("draft_job_id") and built_from["draft_job_id"] != payload["draft_job_id"]:
                return _error("simulation_job_id was built from a different draft; run the simulation for this draft", 400)
            if built_from.get("baseline_source_revision_id") and built_from["baseline_source_revision_id"] != head.source_revision_id:
                return _error("the simulation was run on an older source revision than the current one; run it again for the current revision", 409)
        if kind == "apply" and payload.get("restore_approval_id") is not None:
            # Approval existence + digest + distinct-approver policy +
            # the job row in ONE transaction (`v2_restore.create_restore_apply_job`);
            # publication is still ONLY the apply gate (`ApplyExecutor`).
            created_id = create_restore_apply_job(
                store, authority["tenant_id"], workspace_id, job_id, payload,
                submitter_id=authority["account_id"], max_concurrent_jobs=concurrency_limit,
                budget_reservation=budget_reservation)
            return JSONResponse({"job_id": created_id, "status": "PENDING_APPROVAL"},
                                headers={"Cache-Control": "no-store"})
        from infra import provider_gate
        provider_gate.require_provider(kind)
        from infra import v2_lineage
        result = store.create_job(authority["tenant_id"], workspace_id, job_id, kind, payload,
                                  locked_check=v2_lineage.job_guard(kind, payload, authority["tenant_id"], workspace_id),
                                  expected_version=head.version,
                                  idempotency_key=f"job-create-{job_id}",
                                  max_concurrent_jobs=concurrency_limit,
                                  budget_reservation=budget_reservation,
                                  # Requester attribution in the
                                  # SAME transaction as the job/outbox/reservation
                                  # insert (was a separate set_job_requester call
                                  # that could fail after the job had committed).
                                  requested_by=authority["account_id"])
        admitted = ai_admission.admit(store, authority["tenant_id"], workspace_id, result.revision_id, kind, authority)
        return JSONResponse({"job_id": result.revision_id, "status": "APPROVED" if admitted else "PENDING_APPROVAL"},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except job_quota.JobQuotaError as exc:
        return _error(f"budget_exceeded: {exc}", 409)
    except job_quota.BudgetExceeded as exc:
        return _error(f"budget_exceeded: {exc}", 409)
    except v2_lineage_DraftSuperseded as exc:
        return _error(str(exc), 409)
    except ConcurrencyLimitExceeded as exc:
        from infra import provider_gate
        return provider_gate.refusal_for(exc) or _error(f"budget_exceeded: {exc}", 409)
    except job_quota.BudgetConfigError as exc:
        # A configured-but-invalid budget config must fail loudly, not admit
        # silently -- same "no budget default" principle as the worker's own
        # startup check (contract §5), applied at the API layer too.
        return _error(f"budget config error: {exc}", 500)
    except RestoreRefused as exc:
        return JSONResponse({"error": exc.reasons[0], "reasons": exc.reasons},
                            status_code=exc.status, headers={"Cache-Control": "no-store"})
    except (TenantScopeError, StoreConflict, StaleVersion) as exc:
        # StaleVersion: no source head yet / pinned head moved. A named 409, never an unhandled 500.
        return _error(str(exc), 409)


@router.get("/v2/jobs/{job_id}")
def job_status(request: Request, job_id: str, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        from infra.v2_redaction import redact_job_record
        from infra import v2_job_visibility
        record = store.job_record(authority["tenant_id"], workspace_id, job_id)
        if not v2_job_visibility.readable(store, authority["tenant_id"], workspace_id, record, authority):
            raise TenantScopeError(v2_job_visibility.NOT_FOUND)                  # the kind's own read rule (RQA-01): no bypass through the generic read
        out = redact_job_record(v2_job_visibility.shaped(record), authority)
        from infra.provider_gate import AI_KINDS
        if record["execution_status"] == "FAILED" and record["kind"] in AI_KINDS:
            # Tell the page whether a retry can run at all: a job that used every provider attempt is refused by the worker (and by the retry route).
            try:
                used = store.provider_attempts_used(authority["tenant_id"], workspace_id, [job_id])[job_id]
                limit = job_quota.provider_attempt_limit()
                out["retry"] = {"allowed": used < limit, "attempts_used": used, "attempt_limit": limit}
            except job_quota.BudgetConfigError:
                pass
        return JSONResponse(out, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/jobs/{job_id}/approve")
@db_route
def approve_job(request: Request, job_id: str):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        job = store.job_record(authority["tenant_id"], workspace_id, job_id)
        from infra import v2_job_visibility
        if not v2_job_visibility.readable(store, authority["tenant_id"], workspace_id, job, authority):
            raise TenantScopeError(v2_job_visibility.NOT_FOUND)                  # who may not read a job may not act on it (RQA-01)
        permission = _job_permission(job)
        if permission is None:
            return _error("unsupported job kind", 400)
        require(authority, permission)
        policy = store.workspace_policy(authority["tenant_id"], workspace_id)
        distinct_required = policy["require_distinct_approver"]
        require_distinct_approver(job["requested_by"], authority["account_id"],
                                  require_distinct=distinct_required)
        is_self_approval = (not distinct_required
                            and job["requested_by"] == authority["account_id"])
        # Self-approval is allowed only because the workspace relaxed the
        # policy; that relaxation must always be visible in the audit trail,
        # not silently permitted -- and the transition + audit write are one
        # atomic transaction (see PostgresV2Store.approve_job), so a failure
        # writing the required audit event rolls back the approval too. The
        # job never ends up durably APPROVED with zero audit events.
        store.approve_job(authority["tenant_id"], workspace_id, job_id,
                          authority["account_id"], tuple(sorted(authority["roles"])),
                          self_approval_audit=(
                              {"job_id": job_id, "account_id": authority["account_id"]}
                              if is_self_approval else None))
        return JSONResponse({"job_id": job_id, "status": "APPROVED"},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


def _job_permission(job: dict):
    """Per-job-KIND permission for approve/publish/cancel/retry, except that an
    `apply` job carrying a RESTORE subject is controlled by the RESTORE
    permission (a `source.import` holder must not steer a restore)."""
    if (job["kind"] == "apply" and isinstance(job.get("payload"), dict)
            and job["payload"].get("restore_approval_id") is not None):
        return APPLY_CREATE_SUBJECT_PERMISSION["RESTORE"]
    return JOB_APPROVE_PERMISSION.get(job["kind"])


def _decision_payload_hash(action: str, reason: str, expected_digest: str | None,
                           high_risk: bool) -> str:
    return canonical_hash({"action": action, "reason": reason,
                           "expected_approval_digest": expected_digest, "high_risk": high_risk})


@router.post("/v2/jobs/{job_id}/decision")
@db_route
def decide_job(request: Request, job_id: str):
    """AI7-T04: human approve/reject/request_changes on a `kind=approval`
    job (`job_id` is that approval job's OWN id -- L-031, never
    `proposal_job_id`). Distinct from the job-EXECUTION `/approve` route
    above: that gates whether a job of ANY kind may run at all
    (`JOB_APPROVE_PERMISSION`); this gates the human CONTENT decision on an
    already-COMPLETED approval bundle, under the `change.approve`
    permission reserved for exactly this since AI2-T02 but never exercised
    before this task. Body `actor_id`/`patch`/hash overrides are never
    trusted -- only the server-resolved authority and the approval job's
    OWN persisted `execution_result` are used, per this module's own
    documented invariant.
    """
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    action = body.get("action")
    if action not in ("approve", "reject", "request_changes"):
        return _error("action must be one of approve|reject|request_changes", 400)
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _error("a non-empty reason is required", 400)
    expected_digest = body.get("expected_approval_digest")
    if not isinstance(expected_digest, str) or not expected_digest:
        return _error("expected_approval_digest required", 400)
    idempotency_key = body.get("idempotency_key")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return _error("idempotency_key must be a string", 400)
    high_risk = bool(body.get("high_risk", False))
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "change.approve")
        job = store.job_record(authority["tenant_id"], workspace_id, job_id)
        from infra import v2_job_visibility
        if not v2_job_visibility.readable(store, authority["tenant_id"], workspace_id, job, authority):
            raise TenantScopeError(v2_job_visibility.NOT_FOUND)                  # who may not read a job may not act on it (RQA-01)
        if job["kind"] != "approval":
            return _error("job is not an approval job", 400)
        execution_result = job.get("execution_result") or {}
        if job.get("execution_status") != "COMPLETED" or not execution_result:
            return _error("approval job has not completed yet", 409)
        if action == "approve":
            from infra import v2_lineage
            if v2_lineage.newer_draft_for_proposal(store, authority["tenant_id"], workspace_id, (job.get("payload") or {}).get("proposal_job_id")):
                return _error(v2_lineage.SUPERSEDED_MSG, 409)         # rejecting or asking for changes stays possible
        stored_digest = execution_result.get("approval_digest")
        if not stored_digest or expected_digest != stored_digest:
            return _error("expected_approval_digest does not match the current approval "
                         "digest (stale or forged)", 409)
        # Rule "a BLOCKED/UNAVAILABLE review must not be applied": an `approve` decision is only
        # recordable on a bundle whose OWN computed readiness is READY (review READY_FOR_HUMAN_REVIEW,
        # no upstream blocker, not stale). reject / request_changes stay allowed on a BLOCKED bundle.
        # Deviation (disclosed): a `manual_work_required` bundle (python/markdown, classification
        # manual) publishes NO bytes -- its apply only records pending human work -- so it is the one
        # BLOCKED shape that stays approvable.
        if (action == "approve" and execution_result.get("readiness") != "READY"
                and execution_result.get("required_action") != "manual_work_required"):
            return _error("approval bundle readiness is not READY (blocked or unavailable review "
                          "check): it cannot be approved", 409)
        # Recheck freshness against the CURRENT head, not the approval job's
        # own cached snapshot alone:
        # a dependency change since the approval ran (e.g. a concurrent
        # source publish) must refuse this decision even though the
        # DIGEST STRING itself still matches what was cached at approval
        # time -- the snapshot it was computed over is no longer current.
        head = store.head(authority["tenant_id"], workspace_id)
        snapshot = execution_result.get("snapshot") or {}
        if (snapshot.get("source_revision_id") != head.source_revision_id
                or snapshot.get("decision_epoch") != head.decision_epoch):
            return _error("approval bundle is stale: a dependency changed since it was "
                         "computed", 409)
        policy = store.workspace_policy(authority["tenant_id"], workspace_id)
        distinct_required = policy["require_distinct_approver"]
        requester_id = job["requested_by"]
        self_approval = authority["account_id"] == requester_id
        if high_risk and self_approval:
            # A high-risk decision is never bypassed by the self-approval setting --
            # unlike the job-execution `/approve` route, this is never
            # relaxable by the workspace's require_distinct_approver=False.
            raise PermissionDenied(
                "a high-risk decision always requires a distinct approver")
        require_distinct_approver(requester_id, authority["account_id"],
                                  require_distinct=distinct_required)
        # The person who made the PROPOSAL cannot decide it either, even when someone else prepared the approval bundle.
        try:
            proposal_requester = store.job_record(authority["tenant_id"], workspace_id,
                                                  (job.get("payload") or {}).get("proposal_job_id") or "").get("requested_by")
        except TenantScopeError:
            proposal_requester = None
        if proposal_requester:
            require_distinct_approver(proposal_requester, authority["account_id"], require_distinct=distinct_required)
        if action == "approve":
            from infra.v2_work_routes import open_change_requests, review_digest
            blocked = open_change_requests(review_digest(store, authority["tenant_id"], workspace_id, (job.get("payload") or {}).get("proposal_job_id") or "",
                                                         newest=0)["latest"])            # AH-02: effective verdict per reviewer from SQL (same result, bounded work)
            if blocked:
                return _error("a reviewer asked for changes on this proposal (" + ", ".join(blocked) + "); it cannot be approved until they mark it ready "
                              "or the requester sends a revised proposal", 409)
        payload_hash = _decision_payload_hash(action, reason, expected_digest, high_risk)
        new_proposal_payload = None
        new_proposal_job_id = None
        new_proposal_idempotency_key = None
        if action == "request_changes":
            proposal_job_id = job["payload"].get("proposal_job_id")
            prev_proposal = store.job_record(authority["tenant_id"], workspace_id, proposal_job_id)
            new_proposal_payload = dict(prev_proposal["payload"])
            new_proposal_payload["action"] = "revise"
            new_proposal_payload["previous_proposal_job_id"] = proposal_job_id
            new_proposal_job_id = "prop-" + job_id + "-" + os.urandom(8).hex()
            new_proposal_idempotency_key = f"job-create-{new_proposal_job_id}"
        result = store.record_change_decision(
            authority["tenant_id"], workspace_id, job_id,
            actor_id=authority["account_id"], action=action, reason=reason,
            approval_digest=expected_digest, self_approval=self_approval,
            high_risk=high_risk, idempotency_key=idempotency_key, payload_hash=payload_hash,
            new_proposal_job_id=new_proposal_job_id,
            new_proposal_payload=new_proposal_payload,
            new_proposal_idempotency_key=new_proposal_idempotency_key,
            locked_check=_v2_lineage.proposal_guard(authority["tenant_id"], workspace_id, (job.get("payload") or {}).get("proposal_job_id")))
        response = {"job_id": job_id, "decision_id": result["decision_id"], "action": action,
                   "actor_id": authority["account_id"], "approval_digest": expected_digest,
                   "reason": reason, "self_approval": self_approval}
        if result.get("new_proposal_job_id"):
            response["new_proposal_job_id"] = result["new_proposal_job_id"]
        return JSONResponse(response, headers={"Cache-Control": "no-store"})
    except IdentityDenied as exc:
        # A valid session with no CURRENT membership in this workspace (revoked, or never a
        # member) is an authorization refusal (403), like /v2/hitl/*, /v2/restore/* and the
        # refresh routes; only a missing/invalid session is 401.
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except v2_lineage_DraftSuperseded as exc:
        return _error(str(exc), 409)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.get("/v2/jobs/{job_id}/decisions")
def decision_history(request: Request, job_id: str, workspace_id: str):
    """AI7-T04 read surface: the approval history panel the task card's
    Outputs line names. Same authority boundary as `GET /v2/jobs/{job_id}`."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        # Confirms the job exists/is scoped to this tenant before listing
        # its decisions (never leaks cross-tenant decisions for a job_id
        # that merely collides with another tenant's).
        store.job_record(authority["tenant_id"], workspace_id, job_id)
        decisions = store.change_decisions(authority["tenant_id"], workspace_id, job_id)
        return JSONResponse({"job_id": job_id, "decisions": decisions},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/jobs/{job_id}/publish")
@db_route
def publish_job(request: Request, job_id: str):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        # The caller triggering publish must itself currently belong to the
        # workspace, but the authority that actually matters here is the
        # RECORDED APPROVER's. That recheck happens INSIDE
        # `PostgresV2Store.recheck_and_publish_job`, in the SAME serialized SQL
        # transaction that performs the final publish write, using row locks
        # that conflict with a concurrent grant/revoke -- not a separate
        # read-then-write pair. A revoke landing before, or concurrently with,
        # that transaction either is observed directly or blocks until the
        # transaction resolves; see that method's docstring for the finalized
        # interface.
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        job = store.job_record(authority["tenant_id"], workspace_id, job_id)
        from infra import v2_job_visibility
        if not v2_job_visibility.readable(store, authority["tenant_id"], workspace_id, job, authority):
            raise TenantScopeError(v2_job_visibility.NOT_FOUND)                  # who may not read a job may not act on it (RQA-01)
        if job["status"] != "APPROVED":
            return _error("job is not approved", 409)
        permission = _job_permission(job)
        store.recheck_and_publish_job(authority["tenant_id"], workspace_id, job_id, permission)
        return JSONResponse({"job_id": job_id, "status": "PUBLISHED"},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/jobs/{job_id}/cancel")
@db_route
def cancel_job_route(request: Request, job_id: str):
    """REWORK round (task card, finding B: "Add cancel ... via authenticated,
    server-side permission/scope-checked routes; define exactly which states
    allow the operation, idempotency, and the error for an operation attempted
    too late"). Any member holding the job kind's own approval permission may
    cancel it (the same authority already trusted to approve/reject the job's
    outcome); a job already published, or whose execution has already reached
    a terminal outcome, is refused with `409` -- "attempted too late", not a
    silent no-op. A second cancel call for an already-cancelled job is also a
    `409` (idempotent refusal, not a duplicate success) via the same guarded
    `PostgresV2Store.request_cancel` transition.
    """
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    reason = body.get("reason") if isinstance(body.get("reason"), str) else "cancelled_by_operator"
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        job = store.job_record(authority["tenant_id"], workspace_id, job_id)
        from infra import v2_job_visibility
        if not v2_job_visibility.readable(store, authority["tenant_id"], workspace_id, job, authority):
            raise TenantScopeError(v2_job_visibility.NOT_FOUND)                  # who may not read a job may not act on it (RQA-01)
        permission = _job_permission(job)
        if permission is None:
            return _error("unsupported job kind", 400)
        require(authority, permission)
        store.request_cancel(authority["tenant_id"], workspace_id, job_id, reason)
        return JSONResponse({"job_id": job_id, "status": "CANCELLED"},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/jobs/{job_id}/retry")
@db_route
def retry_job_route(request: Request, job_id: str):
    """REWORK round (task card, finding B: "Add ... retry via authenticated,
    server-side permission/scope-checked routes ... Retry must be bounded
    with a durable reason/retryable flag"). Only a job whose worker execution
    genuinely `FAILED` (the worker's own bounded-retry budget already
    exhausted, `execution_reason` carries the durable failure reason) may be
    retried through this route -- a job still queued, already completed, or
    cancelled is refused with `409` ("attempted too late" the other
    direction: too early, or no longer applicable). Re-enqueues a FRESH
    outbox event via `PostgresV2Store.retry_job`; the original, already-acked
    outbox row is never reused.
    """
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        job = store.job_record(authority["tenant_id"], workspace_id, job_id)
        from infra import v2_job_visibility
        if not v2_job_visibility.readable(store, authority["tenant_id"], workspace_id, job, authority):
            raise TenantScopeError(v2_job_visibility.NOT_FOUND)                  # who may not read a job may not act on it (RQA-01)
        permission = _job_permission(job)
        if permission is None:
            return _error("unsupported job kind", 400)
        require(authority, permission)
        from infra import ai_admission
        if job["kind"] in ai_admission.SELF_SERVE_KINDS:
            require(authority, "ai.use")      # a retry spends the model again: a member whose AI use is switched off cannot start it
        concurrency_limit, budget_reservation = _enforce_job_quota(
            store, authority["tenant_id"], workspace_id, job["kind"])
        from infra.provider_gate import AI_KINDS
        store.retry_job(authority["tenant_id"], workspace_id, job_id,
                        max_concurrent_jobs=concurrency_limit,
                        budget_reservation=budget_reservation,
                        max_provider_attempts=job_quota.provider_attempt_limit() if job["kind"] in AI_KINDS else None)
        return JSONResponse({"job_id": job_id, "execution_status": "QUEUED"},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except JobAttemptsExhausted as exc:
        return JSONResponse({"error": "This job already made all the provider attempts that were approved for it.",
                             "code": "provider_attempts_exhausted", "attempts_used": exc.used, "attempt_limit": exc.limit,
                             "next_action": "It will not be run again and nothing was started or charged. The approved attempts are not reset. "
                                            "If this was the re-analysis of an applied change, the published source is unchanged; choose Request changes or Reject result on that change."},
                            status_code=409, headers={"Cache-Control": "no-store"})
    except (job_quota.JobQuotaError, job_quota.BudgetExceeded,
            ConcurrencyLimitExceeded) as exc:
        return _error(f"budget_exceeded: {exc}", 409)
    except job_quota.BudgetConfigError as exc:
        return _error(f"budget config error: {exc}", 500)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.get("/v2/knowledge/draft/{artifact_id}")
def knowledge_draft(request: Request, artifact_id: str, workspace_id: str):
    """AI4-T04 V1: the pending-human draft read surface. Chains the latest
    "extraction" job for `artifact_id` with the "relation"/"finding" jobs
    that consumed its candidates (`PostgresV2Store.knowledge_draft`). Read
    only -- never accepts or applies anything; a human still confirms
    through the separate, existing `POST /v2/knowledge/confirm` decision
    axis. Same authority boundary as `GET /v2/jobs/{job_id}` (`project.read`,
    resolved fresh server-side, never from the request body)."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        draft = store.knowledge_draft(authority["tenant_id"], workspace_id, artifact_id)
        return JSONResponse(draft, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


def _self_approval_block(store: PostgresV2Store, tenant_id: str, workspace_id: str,
                         target: str, actor_id: str, *, high_risk: bool) -> None:
    """The same actor who authored an answer cannot mint
    the confirming decision for a high-risk/critical case. Best-effort:
    `v2_hitl_answers` is keyed by `question_id`,
    which for a `resolve`/`accepted_unknown` action IS the `target` (a
    gap/question id) the caller names. Only enforced when the caller
    explicitly asserts `high_risk` (the finding's own severity/authority_required
    signal is checked separately by the callers);
    this does not silently widen to every action."""
    if not high_risk:
        return
    with store._tx() as cur:
        cur.execute("""SELECT actor_id FROM v2_hitl_answers
            WHERE tenant_id=%s AND workspace_id=%s AND question_id=%s
            ORDER BY id DESC LIMIT 1""", (tenant_id, workspace_id, target))
        row = cur.fetchone()
    if row is not None and row[0] == actor_id:
        raise PermissionDenied(
            "self-approval blocked: the same actor who authored the answer "
            "cannot mint the confirming decision for a high-risk case")


@router.post("/v2/knowledge/confirm")
@db_route
def confirm_knowledge(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    claim_id = body.get("claim_id")
    if not all(isinstance(x, str) and x for x in (workspace_id, claim_id)):
        return _error("workspace_id and claim_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        _self_approval_block(store, authority["tenant_id"], workspace_id, claim_id,
                             authority["account_id"],
                             high_risk=bool(body.get("high_risk")))
        head = store.head(authority["tenant_id"], workspace_id)
        result = store.record_human_decision(
            authority["tenant_id"], workspace_id,
            actor_id=authority["account_id"], current_permission="knowledge.confirm",
            target=claim_id, action="confirm", expected_version=head.version,
            idempotency_key=f"confirm-{claim_id}-{authority['account_id']}",
            rationale=body.get("rationale"), evidence_ref=body.get("evidence_ref"),
            scope=body.get("scope"), effective_from=body.get("effective_from"),
            effective_until=body.get("effective_until"),
            # A confirmed candidate can itself be a
            # dependency other relations/findings were built over; schedule
            # the real invalidation job INSIDE record_human_decision's own
            # transaction (see that method's docstring) so a scheduling
            # failure rolls back this decision too, instead of a separate
            # post-commit `store.create_job` call that could silently drop
            # the invalidation on failure.
            schedule_invalidation_target=claim_id)
        return JSONResponse({"decision_id": result["decision_id"],
                             "provenance": result["provenance"],
                             "decision_epoch": result["decision_epoch"]},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied as exc:
        # A valid session with no CURRENT membership in this workspace (revoked, or never a
        # member) is an authorization refusal (403), like /v2/hitl/*, /v2/restore/* and the
        # refresh routes; only a missing/invalid session is 401.
        status = _identity_denied_status(exc)
        return _error("sign in required" if status == 401 else "permission denied", status)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict, StaleVersion) as exc:
        return _error(str(exc), 409)                  # StaleVersion: no source head yet / the head moved: a named 409, never a 500


_INDEX_VERSION = "v1"


def _build_blocker(finding: dict) -> dict:
    """Structured blocker DTO -- a plain dict of real fields the finding already
    carries (`severity`/`authority_required`), never a prose
    string a downstream consumer would have to parse."""
    return {"finding_id": finding.get("finding_id"),
           "severity": finding.get("severity"),
           "authority_required": finding.get("authority_required"),
           "reason": "critical_gap_unconfirmed"}


def _index_build(store: PostgresV2Store, tenant_id: str, workspace_id: str,
                 revision_id: str, idempotency_prefix: str) -> None:
    """Index build OUTSIDE the accept transaction, with the
    BUILDING->READY/FAILED pending-marker semantics `record_index_manifest`/
    `current_index_ready` already implement.
    A failed build is recorded as FAILED, never silently left as an absent
    row that `current_index_ready` would also read as not-ready -- but an
    explicit FAILED row is the durable signal an operator/AI caller can act
    on (a failed build never serves an old vector as current). This never
    touches the accept transaction/CAS guard: it runs strictly after
    `publish_knowledge` has already committed the new head."""
    manifest_hash = f"manifest-{revision_id}"
    try:
        store.record_index_manifest(
            tenant_id, workspace_id, revision_id, _INDEX_VERSION,
            manifest_hash, "BUILDING",
            idempotency_key=f"{idempotency_prefix}-building")
        # V1: no real vector/embedding build pipeline exists yet (out of
        # scope here -- the retrieval index is a SEPARATE,
        # source_revision_id-scoped primitive, already reviewed). Marking
        # READY here is the smallest correct wiring of the EXISTING
        # `record_index_manifest` contract; a real async build worker can
        # replace this synchronous call later without changing the contract
        # (BUILDING -> READY/FAILED, keyed by knowledge_revision_id).
        store.record_index_manifest(
            tenant_id, workspace_id, revision_id, _INDEX_VERSION,
            manifest_hash, "READY",
            idempotency_key=f"{idempotency_prefix}-ready")
    except Exception:
        try:
            store.record_index_manifest(
                tenant_id, workspace_id, revision_id, _INDEX_VERSION,
                manifest_hash, "FAILED",
                idempotency_key=f"{idempotency_prefix}-failed")
        except Exception:
            pass


@router.post("/v2/knowledge/accept")
@db_route
def accept_knowledge(request: Request):
    """AI5-T04: the acceptance-gated publish route the task card calls
    'knowledge.confirm -> current permission/inputs + acceptance gate ->
    immutable KM/CAS publication -> projection/index build -> scoped
    query'. `POST /v2/knowledge/confirm` (above) only records the human
    decision; it never publishes a knowledge revision (see that route's
    docstring and `test_confirming_a_decision_today_does_not_publish_any_
    knowledge`). This route is the separate, explicit acceptance step that
    calls the EXISTING, already-reviewed `publish_knowledge` CAS primitive
    verbatim -- no CAS/idempotency logic is reimplemented here.

    Body: workspace_id, knowledge_revision_id, source_revision_id,
    decision_epoch, expected_version, idempotency_key, optional `target`
    (a finding/claim id this KM digest is accepting -- when given, the
    critical-gap blocker check below applies to it; unrelated KM accepts
    with no `target` are not blocked by any other workspace's gap).
    """
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    knowledge_revision_id = body.get("knowledge_revision_id")
    source_revision_id = body.get("source_revision_id")
    decision_epoch = body.get("decision_epoch")
    expected_version = body.get("expected_version")
    idempotency_key = body.get("idempotency_key")
    target = body.get("target")
    if not all(isinstance(x, str) and x for x in
              (workspace_id, knowledge_revision_id, source_revision_id,
               idempotency_key)):
        return _error("workspace_id, knowledge_revision_id, "
                     "source_revision_id and idempotency_key required", 400)
    if not isinstance(decision_epoch, int) or not isinstance(expected_version, int):
        return _error("decision_epoch and expected_version must be integers", 400)
    try:
        # Same gate as `/v2/knowledge/confirm`: a real human actor with the
        # `knowledge.confirm` permission, re-resolved fresh server-side on
        # EVERY call -- a revoked confirmer's stale session/role never
        # reaches `publish_knowledge` (this IS the "revoked confirmer
        # cannot accept" guard the K2 test names; `_authority`/`require`
        # re-check live, not from any cached/body-supplied role).
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        # Critical-gap blocker (a critical gap blocks the affected change but
        # not unrelated reads): reuses the SAME real
        # severity/authority_required signal `POST /v2/gap/accepted_unknown`
        # already trusts (`store.find_finding_by_id`), not a second,
        # parallel criticality mechanism. Blocks ONLY this accept (the
        # affected change); an unrelated `GET /v2/knowledge/draft/...` or
        # `GET /v2/knowledge/current` read for a DIFFERENT target/workspace
        # is never touched by this check.
        if isinstance(target, str) and target:
            finding = store.find_finding_by_id(
                authority["tenant_id"], workspace_id, target)
            if finding is not None and finding.get("severity") in ("HIGH", "CRITICAL") \
                    and finding.get("authority_required") \
                    and store.active_decision_action(
                        authority["tenant_id"], workspace_id, target) is None:
                return JSONResponse(
                    {"error": "critical_gap_blocked", "blocker": _build_blocker(finding)},
                    status_code=409, headers={"Cache-Control": "no-store"})
        result = store.publish_knowledge(
            authority["tenant_id"], workspace_id, knowledge_revision_id,
            source_revision_id, decision_epoch,
            expected_version=expected_version, idempotency_key=idempotency_key,
            actor_id=authority["account_id"])
        # Index build strictly OUTSIDE the accept transaction (with a pending
        # marker per revision).
        # A failure here never rolls back the already-committed publish and
        # never makes `current_index_ready` report a stale READY for this
        # (now current) revision -- it is recorded FAILED instead.
        _index_build(store, authority["tenant_id"], workspace_id,
                    knowledge_revision_id, f"idx-{knowledge_revision_id}")
        return JSONResponse({"version": result.version,
                            "knowledge_revision_id": result.revision_id},
                           headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)
    except StaleVersion as exc:
        return _error(str(exc), 409)


@router.get("/v2/knowledge/current")
def knowledge_current(request: Request, workspace_id: str,
                      index_version: str = _INDEX_VERSION):
    """Authoritative current-KM read: real workspace head plus real index
    readiness for `index_version` (default 'v1') via `current_index_ready`
    -- never a cross-revision fallback (see `current_index_ready`'s own
    'READY cannot regress'/current-head-join semantics)."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        head = store.head(authority["tenant_id"], workspace_id)
        ready = store.current_index_ready(
            authority["tenant_id"], workspace_id, index_version)
        return JSONResponse({
            "version": head.version,
            "knowledge_revision_id": head.knowledge_revision_id,
            "source_revision_id": head.source_revision_id,
            "decision_epoch": head.decision_epoch,
            "index_version": index_version, "index_ready": ready},
            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/knowledge/history")
def knowledge_history(request: Request, workspace_id: str):
    """Revision-comparison / current-vs-historic query route (task card
    item 5; absent before this round -- `test_current_vs_historic_query_
    route_missing` pinned the 404). Returns every real, INSERT-only
    `v2_knowledge_revisions` row for this tenant/workspace (ancestry/epoch/
    source per revision) plus which one is CURRENT, so a caller can compare
    two revisions without ever mixing tenant/workspace scope."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        head = store.head(authority["tenant_id"], workspace_id)
        revisions = store.knowledge_revisions(authority["tenant_id"], workspace_id)
        return JSONResponse({
            "current_knowledge_revision_id": head.knowledge_revision_id,
            "current_decision_epoch": head.decision_epoch,
            "revisions": revisions}, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/change/loop-status")
def change_loop_status(request: Request, workspace_id: str, draft_job_id: str | None = None,
                       apply_job_id: str | None = None):
    """B3: where a change stands after apply (stages, residual sources). Read-only; project.read."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        if not draft_job_id or not apply_job_id:
            return _error("draft_job_id and apply_job_id required", 400)
        from infra.worker.change_loop import loop_status
        return JSONResponse(loop_status(store, authority["tenant_id"], workspace_id, draft_job_id, apply_job_id),
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.get("/v2/knowledge/blockers")
def knowledge_blockers(request: Request, workspace_id: str):
    """Partial-KM coverage/blocker DTO route (task card item 6; absent
    before this round -- `test_partial_km_critical_gap_blocker_dto_route_
    missing` pinned the 404). Lists every real, persisted finding whose
    real severity is HIGH/CRITICAL and `authority_required` is true and
    that has no active confirming decision yet -- the same structured DTO
    shape `POST /v2/knowledge/accept` blocks on, so AI6/AI7 never has to
    parse prose to learn what is blocking an affected change. Scoped
    strictly to `workspace_id`'s own findings; a caller asking about a
    DIFFERENT workspace never sees this workspace's blockers (K3 'unrelated
    reads vẫn scoped')."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        tenant_id = authority["tenant_id"]
        blockers = []
        for finding in store.list_critical_findings(tenant_id, workspace_id):
            finding_id = finding.get("finding_id")
            if finding_id and store.active_decision_action(
                    tenant_id, workspace_id, finding_id) is not None:
                continue
            blockers.append(_build_blocker(finding))
        return JSONResponse({"workspace_id": workspace_id, "blockers": blockers},
                           headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/conflict/resolve")
@db_route
def resolve_conflict(request: Request):
    """AI5-T02 D1: 'chon authority cua rule khong sua observations.' Picking
    an authority (e.g. SOP over code) writes a decision only -- never edits
    the underlying observation/candidate. Uses the same append-only ledger
    write as confirm, action='resolve', gated by 'conflict.resolve' (already
    granted to 'knowledge_owner', per the preflight's item 7 mapping)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    conflict_id = body.get("conflict_id") or body.get("target")
    authoritative_claim_id = body.get("authoritative_claim_id")
    if not all(isinstance(x, str) and x for x in
              (workspace_id, conflict_id, authoritative_claim_id)):
        return _error("workspace_id, conflict_id and authoritative_claim_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "conflict.resolve")
        _self_approval_block(store, authority["tenant_id"], workspace_id, conflict_id,
                             authority["account_id"],
                             high_risk=bool(body.get("high_risk")))
        head = store.head(authority["tenant_id"], workspace_id)
        result = store.record_human_decision(
            authority["tenant_id"], workspace_id,
            actor_id=authority["account_id"], current_permission="conflict.resolve",
            target=conflict_id, action="resolve", expected_version=head.version,
            idempotency_key=f"resolve-{conflict_id}-{authority['account_id']}",
            rationale=body.get("rationale"), evidence_ref=body.get("evidence_ref"),
            scope={"authoritative_claim_id": authoritative_claim_id,
                  **(body.get("scope") or {})},
            effective_from=body.get("effective_from"),
            effective_until=body.get("effective_until"),
            conflict_finding_id=conflict_id,
            actor_kind=authority.get("actor_kind", "human"),
            actor_permissions=permissions_for(authority.get("roles")),
            # The real invalidation job --
            # dependency scan, stale marking, deduplicated reanalysis
            # scheduling, causation chain -- is created INSIDE this same
            # decision transaction (see `record_human_decision`'s docstring),
            # not as a separate post-commit call that a crash/failure could
            # silently drop.
            schedule_invalidation_target=authoritative_claim_id)
        return JSONResponse({"decision_id": result["decision_id"],
                             "provenance": result["provenance"],
                             "decision_epoch": result["decision_epoch"]},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except LifecycleError as exc:
        return _error(f"conflict resolution rejected: {exc}", 400)
    except AuthorizationError as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


def _supersede_or_revoke(request_body: dict, *, action: str, store, authority):
    workspace_id = request_body.get("workspace_id")
    decision_id = request_body.get("decision_id")
    if not all(isinstance(x, str) and x for x in (workspace_id, decision_id)):
        raise ValueError("workspace_id and decision_id required")
    head = store.head(authority["tenant_id"], workspace_id)
    kwargs = {"supersedes": decision_id} if action == "supersede" else {"revokes": decision_id}
    return store.record_human_decision(
        authority["tenant_id"], workspace_id,
        actor_id=authority["account_id"], current_permission="knowledge.confirm",
        target=decision_id, action=action, expected_version=head.version,
        idempotency_key=f"{action}-{decision_id}-{authority['account_id']}",
        rationale=request_body.get("reason") or request_body.get("rationale"),
        evidence_ref=request_body.get("evidence_ref"), **kwargs)


@router.post("/v2/decisions/supersede")
@db_route
def supersede_decision(request: Request):
    """AI5-T02 D1: 'Supersede/revoke decision bang event moi; khong sua/xoa
    lich su.' Adds a NEW decision row that references the prior one; the
    prior row's own columns are never touched, only its nullable
    `superseded_by` back-link is set (see `record_human_decision`)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        result = _supersede_or_revoke(body, action="supersede", store=store,
                                      authority=authority)
        return JSONResponse({"decision_id": result["decision_id"],
                             "provenance": result["provenance"]},
                            headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return _error(str(exc), 400)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/decisions/revoke")
@db_route
def revoke_decision(request: Request):
    """AI5-T02 D1: revoke is the same append-only-event pattern as supersede,
    with its own `action='revoke'`/`revoked_by` back-link so the two are
    distinguishable in the ledger read (AI5-T04)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        result = _supersede_or_revoke(body, action="revoke", store=store,
                                      authority=authority)
        return JSONResponse({"decision_id": result["decision_id"],
                             "provenance": result["provenance"]},
                            headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return _error(str(exc), 400)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.post("/v2/gap/accepted_unknown")
@db_route
def gap_accepted_unknown(request: Request):
    """AI5-T02 D2: 'unknown chi duoc accepted_unknown theo policy, khong tao
    thong tin thieu. Chan no khi critical cho change.' Reuses the EXISTING,
    already-reviewed `core/change_intelligence/v2/lifecycle.py` `gap` machine
    guard (`OPEN/REOPENED -> ACCEPTED_UNKNOWN`) for the reason/scope/owner/
    review_at completeness check, rather than re-implementing an equivalent
    check inline (task card: 'khong tao nguon authority song song').

    Critical gate (AI5-T02 cross-task corrective round, closing REVIEWER's
    BLOCKING-W1 finding on the round-2 verdict): AI4-T03's `FindingsExecutor`
    (`infra/worker/findings.py`) now populates a REAL, documented
    `severity`/`authority_required` signal on every persisted finding, derived
    from the involved `RuleCandidate`(s)' `modality` (must/may/observed --
    see `_severity_and_authority`'s docstring there). This route looks up the
    real finding named by `gap_id` (via `store.find_finding_by_id`) and takes
    ITS `severity` as authoritative: a finding whose real severity is
    `HIGH`/`CRITICAL` is blocked regardless of what the caller claims in
    `critical` -- the caller's own `critical: bool` can only ADD an extra
    block (e.g. for a caller's own out-of-band judgment), it can never
    override or downgrade a real HIGH/CRITICAL signal into a false `false`.
    If no matching finding is found (e.g. `gap_id` does not name a real,
    persisted finding), there is no real signal to fall back on and the
    caller's own `critical` assertion is used alone -- documented, not
    silently treated as automatically safe."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    gap_id = body.get("gap_id")
    if not all(isinstance(x, str) and x for x in (workspace_id, gap_id)):
        return _error("workspace_id and gap_id required", 400)
    from core.change_intelligence.v2.lifecycle import LifecycleError, authorize_transition, TransitionContext

    reason = body.get("reason") or ""
    scope = body.get("scope") or ""
    owner_field = body.get("owner") or ""
    review_at = body.get("review_at") or ""
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        require(authority, "knowledge.confirm")
        # Real signal takes precedence over (cannot be downgraded by) the
        # caller's own `critical` claim -- see the docstring above.
        real_finding = store.find_finding_by_id(
            authority["tenant_id"], workspace_id, gap_id)
        real_severity = (real_finding or {}).get("severity")
        critical_by_signal = real_severity in ("HIGH", "CRITICAL")
        if critical_by_signal or body.get("critical"):
            return _error(
                "critical_unknown_blocked: accepted_unknown is not "
                "permitted for a critical-for-change gap "
                f"(real_severity={real_severity!r}, "
                f"caller_asserted={bool(body.get('critical'))})", 409)
        head = store.head(authority["tenant_id"], workspace_id)
        current_perms = permissions_for(authority.get("roles"))
        authorize_transition("gap", "OPEN", "ACCEPTED_UNKNOWN", TransitionContext(
            actor_id=authority["account_id"], actor_kind=authority.get("actor_kind", "human"),
            permissions=current_perms, current_permissions=current_perms,
            expected_revision=str(head.version), current_revision=str(head.version),
            policy_version=str(head.policy_version), current_policy_version=str(head.policy_version),
            decision_epoch=head.decision_epoch, current_decision_epoch=head.decision_epoch,
            unknown_reason=reason, unknown_scope=scope, unknown_owner=owner_field,
            unknown_review_at=review_at))
        result = store.record_human_decision(
            authority["tenant_id"], workspace_id,
            actor_id=authority["account_id"], current_permission="knowledge.confirm",
            target=gap_id, action="accepted_unknown", expected_version=head.version,
            idempotency_key=f"accepted_unknown-{gap_id}-{authority['account_id']}",
            rationale=reason, evidence_ref=body.get("evidence_ref"),
            scope={"scope": scope, "owner": owner_field, "review_at": review_at})
        return JSONResponse({"decision_id": result["decision_id"],
                             "provenance": result["provenance"]},
                            headers={"Cache-Control": "no-store"})
    except LifecycleError as exc:
        return _error(f"accepted_unknown rejected: {exc}", 400)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.get("/v2/hitl/queue")
def hitl_queue(request: Request, workspace_id: str):
    """AI5-T01: the human work queue read surface. Reuses
    `PostgresV2Store.knowledge_draft`'s own chain reconstruction (L-024) for
    every artifact this workspace has an extraction job for, and redacts raw
    source-quote evidence from the DTO for a caller who does not hold the
    "evidence.read" permission -- redaction happens inside the store method,
    not as an after-the-fact filter here, so there is no serialize-then-strip
    bypass path."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        redact = "evidence.read" not in permissions_for(authority.get("roles"))
        queue = store.hitl_queue(authority["tenant_id"], workspace_id,
                                 redact_evidence=redact)
        return JSONResponse(queue, headers={"Cache-Control": "no-store"})
    except IdentityDenied as exc:
        return _error(str(exc), _identity_denied_status(exc))
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/hitl/answer")
@db_route
def hitl_answer(request: Request):
    """AI5-T01: an answer draft for a pending question -- `knowledge.answer`
    only (already the "requester" role's permission, never granted to
    "knowledge_owner"). This NEVER itself closes the GAP/question or mints a
    confirmed decision; only the separate, existing `POST /v2/knowledge/confirm`
    axis (a different permission, a different actor) can do that. Concurrency
    is enforced via a required `expected_version` (present-but-null means "no
    prior answer yet"; the key being absent entirely is refused as a
    malformed request, matching every other v2 mutating route's required-
    field contract)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    question_id = body.get("question_id")
    answer_text = body.get("answer")
    idempotency_key = body.get("idempotency_key")
    if not all(isinstance(x, str) and x for x in
              (workspace_id, question_id, answer_text, idempotency_key)):
        return _error("workspace_id, question_id, answer and idempotency_key required", 400)
    if "expected_version" not in body:
        return _error("expected_version required", 400)
    expected_version = body.get("expected_version")
    if expected_version is not None and not isinstance(expected_version, int):
        return _error("expected_version must be an integer or null", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "knowledge.answer")
        result = store.hitl_record_answer(
            authority["tenant_id"], workspace_id, question_id,
            authority["account_id"], answer_text, expected_version,
            idempotency_key=idempotency_key)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except IdentityDenied as exc:
        return _error(str(exc), _identity_denied_status(exc))
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except HitlVersionConflict as exc:
        return JSONResponse(
            {"error": "stale_version", "current_version": exc.current_version},
            status_code=409, headers={"Cache-Control": "no-store"})
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)


def _feed_policy_changed(identity, authority, workspace_id) -> None:
    """Extension FS-R1: a membership change bumped the workspace policy version; mirror it so listener tokens issued before it stop working. Best effort, never fails the change."""
    try:
        from infra import v2_satellite_routes
        fresh = identity.current_authority(authority["account_id"], authority["tenant_id"], workspace_id)
        v2_satellite_routes.publish_policy(authority["tenant_id"], workspace_id, fresh.get("policy_version"))
    except Exception:                                                                       # noqa: BLE001 - the token TTL bounds the window when this fails
        pass


@router.post("/v2/membership/grant")
@db_route
def grant_membership(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity = _identity_store()
    if identity is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    account_id = body.get("account_id")
    roles = body.get("roles")
    if not all(isinstance(x, str) and x for x in (workspace_id, account_id)):
        return _error("workspace_id and account_id required", 400)
    if not isinstance(roles, list) or not roles or any(r not in GRANTABLE_ROLES for r in roles):
        return _error("roles must be a non-empty list of policy-allowed roles", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "membership.manage")
        identity.grant_membership(authority["tenant_id"], authority["project_id"],
                                  workspace_id, account_id, tuple(roles))
        _feed_policy_changed(identity, authority, workspace_id)
        return JSONResponse({"account_id": account_id, "roles": sorted(set(roles))},
                            headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except ValueError as exc:
        return _error(str(exc), 404 if "unknown account" in str(exc) else 409)
    except TenantScopeError as exc:
        return _error(str(exc), 409)


@router.post("/v2/membership/revoke")
@db_route
def revoke_membership(request: Request):
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity = _identity_store()
    if identity is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    account_id = body.get("account_id")
    if not all(isinstance(x, str) and x for x in (workspace_id, account_id)):
        return _error("workspace_id and account_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "membership.manage")
        identity.revoke_membership(authority["tenant_id"], authority["project_id"],
                                   workspace_id, account_id)
        _feed_policy_changed(identity, authority, workspace_id)
        return JSONResponse({"account_id": account_id, "revoked": True},
                            headers={"Cache-Control": "no-store"})
    except MembershipNotFound:
        return _error("membership not found", 404)
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)


@router.post("/v2/intent/parse")
@db_route
def parse_intent(request: Request):
    """AI6-T01 `intent.parse`: consume the current accepted KM/source/
    decision tuple (pinned fresh via `store.head` inside `store.parse_intent`
    itself) + a grounded rule/alias `target_hint` (a real `RuleCandidate.
    candidate_id`) or a `structured_form` + the raw user request; produce a
    versioned Intent draft (typed target/value/scope/exclusions/effective
    time + clarification questions). Never mutates source/KM (task card)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    raw_request = body.get("raw_request")
    structured_form = body.get("structured_form")
    target_hint = body.get("target_hint")
    idempotency_key = body.get("idempotency_key")
    if not isinstance(workspace_id, str) or not workspace_id:
        return _error("workspace_id required", 400)
    if not raw_request and not structured_form:
        return _error("raw_request or structured_form required", 400)
    if structured_form is not None and not isinstance(structured_form, dict):
        return _error("structured_form must be an object", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "intent.create")
        key = idempotency_key if isinstance(idempotency_key, str) and idempotency_key else (
            f"parse-{authority['account_id']}-"
            f"{canonical_hash({'raw_request': raw_request, 'structured_form': structured_form, 'target_hint': target_hint})}")
        result = store.parse_intent(
            authority["tenant_id"], workspace_id, actor_id=authority["account_id"],
            raw_request=raw_request, structured_form=structured_form,
            target_hint=target_hint, idempotency_key=key)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)


@router.post("/v2/intent/confirm")
@db_route
def confirm_intent(request: Request):
    """AI6-T01 `intent.confirm`: rechecks current authority/versions (the
    workspace's CURRENT decision_epoch/knowledge_revision_id/
    source_revision_id, independently -- L-007) at confirm time, never just
    trusting the parse-time snapshot; stores the exact digest + audit
    idempotently. Gated by the SAME `knowledge.confirm` permission as
    `/v2/knowledge/confirm` (a human confirmer role), and mints its OWN
    `intent_id`-scoped decision -- never the `claim_id`/`conflict_id` space
    `/v2/knowledge/confirm`/`/v2/conflict/resolve` use (L-031)."""
    if not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    body = _json_body_sync(request)
    workspace_id = body.get("workspace_id")
    intent_id = body.get("intent_id")
    idempotency_key = body.get("idempotency_key")
    if not all(isinstance(x, str) and x for x in (workspace_id, intent_id)):
        return _error("workspace_id and intent_id required", 400)
    key = idempotency_key if isinstance(idempotency_key, str) and idempotency_key else (
        f"confirm-{intent_id}")
    expected_decision_epoch = body.get("expected_decision_epoch")
    if expected_decision_epoch is not None and not isinstance(expected_decision_epoch, int):
        return _error("expected_decision_epoch must be an integer", 400)
    expected_knowledge_revision_id = body.get("expected_knowledge_revision_id")
    expected_source_revision_id = body.get("expected_source_revision_id")
    try:
        authority = _authority(identity, request, workspace_id)
        require_human_actor(authority)
        # The person who made the request confirms THEIR OWN request (intent.confirm_own, owner only, server-checked); anyone else
        # needs the knowledge-confirmer permission as before. Confirming an intent records a decision; it changes no document.
        if "knowledge.confirm" not in permissions_for(authority.get("roles")):
            require(authority, "intent.confirm_own")
            if store.get_intent(authority["tenant_id"], workspace_id, intent_id).get("created_by") != authority["account_id"]:
                raise PermissionDenied("intent.confirm_own: only the person who made this request can confirm it")
        result = store.confirm_intent(
            authority["tenant_id"], workspace_id, intent_id,
            actor_id=authority["account_id"], idempotency_key=key,
            expected_decision_epoch=expected_decision_epoch,
            expected_knowledge_revision_id=expected_knowledge_revision_id,
            expected_source_revision_id=expected_source_revision_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except IntentNotFound:
        return _error("intent not found", 404)
    except IntentStale as exc:
        return JSONResponse(
            {"error": "intent_stale", "stale_fields": list(exc.stale_fields)},
            status_code=409, headers={"Cache-Control": "no-store"})
    except (TenantScopeError, StoreConflict) as exc:
        return _error(str(exc), 409)
    except ValueError as exc:
        return _error(str(exc), 400)


@router.get("/v2/intent/{intent_id}")
def get_intent(request: Request, intent_id: str, workspace_id: str):
    """Stored-intent read (task card's final "stored intent read" step)."""
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        result = store.get_intent(authority["tenant_id"], workspace_id, intent_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except (IntentNotFound, TenantScopeError):
        return _error("intent not found", 404)


def _readable_audit(store, tenant: str, workspace_id: str, events: list[dict], me: str, perms) -> list[dict]:
    """SCN-12: the workspace audit keeps each request family's read rule. An event bound to a business change (`chg-…` as its job, intent or change id, or a job whose
    payload names that change as `change_id` or `intent_id`) is shown only to the person who asked, or to holders of `knowledge.confirm` / `change.approve` (`v2_business_routes._change_record_for`);
    an event of a private request-assistant job (`request.qa`) only to the person who asked it. A binding that cannot be resolved is withheld (fail closed)."""
    sees_changes = bool({"knowledge.confirm", "change.approve"} & set(perms))
    jids = sorted({str((e.get("payload") or {}).get("job_id")) for e in events if (e.get("payload") or {}).get("job_id")})
    jobs: dict[str, tuple] = {}
    if jids:
        with store._tx() as cur:
            cur.execute("SELECT job_id, kind, requested_by, COALESCE(payload->>'change_id', payload->>'intent_id') FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)",
                        (tenant, workspace_id, jids))
            jobs = {r[0]: r[1:] for r in cur.fetchall()}

    def change_of(payload: dict) -> str | None:
        for key in ("change_id", "intent_id", "job_id"):
            v = payload.get(key)
            if isinstance(v, str) and v.startswith("chg-"):
                return v
        job = jobs.get(str(payload.get("job_id")))
        return job[2] if job and isinstance(job[2], str) and job[2].startswith("chg-") else None

    bound = {e["id"] if e.get("id") is not None else idx: change_of(e.get("payload") or {}) for idx, e in enumerate(events)}
    askers: dict[str, str | None] = {}
    wanted = sorted({c for c in bound.values() if c})
    if wanted and not sees_changes:
        with store._tx() as cur:
            cur.execute("SELECT job_id, requested_by FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='change.understand' AND job_id = ANY(%s)",
                        (tenant, workspace_id, wanted))
            askers = {r[0]: r[1] for r in cur.fetchall()}
    out = []
    for idx, e in enumerate(events):
        payload = e.get("payload") or {}
        change = bound[e["id"] if e.get("id") is not None else idx]
        if change and not sees_changes and askers.get(change) != me:
            continue
        job = jobs.get(str(payload.get("job_id")))
        if (job and job[0] == "request.qa" or payload.get("kind") == "request.qa") and (not job or job[1] != me):
            continue
        out.append(e)
    return out


@router.get("/v2/audit/{workspace_id}")
def workspace_audit(request: Request, workspace_id: str):
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    try:
        authority = _authority(identity, request, workspace_id)
        require(authority, "project.read")
        # Every event of the workspace, membership grants / revocations included, comes from the one `v2_audit` table with its id and recorded time. The identity
        # store's `membership.*` list reads the same rows; merging it listed each membership event twice, the copy without id or time (PC-06, 2026-10-26).
        combined = _readable_audit(store, authority["tenant_id"], workspace_id, store.audit_events(authority["tenant_id"], workspace_id),
                                   authority["account_id"], permissions_for(authority.get("roles")))
        # Defense in depth: never surface raw secrets even if a future event
        # type accidentally carried one.
        for event in combined:
            event.get("payload", {}).pop("credential", None)
            event.get("payload", {}).pop("credential_digest", None)
        return JSONResponse({"events": combined}, headers={"Cache-Control": "no-store"})
    except IdentityDenied:
        return _error("sign in required", 401)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
