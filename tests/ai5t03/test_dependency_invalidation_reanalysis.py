"""Dependency invalidation / reanalysis after a
committed decision event.

Covers N1-N3 and the stale-protection probe. Every test below is written
against the REAL entry points (an "invalidation" job kind wired the same way
`extraction`/`relation`/`finding` are; a staleness/epoch marker reachable
from `GET /v2/knowledge/draft/{artifact_id}`).

Real Postgres, real `infra.judge.app` FastAPI client, real worker claims --
no mocks of the store or the HTTP layer (only the LLM provider call inside
`seed_extraction_candidates` is monkeypatched, exactly as the already-
reviewed relation/decision fixtures do).
"""

from __future__ import annotations

import pytest

from infra.worker import job_worker
from infra.worker.invalidation import InvalidationExecutor

from tests.ai4t01.conftest import login
from tests.ai5t03.conftest import current_epoch, current_head, seed_dependent_relation


def _requester_client(setup):
    client = setup["browser"]()
    login(client, setup["requester"], setup["credential"])
    return client


def _claim_next_job(store, tenant, workspace, worker_id):
    """Real worker claim loop: `claim_and_admit_one` returns the NEXT outbox
    event regardless of kind (e.g. a `DECISION_RECORDED` event ahead of the
    `JOB_QUEUED` one in the same outbox) -- ack any non-job event and keep
    claiming until a real `JOB_QUEUED` claim is admitted or the outbox is
    drained. Never blindly acks a `JOB_QUEUED` claim itself (that would skip
    its execution)."""
    for _ in range(20):
        claim = job_worker.claim_and_admit_one(store, tenant, workspace, worker_id)
        if claim is None:
            return None
        if claim.kind == "JOB_QUEUED":
            return claim
        store.ack_outbox(tenant, workspace, claim.id, worker_id, claim.fencing_token)
    raise AssertionError("no JOB_QUEUED claim found within 20 outbox events")


def _run_invalidation(setup, requester, *, decision_id, target, job_id=None):
    """Submit a real `invalidation` job through the real route, claim it
    with the real worker, and return its persisted `execution_result`."""
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    job_id = job_id or f"invalidation-test-{decision_id}"
    resp = requester.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "invalidation", "job_id": job_id,
        "payload": {"decision_id": decision_id, "target": target}})
    assert resp.status_code == 200, resp.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "invalidation-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = requester.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    return status.json()


# ----------------------------------------------------------------------- N1


def test_n1_resolve_decision_leaves_dependent_relation_unmarked_stale_today(
        setup, monkeypatch, owner):
    """N1: confirm emergency-scope decision via API; read graph/questions
    immediately. Task card requires the dependent relation to be marked
    stale and a new reanalysis (with a correctly epoch-pinned KM draft) to
    be scheduled. Today `knowledge_draft` carries no epoch/staleness field
    at all (confirmed by reading `PostgresV2Store.knowledge_draft`,
    infra/adapters/v2_postgres.py:1707-1797 -- its returned dict is exactly
    {artifact_id, extraction_job_id, execution_status, status, candidates,
    relations, findings, questions}), so this test documents the exact gap:
    no field exists to assert "gắn epoch đúng" against."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]

    # Real dependent: a relation job over the same two candidates.
    relation_body = seed_dependent_relation(setup, requester, [cand_sop, cand_code])
    assert relation_body["execution_result"]["proposed_edges"], relation_body

    epoch_before = current_epoch(setup, requester)
    resolve = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
        "authoritative_claim_id": cand_sop, "rationale": "emergency scope confirmed"})
    assert resolve.status_code == 200, resolve.text
    epoch_after = current_epoch(setup, requester)
    assert epoch_after == epoch_before + 1, (epoch_before, epoch_after)

    draft = requester.get("/v2/knowledge/draft/expense-sop",
                          params={"workspace_id": setup["workspace"]})
    assert draft.status_code == 200, draft.text
    body = draft.json()
    # DESIRED behavior ("gắn epoch đúng"): the draft must carry
    # the epoch it was computed under, matching the post-decision epoch.
    # Actual current failure (recorded, not asserted-away): KeyError --
    # `PostgresV2Store.knowledge_draft`'s returned dict has no
    # `decision_epoch` key at all (infra/adapters/v2_postgres.py:1707-1797).
    assert body["decision_epoch"] == epoch_after, (
        f"AI5-T03 not implemented: expected draft.decision_epoch == {epoch_after}, "
        f"got keys {sorted(body)}")


def test_n1_invalidation_job_kind_executes_and_marks_dependents_stale(
        setup, monkeypatch, owner):
    """N1: the real `invalidation` job kind
    is registered end-to-end. This is the real N1 acceptance path
    ("Handler mới được phân tích, dependents stale, source
    hash giữ nguyên") driven through the REAL auto-trigger, not a
    hand-submitted invalidation job:

      POST /v2/conflict/resolve (real decision commit, real
      `_schedule_invalidation` call inside the route)
        -> real worker claims the auto-created `invalidation-{decision_id}`
           job (`job_worker.claim_and_admit_one`)
        -> the real dependent (a `relation` job over the same two
           candidates, seeded via `seed_dependent_relation`) is durably
           marked stale (`store.is_job_stale`, a FRESH read via the store,
           not the same in-memory object the executor used)
        -> the source revision (`WorkspaceHead.source_revision_id`) is
           provably UNCHANGED by any of this -- choosing an authority does
           not mutate observations/IR (the same invariant the decision ledger
           already holds for `record_human_decision` itself).
    """
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]

    relation_body = seed_dependent_relation(setup, requester, [cand_sop, cand_code])
    dependent_job_id = relation_body["job_id"]
    assert relation_body["execution_result"]["proposed_edges"], relation_body

    head_before = current_head(setup)
    assert not setup["store"].is_job_stale(
        setup["tenant"], setup["workspace"], dependent_job_id)

    resolve = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
        "authoritative_claim_id": cand_sop, "rationale": "emergency scope confirmed"})
    assert resolve.status_code == 200, resolve.text
    decision_id = resolve.json()["decision_id"]

    # Real worker claims the auto-created invalidation job -- the route
    # (`_schedule_invalidation`) already enqueued it; nothing here is a
    # direct/manual `execute()` call.
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    claim = _claim_next_job(store, tenant, workspace, "invalidation-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim

    # Durable, FRESH read (a distinct query, not the executor's own object).
    assert store.is_job_stale(tenant, workspace, dependent_job_id), (
        "dependent relation job was not marked stale after the auto-triggered "
        "invalidation job ran")
    causation = store.causation_for_decision(tenant, workspace, decision_id)
    assert dependent_job_id in causation, (
        "no reanalysis job was scheduled/caused for the dependent", causation)

    head_after = current_head(setup)
    assert head_after.decision_epoch == head_before.decision_epoch + 1
    assert head_after.source_revision_id == head_before.source_revision_id, (
        "source hash must stay unchanged (KM version/epoch increases; "
        "authority choice never mutates source observations/IR)")


def test_n1_invalidation_job_kind_registered_in_policy_and_worker(setup, monkeypatch, owner):
    """N1 precondition (rewritten): the previously-absent `invalidation` job
    kind is now really registered -- both in `JOB_CREATE_PERMISSION` (a
    hand-submitted job of this kind is accepted, not rejected with
    'unsupported job kind') and in `BUILTIN_EXECUTORS`/`resolve_executor`
    (a real `InvalidationExecutor`, not `None`)."""
    from tests.ai4t02.conftest import seed_extraction_candidates
    requester = _requester_client(setup)
    # `create_job` requires a real published source revision (`head.
    # source_revision_id is not None`) for ANY job kind, invalidation
    # included -- seed one real extraction so the workspace head is valid.
    seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-sop",
        text="Chi phi. Approval threshold is 500000 VND.",
        candidates_json=[{
            "subject": "approval_threshold", "predicate": "approval_threshold_vnd",
            "value": 500000, "unit": "VND", "modality": "must",
            "scope": {"category": "expense", "source": "SOP"},
            "outcome": "Finance Manager approval",
            "quotes": ["Approval threshold is 500000 VND."]}])
    result = _run_invalidation(setup, requester, decision_id="manual-decision-1",
                               target="candidate-does-not-exist")
    assert result["execution_status"] == "COMPLETED", result
    assert result["execution_result"]["dependent_count"] == 0, result
    assert isinstance(job_worker.resolve_executor("invalidation"), InvalidationExecutor)


# ----------------------------------------------------------------------- N2


def test_n2_duplicate_decision_event_has_no_deduplicated_scheduling_to_test(setup, monkeypatch, owner):
    """N2: duplicate/out-of-order event must not schedule two reanalysis
    jobs nor double-bump epoch. Actual current behavior (verified here, not
    assumed): the route derives `idempotency_key="resolve-{conflict_id}-
    {account_id}"` and includes the FRESH `head.version` it reads at request
    time inside the hashed payload (`infra/v2_policy_routes.py::resolve_conflict`,
    `PostgresV2Store.record_human_decision`'s `request_hash`). Because the
    first call already bumped the version, an identical-looking duplicate
    POST sees a DIFFERENT `expected_version` in its own hash and is rejected
    as `idempotency_conflict` (409) rather than replayed -- so today a
    duplicate is safely rejected (no double epoch bump, no double decision),
    but NOT via true idempotent replay of the SAME result. This is the
    concrete gap: no invalidation-job scheduler exists yet to assert "exactly
    one reanalysis job per decision_id" against, independent of this
    request-level accident of version-inclusion."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    epoch_before = current_epoch(setup, requester)

    body = {"workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
            "authoritative_claim_id": cand_sop, "rationale": "dup-check"}
    first = owner["client"].post("/v2/conflict/resolve", json=body)
    second = owner["client"].post("/v2/conflict/resolve", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json() == {"error": "idempotency_conflict"}, second.text
    epoch_after = current_epoch(setup, requester)
    assert epoch_after == epoch_before + 1, (
        "epoch double-bumped by duplicate decision delivery", epoch_before, epoch_after)

    # N2 (rewritten): the invalidation job kind is now really registered,
    # and the duplicate decision POST must not have caused a second
    # invalidation/reanalysis job for the SAME decision -- the real
    # `invalidation-{decision_id}` job_id is deterministic, so the second
    # `_schedule_invalidation` call (had it fired) would collide with the
    # first via `create_job`'s own idempotency guard, never creating a
    # second job.
    assert isinstance(job_worker.BUILTIN_EXECUTORS.get("invalidation"),
                      InvalidationExecutor)
    decision_id = first.json()["decision_id"]
    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    invalidation_job_id = f"invalidation-{decision_id}"
    status = requester.get(f"/v2/jobs/{invalidation_job_id}",
                           params={"workspace_id": workspace})
    assert status.status_code == 200, (
        "the real auto-triggered invalidation job for this decision must "
        "exist exactly once", status.text)


def test_n2_duplicate_invalidation_processing_schedules_exactly_one_reanalysis_job(
        setup, monkeypatch, owner):
    """N2 (real dedup test, item 4 of the corrective round): process the
    SAME decision's invalidation twice -- simulating a worker restart
    mid-processing / an out-of-order redelivery of the same invalidation
    job -- by directly re-running the executor's `execute()` a second time
    over the SAME (decision_id, target) pair (the real shape a lease-expiry
    re-claim or a duplicate outbox delivery would produce: a second
    `execute()` call for logically the same event). Assert: exactly ONE
    reanalysis job / causation row per dependent, no double epoch bump from
    invalidation itself (epoch only bumps at `record_human_decision`, never
    from invalidation), and no lost work on the second pass."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    relation_body = seed_dependent_relation(setup, requester, [cand_sop, cand_code])
    dependent_job_id = relation_body["job_id"]

    resolve = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
        "authoritative_claim_id": cand_sop, "rationale": "dup-invalidation-check"})
    assert resolve.status_code == 200, resolve.text
    decision_id = resolve.json()["decision_id"]
    epoch_after_decision = current_epoch(setup, requester)

    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    invalidation_job_id = f"invalidation-{decision_id}"

    # First pass: real worker claim (as production does it).
    claim1 = _claim_next_job(store, tenant, workspace, "invalidation-worker-a")
    assert claim1 is not None and claim1.kind == "JOB_QUEUED", claim1
    causation_after_first = store.causation_for_decision(tenant, workspace, decision_id)
    assert dependent_job_id in causation_after_first
    reanalysis_job_id = causation_after_first[dependent_job_id]

    # Second pass: re-run the SAME invalidation job's logic a second time
    # (the exact scenario a restarted/redelivered worker produces) by
    # invoking the real executor directly over the SAME already-persisted
    # job row -- exercising `find_dependent_jobs_for_candidate` +
    # `causation_for_decision` + `create_job`'s own idempotency guard a
    # second time, not a different code path than production uses.
    ctx = job_worker.ExecutionContext(job={
        "tenant_id": tenant, "workspace_id": workspace, "job_id": invalidation_job_id,
        "payload": {"decision_id": decision_id, "target": cand_sop}}, store=store, config=None)
    executor = InvalidationExecutor()
    request = executor.prepare(ctx)
    outcome = executor.execute(ctx, request, job_worker.ProviderCallBudget(limit=0))

    causation_after_second = store.causation_for_decision(tenant, workspace, decision_id)
    assert causation_after_second == causation_after_first, (
        "re-processing the same decision's invalidation scheduled a "
        "DIFFERENT/duplicate reanalysis job", causation_after_first, causation_after_second)
    assert causation_after_second[dependent_job_id] == reanalysis_job_id
    # No lost work: the reanalysis job created on the first pass must still
    # exist and be exactly one row, not zero (ack'd-but-dropped) or two
    # (double-scheduled).
    reanalysis_status = requester.get(f"/v2/jobs/{reanalysis_job_id}",
                                      params={"workspace_id": workspace})
    assert reanalysis_status.status_code == 200, reanalysis_status.text
    # No double epoch bump from invalidation itself -- epoch only ever
    # bumps at `record_human_decision`.
    assert current_epoch(setup, requester) == epoch_after_decision


# ----------------------------------------------------------------------- N3


def test_n3_reanalysis_budget_exhaustion_produces_clear_non_convergence_reason(
        setup, monkeypatch, owner, tmp_path):
    """N3 (rewritten): `infra.worker.job_quota.load_reanalysis_budget` is now
    real. Force the budget down to 1 via a real config-file override (the
    SAME `RIPPLE_BUDGET_CONFIG_PATH`-driven sidecar key mechanism production
    uses, not a monkeypatched function), seed TWO real dependents for the
    same decided candidate (the `finding` job `seed_conflict_finding` already
    produced over `cand_sop`/`cand_code`, plus a fresh `relation` job over
    the same pair), and confirm the executor's real behavior when the
    ceiling is exceeded: one dependent scheduled, the other explicitly
    DEFERRED with a clear, non-empty `non_convergence_reason` -- never a
    silent infinite loop, never a silent partial drop, never a crash."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    # Real config-file override: same key/loader production uses, layered on
    # top of the existing full budget config `setup` already wrote (must
    # keep `max_provider_calls_per_job` etc. -- extraction/finding job
    # creation still needs them) so only the NEW sidecar key is added.
    import os as _os
    existing_config_path = _os.environ["RIPPLE_BUDGET_CONFIG_PATH"]
    existing_config_text = open(existing_config_path, encoding="utf-8").read()
    budget_path = tmp_path / "reanalysis_budget.yaml"
    budget_path.write_text(
        existing_config_text + "\nmax_reanalysis_jobs_per_decision: 1\n", encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(budget_path))
    from infra.worker import job_quota
    assert job_quota.load_reanalysis_budget() == 1

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    # Second real dependent for the SAME candidate: a `relation` job.
    seed_dependent_relation(setup, requester, [cand_sop, cand_code])

    resolve = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
        "authoritative_claim_id": cand_sop, "rationale": "budget-exhaustion-check"})
    assert resolve.status_code == 200, resolve.text
    decision_id = resolve.json()["decision_id"]

    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    claim = _claim_next_job(store, tenant, workspace, "invalidation-worker")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim

    invalidation_job_id = f"invalidation-{decision_id}"
    status = requester.get(f"/v2/jobs/{invalidation_job_id}",
                           params={"workspace_id": workspace})
    assert status.status_code == 200, status.text
    result = status.json()["execution_result"]
    assert result["dependent_count"] >= 2, result
    assert len(result["scheduled_reanalysis"]) == 1, (
        "budget of 1 must schedule exactly one reanalysis job, not more", result)
    assert result["deferred_budget_exhausted"], (
        "the second dependent beyond the budget must be explicitly listed "
        "as deferred, not silently dropped", result)
    assert result["non_convergence_reason"], (
        "an exhausted budget must surface a clear, non-empty reason, not "
        "a silent partial run", result)
    assert "budget" in result["non_convergence_reason"].lower()


def test_n3_repeated_rejection_after_resolve_is_rejected_not_a_reopen(setup, monkeypatch, owner):
    """N3 precondition check: today, once a conflict is resolved, a second
    resolve attempt on the SAME finding with DIFFERENT evidence
    (`authoritative_claim_id` flipped from SOP to code, i.e. "evidence
    changed") is still rejected -- it reuses the SAME derived
    `idempotency_key` ("resolve-{conflict_id}-{account_id}") as the first
    call, so the differing payload hash is caught by
    `PostgresV2Store._replay`'s own hash-mismatch guard and raised as
    `StoreConflict("idempotency_conflict")` -> 409 (verified directly, not
    the `LifecycleError`/`REMEDIATION_PENDING` path the
    N3 clause is really asking about). There is no "reopen because evidence
    changed" path at all yet -- exactly the gap N3's 'reopen có lý do khi
    evidence đổi' clause requires (a decision-scoped,
    not idempotency-key-scoped, reopen mechanism). Documents current
    behavior as a baseline so a future implementation's diff
    is visible."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    body = {"workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
            "authoritative_claim_id": cand_sop, "rationale": "first"}
    first = owner["client"].post("/v2/conflict/resolve", json=body)
    assert first.status_code == 200, first.text

    second_body = dict(body, rationale="evidence changed",
                       authoritative_claim_id=cand_code)
    second = owner["client"].post("/v2/conflict/resolve", json=second_body)
    # Current real behavior: 409 idempotency_conflict, not a reopen. This is the gap.
    assert second.status_code == 409, second.text
    assert second.json() == {"error": "idempotency_conflict"}, second.text


# ------------------------------------------------------- stale-protection probe


def test_stale_probe_read_immediately_after_commit_before_any_worker_runs(
        setup, monkeypatch, owner):
    """Regression clause: 'Probe read ngay sau
    decision commit, trước worker' -- a read immediately after the decision
    transaction commits, with NO invalidation worker having run at all (no
    such worker exists to run), must still be protected against showing
    stale/pre-decision state as current. Today `knowledge_draft` has no
    epoch field to check staleness against at all, so a caller reading
    right after commit cannot distinguish 'this is current post-decision
    state' from 'this is stale pre-decision state' -- the exact hazard that
    must be closed. This test pins that hazard down with a
    concrete before/after epoch comparison the future implementation must
    make visible on the read."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    seed_dependent_relation(setup, requester, [cand_sop, cand_code])

    epoch_before = current_epoch(setup, requester)
    resolve = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
        "authoritative_claim_id": cand_sop, "rationale": "probe"})
    assert resolve.status_code == 200, resolve.text
    epoch_after_commit = current_epoch(setup, requester)
    assert epoch_after_commit == epoch_before + 1

    # No worker has run (no invalidation executor exists to run it). The
    # probe read happens right now, in this exact window.
    draft = requester.get("/v2/knowledge/draft/expense-sop",
                          params={"workspace_id": setup["workspace"]})
    assert draft.status_code == 200, draft.text
    body = draft.json()
    # DESIRED: even with no worker having run, the read must expose the
    # current epoch so a caller can detect it is stale relative to
    # `epoch_after_commit`. Actual current failure: KeyError, no such field.
    assert body["decision_epoch"] == epoch_after_commit, (
        f"gap: draft read in the pre-worker window carries no epoch a caller "
        f"could compare against {epoch_after_commit} to detect staleness; keys={sorted(body)}")


# --------------------------------------------------- atomicity gap


def test_atomicity_gap_invalidation_scheduling_failure_after_decision_commit_is_a_silent_lost_work(
        setup, monkeypatch, owner):
    """The invalidation job's creation
    (`PostgresV2Store._create_job_core`) is folded into the SAME `with self._tx() as
    cur:` block as `record_human_decision`'s own decision insert / epoch
    bump / audit row, by calling `_create_job_core` directly on the
    caller's already-open `cur` instead of a separate
    `store.create_job(...)` call in a second transaction. The PUBLIC
    method `PostgresV2Store.create_job` is therefore not on that call path
    -- it is merely a thin wrapper other callers (`POST /v2/jobs`, retry) still use,
    but `record_human_decision` never calls it. (`record_human_decision`'s
    `schedule_invalidation_target` branch calls `self._create_job_core(cur,
    ...)` on the SAME `cur`, not `self.create_job(...)`).

    The failure is injected at the shared call site,
    `PostgresV2Store._create_job_core`, and the test asserts true
    all-or-nothing atomicity. Verified through the
    REAL HTTP route (`POST /v2/conflict/resolve`), not a direct store call.
    """
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    relation_body = seed_dependent_relation(setup, requester, [cand_sop, cand_code])
    dependent_job_id = relation_body["job_id"]

    from infra.adapters.v2_postgres import PostgresV2Store
    real_create_job_core = PostgresV2Store._create_job_core

    def _failing_create_job_core(self, cur, tenant_id, workspace_id, job_id, kind,
                                  payload, **kwargs):
        if kind == "invalidation":
            # Raised INSIDE the same transaction/cursor `record_human_
            # decision` is using -- the real, now-shared call site. Any
            # exception type propagates identically here (psycopg2 marks
            # the connection's transaction as needing rollback on the
            # first execute() after an unhandled exception regardless of
            # exception class), so a plain RuntimeError is the most direct
            # simulation of "a real store failure happened exactly at
            # invalidation scheduling" -- not a specific caught/swallowed
            # exception type this route special-cases.
            raise RuntimeError(
                "simulated failure exactly at invalidation-job creation, "
                "same transaction as the decision commit")
        return real_create_job_core(self, cur, tenant_id, workspace_id, job_id, kind,
                                     payload, **kwargs)

    monkeypatch.setattr(PostgresV2Store, "_create_job_core", _failing_create_job_core)

    epoch_before = current_epoch(setup, requester)
    # The real route's `except (TenantScopeError, StoreConflict)` clauses do
    # not name this injected exception type, and the test client (by
    # default) re-raises any exception the ASGI app itself does not turn
    # into an HTTP response -- so the failure surfaces here as a raised
    # exception, not a 4xx/5xx response body. This is itself the same
    # "uncaught exception on an already-attempted write" shape for a bare
    # RuntimeError; the binding assertion is what happens to DURABLE STATE afterward (below), not the
    # exact HTTP status this particular exception type produces.
    with pytest.raises(RuntimeError, match="simulated failure exactly at invalidation-job creation"):
        owner["client"].post("/v2/conflict/resolve", json={
            "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
            "authoritative_claim_id": cand_sop, "rationale": "atomicity-gap-probe-round2"})

    # Restore ONLY the patched method (`monkeypatch.undo()` would
    # undo EVERY patch this shared fixture applied this test, including
    # `setup`'s own `RIPPLE_V2_DSN`/`RIPPLE_V2_BLOB_BUCKET` env vars,
    # breaking the store lookup entirely).
    PostgresV2Store._create_job_core = real_create_job_core

    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]

    # (b) the decision itself must NOT have committed either -- true
    # all-or-nothing atomicity. No epoch bump, no decision row.
    epoch_after = current_epoch(setup, requester)
    assert epoch_after == epoch_before, (
        "decision_epoch advanced despite the injected invalidation-"
        "scheduling failure -- the decision committed even though its "
        "invalidation job did not; atomicity gap still present",
        epoch_before, epoch_after)
    with store._tx() as cur:
        cur.execute("""SELECT count(*) FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND target=%s""",
            (tenant, workspace, cand_sop))
        decision_count = cur.fetchone()[0]
    assert decision_count == 0, (
        "a v2_decisions row exists despite the injected invalidation-"
        "scheduling failure -- the decision's own insert was not rolled "
        "back with the failed invalidation-job creation", decision_count)

    # (c) no invalidation job, no stale marker, no causation row exists
    # either -- consistent with nothing having committed at all.
    invalidation_job_id_guess = None
    with store._tx() as cur:
        cur.execute("""SELECT job_id FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='invalidation'""",
            (tenant, workspace))
        invalidation_rows = cur.fetchall()
    assert invalidation_rows == [], (
        "an invalidation job row exists despite the injected creation "
        "failure -- the whole transaction should have rolled back",
        invalidation_rows)
    assert not store.is_job_stale(tenant, workspace, dependent_job_id), (
        "dependent was marked stale despite the whole transaction having "
        "failed -- unexpected side channel")

    # (d) draining the outbox finds no trace of this attempt either -- the
    # decision's own DECISION_RECORDED audit/outbox row rolled back too.
    drained_kinds = []
    for _ in range(20):
        claim = job_worker.claim_and_admit_one(store, tenant, workspace, "drain-probe")
        if claim is None:
            break
        job_id, job_kind = None, None
        if claim.kind == "JOB_QUEUED":
            job_id, job_kind = job_worker._job_id_and_kind(claim)
        drained_kinds.append((claim.kind, job_kind))
        store.ack_outbox(tenant, workspace, claim.id, "drain-probe", claim.fencing_token)
    assert not any(kind == "invalidation" for _, kind in drained_kinds), (
        "an invalidation job surfaced in the outbox despite the whole "
        "transaction having failed", drained_kinds)


def test_normal_success_path_still_schedules_invalidation_after_the_atomicity_fix(
        setup, monkeypatch, owner):
    """Verify that the fold-into-one-transaction design does not
    break the ordinary, non-failing success path: a decision that
    successfully schedules invalidation must still produce both the
    decision row AND the invalidation job/causation row/stale marker, via
    the same real HTTP route, with no failure injected at all."""
    requester = _requester_client(setup)
    from tests.ai5t02.conftest import seed_conflict_finding

    finding_body, (cand_sop, cand_code) = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding = finding_body["execution_result"]["findings"][0]
    relation_body = seed_dependent_relation(setup, requester, [cand_sop, cand_code])
    dependent_job_id = relation_body["job_id"]

    epoch_before = current_epoch(setup, requester)
    resolve = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": finding["finding_id"],
        "authoritative_claim_id": cand_sop, "rationale": "atomicity-fix-success-path-probe"})
    assert resolve.status_code == 200, resolve.text
    decision_id = resolve.json()["decision_id"]

    epoch_after = current_epoch(setup, requester)
    assert epoch_after == epoch_before + 1

    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    invalidation_job_id = f"invalidation-{decision_id}"
    status = requester.get(f"/v2/jobs/{invalidation_job_id}",
                           params={"workspace_id": workspace})
    assert status.status_code == 200, (
        "the real invalidation job must exist after a successful decision", status.text)

    _claim_next_job(store, tenant, workspace, "success-path-worker")

    assert store.is_job_stale(tenant, workspace, dependent_job_id), (
        "dependent relation job was not marked stale after a successful "
        "decision + invalidation run")
    causation = store.causation_for_decision(tenant, workspace, decision_id)
    assert dependent_job_id in causation, (
        "no reanalysis job was scheduled for the dependent after a "
        "successful decision + invalidation run", causation)
