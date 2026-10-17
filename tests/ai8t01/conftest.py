"""Fixtures for the apply-stage tests: artifact executors and materialized staging.

Contract under test:

  - Reuse, not reimplementation: `infra/worker/proposal.py` implements the
    real typed-edit executors (`_yaml_typed_edits`/`_xlsx_typed_edits`,
    staged via `ctx.store.stage_blob`, an IMMUTABLE content-addressed write
    -- never `publish_source`). Materializing an approved edit must REUSE
    those exact functions (or an identical, imported, non-duplicated
    implementation) so apply semantics never differ from the dry run.

  - Job kind `kind="apply"`, `executor_id="apply.materialize.v1"`,
    submitted through the SAME generic `POST /v2/jobs` / `GET /v2/jobs/{id}`
    route pair every sibling kind uses (no second dispatch mechanism).
    Payload: `{"approval_job_id": <approval job id>,
    "expected_approval_digest": <string, required>}`. The executor:
      1. Resolves the approval job (`kind="approval"`, COMPLETED) the same
         way `infra/worker/approval.py::_resolve_job` already does; a
         missing/wrong-kind/not-completed approval is a hard
         `NonRetryableExecutionError`, never a fabricated bundle.
      2. Rejects (non-retryable) unless the approval's OWN
         `execution_result["readiness"] == "READY"` and
         `execution_result["approval_digest"] == expected_approval_digest`
         -- so a caller cannot apply a BLOCKED or since-superseded
         approval bundle by omitting/mismatching the digest.
      3. Resolves the approval's `proposal_job_id`, re-reads the CURRENT
         published artifact bytes fresh via `ctx.store.read_artifact_blob`
         (never trusting the proposal job's OWN already-staged
         `staged_blob_hash` as still valid -- time has passed since the
         proposal ran, and the source may have moved), and REPLAYS the
         proposal's own recorded typed edits (`execution_result["edits"]`,
         verbatim locators/expected_current/proposed values) against that
         freshly-read source using the SAME `_yaml_typed_edits`/
         `_xlsx_typed_edits` functions -- never calling a model, never
         accepting a new patch body in the apply payload.
      4. A locator/expected_current mismatch against the FRESH read (source
         drifted after proposal/approval) is `NonRetryableExecutionError`
         (`"stale_source_at_apply"`), never a fuzzy re-apply.
      5. On success, stages the resulting bytes via `ctx.store.stage_blob`
         (a true no-op stages nothing, `after_blob_hash == before_blob_
         hash`, mirroring the proposal executor's own no-op contract) and
         returns a `staged_manifest` of `{target_artifact_id:
         after_blob_hash}` plus an INDEPENDENT validation report (re-open
         the staged bytes with `openpyxl`/`yaml.safe_load`, i.e. a SECOND,
         independent parse pass distinct from the one that produced them)
         confirming the staged bytes actually parse. Never publishes
         (`store.publish_source` is never called by this executor).

The OBSERVABLE behavior these tests assert (staged manifest/hash
correctness; unrelated content preserved; stale/corrupt/out-of-scope/
renamed-sheet rejection with no partial write; retry idempotency; no model
call after approval) is the contract.

Reuses `tests.ai4t01.conftest` (`setup`/`login`/`publish_with_chunk`/
`drain_outbox`), `tests.ai7t01.conftest` (impact/simulation job helpers),
`tests.ai7t02.conftest`, and `tests.ai7t03.conftest`
(`build_real_ready_review_bundle`, `submit_claim_and_get_approval`)
verbatim (one shared fixture-resolution path). No production module is
touched by this file.
"""

from __future__ import annotations

import io
from uuid import uuid4

import pytest

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


#: PROPOSED kind name (no binding interface exists yet -- see module
#: docstring). Follows the AI7 naming convention (proposal -> review ->
#: approval -> apply), the natural next noun in the task card's own
#: pipeline ("apply staging worker ... staged immutable blob manifest").
APPLY_KIND = "apply"


def _publish_bytes(store, tenant, workspace, *, artifact_id, content: bytes) -> str:
    """Non-text publish (an XLSX workbook is not UTF-8 text) -- same
    `stage_blob` -> `publish_source` primitives
    `tests/ai7t01/test_patch_generation.py::_publish_bytes` already uses."""
    revision_id = f"rev-{uuid4().hex}"
    digest = store.stage_blob(content)
    store.publish_source(tenant, workspace, revision_id, {artifact_id: digest},
                         expected_version=store.head(tenant, workspace).version,
                         operation="source.import.commit", idempotency_key=uuid4().hex)
    return revision_id


def make_xlsx_bytes(*, sheet1_b2=14, sheet2_formula="=A1*2"):
    """A REAL, multi-sheet `.xlsx` workbook -- identical shape to
    `tests/ai7t01/test_patch_generation.py::_make_xlsx_bytes` (reused
    convention, not reinvented): sheet "Sheet1" carries a labeled cell
    (`A1`) and the numeric target cell (`B2`); sheet "Notes" carries an
    untouched cell PLUS a real formula -- both must survive an apply-stage
    materialization untouched."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet1 = workbook.active
    sheet1.title = "Sheet1"
    sheet1["A1"] = "rate_label"
    sheet1["B2"] = sheet1_b2
    sheet1["A1"].font = openpyxl.styles.Font(bold=True)
    notes = workbook.create_sheet("Notes")
    notes["A1"] = 21
    notes["A2"] = sheet2_formula
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _fixture_owner(setup):
    """Same account `tests.ai5t02.conftest.owner` builds (knowledge_owner), for helpers that are not
    pytest fixtures."""
    from tests.ai4t01.conftest import DSN, login
    from infra.adapters.v2_identity import PostgresIdentityStore
    nonce = uuid4().hex
    account, credential = f"owner-{nonce}", f"cred-owner-{nonce}"
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.create_account(account, credential)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                account, ("knowledge_owner",))
    client = setup["browser"]()
    login(client, account, credential)
    return {"account": account, "credential": credential, "client": client}


def build_real_ready_yaml_approval(setup, client, workspace, monkeypatch, owner, *,
                                    target_artifact_id="config-apply-t01.yaml",
                                    expected_current=14, proposed=30):
    """Builds a real, COMPLETED `kind="approval"` job over a real,
    COMPLETED `yaml_typed_edit` proposal + unanimous-pass review pair --
    the exact real upstream bundle the apply-stage executor must consume. Returns
    `(approval_job_id, approval_body, proposal_job_id, review_job_id)`."""
    proposal_job_id, review_job_id, _proposal_body, _review_body = (
        build_real_ready_review_bundle(
            setup, client, workspace, monkeypatch, owner,
            target_artifact_id=target_artifact_id,
            expected_current=expected_current, proposed=proposed, with_contract=True))
    approval_body = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="t01-apply-fixture-approval-worker")
    assert approval_body["execution_status"] == "COMPLETED", approval_body
    approval_job_id = approval_body.get("job_id") or approval_body.get("id")
    return approval_job_id, approval_body, proposal_job_id, review_job_id


def build_real_ready_xlsx_approval(setup, client, workspace, *,
                                    target_artifact_id="rates-apply-t01.xlsx",
                                    sheet="Sheet1", cell="B2",
                                    expected_current=14, proposed=30,
                                    workbook_bytes=None):
    """Builds a real, COMPLETED `kind="approval"` job over a real, COMPLETED
    `xlsx_typed_edit` proposal + unanimous-pass review pair. Returns
    `(approval_job_id, approval_body, proposal_job_id, review_job_id,
    original_bytes)`. Uses a fabricated `intent_id` (sufficient for the
    xlsx_typed_edit branch, per `tests/ai7t01/test_patch_generation.py`'s
    own precedent) since this bundle's purpose is exercising the apply-stage
    executor, not proposal-level intent resolution."""
    original = workbook_bytes if workbook_bytes is not None else make_xlsx_bytes(
        sheet1_b2=expected_current)
    # W1: a BLOCKED review can no longer be approved, so the fixture uses a REAL confirmed intent
    # (a fabricated one always leaves an `intent_not_found` proposal blocker) and a fixture contract.
    # The intent's KM publish comes first, then the workbook (same order as the yaml builders).
    with pytest.MonkeyPatch.context() as _mp:
        _owner = _fixture_owner(setup)
        intent_id = create_confirmed_python_intent(setup, _mp, _owner, raw_value=999)[0]
    _publish_bytes(setup["store"], setup["tenant"], workspace,
                  artifact_id=target_artifact_id, content=original)
    drain_outbox(setup["store"], setup["tenant"], workspace)
    impact_job_id = create_real_impact_job(setup, client, workspace)
    simulation_job_id = create_real_simulation_job(setup, client, workspace)
    proposal_body = submit_claim_and_get(setup, client, workspace, kind="proposal", payload={
        "intent_id": intent_id, "impact_job_id": impact_job_id,
        "simulation_job_id": simulation_job_id, "target_artifact_id": target_artifact_id,
        "edit_kind": "xlsx_typed_edit",
        "edits": [{"sheet": sheet, "cell": cell,
                  "expected_current": expected_current, "proposed": proposed}]})
    assert proposal_body["execution_status"] == "COMPLETED", proposal_body
    proposal_job_id = proposal_body.get("job_id") or proposal_body.get("id")
    review_body = submit_claim_and_get_review(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        worker_id="t01-apply-fixture-review-worker", with_contract=True)
    assert review_body["execution_status"] == "COMPLETED", review_body
    review_job_id = review_body.get("job_id") or review_body.get("id")
    approval_body = submit_claim_and_get_approval(
        setup, client, workspace, proposal_job_id=proposal_job_id,
        review_job_id=review_job_id, worker_id="t01-apply-fixture-approval-worker")
    assert approval_body["execution_status"] == "COMPLETED", approval_body
    approval_job_id = approval_body.get("job_id") or approval_body.get("id")
    return (approval_job_id, approval_body, proposal_job_id, review_job_id, original)


def submit_apply_job(client, workspace, *, approval_job_id, job_id=None,
                     expected_approval_digest=None, extra_payload=None):
    """Submit a `kind="apply"` job through the real generic
    `POST /v2/jobs` route."""
    payload = {"approval_job_id": approval_job_id}
    if expected_approval_digest is not None:
        payload["expected_approval_digest"] = expected_approval_digest
    if extra_payload:
        payload.update(extra_payload)
    return client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": APPLY_KIND,
        "job_id": job_id or f"job-apply-{uuid4().hex}",
        "payload": payload})


def claim_apply_job(setup, workspace, *, worker_id="apply-worker"):
    store, tenant = setup["store"], setup["tenant"]
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, worker_id)
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    return claim


def _grant_change_approver(setup):
    """A DISTINCT `change_approver` account -- never `setup['requester']`,
    the account that submitted the proposal/review/approval chain via
    `build_real_ready_yaml_approval`/`build_real_ready_xlsx_approval` above.
    Mirrors `tests/ai7t04/test_change_decision.py::_grant` verbatim (same
    role, same membership-grant primitive) so a real, distinct human actor
    is available to record the REAL `approve` decision the apply executor
    requires (`infra/worker/apply.py`'s
    `change_decisions`/`approval_not_decided` guard). Using a genuinely
    distinct account (not merely a `high_risk=False` flag) satisfies
    `require_distinct_approver` regardless of the workspace's current
    policy setting, so these fixtures never depend on that policy default."""
    from infra.adapters.v2_identity import PostgresIdentityStore

    nonce = uuid4().hex
    account, credential = f"apply-approver-{nonce}", f"cred-apply-approver-{nonce}"
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.create_account(account, credential)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                account, ("change_approver",))
    client = setup["browser"]()
    login(client, account, credential)
    return {"account": account, "credential": credential, "client": client}


def neutralize_readiness_gate(monkeypatch):
    """In-test seam (precedent `neutralize_f7`): report the approval's readiness as READY to the apply
    executor ONLY, so a test can reach an OLDER guard / behaviour that the W1 readiness gate pre-empts.
    Production is untouched; the product path itself is covered by the separate refusal tests."""
    from infra.worker import apply as apply_mod
    real = apply_mod._resolve_job

    def _ready(ctx, job_id, *, expected_kind, label):
        job = dict(real(ctx, job_id, expected_kind=expected_kind, label=label))
        if expected_kind == "approval":
            job["execution_result"] = {**(job.get("execution_result") or {}), "readiness": "READY"}
        return job
    monkeypatch.setattr(apply_mod, "_resolve_job", _ready)


def force_approve_row(setup, workspace, approval_job_id, digest):
    """Ledger approve row written at store level (the route refuses a BLOCKED bundle, W1)."""
    approver = _grant_change_approver(setup)
    setup["store"].record_change_decision(
        setup["tenant"], workspace, approval_job_id, actor_id=approver["account"], action="approve",
        reason="forced row", approval_digest=digest, self_approval=False, high_risk=False,
        idempotency_key=None, payload_hash="forced")
    return approver


def record_real_approve_decision(setup, workspace, *, approval_job_id, digest,
                                 high_risk=False, approver=None):
    """Records a REAL, durable human `approve` decision via the real
    `POST /v2/jobs/{approval_job_id}/decision` route (never a fabricated/
    mocked one) -- the exact primitive `infra/worker/apply.py`'s
    `change_decisions` guard now requires before it will stage anything.
    Uses a DISTINCT `change_approver` account unless one is supplied.
    Returns the approver dict (`{"account", "credential", "client"}`)."""
    approver = approver or _grant_change_approver(setup)
    resp = submit_decision(
        approver["client"], workspace, job_id=approval_job_id, action="approve",
        reason="AI8-T01 apply-stage fixture: real recorded human approval",
        expected_approval_digest=digest, high_risk=high_risk)
    assert resp.status_code == 200, resp.text
    return approver


def submit_claim_and_get_apply(setup, client, workspace, *, approval_job_id,
                               job_id=None, expected_approval_digest=None,
                               extra_payload=None, worker_id="apply-worker"):
    """PROPOSED end-to-end helper mirroring every sibling kind's own
    submit->claim->GET convention. Callers that expect a REJECTED
    submission (400) must call `submit_apply_job` directly instead."""
    job_id = job_id or f"job-apply-{uuid4().hex}"
    resp = submit_apply_job(
        client, workspace, approval_job_id=approval_job_id, job_id=job_id,
        expected_approval_digest=expected_approval_digest, extra_payload=extra_payload)
    assert resp.status_code == 200, resp.text
    claim_apply_job(setup, workspace, worker_id=worker_id)
    return get_job_status(client, workspace, job_id)
