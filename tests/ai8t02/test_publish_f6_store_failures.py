"""Deterministic store-read failures at the apply
gate end in a named terminal FAILED with ZERO state written; transient store
errors stay retryable.

Every scenario goes through the real chain: POST /v2/jobs -> claim_and_admit_one
-> ApplyExecutor.execute -> job_worker finalize -> GET /v2/jobs/{id}. Durable
state is read from SQL (`store.head`, `publish_footprint`: revisions, the
`apply.publish` ledger, SOURCE_PUBLISHED audit + outbox rows), never from the
job row alone. Corruptions are applied to durable state (the blob store, the
head, the idempotency ledger) or, where the product cannot legitimately reach
the state (a workspace row cannot be deleted: FK from jobs), by an in-test
injection at the store seam. infra/** is never edited.

Classes:
  * translated (must be FAILED + named reason + no state + event acked, not
    redelivered): target_artifact_missing, workspace_not_found,
    target_blob_integrity, prior_publish_unreadable, staged_blob_unverifiable,
    staged_snapshot_invalid -- for the manual branch AND the typed path where
    both reach the site.
  * transient (OSError / psycopg2.OperationalError raised by the store read):
    must NOT become a terminal failure; the job is not FAILED, no state is
    written, and after lease expiry the redelivery succeeds.
"""
from __future__ import annotations

from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from infra.adapters.v2_postgres import TenantScopeError
from infra.worker import apply as apply_mod
from infra.worker import job_worker
from tests.ai4t01.conftest import drain_outbox, publish_with_chunk
from tests.ai7t01.conftest import get_job_status
from tests.ai8t01.conftest import submit_apply_job
from tests.ai8t02.conftest import (
    build_ready_to_publish_yaml, neutralize_f7, publish_footprint, reason_of)
from tests.ai8t02.test_publish_f4_pending_and_scope import _blob_hash, _client
from tests.ai8t02.test_publish_f5_manual_work import _bundle, _state


def _make(kind, setup, client, monkeypatch, owner):
    if kind == "manual":
        return _bundle(setup, client)
    return build_ready_to_publish_yaml(setup, client, setup["workspace"], monkeypatch, owner)


def _submit(setup, client, b):
    job_id = f"job-apply-{uuid4().hex}"
    assert submit_apply_job(client, setup["workspace"], approval_job_id=b["approval_job_id"],
                            job_id=job_id, expected_approval_digest=b["digest"]).status_code == 200
    return job_id


def _outbox_status(setup, event_id):
    with setup["store"]._tx() as cur:
        cur.execute("SELECT status FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND id=%s",
                    (setup["tenant"], setup["workspace"], event_id))
        return cur.fetchone()[0]


def _expire_claimed(setup):
    with setup["store"]._tx() as cur:
        cur.execute("UPDATE v2_outbox SET lease_until=now()-interval '5 seconds' "
                    "WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'",
                    (setup["tenant"], setup["workspace"]))


def _claim(setup, worker):
    return job_worker.claim_and_admit_one(
        setup["store"], setup["tenant"], setup["workspace"], worker)


def _assert_clean_failure(setup, client, job_id, claim, reason, before):
    body = get_job_status(client, setup["workspace"], job_id)
    assert body["execution_status"] == "FAILED", body
    assert reason in reason_of(body), (reason, body)
    assert _state(setup) == before, "state written by a failed apply"
    assert _outbox_status(setup, claim.id) == "DONE", "event not acked: it would be redelivered"
    # not redelivered, even if a lease were somehow still open
    _expire_claimed(setup)
    assert _claim(setup, "f6-redelivery-probe") is None, "failed job redelivered"
    assert get_job_status(client, setup["workspace"], job_id)["execution_status"] == "FAILED"


KINDS = ["manual", "typed"]


# ------------------------------------------------------------------ target absent

@pytest.mark.parametrize("kind", KINDS)
def test_f6_target_missing_named_failure_zero_state_not_redelivered(
        setup, monkeypatch, owner, kind):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _make(kind, setup, client, monkeypatch, owner)
    publish_with_chunk(store, t, ws, artifact_id=f"replacement-{uuid4().hex[:6]}.md", text="x: 1\n")
    drain_outbox(store, t, ws)
    # F7 moved the stale-tuple gate in front of these reads: a target that is
    # absent/unparseable in the CURRENT head is only reachable by moving the head,
    # which F7 now refuses first (`approval_stale_source`, pinned in
    # test_publish_f7_stale_approval.py). This test's subject is the F6 guard, so
    # the F7 comparison is neutralised in-test only.
    neutralize_f7(monkeypatch)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f6-missing")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "target_artifact_missing", before)


@pytest.mark.parametrize("kind", KINDS)
def test_f6_missing_target_guard_red_restore_green(setup, monkeypatch, owner, kind):
    """INJECT-red: the translation neutralised in-test -> the raw TenantScopeError
    escapes the worker and the job is not FAILED (RUNNING/stuck). Restore -> the
    same job, redelivered after lease expiry, ends FAILED target_artifact_missing."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _make(kind, setup, client, monkeypatch, owner)
    publish_with_chunk(store, t, ws, artifact_id=f"replacement-{uuid4().hex[:6]}.md", text="x: 1\n")
    drain_outbox(store, t, ws)
    # F7 moved the stale-tuple gate in front of these reads: a target that is
    # absent/unparseable in the CURRENT head is only reachable by moving the head,
    # which F7 now refuses first (`approval_stale_source`, pinned in
    # test_publish_f7_stale_approval.py). This test's subject is the F6 guard, so
    # the F7 comparison is neutralised in-test only.
    neutralize_f7(monkeypatch)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    with monkeypatch.context() as mp:
        mp.setattr(apply_mod, "_read_artifact",
                   lambda ctx, tn, w, rev, aid, **k: ctx.store.read_artifact_blob(tn, w, rev, aid))
        with pytest.raises(TenantScopeError):
            _claim(setup, "f6-red")
        red = get_job_status(client, ws, job_id)
        assert red["execution_status"] not in ("FAILED", "COMPLETED"), red  # stuck (RUNNING)
        assert _state(setup) == before
    # restore (monkeypatch context exited) -> green
    _expire_claimed(setup)
    claim = _claim(setup, "f6-green")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "target_artifact_missing", before)


# ------------------------------------------------------------------ workspace vanished

def test_f6_workspace_row_cannot_be_deleted_so_head_seam_is_injected(setup):
    """Documents why workspace_not_found is injected: jobs/revisions FK the
    workspace, so a durable delete is refused by the database."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    publish_with_chunk(store, t, ws, artifact_id="anchor.md", text="x: 1\n")
    with pytest.raises(psycopg2.Error):
        with store._tx() as cur:
            cur.execute("DELETE FROM v2_workspaces WHERE tenant_id=%s AND workspace_id=%s", (t, ws))
    store.head(t, ws)  # still there
    assert client is not None


@pytest.mark.parametrize("kind", KINDS)
def test_f6_workspace_not_found_named_failure_zero_state(setup, monkeypatch, owner, kind):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _make(kind, setup, client, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    real = store.head
    armed = {"on": True}

    def _head(tn, w):
        if armed["on"]:
            raise TenantScopeError("workspace not found")
        return real(tn, w)

    monkeypatch.setattr(store, "head", _head)
    claim = _claim(setup, "f6-ws")
    armed["on"] = False
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "workspace_not_found", before)


# ------------------------------------------------------------------ blob corruption

@pytest.mark.parametrize("damage", ["corrupt", "missing"])
@pytest.mark.parametrize("kind", KINDS)
def test_f6_target_blob_damaged_named_failure_zero_state(setup, monkeypatch, owner, kind, damage):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _make(kind, setup, client, monkeypatch, owner)
    drain_outbox(store, t, ws)
    head = store.head(t, ws)
    h = _blob_hash(store, t, ws, head.source_revision_id, b["target_artifact_id"])
    job_id = _submit(setup, client, b)
    good = store.blobs.data[h]
    if damage == "corrupt":
        store.blobs.data[h] = b"garbage-not-matching-digest"
    else:
        del store.blobs.data[h]
    before = _state(setup)
    claim = _claim(setup, "f6-blob")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "target_blob_integrity", before)
    # restore -> a fresh apply over the same approval is no longer blocked by integrity
    store.blobs.data[h] = good
    job2 = _submit(setup, client, b)
    claim2 = _claim(setup, "f6-blob-green")
    assert claim2 is not None
    assert get_job_status(client, ws, job2)["execution_status"] == "COMPLETED"


# ------------------------------------------------------------------ prior publish ledger

def _insert_ledger(setup, job_id, result):
    with setup["store"]._tx() as cur:
        cur.execute("""INSERT INTO v2_idempotency
            (tenant_id, workspace_id, operation, idempotency_key, payload_hash, result)
            VALUES (%s,%s,'apply.publish',%s,%s,%s)""",
                    (setup["tenant"], setup["workspace"], f"job-apply-publish-{job_id}",
                     "corrupt", Json(result)))


@pytest.mark.parametrize("variant", ["ghost_revision", "revision_without_target"])
def test_f6_prior_publish_unreadable_named_failure_zero_state(
        setup, monkeypatch, owner, variant):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = build_ready_to_publish_yaml(setup, client, ws, monkeypatch, owner)
    if variant == "ghost_revision":
        rev = "rev-ghost-does-not-exist"
    else:
        res = publish_with_chunk(store, t, ws, artifact_id=f"other-{uuid4().hex[:6]}.md",
                                 text="x: 1\n")
        rev = store.head(t, ws).source_revision_id
        assert res is not None or rev
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    _insert_ledger(setup, job_id, {"version": 99, "revision_id": rev})
    before = _state(setup)
    claim = _claim(setup, "f6-prior")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "prior_publish_unreadable", before)


@pytest.mark.xfail(strict=True, reason=(
    "PROBE (observation, not a product path): a structurally malformed ledger row "
    "(result without revision_id) makes prior_publish raise KeyError, which is not "
    "translated. The ledger is only written by _record_commit with a fixed shape."))
def test_f6_probe_malformed_ledger_row_is_translated(setup, monkeypatch, owner):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = build_ready_to_publish_yaml(setup, client, ws, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    _insert_ledger(setup, job_id, {})
    before = _state(setup)
    claim = _claim(setup, "f6-malformed")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "prior_publish_unreadable", before)


# ------------------------------------------------------------------ staged blob / snapshot

@pytest.mark.parametrize("mode", ["checksum", "missing"])
def test_f6_staged_blob_unverifiable_named_failure_zero_state(setup, monkeypatch, owner, mode):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = build_ready_to_publish_yaml(setup, client, ws, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    saved_blobs = dict(store.blobs.data)  # the proposal may already have staged this digest
    real_put = store.blobs.put_immutable

    def _bad_put(digest, content):
        if mode == "checksum":
            store.blobs.data[digest] = b"corrupt-after-put"
        else:  # "missing": drop the write (and any earlier copy of this digest)
            store.blobs.data.pop(digest, None)

    with monkeypatch.context() as mp:
        mp.setattr(store.blobs, "put_immutable", _bad_put)
        claim = _claim(setup, "f6-staged")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "staged_blob_unverifiable", before)
    # restore -> green
    store.blobs.data.clear()
    store.blobs.data.update(saved_blobs)
    store.blobs.put_immutable = real_put
    job2 = _submit(setup, client, b)
    assert _claim(setup, "f6-staged-green") is not None
    body = get_job_status(client, ws, job2)
    assert body["execution_status"] == "COMPLETED", reason_of(body)
    assert body["execution_result"]["published"] is True
    assert store.head(t, ws).version == before[0].version + 1


def test_f6_staged_snapshot_invalid_named_failure_zero_state(setup, monkeypatch, owner):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = build_ready_to_publish_yaml(setup, client, ws, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    real = apply_mod._yaml_typed_edits

    with monkeypatch.context() as mp:
        mp.setattr(apply_mod, "_yaml_typed_edits",
                   lambda text, edits: (real(text, edits)[0], False, "a: [unclosed\n"))
        claim = _claim(setup, "f6-snapshot")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "staged_snapshot_invalid", before)
    job2 = _submit(setup, client, b)
    assert _claim(setup, "f6-snapshot-green") is not None
    assert get_job_status(client, ws, job2)["execution_status"] == "COMPLETED"


# ------------------------------------------------------------------ stale-source content (legit drift)

@pytest.mark.parametrize("content", [b"a: [unclosed\n", b"\xff\xfe\x00binary", b"- just\n- a list\n"])
def test_f6_target_republished_with_unparseable_content_fails_clean(
        setup, monkeypatch, owner, content):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = build_ready_to_publish_yaml(setup, client, ws, monkeypatch, owner)
    revision_id = f"rev-{uuid4().hex}"
    digest = store.stage_blob(content)
    store.publish_source(t, ws, revision_id, {b["target_artifact_id"]: digest},
                         expected_version=store.head(t, ws).version,
                         operation="source.import.commit", idempotency_key=uuid4().hex)
    drain_outbox(store, t, ws)
    # F7 moved the stale-tuple gate in front of these reads: a target that is
    # absent/unparseable in the CURRENT head is only reachable by moving the head,
    # which F7 now refuses first (`approval_stale_source`, pinned in
    # test_publish_f7_stale_approval.py). This test's subject is the F6 guard, so
    # the F7 comparison is neutralised in-test only.
    neutralize_f7(monkeypatch)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f6-drift")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "malformed_patch", before)


# ------------------------------------------------------------------ transient errors stay retryable

def _transient_exc(name):
    return {"oserror": OSError("simulated transient I/O"),
            "operational": psycopg2.OperationalError("simulated connection loss")}[name]


@pytest.mark.parametrize("exc_name", ["oserror", "operational"])
@pytest.mark.parametrize("seam", ["read_artifact_blob", "head", "prior_publish"])
@pytest.mark.parametrize("kind", KINDS)
def test_f6_transient_store_error_is_not_terminal_and_redelivery_succeeds(
        setup, monkeypatch, owner, kind, seam, exc_name):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _make(kind, setup, client, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    real = getattr(store, seam)
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _transient_exc(exc_name)
        return real(*a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(store, seam, _flaky)
        with pytest.raises((OSError, psycopg2.OperationalError)):
            _claim(setup, "f6-transient")
    mid = get_job_status(client, ws, job_id)
    assert mid["execution_status"] not in ("FAILED", "COMPLETED"), (
        "transient error turned into a terminal state", mid)
    assert _state(setup) == before
    # redelivery once the lease lapses
    _expire_claimed(setup)
    claim = _claim(setup, "f6-redelivery")
    assert claim is not None
    done = get_job_status(client, ws, job_id)
    assert done["execution_status"] == "COMPLETED", done
    assert _outbox_status(setup, claim.id) == "DONE"
    after_head, after_fp = _state(setup)
    if kind == "typed":
        assert after_head.version == before[0].version + 1
        assert after_fp["apply_publish_keys"] == before[1]["apply_publish_keys"] + 1
        assert after_fp["revisions"] == before[1]["revisions"] + 1
    else:
        assert (after_head, after_fp) == before
    # nothing further to run: the only pending event (typed) is the durable
    # SOURCE_PUBLISHED notification, and no JOB_QUEUED is left for this job
    while True:
        extra = _claim(setup, "f6-after")
        if extra is None:
            break
        assert kind == "typed", extra
        payload = extra.payload if isinstance(extra.payload, dict) else {}
        if extra.kind == "JOB_QUEUED":
            # The durable SOURCE_PUBLISHED notification is consumed
            # by the dispatcher itself, which creates the post-publish stages
            # (verify-/rebuild-<apply>). Those are the ONLY other queued work
            # allowed; the apply job itself must never be queued again.
            assert payload.get("job_id") != job_id, ("apply job redelivered", extra)
            continue
        assert extra.kind == "SOURCE_PUBLISHED", extra
        # the dispatcher already consumed + acked an apply publish event; only
        # ack what is still leased (a DONE row cannot be re-acked)
        if _outbox_status(setup, extra.id) == "CLAIMED":
            setup["store"].ack_outbox(t, ws, extra.id, "f6-after", extra.fencing_token)
