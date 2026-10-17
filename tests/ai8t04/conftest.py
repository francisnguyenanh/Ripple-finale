"""Fixtures for human result acceptance and closed-loop completion.

Result acceptance consumes the verification contract documented in
`tests/ai8t03/conftest.py` (verification report as the
`verify-<apply_job_id>` job result, `km_rebuild`, `GET /v2/changes/<id>`); this
docstring lists only what acceptance adds.

Result-acceptance contract:

* `POST /v2/changes/{apply_job_id}/result-decision`
    body `{workspace_id, action: accept|request_changes|reject, reason,
    expected_report_digest, idempotency_key?}`; permission
    `change.accept_result` + human actor (`require_human_actor`); the actor,
    tenant and roles come from the session only. Registered in
    `infra/adapters/v2_policy.py::ROUTE_PERMISSIONS` (the route registry test
    `tests/security/test_ai2_t02_permission.py::test_route_registry_matches_app`
    walks it).
    200 `{decision_id, action, apply_job_id, report_digest, replayed, state,
    new_proposal_job_id?}`; 400 malformed (missing digest, bad action, empty
    reason); 401 no session; 403 permission / non-human actor; 404 unknown
    change; 409 `{error, reasons:[codes]}` when the completion gate refuses:
      report_missing | report_digest_mismatch | report_stale |
      machine_verification_not_pass | manual_work_unproven |
      reanalysis_incomplete | knowledge_not_accepted | index_not_ready |
      critical_gap_open (unresolved HIGH/CRITICAL finding involving a candidate
      extracted from a changed artifact; unrelated gaps never block) |
      already_decided
  `accept` runs the gate and is refused with EVERY failing code listed; body
  fields such as `override`, `force`, `report_status` are ignored. `request_
  changes` needs only an existing report; it creates (in the SAME transaction
  as the decision row, like `decide_job`) a NEW `proposal` job
  (`payload.action="revise"`, `previous_proposal_job_id` = the applied
  proposal) pinned to the CURRENT head and never edits the report.
  Idempotency: same `(apply_job_id, action, report_digest)` (any idempotency
  key) replays the first decision (`replayed=true`); a different terminal
  action after a decision is `already_decided`.
* `POST /v2/changes/{apply_job_id}/work-proof`
    body `{workspace_id, work_index:int, evidence_ref:str}`; permission
    `change.accept_result`; durable, idempotent per index; the gate treats a
    proven manual item as satisfied while the (immutable) report keeps
    `proof=None`.
* Completion effects (one SQL transaction): decision row (report digest, actor,
  action), audit row `CHANGE_RESULT_DECISION_RECORDED`, and for `accept` one
  outbox row `CHANGE_COMPLETED` (payload `apply_job_id`, `decision_id`,
  `report_digest`, `published_revision_id`, `knowledge_revision_id`,
  `actor_id`). It NEVER moves the workspace head (`version`,
  `source_revision_id`, `knowledge_revision_id`, `decision_epoch`) and never
  writes a source/KM revision or a semantic decision (`v2_decisions`).
* `GET /v2/changes/{apply_job_id}` additionally exposes `completion.decision`
  ({decision_id, action, actor_id, report_digest, reason}), `manual_work[i]
  .proof`, `alignment {global, scope, residual}` (never `global=="ALIGNED"`),
  `result_view {expected, actual, simulation{before,after}, conflicts
  {resolved,residual}, scope, failed_checks, knowledge_delta}` and `lineage
  {chain:[{kind, ref}], source_revision_id, parent_source_revision_id,
  knowledge_candidate_revision_id, prior_knowledge_revision_id,
  verified_version}` where `verified_version` is the legacy verified-lineage
  counter semantics adapted to v2 (`core/change_intelligence/lineage.py`,
  `tests/contract/test_lineage_version.py`): it advances by exactly one when a
  change is COMPLETED on a snapshot not already verified, and is UNCHANGED by
  apply, approval, retry / replayed attestation, request_changes, stale
  reports and refreshes. The chain kinds are, in order: source_before,
  proposal, review, approval, approval_decision, apply, source_after,
  verification, knowledge, completion.
* KM acceptance reuses the reviewed `POST /v2/knowledge/accept` with the
  candidate id from `GET /v2/changes/<id>` (`knowledge.candidate_revision_id`).

"""

from __future__ import annotations

from uuid import uuid4

import pytest  # noqa: F401

from tests.ai8t03.conftest import *  # noqa: F401,F403  (fixtures + T03 helpers)
from tests.ai8t03.conftest import (  # noqa: F401
    DSN, applied_change, change_dto, head_tuple, install_provider, job_row, login,
    outbox_rows, pump, report_of, restarted, write_oracle,
)
from tests.ai4t01.conftest import drain_outbox  # noqa: F401


def submit_result_decision(client, workspace, apply_job_id, *, action="accept",
                           reason="reviewed the exact report", digest=None,
                           idempotency_key=None, extra=None):
    body = {"workspace_id": workspace, "action": action, "reason": reason}
    if digest is not None:
        body["expected_report_digest"] = digest
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    if extra:
        body.update(extra)
    return client.post(f"/v2/changes/{apply_job_id}/result-decision", json=body)


def submit_work_proof(client, workspace, apply_job_id, *, index=0, evidence_ref="PR-123"):
    return client.post(f"/v2/changes/{apply_job_id}/work-proof", json={
        "workspace_id": workspace, "work_index": index, "evidence_ref": evidence_ref})


def approver_client(change):
    """The distinct `change_approver` account that recorded the approve decision
    (role bundle includes change.accept_result: contracts §4 'default cung role
    approver')."""
    return change["ready"]["approver"]["client"]


def accept_candidate_km(setup, owner, change, *, expect=200):
    """Human `knowledge.confirm` accepts the rebuilt KM candidate through the
    knowledge-confirm route."""
    ws, store, t = change["workspace"], setup["store"], setup["tenant"]
    dto = change_dto(change["client"], ws, change["apply_job_id"])
    candidate = dto["knowledge"]["candidate_revision_id"]
    assert candidate, dto
    head = store.head(t, ws)
    resp = owner["client"].post("/v2/knowledge/accept", json={
        "workspace_id": ws, "knowledge_revision_id": candidate,
        "source_revision_id": head.source_revision_id, "decision_epoch": head.decision_epoch,
        "expected_version": head.version, "idempotency_key": f"accept-{uuid4().hex}"})
    assert resp.status_code == expect, resp.text
    return candidate


def machine_verified_change(setup, monkeypatch, owner, tmp_path, *, oracle=30, provider="ok",
                            **kw):
    change = applied_change(setup, monkeypatch, owner, **kw)
    if oracle is not None:
        write_oracle(tmp_path, monkeypatch, artifact_id=change["target"], expected=oracle)
    change["provider"] = install_provider(monkeypatch, provider)
    pump(setup)
    return change


def awaiting_change(setup, monkeypatch, owner, tmp_path, **kw):
    """Report PASS + KM candidate accepted by a human: the only state from which
    a result acceptance may complete."""
    change = machine_verified_change(setup, monkeypatch, owner, tmp_path, **kw)
    dto = change_dto(change["client"], change["workspace"], change["apply_job_id"])
    assert dto["state"] == "MACHINE_VERIFIED", dto
    change["km"] = accept_candidate_km(setup, owner, change)
    change["head_accepted"] = setup["store"].head(setup["tenant"], change["workspace"])
    return change


def report_digest(change, setup):
    return report_of(change, setup["store"], setup["tenant"])["report_digest"]


def result_footprint(setup, change):
    """Every durable write a result decision may make, counted in SQL."""
    store, t, ws = setup["store"], setup["tenant"], change["workspace"]
    with store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s "
                    "AND event_type='CHANGE_RESULT_DECISION_RECORDED'", (t, ws))
        audit = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s "
                    "AND kind='CHANGE_COMPLETED'", (t, ws))
        completed = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s",
                    (t, ws))
        decisions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND "
                    "workspace_id=%s", (t, ws))
        sources = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_knowledge_revisions WHERE tenant_id=%s AND "
                    "workspace_id=%s", (t, ws))
        kms = cur.fetchone()[0]
    return {"audit": audit, "completed_events": completed, "semantic_decisions": decisions,
            "source_revisions": sources, "km_revisions": kms,
            "head": head_tuple(store, t, ws)}


def reasons_in(resp) -> str:
    """All gate reason codes of a 409 body as one string."""
    body = resp.json()
    return " ".join([str(body.get("error"))] + [str(r) for r in (body.get("reasons") or [])])


def build_second_change(setup, monkeypatch, change1, *, expected_current, proposed,
                        apply=True, worker_prefix="c3", edit_path="refund_window_days"):
    """A SECOND change over the head the FIRST change produced (actual S1/K1).
    Uses the same real chain (impact -> simulation -> proposal -> review ->
    approval -> recorded human approve -> apply) and the SAME confirmed intent as
    change 1 (a new intent would publish a new source and move the head). Returns
    a dict; `apply=False` stops after the recorded approve decision."""
    from tests.ai7t01.conftest import (
        create_real_impact_job, create_real_simulation_job, submit_claim_and_get)
    from tests.ai7t02.conftest import submit_claim_and_get_review
    from tests.ai7t03.conftest import submit_claim_and_get_approval
    from tests.ai8t01.conftest import record_real_approve_decision, submit_claim_and_get_apply

    ws, store, t = change1["workspace"], setup["store"], setup["tenant"]
    client = change1["client"]
    drain_outbox(store, t, ws)
    intent_id = job_row(store, t, ws, change1["ready"]["proposal_job_id"])["payload"]["intent_id"]
    impact = create_real_impact_job(setup, client, ws)
    sim = create_real_simulation_job(setup, client, ws)
    prop = submit_claim_and_get(setup, client, ws, kind="proposal", payload={
        "intent_id": intent_id, "impact_job_id": impact, "simulation_job_id": sim,
        "target_artifact_id": change1["target"], "edit_kind": "yaml_typed_edit",
        "edits": [{"path": edit_path, "expected_current": expected_current,
                   "proposed": proposed}]}, worker_id=f"{worker_prefix}-prop")
    out = {"proposal_body": prop}
    if prop["execution_status"] != "COMPLETED":
        return out
    proposal_job_id = prop.get("job_id") or prop.get("id")
    review = submit_claim_and_get_review(setup, client, ws, proposal_job_id=proposal_job_id,
                                         worker_id=f"{worker_prefix}-review")
    review_job_id = review.get("job_id") or review.get("id")
    approval = submit_claim_and_get_approval(
        setup, client, ws, proposal_job_id=proposal_job_id, review_job_id=review_job_id,
        worker_id=f"{worker_prefix}-approval")
    approval_job_id = approval.get("job_id") or approval.get("id")
    digest = approval["execution_result"]["approval_digest"]
    record_real_approve_decision(setup, ws, approval_job_id=approval_job_id, digest=digest,
                                 approver=change1["ready"]["approver"])
    out.update(proposal_job_id=proposal_job_id, review_job_id=review_job_id,
               approval_job_id=approval_job_id, digest=digest, target=change1["target"])
    if not apply:
        return out
    apply_job_id = f"job-apply-{uuid4().hex}"
    head_before = store.head(t, ws)
    body = submit_claim_and_get_apply(setup, client, ws, approval_job_id=approval_job_id,
                                      job_id=apply_job_id, expected_approval_digest=digest,
                                      worker_id=f"{worker_prefix}-apply")
    out.update(apply_job_id=apply_job_id, apply_body=body,
               verify_job_id=verification_job_id(apply_job_id),
               rebuild_job_id=rebuild_job_id(apply_job_id), head_before=head_before,
               s_before=head_before.source_revision_id, workspace=ws,
               client=client, ready={"approver": change1["ready"]["approver"],
                                     "proposal_job_id": proposal_job_id,
                                     "review_job_id": review_job_id,
                                     "approval_job_id": approval_job_id, "digest": digest},
               target=change1["target"])
    if body["execution_status"] == "COMPLETED":
        out["s_after"] = store.head(t, ws).source_revision_id
    return out


def pinned(store, tenant, workspace, job_id):
    """(pinned_source_revision_id, pinned_knowledge_revision_id,
    pinned_decision_epoch) of a job row -- what the job actually read."""
    with store._tx() as cur:
        cur.execute("SELECT pinned_source_revision_id, pinned_knowledge_revision_id, "
                    "pinned_decision_epoch FROM v2_jobs WHERE tenant_id=%s AND "
                    "workspace_id=%s AND job_id=%s", (tenant, workspace, job_id))
        return cur.fetchone()


def patch_authority_kind(monkeypatch, kind):
    """Route-level injection: the caller's session resolves normally but its
    `actor_kind` is forced to a non-human kind (there is no login path that
    yields one, so this is the only way to drive `require_human_actor` through
    a real route). Patches every `infra.*` module that binds `_authority`."""
    import sys
    from infra import v2_policy_routes as routes
    original = routes._authority

    def _fake(identity, request, workspace_id):
        return {**original(identity, request, workspace_id), "actor_kind": kind}

    for name, mod in list(sys.modules.items()):
        if name.startswith("infra.") and getattr(mod, "_authority", None) is original:
            monkeypatch.setattr(mod, "_authority", _fake)


def seed_open_critical_conflict(setup, monkeypatch, target):
    """A REAL, unresolved HIGH conflict finding (conflict-finding executor) between a
    candidate extracted from `target` (the artifact a change will edit) and one
    extracted from a second artifact. Must be seeded BEFORE the change's base
    publish (the seeding helpers publish). Returns the finding id."""
    from tests.ai4t02.conftest import seed_extraction_candidates
    from tests.ai5t02.conftest import _candidate

    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id=target, text="refund_window_days: 14\n",
        candidates_json=[_candidate("refund_window", "refund_window_days", 14,
                                    {"category": "refund", "source": "config"},
                                    "refund_window_days: 14", unit="days")])
    b = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="refund-sop", text="SOP. Refunds are allowed within 30 days.",
        candidates_json=[_candidate("refund_window", "refund_window_days", 30,
                                    {"category": "refund", "source": "SOP"},
                                    "Refunds are allowed within 30 days.", unit="days")])
    ids_ = [a["execution_result"]["candidates"][0]["candidate_id"],
            b["execution_result"]["candidates"][0]["candidate_id"]]
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    job_id = f"job-finding-{uuid4().hex}"
    assert client.post("/v2/jobs", json={"workspace_id": ws, "kind": "finding", "job_id": job_id,
                                         "payload": {"candidate_ids": ids_}}).status_code == 200
    from infra.worker import job_worker
    job_worker.claim_and_admit_one(store, t, ws, "gap-finding-worker")
    findings = job_row(store, t, ws, job_id)["execution_result"]["findings"]
    assert findings and findings[0]["severity"] == "HIGH" and findings[0]["authority_required"]
    return findings[0]["finding_id"]
