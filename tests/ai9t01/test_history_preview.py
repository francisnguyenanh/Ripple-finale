"""History browser + restore preview report the exact delta, are
read-only, and make target == current an explicit no-op.

Entry point of every test: real session -> real route -> SQL (never a helper
alone). Fixtures publish real revisions through the real store primitives;
`test_h1_open_changes_...` uses the real apply change chain.
"""

from __future__ import annotations

from tests.ai9t01.conftest import (
    A_TEXT, B_TEXT, C_TEXT, SHARED_TEXT, assert_no_durable_change, build_history,
    durable_snapshot, get_diff, get_history, head_tuple, ids_of, preview, preview_ok,
    publish_rev, rev_entry, restore_operator, sha,
)


def test_h1_preview_current_to_historic_reports_exact_multi_artifact_delta(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    body = preview_ok(op["client"], setup["workspace"], hist["a"])

    assert body["noop"] is False
    assert body["workspace_id"] == setup["workspace"]
    assert body["current"]["source_revision_id"] == hist["c"]
    assert body["target"]["source_revision_id"] == hist["a"]
    assert body["restored_from"] == hist["a"]
    # Whole-manifest delta: a.yaml changes back, c.yaml (added after A)
    # is REMOVED, the shared artifact is unchanged and only counted.
    delta = body["delta"]
    assert ids_of(delta["changed"]) == ["a.yaml"]
    change = delta["changed"][0]
    assert change["before_hash"] == sha(C_TEXT) and change["after_hash"] == sha(A_TEXT)
    assert ids_of(delta["removed"]) == ["c.yaml"]
    assert delta["added"] == []
    assert delta["unchanged_count"] == 1
    # Intervening edits that will be LOST are listed, not summarised away.
    assert sorted(e["source_revision_id"] for e in body["intervening_edits"]) == sorted(
        [hist["b"], hist["c"]])
    assert body["truncated"] is False
    assert isinstance(body["loss_warning"], str) and body["loss_warning"].strip()
    assert isinstance(body["preview_digest"], str) and body["preview_digest"]
    # Planned verification covers every artifact the restore touches.
    planned = {p["artifact_id"] for p in body["planned_verification"]}
    assert {"a.yaml", "c.yaml"} <= planned
    # `expected` is exactly the current durable tuple the digest is bound to.
    head = setup["store"].head(setup["tenant"], setup["workspace"])
    assert body["expected"] == {
        "version": head.version, "source_revision_id": head.source_revision_id,
        "decision_epoch": head.decision_epoch,
        "knowledge_revision_id": head.knowledge_revision_id}


def test_h1_preview_dto_is_a_reviewed_recovery_proposal(setup):
    """The DTO must be expressible as the schema `RecoveryProposal`
    (contracts 2.2): the schema enforces non-empty ids and current != target."""
    from core.change_intelligence.v2.schema import RecoveryProposal
    hist = build_history(setup, with_decision_and_km=True)
    body = preview_ok(restore_operator(setup)["client"], setup["workspace"], hist["a"])
    proposal = RecoveryProposal(
        id=body["proposal_id"],
        current_source_revision_id=body["current"]["source_revision_id"],
        target_historic_source_revision_id=body["target"]["source_revision_id"],
        delta_preview=body["delta"],
        affected_decision_ids=tuple(body["affected_decision_ids"]),
        affected_knowledge_revision_ids=tuple(body["stale_knowledge_revision_ids"]),
        loss_warning=body["loss_warning"], approval_id=None,
        restored_from=body["restored_from"])
    assert proposal.restored_from == hist["a"]


def test_h1_preview_is_read_only_across_every_durable_table(setup):
    hist = build_history(setup, with_decision_and_km=True)
    op = restore_operator(setup)
    before = durable_snapshot(setup)
    head_before = head_tuple(setup["store"], setup["tenant"], setup["workspace"])
    first = preview_ok(op["client"], setup["workspace"], hist["a"])
    second = preview_ok(op["client"], setup["workspace"], hist["b"])
    noop = preview_ok(op["client"], setup["workspace"], hist["c"])
    history = get_history(op["client"], setup["workspace"])
    assert history.status_code == 200, history.text
    diff = get_diff(op["client"], setup["workspace"], hist["a"], hist["c"])
    assert diff.status_code == 200, diff.text
    assert first["preview_digest"] != second["preview_digest"]
    assert noop["noop"] is True
    after = durable_snapshot(setup)
    assert_no_durable_change(before, after)  # source/KM/decision/audit/outbox/jobs/idempotency/blobs
    assert head_tuple(setup["store"], setup["tenant"], setup["workspace"]) == head_before


def test_h1_target_current_is_an_explicit_noop(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    before = durable_snapshot(setup)
    body = preview_ok(op["client"], setup["workspace"], hist["c"])
    assert body["noop"] is True
    assert body["preview_digest"] is None, "a no-op has nothing to approve"
    assert body["delta"]["changed"] == [] and body["delta"]["added"] == []
    assert body["delta"]["removed"] == []
    assert body["intervening_edits"] == []
    assert body["current"]["source_revision_id"] == body["target"]["source_revision_id"] == hist["c"]
    assert_no_durable_change(before, durable_snapshot(setup))


def test_h1_history_distinguishes_ordinal_version_from_content_hash(setup):
    hist = build_history(setup)
    # Same CONTENT as A, republished as a brand-new event: different id and
    # ordinal, identical content hash.
    a2 = publish_rev(setup, {"a.yaml": A_TEXT, "s.yaml": SHARED_TEXT})
    op = restore_operator(setup)
    resp = get_history(op["client"], setup["workspace"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ea, ea2 = rev_entry(body, hist["a"]), rev_entry(body, a2)
    assert ea["manifest_hash"] == ea2["manifest_hash"], "same content => same content hash"
    assert ea["revision_id"] != ea2["revision_id"]
    assert (ea["sequence"], ea2["sequence"]) == (1, 4)
    assert [r["sequence"] for r in body["revisions"]] == [1, 2, 3, 4]
    assert [r["revision_id"] for r in body["revisions"]] == [hist["a"], hist["b"], hist["c"], a2]
    assert rev_entry(body, hist["b"])["manifest_hash"] != ea["manifest_hash"]
    assert [r["revision_id"] for r in body["revisions"] if r["is_current"]] == [a2]
    assert rev_entry(body, a2)["parent_revision_id"] == hist["c"]
    assert ea["parent_revision_id"] is None
    assert body["head"]["source_revision_id"] == a2
    # A preview from the current head to A sees identical content: the delta is
    # empty although the revision ids differ (content vs event identity).
    prev = preview_ok(op["client"], setup["workspace"], hist["a"])
    assert prev["noop"] is False or prev["delta"]["changed"] == []
    assert prev["delta"]["changed"] == [] and prev["delta"]["removed"] == []


def test_h1_history_lists_source_knowledge_and_decision_timeline(setup):
    hist = build_history(setup, with_decision_and_km=True)
    op = restore_operator(setup)
    body = get_history(op["client"], setup["workspace"]).json()
    kinds = {e["kind"] for e in body["timeline"]}
    assert {"source_revision", "knowledge_revision", "decision"} <= kinds, body["timeline"]
    assert hist["km_id"] in {k["revision_id"] for k in body["knowledge_revisions"]}
    assert hist["decision_id"] in {d["decision_id"] for d in body["decisions"]}
    refs = {(e["kind"], e["ref"]) for e in body["timeline"]}
    assert ("source_revision", hist["a"]) in refs and ("decision", hist["decision_id"]) in refs
    assert body["head"]["knowledge_revision_id"] == hist["km_id"]


def test_h1_history_diff_between_two_revisions(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    ws = setup["workspace"]
    resp = get_diff(op["client"], ws, hist["a"], hist["c"])
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert ids_of(d["changed"]) == ["a.yaml"] and ids_of(d["added"]) == ["c.yaml"]
    assert d["removed"] == [] and d["unchanged_count"] == 1
    assert d["changed"][0]["before_hash"] == sha(A_TEXT) and d["changed"][0]["after_hash"] == sha(C_TEXT)
    back = get_diff(op["client"], ws, hist["c"], hist["a"]).json()
    assert ids_of(back["removed"]) == ["c.yaml"] and back["added"] == []
    same = get_diff(op["client"], ws, hist["b"], hist["b"]).json()
    assert same["changed"] == same["added"] == same["removed"] == []
    assert get_diff(op["client"], ws, hist["a"], "rev-does-not-exist").status_code == 404


def test_h1_preview_lists_affected_decisions_and_stale_knowledge_without_restoring_them(setup):
    hist = build_history(setup, with_decision_and_km=True)
    op = restore_operator(setup)
    body = preview_ok(op["client"], setup["workspace"], hist["a"])
    assert hist["decision_id"] in body["affected_decision_ids"]
    assert hist["km_id"] in body["stale_knowledge_revision_ids"], (
        "the accepted current KM becomes stale after a source restore")
    assert body["current"]["knowledge_revision_id"] == hist["km_id"]
    # A restore never re-elects an old KM/decision: the proposal is source-only.
    assert body["target"]["source_revision_id"] == hist["a"]
    assert body["loss_warning"]


def test_h1_history_limit_shows_omissions_instead_of_hiding_revisions(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    body = get_history(op["client"], setup["workspace"], limit=2).json()
    assert len(body["revisions"]) == 2
    assert body["omitted"]["revisions"] == 1
    # the newest revisions are kept; the current head is always visible
    assert body["revisions"][-1]["revision_id"] == hist["c"] and body["revisions"][-1]["is_current"]


def test_h1_open_changes_are_listed_in_the_preview_using_the_real_change_chain(
        setup, monkeypatch, owner):
    """An approved-but-unapplied change pinned to the current head is an open
    change a restore will invalidate (card: 'open changes se invalidated')."""
    change = applied_change_for_open(setup, monkeypatch, owner)
    op = restore_operator(setup)
    body = preview_ok(op["client"], setup["workspace"], change["s0"])
    text = str(body["open_changes"])
    assert change["second"]["approval_job_id"] in text or change["second"]["proposal_job_id"] in text, (
        body["open_changes"])
    assert body["intervening_edits"], "the applied change is an intervening edit"
    assert change["s1"] in {e["source_revision_id"] for e in body["intervening_edits"]}


def applied_change_for_open(setup, monkeypatch, owner):
    from tests.ai9t01.conftest import applied_change, build_second_change
    change = applied_change(setup, monkeypatch, owner)
    second = build_second_change(setup, monkeypatch, change, expected_current=30, proposed=45,
                                 apply=False)
    assert second.get("approval_job_id"), second
    change["second"] = second
    return change


# W1: a BLOCKED review can no longer be approved; this module drives pre-W1 journeys whose subject is
# downstream of the approval, so the review stage states a legacy fixture contract (tests/conftest.py).
import pytest as _w1_pytest  # noqa: E402
pytestmark = _w1_pytest.mark.usefixtures("legacy_contract_injection")
