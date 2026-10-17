"""Apply output contract: "Result returns published
artifacts + pending human work + exact verification scope".

Everything goes through the real path: POST /v2/jobs {apply} ->
claim_and_admit_one -> ApplyExecutor.execute -> GET /v2/jobs/{id}. Expected
values are computed INDEPENDENTLY from the upstream proposal/review job rows
(GET) and from the store head/blob reads, never from the apply result itself.

Sections
  A  typed-edit (yaml, xlsx): [] pending + scope == independent expectation.
  B  no-op: after == before, revision None.
  C  recovered_prior_publish retry returns identical F4 fields.
  D  derivation: upstream plan/review/approval mutated -> apply output follows
     (proves not hardcoded); pending entries reported verbatim but never
     published as edits.
  E  failure injection: helper neutralized (empty / hardcoded) -> the same
     assertions go red; real helper -> green.
  F  manual-work approval: head unchanged and pending human work reported.
"""

from __future__ import annotations

import copy
from uuid import uuid4

import pytest
from psycopg2.extras import Json

from infra.adapters import v2_postgres
from infra.worker import apply as apply_mod
from infra.worker import job_worker
from tests.ai4t01.conftest import drain_outbox, publish_with_chunk
from tests.ai7t01.conftest import (
    create_real_impact_job, create_real_simulation_job, submit_job)
from tests.ai7t02.conftest import submit_claim_and_get_review
from tests.ai7t03.conftest import submit_approval_job
from tests.ai8t01.conftest import (
    build_real_ready_xlsx_approval, get_job_status, login,
    record_real_approve_decision, submit_apply_job, submit_claim_and_get_apply)
from tests.ai8t02.conftest import build_ready_to_publish_yaml, publish_footprint
from tests.ai8t02.test_publish_round2_attacks import _crash_after_commit_then_retry

SCOPE_KEYS = {"target_artifact_ids", "before_blob_hash", "after_blob_hash",
              "published_revision_id", "verification_plan", "review_job_id",
              "review_board_id", "review_checks"}


def _client(setup):
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    return client


def _blob_hash(store, t, ws, revision, target):
    return store.read_artifact_blob(t, ws, revision, target)["blob_hash"]


def _expected(client, workspace, ready, *, before, after, revision):
    """Independent expectation built from upstream job rows + store reads."""
    proposal = get_job_status(client, workspace, ready["proposal_job_id"])["execution_result"]
    review = get_job_status(client, workspace, ready["review_job_id"])["execution_result"]
    pending = [dict(e) for e in proposal["verification_plan"]
               if e.get("action") == "human_authoring_required"]
    return pending, {
        "target_artifact_ids": [ready["target_artifact_id"]],
        "before_blob_hash": before,
        "after_blob_hash": after,
        "published_revision_id": revision,
        "verification_plan": [dict(e) for e in proposal["verification_plan"]],
        "review_job_id": ready["review_job_id"],
        "review_board_id": review["board_id"],
        "review_checks": {n: e["status"] for n, e in review["checks"].items()},
    }


def _assert_f4(body, pending, scope):
    res = body["execution_result"]
    assert res["pending_human_work"] == pending, res["pending_human_work"]
    assert res["verification_scope"] == scope, res["verification_scope"]


def _apply(setup, client, ready, **kw):
    return submit_claim_and_get_apply(
        setup, client, setup["workspace"], approval_job_id=ready["approval_job_id"],
        expected_approval_digest=ready["digest"], **kw)


def _yaml_positive(setup, monkeypatch, owner):
    """Full positive scenario + assertions; reused by the injection test."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    head_before = store.head(t, workspace)
    before = _blob_hash(store, t, workspace, head_before.source_revision_id,
                        ready["target_artifact_id"])
    body = _apply(setup, client, ready)
    assert body["execution_status"] == "COMPLETED", body
    head_after = store.head(t, workspace)
    after = _blob_hash(store, t, workspace, head_after.source_revision_id,
                       ready["target_artifact_id"])
    assert after != before and head_after.version == head_before.version + 1
    pending, scope = _expected(client, workspace, ready, before=before, after=after,
                               revision=head_after.source_revision_id)
    assert pending == [] and set(scope) == SCOPE_KEYS
    assert scope["review_checks"] and scope["review_board_id"]
    # the approved plan itself must bind the published hashes (plausibility of
    # "exact" scope): diff_review.after_hash == the actually published hash
    diff = [e for e in scope["verification_plan"] if e["action"] == "diff_review"]
    # NOTE upstream quirk (proposal.py): plan before_hash carries the
    # store's "sha256:" prefix, after_hash is bare hex; compare normalized.
    norm = lambda h: h.split(":", 1)[-1]  # noqa: E731
    assert diff and norm(diff[0]["after_hash"]) == norm(after)
    assert norm(diff[0]["before_hash"]) == norm(before)
    _assert_f4(body, pending, scope)
    return client, ready, body, scope


# --------------------------------------------------------------------------- A

def test_a1_yaml_typed_edit_pending_empty_and_scope_matches_actual_state(
        setup, monkeypatch, owner):
    _yaml_positive(setup, monkeypatch, owner)


def test_a2_xlsx_typed_edit_scope_matches_actual_state(setup):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    approval_id, abody, proposal_id, review_id, _orig = build_real_ready_xlsx_approval(
        setup, client, workspace, target_artifact_id=f"rates-f4-{uuid4().hex[:6]}.xlsx")
    digest = abody["execution_result"]["approval_digest"]
    record_real_approve_decision(setup, workspace, approval_job_id=approval_id, digest=digest)
    target = get_job_status(client, workspace, proposal_id)["execution_result"]["target_artifact_id"]
    ready = {"proposal_job_id": proposal_id, "review_job_id": review_id,
             "target_artifact_id": target, "approval_job_id": approval_id, "digest": digest}
    head_before = store.head(t, workspace)
    before = _blob_hash(store, t, workspace, head_before.source_revision_id, target)
    body = _apply(setup, client, ready)
    assert body["execution_status"] == "COMPLETED", body
    head_after = store.head(t, workspace)
    after = _blob_hash(store, t, workspace, head_after.source_revision_id, target)
    assert after != before
    pending, scope = _expected(client, workspace, ready, before=before, after=after,
                               revision=head_after.source_revision_id)
    _assert_f4(body, pending, scope)


# --------------------------------------------------------------------------- B

def test_b_no_op_scope_after_equals_before_revision_none(setup, monkeypatch, owner):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    # W1: the product path refuses a no-op bundle (test_b2 below). Here ONLY the readiness gate is
    # neutralized in-test and the approve row forced, to reach the original f4 scope assertions.
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner,
                                        expected_current=14, proposed=14, record_approval=False)
    from tests.ai8t01.conftest import force_approve_row, neutralize_readiness_gate
    ready["approver"] = force_approve_row(setup, workspace, ready["approval_job_id"], ready["digest"])
    neutralize_readiness_gate(monkeypatch)
    head, fp = store.head(t, workspace), publish_footprint(store, t, workspace)
    before = _blob_hash(store, t, workspace, head.source_revision_id, ready["target_artifact_id"])
    body = _apply(setup, client, ready)
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["is_no_op"] is True
    assert store.head(t, workspace) == head and publish_footprint(store, t, workspace) == fp
    pending, scope = _expected(client, workspace, ready, before=before, after=before,
                               revision=None)
    assert scope["published_revision_id"] is None
    _assert_f4(body, pending, scope)


def test_b2_no_op_bundle_is_refused_by_the_product_path(setup, monkeypatch, owner):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner,
                                        expected_current=14, proposed=14, record_approval=False)
    head, fp = store.head(t, workspace), publish_footprint(store, t, workspace)
    from tests.ai8t02.conftest import assert_noop_bundle_is_unapprovable_and_unapplyable, head_core
    body = assert_noop_bundle_is_unapprovable_and_unapplyable(setup, client, workspace, ready)
    assert (body.get("execution_result") or {}).get("published") is not True
    assert head_core(store.head(t, workspace)) == head_core(head)
    assert publish_footprint(store, t, workspace) == fp


# --------------------------------------------------------------------------- C

def test_c_recovered_prior_publish_returns_identical_f4_fields(setup, monkeypatch, owner):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    before = _blob_hash(store, t, workspace, store.head(t, workspace).source_revision_id,
                        ready["target_artifact_id"])
    job_id, committed, fp = _crash_after_commit_then_retry(
        setup, monkeypatch, client, workspace, ready, False)
    body = get_job_status(client, workspace, job_id)
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["recovered_prior_publish"] is True
    after = _blob_hash(store, t, workspace, committed.source_revision_id,
                       ready["target_artifact_id"])
    pending, scope = _expected(client, workspace, ready, before=before, after=after,
                               revision=committed.source_revision_id)
    # identical to what a normal (non-recovered) run reports for the same publish
    _assert_f4(body, pending, scope)
    assert store.head(t, workspace) == committed and publish_footprint(store, t, workspace) == fp


# --------------------------------------------------------------------------- D

def _mutate_result(store, t, ws, job_id, fn):
    from tests import _row_gate
    _row_gate.disable()  # W2-AI10-09: forged row after approval, see tests/_row_gate.py
    with store._tx() as cur:
        cur.execute("SELECT execution_result FROM v2_jobs WHERE tenant_id=%s AND "
                    "workspace_id=%s AND job_id=%s", (t, ws, job_id))
        result = copy.deepcopy(cur.fetchone()[0])
        fn(result)
        cur.execute("UPDATE v2_jobs SET execution_result=%s WHERE tenant_id=%s AND "
                    "workspace_id=%s AND job_id=%s", (Json(result), t, ws, job_id))


def test_d1_scope_follows_the_upstream_plan_and_review_not_a_constant(setup, monkeypatch, owner):
    """Two different real bundles, then a third whose upstream rows are edited:
    every review/plan-derived value must follow."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    r1 = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner,
                                     proposed=31)
    b1 = _apply(setup, client, r1)
    # (fixture publishes are single-artifact revisions: build each bundle
    # only after the previous apply, or the earlier target is evicted)
    r2 = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner,
                                     proposed=32)
    b2 = _apply(setup, client, r2, worker_id="f4-w2")
    s1, s2 = (b["execution_result"]["verification_scope"] for b in (b1, b2))
    assert s1["after_blob_hash"] != s2["after_blob_hash"]
    assert s1["review_job_id"] == r1["review_job_id"] != s2["review_job_id"]
    assert s1["published_revision_id"] != s2["published_revision_id"]
    assert s1["target_artifact_ids"] != s2["target_artifact_ids"]
    assert s1["verification_plan"] != s2["verification_plan"]  # per-bundle hashes

    # Edit the upstream rows of a third bundle.
    r3 = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    extra = {"action": "human_authoring_required", "reason": "f4-marker-manual-step"}
    _mutate_result(store, t, workspace, r3["proposal_job_id"],
                   lambda res: res["verification_plan"].append(extra))
    _mutate_result(store, t, workspace, r3["review_job_id"], lambda res: (
        res["checks"].__setitem__("f4_extra_check", {"status": "PASS", "detail": "x"}),
        res["checks"]["acceptance_tests"].__setitem__("status", "F4_MUTATED"),
        res.__setitem__("board_id", "f4-board")))
    head_before = store.head(t, workspace)
    body = _apply(setup, client, r3, worker_id="f4-w3")
    assert body["execution_status"] == "COMPLETED", body
    res = body["execution_result"]
    assert res["pending_human_work"] == [extra]  # verbatim
    scope = res["verification_scope"]
    assert extra in scope["verification_plan"]
    assert scope["review_board_id"] == "f4-board"
    assert scope["review_checks"]["f4_extra_check"] == "PASS"
    assert scope["review_checks"]["acceptance_tests"] == "F4_MUTATED"
    # manual work is REPORTED, never applied: exactly the typed edit was published
    head_after = store.head(t, workspace)
    assert head_after.version == head_before.version + 1
    text = store.read_artifact_blob(t, workspace, head_after.source_revision_id,
                                    r3["target_artifact_id"])["content"].decode()
    assert "f4-marker-manual-step" not in text and str(r3["proposed"]) in text


def test_d2_approval_manual_work_marker_yields_one_synthetic_entry(setup, monkeypatch, owner):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    ready = build_ready_to_publish_yaml(setup, client, workspace, monkeypatch, owner)
    _mutate_result(store, t, workspace, ready["approval_job_id"],
                   lambda res: res.__setitem__("required_action", "manual_work_required"))
    body = _apply(setup, client, ready)
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["pending_human_work"] == [
        {"action": "human_authoring_required",
         "reason": "approval_required_action_manual_work_required"}]
    assert body["execution_result"]["published"] is True  # typed edit only


# --------------------------------------------------------------------------- E

@pytest.mark.parametrize("mode", ["empty", "hardcoded", "real"],
                         ids=["INJECT-empty", "INJECT-hardcoded", "guard-green"])
def test_e_injection_hardcoded_or_empty_fields_are_detected(
        setup, monkeypatch, owner, mode):
    if mode == "empty":
        monkeypatch.setattr(apply_mod, "_pending_and_scope", lambda *a, **k: ([], {}))
    elif mode == "hardcoded":
        monkeypatch.setattr(apply_mod, "_pending_and_scope", lambda *a, **k: (
            [], {**{k_: None for k_ in SCOPE_KEYS}, "target_artifact_ids": ["x"],
                 "verification_plan": [], "review_checks": {}}))
    if mode == "real":
        _yaml_positive(setup, monkeypatch, owner)
    else:
        with pytest.raises((AssertionError, KeyError)):
            _yaml_positive(setup, monkeypatch, owner)


# --------------------------------------------------------------------------- F

def _manual_approval(setup, client, workspace):
    store, t = setup["store"], setup["tenant"]
    target = f"notes-f4-manual-{uuid4().hex[:6]}.md"
    publish_with_chunk(store, t, workspace, artifact_id=target, text="unrelated: 1\n")
    drain_outbox(store, t, workspace)
    impact = create_real_impact_job(setup, client, workspace)
    sim = create_real_simulation_job(setup, client, workspace)
    proposal_id = f"job-proposal-{uuid4().hex}"
    r = submit_job(client, workspace, kind="proposal", job_id=proposal_id, payload={
        "intent_id": f"intent-nonexistent-{uuid4().hex}", "impact_job_id": impact,
        "simulation_job_id": sim, "target_artifact_id": target, "edit_kind": "markdown"})
    assert r.status_code == 200, r.text
    job_worker.claim_and_admit_one(store, t, workspace, "f4-proposal")
    pb = get_job_status(client, workspace, proposal_id)
    assert pb["execution_result"]["classification"] == "manual", pb
    review = submit_claim_and_get_review(setup, client, workspace, proposal_job_id=proposal_id,
                                         worker_id="f4-review")
    review_id = review.get("job_id") or review.get("id")
    approval_id = f"job-approval-{uuid4().hex}"
    assert submit_approval_job(client, workspace, proposal_job_id=proposal_id,
                               review_job_id=review_id, job_id=approval_id).status_code == 200
    job_worker.claim_and_admit_one(store, t, workspace, "f4-approval")
    ab = get_job_status(client, workspace, approval_id)
    digest = ab["execution_result"]["approval_digest"]
    assert ab["execution_result"]["required_action"] == "manual_work_required"
    record_real_approve_decision(setup, workspace, approval_job_id=approval_id, digest=digest)
    return approval_id, digest, pb


def test_f_manual_approval_never_published_as_edit_head_and_footprint_unchanged(setup):
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    approval_id, digest, _pb = _manual_approval(setup, client, workspace)
    head, fp = store.head(t, workspace), publish_footprint(store, t, workspace)
    body = submit_claim_and_get_apply(setup, client, workspace, approval_job_id=approval_id,
                                      expected_approval_digest=digest)
    assert store.head(t, workspace) == head and publish_footprint(store, t, workspace) == fp
    assert not (body.get("execution_result") or {}).get("published")


def test_f5_manual_approval_result_reports_pending_human_work(setup):
    """Regression: a human-approved manual-work approval
    (proposal classification `manual`, the ONLY producer of
    `human_authoring_required` / `required_action=manual_work_required`) ends
    in FAILED `unsupported_edit_kind` at the apply gate, so the required "Result
    returns ... pending human work" would never be delivered for manual work: the
    non-empty `pending_human_work` branch is reachable only for a typed-edit
    apply whose upstream plan was hand-edited (test_d1/d2)."""
    workspace, store, t = setup["workspace"], setup["store"], setup["tenant"]
    client = _client(setup)
    approval_id, digest, pb = _manual_approval(setup, client, workspace)
    body = submit_claim_and_get_apply(setup, client, workspace, approval_job_id=approval_id,
                                      expected_approval_digest=digest)
    result = body.get("execution_result") or {}
    pending = result.get("pending_human_work")
    assert pending, ("manual approval produced no pending_human_work; apply status="
                     f"{body.get('execution_status')} reason={body.get('execution_reason')}")
    assert all(e["action"] == "human_authoring_required" for e in pending)
    assert not result.get("published")
