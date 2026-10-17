# -*- coding: utf-8 -*-
"""D4 -- one counter, and the seven situations that must not move it wrongly.

The failure this file exists to prevent is the quiet one: a version that
increments twice for one logical transition. Nothing breaks, nothing
raises, and from then on the history says more happened than happened.
Every test here is one way that could occur.
"""

from __future__ import annotations

import pytest

from core.change_intelligence.lineage import (
    BASELINE_VERSION,
    LineageMismatch,
    NotDeterministic,
    VerifiedLineage,
    render,
)


@pytest.fixture
def lineage():
    return VerifiedLineage(session_id="SESSION-A", baseline_id="BASE-001")


def _verify(lineage, snapshot="SNAP-1", parent=None, changeset="CHG-1",
            artifacts=("workflow/routes.yaml",)):
    return lineage.record_verified(
        snapshot_id=snapshot, parent_snapshot_id=parent,
        changeset_id=changeset, actor="qa.lead",
        timestamp="2026-09-20T10:00:00Z", artifacts=artifacts)


# --- who owns it, and where it starts ----------------------------------------


def test_the_baseline_is_version_zero(lineage):
    """It is the reference. There was nothing to verify it against."""
    assert lineage.version == BASELINE_VERSION == 0
    assert lineage.head_snapshot == ""


def test_the_session_owns_the_version(lineage):
    assert lineage.session_id == "SESSION-A"
    assert hasattr(lineage, "version")


def test_there_is_one_counter_and_it_is_derived_not_stored(lineage):
    """A stored counter and a list can disagree. A derived one cannot."""
    assert "version" not in VerifiedLineage.__dataclass_fields__
    _verify(lineage)
    assert lineage.version == lineage.entries[-1].version


# --- the seven cases ----------------------------------------------------------


def test_apply_once_does_not_move_the_version(lineage):
    """Apply produces a snapshot. Only verification advances the state.

    Incrementing here would mean the version advances for a change nobody
    confirmed landed -- declared rather than actual.
    """
    assert lineage.version == 0  # an apply happened; nothing was told to this


def test_verify_once_increments_by_one(lineage):
    entry = _verify(lineage)
    assert lineage.version == 1
    assert entry.version == 1
    assert lineage.head_snapshot == "SNAP-1"


def test_repeated_verify_of_the_same_snapshot_does_not_increment(lineage):
    """The case that matters. A retry is not a second advance."""
    first = _verify(lineage)
    second = _verify(lineage)
    third = _verify(lineage)
    assert lineage.version == 1
    assert first is second is third
    assert len(lineage.entries) == 1


def test_apply_after_verify_increments_once_more(lineage):
    _verify(lineage, snapshot="SNAP-1", parent=None)
    _verify(lineage, snapshot="SNAP-2", parent="SNAP-1", changeset="CHG-2")
    assert lineage.version == 2
    assert lineage.head_snapshot == "SNAP-2"


def test_a_failed_apply_never_reaches_the_lineage(lineage):
    """No snapshot, nothing recorded, version unchanged."""
    assert lineage.version == 0
    assert lineage.entries == []


def test_a_failed_verification_does_not_increment(lineage):
    lineage.record_failure(snapshot_id="SNAP-1", changeset_id="CHG-1",
                           actor="qa.lead", timestamp="t",
                           reason="the spreadsheet still holds the old value")
    assert lineage.version == 0
    assert lineage.entries == []
    assert lineage.failures


def test_a_failed_verification_keeps_its_reason(lineage):
    """"Tried three times, failed the same way" is what a person needs."""
    for _ in range(3):
        lineage.record_failure(snapshot_id="SNAP-1", changeset_id="CHG-1",
                               actor="qa", timestamp="t",
                               reason="the spreadsheet still holds the old value")
    assert len(lineage.failures_for("SNAP-1")) == 3
    assert all("spreadsheet" in f.reason for f in lineage.failures_for("SNAP-1"))


def test_a_failure_with_no_reason_is_refused(lineage):
    with pytest.raises(NotDeterministic, match="indistinguishable"):
        lineage.record_failure(snapshot_id="S", changeset_id="C", actor="a",
                               timestamp="t", reason="")


def test_a_snapshot_that_failed_can_still_be_verified_later(lineage):
    """The drift was fixed and re-verified. That is one advance, not two."""
    lineage.record_failure(snapshot_id="SNAP-1", changeset_id="CHG-1",
                           actor="qa", timestamp="t", reason="incomplete")
    _verify(lineage, snapshot="SNAP-1")
    assert lineage.version == 1
    assert lineage.failures_for("SNAP-1")


def test_reverse_drift_produces_a_snapshot_that_is_not_verified(lineage):
    """An out-of-band edit advances the snapshot chain, never the version."""
    _verify(lineage, snapshot="SNAP-1")
    assert lineage.version == 1
    assert not lineage.is_verified("SNAP-2-external-edit")
    assert lineage.head_snapshot == "SNAP-1"


# --- parent and child ---------------------------------------------------------


def test_the_first_entry_may_descend_from_the_baseline(lineage):
    entry = _verify(lineage, snapshot="SNAP-1", parent=None)
    assert entry.parent_snapshot_id is None


def test_an_entry_whose_parent_is_not_the_head_is_refused(lineage):
    _verify(lineage, snapshot="SNAP-1")
    with pytest.raises(LineageMismatch, match="leave a gap"):
        _verify(lineage, snapshot="SNAP-3", parent="SNAP-2",
                changeset="CHG-3")


def test_the_mismatch_names_both_the_claimed_parent_and_the_head(lineage):
    _verify(lineage, snapshot="SNAP-1")
    with pytest.raises(LineageMismatch) as caught:
        _verify(lineage, snapshot="SNAP-3", parent="SNAP-2")
    assert "SNAP-2" in str(caught.value) and "SNAP-1" in str(caught.value)


def test_the_chain_includes_the_baseline(lineage):
    _verify(lineage, snapshot="SNAP-1")
    _verify(lineage, snapshot="SNAP-2", parent="SNAP-1", changeset="CHG-2")
    assert lineage.chain == ((0, ""), (1, "SNAP-1"), (2, "SNAP-2"))


def test_every_version_resolves_back_to_its_snapshot(lineage):
    _verify(lineage, snapshot="SNAP-1")
    _verify(lineage, snapshot="SNAP-2", parent="SNAP-1", changeset="CHG-2")
    assert lineage.at(1).snapshot_id == "SNAP-1"
    assert lineage.at(2).snapshot_id == "SNAP-2"
    with pytest.raises(KeyError):
        lineage.at(3)


def test_verification_refers_to_the_snapshot_not_the_version(lineage):
    """The snapshot id IS the state; a version needs this table to resolve."""
    entry = _verify(lineage, snapshot="SNAP-1")
    assert entry.snapshot_id
    assert lineage.entry_for("SNAP-1") is entry
    assert lineage.entry_for("SNAP-9") is None


# --- attribution and determinism ---------------------------------------------


def test_an_entry_without_an_actor_is_refused(lineage):
    with pytest.raises(NotDeterministic, match="verified_by"):
        lineage.record_verified(
            snapshot_id="S", parent_snapshot_id=None, changeset_id="C",
            actor="", timestamp="t")


def test_an_entry_without_a_timestamp_is_refused(lineage):
    with pytest.raises(NotDeterministic, match="verified_at"):
        lineage.record_verified(
            snapshot_id="S", parent_snapshot_id=None, changeset_id="C",
            actor="a", timestamp="")


def test_the_module_reads_no_clock():
    """Scanned by AST: a text scan flags the docstring that forbids it."""
    from tests.contract.test_verification import _executable_source
    from core.change_intelligence import lineage as module

    source = _executable_source(module)
    for banned in ("datetime.now", "time.time", "uuid", "random"):
        assert banned not in source, (
            f"{banned} would make the same transition produce a different "
            f"record on two runs")


def test_the_artifact_list_is_sorted_so_two_runs_match(lineage):
    entry = _verify(lineage, artifacts=("b.yaml", "a.yaml"))
    assert entry.artifacts == ("a.yaml", "b.yaml")


def test_rendering_is_deterministic(lineage):
    _verify(lineage)
    lineage.record_failure(snapshot_id="SNAP-2", changeset_id="CHG-2",
                           actor="qa", timestamp="t", reason="incomplete")
    assert render(lineage) == render(lineage)


# --- the boundary with the other version -------------------------------------


def test_this_module_does_not_touch_the_document_pipeline_ref_version():
    """Two counters for two things. Not one counter forced to mean both."""
    import inspect

    from core.change_intelligence import lineage as module

    source = inspect.getsource(module)
    for symbol in ("make_ref", "ArtifactRef", "ref_parts", "ref_id"):
        assert symbol not in source, (
            f"this module reaches into the document pipeline's ref system "
            f"via {symbol}; the two versions describe different things and "
            f"merging them would give one counter two meanings")
