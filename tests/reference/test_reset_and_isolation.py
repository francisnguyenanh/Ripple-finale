# -*- coding: utf-8 -*-
"""Reset and session isolation, against the real seed.

These properties also hold on four toy artifacts. This runs them on the twenty
artifacts of the actual reference system, through the product contracts, because
"it worked on toy data" is not the claim that matters.
"""

from __future__ import annotations

import pathlib

import pytest

from core.change_intelligence.session import (
    InMemoryBaseline,
    InMemoryOverlay,
    InMemorySessionStore,
)
from core.change_intelligence.storage import BaselineImmutableError, SessionScopeError
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"


@pytest.fixture(scope="module")
def built():
    return build_baseline(SEED)


@pytest.fixture
def baseline(built):
    return InMemoryBaseline.from_artifacts(built["artifacts"])


@pytest.fixture
def store(baseline):
    return InMemorySessionStore(baseline)


# --- the baseline ------------------------------------------------------------


def test_the_baseline_covers_all_six_artifact_families(built):
    empty = [family for family, names in built["families"].items() if not names]
    assert not empty, f"families with no artifact: {empty}"
    assert built["artifact_count"] >= 18


def test_the_baseline_id_is_stable_across_builds():
    assert build_baseline(SEED)["baseline_id"] == build_baseline(SEED)["baseline_id"]


def test_the_baseline_id_follows_content(built):
    artifacts = dict(built["artifacts"])
    first = InMemoryBaseline.from_artifacts(artifacts).baseline_id
    artifacts["workflow/approval_routes.yaml"] = artifacts[
        "workflow/approval_routes.yaml"].replace(b"1000000", b"500000")
    assert InMemoryBaseline.from_artifacts(artifacts).baseline_id != first


def test_fi1_the_baseline_refuses_to_be_written_through(baseline):
    """The spike's FI-1, on the real seed."""
    before = baseline.baseline_id
    with pytest.raises(BaselineImmutableError):
        baseline.write("workflow/approval_routes.yaml", b"threshold: 1\n")
    assert baseline.baseline_id == before


# --- the overlay ---------------------------------------------------------------


def test_a_write_leaves_the_baseline_untouched(baseline):
    before = baseline.baseline_id
    overlay = InMemoryOverlay(baseline, "SESSION-TEST")
    overlay.write("workflow/approval_routes.yaml",
                  baseline.read("workflow/approval_routes.yaml").replace(b"1000000", b"500000"))
    assert baseline.baseline_id == before
    assert b"1000000" in baseline.read("workflow/approval_routes.yaml")


def test_a_read_resolves_the_overlay_first(baseline):
    overlay = InMemoryOverlay(baseline, "SESSION-TEST")
    snapshot = overlay.write("docs/faq.md", b"# replaced\n")
    assert snapshot.read("docs/faq.md") == b"# replaced\n"
    assert snapshot.read("docs/procurement-sop.md") == baseline.read("docs/procurement-sop.md")


def test_an_older_snapshot_still_reads_the_older_world(baseline):
    overlay = InMemoryOverlay(baseline, "SESSION-TEST")
    a0 = overlay.head
    overlay.write("docs/faq.md", b"# replaced\n")
    assert a0.read("docs/faq.md") == baseline.read("docs/faq.md")


def test_a_write_that_restores_the_original_bytes_reports_no_change(baseline):
    """Otherwise P8 receives a phantom ArtifactChanged on every no-op apply."""
    overlay = InMemoryOverlay(baseline, "SESSION-TEST")
    a1 = overlay.write("docs/faq.md", baseline.read("docs/faq.md"))
    assert a1.list_changed() == []
    assert a1.diff_from(overlay.head) == []


def test_diff_between_snapshots_names_only_what_really_moved(baseline):
    overlay = InMemoryOverlay(baseline, "SESSION-TEST")
    a0 = overlay.head
    a1 = overlay.write("data/approval_matrix.xlsx", b"not really a workbook")
    assert a1.diff_from(a0) == ["data/approval_matrix.xlsx"]


def test_an_overlay_without_a_session_id_is_refused(baseline):
    with pytest.raises(SessionScopeError):
        InMemoryOverlay(baseline, "")


def test_a_snapshot_cannot_be_read_through_another_sessions_overlay(baseline):
    """I18. The failure this prevents is silent: one judge reading another's work."""
    a = InMemoryOverlay(baseline, "SESSION-A")
    b = InMemoryOverlay(baseline, "SESSION-B")
    snapshot = a.write("docs/faq.md", b"# A wrote this\n")
    with pytest.raises(SessionScopeError):
        b.read_at(snapshot, "docs/faq.md")


# --- sessions and reset ---------------------------------------------------------


def test_two_sessions_on_one_baseline_never_see_each_other(store, baseline):
    a, b = store.create(), store.create()
    store.overlay(a).write("workflow/approval_routes.yaml", b"# A\n")
    store.overlay(b).write("workflow/approval_routes.yaml", b"# B\n")

    assert store.overlay(a).read("workflow/approval_routes.yaml") == b"# A\n"
    assert store.overlay(b).read("workflow/approval_routes.yaml") == b"# B\n"
    assert baseline.baseline_id == store.baseline.baseline_id


def test_reset_returns_a_world_identical_to_the_baseline(store, baseline):
    session = store.create()
    store.overlay(session).write("docs/faq.md", b"# scribbled on\n")

    fresh = store.reset(session)
    for name in baseline.names():
        assert store.overlay(fresh).read(name) == baseline.read(name)
    assert store.overlay(fresh).head.list_changed() == []


def test_reset_invalidates_the_old_session_id(store):
    """A caller still holding the old id should find out, not write into a
    world that no longer exists."""
    session = store.create()
    fresh = store.reset(session)
    assert fresh != session
    with pytest.raises(SessionScopeError):
        store.overlay(session)


def test_reset_does_not_disturb_another_session(store):
    a, b = store.create(), store.create()
    store.overlay(a).write("docs/faq.md", b"# A\n")
    store.overlay(b).write("docs/faq.md", b"# B\n")

    store.reset(a)
    assert store.overlay(b).read("docs/faq.md") == b"# B\n"


def test_the_baseline_hash_is_unchanged_after_the_entire_exercise(store, baseline):
    before = baseline.baseline_id
    for _ in range(3):
        session = store.create()
        store.overlay(session).write("workflow/approval_routes.yaml", b"# churn\n")
        store.reset(session)
    assert baseline.baseline_id == before
    assert build_baseline(SEED)["baseline_id"] == before


def test_a_session_cannot_be_created_against_a_foreign_baseline(store):
    with pytest.raises(SessionScopeError):
        store.create("0" * 64)


# --- the seed still behaves through the overlay ---------------------------------


def test_the_reference_app_runs_against_an_overlaid_workflow(baseline):
    """The point of the whole arrangement: a changed artifact that never
    touched disk is still executable."""
    from reference_system.application.approval_service import PurchaseRequest
    from reference_system.application.workflow_engine import load_routes, route_by_workflow

    overlay = InMemoryOverlay(baseline, "SESSION-TEST")
    original = baseline.read("workflow/approval_routes.yaml")
    snapshot = overlay.write("workflow/approval_routes.yaml",
                             original.replace(b"value: 1000000", b"value: 500000"))

    request = PurchaseRequest(id="PO-1", amount=750_000)
    assert route_by_workflow(request, load_routes(original)).outcome == "auto_approved"
    assert route_by_workflow(
        request, load_routes(snapshot.read("workflow/approval_routes.yaml"))
    ).outcome == "manager_approval"
