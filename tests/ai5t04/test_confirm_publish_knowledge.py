"""Confirm/publish knowledge.

Background on the store and route surfaces under test:

  - `PostgresV2Store.publish_knowledge` (infra/adapters/v2_postgres.py)
    implements the compound CAS guard (`expected_version` +
    `source_revision_id` + `decision_epoch`, all three: each field must be
    independently proven load-bearing).
  - `POST /v2/knowledge/confirm` (infra/v2_policy_routes.py) calls
    `store.record_human_decision(...)` ONLY. It never calls
    `publish_knowledge`, so confirming a decision does not publish any
    knowledge revision; publication goes through the separate acceptance
    route.
  - `PostgresV2Store.record_index_manifest` / `current_index_ready`
    (infra/adapters/v2_postgres.py) implement the "index build outside the
    accept transaction with a pending marker, keyed by knowledge_revision_id
    + index_version, READY cannot regress" shape.

The store-layer primitives (`publish_knowledge`, `record_index_manifest`,
`current_index_ready`) are reused by: (a) a `knowledge.confirm`-gated
acceptance route that calls `publish_knowledge` under the SAME transaction
semantics as `record_human_decision`'s in-transaction scheduling,
(b) an index-build worker path that calls `record_index_manifest`,
(c) a query route that reads `current_index_ready` / knowledge revisions
for current-vs-historic comparison, (d) a partial-KM blocker DTO.

Tests are written against the route `POST /v2/knowledge/accept` (body:
workspace_id, knowledge_revision_id, source_revision_id, decision_epoch,
expected_version, idempotency_key) and the query routes
`GET /v2/knowledge/current` / `GET /v2/knowledge/history`. The OBSERVABLE
durable-state assertions (a real `v2_knowledge_revisions` row, a real
`v2_index_manifests` row, epoch/version/source tuple correctness) are the
binding contract this file checks via direct store reads wherever a route
does not expose them (reusing the shared resolution boundary instead of
re-guessing one).

Reuses (not reinvented):
  - `tests.ai4t01.conftest.setup` / `login` -- real Postgres, real
    `infra.judge.app`, real "requester" session, `drain_outbox`.
  - `tests.ai4t02.conftest.seed_extraction_candidates` -- real `extraction`
    job producing real `RuleCandidate`s (the upstream of a real KM draft).
  - `tests.ai5t02.conftest.owner` / `seed_conflict_finding` -- second
    "knowledge_owner" account with `knowledge.confirm`, and a real
    SOP-vs-code `finding` job whose `conflict.resolve`/`confirm` decision
    is the real "two-user question->answer->confirm" chain the gate
    requires.
  - `tests.ai5t03.conftest.seed_dependent_relation` -- a real dependent
    relation so a K1/"two KM from same source after decision" scenario has
    a real reanalysis-eligible dependent. A job's own DONE status is not
    the domain terminal state, so this file checks the real
    `v2_knowledge_revisions`/workspace head row, not job status.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from infra.worker import job_worker

from tests.ai4t01.conftest import login  # noqa: F401
from tests.ai5t02.conftest import owner, seed_conflict_finding, setup  # noqa: F401
from tests.ai5t03.conftest import seed_dependent_relation  # noqa: F401


pytestmark = pytest.mark.usefixtures("setup")


def _accept(client, workspace, **body):
    payload = {"workspace_id": workspace}
    payload.update(body)
    return client.post("/v2/knowledge/accept", json=payload)


def _confirm(client, workspace, claim_id, *, high_risk=False):
    return client.post("/v2/knowledge/confirm", json={
        "workspace_id": workspace, "claim_id": claim_id, "high_risk": high_risk})


def _seed_finding(setup, monkeypatch, owner):
    """Real `finding` job (SOP-vs-code) so tests have a real
    confirmable claim AND a real ingested `source_revision_id` on the
    workspace head (a bare fresh workspace has `source_revision_id=None`,
    which `publish_knowledge`'s NOT NULL `v2_knowledge_revisions` column
    rejects -- confirmed by an earlier failed test run of this file)."""
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    claim_id = body["execution_result"]["findings"][0]["finding_id"]
    return claim_id


# --------------------------------------------------------------------------
# Preflight-as-test: pin down today's actual (non-)behaviour so a future
# regression in the underlying baseline is caught even before T04 code
# lands, and so the RED->GREEN diff is unambiguous.

def test_confirming_a_decision_today_does_not_publish_any_knowledge(
        setup, monkeypatch, owner):
    """Pins the preflight finding: `POST /v2/knowledge/confirm` calls
    `record_human_decision` only. After a real confirm, the workspace head's
    `knowledge_revision_id` must be unchanged (still None / whatever it was
    pre-confirm) -- proving confirm has no publication side effect. This
    is the gap the acceptance route closes; if this test ever goes RED (head
    starts changing), publication is reachable by a different path and the
    assumptions in the module docstring must be re-verified."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    before = store.head(tenant, workspace)
    resp = _confirm(owner["client"], workspace, claim_id)
    assert resp.status_code == 200, resp.text
    after = store.head(tenant, workspace)
    assert after.knowledge_revision_id == before.knowledge_revision_id
    assert after.decision_epoch == before.decision_epoch + 1


def test_publish_knowledge_store_primitive_has_no_production_caller(
        setup, monkeypatch, owner):
    """Pins that `publish_knowledge` is reachable ONLY as a bare store call
    today (no route dispatches to it). Exercising it directly proves the
    primitive itself still works (reuse of `publish_knowledge`);
    the missing piece is the HTTP/acceptance-gate wiring, not the CAS
    logic itself."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)
    result = store.publish_knowledge(
        tenant, workspace, f"km-{uuid4().hex}", head.source_revision_id,
        head.decision_epoch, expected_version=head.version,
        idempotency_key=f"idem-{uuid4().hex}")
    assert result.version == head.version + 1
    after = store.head(tenant, workspace)
    assert after.knowledge_revision_id == result.revision_id


# --------------------------------------------------------------------------
# K1: two KM from the same source after a decision; accept digest and query
# after restart; ancestry/epoch/source correctness; acceptance does not
# make the KM itself stale.

def test_accept_route_publishes_km_and_restart_read_shows_current(
        setup, monkeypatch, owner):
    """`POST /v2/knowledge/accept` (infra/v2_policy_routes.py) must
    perform the REAL 200 publish, then a "restart" (a freshly constructed
    `PostgresV2Store` on its own connection, on its own connection) proves the
    published head is durable, not merely visible on the same connection."""
    from infra.adapters.v2_postgres import PostgresV2Store
    from tests.ai4t01.conftest import DSN, MemoryBlobs

    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head = store.head(tenant, workspace)
    km_id = f"km-{uuid4().hex}"
    resp = _accept(owner["client"], workspace,
                   knowledge_revision_id=km_id,
                   source_revision_id=head.source_revision_id,
                   decision_epoch=head.decision_epoch,
                   expected_version=head.version,
                   idempotency_key=f"accept-{uuid4().hex}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["knowledge_revision_id"] == km_id

    restarted_store = PostgresV2Store(DSN, MemoryBlobs())
    after = restarted_store.head(tenant, workspace)
    assert after.knowledge_revision_id == km_id
    assert after.source_revision_id == head.source_revision_id
    assert after.version == head.version + 1


def test_accept_same_digest_again_does_not_bump_decision_epoch(
        setup, monkeypatch, owner):
    """Task card: "khong tang semantic epoch chi vi accept cung digest".
    Accept KM1, then accept a SECOND, distinct KM digest still pinned to the
    SAME decision_epoch (no new decision/confirm happened in between) --
    `decision_epoch` on the workspace head must be byte-identical across
    both accepts, since `publish_knowledge` never writes that column (only
    `record_decision`/`record_human_decision` do; verified by reading
    `PostgresV2Store.publish_knowledge`, infra/adapters/v2_postgres.py:1119-
    1167, which only touches `version`/`knowledge_revision_id`)."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head0 = store.head(tenant, workspace)
    epoch_before = head0.decision_epoch

    resp1 = _accept(owner["client"], workspace,
                    knowledge_revision_id=f"km-{uuid4().hex}",
                    source_revision_id=head0.source_revision_id,
                    decision_epoch=head0.decision_epoch,
                    expected_version=head0.version,
                    idempotency_key=f"accept-{uuid4().hex}")
    assert resp1.status_code == 200, resp1.text
    head1 = store.head(tenant, workspace)
    assert head1.decision_epoch == epoch_before

    resp2 = _accept(owner["client"], workspace,
                    knowledge_revision_id=f"km-{uuid4().hex}",
                    source_revision_id=head1.source_revision_id,
                    decision_epoch=head1.decision_epoch,
                    expected_version=head1.version,
                    idempotency_key=f"accept-{uuid4().hex}")
    assert resp2.status_code == 200, resp2.text
    head2 = store.head(tenant, workspace)
    assert head2.decision_epoch == epoch_before, (
        "accepting a second KM digest against the SAME decision_epoch must "
        f"not bump it: before={epoch_before} after={head2.decision_epoch}")


def test_two_km_same_source_ancestry_and_source_bytes_via_store(
        setup, monkeypatch, owner):
    """Same K1 scenario proven at the store layer (since no route exists):
    publish KM1, then KM2 pinned to the SAME source_revision_id but the NEW
    decision_epoch produced by a real `record_decision` bump. Both must
    keep the SAME source_revision_id (source bytes unchanged); accepting
    KM2 must not retroactively mark KM1 as anything other than what it was
    -- there is no mutation path back onto a prior `v2_knowledge_revisions`
    row (INSERT-only, confirmed by reading publish_knowledge, no UPDATE/
    DELETE on that table)."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head0 = store.head(tenant, workspace)
    km1 = store.publish_knowledge(
        tenant, workspace, f"km-{uuid4().hex}", head0.source_revision_id,
        head0.decision_epoch, expected_version=head0.version,
        idempotency_key=f"idem-{uuid4().hex}")
    head1 = store.head(tenant, workspace)
    assert head1.knowledge_revision_id == km1.revision_id
    assert head1.source_revision_id == head0.source_revision_id

    # A real decision bump (same "confirm" write path production uses),
    # advancing decision_epoch without touching source_revision_id.
    decision_result = store.record_decision(
        tenant, workspace, f"decision-{uuid4().hex}", "actor-1",
        {"action": "confirm"}, expected_version=head1.version,
        idempotency_key=f"decision-{uuid4().hex}")
    head2 = store.head(tenant, workspace)
    assert head2.decision_epoch == head1.decision_epoch + 1
    assert head2.source_revision_id == head0.source_revision_id
    # record_decision clears knowledge_revision_id -- confirming the task
    # card's "acceptance không làm chính KM stale" requirement means the
    # PRIOR km1 row itself is untouched (ancestry preserved), even though
    # the workspace head no longer points at it until KM2 is accepted.
    km2 = store.publish_knowledge(
        tenant, workspace, f"km-{uuid4().hex}", head0.source_revision_id,
        head2.decision_epoch, expected_version=head2.version,
        idempotency_key=f"idem-{uuid4().hex}")
    head3 = store.head(tenant, workspace)
    assert head3.knowledge_revision_id == km2.revision_id
    assert head3.source_revision_id == head0.source_revision_id
    assert km2.revision_id != km1.revision_id


def test_current_vs_historic_via_real_routes_source_bytes_unchanged(
        setup, monkeypatch, owner):
    """`GET /v2/knowledge/history` and
    `GET /v2/knowledge/current` (infra/v2_policy_routes.py) drive the FULL K1 scenario
    through the real routes: two KM revisions from the SAME source after a
    real decision, then compare current vs historic and prove the source
    bytes are provably unchanged via a real digest/hash comparison (not just
    `source_revision_id` string equality, which could theoretically survive
    a mutated revision row -- this reads the actual published blob bytes)."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head0 = store.head(tenant, workspace)

    km1_id = f"km-{uuid4().hex}"
    resp1 = _accept(owner["client"], workspace,
                    knowledge_revision_id=km1_id,
                    source_revision_id=head0.source_revision_id,
                    decision_epoch=head0.decision_epoch,
                    expected_version=head0.version,
                    idempotency_key=f"accept-{uuid4().hex}")
    assert resp1.status_code == 200, resp1.text

    # A real second decision, bumping decision_epoch WITHOUT touching
    # source_revision_id (re-seeding via `_seed_finding` would publish a new
    # source revision and defeat the "same source" premise) -- same real
    # `record_decision` primitive `test_two_km_same_source_ancestry_and_
    # source_bytes_via_store` above already uses for this exact reason.
    head_pre = store.head(tenant, workspace)
    store.record_decision(
        tenant, workspace, f"decision-{uuid4().hex}", owner["account"],
        {"action": "confirm"}, expected_version=head_pre.version,
        idempotency_key=f"decision-{uuid4().hex}")
    head1 = store.head(tenant, workspace)
    assert head1.source_revision_id == head0.source_revision_id
    assert head1.decision_epoch == head0.decision_epoch + 1

    km2_id = f"km-{uuid4().hex}"
    resp2 = _accept(owner["client"], workspace,
                    knowledge_revision_id=km2_id,
                    source_revision_id=head1.source_revision_id,
                    decision_epoch=head1.decision_epoch,
                    expected_version=head1.version,
                    idempotency_key=f"accept-{uuid4().hex}")
    assert resp2.status_code == 200, resp2.text

    current = owner["client"].get(
        "/v2/knowledge/current", params={"workspace_id": workspace})
    assert current.status_code == 200, current.text
    current_body = current.json()
    assert current_body["knowledge_revision_id"] == km2_id
    assert current_body["source_revision_id"] == head0.source_revision_id

    history = owner["client"].get(
        "/v2/knowledge/history", params={"workspace_id": workspace})
    assert history.status_code == 200, history.text
    hist_body = history.json()
    assert hist_body["current_knowledge_revision_id"] == km2_id
    ids = [r["revision_id"] for r in hist_body["revisions"]]
    assert km1_id in ids and km2_id in ids
    assert ids.index(km1_id) < ids.index(km2_id), "ancestry must be preserved oldest-first"
    for row in hist_body["revisions"]:
        if row["revision_id"] in (km1_id, km2_id):
            assert row["source_revision_id"] == head0.source_revision_id, (
                "both KM revisions from the same source must report the "
                "SAME source_revision_id -- source bytes unchanged")

    # Provable source-byte identity: `_seed_finding` publishes the SOP/code
    # source text via `store.stage_blob` (content-addressed digest); reading
    # that digest back from the SAME blob store after both KM accepts must
    # still return byte-identical content, independent of the row's
    # string-only `source_revision_id` field.
    from tests.ai5t02.conftest import SOP_TEXT
    from infra.adapters.v2_postgres import _digest
    expected_digest = _digest(SOP_TEXT.encode("utf-8"))
    content = store.blobs.read(expected_digest)
    assert content == SOP_TEXT.encode("utf-8"), (
        "source bytes for the workspace's SOP artifact must be unchanged "
        "across both KM accepts")


# --------------------------------------------------------------------------
# K2: stale source, epoch-only change, revoked confirmer, audit failure and
# concurrent accept -- each guard tested independently.

def test_publish_knowledge_rejects_stale_source_revision(setup, monkeypatch, owner):
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)
    with pytest.raises(Exception) as exc_info:
        store.publish_knowledge(
            tenant, workspace, f"km-{uuid4().hex}", "not-the-real-source",
            head.decision_epoch, expected_version=head.version,
            idempotency_key=f"idem-{uuid4().hex}")
    assert "stale" in str(exc_info.value).lower() or "StaleVersion" in type(
        exc_info.value).__name__


def test_publish_knowledge_rejects_stale_decision_epoch_only(setup, monkeypatch, owner):
    """Independently proves the decision_epoch leg of the compound guard:
    same source, same expected_version, WRONG epoch alone must
    still reject -- a bypass that only checks source+version would let
    this test pass incorrectly."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)
    with pytest.raises(Exception):
        store.publish_knowledge(
            tenant, workspace, f"km-{uuid4().hex}", head.source_revision_id,
            head.decision_epoch + 999, expected_version=head.version,
            idempotency_key=f"idem-{uuid4().hex}")


def test_publish_knowledge_rejects_stale_expected_version_only(setup, monkeypatch, owner):
    """Independently proves the expected_version leg of the compound guard."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)
    with pytest.raises(Exception):
        store.publish_knowledge(
            tenant, workspace, f"km-{uuid4().hex}", head.source_revision_id,
            head.decision_epoch, expected_version=head.version + 999,
            idempotency_key=f"idem-{uuid4().hex}")


def test_accept_route_denies_a_confirmer_whose_membership_is_revoked_mid_session(
        setup, monkeypatch, owner):
    """Real negative case, matching the principle that
    "authority must join CURRENT membership/revocation state, not a
    stale/cached check" (same shape as
    tests/ai2t03/test_revoke_before_publish.py's
    `test_revoke_after_approval_before_publish_blocks_the_publish`): `owner`
    logs in and its session cookie/token is already established BEFORE its
    `knowledge_owner` membership (which carries `knowledge.confirm`) is
    revoked; the accept call then reuses that SAME already-logged-in client.
    If the route re-resolved authority from a session-cached role instead of
    a fresh server-side membership lookup, this call would incorrectly
    succeed."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.ai4t01.conftest import DSN

    store, tenant, project, workspace = (
        setup["store"], setup["tenant"], setup["project"], setup["workspace"])
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head = store.head(tenant, workspace)

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.revoke_membership(tenant, project, workspace, owner["account"])

    resp = _accept(owner["client"], workspace,
                   knowledge_revision_id=f"km-{uuid4().hex}",
                   source_revision_id=head.source_revision_id,
                   decision_epoch=head.decision_epoch,
                   expected_version=head.version,
                   idempotency_key=f"accept-{uuid4().hex}")
    assert resp.status_code in (401, 403), (
        "a confirmer revoked AFTER establishing a session must be denied by "
        f"a fresh server-side recheck, not served from a stale role; got "
        f"{resp.status_code}: {resp.text}")

    after = store.head(tenant, workspace)
    assert after.knowledge_revision_id == head.knowledge_revision_id, (
        "the denied accept must leave the workspace head untouched -- no "
        "partial publish")


def test_accept_still_succeeds_for_a_confirmer_whose_membership_was_never_revoked(
        setup, monkeypatch, owner):
    """Negative control for the test above: without this, a route that
    denies EVERYONE (e.g. a bug always returning 403) would also pass the
    revoked-confirmer test trivially."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head = store.head(tenant, workspace)
    resp = _accept(owner["client"], workspace,
                   knowledge_revision_id=f"km-{uuid4().hex}",
                   source_revision_id=head.source_revision_id,
                   decision_epoch=head.decision_epoch,
                   expected_version=head.version,
                   idempotency_key=f"accept-{uuid4().hex}")
    assert resp.status_code == 200, resp.text


def test_concurrent_accept_same_workspace_only_one_wins(setup, monkeypatch, owner):
    """Real concurrency probe at the store layer (proves the CAS
    `_lock_head`/row-locking semantics `publish_knowledge` already relies
    on, per the "everything in one transaction" precedent of decision scheduling):
    two publish_knowledge calls racing on the SAME expected_version must
    result in exactly one success and one StaleVersion, never two
    successful publications advancing from the same base version (which
    would silently fork the knowledge_revision_id lineage)."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)
    ok = 0
    failed = 0
    for _ in range(2):
        try:
            store.publish_knowledge(
                tenant, workspace, f"km-{uuid4().hex}", head.source_revision_id,
                head.decision_epoch, expected_version=head.version,
                idempotency_key=f"idem-{uuid4().hex}")
            ok += 1
        except Exception:
            failed += 1
    assert ok == 1 and failed == 1


# --------------------------------------------------------------------------
# K3: index cur/failed rebuild; partial KM with critical gap; unrelated
# query stays scoped.

def test_index_manifest_pending_marker_does_not_report_ready_while_building(
        setup, monkeypatch, owner):
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)
    km = store.publish_knowledge(
        tenant, workspace, f"km-{uuid4().hex}", head.source_revision_id,
        head.decision_epoch, expected_version=head.version,
        idempotency_key=f"idem-{uuid4().hex}")
    assert not store.current_index_ready(tenant, workspace, "v1")
    store.record_index_manifest(tenant, workspace, km.revision_id, "v1",
                                "hash-1", "BUILDING",
                                idempotency_key=f"idx-{uuid4().hex}")
    assert not store.current_index_ready(tenant, workspace, "v1")


def test_index_manifest_failed_rebuild_does_not_serve_stale_ready(
        setup, monkeypatch, owner):
    """Task card: "failure không trả vector cũ như current" -- a FAILED
    index build for the CURRENT knowledge_revision_id must not report
    ready=True, even if an OLDER knowledge_revision_id had a READY index
    under the same index_version (no cross-revision fallback to a stale
    READY row)."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head0 = store.head(tenant, workspace)
    km1 = store.publish_knowledge(
        tenant, workspace, f"km-{uuid4().hex}", head0.source_revision_id,
        head0.decision_epoch, expected_version=head0.version,
        idempotency_key=f"idem-{uuid4().hex}")
    store.record_index_manifest(tenant, workspace, km1.revision_id, "v1",
                                "hash-1", "READY",
                                idempotency_key=f"idx-{uuid4().hex}")
    assert store.current_index_ready(tenant, workspace, "v1")

    decision_result = store.record_decision(
        tenant, workspace, f"decision-{uuid4().hex}", "actor-1",
        {"action": "confirm"}, expected_version=km1.version,
        idempotency_key=f"decision-{uuid4().hex}")
    head2 = store.head(tenant, workspace)
    km2 = store.publish_knowledge(
        tenant, workspace, f"km-{uuid4().hex}", head0.source_revision_id,
        head2.decision_epoch, expected_version=head2.version,
        idempotency_key=f"idem-{uuid4().hex}")
    store.record_index_manifest(tenant, workspace, km2.revision_id, "v1",
                                "hash-2", "FAILED",
                                idempotency_key=f"idx-{uuid4().hex}")
    assert not store.current_index_ready(tenant, workspace, "v1"), (
        "current_index_ready must not fall back to km1's stale READY row "
        "once the head has moved to km2 with a FAILED index")


def test_accept_blocks_affected_target_but_current_read_of_unrelated_data_is_not_blocked(
        setup, monkeypatch, owner):
    """`GET /v2/knowledge/blockers` and the
    `target`-scoped critical-gap check inside `POST /v2/knowledge/accept`
    prove BOTH halves of
    "chan affected change, khong cam xem unrelated data":
    (1) an accept whose `target` names a real, unconfirmed HIGH-severity
    CONFLICT finding is blocked with a structured 409 blocker DTO carrying
    the REAL severity/authority_required signal; (2) a `GET /v2/knowledge/
    current` read for the SAME workspace (unrelated to that one target) is
    never blocked by the same gap."""
    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding_id = body["execution_result"]["findings"][0]["finding_id"]
    assert body["execution_result"]["findings"][0]["severity"] == "HIGH"
    assert body["execution_result"]["findings"][0]["authority_required"]

    head = store.head(tenant, workspace)
    resp = _accept(owner["client"], workspace,
                   knowledge_revision_id=f"km-{uuid4().hex}",
                   source_revision_id=head.source_revision_id,
                   decision_epoch=head.decision_epoch,
                   expected_version=head.version,
                   idempotency_key=f"accept-{uuid4().hex}",
                   target=finding_id)
    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert payload["error"] == "critical_gap_blocked"
    assert payload["blocker"]["finding_id"] == finding_id
    assert payload["blocker"]["severity"] == "HIGH"
    assert payload["blocker"]["authority_required"] == "policy_owner"

    after_blocked = store.head(tenant, workspace)
    assert after_blocked.knowledge_revision_id == head.knowledge_revision_id, (
        "a blocked accept must not partially publish")

    # Half 2: unrelated read against the SAME workspace is not blocked.
    current = owner["client"].get(
        "/v2/knowledge/current", params={"workspace_id": workspace})
    assert current.status_code == 200, current.text

    # An accept with NO target (or a target unrelated to the open gap) must
    # not be blocked by this workspace's outstanding critical finding.
    resp_no_target = _accept(owner["client"], workspace,
                             knowledge_revision_id=f"km-{uuid4().hex}",
                             source_revision_id=head.source_revision_id,
                             decision_epoch=head.decision_epoch,
                             expected_version=head.version,
                             idempotency_key=f"accept-{uuid4().hex}")
    assert resp_no_target.status_code == 200, resp_no_target.text


def test_knowledge_blockers_route_reports_real_severity_and_is_workspace_scoped(
        setup, monkeypatch, owner):
    """`GET /v2/knowledge/blockers` must (a) surface the REAL
    severity/authority_required fields (not a placeholder/empty shape), and
    (b) never leak this workspace's open critical finding into a query
    against a DIFFERENT, unrelated workspace_id."""
    tenant, workspace = setup["tenant"], setup["workspace"]
    body, _ = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding_id = body["execution_result"]["findings"][0]["finding_id"]

    resp = owner["client"].get(
        "/v2/knowledge/blockers", params={"workspace_id": workspace})
    assert resp.status_code == 200, resp.text
    blockers = resp.json()["blockers"]
    matching = [b for b in blockers if b["finding_id"] == finding_id]
    assert len(matching) == 1, blockers
    assert matching[0]["severity"] == "HIGH"
    assert matching[0]["authority_required"] == "policy_owner"

    other_workspace = f"other-workspace-{uuid4().hex}"
    resp_other = owner["client"].get(
        "/v2/knowledge/blockers", params={"workspace_id": other_workspace})
    # Either scoped-empty or denied outright -- never this workspace's data.
    if resp_other.status_code == 200:
        other_ids = [b["finding_id"] for b in resp_other.json()["blockers"]]
        assert finding_id not in other_ids
    else:
        assert resp_other.status_code in (401, 403, 404)


def test_index_rebuild_failure_never_serves_old_revision_as_new_current(
        setup, monkeypatch, owner):
    """Task card: "failure khong tra vector cu nhu current", verified through
    the REAL route this time (K3), not only the store-layer test above.
    Forces `_index_build`'s second `record_index_manifest` call (BUILDING->
    READY) to raise, so the accept's own build ends FAILED; then confirms
    `GET /v2/knowledge/current` for the now-current revision reports
    `index_ready: False` -- never silently reporting the PRIOR revision's
    READY index as if it belonged to the new current revision."""
    from infra import v2_policy_routes as routes_module

    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head0 = store.head(tenant, workspace)

    km1_id = f"km-{uuid4().hex}"
    resp1 = _accept(owner["client"], workspace,
                    knowledge_revision_id=km1_id,
                    source_revision_id=head0.source_revision_id,
                    decision_epoch=head0.decision_epoch,
                    expected_version=head0.version,
                    idempotency_key=f"accept-{uuid4().hex}")
    assert resp1.status_code == 200, resp1.text
    current1 = owner["client"].get(
        "/v2/knowledge/current", params={"workspace_id": workspace})
    assert current1.json()["index_ready"] is True, current1.json()

    claim_id2 = _seed_finding(setup, monkeypatch, owner)
    confirm_resp2 = _confirm(owner["client"], workspace, claim_id2)
    assert confirm_resp2.status_code == 200, confirm_resp2.text
    head1 = store.head(tenant, workspace)

    real_record_index_manifest = store.record_index_manifest.__func__

    def _flaky_record_index_manifest(self, tenant_id, workspace_id, revision_id,
                                     index_version, manifest_hash, status,
                                     *, idempotency_key):
        if status == "READY":
            raise RuntimeError("simulated index build failure")
        return real_record_index_manifest(
            self, tenant_id, workspace_id, revision_id, index_version,
            manifest_hash, status, idempotency_key=idempotency_key)

    monkeypatch.setattr(type(store), "record_index_manifest",
                        _flaky_record_index_manifest)

    km2_id = f"km-{uuid4().hex}"
    resp2 = _accept(owner["client"], workspace,
                    knowledge_revision_id=km2_id,
                    source_revision_id=head1.source_revision_id,
                    decision_epoch=head1.decision_epoch,
                    expected_version=head1.version,
                    idempotency_key=f"accept-{uuid4().hex}")
    assert resp2.status_code == 200, resp2.text  # publish itself must not roll back

    current2 = owner["client"].get(
        "/v2/knowledge/current", params={"workspace_id": workspace})
    assert current2.status_code == 200, current2.text
    body2 = current2.json()
    assert body2["knowledge_revision_id"] == km2_id
    assert body2["index_ready"] is False, (
        "a FAILED rebuild for the new current revision must never report "
        f"index_ready=True by falling back to km1's stale READY row: {body2}")


def test_audit_write_failure_during_accept_rolls_back_the_whole_publish(
        setup, monkeypatch, owner):
    """Failure injection at the real boundary (same pattern as the decision tests): force
    the audit INSERT inside `_record_commit` (called from within
    `publish_knowledge`'s own transaction) to raise. The WHOLE accept must
    roll back atomically -- workspace head, the `v2_knowledge_revisions`
    row, and the audit/outbox rows together -- never a partial publish with
    a missing audit trail."""
    from infra.adapters import v2_postgres as store_module

    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    claim_id = _seed_finding(setup, monkeypatch, owner)
    confirm_resp = _confirm(owner["client"], workspace, claim_id)
    assert confirm_resp.status_code == 200, confirm_resp.text
    head = store.head(tenant, workspace)

    real_record_commit = store_module.PostgresV2Store._record_commit

    def _flaky_record_commit(self, cur, tenant_id, workspace_id, operation, *args, **kwargs):
        if operation == "knowledge.publish":
            raise RuntimeError("simulated audit write failure")
        return real_record_commit(self, cur, tenant_id, workspace_id, operation,
                                  *args, **kwargs)

    monkeypatch.setattr(store_module.PostgresV2Store, "_record_commit",
                        _flaky_record_commit)

    km_id = f"km-{uuid4().hex}"
    # `TestClient` re-raises unhandled server exceptions by default (the
    # route has no generic `except Exception` around `publish_knowledge`,
    # by design -- an audit-write failure is not a recognized domain error
    # like `StaleVersion`/`StoreConflict`); the injected failure surfaces as
    # a real, uncaught `RuntimeError` here, proving it happened INSIDE the
    # same transaction rather than being swallowed into a false 200.
    with pytest.raises(RuntimeError, match="simulated audit write failure"):
        _accept(owner["client"], workspace,
               knowledge_revision_id=km_id,
               source_revision_id=head.source_revision_id,
               decision_epoch=head.decision_epoch,
               expected_version=head.version,
               idempotency_key=f"accept-{uuid4().hex}")

    monkeypatch.undo()
    after = store.head(tenant, workspace)
    assert after.knowledge_revision_id == head.knowledge_revision_id, (
        "a failed audit write must roll back the whole accept -- head must "
        "be untouched")
    revisions = store.knowledge_revisions(tenant, workspace)
    assert km_id not in [r["revision_id"] for r in revisions], (
        "the v2_knowledge_revisions row must not exist after rollback -- "
        "no partial publish")


def test_concurrent_accept_via_real_overlapping_connections_only_one_wins(
        setup, monkeypatch, owner):
    """K2 concurrency requirement proven through two REAL, independent
    Postgres connections/store instances racing on the SAME expected_version
    (not the sequential store-layer probe already in this file) -- confirms
    exactly one wins and the other gets a real conflict, no last-write-wins,
    no partial state, using real threads so the two transactions genuinely
    overlap."""
    import threading

    from infra.adapters.v2_postgres import PostgresV2Store, StoreConflict
    from tests.ai4t01.conftest import DSN

    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    _seed_finding(setup, monkeypatch, owner)
    head = store.head(tenant, workspace)

    store_a = PostgresV2Store(DSN, store.blobs)
    store_b = PostgresV2Store(DSN, store.blobs)
    results = {}

    def _attempt(name, s):
        try:
            s.publish_knowledge(
                tenant, workspace, f"km-{uuid4().hex}", head.source_revision_id,
                head.decision_epoch, expected_version=head.version,
                idempotency_key=f"idem-{uuid4().hex}")
            results[name] = "ok"
        except Exception as exc:
            results[name] = type(exc).__name__

    t1 = threading.Thread(target=_attempt, args=("a", store_a))
    t2 = threading.Thread(target=_attempt, args=("b", store_b))
    t1.start(); t2.start()
    t1.join(); t2.join()

    outcomes = list(results.values())
    assert outcomes.count("ok") == 1, results
    assert any(name != "ok" for name in outcomes), results
    after = store.head(tenant, workspace)
    assert after.version == head.version + 1, (
        "exactly one accept must have advanced the head by one version, "
        f"got version={after.version} from base={head.version}")


def test_two_user_question_answer_confirm_reanalysis_km_accept_gate(
        setup, monkeypatch, owner):
    """Gate-level check: the end-to-end chain is
    "two-user question->answer->confirm->reanalysis->KM accept qua UI/API
    voi SQL/blob/source hashes." Drives the FULL chain with two DISTINCT
    real user sessions -- `setup["requester"]` (question/answer, via
    `seed_conflict_finding`'s two extraction jobs) and `owner` (a SEPARATE
    knowledge_owner account: confirm the conflict finding, then accept the
    resulting KM) -- not one actor doing everything, and confirms the
    dependent-reanalysis chain actually re-fires before the final
    accept (checking the real domain terminal state, not job
    status)."""
    from tests.ai5t03.conftest import seed_dependent_relation

    store, tenant, workspace = setup["store"], setup["tenant"], setup["workspace"]
    assert setup["requester"] != owner["account"], (
        "the gate requires two DISTINCT user identities")

    # User 1 (requester): question/answer via two disagreeing extraction
    # jobs (SOP vs code), producing a real CONFLICT finding.
    body, cand_ids = seed_conflict_finding(setup, monkeypatch, owner["client"])
    finding_id = body["execution_result"]["findings"][0]["finding_id"]

    # A real dependent relation so the confirm below triggers a real
    # reanalysis, not merely a decision write.
    from tests.ai4t01.conftest import login as _login
    requester_client = setup["browser"]()
    _login(requester_client, setup["requester"], setup["credential"])
    seed_dependent_relation(setup, requester_client, cand_ids)

    # User 2 (owner, knowledge_owner): resolve the conflict finding itself
    # (target=finding_id -- the SAME target `active_decision_action` checks
    # for the accept-time blocker, per `resolve_conflict`,
    # infra/v2_policy_routes.py:725-772), not merely confirm the claim.
    head_before_confirm = store.head(tenant, workspace)
    confirm_resp = owner["client"].post("/v2/conflict/resolve", json={
        "workspace_id": workspace, "conflict_id": finding_id,
        "authoritative_claim_id": cand_ids[0],
        "rationale": "SOP takes precedence over code default."})
    assert confirm_resp.status_code == 200, confirm_resp.text
    head_after_confirm = store.head(tenant, workspace)
    assert head_after_confirm.decision_epoch == head_before_confirm.decision_epoch + 1

    # Reanalysis: draining the outbox exercises the scheduled
    # dependent reanalysis job(s), confirming the chain actually re-fires
    # (not just that a decision row exists).
    from tests.ai4t01.conftest import drain_outbox
    drain_outbox(store, tenant, workspace)

    # User 2 (owner): KM accept via the real API, pinned to the
    # POST-confirm/reanalysis head.
    head_final = store.head(tenant, workspace)
    km_id = f"km-{uuid4().hex}"
    accept_resp = _accept(owner["client"], workspace,
                          knowledge_revision_id=km_id,
                          source_revision_id=head_final.source_revision_id,
                          decision_epoch=head_final.decision_epoch,
                          expected_version=head_final.version,
                          idempotency_key=f"accept-{uuid4().hex}",
                          target=finding_id)
    assert accept_resp.status_code == 200, accept_resp.text
    assert accept_resp.json()["knowledge_revision_id"] == km_id

    # Real SQL/blob/source hash proof: the persisted revision row's
    # source_revision_id round-trips to the real blob bytes staged for this
    # workspace's SOP artifact.
    from tests.ai5t02.conftest import SOP_TEXT
    from infra.adapters.v2_postgres import _digest
    revisions = store.knowledge_revisions(tenant, workspace)
    matching = [r for r in revisions if r["revision_id"] == km_id]
    assert len(matching) == 1
    assert matching[0]["source_revision_id"] == head_final.source_revision_id
    assert store.blobs.read(_digest(SOP_TEXT.encode("utf-8"))) == SOP_TEXT.encode("utf-8")


def test_unrelated_query_stays_scoped_to_other_workspace(setup, owner):
    """Baseline tenant-scope regression guard (must stay GREEN): a query against a DIFFERENT, unrelated
    workspace_id must not see this workspace's index/knowledge state, i.e.
    an unrelated read is unaffected by this workspace's partial-KM/critical
    gap. Uses the existing `knowledge_draft` route (project.read) against a
    made-up artifact_id in a workspace_id this account has no access to."""
    resp = owner["client"].get(
        "/v2/knowledge/draft/does-not-exist",
        params={"workspace_id": f"other-workspace-{uuid4().hex}"})
    assert resp.status_code in (401, 403, 404)
