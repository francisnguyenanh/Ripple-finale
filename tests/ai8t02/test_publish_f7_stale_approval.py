"""Stale approval: an approval is bound to the head it was approved
on. If the source changed since review, the proposal is stale and returns to
analysis; it is never rebased with the approval kept. The approval `snapshot` = (source_revision_id, decision_epoch);
apply must refuse when the CURRENT head differs, before any stage/publish, and
again against the row-LOCKED head inside the publish transaction.

Reading pinned here: ANY source_revision_id change is
stale -- a sibling-only change or a byte-identical republish of the target
counts; a stale MANUAL-work approval does not report pending human work.

Everything goes through the real chain: POST /v2/jobs -> claim_and_admit_one ->
ApplyExecutor.execute -> [prior_publish | publish_source(head_check)] ->
job_worker finalize -> GET /v2/jobs/{id}. Durable state = `store.head` + SQL
footprint (revisions, `apply.publish` ledger, SOURCE_PUBLISHED audit + outbox).
Guard-removal injections are in-test monkeypatches only; infra/** is untouched.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from infra.adapters import v2_postgres
from infra.worker import apply as apply_mod
from infra.worker import job_worker
from tests.ai4t01.conftest import drain_outbox, publish_with_chunk
from tests.ai7t01.conftest import get_job_status
from tests.ai8t01.conftest import submit_apply_job
from tests.ai8t02.conftest import (
    artifact_text, build_ready_to_publish_yaml, publish_footprint, reason_of)
from tests.ai8t02.test_publish_f4_pending_and_scope import _blob_hash, _client, _mutate_result
from tests.ai8t02.test_publish_f5_manual_work import _bundle, _state
from tests.ai8t02.test_publish_f6_store_failures import (
    _assert_clean_failure, _claim, _expire_claimed, _outbox_status, _submit)

MOVES = ["identical_target_plus_sibling", "identical_target_only", "target_changed"]


def _typed(setup, client, monkeypatch, owner):
    return build_ready_to_publish_yaml(setup, client, setup["workspace"], monkeypatch, owner)


def _move_head(setup, target, how):
    """Publish a NEW source revision after the approval was decided."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    head = store.head(t, ws)
    same = store.read_artifact_blob(t, ws, head.source_revision_id, target)["blob_hash"]
    if how == "identical_target_plus_sibling":  # byte-identical target plus a sibling artifact
        manifest = {target: same, "f7-sibling.yaml": store.stage_blob(b"sibling: 1\n")}
    elif how == "identical_target_only":
        manifest = {target: same}
    elif how == "target_changed":
        manifest = {target: store.stage_blob(b"refund_window_days: 22\n")}
    else:  # pragma: no cover
        raise AssertionError(how)
    result = store.publish_source(
        t, ws, f"rev-f7-move-{uuid4().hex}", manifest, expected_version=head.version,
        operation="source.import.commit", idempotency_key=uuid4().hex)
    drain_outbox(store, t, ws)
    return result


def _bump_epoch(setup, owner):
    """A REAL recorded human decision: the only site that bumps decision_epoch."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    before = store.head(t, ws)
    store.record_human_decision(
        t, ws, actor_id=owner["account"], current_permission="knowledge.confirm",
        target=f"f7-epoch-target-{uuid4().hex[:6]}", action="confirm",
        expected_version=before.version, idempotency_key=uuid4().hex,
        rationale="f7 epoch bump")
    after = store.head(t, ws)
    assert after.decision_epoch == before.decision_epoch + 1
    assert after.source_revision_id == before.source_revision_id, "epoch bump must not move source"
    drain_outbox(store, t, ws)
    return after


def _no_apply_revision(setup, job_id):
    with setup["store"]._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_source_revisions WHERE tenant_id=%s AND "
                    "workspace_id=%s AND revision_id=%s",
                    (setup["tenant"], setup["workspace"], f"rev-apply-{job_id}"))
        assert cur.fetchone()[0] == 0, "stale apply left its revision row"


def _run_apply(setup, client, b, worker="f7-worker"):
    job_id = _submit(setup, client, b)
    claim = _claim(setup, worker)
    assert claim is not None
    return job_id, claim


# =========================================================================== (a)

@pytest.mark.parametrize("how", MOVES)
def test_f7a_typed_head_moved_after_approve_fails_closed_zero_state(
        setup, monkeypatch, owner, how):
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    _move_head(setup, b["target_artifact_id"], how)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    text_before = artifact_text(setup["store"], setup["tenant"], setup["workspace"],
                                b["target_artifact_id"])
    claim = _claim(setup, "f7a")
    assert claim is not None
    # FAILED approval_stale_source, head/revisions/ledger/audit/outbox unchanged,
    # event DONE (terminal: no forever-redelivery)
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_source", before)
    _no_apply_revision(setup, job_id)
    body = get_job_status(client, setup["workspace"], job_id)
    assert not (body.get("execution_result") or {}).get("published_revision_id"), body
    assert artifact_text(setup["store"], setup["tenant"], setup["workspace"],
                         b["target_artifact_id"]) == text_before
    assert str(b["proposed"]) not in text_before


def test_f7a_reviewer_repro_byte_identical_target_plus_sibling_is_not_applied(
        setup, monkeypatch, owner):
    """Stale-approval probe: ready-to-publish yaml bundle +
    recorded approve; publish a NEW revision (target bytes IDENTICAL, plus a
    sibling artifact) via publish_source; submit + claim the apply. The apply must not
    rebase onto the moved head and keep the approval. Required: FAILED, named stale reason, head unmoved by
    the apply. A control on an unmoved head in the same workspace type proves
    the same bundle shape otherwise publishes."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    head0 = store.head(t, ws)
    moved = _move_head(setup, b["target_artifact_id"], "identical_target_plus_sibling")
    assert moved.version == head0.version + 1
    assert _blob_hash(store, t, ws, moved.revision_id, b["target_artifact_id"]) == _blob_hash(
        store, t, ws, head0.source_revision_id, b["target_artifact_id"]), (
        "probe precondition: target bytes identical across the two revisions")
    head_moved, fp = _state(setup)
    job_id, _claim_ = _run_apply(setup, client, b, "f7-repro")
    body = get_job_status(client, ws, job_id)
    assert body["execution_status"] == "FAILED", body
    assert "approval_stale_source" in reason_of(body), body
    assert _state(setup) == (head_moved, fp)
    assert store.head(t, ws).version == head0.version + 1  # only the probe's own publish
    # control: same bundle shape, no move -> publishes
    ctl = _typed(setup, client, monkeypatch, owner)
    ctl_head, _ = _state(setup)
    ok = get_job_status(client, ws, _run_apply(setup, client, ctl, "f7-repro-ctl")[0])
    assert ok["execution_status"] == "COMPLETED", ok
    assert store.head(t, ws).version == ctl_head.version + 1


# =========================================================================== (b)

def test_f7b_decision_epoch_bumped_after_approve_fails_stale_epoch(
        setup, monkeypatch, owner):
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    _bump_epoch(setup, owner)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7b")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_decision_epoch", before)
    _no_apply_revision(setup, job_id)


def test_f7b_manual_decision_epoch_bumped_fails_stale_epoch_no_pending_work(
        setup, owner):
    client = _client(setup)
    b = _bundle(setup, client)
    _bump_epoch(setup, owner)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7b-manual")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_decision_epoch", before)
    assert not (get_job_status(client, setup["workspace"], job_id).get("execution_result")
                or {}).get("pending_human_work")


def test_f7b_source_and_epoch_both_moved_reports_source_first(setup, monkeypatch, owner):
    """Documents the deterministic order of the two named reasons."""
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    _move_head(setup, b["target_artifact_id"], "identical_target_only")
    _bump_epoch(setup, owner)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7b-both")
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_source", before)


# =========================================================================== (c)

def test_f7c_control_fresh_approval_on_unchanged_head_still_publishes(
        setup, monkeypatch, owner):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    head, fp = _state(setup)
    job_id, _ = _run_apply(setup, client, b, "f7c")
    body = get_job_status(client, ws, job_id)
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["published"] is True
    assert store.head(t, ws).version == head.version + 1
    assert str(b["proposed"]) in artifact_text(store, t, ws, b["target_artifact_id"])
    after = publish_footprint(store, t, ws)
    assert after["apply_publish_keys"] == fp["apply_publish_keys"] + 1


def test_f7c_control_unrelated_policy_change_after_approve_does_not_stale(
        setup, monkeypatch, owner):
    """The gate is exactly (source_revision_id, decision_epoch): a membership
    grant/revoke of an UNRELATED account after approval bumps policy_version
    only and must not stale the approval."""
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.ai4t01.conftest import DSN
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    before_head = store.head(t, ws)
    identities = PostgresIdentityStore(DSN)
    acct = f"f7-unrelated-{uuid4().hex[:8]}"
    identities.create_account(acct, f"cred-{acct}")
    identities.grant_membership(t, setup["project"], ws, acct, ("change_approver",))
    identities.revoke_membership(t, setup["project"], ws, acct)
    mid = store.head(t, ws)
    assert mid.policy_version != before_head.policy_version, "precondition: policy moved"
    assert (mid.source_revision_id, mid.decision_epoch) == (
        before_head.source_revision_id, before_head.decision_epoch)
    job_id, _ = _run_apply(setup, client, b, "f7c-policy")
    assert get_job_status(client, ws, job_id)["execution_status"] == "COMPLETED"


# =========================================================================== (d)

def _commit_then_die(monkeypatch):
    original = v2_postgres.PostgresV2Store.publish_source

    class Died(BaseException):
        pass

    def _wrapped(self, *a, **k):
        result = original(self, *a, **k)
        raise Died()

    return Died, _wrapped


@pytest.mark.parametrize("extra_move", [False, True], ids=["own-publish-only", "plus-later-publish"])
def test_f7d_retry_recovery_after_committed_publish_is_exempt_from_stale_check(
        setup, monkeypatch, owner, extra_move):
    """Crash after the publish committed but before the job was finalized. The
    head now differs from the approval snapshot BECAUSE of that very publish
    (and, in the second variant, a later publish too); the retry must return the
    prior committed result, not FAIL approval_stale_source and not publish twice."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    Died, wrapped = _commit_then_die(monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", wrapped)
        with pytest.raises(Died):
            _claim(setup, "f7d-dying")
    committed = store.head(t, ws)
    snapshot_head_moved = committed.source_revision_id.startswith("rev-apply-")
    assert snapshot_head_moved, "precondition: the head moved because of the apply's own publish"
    if extra_move:
        _move_head(setup, b["target_artifact_id"], "identical_target_plus_sibling")
    committed = store.head(t, ws)
    fp = publish_footprint(store, t, ws)
    _expire_claimed(setup)
    assert _claim(setup, "f7d-recovery") is not None
    body = get_job_status(client, ws, job_id)
    assert body["execution_status"] == "COMPLETED", body
    res = body["execution_result"]
    assert res["recovered_prior_publish"] is True and res["published"] is True
    assert res["published_revision_id"] == f"rev-apply-{job_id}"
    assert store.head(t, ws) == committed and publish_footprint(store, t, ws) == fp, (
        "recovery must not publish again")


def test_f7d_red_recovery_exemption_is_what_saves_the_retry(setup, monkeypatch, owner):
    """Neutralise prior_publish (in-test): the retry then meets the F7 gate on the
    head its own publish moved and is refused -- proving the recovery exemption
    is load-bearing (and the F7 gate really covers that head)."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    Died, wrapped = _commit_then_die(monkeypatch)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "publish_source", wrapped)
        with pytest.raises(Died):
            _claim(setup, "f7d-red-dying")
    committed, fp = store.head(t, ws), publish_footprint(store, t, ws)
    _expire_claimed(setup)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "prior_publish", lambda self, *a, **k: None)
        assert _claim(setup, "f7d-red-recovery") is not None
    body = get_job_status(client, ws, job_id)
    assert body["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(body), body
    assert store.head(t, ws) == committed and publish_footprint(store, t, ws) == fp


# =========================================================================== (e)

@pytest.mark.parametrize("how", MOVES)
def test_f7e_manual_branch_with_moved_head_fails_closed_no_pending_work(setup, how):
    client = _client(setup)
    b = _bundle(setup, client)
    _move_head(setup, b["target_artifact_id"], how)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7e")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_source", before)
    res = get_job_status(client, setup["workspace"], job_id).get("execution_result") or {}
    assert not res.get("pending_human_work") and not res.get("verification_scope"), res


def test_f7e_manual_control_current_head_still_completes_with_pending_work(setup):
    client = _client(setup)
    b = _bundle(setup, client)
    head, fp = _state(setup)
    job_id, _ = _run_apply(setup, client, b, "f7e-ctl")
    body = get_job_status(client, setup["workspace"], job_id)
    assert body["execution_status"] == "COMPLETED", body
    assert body["execution_result"]["pending_human_work"]
    assert _state(setup) == (head, fp)


# =========================================================================== (f)

def _snap(**over):
    def fn(res):
        snap = res.get("snapshot")
        for k, v in over.items():
            if v is None:
                snap.pop(k, None)
            else:
                snap[k] = v
    return fn


def _pop_snapshot(res):
    res.pop("snapshot", None)


SNAPSHOT_CASES = {
    "absent": _pop_snapshot,
    "null": lambda r: r.__setitem__("snapshot", None),
    "not_a_dict": lambda r: r.__setitem__("snapshot", "abc"),
    "empty_dict": lambda r: r.__setitem__("snapshot", {}),
    "no_source_revision": _snap(source_revision_id=None),
    "empty_source_revision": _snap(source_revision_id=""),
    "no_epoch": _snap(decision_epoch=None),
    "epoch_string": _snap(decision_epoch="0"),
    "epoch_bool": _snap(decision_epoch=True),
    "epoch_float": _snap(decision_epoch=1.0),
}


@pytest.mark.parametrize("case", sorted(SNAPSHOT_CASES))
@pytest.mark.parametrize("kind", ["typed", "manual"])
def test_f7f_missing_or_malformed_snapshot_fails_closed(setup, monkeypatch, owner, kind, case):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner) if kind == "typed" else _bundle(setup, client)
    _mutate_result(store, t, ws, b["approval_job_id"], SNAPSHOT_CASES[case])
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7f")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, "approval_snapshot_missing", before)
    _no_apply_revision(setup, job_id)


@pytest.mark.parametrize("flag,reason", [("source_stale", "approval_stale_source"),
                                          ("epoch_stale", "approval_stale_decision_epoch")])
@pytest.mark.parametrize("kind", ["typed", "manual"])
def test_f7f_freshness_stale_flag_fails_closed_even_on_current_head(
        setup, monkeypatch, owner, kind, flag, reason):
    """Approval computed over an already-stale proposal: head == snapshot, yet the
    approval's own freshness flag says stale -> refuse."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner) if kind == "typed" else _bundle(setup, client)
    _mutate_result(store, t, ws, b["approval_job_id"],
                   lambda r: r.__setitem__("freshness", {**(r.get("freshness") or {}), flag: True}))
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7f-fresh")
    assert claim is not None
    _assert_clean_failure(setup, client, job_id, claim, reason, before)


def test_f7f_real_approval_carries_the_snapshot_the_gate_compares(setup, monkeypatch, owner):
    """The tuple the gate reads is the approval's REAL recorded state (not a
    constant): it equals the head at approve time, and changes with it."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    head = store.head(t, ws)
    res = get_job_status(client, ws, b["approval_job_id"])["execution_result"]
    assert res["snapshot"]["source_revision_id"] == head.source_revision_id
    assert res["snapshot"]["decision_epoch"] == head.decision_epoch
    assert not (res.get("freshness") or {}).get("source_stale")
    assert not (res.get("freshness") or {}).get("epoch_stale")


# =========================================================================== F6 shadowing pins

@pytest.mark.parametrize("kind", ["manual", "typed"])
def test_f7_target_removed_by_later_publish_now_reports_stale_source(
        setup, monkeypatch, owner, kind):
    """Product path of the old F6 'target absent from head' scenario: the head can
    only lose the target by moving, and F7 refuses that first (the F6 guard
    itself is proven in test_publish_f6_store_failures with F7 neutralised)."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _bundle(setup, client) if kind == "manual" else _typed(setup, client, monkeypatch, owner)
    publish_with_chunk(store, t, ws, artifact_id=f"replacement-{uuid4().hex[:6]}.md", text="x: 1\n")
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7-target-removed")
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_source", before)


@pytest.mark.parametrize("content", [b"a: [unclosed\n", b"\xff\xfe\x00binary", b"- just\n- a list\n"])
def test_f7_target_republished_unparseable_now_reports_stale_source(
        setup, monkeypatch, owner, content):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    store.publish_source(t, ws, f"rev-{uuid4().hex}",
                         {b["target_artifact_id"]: store.stage_blob(content)},
                         expected_version=store.head(t, ws).version,
                         operation="source.import.commit", idempotency_key=uuid4().hex)
    drain_outbox(store, t, ws)
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7-unparseable")
    _assert_clean_failure(setup, client, job_id, claim, "approval_stale_source", before)


# =========================================================================== (g) authoritative in-tx check

class _Selective:
    """In-test guard removal for ONE of the two F7 comparisons. The pre-check gets
    the head from `store.head`; the in-tx check gets the object returned by
    `_lock_head` (same tx), which is how the two are told apart."""

    def __init__(self, monkeypatch, *, skip_pre=False, skip_tx=False):
        self.locked, self.calls = [], {"pre": 0, "tx": 0}
        real_gate = apply_mod._assert_snapshot_current
        real_lock = v2_postgres.PostgresV2Store._lock_head
        me = self

        def lock(store_self, *a, **k):
            head = real_lock(store_self, *a, **k)
            me.locked.append(head)
            return head

        def gate(snapshot, head):
            in_tx = any(head is h for h in me.locked)
            me.calls["tx" if in_tx else "pre"] += 1
            if (in_tx and skip_tx) or (not in_tx and skip_pre):
                return None
            return real_gate(snapshot, head)

        monkeypatch.setattr(v2_postgres.PostgresV2Store, "_lock_head", lock)
        monkeypatch.setattr(apply_mod, "_assert_snapshot_current", gate)


def _interleaving_publish(monkeypatch, setup, target, *, rebind_expected_version=False):
    """Wrap publish_source: right before the apply's publish transaction starts
    (i.e. AFTER the executor's pre-check), another publish moves the head.
    `rebind_expected_version` re-points the apply's CAS at the moved head, the
    shape of the original F7 defect (expected_version = a head re-read after the
    move), so ONLY the stale-tuple check can refuse it."""
    original = v2_postgres.PostgresV2Store.publish_source
    fired = []

    def wrapped(self, tenant_id, workspace_id, revision_id, manifest, **kwargs):
        if kwargs.get("operation") == "apply.publish" and not fired:
            head = self.head(tenant_id, workspace_id)
            same = self.read_artifact_blob(
                tenant_id, workspace_id, head.source_revision_id, target)["blob_hash"]
            fired.append(original(
                self, tenant_id, workspace_id, f"rev-f7-interloper-{uuid4().hex}",
                {target: same, "f7-interloper.yaml": self.stage_blob(b"i: 1\n")},
                expected_version=head.version, operation="interloper.publish",
                idempotency_key=uuid4().hex))
            if rebind_expected_version:
                kwargs = dict(kwargs, expected_version=self.head(tenant_id, workspace_id).version)
        return original(self, tenant_id, workspace_id, revision_id, manifest, **kwargs)

    monkeypatch.setattr(v2_postgres.PostgresV2Store, "publish_source", wrapped)
    return fired


def test_f7g_head_moves_between_precheck_and_publish_tx_is_refused_by_locked_check(
        setup, monkeypatch, owner):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    sel = _Selective(monkeypatch)
    fired = _interleaving_publish(monkeypatch, setup, b["target_artifact_id"])
    drain_outbox(store, t, ws)
    head_before = store.head(t, ws)
    fp_before = publish_footprint(store, t, ws)
    job_id, claim = _run_apply(setup, client, b, "f7g")
    assert fired, "interloper did not run"
    body = get_job_status(client, ws, job_id)
    assert body["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(body), body
    # precondition of the scenario: the pre-check PASSED (ran on the pre-move
    # head) and the refusal came from the check on the LOCKED head
    assert sel.calls["pre"] == 1 and sel.calls["tx"] == 1, sel.calls
    head_after = store.head(t, ws)
    assert head_after.source_revision_id == fired[0].revision_id
    assert head_after.version == head_before.version + 1, "only the interloper may move the head"
    fp_after = publish_footprint(store, t, ws)
    assert fp_after["apply_publish_keys"] == fp_before["apply_publish_keys"] == 0 or \
        fp_after["apply_publish_keys"] == fp_before["apply_publish_keys"]
    assert fp_after["revisions"] == fp_before["revisions"] + 1  # the interloper's only
    _no_apply_revision(setup, job_id)
    assert _outbox_status(setup, claim.id) == "DONE"


@pytest.mark.parametrize("neutralize", [True, False], ids=["INJECT-red", "guard-green"])
def test_f7h_in_tx_locked_head_check_removal_red_green(setup, monkeypatch, owner, neutralize):
    """Guard-removal for the AUTHORITATIVE check. Interleave + CAS re-bound to the
    moved head (the original defect shape) so nothing but the locked-head
    stale-tuple check can refuse. red: check removed -> the apply COMPLETES on the
    moved head (violation observable); green: check present -> FAILED, no state."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    _Selective(monkeypatch, skip_tx=neutralize)
    fired = _interleaving_publish(monkeypatch, setup, b["target_artifact_id"],
                                  rebind_expected_version=True)
    drain_outbox(store, t, ws)
    fp_before = publish_footprint(store, t, ws)
    job_id, _ = _run_apply(setup, client, b, "f7h-tx")
    assert fired
    body = get_job_status(client, ws, job_id)
    if neutralize:
        assert body["execution_status"] == "COMPLETED", body  # violation is observable
        assert store.head(t, ws).source_revision_id == f"rev-apply-{job_id}"
    else:
        assert body["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(body), body
        assert publish_footprint(store, t, ws)["apply_publish_keys"] == fp_before["apply_publish_keys"]
        _no_apply_revision(setup, job_id)
        assert store.head(t, ws).source_revision_id == fired[0].revision_id


def test_f7h_manual_in_tx_locked_check_red_restore_green(setup, monkeypatch):
    """Manual-work branch: head moves after the pre-check, inside the branch's own
    authority-recheck transaction. red (locked check removed): COMPLETES and
    reports pending work for a stale approval; restored: FAILED, no report."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b1 = _bundle(setup, client)
    b2 = _bundle(setup, client, target=b1["target_artifact_id"], publish_base=False)
    target = b1["target_artifact_id"]
    original = v2_postgres.PostgresV2Store._recheck_actor_authority
    moved = []

    def recheck(self, cur, *a, **k):
        # runs inside the manual branch's tx, after the pre-check
        if not moved:
            head = self.head(t, ws)
            same = self.read_artifact_blob(t, ws, head.source_revision_id, target)["blob_hash"]
            moved.append(self.publish_source(
                t, ws, f"rev-f7-manual-{uuid4().hex}",
                {target: same, "f7-manual-sib.yaml": self.stage_blob(b"m: 1\n")},
                expected_version=head.version, operation="interloper.publish",
                idempotency_key=uuid4().hex))
        return original(self, cur, *a, **k)

    with monkeypatch.context() as mp:
        _Selective(mp, skip_tx=True)
        mp.setattr(v2_postgres.PostgresV2Store, "_recheck_actor_authority", recheck)
        job1, _ = _run_apply(setup, client, b1, "f7h-manual-red")
        red = get_job_status(client, ws, job1)
    assert moved, "interleave did not run"
    assert red["execution_status"] == "COMPLETED" and red["execution_result"]["pending_human_work"], red

    # restore: locked check back. b2 was approved on the ORIGINAL head; interleave again.
    moved.clear()
    drain_outbox(store, t, ws)
    with monkeypatch.context() as mp:
        # pre-check would also refuse (head already moved by the red run); remove it
        # so this leg isolates the LOCKED check, which must refuse on its own.
        _Selective(mp, skip_pre=True)
        mp.setattr(v2_postgres.PostgresV2Store, "_recheck_actor_authority", recheck)
        head, fp = _state(setup)
        job2, _ = _run_apply(setup, client, b2, "f7h-manual-green")
    green = get_job_status(client, ws, job2)
    assert green["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(green), green
    assert not (green.get("execution_result") or {}).get("pending_human_work")
    assert store.head(t, ws).version == head.version + 1  # only the second interloper


@pytest.mark.parametrize("skip", ["pre", "both"])
def test_f7h_precheck_removal_red_restore_green(setup, monkeypatch, owner, skip):
    """Guard-removal for the PRE-check (moved head BEFORE the apply is claimed).
      * skip='pre': the locked check still refuses (no state), but the early
        failure is lost -- the blob was already staged (stage_blob spy > 0);
        restored, the same approval fails before staging (spy == 0).
      * skip='both': red = the apply COMPLETES on the moved head (the F7 defect);
        restored = FAILED approval_stale_source."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    _move_head(setup, b["target_artifact_id"], "identical_target_plus_sibling")
    staged = []
    real_stage = v2_postgres.PostgresV2Store.stage_blob

    def spy(self, content):
        staged.append(len(content))
        return real_stage(self, content)

    drain_outbox(store, t, ws)
    fp0 = publish_footprint(store, t, ws)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "stage_blob", spy)
        _Selective(mp, skip_pre=True, skip_tx=(skip == "both"))
        job1, _ = _run_apply(setup, client, b, "f7h-pre-red")
    red = get_job_status(client, ws, job1)
    if skip == "pre":
        assert red["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(red), red
        assert staged, "pre-check removed: staging should now happen before the refusal"
        assert publish_footprint(store, t, ws) == fp0
    else:
        assert red["execution_status"] == "COMPLETED", red  # the F7 defect, observable
        assert store.head(t, ws).source_revision_id == f"rev-apply-{job1}"
    # restore (patches gone): same approval, fresh apply job
    staged.clear()
    drain_outbox(store, t, ws)
    head, fp = _state(setup)
    with monkeypatch.context() as mp:
        mp.setattr(v2_postgres.PostgresV2Store, "stage_blob", spy)
        job2 = _submit(setup, client, b)
        claim = _claim(setup, "f7h-pre-green")
        assert claim is not None
    green = get_job_status(client, ws, job2)
    assert green["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(green), green
    assert not staged, "with the pre-check restored the refusal must precede staging"
    assert _state(setup) == (head, fp)


# =========================================================================== (i) redelivery

def test_f7i_terminal_stale_failure_is_not_redelivered_or_retried(setup, monkeypatch, owner):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    client = _client(setup)
    b = _typed(setup, client, monkeypatch, owner)
    _move_head(setup, b["target_artifact_id"], "identical_target_plus_sibling")
    job_id = _submit(setup, client, b)
    before = _state(setup)
    claim = _claim(setup, "f7i-first")
    assert claim is not None
    assert get_job_status(client, ws, job_id)["execution_status"] == "FAILED"
    assert _outbox_status(setup, claim.id) == "DONE"
    for i in range(3):  # repeated lease expiry + claims: nothing is ever redelivered
        _expire_claimed(setup)
        assert _claim(setup, f"f7i-probe-{i}") is None
    body = get_job_status(client, ws, job_id)
    assert body["execution_status"] == "FAILED" and "approval_stale_source" in reason_of(body)
    assert _state(setup) == before
    # a NEW job over the same stale approval is refused the same way (no rebase)
    job2 = _submit(setup, client, b)
    assert _claim(setup, "f7i-second") is not None
    assert "approval_stale_source" in reason_of(get_job_status(client, ws, job2))
    assert _state(setup) == before


# =========================================================================== store-level head_check contract

def _store_publish(setup, target, text, key, *, head_check=None, version=None, rev=None):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    head = store.head(t, ws)
    return store.publish_source(
        t, ws, rev or f"rev-hc-{uuid4().hex}", {target: store.stage_blob(text)},
        expected_version=head.version if version is None else version,
        operation="hc.publish", idempotency_key=key, head_check=head_check)


def test_store_head_check_default_off_and_runs_on_locked_head_before_cas(setup):
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    # default: no head_check argument -> unchanged behaviour
    r0 = _store_publish(setup, "hc.txt", b"one", uuid4().hex)
    seen = []
    head = store.head(t, ws)

    def check(locked):
        seen.append(locked)

    # stale expected_version + a head_check that ALSO raises: head_check wins
    # (runs before the CAS) -> its exception, not StaleVersion
    class Boom(Exception):
        pass

    def boom(locked):
        raise Boom()

    with pytest.raises(Boom):
        _store_publish(setup, "hc.txt", b"two", uuid4().hex, head_check=boom,
                       version=head.version - 1)
    # head_check passing + stale version -> CAS still refuses
    with pytest.raises(v2_postgres.StaleVersion):
        _store_publish(setup, "hc.txt", b"two", uuid4().hex, head_check=check,
                       version=head.version - 1)
    assert seen and seen[-1].source_revision_id == r0.revision_id  # the locked head
    # raising head_check leaves no revision behind
    fp = publish_footprint(store, t, ws)
    with pytest.raises(Boom):
        _store_publish(setup, "hc.txt", b"three", uuid4().hex, head_check=boom)
    assert publish_footprint(store, t, ws) == fp
    assert store.head(t, ws) == head


def test_store_head_check_is_skipped_for_an_idempotent_replay(setup):
    """The check runs AFTER the replay lookup: a committed publish replayed under
    the same key returns its prior result even if the check would now raise."""
    store, t, ws = setup["store"], setup["tenant"], setup["workspace"]
    key, rev = uuid4().hex, f"rev-hc-replay-{uuid4().hex}"
    head = store.head(t, ws)
    first = _store_publish(setup, "hc-replay.txt", b"x", key, rev=rev, version=head.version)

    def boom(locked):
        raise AssertionError("head_check must not run for a replay")

    again = _store_publish(setup, "hc-replay.txt", b"x", key, rev=rev, head_check=boom,
                           version=head.version)
    assert (again.version, again.revision_id) == (first.version, first.revision_id)
    assert again.replayed is True and first.replayed is False
