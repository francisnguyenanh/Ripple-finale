"""Knowledge rebuild from the NEW source, AI-outage / index
failure / stale-source / duplicate / crash / restart recovery.

Requirements covered: set old KM STALE; re-ingest changed artifacts (a new
source creates new observations and never mutates the old IR); persist the new
KM candidate and index rebuild; carry decisions only when scope, evidence and
dependencies are still valid; stale worker results never promote the head; an AI
outage after publish keeps REANALYSING/FAILED and retries without losing the
source commit; a knowledge/index failure keeps the source published but blocks
completion.

All scenarios: real apply gate -> durable SOURCE_PUBLISHED outbox row ->
`job_worker.claim_and_admit_one` (the production dispatcher) -> SQL / API reads.
Failures are injected at the provider seam (`extraction._client_for_model`,
the same seam the extraction tests use), the store seam (class-level monkeypatch), or as an
extra durable outbox row / dead worker.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from infra.adapters import v2_postgres
from infra.worker import job_worker
from tests.ai4t01.conftest import publish_with_chunk
from tests.ai8t03.conftest import (
    applied_change, assert_head_unmoved, change_dto, count_jobs, exceptions_of,
    head_tuple, install_provider, job_row, login, outbox_rows, publish_footprint, pump,
    report_of, restarted, source_published_event, write_oracle,
)

Store = v2_postgres.PostgresV2Store
NOT_DONE = ("MACHINE_VERIFIED", "AWAITING_ACCEPTANCE", "COMPLETED")


def _run(setup, monkeypatch, owner, tmp_path, *, provider="ok", oracle=30, **kw):
    change = applied_change(setup, monkeypatch, owner, **kw)
    if oracle is not None:
        write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=oracle)
    change["provider"] = install_provider(monkeypatch, provider)
    return change


def _reanalysis_jobs(store, tenant, workspace, apply_job_id):
    with store._tx() as cur:
        cur.execute("SELECT job_id, execution_status, pinned_source_revision_id, payload "
                    "FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' "
                    "AND payload->>'caused_by_apply_job_id'=%s ORDER BY job_id",
                    (tenant, workspace, apply_job_id))
        return cur.fetchall()


# ------------------------------------------------------------------ Z3: new source -> new observations, old IR untouched

def test_z3_rebuild_records_new_observations_under_new_revision_old_ir_untouched(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    s0_before = store.all_source_observations(tenant, workspace, change["s0"])
    assert s0_before, "fixture must leave S0 observations (the immutable old IR)"
    assert store.all_source_observations(tenant, workspace, change["s1"]) == []
    log = pump(setup)
    assert not exceptions_of(log), log

    # C01: the old revision's observations are byte-for-byte what they were.
    assert store.all_source_observations(tenant, workspace, change["s0"]) == s0_before
    # New observations exist under the NEW revision and read the ACTUAL new value.
    new = [o for o in store.all_source_observations(tenant, workspace, change["s1"])
           if o["artifact_id"] == change["target"]]
    assert new, "re-ingest of the changed artifact must record observations for S1"
    blob = json.dumps([o["raw_value"] for o in new])
    assert "30" in blob and "14" not in blob, blob

    # Index rebuilt for S1 and retrieval returns the NEW evidence (no stale vector).
    status = store.retrieval_status(tenant, workspace)
    assert status["source_revision_id"] == change["s1"] and status["status"] == "READY", status
    res = store.retrieve(tenant, workspace, "refund_window_days", top_k=10)
    hits = [r for r in res["results"] if r["artifact_id"] == change["target"]]
    assert hits and all("14" not in json.dumps(r["raw_value"]) for r in hits), hits
    s1_blob = {a["artifact_id"]: a["blob_hash"] for a in
               store.source_revision_view(tenant, workspace, change["s1"])["artifacts"]}
    assert all(r["blob_hash"] == s1_blob[change["target"]] for r in hits)
    assert_head_unmoved(setup, change)


def test_z3_old_km_is_stale_and_new_km_is_only_a_pending_candidate(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    before_revisions = store.knowledge_revisions(tenant, workspace)
    pump(setup)
    # workspace head: no current KM (the pre-apply KM is stale) and none promoted
    cur = change["client"].get("/v2/knowledge/current", params={"workspace_id": workspace}).json()
    assert cur["knowledge_revision_id"] is None
    assert cur["source_revision_id"] == change["s1"]
    assert store.knowledge_revisions(tenant, workspace) == before_revisions, \
        "rebuild must not write an accepted knowledge revision"
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] == "MACHINE_VERIFIED", dto
    k = dto["knowledge"]
    assert k["status"] == "PENDING_HUMAN" and k["accepted"] is False
    assert isinstance(k["candidate_revision_id"], str) and k["candidate_revision_id"]
    assert k["candidate_revision_id"] not in {r["revision_id"] for r in before_revisions}
    assert k["retrieval_index_status"] == "READY"
    assert dto["reanalysis"]["status"] == "COMPLETED"
    assert dto["lineage"]["source_revision_id"] == change["s1"]
    assert dto["lineage"]["parent_source_revision_id"] == change["s0"]
    assert dto["lineage"]["knowledge_candidate_revision_id"] == k["candidate_revision_id"]
    assert_head_unmoved(setup, change)


def test_z3_full_reanalysis_fallback_when_dependency_coverage_is_missing(
        setup, monkeypatch, owner, tmp_path):
    siblings = {"sibling-dep.yaml": "dep_value: 7\n"}
    change = _run(setup, monkeypatch, owner, tmp_path, siblings=siblings)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    pump(setup)
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["reanalysis"]["scope"] == "FULL", dto["reanalysis"]
    assert "dependency_coverage" in (dto["reanalysis"].get("reason") or ""), dto["reanalysis"]
    rows = _reanalysis_jobs(store, tenant, workspace, change["apply_job_id"])
    assert {r[3]["artifact_id"] for r in rows} == {change["target"], "sibling-dep.yaml"}
    assert all(r[2] == change["s1"] for r in rows), "reanalysis must run on the NEW source"
    assert all(r[1] == "COMPLETED" for r in rows)
    assert sorted(dto["reanalysis"]["job_ids"]) == sorted(r[0] for r in rows)


def test_z3_carry_forward_reopens_a_decision_whose_evidence_binding_is_not_valid(
        setup, monkeypatch, owner, tmp_path):
    """The fixture chain records exactly one real human decision (epoch 1) whose
    evidence lives in an artifact that is NOT part of the applied revision. A
    decision is carried forward only when its evidence binding still validates on
    the new source; here it cannot, so it must be REOPENED with a reason (never
    silently carried, never silently dropped), and carrying/reopening must not
    write a new decision (epoch unchanged)."""
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    with store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s",
                    (tenant, workspace))
        decision_ids = [r[0] for r in cur.fetchall()]
    assert decision_ids, "fixture must carry at least one real human decision"
    epoch_before = store.head(tenant, workspace).decision_epoch
    pump(setup)
    result = job_row(store, tenant, workspace, change["rebuild_job_id"])["execution_result"]
    carried = {c["decision_id"] for c in result["carry_forward"]["carried"]}
    reopened = {c["decision_id"]: c for c in result["carry_forward"]["reopened"]}
    for did in decision_ids:
        assert (did in carried) != (did in reopened), (did, carried, reopened)
    assert not (carried & set(decision_ids)), "binding is invalid: nothing may be carried"
    assert all(reopened[d]["reason"] for d in decision_ids)
    assert store.head(tenant, workspace).decision_epoch == epoch_before
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s",
                    (tenant, workspace))
        assert cur.fetchone()[0] == len(decision_ids), "carry-forward must not mint decisions"


def test_z3_affected_claims_are_invalidated_through_the_existing_invalidation_path(
        setup, monkeypatch, owner, tmp_path):
    """Invalidate affected claims/edges and retrieve related counterevidence.
    A finding built over a candidate extracted from the changed artifact is
    a dependent of that candidate (dependency scan). The rebuild must route it
    through the EXISTING invalidation executor (decision id `apply-<apply_job_id>`):
    the dependent is marked stale, a deduplicated reanalysis of the same kind is
    scheduled, and the causation chain is recorded -- without writing a decision
    (epoch unchanged) or touching the old job's result."""
    from tests.ai4t02.conftest import seed_extraction_candidates
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    target = f"config-t03dep-{uuid4().hex[:6]}.yaml"
    body = seed_extraction_candidates(
        setup, monkeypatch, artifact_id=target, text="refund_window_days: 14\n",
        candidates_json=[{"subject": "refund", "predicate": "window_days", "value": 14,
                          "modality": "must", "scope": {"category": "refund"},
                          "quotes": ["refund_window_days: 14"]}])
    candidate = body["execution_result"]["candidates"][0]["candidate_id"]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    finding_id = f"job-finding-{uuid4().hex}"
    assert client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "finding", "job_id": finding_id,
        "payload": {"candidate_ids": [candidate]}}).status_code == 200
    job_worker.claim_and_admit_one(store, tenant, workspace, "dep-finding-worker")
    old_result = job_row(store, tenant, workspace, finding_id)["execution_result"]
    assert job_row(store, tenant, workspace, finding_id)["execution_status"] == "COMPLETED"

    change = _run(setup, monkeypatch, owner, tmp_path, target=target)
    epoch = store.head(tenant, workspace).decision_epoch
    assert store.is_job_stale(tenant, workspace, finding_id) is False
    log = pump(setup)
    assert not exceptions_of(log), log
    decision = f"apply-{change['apply_job_id']}"
    assert store.is_job_stale(tenant, workspace, finding_id) is True
    caused = store.causation_for_decision(tenant, workspace, decision)
    assert finding_id in caused, caused
    reanalysis = job_row(store, tenant, workspace, caused[finding_id])
    assert reanalysis["kind"] == "finding" and reanalysis["execution_status"] == "COMPLETED"
    # history is not rewritten and no decision was minted
    assert job_row(store, tenant, workspace, finding_id)["execution_result"] == old_result
    assert store.head(tenant, workspace).decision_epoch == epoch
    result = job_row(store, tenant, workspace, change["rebuild_job_id"])["execution_result"]
    assert finding_id in json.dumps(result["invalidation"]), result.get("invalidation")
    # counterevidence is read from the NEW source's index only
    s1_hashes = {a["blob_hash"] for a in
                 store.source_revision_view(tenant, workspace, change["s1"])["artifacts"]}
    assert isinstance(result["counterevidence"], list)
    assert all(c["blob_hash"] in s1_hashes for c in result["counterevidence"])


def test_z3_rebuild_proceeds_when_the_report_fails_and_report_survives_when_rebuild_fails(
        setup, monkeypatch, owner, tmp_path):
    """The two stages are independent: a FAIL report does not stop the source
    truth from being re-ingested; and (next test) a failed rebuild never
    destroys/rewrites the report."""
    change = _run(setup, monkeypatch, owner, tmp_path, oracle=45)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    pump(setup)
    assert report_of(change, store, tenant)["status"] == "FAILED"
    assert job_row(store, tenant, workspace, change["rebuild_job_id"])["execution_status"] == "COMPLETED"
    assert store.retrieval_status(tenant, workspace)["status"] == "READY"
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] not in NOT_DONE and dto["completion"]["eligible"] is False


# ------------------------------------------------------------------ Z2: AI outage after publish

def _outage_to_terminal(setup, monkeypatch, owner, tmp_path):
    """A published change whose re-analysis lost the provider: the worker made every approved provider attempt and the job is FAILED."""
    change = _run(setup, monkeypatch, owner, tmp_path, provider="outage")
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    footprint = publish_footprint(store, tenant, workspace)
    log = pump(setup)
    assert not exceptions_of(log), log
    return change, store, tenant, workspace, footprint


def _booked(store, tenant):
    with store._tx() as cur:
        cur.execute("SELECT count(*), COALESCE(SUM(cost_usd::numeric), 0) FROM v2_job_cost_ledger WHERE tenant_id=%s", (tenant,))
        ledger = cur.fetchone()
        cur.execute("SELECT count(*) FROM v2_job_attempts WHERE tenant_id=%s", (tenant,))
        return ledger, cur.fetchone()[0]


def test_z2_provider_outage_after_publish_keeps_source_blocks_completion_and_exhausted_attempts_are_terminal(
        setup, monkeypatch, owner, tmp_path):
    """Owner decision (Option A): a re-analysis that used every approved provider attempt is TERMINAL. This test was first written to expect that a retry,
    once the provider is back, ends in MACHINE_VERIFIED; the attempt limit is a deliberate budget guard, so the contract is now: the source stays as
    applied, the report stays, the change cannot complete, the retry is refused AT THE ROUTE (no queue, no reservation, no provider call, no booking)
    and the read model says so."""
    change, store, tenant, workspace, footprint = _outage_to_terminal(setup, monkeypatch, owner, tmp_path)

    # source commit is untouched; the machine report (bytes) does not depend on AI
    assert store.head(tenant, workspace).source_revision_id == change["s1"]
    assert publish_footprint(store, tenant, workspace) == footprint
    assert report_of(change, store, tenant)["status"] == "PASSED"
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] in ("REANALYSING", "FAILED"), dto
    assert dto["reanalysis"]["status"] == "FAILED"
    assert dto["knowledge"]["candidate_revision_id"] is None
    assert dto["completion"]["eligible"] is False
    assert "reanalysis" in " ".join(dto["completion"]["blocked_reasons"]).lower()
    job_ids = dto["reanalysis"]["job_ids"]
    assert job_ids and all(job_row(store, tenant, workspace, j)["execution_status"] == "FAILED" for j in job_ids)
    assert_head_unmoved(setup, change)

    # the read model distinguishes a failed job that can still be retried from one that has no attempt left
    re = dto["reanalysis"]
    assert re["terminal"] is True and re["exhausted_job_ids"] == job_ids and re["retryable_job_ids"] == [], re
    assert re["attempt_limit"] and re["attempt_limit"] >= 1
    detail = change["client"].get(f"/v2/jobs/{job_ids[0]}", params={"workspace_id": workspace}).json()
    assert detail["retry"]["allowed"] is False and detail["retry"]["attempts_used"] >= detail["retry"]["attempt_limit"], detail.get("retry")

    # the provider is back, but the approved attempts are not reset: the retry is refused AT ONCE with a reason; nothing is queued, reserved, called or booked
    change["provider"].mode = "ok"
    calls_before, booked_before = len(change["provider"].calls), _booked(store, tenant)
    queued_before = len(outbox_rows(store, tenant, workspace, "JOB_QUEUED"))
    for jid in job_ids:
        resp = change["client"].post(f"/v2/jobs/{jid}/retry", json={"workspace_id": workspace})
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "provider_attempts_exhausted" and body["attempts_used"] >= body["attempt_limit"], body
        assert job_row(store, tenant, workspace, jid)["execution_status"] == "FAILED"
    pump(setup)
    assert len(outbox_rows(store, tenant, workspace, "JOB_QUEUED")) == queued_before, "a refused retry queues nothing"
    assert len(change["provider"].calls) == calls_before, "no provider call"
    assert _booked(store, tenant) == booked_before, "no new attempt, reservation or cost"

    # completion and acceptance stay blocked; the published source and the verification report are untouched
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["reanalysis"]["terminal"] is True and dto["completion"]["eligible"] is False
    assert dto["knowledge"]["candidate_revision_id"] is None
    assert report_of(change, store, tenant)["status"] == "PASSED"
    assert publish_footprint(store, tenant, workspace) == footprint
    assert_head_unmoved(setup, change)
    approver = change["ready"]["approver"]["client"]
    digest = dto["verification"]["report_digest"]
    refused = approver.post(f"/v2/changes/{change['apply_job_id']}/result-decision",
                            json={"workspace_id": workspace, "action": "accept", "reason": "attempt", "expected_report_digest": digest})
    assert refused.status_code == 409 and "reanalysis_incomplete" in refused.text, refused.text


def test_z2_a_failed_reanalysis_with_attempts_left_is_still_retried_to_machine_verified(setup, monkeypatch, owner, tmp_path):
    """The retry that already existed is kept for the case it can serve: a FAILED job that still has a provider attempt left. (The state is built from the
    outage by removing the record of one attempt, because the stub provider otherwise always spends both.)"""
    change, store, tenant, workspace, footprint = _outage_to_terminal(setup, monkeypatch, owner, tmp_path)
    job_ids = change_dto(change["client"], workspace, change["apply_job_id"])["reanalysis"]["job_ids"]
    with store._tx() as cur:
        cur.execute("""DELETE FROM v2_job_attempts WHERE ctid IN (SELECT ctid FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s
                          AND provider_calls > 0 ORDER BY attempt_key LIMIT 1)""", (tenant, workspace, job_ids[0]))
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["reanalysis"]["terminal"] is False and dto["reanalysis"]["retryable_job_ids"] == job_ids, dto["reanalysis"]
    assert change["client"].get(f"/v2/jobs/{job_ids[0]}", params={"workspace_id": workspace}).json()["retry"]["allowed"] is True
    change["provider"].mode = "ok"
    for jid in job_ids:
        resp = change["client"].post(f"/v2/jobs/{jid}/retry", json={"workspace_id": workspace})
        assert resp.status_code == 200, resp.text
    pump(setup)
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] == "MACHINE_VERIFIED", dto
    assert dto["reanalysis"]["status"] == "COMPLETED" and dto["reanalysis"]["terminal"] is False
    assert dto["knowledge"]["candidate_revision_id"]
    assert count_jobs(store, tenant, workspace, "verification") == 1
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 1
    assert publish_footprint(store, tenant, workspace) == footprint
    assert_head_unmoved(setup, change)


@pytest.mark.parametrize("action", ["request_changes", "reject"])
def test_z2_after_a_terminal_reanalysis_a_person_with_the_result_permission_can_request_changes_or_reject(setup, monkeypatch, owner, tmp_path, action):
    change, store, tenant, workspace, footprint = _outage_to_terminal(setup, monkeypatch, owner, tmp_path)
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["reanalysis"]["terminal"] is True
    body = {"workspace_id": workspace, "action": action, "reason": "the AI re-analysis could not be completed",
            "expected_report_digest": dto["verification"]["report_digest"]}
    path = f"/v2/changes/{change['apply_job_id']}/result-decision"
    calls_before, booked_before = len(change["provider"].calls), _booked(store, tenant)
    assert change["client"].post(path, json=body).status_code == 403, "the requester holds no result permission"
    ok = change["ready"]["approver"]["client"].post(path, json=body)
    assert ok.status_code == 200, ok.text
    assert (ok.json().get("new_proposal_job_id") is not None) == (action == "request_changes"), ok.text
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["completion"]["decision"]["action"] == action and dto["completion"]["eligible"] is False
    assert dto["knowledge"]["candidate_revision_id"] is None
    assert report_of(change, store, tenant)["status"] == "PASSED"
    assert len(change["provider"].calls) == calls_before and _booked(store, tenant) == booked_before, "a human decision calls no provider and books nothing"
    assert_head_unmoved(setup, change)


def test_z2_retrieval_index_unavailable_keeps_source_blocks_completion_and_recovers(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    footprint = publish_footprint(store, tenant, workspace)

    def boom(self, *a, **k):
        raise RuntimeError("index backend unavailable")

    with monkeypatch.context() as mp:
        mp.setattr(Store, "build_retrieval_index", boom)
        log = pump(setup)
    assert not exceptions_of(log), ("a deterministic index failure must end the job "
                                    f"FAILED, not escape the dispatcher: {log}")
    assert store.head(tenant, workspace).source_revision_id == change["s1"]
    assert publish_footprint(store, tenant, workspace) == footprint
    assert report_of(change, store, tenant)["status"] == "PASSED", "the report must survive a failed rebuild"
    rebuild = job_row(store, tenant, workspace, change["rebuild_job_id"])
    assert rebuild["execution_status"] == "FAILED" and rebuild["execution_reason"]
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] not in NOT_DONE
    assert dto["knowledge"]["retrieval_index_status"] != "READY"
    assert dto["knowledge"]["candidate_revision_id"] is None
    assert dto["completion"]["eligible"] is False
    assert "index" in " ".join(dto["completion"]["blocked_reasons"]).lower()
    # no stale vector is served as current
    res = store.retrieve(tenant, workspace, "refund_window_days")
    assert res["status"] != "READY" and res["results"] == []
    assert_head_unmoved(setup, change)

    approver_client = change["ready"]["approver"]["client"]
    resp = approver_client.post(f"/v2/jobs/{change['rebuild_job_id']}/retry",
                                json={"workspace_id": workspace})
    assert resp.status_code == 200, resp.text
    pump(setup)
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] == "MACHINE_VERIFIED", dto
    assert dto["knowledge"]["retrieval_index_status"] == "READY"
    assert_head_unmoved(setup, change)


@pytest.mark.parametrize("claims_before", [0, 3], ids=["before-rebuild", "mid-reanalysis"])
def test_z2_newer_source_between_reanalysis_supersedes_and_never_promotes(
        setup, monkeypatch, owner, tmp_path, claims_before):
    """Another source publish lands before the reanalysis finished. The stale
    change may keep its report as history but must be STALE: no KM candidate to
    promote, no completion, and the worker never touches the newer head."""
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    if claims_before:
        pump(setup, max_claims=claims_before)
    publish_with_chunk(store, tenant, workspace, artifact_id="newer-import.yaml",
                       text="newer: 1\n")
    newer = head_tuple(store, tenant, workspace)
    log = pump(setup)
    assert not exceptions_of(log), log
    assert head_tuple(store, tenant, workspace) == newer, "a stale worker moved the newer head"
    report = report_of(change, store, tenant)
    assert report["after_source_revision_id"] == change["s1"], "report stays pinned to the change's own revision"
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] == "STALE", dto
    assert dto["knowledge"]["status"] == "STALE"
    assert dto["knowledge"]["accepted"] is False
    assert dto["completion"]["status"] == "STALE" and dto["completion"]["eligible"] is False
    if claims_before == 0:
        assert _reanalysis_jobs(store, tenant, workspace, change["apply_job_id"]) == [], \
            "no reanalysis may be scheduled against a superseded source"


# ------------------------------------------------------------------ Z2: duplicate / crash / restart

def test_z2_duplicate_source_published_row_creates_one_job_set(setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    event = source_published_event(store, tenant, workspace, change["apply_job_id"])
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_outbox (tenant_id, workspace_id, kind, payload) "
                    "SELECT tenant_id, workspace_id, kind, payload FROM v2_outbox WHERE id=%s",
                    (event[0],))
    pump(setup)
    rows = [r for r in outbox_rows(store, tenant, workspace, "SOURCE_PUBLISHED")
            if isinstance(r[3], dict) and r[3].get("job_id") == change["apply_job_id"]]
    assert len(rows) == 2 and all(r[2] == "DONE" for r in rows), rows
    assert count_jobs(store, tenant, workspace, "verification") == 1
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 1
    assert len(_reanalysis_jobs(store, tenant, workspace, change["apply_job_id"])) == 1
    assert len(store.job_attempts(tenant, workspace, change["verify_job_id"])) == 1
    assert change_dto(change["client"], workspace, change["apply_job_id"])["state"] == "MACHINE_VERIFIED"
    assert_head_unmoved(setup, change)


def test_z2_worker_dies_after_creating_jobs_before_ack_loses_nothing(
        setup, monkeypatch, owner, tmp_path):
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    event = source_published_event(store, tenant, workspace, change["apply_job_id"])
    original = Store.ack_outbox

    class Died(BaseException):
        pass

    def _die(self, t, w, event_id, *a, **k):
        if event_id == event[0]:
            raise Died()
        return original(self, t, w, event_id, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(Store, "ack_outbox", _die)
        try:
            job_worker.claim_and_admit_one(store, tenant, workspace, "dying-consumer",
                                           lease_seconds=30)
        except Died:
            pass
    # Nothing is lost: the rebuild intent is either still queued (CLAIMED/PENDING)
    # or fully consumed with its jobs present -- never "consumed but jobs missing".
    status = store.outbox_status(tenant, workspace, event[0])
    jobs = (count_jobs(store, tenant, workspace, "verification"),
            count_jobs(store, tenant, workspace, "km_rebuild"))
    assert status in ("CLAIMED", "PENDING") or jobs == (1, 1), (status, jobs)
    if status != "DONE":
        with store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                        "WHERE id=%s AND status='CLAIMED'", (event[0],))
    pump(setup)
    assert store.outbox_status(tenant, workspace, event[0]) == "DONE"
    assert count_jobs(store, tenant, workspace, "verification") == 1
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 1
    assert change_dto(change["client"], workspace, change["apply_job_id"])["state"] == "MACHINE_VERIFIED"
    assert_head_unmoved(setup, change)


@pytest.mark.parametrize("claims_before_restart", [0, 1, 2, 3])
def test_z2_restart_at_every_boundary_reaches_the_same_final_state(
        setup, monkeypatch, owner, tmp_path, claims_before_restart):
    """Boundaries: 0 = right after the apply commit, 1 = event consumed / jobs
    queued, 2 = one stage job done, 3 = report + rebuild done, reanalysis queued.
    A fresh store object (process restart) must finish the same work, once."""
    change = _run(setup, monkeypatch, owner, tmp_path)
    tenant, workspace = setup["tenant"], setup["workspace"]
    if claims_before_restart:
        pump(setup, max_claims=claims_before_restart)
        mid = change_dto(change["client"], workspace, change["apply_job_id"])
        assert mid["state"] != "MACHINE_VERIFIED", "a half-run change must not look verified"
    fresh = restarted(setup)
    log = pump(fresh)
    assert not exceptions_of(log), log
    store = fresh["store"]
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] == "MACHINE_VERIFIED", dto
    assert report_of(change, store, tenant)["status"] == "PASSED"
    assert count_jobs(store, tenant, workspace, "verification") == 1
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 1
    assert len(_reanalysis_jobs(store, tenant, workspace, change["apply_job_id"])) == 1
    assert source_published_event(store, tenant, workspace, change["apply_job_id"])[2] == "DONE"
    assert_head_unmoved(fresh, change)


def test_z2_report_and_candidate_are_durable_across_a_fresh_session_and_store(
        setup, monkeypatch, owner, tmp_path):
    from tests.ai8t03.conftest import login
    change = _run(setup, monkeypatch, owner, tmp_path)
    pump(setup)
    first = change_dto(change["client"], setup["workspace"], change["apply_job_id"])
    fresh_client = setup["browser"]()
    login(fresh_client, setup["requester"], setup["credential"])
    second = change_dto(fresh_client, setup["workspace"], change["apply_job_id"])
    assert second["verification"] == first["verification"]
    assert second["knowledge"]["candidate_revision_id"] == first["knowledge"]["candidate_revision_id"]
    fresh = restarted(setup)
    assert report_of(change, fresh["store"], setup["tenant"])["report_digest"] == \
        first["verification"]["report_digest"]


# ------------------------------------------------------------------ shared dispatcher

def test_z2_dispatcher_ignores_non_apply_source_published_and_still_runs_other_kinds(
        setup, monkeypatch, owner):
    """The SOURCE_PUBLISHED branch added to the shared dispatcher must
    (a) do nothing for a non-apply publish and (b) not disturb other executors."""
    from infra.worker.job_worker import ExecutionOutcome, ExecutionRequest

    class Probe:
        executor_id = "t03.probe.v1"
        external = False

        def prepare(self, ctx):
            return ExecutionRequest(prompt=None, schema=None, provider_calls_planned=0,
                                    external=False)

        def execute(self, ctx, request, calls):
            return ExecutionOutcome(result={"probe": "ok"})

    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    monkeypatch.setitem(job_worker.PROVIDER_EXECUTORS, "t03probe", Probe())
    publish_with_chunk(store, tenant, workspace, artifact_id="plain-import.yaml", text="a: 1\n")
    job_id = f"job-probe-{uuid4().hex}"
    store.create_job(tenant, workspace, job_id, "t03probe", {},
                     expected_version=store.head(tenant, workspace).version,
                     idempotency_key=f"create-{job_id}")
    log = pump(setup)
    assert not exceptions_of(log), log
    assert job_row(store, tenant, workspace, job_id)["execution_status"] == "COMPLETED"
    for kind in ("verification", "km_rebuild"):
        assert count_jobs(store, tenant, workspace, kind) == 0, kind


def test_z2_stale_late_worker_cannot_produce_a_second_or_conflicting_report(
        setup, monkeypatch, owner, tmp_path):
    """Lease/fencing: the verification executor runs after its
    claim's lease has been stolen. Whatever the shared runner does with the stale
    result, the end state has exactly ONE report, no head movement, all events
    acked, and a later worker/pump changes nothing."""
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    real = job_worker.BUILTIN_EXECUTORS["verification"]

    class LeaseThief:
        executor_id = real.executor_id
        external = real.external

        def prepare(self, ctx):
            return real.prepare(ctx)

        def execute(self, ctx, request, calls):
            with store._tx() as cur:
                cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                            "WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED' "
                            "AND status='CLAIMED'", (tenant, workspace))
            return real.execute(ctx, request, calls)

    with monkeypatch.context() as mp:
        mp.setitem(job_worker.BUILTIN_EXECUTORS, "verification", LeaseThief())
        pump(setup)
    pump(setup)  # the rightful next owner
    rec = job_row(store, tenant, workspace, change["verify_job_id"])
    assert rec["execution_status"] == "COMPLETED"
    digest = rec["execution_result"]["report_digest"]
    pump(setup)
    assert job_row(store, tenant, workspace, change["verify_job_id"])["execution_result"]["report_digest"] == digest
    assert count_jobs(store, tenant, workspace, "verification") == 1
    assert all(r[2] == "DONE" for r in outbox_rows(store, tenant, workspace)), outbox_rows(store, tenant, workspace)
    assert_head_unmoved(setup, change)


@pytest.mark.parametrize("when", ["before-finish", "after-finish"])
@pytest.mark.parametrize("stage", ["verify", "rebuild"])
def test_z2_process_death_around_the_terminal_write_recovers_without_duplicates(
        setup, monkeypatch, owner, tmp_path, stage, when):
    """Crash recovery for the new executors: kill the worker (BaseException) right
    before / right after the terminal write of the verification or rebuild job.
    The reclaimed retry must finish the work exactly once: one report, one
    reanalysis job, no duplicated observations, no head movement."""
    change = _run(setup, monkeypatch, owner, tmp_path)
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    victim = change["verify_job_id"] if stage == "verify" else change["rebuild_job_id"]
    original = Store.finish_execution
    fired = {"n": 0}

    class Died(BaseException):
        pass

    def _wrapped(self, t, w, job_id, *a, **k):
        if job_id == victim and fired["n"] == 0:
            fired["n"] += 1
            if when == "before-finish":
                raise Died()
            original(self, t, w, job_id, *a, **k)
            raise Died()
        return original(self, t, w, job_id, *a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(Store, "finish_execution", _wrapped)
        try:
            pump(setup)
        except Died:
            pass
    assert fired["n"] == 1, "the victim job never reached its terminal write"
    obs_after_death = len(store.all_source_observations(tenant, workspace, change["s1"]))
    with store._tx() as cur:
        cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                    "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'",
                    (tenant, workspace))
    log = pump(setup)
    assert not exceptions_of(log), log
    assert count_jobs(store, tenant, workspace, "verification") == 1
    assert count_jobs(store, tenant, workspace, "km_rebuild") == 1
    assert len(_reanalysis_jobs(store, tenant, workspace, change["apply_job_id"])) == 1
    if stage == "rebuild":
        # observations were already recorded before the death: a re-run must not duplicate them
        assert obs_after_death > 0
        assert len(store.all_source_observations(tenant, workspace, change["s1"])) == obs_after_death
    dto = change_dto(change["client"], workspace, change["apply_job_id"])
    assert dto["state"] == "MACHINE_VERIFIED", dto
    assert report_of(change, store, tenant)["status"] == "PASSED"
    assert all(r[2] == "DONE" for r in outbox_rows(store, tenant, workspace))
    assert_head_unmoved(setup, change)
