"""Approved manual-work bundles at the apply gate.

Requirement: external manual actions are not faked as source edits; the result
returns published artifacts + pending human work + exact verification scope.
Contract asserted here:

  * a python/markdown proposal classified `manual`, human-approved with the
    approval's `required_action == "manual_work_required"` and ZERO recorded
    edits, ends COMPLETED with `published` False, no revision, `is_no_op`,
    pending human work + verification scope from the REAL upstream rows;
    nothing is staged or published (head + SQL footprint unchanged);
  * every earlier gate (digest, recorded human approve decision, current
    approver authority) still applies;
  * any OTHER python/markdown shape still fails `unsupported_edit_kind`.

Every scenario goes through the real chain: POST /v2/jobs -> claim_and_admit_one
-> ApplyExecutor.execute -> job_worker finalize -> GET /v2/jobs/{id}. Guard
injections are in-test monkeypatches only (infra is never edited).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters import v2_postgres
from infra.worker import apply as apply_mod
from infra.worker import job_worker
from tests.ai4t01.conftest import drain_outbox, publish_with_chunk
from tests.ai7t01.conftest import (
    create_real_impact_job, create_real_simulation_job, get_job_status, submit_job)
from tests.ai7t02.conftest import submit_claim_and_get_review
from tests.ai7t03.conftest import submit_approval_job
from tests.ai7t04.conftest import submit_decision
from tests.ai8t01.conftest import (
    _grant_change_approver, record_real_approve_decision, submit_apply_job,
    submit_claim_and_get_apply)
from tests.ai8t02.conftest import build_ready_to_publish_yaml, publish_footprint, revoke_approver
from tests.ai8t02.test_publish_f4_pending_and_scope import (
    SCOPE_KEYS, _blob_hash, _client, _expected, _mutate_result)


def _bundle(setup, client, *, decision="approve", target=None, publish_base=True):
    """Real manual-work chain up to (optionally) a recorded decision.
    `publish_base=False` reuses the head/target an earlier bundle already
    published (F7: a second base publish would move the head and stale the
    earlier bundle's approval), so both approvals pin the same snapshot."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    target = target or f"notes-f5-manual-{uuid4().hex[:6]}.md"
    if publish_base:
        publish_with_chunk(store, t, workspace, artifact_id=target, text="unrelated: 1\n")
    drain_outbox(store, t, workspace)
    impact = create_real_impact_job(setup, client, workspace)
    sim = create_real_simulation_job(setup, client, workspace)
    proposal_id = f"job-proposal-{uuid4().hex}"
    r = submit_job(client, workspace, kind="proposal", job_id=proposal_id, payload={
        "intent_id": f"intent-nonexistent-{uuid4().hex}", "impact_job_id": impact,
        "simulation_job_id": sim, "target_artifact_id": target, "edit_kind": "markdown"})
    assert r.status_code == 200, r.text
    job_worker.claim_and_admit_one(store, t, workspace, "f5-proposal")
    pb = get_job_status(client, workspace, proposal_id)
    assert pb["execution_result"]["classification"] == "manual", pb
    review = submit_claim_and_get_review(setup, client, workspace, proposal_job_id=proposal_id,
                                         worker_id="f5-review")
    review_id = review.get("job_id") or review.get("id")
    approval_id = f"job-approval-{uuid4().hex}"
    assert submit_approval_job(client, workspace, proposal_job_id=proposal_id,
                               review_job_id=review_id, job_id=approval_id).status_code == 200
    job_worker.claim_and_admit_one(store, t, workspace, "f5-approval")
    ab = get_job_status(client, workspace, approval_id)
    digest = ab["execution_result"]["approval_digest"]
    assert ab["execution_result"]["required_action"] == "manual_work_required"
    approver = None
    if decision == "approve":
        approver = record_real_approve_decision(
            setup, workspace, approval_job_id=approval_id, digest=digest)
    elif decision == "reject":
        approver = _grant_change_approver(setup)
        resp = submit_decision(approver["client"], workspace, job_id=approval_id,
                               action="reject", reason="no", expected_approval_digest=digest)
        assert resp.status_code == 200, resp.text
    return {"approval_job_id": approval_id, "digest": digest, "proposal_job_id": proposal_id,
            "review_job_id": review_id, "target_artifact_id": target, "approver": approver}


def _apply(setup, client, b, **kw):
    return submit_claim_and_get_apply(
        setup, client, setup["workspace"], approval_job_id=b["approval_job_id"],
        expected_approval_digest=kw.pop("digest", b["digest"]), **kw)


def _state(setup):
    s, t, w = setup["store"], setup["tenant"], setup["workspace"]
    return s.head(t, w), publish_footprint(s, t, w)


def _reason(body):
    return body.get("execution_reason") or ""


# --------------------------------------------------------------------------- positive

def test_f5_manual_approval_completes_with_pending_human_work_and_scope(setup):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    head, fp = _state(setup)
    current = _blob_hash(store, t, workspace, head.source_revision_id, b["target_artifact_id"])
    body = _apply(setup, client, b)
    assert body["execution_status"] == "COMPLETED", body
    res = body["execution_result"]
    assert res["published"] is False and res["published_revision_id"] is None
    assert res["is_no_op"] is True and res["manual_work_only"] is True
    assert res["edit_kind"] == "markdown" and res["target_artifact_id"] == b["target_artifact_id"]
    assert res["staged_manifest"] == {b["target_artifact_id"]: current}
    pending, scope = _expected(client, workspace, b, before=current, after=current, revision=None)
    assert pending, "the real proposal plan must carry human_authoring_required"
    assert all(e["action"] == "human_authoring_required" for e in pending)
    assert res["pending_human_work"] == pending
    assert res["verification_scope"] == scope and set(scope) == SCOPE_KEYS
    assert scope["review_checks"] and scope["review_board_id"]
    assert _state(setup) == (head, fp), "manual work touched head or the publish footprint"
    assert store.read_artifact_blob(t, workspace, head.source_revision_id,
                                    b["target_artifact_id"])["content"] == b"unrelated: 1\n"


def test_f5_python_edit_kind_manual_also_completes(setup):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    _mutate_result(store, t, workspace, b["proposal_job_id"],
                   lambda r: r.__setitem__("edit_kind", "python"))
    head, fp = _state(setup)
    body = _apply(setup, client, b)
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["edit_kind"] == "python"
    assert body["execution_result"]["pending_human_work"]
    assert body["execution_result"]["published"] is False
    assert _state(setup) == (head, fp)


# --------------------------------------------------------------------------- earlier gates

@pytest.mark.parametrize("case", ["undecided", "rejected", "wrong_digest"])
def test_f5_earlier_gates_still_fail_before_manual_branch(setup, case):
    client = _client(setup)
    b = _bundle(setup, client, decision={"undecided": None, "rejected": "reject"}.get(case, "approve"))
    head, fp = _state(setup)
    kw = {"digest": "sha256:" + "0" * 64} if case == "wrong_digest" else {}
    body = _apply(setup, client, b, **kw)
    assert body["execution_status"] == "FAILED", body
    expected = "approval_digest_mismatch" if case == "wrong_digest" else "approval_not_decided"
    assert _reason(body).startswith(expected), body
    assert not (body.get("execution_result") or {}).get("pending_human_work")
    assert _state(setup) == (head, fp)


# --------------------------------------------------------------------------- revoked approver

@pytest.mark.parametrize("neutralize", [True, False], ids=["INJECT-red", "guard-green"])
def test_f5_revoked_approver_never_completes_manual_apply(setup, monkeypatch, neutralize):
    client = _client(setup)
    b = _bundle(setup, client)
    revoke_approver(setup, setup["workspace"], b["approver"])
    head, fp = _state(setup)
    if neutralize:
        monkeypatch.setattr(v2_postgres.PostgresV2Store, "_recheck_actor_authority",
                            lambda self, *a, **k: None)
    body = _apply(setup, client, b)
    if neutralize:
        # red: with the recheck disabled the violation is observable
        assert body["execution_status"] == "COMPLETED", body
    else:
        assert body["execution_status"] == "FAILED", body
        assert _reason(body).startswith("approver_authority_revoked"), body
        assert not (body.get("execution_result") or {}).get("pending_human_work")
    assert _state(setup) == (head, fp)


def test_f5_revoked_red_restore_green_and_unrevoked_control(setup, monkeypatch):
    """Same revoked approval: guard off -> COMPLETED (violation), guard restored
    -> FAILED approver_authority_revoked; an unrevoked bundle is the green control."""
    client = _client(setup)
    b1 = _bundle(setup, client)
    # same head + target as b1 (no second base publish: F7 would stale b1)
    b2 = _bundle(setup, client, target=b1["target_artifact_id"], publish_base=False)
    revoke_approver(setup, setup["workspace"], b1["approver"])
    head, fp = _state(setup)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "_recheck_actor_authority",
                   lambda self, *a, **k: None)
        red = _apply(setup, client, b1)
    assert red["execution_status"] == "COMPLETED"
    restored = _apply(setup, client, b1)
    assert restored["execution_status"] == "FAILED"
    assert _reason(restored).startswith("approver_authority_revoked")
    assert _apply(setup, client, b2)["execution_status"] == "COMPLETED"
    assert _state(setup) == (head, fp)


# --------------------------------------------------------------------------- retry / repeat

def test_f5_repeat_and_retry_are_stable_and_write_nothing(setup):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    head, fp = _state(setup)
    job_id = f"job-apply-{uuid4().hex}"
    first = _apply(setup, client, b, job_id=job_id)
    assert first["execution_status"] == "COMPLETED", first
    # store-level job-create replay with the ORIGINAL binding: prior job, no re-run
    store.create_job(t, workspace, job_id, kind="apply",
                     payload={"approval_job_id": b["approval_job_id"],
                              "expected_approval_digest": b["digest"]},
                     expected_version=head.version, idempotency_key=f"job-create-{job_id}")
    assert get_job_status(client, workspace, job_id)["execution_result"] == first["execution_result"]
    second = _apply(setup, client, b)
    assert second["execution_status"] == "COMPLETED"
    for key in ("pending_human_work", "verification_scope", "published", "staged_manifest",
                "is_no_op", "manual_work_only", "target_artifact_id"):
        assert second["execution_result"][key] == first["execution_result"][key], key
    assert _state(setup) == (head, fp)


# --------------------------------------------------------------------------- concurrent head change

@pytest.mark.parametrize("move", ["sibling_only", "target_changed"])
def test_f5_manual_apply_on_moved_head_fails_stale_and_reports_no_pending_work(setup, move):
    """Stale approval: a manual apply on a moved head does not complete.
    Source changed since review => stale proposal, not a
    valid approval. A stale manual-work approval must NOT report pending human
    work as COMPLETED; it fails closed with the named reason and writes nothing."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    publish_with_chunk(store, t, workspace, artifact_id="unrelated-f5-other.md", text="other: 1\n")
    if move == "target_changed":
        publish_with_chunk(store, t, workspace, artifact_id=b["target_artifact_id"],
                           text="changed concurrently: 2\n")
    drain_outbox(store, t, workspace)
    head, fp = _state(setup)
    body = _apply(setup, client, b)
    assert body["execution_status"] == "FAILED", body
    assert _reason(body).startswith("approval_stale_source"), body
    assert not (body.get("execution_result") or {}).get("pending_human_work"), body
    assert _state(setup) == (head, fp), "stale manual apply wrote state"


# --------------------------------------------------------------------------- mixed / not silently completed

def test_f5_typed_edit_with_manual_markers_still_publishes_typed_edit_only(setup, monkeypatch, owner):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)

    def _mark(res):
        res["classification"] = "manual"
        res["verification_plan"] = list(res.get("verification_plan") or []) + [
            {"action": "human_authoring_required", "reason": "f5-mixed-marker"}]
    _mutate_result(store, t, workspace, ready["proposal_job_id"], _mark)
    _mutate_result(store, t, workspace, ready["approval_job_id"],
                   lambda r: r.__setitem__("required_action", "manual_work_required"))
    head, fp = _state(setup)
    body = submit_claim_and_get_apply(setup, client, workspace,
                                      approval_job_id=ready["approval_job_id"],
                                      expected_approval_digest=ready["digest"])
    assert body["execution_status"] == "COMPLETED", body
    res = body["execution_result"]
    assert res["published"] is True and not res.get("manual_work_only")
    assert any(e.get("reason") == "f5-mixed-marker" for e in res["pending_human_work"])
    h2, fp2 = _state(setup)
    assert h2.version == head.version + 1 and fp2["revisions"] == fp["revisions"] + 1


def _shape(kind):
    """Mutations turning the manual bundle into a NON-manual shape."""
    if kind == "proposal_not_manual":
        return "proposal", lambda r: r.__setitem__("classification", "recommended")
    if kind == "manual_with_recorded_edits":
        return "proposal", lambda r: r.__setitem__("edits", [{"path": "a", "before": 1, "after": 2}])
    if kind == "approval_not_manual_work":
        return "approval", lambda r: r.__setitem__("required_action", "approve_and_apply")
    if kind == "approval_no_required_action":
        return "approval", lambda r: r.pop("required_action", None)
    if kind == "unknown_edit_kind":
        return "proposal", lambda r: r.__setitem__("edit_kind", "toml")
    raise AssertionError(kind)


SHAPES = ["proposal_not_manual", "manual_with_recorded_edits", "approval_not_manual_work",
          "approval_no_required_action", "unknown_edit_kind"]


@pytest.mark.parametrize("kind", SHAPES)
def test_f5_non_manual_python_markdown_shapes_still_fail_unsupported_edit_kind(setup, monkeypatch, kind):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    which, fn = _shape(kind)
    _mutate_result(store, t, workspace, b[f"{which}_job_id"], fn)
    head, fp = _state(setup)
    if which == "approval":
        # W1: an approval turned non-manual is BLOCKED and the readiness gate would pre-empt the guard
        # under test; neutralize ONLY that gate in-test (see the sibling test for the product path).
        from tests.ai8t01.conftest import neutralize_readiness_gate
        neutralize_readiness_gate(monkeypatch)
    body = _apply(setup, client, b)
    assert body["execution_status"] == "FAILED", body
    assert _reason(body).startswith("unsupported_edit_kind"), body
    assert not (body.get("execution_result") or {}).get("pending_human_work")
    assert _state(setup) == (head, fp)


@pytest.mark.parametrize("kind", ["proposal_not_manual", "manual_with_recorded_edits",
                                  "approval_not_manual_work"])
@pytest.mark.parametrize("neutralize", [True, False], ids=["INJECT-red", "guard-green"])
def test_f5_condition_clauses_injection(setup, monkeypatch, kind, neutralize):
    """Neutralize ONE clause of the manual condition in-test by rewriting what the
    executor's `_resolve_job` returns (infra untouched): with the clause gone the
    non-manual bundle silently COMPLETES (violation observable); with the real
    condition it fails `unsupported_edit_kind`."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    which, fn = _shape(kind)
    _mutate_result(store, t, workspace, b[f"{which}_job_id"], fn)
    head, fp = _state(setup)
    if which == "approval":
        from tests.ai8t01.conftest import neutralize_readiness_gate
        neutralize_readiness_gate(monkeypatch)  # W1: only the new gate; the guard under test stays real
    if neutralize:
        real = apply_mod._resolve_job

        def _forged(ctx, job_id, *, expected_kind, label):
            job = dict(real(ctx, job_id, expected_kind=expected_kind, label=label))
            res = dict(job.get("execution_result") or {})
            if expected_kind == "proposal":
                res["classification"] = "manual"
                res["edits"] = []
            if expected_kind == "approval":
                res["required_action"] = "manual_work_required"
            job["execution_result"] = res
            return job
        monkeypatch.setattr(apply_mod, "_resolve_job", _forged)
    body = _apply(setup, client, b)
    if neutralize:
        assert body["execution_status"] == "COMPLETED", body
    else:
        assert body["execution_status"] == "FAILED" and _reason(body).startswith(
            "unsupported_edit_kind"), body
    assert _state(setup) == (head, fp)


@pytest.mark.parametrize("kind", ["approval_not_manual_work", "approval_no_required_action"])
def test_f5_non_manual_approval_shapes_are_refused_by_the_readiness_gate_on_the_product_path(setup, kind):
    """W1 product path (no neutralization): the same mutated approvals are BLOCKED and refused at apply."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    which, fn = _shape(kind)
    _mutate_result(store, t, workspace, b[f"{which}_job_id"], fn)
    head, fp = _state(setup)
    body = _apply(setup, client, b)
    assert body["execution_status"] == "FAILED", body
    assert _reason(body).startswith("approval_readiness_blocked"), body
    assert _state(setup) == (head, fp)


# --------------------------------------------------------------------------- lease loss

@pytest.mark.parametrize("takeover", [False, True], ids=["expired-unclaimed", "taken-over"])
def test_f5_lease_loss_on_manual_path_is_recoverable_and_writes_no_source_state(
        setup, monkeypatch, takeover):
    """Lease lost mid-execution on the manual path. There is no publish side
    effect to fence here, so the safety properties are: no head/footprint
    change, the stale worker's ack is refused (its event is NOT marked DONE by
    it), and the job is never stuck -- a new owner reclaims the event and drains
    it (`ALREADY_PROCESSED`/`DONE`), job COMPLETED with one result.
    OBSERVATION: job_worker `_terminal` calls the unfenced
    `finish_execution` BEFORE the fenced `_ack`, so the stale worker's
    deterministic, side-effect-free COMPLETED terminal row does land."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    drain_outbox(store, t, workspace)
    head, fp = _state(setup)
    job_id = f"job-apply-{uuid4().hex}"
    assert submit_apply_job(client, workspace, approval_job_id=b["approval_job_id"], job_id=job_id,
                            expected_approval_digest=b["digest"]).status_code == 200
    real = apply_mod._pending_and_scope
    taken = {}

    def _lose_lease(*a, **k):
        with store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                        "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'",
                        (t, workspace))
        if takeover:
            taken["claim"] = store.claim_outbox(t, workspace, "f5-new-owner", lease_seconds=60)
            assert taken["claim"] is not None
        return real(*a, **k)

    def _outbox_status(event_id):
        with store._tx() as cur:
            cur.execute("SELECT status FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s "
                        "AND id=%s", (t, workspace, event_id))
            return cur.fetchone()[0]

    with monkeypatch.context() as mp:
        mp.setattr(apply_mod, "_pending_and_scope", _lose_lease)
        stale_claim = job_worker.claim_and_admit_one(store, t, workspace, "f5-stale")
    assert stale_claim is not None
    assert _outbox_status(stale_claim.id) == "CLAIMED", "stale worker acked an event it no longer owns"
    assert _state(setup) == (head, fp)
    if takeover:
        outcome = job_worker.execute_admitted_claim(
            store, t, workspace, taken["claim"], "f5-new-owner")
        assert outcome in ("DONE", "ALREADY_PROCESSED"), outcome
    else:
        assert job_worker.claim_and_admit_one(store, t, workspace, "f5-new-owner") is not None
    assert _outbox_status(stale_claim.id) == "DONE", "event stuck: new owner could not drain it"
    done = get_job_status(client, workspace, job_id)
    assert done["execution_status"] == "COMPLETED", done
    assert done["execution_result"]["pending_human_work"]
    assert done["execution_result"]["published"] is False
    assert _state(setup) == (head, fp)


# --------------------------------------------------------------------------- F6 (target vanished)

def _apply_expect_clean_terminal(setup, client, b, worker):
    workspace = setup["workspace"]
    job_id = f"job-apply-{uuid4().hex}"
    assert submit_apply_job(client, workspace, approval_job_id=b["approval_job_id"], job_id=job_id,
                            expected_approval_digest=b["digest"]).status_code == 200
    try:
        job_worker.claim_and_admit_one(setup["store"], setup["tenant"], workspace, worker)
    except Exception as exc:  # noqa: BLE001 -- the point of the test
        pytest.fail(f"worker crashed with uncaught {type(exc).__name__}: {exc} "
                    f"(job status={get_job_status(client, workspace, job_id)['execution_status']})")
    return get_job_status(client, workspace, job_id)


def test_f6_OPEN_manual_apply_target_absent_from_current_head_fails_cleanly(setup):
    """Source changed since review so the manual target is no longer in head (a
    later import/publish without it). Expected: a named terminal FAILED (fail
    closed, nothing written), never an exception out of the worker."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    b = _bundle(setup, client)
    publish_with_chunk(store, t, workspace, artifact_id="replacement-f6.md", text="x: 1\n")
    drain_outbox(store, t, workspace)
    head, fp = _state(setup)
    body = _apply_expect_clean_terminal(setup, client, b, "f6-manual")
    assert body["execution_status"] == "FAILED", body
    assert _state(setup) == (head, fp)


def test_f6_OPEN_typed_apply_target_absent_from_current_head_fails_cleanly(setup, monkeypatch, owner):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    publish_with_chunk(store, t, workspace, artifact_id="replacement-f6b.md", text="x: 1\n")
    drain_outbox(store, t, workspace)
    head, fp = _state(setup)
    body = _apply_expect_clean_terminal(setup, client, ready, "f6-typed")
    assert body["execution_status"] == "FAILED", body
    assert _state(setup) == (head, fp)
