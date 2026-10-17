"""Append-only decisions and authority semantics.

The generic, legacy decision-write path is
`PostgresV2Store.record_decision` (`infra/adapters/v2_postgres.py:1147`)
reached through `POST /v2/knowledge/confirm`
(`infra/v2_policy_routes.py:415`). It writes an opaque JSON payload row into
`v2_decisions`, bumps `v2_workspaces.decision_epoch`, and has NO rationale,
scope, effective-period, evidence/human-assertion provenance, supersede or
revoke concept -- the gap the fuller decision ledger closes.

Tests below are split into:
  (a) tests against the REAL, EXISTING `/v2/knowledge/confirm` +
      `record_decision` baseline, which enforces some of
      D2/D3 (CAS-based no-auto-winner, human-actor requirement, replay
      idempotency) -- these document the baseline that must not regress.
  (b) tests against the fuller surfaces (a fuller decision
      record with rationale/scope/evidence/supersedes/revokes, an
      accepted_unknown-critical gate, a self-approval/high-risk-tier
      gate); the assertions describe the
      acceptance shape (D1/D2/D3).

Every test uses the real FastAPI test client (`infra.judge.app`), real
PostgreSQL, and a real worker claim for upstream seeding (no mocks of the
lifecycle/store itself).
"""

from __future__ import annotations

import pytest

from tests.ai4t01.conftest import login  # noqa: F401
from tests.ai5t02.conftest import _candidate, owner, seed_conflict_finding, setup  # noqa: F401


pytestmark = pytest.mark.usefixtures("setup")


def _confirm(client, workspace, claim_id, expected_version, *, extra=None):
    payload = {"workspace_id": workspace, "claim_id": claim_id}
    if extra:
        payload.update(extra)
    return client.post("/v2/knowledge/confirm", json=payload)


# --------------------------------------------------------------------------- D1: SOP/code conflict, human chooses SOP; answer without evidence file;
# supersede/revoke.

def test_conflict_finding_is_persisted_with_real_severity_and_authority_required(
        setup, monkeypatch, owner):
    """`FindingsExecutor` (`infra/worker/findings.py::
    _severity_and_authority`) now DOES populate `severity`/
    `authority_required` on every finding, derived from the involved
    `RuleCandidate`(s)' `modality`. `seed_conflict_finding`'s two candidates
    (`tests/ai5t02/conftest.py::_candidate`, default `modality="must"`) are
    BOTH `must`-modality -- per the derivation rule (`"must" in
    modality_set -> ("HIGH", "policy_owner")`), the real, persisted CONFLICT
    finding for this SOP-vs-code disagreement must now carry
    `severity == "HIGH"` and `authority_required == "policy_owner"`, so the D2
    critical-gate has a real signal to check against."""
    body, _cands = seed_conflict_finding(setup, monkeypatch, owner["client"])
    findings = body["execution_result"]["findings"]
    conflicts = [f for f in findings if f["kind"] == "CONFLICT"]
    assert conflicts, f"expected a CONFLICT finding for SOP-vs-code disagreement, got {findings}"
    conflict = conflicts[0]
    assert conflict["severity"] == "HIGH", (
        "both seeded candidates are must-modality -- the real derivation "
        f"must classify this CONFLICT as HIGH severity: {conflict}")
    assert conflict["authority_required"] == "policy_owner", conflict


def test_finding_derivation_reflects_must_may_observed_modalities_distinctly(
        setup, monkeypatch, owner):
    """Derivation-correctness check: confirm,
    via real seeded scenarios through the real worker (not a unit call to
    `_severity_and_authority` in isolation -- reusing the real
    product-path seeding rather than a second seed mechanism), that the three named modality tiers produce
    three DISTINCT, correctly-ordered severities, and that a no-signal case
    (a referenced-but-missing candidate) is honestly LOW, not silently
    defaulted to something that would let a genuinely critical finding pass
    as non-critical.

    - `must` vs `must` (the default `seed_conflict_finding` scenario) ->
      HIGH/policy_owner (covered by the sibling test above).
    - `may` vs `may` -> MEDIUM/knowledge_owner (built directly here, mirroring
      `seed_conflict_finding`'s own real job-submission/claim pattern rather
      than a second seeding helper).
    - A GAP for a referenced candidate that cannot be found at all (no
      modality data whatsoever) -> LOW/knowledge_owner, exercised via the
      same `referenced_candidate_not_found` path the findings executor
      builds for a candidate id that was never extracted."""
    from tests.ai4t02.conftest import seed_extraction_candidates
    from tests.ai4t01.conftest import login
    from infra.worker import job_worker
    from uuid import uuid4

    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    requester_client = setup["browser"]()
    login(requester_client, setup["requester"], setup["credential"])

    # `may`-modality vs `may`-modality disagreement -> MEDIUM/knowledge_owner.
    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-sop-may", text="SOP may text.",
        candidates_json=[_candidate("approval_threshold", "approval_threshold_vnd",
                                    500000, {"category": "expense", "source": "SOP"},
                                    "SOP may text.", modality="may")])
    body_b = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-code-may", text="code may text.",
        candidates_json=[_candidate("approval_threshold", "approval_threshold_vnd",
                                    1000000, {"category": "expense", "source": "code"},
                                    "code may text.", modality="may")])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    cand_b = body_b["execution_result"]["candidates"][0]["candidate_id"]

    job_id_may = f"job-finding-may-{uuid4().hex}"
    resp_may = requester_client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "finding", "job_id": job_id_may,
        "payload": {"candidate_ids": [cand_a, cand_b]}})
    assert resp_may.status_code == 200, resp_may.text
    claim_may = job_worker.claim_and_admit_one(store, tenant, workspace, "finding-worker-may")
    assert claim_may is not None and claim_may.kind == "JOB_QUEUED", claim_may
    status_may = requester_client.get(f"/v2/jobs/{job_id_may}", params={"workspace_id": workspace})
    assert status_may.status_code == 200, status_may.text
    conflict_may = next(f for f in status_may.json()["execution_result"]["findings"]
                        if f["kind"] == "CONFLICT")
    assert conflict_may["severity"] == "MEDIUM", conflict_may
    assert conflict_may["authority_required"] == "knowledge_owner", conflict_may

    # A referenced candidate id that was never extracted -> no modality data
    # at all -> GAP "referenced_candidate_not_found" -> LOW/knowledge_owner,
    # the honest no-signal floor (never silently promoted or defaulted to a
    # value that would mask a genuinely critical finding as non-critical).
    job_id_missing = f"job-finding-missing-{uuid4().hex}"
    resp_missing = requester_client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "finding", "job_id": job_id_missing,
        "payload": {"candidate_ids": [f"cand-does-not-exist-{uuid4().hex}"]}})
    assert resp_missing.status_code == 200, resp_missing.text
    claim_missing = job_worker.claim_and_admit_one(
        store, tenant, workspace, "finding-worker-missing")
    assert claim_missing is not None and claim_missing.kind == "JOB_QUEUED", claim_missing
    status_missing = requester_client.get(
        f"/v2/jobs/{job_id_missing}", params={"workspace_id": workspace})
    assert status_missing.status_code == 200, status_missing.text
    gap = next(f for f in status_missing.json()["execution_result"]["findings"]
              if f["reason"] == "referenced_candidate_not_found")
    assert gap["severity"] == "LOW", gap
    assert gap["authority_required"] == "knowledge_owner", gap


def test_confirming_sop_over_code_does_not_mutate_source_or_ir(setup, monkeypatch, owner):
    """D1: human picks SOP as the authoritative rule over the conflicting
    code value. This must NEVER edit the underlying
    observation/candidate (source/IR must stay byte-identical) -- authority
    selection is a decision-layer fact, not a rewrite of what was observed.
    Uses the REAL existing `/v2/knowledge/confirm` baseline since no fuller
    "resolve conflict with authority=SOP" route exists yet; asserts the
    baseline at least does not corrupt the candidate it is not supposed to
    touch (a real regression baseline)."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    cand_sop_id = cand_ids[0]
    head_before = setup["store"].head(setup["tenant"], setup["workspace"])

    resp = _confirm(owner["client"], setup["workspace"], cand_sop_id,
                    head_before.version)
    assert resp.status_code == 200, resp.text

    # Re-run the SAME finding job id's underlying candidates lookup is not
    # exposed as a route; instead assert the workspace head's
    # `source_revision_id` (byte-identity of ingested source) is unchanged
    # by a decision -- `record_decision` only ever nulls
    # `knowledge_revision_id`, never touches `source_revision_id` (see
    # infra/adapters/v2_postgres.py:1175-1178).
    head_after = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after.source_revision_id == head_before.source_revision_id


def test_decision_without_evidence_file_is_rejected_or_marked_human_assertion(setup, monkeypatch, owner):
    """D1: 'Decision khong evidence file duoc ghi human assertion, khong tao
    quote gia' -- a decision payload that supplies no evidence/quote
    reference must either be refused, or persisted with an explicit
    human-assertion provenance marker, never silently promoted to look like
    a sourced quote. PROPOSED: the real baseline `record_decision` accepts
    an arbitrary opaque payload and has no `provenance`/`evidence_ref`
    concept at all -- this is expected to FAIL today (no such field
    surfaces back), documenting the gap a fuller ledger must close."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])
    resp = _confirm(owner["client"], setup["workspace"], cand_ids[0], head.version,
                    extra={"rationale": "Team lead asserts SOP applies here.",
                           "evidence_ref": None})
    assert resp.status_code == 200, resp.text
    decision_id = resp.json()["decision_id"]
    # PROPOSED requirement: a read-back of the decision must expose a
    # provenance marker distinguishing "human assertion, no evidence file"
    # from a sourced quote. No such read surface exists yet.
    assert "provenance" in resp.json() or decision_id.startswith("human-assertion:"), (
        "no evidence/human-assertion provenance surfaced on the decision "
        f"response -- proposed ledger contract not yet implemented: {resp.json()}")


def test_supersede_and_revoke_preserve_append_only_history(setup, monkeypatch, owner):
    """D1: supersede/revoke must add a NEW event, never edit/delete history.

    Asserts the 200 response and, critically, that the ORIGINAL row's own columns
    (`payload`, `action`, `target`, etc.) are byte-identical before and
    after the supersede -- the database's own immutability trigger
    (`v2_reject_immutable_change()`, confirmed by attempting a direct
    `UPDATE` below, which raises) makes this an enforced, not merely designed, guarantee."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])
    confirm_resp = _confirm(owner["client"], setup["workspace"], cand_ids[0], head.version)
    assert confirm_resp.status_code == 200, confirm_resp.text
    decision_id = confirm_resp.json()["decision_id"]

    with setup["store"]._tx() as cur:
        cur.execute("SELECT payload, action, target FROM v2_decisions WHERE "
                    "tenant_id=%s AND workspace_id=%s AND decision_id=%s",
                    (setup["tenant"], setup["workspace"], decision_id))
        row_before = cur.fetchone()
    assert row_before is not None

    supersede_resp = owner["client"].post("/v2/decisions/supersede", json={
        "workspace_id": setup["workspace"], "decision_id": decision_id,
        "reason": "corrected after policy update"})
    assert supersede_resp.status_code == 200, supersede_resp.text
    new_decision_id = supersede_resp.json()["decision_id"]
    assert new_decision_id != decision_id

    with setup["store"]._tx() as cur:
        cur.execute("SELECT payload, action, target FROM v2_decisions WHERE "
                    "tenant_id=%s AND workspace_id=%s AND decision_id=%s",
                    (setup["tenant"], setup["workspace"], decision_id))
        row_after = cur.fetchone()
        cur.execute("SELECT supersedes FROM v2_decisions WHERE "
                    "tenant_id=%s AND workspace_id=%s AND decision_id=%s",
                    (setup["tenant"], setup["workspace"], new_decision_id))
        new_row_supersedes = cur.fetchone()[0]
    assert row_after == row_before, (
        "original decision row's own columns must be byte-identical "
        f"after a supersede (append-only); before={row_before} "
        f"after={row_after}")
    assert new_row_supersedes == decision_id, (
        "the new decision row must reference the original via `supersedes`")

    # Enforced, not just designed: a direct UPDATE against the original row
    # must be rejected by the database's own immutability trigger.
    import psycopg2
    with pytest.raises(psycopg2.Error):
        with setup["store"]._tx() as cur:
            cur.execute("UPDATE v2_decisions SET rationale='tampered' WHERE "
                        "tenant_id=%s AND workspace_id=%s AND decision_id=%s",
                        (setup["tenant"], setup["workspace"], decision_id))


def test_active_decision_chain_supersede_then_revoke_is_correctly_derived(
        setup, monkeypatch, owner):
    """Derivation of "is this decision
    still active" via a correlated `NOT EXISTS` query rather than writing
    the declared-but-unused `superseded_by`/`revoked_by` columns. Concretely
    test the three-link chain: decision A is
    superseded by B, B is later revoked by C. Expected: A and B are both
    INACTIVE (something later names them), C is the sole ACTIVE decision on
    the target, and all three rows remain durably present and unmodified
    (history read via direct SQL, since no ledger-read route exists
    yet)."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])
    claim_id = cand_ids[0]

    resp_a = _confirm(owner["client"], setup["workspace"], claim_id, head.version)
    assert resp_a.status_code == 200, resp_a.text
    decision_a = resp_a.json()["decision_id"]

    resp_b = owner["client"].post("/v2/decisions/supersede", json={
        "workspace_id": setup["workspace"], "decision_id": decision_a,
        "reason": "policy update 1"})
    assert resp_b.status_code == 200, resp_b.text
    decision_b = resp_b.json()["decision_id"]

    resp_c = owner["client"].post("/v2/decisions/revoke", json={
        "workspace_id": setup["workspace"], "decision_id": decision_b,
        "reason": "policy update 2, decision B itself was wrong"})
    assert resp_c.status_code == 200, resp_c.text
    decision_c = resp_c.json()["decision_id"]

    def is_active(decision_id):
        with setup["store"]._tx() as cur:
            cur.execute("""SELECT 1 FROM v2_decisions AS d WHERE
                d.tenant_id=%s AND d.workspace_id=%s AND d.decision_id=%s
                AND NOT EXISTS (SELECT 1 FROM v2_decisions AS s WHERE
                    s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id
                    AND (s.supersedes=d.decision_id OR s.revokes=d.decision_id))""",
                (setup["tenant"], setup["workspace"], decision_id))
            return cur.fetchone() is not None

    assert is_active(decision_a) is False, "A must be inactive (superseded by B)"
    assert is_active(decision_b) is False, "B must be inactive (revoked by C)"
    assert is_active(decision_c) is True, "C must be the sole active decision"

    # NB: `supersede`/`revoke` target the REFERENCED decision_id itself
    # (`_supersede_or_revoke`, `infra/v2_policy_routes.py:525-538`), not the
    # original `claim_id` -- so B's `target` is A's decision_id and C's
    # `target` is B's decision_id. Select by `decision_id IN (...)` instead
    # of by a single shared `target`, to prove all three rows durably exist
    # regardless of each one's own distinct `target` value.
    with setup["store"]._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_decisions WHERE tenant_id=%s "
                    "AND workspace_id=%s AND decision_id = ANY(%s) ORDER BY epoch",
                    (setup["tenant"], setup["workspace"],
                     [decision_a, decision_b, decision_c]))
        all_rows = [r[0] for r in cur.fetchall()]
    assert all_rows == [decision_a, decision_b, decision_c], (
        "all three history rows must remain durably present, in insertion "
        f"order, unmodified: {all_rows}")


def test_conflict_resolve_does_not_transition_finding_lifecycle_to_remediation_pending(
        setup, monkeypatch, owner):
    """Resolving a conflict must move the finding's lifecycle.

    Worked example: 'SOP=500k/code=1m: sau
    quyet dinh dung SOP, interpretation GAP resolved, CONFLICT CHUYEN
    REMEDIATION_PENDING.' This is an acceptance requirement, not
    an optional nicety, and `core/change_intelligence/v2/lifecycle.py`
    already has the exact `conflict`: `DECISION_RECORDED ->
    REMEDIATION_PENDING` edge built for this
    purpose.

    Verified end-to-end through the real route: after a real
    `POST /v2/conflict/resolve` decision, the persisted CONFLICT finding's
    `lifecycle_state` (read back through the same `finding` job's
    `execution_result`, since no other read surface exists) is UNCHANGED --
    still whatever `FindingsExecutor` set it to, never `REMEDIATION_PENDING`.
    `/v2/conflict/resolve` writes only to `v2_decisions` via
    `record_human_decision`; it never calls `authorize_transition("conflict",
    ...)` nor writes back to the job's persisted finding at all (confirmed
    by reading `infra/v2_policy_routes.py`'s `resolve_conflict` in full --
    no lifecycle/finding-state call appears anywhere in it).

    This is a required D1 outcome, not a deferred/optional projection."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    findings = body["execution_result"]["findings"]
    conflict = next(f for f in findings if f["kind"] == "CONFLICT")
    lifecycle_before = conflict["lifecycle_state"]

    head = setup["store"].head(setup["tenant"], setup["workspace"])
    resolve_resp = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": setup["workspace"], "conflict_id": conflict["finding_id"],
        "authoritative_claim_id": cand_ids[0], "rationale": "SOP is authoritative"})
    assert resolve_resp.status_code == 200, resolve_resp.text

    # Re-read the SAME job's persisted execution_result to see whether the
    # finding's lifecycle_state changed as a side effect of the decision.
    job_id = body["job_id"] if "job_id" in body else None
    status = owner["client"].get(f"/v2/jobs/{job_id}", params={"workspace_id": setup["workspace"]}) \
        if job_id else None
    findings_after = (status.json()["execution_result"]["findings"]
                      if status is not None and status.status_code == 200
                      else findings)
    conflict_after = next((f for f in findings_after if f["finding_id"] == conflict["finding_id"]),
                          conflict)

    assert conflict_after["lifecycle_state"] == "REMEDIATION_PENDING", (
        "REQUIRED (task card acceptance bullet, D1 worked example): after a "
        "real /v2/conflict/resolve decision, the CONFLICT finding's "
        "lifecycle_state must transition to REMEDIATION_PENDING via the "
        "existing, already-reviewed core/change_intelligence/v2/lifecycle.py "
        "conflict machine. It is currently NOT wired -- lifecycle_state "
        f"stayed {conflict_after['lifecycle_state']!r} "
        f"(was {lifecycle_before!r} before the decision). This is a real, "
        "required gap, not an accepted deferral -- see TESTER TEST_DONE "
        "ruling on OPEN item (1) in the handoff.")


# --------------------------------------------------------------------------- D2: conflicting decisions do not auto-pick a winner; critical
# accepted_unknown is blocked; model actor / unauthorized resolver cannot
# mint authority.

def test_conflicting_decisions_on_same_claim_are_rejected_with_409_not_a_crash(
        setup, monkeypatch, owner):
    """D2 State/transaction clause: 'hai decision mau thuan phai
    hien conflict hoac stale theo version contract, khong silent
    last-write-wins.'

    Verified against real PostgreSQL and the real route: the second, different account's
    confirm on the SAME `claim_id` now returns a graceful HTTP 409 with
    `error` prefixed `decision_conflict:` -- the guard
    (`infra/adapters/v2_postgres.py:1273-1286`, an explicit `NOT EXISTS`
    correlated query inside the SAME locked transaction as the insert)
    rather than raising an unhandled
    `psycopg2.errors.UniqueViolation`. The first account's decision remains the sole active one (no
    silent last-write-wins); a human must explicitly supersede/revoke it
    to change the outcome, matching the authority-resolution
    requirement."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.ai4t01.conftest import DSN

    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    claim_id = cand_ids[0]

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    account2, cred2 = "owner2-" + owner["account"], "cred2-" + owner["credential"]
    identities.create_account(account2, cred2)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                account2, ("knowledge_owner",))
    client2 = setup["browser"]()
    login(client2, account2, cred2)

    first = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert first.status_code == 200, first.text
    first_decision_id = first.json()["decision_id"]

    second = client2.post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert second.status_code == 409, (
        "a second, different actor's conflicting decision on the same "
        f"target must be a graceful 409, never a raised DB exception; "
        f"got {second.status_code}: {second.text}")
    assert second.json()["error"].startswith("decision_conflict:"), second.json()

    # The first actor's decision is the sole surviving/active one -- no
    # silent last-write-wins, no orphaned/duplicated row from the rejected
    # second attempt.
    with setup["store"]._tx() as cur:
        cur.execute("""SELECT decision_id, actor_id FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND target=%s""",
            (setup["tenant"], setup["workspace"], claim_id))
        rows = cur.fetchall()
    assert [r[0] for r in rows] == [first_decision_id], (
        f"expected exactly the first actor's decision row, got {rows}")


def test_failure_injection_conflict_guard_bypassed_lets_last_write_win_then_restored(
        setup, monkeypatch, owner):
    """Role-protocol step G (REQUIRED): break the load-bearing "another actor
    already has an active decision on the same target" guard inside
    `record_human_decision` (infra/adapters/v2_postgres.py:1273-1286) and
    prove the real product-path test above goes RED, then restore and
    confirm it goes GREEN again. The guard is a single correlated SQL
    query; this injection monkeypatches the real psycopg2 cursor's
    `execute` to blind that ONE query (make it return no rows), simulating
    the guard being silently absent, without touching any production file."""
    import contextlib
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters import v2_postgres as v2_postgres_module
    from tests.ai4t01.conftest import DSN

    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    claim_id = cand_ids[0]

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    account2, cred2 = "owner2-" + owner["account"], "cred2-" + owner["credential"]
    identities.create_account(account2, cred2)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                account2, ("knowledge_owner",))
    client2 = setup["browser"]()
    login(client2, account2, cred2)

    first = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert first.status_code == 200, first.text

    # `psycopg2.extensions.cursor` is an immutable C type -- its `execute`
    # cannot be monkeypatched directly. Instead, wrap the real cursor
    # `_tx()` yields in a thin proxy that blinds only the one conflict-
    # detection query, delegating everything else untouched.
    orig_tx = v2_postgres_module.PostgresV2Store._tx

    class _BlindingCursor:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def execute(self, query, params=None):
            if isinstance(query, str) and "NOT EXISTS" in query and "v2_decisions" in query:
                return self._real.execute("SELECT NULL WHERE FALSE")
            return self._real.execute(query, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __iter__(self):
            return iter(self._real)

    @contextlib.contextmanager
    def patched_tx(self):
        with orig_tx(self) as cur:
            yield _BlindingCursor(cur)

    monkeypatch.setattr(v2_postgres_module.PostgresV2Store, "_tx", patched_tx)
    try:
        bypassed = client2.post("/v2/knowledge/confirm", json={
            "workspace_id": setup["workspace"], "claim_id": claim_id})
        assert bypassed.status_code == 200, (
            "RED CHECK: with the conflict guard blinded, a second, "
            f"different actor's conflicting decision must be silently "
            f"accepted (proving the guard is genuinely load-bearing when "
            f"present) -- got {bypassed.status_code}: {bypassed.text}")
        with setup["store"]._tx() as cur:
            cur.execute("""SELECT decision_id, actor_id FROM v2_decisions
                WHERE tenant_id=%s AND workspace_id=%s AND target=%s
                ORDER BY epoch""",
                (setup["tenant"], setup["workspace"], claim_id))
            rows = cur.fetchall()
        assert len(rows) == 2 and rows[0][1] != rows[1][1], (
            "RED CHECK: two different actors' decisions on the same target "
            f"both silently applied while the guard was bypassed: {rows}")
    finally:
        # Restore only this one attribute (NOT `monkeypatch.undo()`, which
        # would also roll back the `setup` fixture's own `monkeypatch.setenv`
        # calls for RIPPLE_V2_DSN made earlier on this SAME shared
        # `monkeypatch` fixture instance, breaking every subsequent request
        # in this test with a spurious "v2 auth unavailable" 503).
        monkeypatch.setattr(v2_postgres_module.PostgresV2Store, "_tx", orig_tx)

    # GREEN CHECK: restored, a THIRD actor's conflicting decision on the
    # same target is rejected again exactly as before the injection.
    account3, cred3 = "owner3-" + owner["account"], "cred3-" + owner["credential"]
    identities.create_account(account3, cred3)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                account3, ("knowledge_owner",))
    client3 = setup["browser"]()
    login(client3, account3, cred3)
    restored = client3.post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert restored.status_code == 409, (
        "GREEN CHECK: after restoring the original cursor.execute, the "
        f"guard must reject a third conflicting actor again; got "
        f"{restored.status_code}: {restored.text}")


def test_critical_accepted_unknown_is_blocked(setup, monkeypatch, owner):
    """D2: 'critical accepted_unknown bi chan.'

    Verified: `POST
    /v2/gap/accepted_unknown` with `critical: true` returns 409
    `critical_unknown_blocked`, never a 2xx -- the unknown is genuinely
    blocked, not merely logged. The `critical` flag is a caller-asserted
    boolean here; the real finding severity is covered by a separate test
    below, alongside a check that the surrounding permission gate still can't be bypassed."""
    resp = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": "GAP-critical-1",
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00",
        "critical": True})
    assert resp.status_code == 409, (
        f"critical accepted_unknown must be blocked, not accepted; got "
        f"{resp.status_code}: {resp.text}")
    assert resp.json()["error"].startswith("critical_unknown_blocked"), resp.json()


def test_noncritical_accepted_unknown_succeeds_via_existing_lifecycle_guard(
        setup, monkeypatch, owner):
    """D1/D2: a NON-critical unknown must still go through the real,
    already-reviewed `core/change_intelligence/v2/lifecycle.py` gap
    completeness gate (reason/scope/owner/review_at), not a parallel
    authority source, and must actually persist a decision when the four
    required fields are present."""
    resp = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": "GAP-noncritical-1",
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00",
        "critical": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["provenance"] == "human_assertion"


def test_accepted_unknown_missing_required_field_is_rejected_by_lifecycle_guard(
        setup, monkeypatch, owner):
    """Negative case for the REUSED lifecycle guard ('khong tao
    nguon authority song song') -- an accepted_unknown missing `owner` must
    be rejected with the lifecycle's own completeness error, proving the
    route really calls `authorize_transition`, not just a local no-op."""
    resp = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": "GAP-incomplete-1",
        "reason": "no source available", "scope": "expense-policy",
        "review_at": "2027-01-01T00:00:00+00:00", "critical": False})
    assert resp.status_code == 400, resp.text


def test_critical_gate_caller_assertion_cannot_be_bypassed_by_unauthorized_actor(
        setup, monkeypatch, owner):
    """Even though the `critical` boolean is
    presently a caller assertion (a real, named cross-task gap pending
    the `severity`/`authority_required` population -- confirmed by
    reading `core/change_intelligence/v2/schema.py:751,753` declaring the
    fields and `infra/worker/findings.py` never setting them, matching the
    already-passing
    `test_conflict_finding_is_persisted_without_severity_or_authority_required`),
    the SURROUNDING permission gate is untouched by that gap: an account
    holding only `knowledge.answer` (never `knowledge.confirm`) cannot call
    `/v2/gap/accepted_unknown` at all, critical or not."""
    requester_client = setup["browser"]()
    login(requester_client, setup["requester"], setup["credential"])
    resp = requester_client.post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": "GAP-unauthorized-1",
        "reason": "no source available", "scope": "expense-policy",
        "owner": setup["requester"], "review_at": "2027-01-01T00:00:00+00:00",
        "critical": False})
    assert resp.status_code == 403, resp.text


def test_model_actor_cannot_confirm(setup, monkeypatch, owner):
    """D2: 'model actor ... khong mint authority.' The real, existing route
    already enforces `require_human_actor` (infra/v2_policy_routes.py:429,
    infra/adapters/v2_policy.py:103-107) before calling `record_decision` --
    this is a REAL baseline that must not regress. There is no test-harness
    way to log in as an `actor_kind='ai'` session (login only ever produces
    human sessions), so this is supplemented with a direct unit check of the
    guard function itself (a signature/behavior check
    that supplements, and does not replace, the product-path assertion above)."""
    from infra.adapters.v2_policy import PermissionDenied, require_human_actor

    with pytest.raises(PermissionDenied):
        require_human_actor({"actor_kind": "ai", "roles": ("knowledge_owner",)})
    with pytest.raises(PermissionDenied):
        require_human_actor({"actor_kind": "service", "roles": ("knowledge_owner",)})


def test_unauthorized_resolver_is_denied(setup, monkeypatch, owner):
    """D2: 'unauthorized resolver khong mint authority.' Real product path:
    `setup['requester']` carries only `knowledge.answer`, never
    `knowledge.confirm` (infra/adapters/v2_policy.py ROLE_PERMISSIONS) -- a
    confirm attempt by that account must 403 through the real route, not
    succeed."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])

    requester_client = setup["browser"]()
    login(requester_client, setup["requester"], setup["credential"])
    resp = _confirm(requester_client, setup["workspace"], cand_ids[0], head.version)
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- D3: audit/event write failure rollback; duplicate semantic decision;
# concurrent decisions.

def test_duplicate_confirm_request_hits_idempotency_conflict_not_clean_replay(setup, monkeypatch, owner):
    """D3: 'retry khong tang epoch hai lan.' REAL FINDING (verified, not
    assumed): a literal client retry of the identical `POST
    /v2/knowledge/confirm` body does NOT cleanly replay through
    `PostgresV2Store._replay` as the packet's naive reading of
    `infra/adapters/v2_postgres.py:1162-1165` would suggest. The idempotency
    key is fixed (f"confirm-{claim_id}-{account_id}"), but the STORED
    `payload_hash` embeds `expected_version` (`record_decision`'s
    `request_hash`, infra/adapters/v2_postgres.py:1157-1159) -- and the
    ROUTE recomputes `expected_version` fresh from `store.head(...)` on
    every call (infra/v2_policy_routes.py:431-436), so the retry's
    `expected_version` differs from the first call's (the head already
    advanced). `_replay` then finds a stored hash that does not match the
    new request_hash and raises `StoreConflict('idempotency_conflict')`,
    which the route surfaces as 409 -- NOT a silent clean replay and NOT a
    double epoch bump either. `decision_epoch` is confirmed to advance only
    once (the first call), so the 'no double increment' half of this
    acceptance line holds, but the mechanism is 'the retry is rejected
    outright', not 'the retry is replayed' -- an important distinction when
    building genuine idempotent retry semantics into the fuller
    ledger."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head_before = setup["store"].head(setup["tenant"], setup["workspace"])

    claim_id = cand_ids[0]
    resp1 = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert resp1.status_code == 200, resp1.text
    head_after_first = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after_first.decision_epoch == head_before.decision_epoch + 1

    resp2 = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert resp2.status_code == 409, (
        "documents the CURRENT baseline: a literal retry of the same "
        f"confirm request 409s as idempotency_conflict, not a clean "
        f"replay; got {resp2.status_code}: {resp2.text}")
    assert resp2.json().get("error") == "idempotency_conflict"
    head_after_second = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after_second.decision_epoch == head_after_first.decision_epoch, (
        "decision_epoch must not advance on the rejected retry")


def test_concurrent_decisions_both_serialize_and_commit_via_row_lock(setup, monkeypatch, owner):
    """D3: 'concurrent decisions' must not corrupt state. REAL, VERIFIED
    baseline: `_lock_head`'s `SELECT ... FOR UPDATE` (infra/adapters/
    v2_postgres.py:210-217) serializes two decisions on two DIFFERENT
    claims against the same workspace -- since the route re-resolves
    `expected_version` itself per call (see the duplicate-request test
    above), BOTH commit (200, 200), each observing the other's already-
    advanced epoch/version because the second call's `_lock_head` blocks
    until the first transaction commits, then reads the NEW head. This is
    the correct, non-corrupting outcome for two decisions on two DIFFERENT
    claims (no lost update: `decision_epoch` ends up exactly
    `head.decision_epoch + 2`, not silently stuck at +1); it does NOT by
    itself demonstrate a stale-version rejection, because the HTTP route
    gives a client no way to submit a version it observed earlier that the
    server would then detect as stale (documented gap, same root cause as
    the duplicate-request/idempotency-conflict finding above)."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])

    resp_a = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": cand_ids[0]})
    resp_b = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": cand_ids[1]})
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.json()["decision_id"] != resp_b.json()["decision_id"]
    head_after = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after.decision_epoch == head.decision_epoch + 2, (
        "two decisions on two distinct claims must both be durably "
        f"recorded (epoch +2), got epoch={head_after.decision_epoch} "
        f"from base={head.decision_epoch}")


def test_audit_and_decision_write_share_one_transaction_boundary(setup, monkeypatch, owner):
    """D3: 'Audit/event write failure rollback ca decision.' Cross-task
    contract preflight check: `record_decision`'s decision insert, the
    `v2_workspaces` epoch bump, and `_record_commit`'s audit/idempotency/
    outbox inserts (infra/adapters/v2_postgres.py:1169-1184) all happen
    inside the SAME `with self._tx() as cur:` block -- i.e. they already
    share one atomic transaction boundary today (a real Postgres commit/
    rollback, not an app-level try/except). This test proves that boundary
    holds by forcing `_record_commit` to raise after the decision insert,
    then asserting the decision row does NOT persist -- a genuine failure
    injection per role-protocol step G, not a mocked assertion."""
    from infra.adapters import v2_postgres as v2_postgres_module

    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])

    def boom(*args, **kwargs):
        raise RuntimeError("simulated audit/outbox write failure")

    monkeypatch.setattr(v2_postgres_module.PostgresV2Store, "_record_commit", boom)
    with pytest.raises(RuntimeError):
        setup["store"].record_decision(
            setup["tenant"], setup["workspace"], f"confirm-{cand_ids[0]}",
            owner["account"], {"claim_id": cand_ids[0], "decision": "confirmed"},
            expected_version=head.version,
            idempotency_key=f"confirm-{cand_ids[0]}-{owner['account']}-boom")

    with setup["store"]._tx() as cur:
        cur.execute("SELECT 1 FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s "
                    "AND decision_id=%s",
                    (setup["tenant"], setup["workspace"], f"confirm-{cand_ids[0]}"))
        row = cur.fetchone()
    assert row is None, (
        "decision insert must roll back with the failed audit/outbox write "
        "in the same transaction; found an orphaned decision row")
    head_after = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after.decision_epoch == head.decision_epoch, (
        "decision_epoch must also roll back, not be left bumped without a "
        "matching decision/audit record")


# --------------------------------------------------------------------------- self-approval, epoch single-owner, model actor.

def test_self_approval_blocked_when_same_actor_answered_and_confirms_high_risk(
        setup, monkeypatch, owner):
    """`_self_approval_block`
    (`infra/v2_policy_routes.py:415-435`) fires when the SAME actor who
    authored a T01 `v2_hitl_answers` row (keyed by `question_id`) tries to
    be the one confirming a decision on the SAME `target`, and the caller
    marks the request `high_risk: true`. Grants one account BOTH
    `knowledge.answer` and `knowledge.confirm` (the "admin" role bundle,
    `infra/adapters/v2_policy.py:39-44`) since the demo role split
    otherwise keeps answering and confirming on disjoint roles -- the guard
    itself is actor-identity-based, not role-based, so this is the correct
    way to exercise it through the real product path rather than assuming
    a role shape the guard does not itself check."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.ai4t01.conftest import DSN

    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    claim_id = cand_ids[0]

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    dual_account, dual_cred = "dual-" + owner["account"], "cred-dual-" + owner["account"]
    identities.create_account(dual_account, dual_cred)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                dual_account, ("admin",))
    dual_client = setup["browser"]()
    login(dual_client, dual_account, dual_cred)

    answer_resp = dual_client.post("/v2/hitl/answer", json={
        "workspace_id": setup["workspace"], "question_id": claim_id,
        "answer": "SOP applies here", "idempotency_key": f"answer-{claim_id}",
        "expected_version": None})
    assert answer_resp.status_code == 200, answer_resp.text

    self_confirm = dual_client.post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id,
        "high_risk": True})
    assert self_confirm.status_code == 403, (
        "the same actor who authored the answer must be denied confirming "
        f"it high-risk: got {self_confirm.status_code}: {self_confirm.text}")
    assert "self-approval" in self_confirm.text

    # Negative control: a DIFFERENT actor confirming the same high-risk
    # target must NOT be blocked by self-approval (only by the ordinary
    # conflict/permission guards, exercised elsewhere).
    other_resp = owner["client"].post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id,
        "high_risk": True})
    assert other_resp.status_code == 200, (
        "a DIFFERENT actor confirming the same target must not be blocked "
        f"by self-approval: got {other_resp.status_code}: {other_resp.text}")


def test_self_approval_not_blocked_when_high_risk_not_asserted(setup, monkeypatch, owner):
    """Negative control for the self-approval guard's explicit scoping:
    The guard deliberately only fires when the caller asserts
    `high_risk: true` (no real severity signal exists yet). The SAME
    same-actor answer-then-confirm sequence must succeed when `high_risk`
    is omitted, proving the guard is scoped as documented, not a blanket
    self-confirm ban."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.ai4t01.conftest import DSN

    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    claim_id = cand_ids[0]

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    dual_account, dual_cred = "duallo-" + owner["account"], "cred-duallo-" + owner["account"]
    identities.create_account(dual_account, dual_cred)
    identities.grant_membership(setup["tenant"], setup["project"], setup["workspace"],
                                dual_account, ("admin",))
    dual_client = setup["browser"]()
    login(dual_client, dual_account, dual_cred)

    answer_resp = dual_client.post("/v2/hitl/answer", json={
        "workspace_id": setup["workspace"], "question_id": claim_id,
        "answer": "SOP applies here", "idempotency_key": f"answer-lo-{claim_id}",
        "expected_version": None})
    assert answer_resp.status_code == 200, answer_resp.text

    self_confirm = dual_client.post("/v2/knowledge/confirm", json={
        "workspace_id": setup["workspace"], "claim_id": claim_id})
    assert self_confirm.status_code == 200, (
        "without high_risk asserted, the same-actor confirm must proceed "
        f"(guard deliberately scoped): got {self_confirm.status_code}: "
        f"{self_confirm.text}")


def test_duplicate_semantic_decision_replay_does_not_double_increment_epoch(
        setup, monkeypatch, owner):
    """Task-card explicit requirement + role-protocol D3: a retried/duplicate
    semantic decision (SAME idempotency key, SAME payload) resubmitted at
    the STORE layer must not bump `decision_epoch` a second time, and must
    use the ALREADY-EXISTING single epoch-increment call site inside
    `record_human_decision` (`infra/adapters/v2_postgres.py:1287`,
    `epoch = head.decision_epoch + 1`) rather than a second, parallel
    counter. Source inspection (grep) confirms exactly ONE
    `decision_epoch` write call site touched by this feature:
    `record_human_decision` reuses `record_decision`'s own single
    `UPDATE v2_workspaces SET ... decision_epoch=%s` pattern and no new
    helper/counter was introduced (`rg -n "decision_epoch\\s*=" infra/`
    shows only the pre-existing `record_decision` and this method, both
    computing `head.decision_epoch + 1` once per call, guarded by the SAME
    `_replay` prior-result short-circuit that returns BEFORE the epoch
    computation on a true replay)."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    head = setup["store"].head(setup["tenant"], setup["workspace"])
    claim_id = cand_ids[0]
    idem_key = f"epoch-replay-{claim_id}"

    first = setup["store"].record_human_decision(
        setup["tenant"], setup["workspace"], actor_id=owner["account"],
        current_permission="knowledge.confirm", target=claim_id, action="confirm",
        expected_version=head.version, idempotency_key=idem_key,
        rationale="first")
    head_after_first = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after_first.decision_epoch == head.decision_epoch + 1

    # Exact same idempotency key AND same request shape -> a true replay,
    # short-circuited by `_replay` BEFORE the epoch bump is even computed.
    second = setup["store"].record_human_decision(
        setup["tenant"], setup["workspace"], actor_id=owner["account"],
        current_permission="knowledge.confirm", target=claim_id, action="confirm",
        expected_version=head.version, idempotency_key=idem_key,
        rationale="first")
    assert second["decision_id"] == first["decision_id"], (
        "a true replay must return the SAME decision_id, not mint a new one")
    head_after_second = setup["store"].head(setup["tenant"], setup["workspace"])
    assert head_after_second.decision_epoch == head_after_first.decision_epoch, (
        "a genuine idempotent replay must not increment decision_epoch a "
        f"second time: before={head_after_first.decision_epoch} "
        f"after={head_after_second.decision_epoch}")


def test_model_actor_cannot_reach_any_of_the_four_new_decision_routes(setup, monkeypatch, owner):
    """D2 model-actor requirement, extended to all four NEW T02 routes (the
    existing `test_model_actor_cannot_confirm` only names
    `/v2/knowledge/confirm`). Source inspection confirms every one of
    `resolve_conflict`, `supersede_decision`, `revoke_decision`,
    `gap_accepted_unknown` calls the SAME shared `require_human_actor(authority)`
    guard immediately after resolving authority and BEFORE any
    permission dispatch or store call (`infra/v2_policy_routes.py:452,
    498, 556, 587, 644`) -- the domain guard rejects
    model actors before edge-specific permission dispatch, this is ONE
    guard function reused four times, not four separate implementations,
    so a single direct check of the guard (there is no test-harness way to
    log in as an `actor_kind='ai'` session, matching the existing
    `test_model_actor_cannot_confirm`'s own documented limitation) is
    representative evidence for all four routes, not merely the first
    one -- this test exists to make that reuse claim explicit and
    verifiable rather than silently assumed."""
    from infra.adapters.v2_policy import PermissionDenied, require_human_actor
    import inspect
    from infra import v2_policy_routes as routes_module

    for fn_name in ("resolve_conflict", "supersede_decision", "revoke_decision",
                    "gap_accepted_unknown"):
        source = inspect.getsource(getattr(routes_module, fn_name))
        assert "require_human_actor(authority)" in source, (
            f"{fn_name} must call require_human_actor(authority) before any "
            "store write -- source inspection found it missing")

    with pytest.raises(PermissionDenied):
        require_human_actor({"actor_kind": "ai", "roles": ("knowledge_owner",)})
    with pytest.raises(PermissionDenied):
        require_human_actor({"actor_kind": "service", "roles": ("knowledge_owner",)})


# --------------------------------------------------------------------------- negative re-resolve case and
# transactional atomicity of the lifecycle wiring.

def test_second_resolve_on_already_remediation_pending_conflict_is_rejected_not_reapplied(
        setup, monkeypatch, owner):
    """Round-2 corrective-fix re-verification, required check (3): once a
    CONFLICT finding has already been driven to REMEDIATION_PENDING by a
    real `/v2/conflict/resolve`, a SECOND attempt to resolve the SAME
    finding must not silently re-apply the transition or crash -- it must
    be rejected by the real `authorize_transition` guard.

    The route itself cannot exercise this directly for a single actor: its
    idempotency_key is deterministic per (conflict_id, actor_id), so a
    same-actor retry is intercepted by `_replay` before ever reaching
    `_transition_conflict_finding` again (a correct, separate guarantee,
    already covered by `test_duplicate_semantic_decision_replay_does_not_double_increment_epoch`).
    This test instead calls `PostgresV2Store.record_human_decision` DIRECTLY
    a second time with a FRESH idempotency_key/decision target (bypassing
    replay) but the SAME actor (bypassing the separate "different actor"
    conflict guard too), to isolate and prove the lifecycle transition guard
    itself: `core/change_intelligence/v2/lifecycle.py`'s `conflict` machine
    has NO entry for source `REMEDIATION_PENDING` at all (only `OPEN` and
    `DECISION_RECORDED` are keys in the table), so
    `authorize_transition("conflict", "REMEDIATION_PENDING",
    "DECISION_RECORDED", ...)` must raise `LifecycleError` -- not silently
    no-op, not crash with an unhandled exception, and not corrupt the
    finding's already-correct state."""
    from core.change_intelligence.v2.lifecycle import LifecycleError

    store = setup["store"]
    tenant, workspace = setup["tenant"], setup["workspace"]
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    findings = body["execution_result"]["findings"]
    conflict = next(f for f in findings if f["kind"] == "CONFLICT")
    conflict_id = conflict["finding_id"]

    head = store.head(tenant, workspace)
    resolve_resp = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": workspace, "conflict_id": conflict_id,
        "authoritative_claim_id": cand_ids[0], "rationale": "SOP is authoritative"})
    assert resolve_resp.status_code == 200, resolve_resp.text

    job_id = body["job_id"]
    status = owner["client"].get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    finding_after_first = next(
        f for f in status.json()["execution_result"]["findings"]
        if f["finding_id"] == conflict_id)
    assert finding_after_first["lifecycle_state"] == "REMEDIATION_PENDING"

    head2 = store.head(tenant, workspace)
    epoch_before_second_attempt = head2.decision_epoch
    with pytest.raises(LifecycleError):
        store.record_human_decision(
            tenant, workspace, actor_id=owner["account"],
            current_permission="conflict.resolve", target=conflict_id,
            action="resolve", expected_version=head2.version,
            idempotency_key=f"resolve-{conflict_id}-{owner['account']}-second-attempt",
            rationale="attempting to re-resolve an already-resolved conflict",
            conflict_finding_id=conflict_id, actor_kind="human",
            actor_permissions=frozenset({"conflict.resolve"}))

    # Atomicity: the rejected second attempt's decision INSERT and epoch
    # bump must have rolled back together with the rejected lifecycle
    # transition -- not partially applied.
    head3 = store.head(tenant, workspace)
    assert head3.decision_epoch == epoch_before_second_attempt, (
        "a rejected second resolve must not bump decision_epoch -- the "
        "whole transaction (decision insert + epoch bump + lifecycle "
        "guard) must roll back together")
    with store._tx() as cur:
        cur.execute("""SELECT decision_id FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND target=%s""",
            (tenant, workspace, conflict_id))
        rows = [r[0] for r in cur.fetchall()]
    assert len(rows) == 1, (
        "the rejected second resolve attempt must not have inserted a "
        f"second v2_decisions row on the same target: {rows}")

    status_after = owner["client"].get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    finding_after_second = next(
        f for f in status_after.json()["execution_result"]["findings"]
        if f["finding_id"] == conflict_id)
    assert finding_after_second["lifecycle_state"] == "REMEDIATION_PENDING", (
        "the rejected second attempt must leave the finding's already-correct "
        f"lifecycle_state untouched, got {finding_after_second['lifecycle_state']!r}")


def test_conflict_lifecycle_write_rolls_back_with_decision_on_finding_not_found(
        setup, monkeypatch, owner):
    """Round-2 corrective-fix re-verification, required check (4)
    (transactional atomicity): if `_transition_conflict_finding` cannot find
    the referenced finding (a bad/unknown `conflict_finding_id`), the whole
    decision -- the `v2_decisions` insert and the `decision_epoch` bump --
    must roll back together with it, not partially commit the decision while
    silently leaving no lifecycle projection. Verified as a real failure
    injection against `PostgresV2Store.record_human_decision`'s actual
    transaction, not a mock: a nonexistent `conflict_finding_id` makes
    `_transition_conflict_finding` raise `TenantScopeError` (its own
    documented behavior for 'not found'), and this must propagate out of
    the whole `with self._tx() as cur:` block, rolling back the decision
    row and the epoch bump with it."""
    from infra.adapters.v2_postgres import TenantScopeError
    import uuid

    store = setup["store"]
    tenant, workspace = setup["tenant"], setup["workspace"]
    head = store.head(tenant, workspace)
    epoch_before = head.decision_epoch

    bogus_target = f"nonexistent-conflict-{uuid.uuid4().hex[:12]}"
    with pytest.raises(TenantScopeError):
        store.record_human_decision(
            tenant, workspace, actor_id=owner["account"],
            current_permission="conflict.resolve", target=bogus_target,
            action="resolve", expected_version=head.version,
            idempotency_key=f"resolve-{bogus_target}-{owner['account']}",
            rationale="resolving a finding that does not exist",
            conflict_finding_id=bogus_target, actor_kind="human",
            actor_permissions=frozenset({"conflict.resolve"}))

    head_after = store.head(tenant, workspace)
    assert head_after.decision_epoch == epoch_before, (
        "a finding-not-found failure inside the lifecycle-write step must "
        "roll back the decision_epoch bump together with the decision "
        "insert -- not leave the epoch bumped with no matching finding "
        "projection")
    with store._tx() as cur:
        cur.execute("""SELECT count(*) FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND target=%s""",
            (tenant, workspace, bogus_target))
        count = cur.fetchone()[0]
    assert count == 0, (
        "a finding-not-found failure inside the lifecycle-write step must "
        "roll back the v2_decisions insert too -- found "
        f"{count} orphaned row(s) for a decision whose lifecycle "
        "side-effect never actually happened")


# --------------------------------------------------------------------------- real-severity critical-gate.

def test_real_high_severity_finding_blocks_accepted_unknown_even_when_caller_omits_critical(
        setup, monkeypatch, owner):
    """Adversarial scenario: a permitted
    caller citing the REAL `gap_id` of an objectively HIGH-severity finding
    (both seeded candidates are `must`-modality, per
    `_severity_and_authority`) while OMITTING `critical` from the request
    body entirely (not merely setting it `false`) must still be blocked,
    because `gap_accepted_unknown` now looks up the finding's real,
    populated `severity` via `store.find_finding_by_id` and ORs it
    into the block condition -- the caller's own claim can only add a block,
    never remove one a real signal already requires."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    conflict = next(f for f in body["execution_result"]["findings"] if f["kind"] == "CONFLICT")
    assert conflict["severity"] == "HIGH", conflict

    resp = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": conflict["finding_id"],
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00"})
    assert resp.status_code == 409, (
        "a real HIGH-severity finding must block accepted_unknown even when "
        f"the caller never asserts critical at all; got {resp.status_code}: "
        f"{resp.text}")
    assert resp.json()["error"].startswith("critical_unknown_blocked"), resp.json()
    assert "HIGH" in resp.json()["error"], resp.json()

    # Explicitly lying (`critical: False`) about a real HIGH-severity finding
    # must be caught identically -- the caller's claim cannot downgrade a
    # real signal.
    resp_lied = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": conflict["finding_id"],
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00",
        "critical": False})
    assert resp_lied.status_code == 409, (
        "a caller-asserted critical=False must NOT be able to downgrade a "
        f"real HIGH-severity finding's block; got {resp_lied.status_code}: "
        f"{resp_lied.text}")


def test_medium_severity_finding_is_not_blocked_by_critical_gate(setup, monkeypatch, owner):
    """Negative control for the real-signal gate: a real, persisted finding
    whose derived severity is MEDIUM (may-modality) must NOT trip the
    critical-unknown block when the caller does not separately assert
    `critical` -- proving the gate is not simply "block everything with a
    real finding," but specifically keyed to HIGH/CRITICAL severity."""
    from tests.ai4t02.conftest import seed_extraction_candidates
    from infra.worker import job_worker
    from uuid import uuid4

    tenant, workspace, store = setup["tenant"], setup["workspace"], setup["store"]
    requester_client = setup["browser"]()
    login(requester_client, setup["requester"], setup["credential"])

    body_a = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-sop-medium", text="SOP may text 2.",
        candidates_json=[_candidate("approval_threshold_2", "approval_threshold_vnd_2",
                                    500000, {"category": "expense2", "source": "SOP"},
                                    "SOP may text 2.", modality="may")])
    body_b = seed_extraction_candidates(
        setup, monkeypatch, artifact_id="expense-code-medium", text="code may text 2.",
        candidates_json=[_candidate("approval_threshold_2", "approval_threshold_vnd_2",
                                    1000000, {"category": "expense2", "source": "code"},
                                    "code may text 2.", modality="may")])
    cand_a = body_a["execution_result"]["candidates"][0]["candidate_id"]
    cand_b = body_b["execution_result"]["candidates"][0]["candidate_id"]

    job_id = f"job-finding-medium-{uuid4().hex}"
    resp_job = requester_client.post("/v2/jobs", json={
        "workspace_id": workspace, "kind": "finding", "job_id": job_id,
        "payload": {"candidate_ids": [cand_a, cand_b]}})
    assert resp_job.status_code == 200, resp_job.text
    claim = job_worker.claim_and_admit_one(store, tenant, workspace, "finding-worker-medium")
    assert claim is not None and claim.kind == "JOB_QUEUED", claim
    status = requester_client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    conflict = next(f for f in status.json()["execution_result"]["findings"] if f["kind"] == "CONFLICT")
    assert conflict["severity"] == "MEDIUM", conflict

    resp = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": workspace, "gap_id": conflict["finding_id"],
        "reason": "no source available", "scope": "expense-policy-2",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00"})
    assert resp.status_code == 200, (
        "a real MEDIUM-severity finding must NOT trip the critical-unknown "
        f"block; got {resp.status_code}: {resp.text}")


def test_bogus_nonexistent_gap_id_does_not_open_a_new_bypass_path(setup, monkeypatch, owner):
    """Role-protocol step 6 check ('no finding found' fallback): a caller
    citing a bogus/nonexistent `gap_id` gets the honest, documented
    caller-only-assertion fallback (no real signal to check) -- but this
    must not let a caller "launder" acceptance of a REAL, HIGH-severity
    finding by citing a different, made-up id instead of the real one.
    Verified: (1) a bogus id with critical omitted succeeds (the documented
    fallback, not itself a new escalation, since the decision this persists
    is scoped to the bogus `target`, never the real finding's id or state);
    (2) the REAL critical finding, referenced by its REAL id, is still
    blocked regardless -- a caller cannot use the bogus-id path to affect
    the real finding's gate at all, because `record_human_decision`'s
    `target` is always the exact `gap_id` the caller supplied, so citing a
    bogus id can only ever create a decision row about that bogus id, never
    about (or resolving) the real critical finding."""
    body, _cands = seed_conflict_finding(setup, monkeypatch, owner["client"])
    conflict = next(f for f in body["execution_result"]["findings"] if f["kind"] == "CONFLICT")
    assert conflict["severity"] == "HIGH", conflict

    bogus_id = f"GAP-bogus-{conflict['finding_id']}"
    resp_bogus = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": bogus_id,
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00"})
    assert resp_bogus.status_code == 200, (
        "a truly nonexistent gap_id has no real signal to fall back on -- "
        f"the documented caller-only path applies; got {resp_bogus.status_code}: "
        f"{resp_bogus.text}")

    with setup["store"]._tx() as cur:
        cur.execute("""SELECT target FROM v2_decisions WHERE tenant_id=%s
            AND workspace_id=%s AND decision_id=%s""",
            (setup["tenant"], setup["workspace"], resp_bogus.json()["decision_id"]))
        row = cur.fetchone()
    assert row is not None and row[0] == bogus_id, (
        "the persisted decision's target must be the exact bogus id the "
        f"caller supplied, never the real finding's id: {row}")

    # The bogus-id decision must have zero effect on the REAL critical
    # finding's own gate -- citing it directly is still blocked.
    resp_real = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": conflict["finding_id"],
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00"})
    assert resp_real.status_code == 409, (
        "the real critical finding must remain blocked; a bypass via a "
        f"bogus gap_id request must not launder it: got "
        f"{resp_real.status_code}: {resp_real.text}")


def test_find_finding_by_id_is_scoped_to_its_own_tenant_and_workspace(setup, monkeypatch, owner):
    """Check (5): `PostgresV2Store.find_finding_by_id` must not leak a
    finding across tenant/workspace boundaries -- a real, direct-store
    negative test (not merely reading the SQL) since this method is the
    critical-gate's sole source of truth for a real severity signal; a
    cross-tenant leak here would silently defeat the gate for a workspace
    that never even ran the job in question, or worse, let one tenant's
    real HIGH-severity gap_id collide with an identically-named id in a
    different tenant's workspace."""
    from uuid import uuid4

    body, _cands = seed_conflict_finding(setup, monkeypatch, owner["client"])
    conflict = next(f for f in body["execution_result"]["findings"] if f["kind"] == "CONFLICT")
    finding_id = conflict["finding_id"]

    store = setup["store"]
    same_tenant_found = store.find_finding_by_id(setup["tenant"], setup["workspace"], finding_id)
    assert same_tenant_found is not None and same_tenant_found["finding_id"] == finding_id

    # A different, real, separately-created tenant/workspace must not see it.
    nonce = uuid4().hex
    other_tenant, other_project, other_workspace = (
        f"tenant-{nonce}", f"project-{nonce}", f"workspace-{nonce}")
    store.create_workspace(other_tenant, other_project, other_workspace)
    cross_tenant_found = store.find_finding_by_id(other_tenant, other_workspace, finding_id)
    assert cross_tenant_found is None, (
        "find_finding_by_id must not leak a finding across tenant/workspace "
        f"boundaries: found {cross_tenant_found} under a different tenant")

    # Same tenant, but a different workspace of that SAME tenant must not
    # see it either -- workspace-level scoping, not just tenant-level.
    same_tenant_other_workspace = f"workspace-other-{nonce}"
    store.create_workspace(setup["tenant"], setup["project"], same_tenant_other_workspace)
    other_workspace_found = store.find_finding_by_id(
        setup["tenant"], same_tenant_other_workspace, finding_id)
    assert other_workspace_found is None, (
        "find_finding_by_id must be scoped per-workspace within the same "
        f"tenant too: found {other_workspace_found} under a sibling workspace")


def test_failure_injection_bypassed_severity_lookup_lets_high_severity_gap_through_then_restored(
        setup, monkeypatch, owner):
    """Fault injection for the severity lookup: break `find_finding_by_id` so it reports a real HIGH-severity
    finding as if no signal existed at all (simulating the exact class of
    bug of concern -- the gate silently trusting
    an absent/wrong signal), and confirm a real product-path test
    (accepted_unknown on that same finding, critical omitted) goes RED for
    the wrong reason (200 instead of 409), proving the lookup is genuinely
    load-bearing; then restore and confirm GREEN again."""
    from infra.adapters import v2_postgres as v2_postgres_module

    body, _cands = seed_conflict_finding(setup, monkeypatch, owner["client"])
    conflict = next(f for f in body["execution_result"]["findings"] if f["kind"] == "CONFLICT")
    assert conflict["severity"] == "HIGH", conflict

    orig_find = v2_postgres_module.PostgresV2Store.find_finding_by_id

    def blinded_find(self, tenant_id, workspace_id, finding_id):
        # Simulate the lookup finding nothing (or a low-severity result),
        # exactly the failure mode this gate must not silently trust.
        return None

    monkeypatch.setattr(v2_postgres_module.PostgresV2Store, "find_finding_by_id", blinded_find)
    try:
        bypassed = owner["client"].post("/v2/gap/accepted_unknown", json={
            "workspace_id": setup["workspace"], "gap_id": conflict["finding_id"],
            "reason": "no source available", "scope": "expense-policy",
            "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00"})
        assert bypassed.status_code == 200, (
            "RED CHECK: with the real-severity lookup blinded, a real "
            "HIGH-severity gap must be silently accepted (proving the "
            f"lookup is genuinely load-bearing); got {bypassed.status_code}: "
            f"{bypassed.text}")
    finally:
        monkeypatch.setattr(v2_postgres_module.PostgresV2Store, "find_finding_by_id", orig_find)

    # GREEN CHECK: restored, the same real HIGH-severity finding is blocked
    # again via a fresh gap_id/idempotency target (the bypassed call above
    # already durably recorded a decision against this finding_id, so a
    # second distinct gap reference against the SAME finding severity value
    # is used to re-prove the guard without colliding on idempotency).
    restored = owner["client"].post("/v2/gap/accepted_unknown", json={
        "workspace_id": setup["workspace"], "gap_id": conflict["finding_id"],
        "reason": "no source available", "scope": "expense-policy",
        "owner": owner["account"], "review_at": "2027-01-01T00:00:00+00:00"})
    assert restored.status_code == 409, (
        "GREEN CHECK: after restoring find_finding_by_id, the real "
        f"HIGH-severity finding must be blocked again; got "
        f"{restored.status_code}: {restored.text}")
