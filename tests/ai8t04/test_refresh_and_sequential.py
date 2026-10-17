"""An external refresh before the final acceptance makes the report
stale, and a SECOND change is proposed / simulated / applied from the ACTUAL
S1/K1 the first change produced (never a cached S0), ending at S2/K2 with the
exported bytes and accepted KM matching the reports.

Everything goes through the real chain; the "refresh" is a real second
`publish_source` on the same workspace head.
"""

from __future__ import annotations

import base64

from tests.ai4t01.conftest import publish_with_chunk
from tests.ai8t04.conftest import (
    accept_candidate_km, approver_client, awaiting_change, build_second_change, change_dto,
    head_tuple, job_row, patch_authority_kind, pinned, pump, report_digest, report_of,
    result_footprint, submit_result_decision, write_oracle,
)


def test_c3_external_refresh_before_acceptance_makes_the_report_stale(
        setup, monkeypatch, owner, tmp_path):
    change = awaiting_change(setup, monkeypatch, owner, tmp_path)
    store, tenant, ws = setup["store"], setup["tenant"], change["workspace"]
    digest = report_digest(change, setup)
    report_before = job_row(store, tenant, ws, change["verify_job_id"])
    # another actor refreshes the source between machine verification and acceptance
    publish_with_chunk(store, tenant, ws, artifact_id="refresh-import.yaml", text="fresh: 1\n")
    fp = result_footprint(setup, change)

    resp = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  digest=digest)
    assert resp.status_code == 409 and "report_stale" in resp.text, resp.text
    assert result_footprint(setup, change) == fp
    dto = change_dto(change["client"], ws, change["apply_job_id"])
    assert dto["state"] == "STALE" and dto["completion"]["status"] == "STALE"
    assert dto["completion"]["eligible"] is False and dto["completion"]["decision"] is None
    assert dto["lineage"]["verified_version"] == 0, "a stale report verifies nothing"
    # history is kept, not rewritten
    assert job_row(store, tenant, ws, change["verify_job_id"]) == report_before
    assert report_of(change, store, tenant)["after_source_revision_id"] == change["s1"]


def test_c3_refresh_between_machine_verification_and_km_acceptance_also_stales(
        setup, monkeypatch, owner, tmp_path):
    from tests.ai8t04.conftest import machine_verified_change
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path)
    store, tenant, ws = setup["store"], setup["tenant"], change["workspace"]
    publish_with_chunk(store, tenant, ws, artifact_id="refresh-import.yaml", text="fresh: 1\n")
    dto = change_dto(change["client"], ws, change["apply_job_id"])
    assert dto["state"] == "STALE", dto
    # the old candidate is not promotable against the refreshed head (reviewed guard)
    head = store.head(tenant, ws)
    candidate = dto["knowledge"]["candidate_revision_id"] or "km-cand-stale"
    resp = owner["client"].post("/v2/knowledge/accept", json={
        "workspace_id": ws, "knowledge_revision_id": candidate,
        "source_revision_id": change["s1"], "decision_epoch": head.decision_epoch,
        "expected_version": head.version, "idempotency_key": "stale-accept"})
    assert resp.status_code == 409, resp.text
    resp = submit_result_decision(approver_client(change), ws, change["apply_job_id"],
                                  digest=report_digest(change, setup))
    assert resp.status_code == 409
    assert "report_stale" in resp.text or "knowledge_not_accepted" in resp.text


def _export(client, ws, revision, artifact_id) -> bytes:
    resp = client.get(f"/v2/sources/revisions/{revision}/artifacts/{artifact_id}/export",
                      params={"workspace_id": ws})
    assert resp.status_code == 200, resp.text
    return base64.b64decode(resp.json()["content_b64"])


def test_c3_second_change_reads_actual_s1_k1_and_completes_at_s2_k2(
        setup, monkeypatch, owner, tmp_path):
    from infra import v2_source_routes
    monkeypatch.setattr(v2_source_routes, "_blob_port", lambda: setup["store"].blobs)
    store, tenant = setup["store"], setup["tenant"]

    # ---- change 1: S0 (14) -> S1 (30), K1 accepted, result accepted
    c1 = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws = c1["workspace"]
    d1 = report_digest(c1, setup)
    r1 = submit_result_decision(approver_client(c1), ws, c1["apply_job_id"], digest=d1)
    assert r1.status_code == 200, r1.text
    s1, k1, target = c1["s1"], c1["km"], c1["target"]
    head1 = store.head(tenant, ws)
    assert (head1.source_revision_id, head1.knowledge_revision_id) == (s1, k1)
    s1_blob = store.read_artifact_blob(tenant, ws, s1, target)
    assert s1_blob["content"] == b"refund_window_days: 30\n"
    assert change_dto(c1["client"], ws, c1["apply_job_id"])["lineage"]["verified_version"] == 1

    # ---- a proposal that still believes S0 (14) is current cannot be built on S1
    stale = build_second_change(setup, monkeypatch, c1, expected_current=14, proposed=45,
                                apply=False, worker_prefix="c3-stale")
    assert stale["proposal_body"]["execution_status"] == "FAILED", stale["proposal_body"]

    # ---- change 2 from the ACTUAL S1/K1
    write_oracle(tmp_path, monkeypatch, artifact_id=target, expected=45)
    c2 = build_second_change(setup, monkeypatch, c1, expected_current=30, proposed=45)
    assert c2["apply_body"]["execution_status"] == "COMPLETED", c2["apply_body"]
    prop = job_row(store, tenant, ws, c2["proposal_job_id"])
    assert pinned(store, tenant, ws, c2["proposal_job_id"])[:2] == (s1, k1), \
        "next proposal must be pinned to the actual S1/K1"
    assert prop["execution_result"]["before_blob_hash"] == s1_blob["blob_hash"]
    assert prop["execution_result"]["edits"][0]["before"] == 30
    applied = c2["apply_body"]["execution_result"]
    assert applied["before_blob_hash"] == s1_blob["blob_hash"], "apply read S1, not a cached S0"
    s2 = c2["s_after"]
    assert store.source_revision_view(tenant, ws, s2)["parent_revision_id"] == s1

    # ---- verify + rebuild + accept KM + accept result for change 2
    pump(setup)
    dto2 = change_dto(c2["client"], ws, c2["apply_job_id"])
    assert dto2["state"] == "MACHINE_VERIFIED", dto2
    assert dto2["lineage"]["verified_version"] == 1, "apply / verification must not advance the counter"
    rep2 = report_of(c2, store, tenant)
    assert rep2["status"] == "PASSED" and rep2["parent_revision_id"] == s1
    assert rep2["actual_values"][target]["refund_window_days"] == 45
    # the byte readback is done by a member who may read source content (knowledge_owner); the change's requester may not (403)
    assert c2["client"].get(f"/v2/sources/revisions/{s2}/artifacts/{target}/export", params={"workspace_id": ws}).status_code == 403
    exported = _export(owner["client"], ws, s2, target)
    assert exported == b"refund_window_days: 45\n"
    assert rep2["artifacts"][target]["sha256"] == __import__("hashlib").sha256(exported).hexdigest()
    assert _export(owner["client"], ws, s1, target) == b"refund_window_days: 30\n", "S1 is immutable"

    k2 = accept_candidate_km(setup, owner, c2)
    r2 = submit_result_decision(approver_client(c2), ws, c2["apply_job_id"],
                                digest=rep2["report_digest"])
    assert r2.status_code == 200, r2.text

    head2 = store.head(tenant, ws)
    assert (head2.source_revision_id, head2.knowledge_revision_id) == (s2, k2) and k2 != k1
    revs = {r["revision_id"]: r for r in store.knowledge_revisions(tenant, ws)}
    assert revs[k1]["source_revision_id"] == s1 and revs[k2]["source_revision_id"] == s2

    lin2 = change_dto(c2["client"], ws, c2["apply_job_id"])["lineage"]
    assert lin2["verified_version"] == 2
    assert (lin2["chain"][0]["kind"], lin2["chain"][0]["ref"]) == ("source_before", s1)
    assert lin2["parent_source_revision_id"] == s1
    assert lin2["prior_knowledge_revision_id"] == k1
    assert lin2["knowledge_candidate_revision_id"] == k2

    # change 1 is history: still COMPLETED, same decision, same digest
    dto1 = change_dto(c1["client"], ws, c1["apply_job_id"])
    assert dto1["state"] == "COMPLETED"
    assert dto1["completion"]["decision"]["decision_id"] == r1.json()["decision_id"]
    assert dto1["verification"]["report_digest"] == d1


def test_c3_change_one_approval_cannot_be_replayed_after_change_two_moved_the_head(
        setup, monkeypatch, owner, tmp_path):
    """A cached S0-era approval cannot overwrite S2: the head-bound tuple
    still refuses it, so the sequential loop always reads the actual head."""
    from uuid import uuid4

    from tests.ai8t01.conftest import submit_claim_and_get_apply
    c1 = awaiting_change(setup, monkeypatch, owner, tmp_path)
    ws, store, tenant = c1["workspace"], setup["store"], setup["tenant"]
    assert submit_result_decision(approver_client(c1), ws, c1["apply_job_id"],
                                  digest=report_digest(c1, setup)).status_code == 200
    write_oracle(tmp_path, monkeypatch, artifact_id=c1["target"], expected=45)
    c2 = build_second_change(setup, monkeypatch, c1, expected_current=30, proposed=45)
    assert c2["apply_body"]["execution_status"] == "COMPLETED"
    # change 2's durable SOURCE_PUBLISHED notification is still pending and is
    # older than the replay's JOB_QUEUED event: let the production dispatcher
    # consume it first (a harness ordering fix; the replay assertions are unchanged)
    pump(setup)
    head = head_tuple(store, tenant, ws)
    body = submit_claim_and_get_apply(
        setup, c1["client"], ws, approval_job_id=c1["ready"]["approval_job_id"],
        job_id=f"job-apply-replay-{uuid4().hex}",
        expected_approval_digest=c1["ready"]["digest"], worker_id="c3-replay")
    assert body["execution_status"] == "FAILED", body
    assert "approval_stale" in (body.get("execution_reason") or ""), body
    assert head_tuple(store, tenant, ws) == head


# W1: a BLOCKED review can no longer be approved; this module drives pre-W1 journeys whose subject is
# downstream of the approval, so the review stage states a legacy fixture contract (tests/conftest.py).
import pytest as _w1_pytest  # noqa: E402
pytestmark = _w1_pytest.mark.usefixtures("legacy_contract_injection")
