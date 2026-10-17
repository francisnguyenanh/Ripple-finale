# -*- coding: utf-8 -*-
"""H1 -- the contract is a dated changelog, and this pins that it stays one.

The decision and its evidence are in
`docs/verification/h1_and_wiring_decisions.md`. This file exists so the
decision cannot be quietly reversed in either direction: nobody builds a
git integration the contract does not ask for, and nobody weakens the
provenance that P8 actually relies on.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DECISION = ROOT / "docs" / "verification" / "h1_and_wiring_decisions.md"

#: Whole-word match. A substring scan flags "legitimate", which contains
#: "git" -- the same false positive this repository has now fixed four
#: times under four different names.
WORD = r"\b%s\b"


def _production_files():
    for root in ("core", "reference_system"):
        for path in (ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            yield path


# --- the decision is written down --------------------------------------------


def test_the_decision_exists_and_states_which_way_it_went():
    text = DECISION.read_text(encoding="utf-8")
    collapsed = " ".join(text.split())
    assert "Do NOT build a Git-backed provenance layer" in collapsed
    assert "domain audit lineage" in text


def test_the_decision_names_the_three_consumers_it_was_checked_against():
    text = DECISION.read_text(encoding="utf-8")
    for consumer in ("_has_time_ordering", "stale_from_history",
                     "P8 verification"):
        assert consumer in text


# --- no git integration was built --------------------------------------------


def test_no_production_module_shells_out_to_git():
    offenders = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            rendered = ast.unparse(node)
            if ("subprocess" in rendered or "popen" in rendered.lower()) \
                    and "git" in rendered.lower():
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"{offenders} shell out to git; H1 was resolved as domain audit "
        f"lineage, and a git integration is a separate platform project")


def test_no_production_module_imports_a_git_library():
    offenders = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0] in ("git", "dulwich", "pygit2"):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


# --- what the contract does require ------------------------------------------


def test_time_ordering_comes_from_a_dated_record_not_a_commit():
    from core.change_intelligence import alignment

    source = inspect.getsource(alignment._has_time_ordering)
    assert "observed_at" in source
    assert "commit" not in source.lower()


def test_the_changelog_records_carry_a_date_and_an_author():
    import json

    path = ROOT / "reference_system" / "history" / "changelog.jsonl"
    records = [json.loads(line) for line in
               path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records
    for record in records:
        assert record["date"], record
        assert record["author"], record


def test_the_changelog_carries_what_git_could_not_supply():
    """A commit says a line changed. It does not say somebody meant to
    change it back -- which is the finding this family exists for."""
    import json

    path = ROOT / "reference_system" / "history" / "changelog.jsonl"
    records = [json.loads(line) for line in
               path.read_text(encoding="utf-8").splitlines() if line.strip()]
    declared_temporary = [r for r in records if "temporary" in r.get("note", "")]
    assert declared_temporary, (
        "no change declares itself temporary; the most valuable thing this "
        "family extracts is an intention that was recorded and not carried out")


# --- P8's provenance comes from the lineage ----------------------------------


def test_reverse_drift_is_derived_from_the_lineage_not_a_repository():
    """Matched on whole words in executable code only.

    A substring scan flags "legitimate", which contains "git" -- the same
    class of false positive that has now been fixed four times in this
    repository. Docstrings are stripped and the match is word-bounded.
    """
    import re

    from tests.contract.test_verification import _executable_source
    from core.change_intelligence import verification

    assert "REVERSE_DRIFT" in inspect.getsource(verification)
    source = _executable_source(verification).lower()
    for banned in ("git", "commit", "repository", "revision"):
        assert not re.search(WORD % banned, source), (
            f"verification refers to {banned!r}; the provenance question P8 "
            f"asks is answered by the snapshot chain, and the runtime never "
            f"touches a repository")


def test_a_snapshot_is_addressable_without_any_repository():
    from core.change_intelligence.session import (
        InMemoryBaseline,
        InMemoryOverlay,
    )

    overlay = InMemoryOverlay(
        InMemoryBaseline.from_artifacts({"a.yaml": b"v: 1\n"}), "S")
    before = overlay.head
    after = overlay.write("a.yaml", b"v: 2\n")
    assert after.snapshot_id and after.parent_snapshot_id == before.snapshot_id


def test_an_unverified_snapshot_is_exactly_the_reverse_drift_signal():
    from core.change_intelligence.lineage import VerifiedLineage

    lineage = VerifiedLineage(session_id="S", baseline_id="B")
    lineage.record_verified(snapshot_id="SNAP-1", parent_snapshot_id=None,
                            changeset_id="CHG-1", actor="qa", timestamp="t")
    assert lineage.is_verified("SNAP-1")
    assert not lineage.is_verified("SNAP-2"), (
        "a snapshot with no lineage entry IS a change nobody confirmed; "
        "that is the whole provenance mechanism P8 needs")


# --- the limitation stays stated ---------------------------------------------


@pytest.mark.parametrize("document", [
    "docs/verification/gate_G2.md",
    "docs/verification/h1_and_wiring_decisions.md",
])
def test_the_limitation_is_stated_where_a_reader_would_look(document):
    text = (ROOT / document).read_text(encoding="utf-8").lower()
    assert "git" in text, (
        f"{document} no longer says that history is declared rather than "
        f"excavated; a limitation nobody states is one a reader discovers")
