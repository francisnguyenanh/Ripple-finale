"""Fixtures for the atomic managed-source publish gate.

The publish step consumes the staged bytes and checks of the apply stage, the
approval, and the current authority/source/KM/epoch.

Upstream interface:

  - The apply stage is `infra/worker/apply.py::ApplyExecutor`
    (`kind="apply"`, `executor_id="apply.materialize.v1"`). Its
    `execution_result` carries
    `staged_manifest` (dict `{target_artifact_id: staged_blob_hash}`),
    `target_artifact_id`, `proposal_job_id`, `approval_job_id`, `edit_kind`,
    `is_no_op`, `before_blob_hash` (the `execute()` return value of
    `infra/worker/apply.py`). The module docstring of that executor is
    explicit: "Stage, never publish" -- `store.publish_source`/
    any `store.head`-advancing call is NEVER made by `ApplyExecutor`. The
    publish step is added on top of it.

  - There is no code path that calls `store.publish_source` (or advances
    `store.head`/`source_revision_id`) as a result of a COMPLETED
    `kind="apply"` job other than the gate under test. The only two other
    atomic-publish transaction
    primitives in the store are `infra.adapters.v2_postgres.PostgresV2Store
    .publish_source` (CAS: one SQL transaction, locks
    `v2_workspaces` head row, checks `expected_version`, writes
    `v2_source_revisions`/`v2_blob_refs`/`v2_artifact_revisions`, advances
    `version`/`source_revision_id`, records an idempotency/audit row via
    `_record_commit`, accepts an optional `before_commit` callback invoked
    INSIDE the same transaction before commit -- a real, already-reviewed
    extension point for also writing
    ApplyAttempt/audit/outbox rows atomically) and `.recheck_and_publish_job`
    (a DIFFERENT, job-status-only publish gate for the
    legacy `v2_jobs.status: APPROVED -> PUBLISHED` flow, re-checking approver
    authority with `FOR UPDATE` locks inside one transaction; NOT the source-
    manifest publish path). Calling two methods with separate transactions
    is not an atomic unit: simply calling `publish_source` and then a SEPARATE
    `recheck_and_publish_job`-style authority recheck in two different
    `with self._tx()` blocks would NOT satisfy the atomicity requirement;
    the authority/readiness/fencing check and the
    publish write must happen in ONE transaction (via `publish_source`'s
    own `before_commit` hook, or a combined store method).

  - Entry point: the REAL `POST /v2/jobs
    {kind:"apply"}` -> `claim_and_admit_one` -> `ApplyExecutor.execute` ->
    `GET /v2/jobs/{id}` gate is "the one gate" (the existing apply gate
    extended with publish). Tests below submit a REAL apply job
    through this REAL route (never a mock), first record a REAL human `approve`
    decision via the change-decision route (a mandatory precondition of the
    apply stage), and assert the OBSERVABLE outcome the card requires:
    a COMPLETED apply job must leave `store.head(...)` advanced to a NEW
    `source_revision_id`/`version` whose `read_artifact_blob` for
    `target_artifact_id` returns the staged bytes -- i.e. staging AND
    publish happen as one gate, not two disconnected steps a caller must
    remember to chain. A guessed field name in the eventual
    `execution_result` (e.g. a `published_revision_id` key) is NOT asserted
    here; only the durable, store-level publish outcome is.

Reuses `tests.ai8t01.conftest` verbatim (one shared
fixture-resolution path): `build_real_ready_yaml_approval`,
`build_real_ready_xlsx_approval`, `record_real_approve_decision`,
`submit_apply_job`, `claim_apply_job`, `submit_claim_and_get_apply`,
`_grant_change_approver`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters.v2_identity import PostgresIdentityStore
from infra.worker import job_worker

from tests.ai4t01.conftest import DSN, drain_outbox, login, publish_with_chunk, setup  # noqa: F401
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai7t01.conftest import (  # noqa: F401
    claim_proposal_job,
    create_confirmed_python_intent,
    create_real_impact_job,
    create_real_simulation_job,
    get_job_status,
    submit_claim_and_get,
    submit_job,
)
from tests.ai7t02.conftest import (  # noqa: F401
    create_real_clean_yaml_proposal_job,
    create_real_yaml_proposal_job,
    submit_claim_and_get_review,
)
from tests.ai7t03.conftest import (  # noqa: F401
    APPROVAL_KIND,
    build_real_ready_review_bundle,
    claim_approval_job,
    submit_approval_job,
    submit_claim_and_get_approval,
)
from tests.ai7t04.conftest import submit_decision  # noqa: F401
from tests.ai8t01.conftest import (  # noqa: F401
    APPLY_KIND,
    _grant_change_approver,
    _publish_bytes,
    build_real_ready_xlsx_approval,
    build_real_ready_yaml_approval,
    claim_apply_job,
    make_xlsx_bytes,
    record_real_approve_decision,
    submit_apply_job,
    submit_claim_and_get_apply,
)


def revoke_approver(setup, workspace, approver):
    """Revoke the distinct change_approver's membership (same primitive
    `tests/ai2t03/test_revoke_before_publish.py` already exercises against
    the legacy publish gate) -- used here to test the "revoked
    approver never publishes" requirement against the apply/publish
    gate."""
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.revoke_membership(setup["tenant"], setup["project"], workspace,
                                 approver["account"])


def build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner, *,
                                 target_artifact_id=None, expected_current=14,
                                 proposed=30, record_approval=True):
    """Full real pipeline through a RECORDED human approve decision --
    the exact precondition `infra/worker/apply.py`'s own
    `approval_not_decided` guard requires before it will even stage
    anything. Returns a dict with everything a publish-gate test needs:
    `approval_job_id`, `digest`, `approver`, `target_artifact_id`."""
    target_artifact_id = target_artifact_id or f"config-apply-t02-{uuid4().hex[:8]}.yaml"
    approval_job_id, approval_body, proposal_job_id, review_job_id = (
        build_real_ready_yaml_approval(
            setup, client, workspace, monkeypatch, owner,
            target_artifact_id=target_artifact_id,
            expected_current=expected_current, proposed=proposed))
    digest = approval_body["execution_result"]["approval_digest"]
    approver = (record_real_approve_decision(
        setup, workspace, approval_job_id=approval_job_id, digest=digest)
        if record_approval else None)
    return {
        "approval_job_id": approval_job_id, "digest": digest,
        "approver": approver, "target_artifact_id": target_artifact_id,
        "proposal_job_id": proposal_job_id, "review_job_id": review_job_id,
        "expected_current": expected_current, "proposed": proposed,
    }


# ---------------------------------------------------------------------------
# Verification helpers.
# ---------------------------------------------------------------------------

def publish_footprint(store, tenant, workspace):
    """Durable, SQL-level footprint of every publish-side write. Verification
    reads the DB (not the job row) so a test can prove *no partial state*:
    revision rows, the `apply.publish` idempotency ledger, and the
    audit + outbox rows `_record_commit` writes in the same transaction."""
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_source_revisions "
                    "WHERE tenant_id=%s AND workspace_id=%s", (tenant, workspace))
        revisions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_idempotency WHERE tenant_id=%s "
                    "AND workspace_id=%s AND operation='apply.publish'",
                    (tenant, workspace))
        apply_publish_keys = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s "
                    "AND workspace_id=%s AND event_type='SOURCE_PUBLISHED'",
                    (tenant, workspace))
        audit = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s "
                    "AND workspace_id=%s AND kind='SOURCE_PUBLISHED'",
                    (tenant, workspace))
        outbox = cur.fetchone()[0]
    return {"revisions": revisions, "apply_publish_keys": apply_publish_keys,
            "audit": audit, "outbox": outbox}


def artifact_text(store, tenant, workspace, target):
    head = store.head(tenant, workspace)
    return store.read_artifact_blob(
        tenant, workspace, head.source_revision_id, target)["content"].decode("utf-8")


def reason_of(body):
    """Failure reason text of a terminal job body (execution_reason /
    execution_result, whichever the API exposes)."""
    return " ".join(str(body.get(k)) for k in
                    ("execution_reason", "reason", "execution_result", "failure_reason"))


# ---------------------------------------------------------------------------
# Stale-approval helpers.
#
# Contract: the approval `snapshot` (source_revision_id, decision_epoch)
# must equal the CURRENT head at apply time, so ANY publish between approve
# and apply stales the approval. A fixture that builds a second bundle (whose
# base publish moves the head) or that publishes a sibling / re-publishes the
# target after building therefore tests the SUPERSEDED contract. These
# helpers seed everything BEFORE the bundle is approved.
# ---------------------------------------------------------------------------

def build_ready_bundles(setup, client, workspace, monkeypatch, owner, *,
                        target_artifact_id=None, expected_current=14,
                        proposed_values=(30,), siblings=None, base_text=None,
                        edit_path="refund_window_days", record_approval=True):
    """Like `build_ready_to_publish_yaml`, but the base head is published ONCE
    (optionally a multi-artifact base: `siblings` = {artifact_id: text}) and
    then N bundles (one per `proposed_values` entry) are proposed, reviewed,
    approved and human-decided over that same head, so all N approvals carry
    the identical (source_revision_id, decision_epoch) snapshot. Returns a list
    of `ready` dicts shaped like `build_ready_to_publish_yaml`'s."""
    from tests.ai7t01.conftest import submit_claim_and_get as _scg
    from tests.ai7t02.conftest import submit_claim_and_get_review as _rev
    store, tenant = setup["store"], setup["tenant"]
    target = target_artifact_id or f"config-apply-t02-{uuid4().hex[:8]}.yaml"
    intent_id, _cand = create_confirmed_python_intent(setup, monkeypatch, owner, raw_value=999)
    text = base_text or f"refund_window_days: {expected_current}\n"
    rev = f"rev-{uuid4().hex}"
    manifest = {target: store.stage_blob(text.encode("utf-8"))}
    for aid, body in (siblings or {}).items():
        manifest[aid] = store.stage_blob(body.encode("utf-8"))
    store.publish_source(tenant, workspace, rev, manifest,
                         expected_version=store.head(tenant, workspace).version,
                         operation="source.import.commit", idempotency_key=uuid4().hex)
    store.record_source_observations(
        tenant, workspace, rev, target,
        [{"kind": "text_chunk", "locator": {"chunk": 0}, "raw_value": text,
          "fields": {}, "parser_version": "1.0.0"}])
    drain_outbox(store, tenant, workspace)
    impact = create_real_impact_job(setup, client, workspace)
    sim = create_real_simulation_job(setup, client, workspace)
    out = []
    for i, proposed in enumerate(proposed_values):
        body = _scg(setup, client, workspace, kind="proposal", payload={
            "intent_id": intent_id, "impact_job_id": impact, "simulation_job_id": sim,
            "target_artifact_id": target, "edit_kind": "yaml_typed_edit",
            "edits": [{"path": edit_path, "expected_current": expected_current,
                       "proposed": proposed}]}, worker_id=f"t02-bundles-prop-{i}")
        assert body["execution_status"] == "COMPLETED", body
        proposal_job_id = body.get("job_id") or body.get("id")
        review = _rev(setup, client, workspace, proposal_job_id=proposal_job_id,
                      worker_id=f"t02-bundles-review-{i}", with_contract=True)
        assert review["execution_status"] == "COMPLETED", review
        review_job_id = review.get("job_id") or review.get("id")
        appr = submit_claim_and_get_approval(
            setup, client, workspace, proposal_job_id=proposal_job_id,
            review_job_id=review_job_id, worker_id=f"t02-bundles-appr-{i}")
        assert appr["execution_status"] == "COMPLETED", appr
        approval_job_id = appr.get("job_id") or appr.get("id")
        digest = appr["execution_result"]["approval_digest"]
        approver = (record_real_approve_decision(
            setup, workspace, approval_job_id=approval_job_id, digest=digest)
            if record_approval else None)
        out.append({"approval_job_id": approval_job_id, "digest": digest,
                    "approver": approver, "target_artifact_id": target,
                    "proposal_job_id": proposal_job_id, "review_job_id": review_job_id,
                    "expected_current": expected_current, "proposed": proposed})
    drain_outbox(store, tenant, workspace)
    return out


def head_core(head):
    """Head identity without `policy_version` (granting the fixture approver's membership bumps it)."""
    return (head.version, head.source_revision_id, head.decision_epoch, head.knowledge_revision_id)


def assert_noop_bundle_is_unapprovable_and_unapplyable(setup, client, workspace, ready):
    """W1 rule (a BLOCKED / UNAVAILABLE review is never approved or applied): a true no-op bundle's
    review check is UNAVAILABLE, so the decision route refuses `approve` (409) and the apply gate
    refuses even a forced approve row (`approval_readiness_blocked`). Returns the apply body. The
    former "approved no-op completes with is_no_op" path is unreachable by design."""
    from tests.ai7t04.conftest import submit_decision
    from tests.ai8t01.conftest import _grant_change_approver, submit_claim_and_get_apply
    store, tenant = setup["store"], setup["tenant"]
    approver = _grant_change_approver(setup)
    resp = submit_decision(approver["client"], workspace, job_id=ready["approval_job_id"],
                           action="approve", expected_approval_digest=ready["digest"])
    assert resp.status_code == 409, resp.text
    store.record_change_decision(
        tenant, workspace, ready["approval_job_id"], actor_id=approver["account"], action="approve",
        reason="forced row", approval_digest=ready["digest"], self_approval=False, high_risk=False,
        idempotency_key=None, payload_hash="forced")
    body = submit_claim_and_get_apply(
        setup, client, workspace, approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"], worker_id="noop-refusal-worker")
    assert body["execution_status"] == "FAILED", body
    assert "approval_readiness_blocked" in (body["execution_reason"] or ""), body
    return body


def neutralize_f7(monkeypatch):
    """In-test only: disable BOTH F7 stale-tuple comparisons (pre-check and the
    in-tx head_check both call the module-level `_assert_snapshot_current`).
    Used by the F6 tests, whose subject is a DIFFERENT guard that a moved head
    would otherwise pre-empt (a target that is absent/unparseable in the current
    head can only be reached by moving the head, which F7 now refuses first)."""
    from infra.worker import apply as apply_mod
    monkeypatch.setattr(apply_mod, "_assert_snapshot_current", lambda snapshot, head: None)
