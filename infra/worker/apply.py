"""Executor for job kind ``apply`` (executor id ``apply.materialize.v1``): applies an APPROVED change, or an approved RESTORE, to the managed sources through one guarded publish.

What it does, in order
  1. Resolves the approval bundle named by ``payload.approval_job_id`` / ``expected_approval_digest`` (payload shape is validated at the route, ``infra/v2_policy_routes.py::create_job``, and again
     here in ``prepare()`` as defence in depth). A restore uses ``payload.restore_approval_id`` instead; the two are mutually exclusive.
  2. Requires the approval to be READY (``approval_readiness_blocked`` otherwise), requires a recorded human ``approve`` decision for this exact approval and digest in the decision ledger
     (``approval_not_decided`` otherwise), and recomputes ``approval_digest`` from the CURRENT proposal row, review row and the approval's recorded snapshot/policy/required action
     (``approval.approval_digest_inputs``, the definition the approval executor used). It must equal the digest in the payload, in the approval result and in the ledger row, before any replay:
     a row edited after approval fails closed (``approved_rows_digest_mismatch`` / ``approval_row_digest_mismatch``) with nothing staged or published.
  3. Re-reads the CURRENT published bytes of the target artifact (``ctx.store.head`` + ``read_artifact_blob`` at this execution, never a value cached at proposal, review or approval time) and replays the
     proposal's RECORDED typed edits (locators, ``expected_current``, ``proposed``, taken verbatim from the proposal result) with the same ``_yaml_typed_edits`` / ``_xlsx_typed_edits`` functions the
     proposal used. A source that drifted after approval raises the same ``malformed_patch`` / "stale expected_current" error those functions raise; nothing is guessed or fuzzy-applied.
  4. Stages the resulting bytes (immutable, content addressed) and publishes them once through ``ctx.store.publish_source``. A true no-op stages nothing and publishes nothing; its staged manifest points at
     the unchanged current blob hash.

Supported edit kinds and limits
  - Only ``yaml_typed_edit`` and ``xlsx_typed_edit`` are replayable. Any other kind (``markdown`` and ``python`` proposals are classified as manual work, there is no ``delete`` or ``rename`` producer upstream) is
    refused with the named, non-retryable ``unsupported_edit_kind``; it is a defensive default, not a silent no-op.
  - One apply job covers exactly one target artifact: the proposal and approval executors bind one proposal to one ``target_artifact_id``. ``staged_manifest`` is keyed by artifact id so a future
    multi-artifact bundle can add keys, but today it always holds one. Atomic multi-artifact apply needs an upstream bundling contract first; this is a stated gap.
  - Any materialization failure (stale, corrupt, renamed sheet, malformed patch) raises ``NonRetryableExecutionError`` before ``staged_manifest`` is set: the job fails with no partial manifest.
  - ``external = False``: the executor never calls a model provider. It is a deterministic replay of an already approved, already recorded edit set.

Publish gate (one transaction)
  - ``publish_source`` is called exactly once with an ``authority_check`` closure that re-runs the deciding human approver's CURRENT authority (account not disabled or expired, membership still holding
    ``source.import``) under ``FOR UPDATE`` locks INSIDE the publish transaction. Blob stage, CAS on the manifest and head, audit rows and the outbox event commit or roll back together. A revoked approver
    surfaces as the non-retryable ``approver_authority_revoked`` and nothing is written.
  - The CAS uses ``expected_version`` of the head read in step 3. A concurrent apply to the same artifact is normally caught earlier by the replay check; the CAS is the store-level backstop.
  - Idempotency: resubmitting the same ``job_id`` returns the first run's recorded result (the job table's create idempotency); ``publish_source`` adds its own ledger (``operation="apply.publish"``,
    ``idempotency_key=f"job-apply-publish-{job_id}"``), so even a direct second call cannot advance the head twice.

Restore
  The same executor applies a RESTORE approval: a whole-manifest forward publication (parent = current head, ``restored_from`` = target, ``trigger="restore"``, artifacts absent from the target manifest
  removed). It goes through the same ``_publish_through_gate`` with the same guards: digest binding, stale-tuple check on the row-locked head (``_assert_snapshot_current``), the approver's current
  authority under lock (permission chosen by approval subject, ``APPROVAL_SUBJECT_PERMISSION``), outbox fencing, CAS and the ``apply.publish`` ledger. There is no second restore guard path.

History: earlier revisions of this docstring recorded design deliberations between work rounds (a readiness gate that could not be satisfied before the verification contract existed, and its replacement);
they are in the version control history. The behaviour above is the current one.
"""

from __future__ import annotations

from infra import v2_lineage
from infra.adapters.v2_policy import APPROVAL_SUBJECT_PERMISSION
from infra.adapters.v2_postgres import (
    BlobIntegrityError, LeaseLost, StaleAuthorityError, StaleVersion,
    TenantScopeError,
)
from infra.adapters.v2_restore import load_restore_approval
from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from core.change_intelligence.v2.serialize import canonical_hash
from infra.worker.approval import approval_digest_inputs
from infra.worker.proposal import _xlsx_typed_edits, _yaml_typed_edits

_REQUIRED_PAYLOAD_FIELDS = ("approval_job_id", "expected_approval_digest")

_SUPPORTED_EDIT_KINDS = ("yaml_typed_edit", "xlsx_typed_edit")


def _resolve_job(ctx: ExecutionContext, job_id, *, expected_kind: str, label: str):
    """Same real, tenant/workspace-scoped resolution primitive
    `infra/worker/approval.py::_resolve_job` already established
    (a reused pattern, not a second one). A missing/wrong-kind/
    not-completed reference is a hard, non-retryable failure -- there is
    nothing real to apply."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    try:
        job = ctx.store.job_record(tenant_id, workspace_id, job_id)
    except Exception as exc:
        raise NonRetryableExecutionError(
            f"{label}_job_not_found", str(job_id)) from exc
    if job.get("kind") != expected_kind:
        raise NonRetryableExecutionError(
            f"{label}_job_wrong_kind", f"kind={job.get('kind')!r}")
    if job.get("execution_status") != "COMPLETED":
        raise NonRetryableExecutionError(
            f"{label}_job_not_completed", f"status={job.get('execution_status')!r}")
    return job


def _current_head(ctx, tenant_id, workspace_id):
    """The workspace vanishing between claim and execute is a
    terminal, named failure (nothing to apply against), never an uncaught
    `TenantScopeError`. Transient store errors (connection etc.) are NOT
    translated: they escape as before and stay retryable via redelivery."""
    try:
        return ctx.store.head(tenant_id, workspace_id)
    except TenantScopeError as exc:
        raise NonRetryableExecutionError("workspace_not_found", str(exc)) from exc


def _read_artifact(ctx, tenant_id, workspace_id, revision_id, artifact_id, *,
                   reason="target_artifact_missing"):
    """`read_artifact_blob` with its deterministic absence /
    corruption failures translated to named terminal failures. The approved
    target may no longer be in the current head (a later import/publish that
    omits it) -> `target_artifact_missing` (fail closed, nothing written).
    A corrupt/missing stored blob -> `target_blob_integrity`."""
    try:
        return ctx.store.read_artifact_blob(
            tenant_id, workspace_id, revision_id, artifact_id)
    except TenantScopeError as exc:
        raise NonRetryableExecutionError(
            reason, f"artifact_id={artifact_id!r} revision_id={revision_id!r}: {exc}") from exc
    except BlobIntegrityError as exc:
        raise NonRetryableExecutionError(
            "target_blob_integrity",
            f"artifact_id={artifact_id!r} revision_id={revision_id!r}: {exc}") from exc


def _approval_snapshot(approval_result):
    """The approval's recorded (source_revision_id, decision_epoch)
    tuple -- the exact state the digest binds and `decide_job` re-checked when
    the human approved. Fail closed if absent/malformed, or if the approval
    was itself computed over an already-stale proposal (`freshness` flags)."""
    snap = approval_result.get("snapshot")
    if (not isinstance(snap, dict) or not snap.get("source_revision_id")
            or type(snap.get("decision_epoch")) is not int):
        raise NonRetryableExecutionError(
            "approval_snapshot_missing",
            "approval carries no usable source_revision_id/decision_epoch snapshot")
    freshness = approval_result.get("freshness") or {}
    if freshness.get("source_stale"):
        raise NonRetryableExecutionError(
            "approval_stale_source",
            "approval was computed over a proposal pinned to an older source revision")
    if freshness.get("epoch_stale"):
        raise NonRetryableExecutionError(
            "approval_stale_decision_epoch",
            "approval was computed over a proposal pinned to an older decision epoch")
    return snap["source_revision_id"], snap["decision_epoch"]


def _assert_snapshot_current(snapshot, head):
    """Source changed since review => stale proposal returns to analysis;
    never rebase and keep the approval. Compare the
    approval snapshot with a head (the pre-read head for early failure, and
    the row-LOCKED head inside the publish / manual-work transaction for the
    authoritative check). Raises a named, non-retryable failure."""
    source_revision_id, decision_epoch = snapshot
    if head.source_revision_id != source_revision_id:
        raise NonRetryableExecutionError(
            "approval_stale_source",
            f"approved source_revision_id={source_revision_id!r}, current "
            f"{head.source_revision_id!r}; proposal must return to analysis")
    if head.decision_epoch != decision_epoch:
        raise NonRetryableExecutionError(
            "approval_stale_decision_epoch",
            f"approved decision_epoch={decision_epoch!r}, current "
            f"{head.decision_epoch!r}; proposal must return to analysis")


def _pending_and_scope(ctx, approval_result, proposal_result, target_artifact_id,
                       before_hash, after_hash, published_revision_id):
    """Result of published artifacts + pending human work + exact
    verification scope. Both fields are derived from the REAL
    approved chain, never hardcoded:

      - `pending_human_work`: every `verification_plan` entry the proposal
        recorded with `action == "human_authoring_required"` (the proposal's
        own "manual work" terminology, `ProposalExecutor`), plus the
        approval's `required_action == "manual_work_required"` marker. Such
        work is only REPORTED here; it is never applied as a source edit.
      - `verification_scope`: the proposal's own `verification_plan`
        (verbatim), the review job's `checks` (name -> status) and
        `board_id`, and the exact target artifact + before/after blob hashes
        this apply published. Independent verification is a separate stage.
    """
    plan = [dict(e) for e in (proposal_result.get("verification_plan") or [])
            if isinstance(e, dict)]
    pending = [e for e in plan if e.get("action") == "human_authoring_required"]
    if approval_result.get("required_action") == "manual_work_required" and not pending:
        pending.append({"action": "human_authoring_required",
                        "reason": "approval_required_action_manual_work_required"})
    review_checks, board_id = {}, None
    review_job_id = approval_result.get("review_job_id")
    if review_job_id:
        review_job = _resolve_job(ctx, review_job_id, expected_kind="review",
                                  label="review")
        review_result = review_job.get("execution_result") or {}
        board_id = review_result.get("board_id")
        review_checks = {name: (entry or {}).get("status")
                         for name, entry in (review_result.get("checks") or {}).items()}
    scope = {
        "target_artifact_ids": [target_artifact_id],
        "before_blob_hash": before_hash,
        "after_blob_hash": after_hash,
        "published_revision_id": published_revision_id,
        "verification_plan": plan,
        "review_job_id": review_job_id,
        "review_board_id": board_id,
        "review_checks": review_checks,
    }
    return pending, scope


def _publish_through_gate(ctx, *, revision_id, manifest, head, publish_key, subject,
                          actor_id, snapshot, carry_forward, audit_context,
                          restored_from=None, trigger=None, proposal_job_id=None):
    """THE apply gate's one publication call site (reused unchanged
    by the RESTORE subject): one `publish_source` transaction with the
    approver-authority recheck (permission mapped per approval subject), the
    outbox fence, the stale-tuple check on the row-locked head, CAS on the
    pre-read head version and the `apply.publish` idempotency ledger. Store
    failures are translated to named terminal failures; the transaction has
    already rolled back when they surface."""
    tenant_id, workspace_id = ctx.job["tenant_id"], ctx.job["workspace_id"]
    permission = APPROVAL_SUBJECT_PERMISSION[subject]

    def _authority_check(cur, _store=ctx.store, _tenant=tenant_id,
                         _workspace=workspace_id, _actor=actor_id,
                         _permission=permission):
        # The SAME permission that recorded the human approval in the first
        # place (CHANGE -> change.approve, RESTORE -> source.restore),
        # rechecked at publish time, under the SAME transaction.
        _store._recheck_actor_authority(cur, _tenant, _workspace, _actor, _permission)

    def _head_check(locked_head, _snapshot=snapshot):
        # Authoritative stale-tuple check on the head locked INSIDE the
        # publish tx (a move after the pre-read is refused).
        _assert_snapshot_current(_snapshot, locked_head)

    _draft_guard = v2_lineage.proposal_guard(tenant_id, workspace_id, proposal_job_id) if proposal_job_id else None

    restore_kwargs = {}
    if restored_from is not None:
        restore_kwargs = {"restored_from": restored_from, "trigger": trigger}
    try:
        return ctx.store.publish_source(
            tenant_id, workspace_id, revision_id, manifest,
            expected_version=head.version,
            operation="apply.publish",
            idempotency_key=publish_key,
            authority_check=_authority_check,
            head_check=_head_check,
            # The draft behind a CHANGE must not have been revised: checked on the publish transaction's own cursor under the workspace-row lock (a
            # revision is a job insert under the same lock), so a revise either commits before this check (nothing is published) or after the
            # publish commits (the publish is history). Not a second check outside the transaction.
            locked_check=_draft_guard,
            # A revision is the WHOLE snapshot: a CHANGE keeps every other
            # artifact of the current head and replaces only the target; a
            # RESTORE passes the whole target manifest (nothing carried).
            carry_forward=carry_forward,
            # Fencing: a worker that lost its lease cannot commit.
            fence=ctx.lease,
            audit_context=audit_context,
            **restore_kwargs)
    except LeaseLost:
        raise  # job_worker: LEASE_LOST, no terminal write by the stale worker
    except StaleAuthorityError as exc:
        raise NonRetryableExecutionError(
            "approver_authority_revoked", str(exc)) from exc
    except StaleVersion as exc:
        raise NonRetryableExecutionError(
            "publish_stale_head", str(exc)) from exc
    except v2_lineage.DraftSuperseded as exc:
        raise NonRetryableExecutionError("draft_superseded", str(exc)) from exc
    except NonRetryableExecutionError:
        raise
    except Exception as exc:
        # Any other publish-transaction failure (injected crash, broken
        # audit/outbox write, a restore manifest that is not the target's,
        # etc.) must surface as a FAILED attempt, never a silent "staged
        # only" COMPLETED job -- the transaction itself has already rolled
        # back (`_tx()`'s own rollback-on-exception).
        raise NonRetryableExecutionError(
            "publish_transaction_failed",
            f"{type(exc).__name__}: {exc}") from exc


def _verify_bound_rows(ctx, approval_job_id, approval_result, proposal_job_id,
                       proposal_result, expected_digest):
    """Recompute the approval digest from the current
    stored rows and require it to equal the digest the approval bound and the
    human approved. Fail closed (named, non-retryable) on any mismatch or
    missing input; runs before any replay, stage or publish."""
    if approval_result.get("proposal_job_id") != proposal_job_id:
        raise NonRetryableExecutionError(
            "approval_row_digest_mismatch",
            f"approval {approval_job_id!r} proposal_job_id does not match")
    review_job_id = approval_result.get("review_job_id")
    if not isinstance(review_job_id, str) or not review_job_id:
        raise NonRetryableExecutionError(
            "approval_row_digest_mismatch", "approval carries no review_job_id")
    review_job = _resolve_job(ctx, review_job_id, expected_kind="review", label="review")
    review_result = review_job.get("execution_result") or {}
    snap = approval_result.get("snapshot")
    if (not isinstance(snap, dict) or not snap.get("source_revision_id")
            or type(snap.get("decision_epoch")) is not int):
        raise NonRetryableExecutionError(
            "approval_snapshot_missing",
            "approval carries no usable source_revision_id/decision_epoch snapshot")

    def _digest(p_result, r_result):
        return canonical_hash(approval_digest_inputs(
            proposal_job_id=proposal_job_id, proposal_result=p_result,
            review_job_id=review_job_id, review_result=r_result,
            source_revision_id=snap.get("source_revision_id"),
            decision_epoch=snap.get("decision_epoch"),
            policy_snapshot=approval_result.get("policy_snapshot"),
            required_action=approval_result.get("required_action")))

    if _digest(proposal_result, review_result) == expected_digest:
        return
    raise NonRetryableExecutionError(
        "approved_rows_digest_mismatch",
        f"recomputed approval digest over the stored proposal/review/approval "
        f"rows != approved digest {expected_digest!r}; a stored row changed "
        f"after approval")


def _assert_draft_current(ctx, proposal_job_id) -> None:
    """A superseded draft's proposal is history: never reviewed, bundled or applied from now on (checked again here because the job may have been queued or running when the draft was revised)."""
    newer = v2_lineage.newer_draft_for_proposal(ctx.store, ctx.job["tenant_id"], ctx.job["workspace_id"], proposal_job_id)
    if newer:
        raise NonRetryableExecutionError("draft_superseded", f"{v2_lineage.SUPERSEDED_MSG} (newer draft {newer})")


class ApplyExecutor:
    """See module docstring. Zero provider calls (`external = False`) -- a
    pure, deterministic replay of an already-approved edit set against
    freshly re-read source bytes."""

    external = False
    executor_id = "apply.materialize.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        required = _REQUIRED_PAYLOAD_FIELDS
        if payload.get("restore_approval_id") is not None:
            if payload.get("approval_job_id") is not None:
                raise NonRetryableExecutionError(
                    "apply_subject_ambiguous",
                    "payload names both approval_job_id and restore_approval_id")
            required = ("restore_approval_id", "expected_approval_digest")
        for key in required:
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise NonRetryableExecutionError(
                    f"{key}_required", f"payload.{key} must be a non-empty string")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        tenant_id, workspace_id = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]
        if payload.get("restore_approval_id") is not None:
            return self._execute_restore(ctx)
        approval_job_id = payload.get("approval_job_id")
        expected_approval_digest = payload.get("expected_approval_digest")

        approval_job = _resolve_job(
            ctx, approval_job_id, expected_kind="approval", label="approval")
        approval_result = approval_job.get("execution_result") or {}

        if not approval_result.get("approval_digest"):
            raise NonRetryableExecutionError(
                "approval_not_ready",
                f"approval_job_id={approval_job_id!r} carries no approval_digest")
        if approval_result.get("approval_digest") != expected_approval_digest:
            raise NonRetryableExecutionError(
                "approval_digest_mismatch",
                f"expected {expected_approval_digest!r}, actual "
                f"{approval_result.get('approval_digest')!r}")

        # W1 user rule "Review BLOCKED/UNAVAILABLE khong duoc apply" (defence in depth with the
        # decision route): a bundle whose computed readiness is not READY (blocked / unavailable
        # review check, upstream blocker, stale) is never applied, whatever approve row exists.
        # (a `manual_work_required` bundle publishes no bytes, only pending human work -- the one
        # BLOCKED shape that stays applicable; disclosed deviation, same exemption as the route.)
        if (approval_result.get("readiness") != "READY"
                and approval_result.get("required_action") != "manual_work_required"):
            raise NonRetryableExecutionError(
                "approval_readiness_blocked",
                f"approval_job_id={approval_job_id!r} readiness is "
                f"{approval_result.get('readiness')!r}, not 'READY'")

        # A `kind="approval"` job is
        # only a computed snapshot (`ApprovalExecutor`'s own docstring: "a
        # once-green readiness here is never treated as a durable grant") --
        # it is NOT itself a human decision. The real decision
        # ledger (`change_decisions`/`record_change_decision`, exposed via
        # `POST /v2/jobs/{job_id}/decision` and `GET .../decisions`) exists for
        # exactly this reason. Every one of proposal/review/approval/apply
        # shares the SAME `source.import` creation permission, so digest
        # match alone lets a single account self-drive the whole chain with
        # no independent human ever recording an approval. This executor
        # must therefore query the real ledger for a genuine, matching
        # `action == "approve"` row before staging anything.
        #
        # `record_change_decision` enforces at most one terminal decision
        # (`approve` or `reject`) per approval `job_id` ever (see its own
        # `StoreConflict` guard), so there is never more than one `approve`
        # row to disambiguate here -- any number of `request_changes` rows
        # may precede it (each spawning its own NEW proposal job id), but a `reject`/`request_changes`-only history,
        # or no recorded decision at all, must reject before staging.
        decisions = ctx.store.change_decisions(tenant_id, workspace_id, approval_job_id)
        approving_decision = next(
            (d for d in decisions if d.get("action") == "approve"
             and d.get("approval_digest") == expected_approval_digest), None)
        if approving_decision is None:
            raise NonRetryableExecutionError(
                "approval_not_decided",
                f"approval_job_id={approval_job_id!r} has no recorded human "
                f"'approve' decision matching digest {expected_approval_digest!r}")
        approving_actor_id = approving_decision.get("actor_id")

        proposal_job_id = approval_result.get("proposal_job_id")
        proposal_job = _resolve_job(
            ctx, proposal_job_id, expected_kind="proposal", label="proposal")
        proposal_result = proposal_job.get("execution_result") or {}

        _verify_bound_rows(ctx, approval_job_id, approval_result, proposal_job_id,
                           proposal_result, expected_approval_digest)

        target_artifact_id = proposal_result.get("target_artifact_id")
        edit_kind = proposal_result.get("edit_kind")
        recorded_edits = proposal_result.get("edits") or []
        if not isinstance(target_artifact_id, str) or not target_artifact_id:
            raise NonRetryableExecutionError(
                "proposal_missing_target_artifact_id", str(proposal_job_id))

        # Retry recovery (same apply key, earlier attempt/result): if an earlier execution of THIS job already committed its
        # publish (e.g. the worker died after the commit, before the job was
        # finalized), return that prior committed attempt. This must run
        # BEFORE the typed-edit replay: the source already holds the edited
        # bytes, so the replay would reject them as "stale expected_current".
        job_id = job.get("job_id")
        publish_key = f"job-apply-publish-{job_id}"
        prior = ctx.store.prior_publish(
            tenant_id, workspace_id, "apply.publish", publish_key)
        if prior is not None:
            published_hash = _read_artifact(
                ctx, tenant_id, workspace_id, prior["revision_id"],
                target_artifact_id, reason="prior_publish_unreadable")["blob_hash"]
            parent_id = prior.get("parent_revision_id")
            prior_before = (_read_artifact(
                ctx, tenant_id, workspace_id, parent_id, target_artifact_id,
                reason="prior_publish_unreadable")["blob_hash"]
                if parent_id else None)
            pending_work, scope = _pending_and_scope(
                ctx, approval_result, proposal_result, target_artifact_id,
                prior_before, published_hash, prior["revision_id"])
            return ExecutionOutcome(result={
                "executor_id": self.executor_id,
                "approval_job_id": approval_job_id,
                "proposal_job_id": proposal_job_id,
                "target_artifact_id": target_artifact_id,
                "edit_kind": edit_kind,
                "is_no_op": False,
                "before_blob_hash": prior_before,
                "staged_manifest": {target_artifact_id: published_hash},
                "published": True,
                "published_revision_id": prior["revision_id"],
                "recovered_prior_publish": True,
                "pending_human_work": pending_work,
                "verification_scope": scope,
            }, model_id=None)

        # Stale-tuple gate. Everything from here on is a NEW
        # publish/report under this approval, so the approval snapshot must
        # still equal the current head. (`prior_publish` recovery above is
        # deliberately exempt: the job's own committed publish moved the head.)
        # This pre-read check fails early; the authoritative check repeats
        # against the row-locked head inside the manual-work / publish tx.
        _assert_draft_current(ctx, proposal_job_id)                      # a NEW publish never comes from a superseded draft (a recovered earlier publish above is history and exempt)
        snapshot = _approval_snapshot(approval_result)
        head = _current_head(ctx, tenant_id, workspace_id)
        _assert_snapshot_current(snapshot, head)

        # External manual actions are NOT faked as source edits; the result
        # reports pending human work. A human-approved
        # manual-work bundle -- proposal classification `manual` (python/
        # markdown edit_kind, ZERO recorded edits: the only shape
        # `ProposalExecutor` produces for it) AND approval `required_action ==
        # "manual_work_required"` -- has no typed edit to replay. It completes
        # through the same recovered/no-op result shape: no stage, no publish,
        # `published` False, pending work + scope derived from the real rows.
        # Every gate above (digest, recorded human approve decision) already
        # ran. Anything else with these edit kinds (e.g. a non-manual python/
        # markdown proposal, or manual with recorded edits) still falls to
        # `unsupported_edit_kind` below.
        if (edit_kind in ("python", "markdown")
                and proposal_result.get("classification") == "manual"
                and approval_result.get("required_action") == "manual_work_required"
                and not recorded_edits):
            # No publish transaction to hold the authority recheck, so run it
            # here (same permission, same locks): a revoked approver must not
            # get a COMPLETED apply either.
            try:
                with ctx.store._tx() as cur:
                    ctx.store._recheck_actor_authority(
                        cur, tenant_id, workspace_id, approving_actor_id,
                        APPROVAL_SUBJECT_PERMISSION["CHANGE"])
                    # A stale approval must not report pending human work
                    # as valid; checked on the locked head (same lock order as
                    # publish_source: authority, then head).
                    locked_head = ctx.store._lock_head(cur, tenant_id, workspace_id)
                    _assert_snapshot_current(snapshot, locked_head)
                    v2_lineage.proposal_guard(tenant_id, workspace_id, proposal_job_id)(cur)
            except v2_lineage.DraftSuperseded as exc:
                raise NonRetryableExecutionError("draft_superseded", str(exc)) from exc
            except StaleAuthorityError as exc:
                raise NonRetryableExecutionError(
                    "approver_authority_revoked", str(exc)) from exc
            manual_hash = _read_artifact(
                ctx, tenant_id, workspace_id, locked_head.source_revision_id,
                target_artifact_id).get("blob_hash")
            pending_work, scope = _pending_and_scope(
                ctx, approval_result, proposal_result, target_artifact_id,
                manual_hash, manual_hash, None)
            return ExecutionOutcome(result={
                "executor_id": self.executor_id,
                "approval_job_id": approval_job_id,
                "proposal_job_id": proposal_job_id,
                "target_artifact_id": target_artifact_id,
                "edit_kind": edit_kind,
                "is_no_op": True,
                "manual_work_only": True,
                "before_blob_hash": manual_hash,
                "staged_manifest": {target_artifact_id: manual_hash},
                "published": False,
                "published_revision_id": None,
                "pending_human_work": pending_work,
                "verification_scope": scope,
            }, model_id=None)

        # Fresh, never staled: the CURRENT head and CURRENT bytes, read at
        # THIS execution -- never the proposal's own pinned/staged values.
        artifact = _read_artifact(
            ctx, tenant_id, workspace_id, head.source_revision_id, target_artifact_id)
        before_bytes = artifact["content"]
        before_hash = artifact.get("blob_hash")

        if edit_kind == "yaml_typed_edit":
            try:
                text = before_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raise NonRetryableExecutionError(
                    "malformed_patch", "current target is not valid UTF-8 text")
            edits_in = [{"path": e.get("path"), "expected_current": e.get("before"),
                        "proposed": e.get("after")} for e in recorded_edits]
            _applied, is_no_op, new_text = _yaml_typed_edits(text, edits_in)
            after_bytes = None if is_no_op else new_text.encode("utf-8")
        elif edit_kind == "xlsx_typed_edit":
            edits_in = [{"sheet": e.get("sheet"), "cell": e.get("cell"),
                        "expected_current": e.get("before"),
                        "proposed": e.get("after")} for e in recorded_edits]
            _applied, is_no_op, new_bytes = _xlsx_typed_edits(before_bytes, edits_in)
            after_bytes = new_bytes
        else:
            raise NonRetryableExecutionError(
                "unsupported_edit_kind", f"edit_kind={edit_kind!r}")

        if is_no_op:
            staged_hash = before_hash
        else:
            try:
                staged_hash = ctx.store.stage_blob(after_bytes)
                # Independent re-open of the exact bytes just staged -- a
                # SECOND, distinct parse pass from the one that produced them
                # (independent validation of the staged snapshot).
                staged_bytes = ctx.store._verified_blob(staged_hash)
            except BlobIntegrityError as exc:
                raise NonRetryableExecutionError(
                    "staged_blob_unverifiable", str(exc)) from exc
            try:
                if edit_kind == "yaml_typed_edit":
                    import yaml
                    yaml.safe_load(staged_bytes.decode("utf-8"))
                elif edit_kind == "xlsx_typed_edit":
                    import io as _io
                    import openpyxl
                    openpyxl.load_workbook(_io.BytesIO(staged_bytes))
            except Exception as exc:  # deterministic bad content, never publish
                raise NonRetryableExecutionError(
                    "staged_snapshot_invalid",
                    f"{type(exc).__name__}: {exc}") from exc

        published_revision_id = None
        if not is_no_op:
            # The ONE gate also publishes, in the SAME
            # transaction as the CAS head-advance and the audit/outbox
            # write (`publish_source`'s own `_record_commit`, invoked
            # inside its own `with self._tx()` block) -- never a second,
            # separately-transacted call. `authority_check` reruns the deciding human
            # approver's CURRENT authority under `FOR UPDATE`, inside that
            # SAME transaction, so a revoke recorded after the decision but
            # before this apply runs rolls back the whole write, not just
            # a job-status field.
            # Deterministic per job, so a retry's payload hash matches the
            # `apply.publish` ledger row (idempotent replay, never a second
            # revision).
            published_revision_id = f"rev-apply-{job_id}"
            _publish_through_gate(
                ctx, revision_id=published_revision_id,
                manifest={target_artifact_id: staged_hash}, head=head,
                publish_key=publish_key, subject="CHANGE", actor_id=approving_actor_id,
                snapshot=snapshot, carry_forward=True, proposal_job_id=proposal_job_id,
                audit_context={"job_id": job_id, "kind": "apply",
                               "approval_job_id": approval_job_id,
                               "approver_actor_id": approving_actor_id,
                               "target_artifact_id": target_artifact_id})

        pending_work, scope = _pending_and_scope(
            ctx, approval_result, proposal_result, target_artifact_id,
            before_hash, staged_hash, published_revision_id)
        result = {
            "executor_id": self.executor_id,
            "approval_job_id": approval_job_id,
            "proposal_job_id": proposal_job_id,
            "target_artifact_id": target_artifact_id,
            "edit_kind": edit_kind,
            "is_no_op": is_no_op,
            "before_blob_hash": before_hash,
            "staged_manifest": {target_artifact_id: staged_hash},
            "published": not is_no_op,
            "published_revision_id": published_revision_id,
            "pending_human_work": pending_work,
            "verification_scope": scope,
        }
        return ExecutionOutcome(result=result, model_id=None)

    def _execute_restore(self, ctx: ExecutionContext) -> ExecutionOutcome:
        """Apply a RESTORE approval through the same gate. See the
        module docstring; every guard below is the shared one (no parallel
        restore guard path)."""
        job = ctx.job
        tenant_id, workspace_id = job["tenant_id"], job["workspace_id"]
        payload = job["payload"]
        restore_approval_id = payload.get("restore_approval_id")
        approval = load_restore_approval(ctx.store, tenant_id, workspace_id,
                                         restore_approval_id)
        if approval is None:
            raise NonRetryableExecutionError(
                "restore_approval_not_found", f"restore_approval_id={restore_approval_id!r}")
        if approval["approval_digest"] != payload.get("expected_approval_digest"):
            raise NonRetryableExecutionError(
                "approval_digest_mismatch",
                f"expected {payload.get('expected_approval_digest')!r}, actual "
                f"{approval['approval_digest']!r}")
        target_id = approval["target_revision_id"]
        approving_actor_id = approval["actor_id"]
        job_id = job.get("job_id")
        publish_key = f"job-apply-publish-{job_id}"

        def _result(published_revision_id, manifest, touched, **extra):
            return {
                "executor_id": self.executor_id, "subject": "RESTORE",
                "restore_approval_id": restore_approval_id, "restored_from": target_id,
                "target_revision_id": target_id, "is_no_op": False,
                "before_blob_hash": None, "staged_manifest": manifest, "published": True,
                "published_revision_id": published_revision_id,
                "pending_human_work": [],
                "verification_scope": {
                    "target_artifact_ids": touched,
                    "published_revision_id": published_revision_id,
                    "restored_from": target_id, "verification_plan": [],
                    "review_job_id": None, "review_board_id": None, "review_checks": {}},
                **extra}

        # Retry recovery (as the CHANGE flow): an earlier execution of THIS job
        # already committed -> return it, before any staleness check (the
        # job's own publish moved the head).
        prior = ctx.store.prior_publish(tenant_id, workspace_id, "apply.publish", publish_key)
        if prior is not None:
            prior_view = ctx.store.source_revision_view(
                tenant_id, workspace_id, prior["revision_id"])
            manifest = {a["artifact_id"]: a["blob_hash"] for a in prior_view["artifacts"]}
            parent_id = prior.get("parent_revision_id")
            parent_map = ({a["artifact_id"]: a["blob_hash"]
                           for a in ctx.store.source_revision_view(
                               tenant_id, workspace_id, parent_id)["artifacts"]}
                          if parent_id else {})
            touched = sorted(a for a in set(parent_map) | set(manifest)
                             if parent_map.get(a) != manifest.get(a))
            return ExecutionOutcome(result=_result(
                prior["revision_id"], manifest, touched, recovered_prior_publish=True),
                model_id=None)

        snap = approval.get("snapshot")
        if (not isinstance(snap, dict) or not snap.get("source_revision_id")
                or type(snap.get("decision_epoch")) is not int):
            raise NonRetryableExecutionError(
                "approval_snapshot_missing",
                "restore approval carries no usable source_revision_id/decision_epoch snapshot")
        snapshot = (snap["source_revision_id"], snap["decision_epoch"])
        head = _current_head(ctx, tenant_id, workspace_id)
        _assert_snapshot_current(snapshot, head)

        try:
            target_view = ctx.store.source_revision_view(tenant_id, workspace_id, target_id)
            head_view = ctx.store.source_revision_view(
                tenant_id, workspace_id, head.source_revision_id)
        except TenantScopeError as exc:
            raise NonRetryableExecutionError("restore_target_missing", str(exc)) from exc
        manifest = {a["artifact_id"]: a["blob_hash"] for a in target_view["artifacts"]}
        if not manifest:
            raise NonRetryableExecutionError("restore_target_empty", str(target_id))
        for artifact_id in manifest:  # every artifact must be restorable, before any write
            _read_artifact(ctx, tenant_id, workspace_id, target_id, artifact_id,
                           reason="restore_target_missing")
        head_map = {a["artifact_id"]: a["blob_hash"] for a in head_view["artifacts"]}
        touched = sorted(a for a in set(head_map) | set(manifest)
                         if head_map.get(a) != manifest.get(a))
        if not touched:
            raise NonRetryableExecutionError(
                "restore_noop_identical_content",
                "the current head already holds exactly the target content")

        published_revision_id = f"rev-apply-{job_id}"
        _publish_through_gate(
            ctx, revision_id=published_revision_id, manifest=manifest, head=head,
            publish_key=publish_key, subject="RESTORE", actor_id=approving_actor_id,
            snapshot=snapshot, carry_forward=False, restored_from=target_id,
            trigger="restore",
            audit_context={"job_id": job_id, "kind": "apply", "subject": "RESTORE",
                           "restore_approval_id": restore_approval_id,
                           "approver_actor_id": approving_actor_id,
                           "restored_from": target_id})
        return ExecutionOutcome(result=_result(published_revision_id, manifest, touched),
                                model_id=None)


__all__ = ["ApplyExecutor"]
