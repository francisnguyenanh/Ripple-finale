"""The preview digest binds the exact current state and target, so a
preview left open goes stale when current moves; size bounds show omissions
and never shrink what the digest covers; route registry / permission guard."""

from __future__ import annotations

from uuid import uuid4

from tests.ai9t01.conftest import (
    MAX_ITEMS_ENV, build_history, other_workspace, preview_ok, publish_rev, restore_operator,
)


def test_h3_digest_is_deterministic_and_bound_to_target(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    ws = setup["workspace"]
    d1 = preview_ok(op["client"], ws, hist["a"])["preview_digest"]
    d2 = preview_ok(op["client"], ws, hist["a"])["preview_digest"]
    other = preview_ok(op["client"], ws, hist["b"])["preview_digest"]
    assert d1 == d2 and d1 != other
    # A second account previewing the same state gets the same digest (it binds
    # state, not the caller or the time).
    assert preview_ok(restore_operator(setup)["client"], ws, hist["a"])["preview_digest"] == d1


def test_h3_new_publish_while_preview_is_open_changes_the_digest(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    ws = setup["workspace"]
    stale = preview_ok(op["client"], ws, hist["a"])
    newer = publish_rev(setup, {"a.yaml": "refund_window_days: 60\n", "s.yaml": "shared_limit: 1\n"})
    fresh = preview_ok(op["client"], ws, hist["a"])
    assert fresh["preview_digest"] != stale["preview_digest"]
    assert fresh["current"]["source_revision_id"] == newer
    assert newer in {e["source_revision_id"] for e in fresh["intervening_edits"]}
    assert fresh["expected"]["version"] == stale["expected"]["version"] + 1


def test_h3_new_decision_epoch_while_preview_is_open_changes_the_digest(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    ws, store, t = setup["workspace"], setup["store"], setup["tenant"]
    stale = preview_ok(op["client"], ws, hist["a"])
    store.record_decision(t, ws, f"dec-{uuid4().hex[:6]}", setup["admin"],
                          {"action": "CONFIRM", "artifact_id": "a.yaml"},
                          expected_version=store.head(t, ws).version, idempotency_key=uuid4().hex)
    fresh = preview_ok(op["client"], ws, hist["a"])
    assert fresh["preview_digest"] != stale["preview_digest"]
    assert fresh["expected"]["decision_epoch"] == stale["expected"]["decision_epoch"] + 1


def test_h3_digest_is_bound_to_the_workspace(setup):
    """Identical file content in two workspaces still yields different digests
    (workspace/tenant scoping is part of the binding)."""
    hist = build_history(setup)
    other = other_workspace(setup)
    from tests.ai9t01.conftest import A_TEXT, B_TEXT, SHARED_TEXT, make_account
    a2 = publish_rev(setup, {"a.yaml": A_TEXT, "s.yaml": SHARED_TEXT},
                     tenant=other["tenant"], workspace=other["workspace"])
    publish_rev(setup, {"a.yaml": B_TEXT, "s.yaml": SHARED_TEXT},
                tenant=other["tenant"], workspace=other["workspace"])
    op2 = make_account(setup, ("restore_operator",), "op-other", workspace=other["workspace"])
    op = restore_operator(setup)
    publish_rev(setup, {"a.yaml": B_TEXT, "s.yaml": SHARED_TEXT})
    d_here = preview_ok(op["client"], setup["workspace"], hist["a"])["preview_digest"]
    d_there = preview_ok(op2["client"], other["workspace"], a2)["preview_digest"]
    assert d_here != d_there


def test_h3_size_bound_shows_omissions_and_the_digest_still_covers_everything(
        setup, monkeypatch):
    """Card: 'Diff/preview size bounds phai hien omissions, khong giau
    intervening edits roi cho approve nhu day du'. A cap of 2 hides items but
    says so (`truncated`, `omitted`); the digest is over the FULL set, so the
    truncated and the untruncated preview carry the SAME digest and a client
    can never mistake the shortened view for the whole."""
    files = {f"f{i}.yaml": f"k: {i}\n" for i in range(5)}
    first = publish_rev(setup, files)
    for n in range(4):  # 4 intervening revisions, each changing all 5 artifacts
        publish_rev(setup, {f"f{i}.yaml": f"k: {i + 10 * (n + 1)}\n" for i in range(5)})
    op = restore_operator(setup)
    ws = setup["workspace"]

    monkeypatch.setenv(MAX_ITEMS_ENV, "100")
    full = preview_ok(op["client"], ws, first)
    assert full["truncated"] is False
    assert len(full["delta"]["changed"]) == 5 and len(full["intervening_edits"]) == 4
    assert full["omitted"]["delta"] == 0 and full["omitted"]["intervening_edits"] == 0

    monkeypatch.setenv(MAX_ITEMS_ENV, "2")
    cut = preview_ok(op["client"], ws, first)
    assert cut["truncated"] is True
    assert len(cut["delta"]["changed"]) == 2 and cut["omitted"]["delta"] == 3
    assert len(cut["intervening_edits"]) == 2 and cut["omitted"]["intervening_edits"] == 2
    assert cut["preview_digest"] == full["preview_digest"], (
        "the digest must bind the full untruncated set")
    assert cut["delta"]["unchanged_count"] == full["delta"]["unchanged_count"]


def test_h3_routes_are_registered_with_the_intended_permissions():
    from infra.adapters.v2_policy import ROUTE_PERMISSIONS
    assert ROUTE_PERMISSIONS[("GET", "/v2/history/{workspace_id}")] == "project.read"
    assert ROUTE_PERMISSIONS[("GET", "/v2/history/{workspace_id}/diff")] == "project.read"
    assert ROUTE_PERMISSIONS[("POST", "/v2/restore/preview")] == "source.restore"
