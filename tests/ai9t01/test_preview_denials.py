"""Cross-workspace/tenant target, revoked read, historical role,
missing/corrupt blob -> deny/error WITHOUT leaking and WITHOUT falling back to
another target; no denial writes durable state."""

from __future__ import annotations

import json

import pytest

from tests.ai9t01.conftest import (
    A_TEXT, SHARED_TEXT, assert_no_durable_change, build_history, durable_snapshot,
    get_diff, get_history, make_account, other_workspace, preview, restore_operator, revoke,
    sha,
)

FOREIGN_MARKERS = ("foreign_secret_marker", "4242")


def _no_leak(resp, *needles):
    text = resp.text
    for n in needles:
        assert n not in text, f"response leaked {n!r}: {text}"


@pytest.mark.parametrize("scope", ["same-tenant-other-workspace", "cross-tenant",
                                   "member-of-both-workspaces"])
def test_h2_target_from_another_workspace_or_tenant_is_refused_without_leak(setup, scope):
    hist = build_history(setup)
    foreign = other_workspace(setup, cross_tenant=(scope == "cross-tenant"))
    op = restore_operator(setup)
    if scope == "member-of-both-workspaces":
        from infra.adapters.v2_identity import PostgresIdentityStore
        from tests.ai9t01.conftest import DSN
        PostgresIdentityStore(DSN).grant_membership(
            setup["tenant"], setup["project"], foreign["workspace"], op["account"],
            ("restore_operator",))
    control = preview(op["client"], setup["workspace"], hist["a"])
    assert control.status_code == 200, control.text  # positive control: the route itself works
    before = durable_snapshot(setup)
    resp = preview(op["client"], setup["workspace"], foreign["revision_id"])
    assert resp.status_code == 404, resp.text
    _no_leak(resp, foreign["revision_id"], foreign["workspace"], sha("foreign_secret_marker: 4242\n"),
             *FOREIGN_MARKERS)
    diff = get_diff(op["client"], setup["workspace"], hist["a"], foreign["revision_id"])
    assert diff.status_code == 404, diff.text
    _no_leak(diff, foreign["revision_id"], *FOREIGN_MARKERS)
    assert_no_durable_change(before, durable_snapshot(setup))


def test_h2_naming_a_workspace_the_caller_is_not_a_member_of_is_refused(setup):
    build_history(setup)
    foreign = other_workspace(setup, cross_tenant=True)
    op = restore_operator(setup)
    for call in (
            lambda: preview(op["client"], foreign["workspace"], foreign["revision_id"]),
            lambda: get_history(op["client"], foreign["workspace"]),
            lambda: get_diff(op["client"], foreign["workspace"], foreign["revision_id"],
                             foreign["revision_id"])):
        resp = call()
        assert resp.status_code in (401, 403), resp.text
        _no_leak(resp, foreign["revision_id"], *FOREIGN_MARKERS)


@pytest.mark.parametrize("payload", [
    {},
    {"target_revision_id": ""},
    {"target_revision_id": 7},
    {"target_revision_id": ["a"]},
    {"target_revision_id": None},
], ids=["missing", "empty", "int", "list", "null"])
def test_h2_malformed_target_is_a_400_not_a_500(setup, payload):
    build_history(setup)
    op = restore_operator(setup)
    resp = op["client"].post("/v2/restore/preview",
                             json={"workspace_id": setup["workspace"], **payload})
    assert resp.status_code == 400, resp.text


def test_h2_unknown_target_is_404_and_does_not_fall_back_to_another_revision(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    control = preview(op["client"], setup["workspace"], hist["a"])
    assert control.status_code == 200, control.text  # positive control
    resp = preview(op["client"], setup["workspace"], "rev-does-not-exist")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert "preview_digest" not in body and "delta" not in body


def test_h2_revoked_membership_loses_history_and_preview_immediately(setup):
    hist = build_history(setup)
    op = restore_operator(setup)
    ok = preview(op["client"], setup["workspace"], hist["a"])
    assert ok.status_code == 200, ok.text  # it WAS allowed while the membership held
    assert get_history(op["client"], setup["workspace"]).status_code == 200
    revoke(setup, op)
    before = durable_snapshot(setup)
    for resp in (preview(op["client"], setup["workspace"], hist["a"]),
                 get_history(op["client"], setup["workspace"]),
                 get_diff(op["client"], setup["workspace"], hist["a"], hist["c"])):
        assert resp.status_code in (401, 403), resp.text
        _no_leak(resp, hist["a"], hist["b"], hist["c"], sha(A_TEXT), "a.yaml")
    assert_no_durable_change(before, durable_snapshot(setup))


def test_h2_historical_role_does_not_grant_current_read_or_restore(setup):
    """The account held restore_operator when the target revision was created;
    its CURRENT membership is a narrower role. Read history uses CURRENT
    authority only: project.read still works, restore preview (source.restore)
    is refused - the historical grant never survives."""
    hist = build_history(setup)
    op = restore_operator(setup)
    revoke(setup, op)
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.ai9t01.conftest import DSN
    PostgresIdentityStore(DSN).grant_membership(
        setup["tenant"], setup["project"], setup["workspace"], op["account"], ("knowledge_owner",))
    from tests.ai9t01.conftest import login
    client = setup["browser"]()
    login(client, op["account"], op["credential"])
    assert get_history(client, setup["workspace"]).status_code == 200
    resp = preview(client, setup["workspace"], hist["a"])
    assert resp.status_code == 403, resp.text
    _no_leak(resp, sha(A_TEXT), hist["a"])


@pytest.mark.parametrize("roles", [("requester",), ("change_approver",), ("knowledge_owner",)],
                         ids=lambda r: r[0])
def test_h2_preview_needs_source_restore_not_change_permissions(setup, roles):
    """Permission mapping: change.approve / change.accept_result /
    knowledge.confirm holders can READ history but cannot preview a restore."""
    hist = build_history(setup)
    who = make_account(setup, roles, "not-restore")
    assert get_history(who["client"], setup["workspace"]).status_code == 200
    resp = preview(who["client"], setup["workspace"], hist["a"])
    assert resp.status_code == 403, resp.text


def test_h2_no_session_is_401(setup):
    hist = build_history(setup)
    anon = setup["browser"]()
    assert preview(anon, setup["workspace"], hist["a"]).status_code == 401
    assert get_history(anon, setup["workspace"]).status_code == 401
    assert get_diff(anon, setup["workspace"], hist["a"], hist["c"]).status_code == 401


@pytest.mark.parametrize("artifact,damage", [
    ("a.yaml", "missing"), ("a.yaml", "corrupt"),
    ("s.yaml", "missing"), ("s.yaml", "corrupt")],
    ids=["changed-missing", "changed-corrupt", "unchanged-missing", "unchanged-corrupt"])
def test_h2_missing_or_corrupt_target_blob_blocks_preview_without_fallback(
        setup, artifact, damage):
    """Retained metadata but a deleted/checksum-invalid blob: the preview is a
    409 for a damaged blob of ANY artifact in the target manifest (also an
    unchanged one - the whole target must be restorable), with no digest, no
    delta, no other-revision fallback and no bytes in the error."""
    hist = build_history(setup)
    op = restore_operator(setup)
    store = setup["store"]
    text = A_TEXT if artifact == "a.yaml" else SHARED_TEXT
    digest = sha(text)
    good = store.blobs.data[digest]
    before = durable_snapshot(setup)
    garbage = b"garbage-not-matching-digest-XYZZY"
    try:
        if damage == "corrupt":
            store.blobs.data[digest] = garbage
        else:
            del store.blobs.data[digest]
        resp = preview(op["client"], setup["workspace"], hist["a"])
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body.get("error") == "restore_target_unavailable", body
        assert "preview_digest" not in body or body["preview_digest"] is None
        assert "delta" not in body
        _no_leak(resp, "XYZZY", hist["b"])
        assert artifact in json.dumps(body), "the operator must learn WHICH artifact is unusable"
        # no read/preview side effect on the durable state (blob damaged in test only)
        after = durable_snapshot(setup)
        after["__blobs__"] = before["__blobs__"] = None
        assert_no_durable_change(before, after)
    finally:
        store.blobs.data[digest] = good
    healthy = preview(op["client"], setup["workspace"], hist["a"])
    assert healthy.status_code == 200, "restoring the blob must make the target usable again"
