# -*- coding: utf-8 -*-
"""Glossary consistency, enforced mechanically.

The rule is about code, not prose. A check that fires on documentation is a
check under which nobody can write documentation -- including the glossary
itself, which has to name the forbidden words in order to forbid them.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from test_architecture import _code_lines  # same docstring/comment stripping

ROOT = pathlib.Path(__file__).resolve().parents[2]
GLOSSARY = ROOT / "docs" / "domain" / "glossary.md"

#: Where the product's own vocabulary has to hold. `core/` at large is the
#: inherited framework and speaks its own dialect; imposing this glossary on
#: 56,000 lines written for another domain would be a rename, not a contract.
PRODUCT_CODE = [ROOT / "core" / "change_intelligence"]

BANNED = {
    "mismatch": "drift or conflict",
    "discrepancy": "drift or conflict",
}

TERMS = [
    "Artifact", "Reality Fact", "Knowledge Record", "Intent", "Evidence",
    "Conflict", "Gap", "Snapshot", "Impact Edge", "Shadow Change",
    "Behavioral Scenario", "ChangeSet", "Approval", "Verification",
    "Alignment State", "Change Decision",
]


def _product_files() -> list[pathlib.Path]:
    return [
        p
        for root in PRODUCT_CODE
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def test_the_glossary_exists_and_defines_every_term():
    text = GLOSSARY.read_text(encoding="utf-8")
    missing = [t for t in TERMS if f"**{t}**" not in text]
    assert not missing, f"glossary does not define: {missing}"


@pytest.mark.parametrize("word,instead", sorted(BANNED.items()))
def test_a_banned_synonym_never_appears_in_product_code(word, instead):
    """`mismatch` and `discrepancy` say that two things differ without saying
    whether we know which is right -- which is the entire distinction the
    product sells."""
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _product_files()
        for n, line in _code_lines(p)
        if re.search(rf"\b{word}\b", line, re.I)
    ]
    assert not offenders, (
        f"{word!r} appears in product code at {offenders}; say {instead} instead"
    )


def test_inconsistency_is_reserved_for_simulation_and_says_so():
    """The one deliberate exception, and the reason it is not an accident.

    In simulation, an Inconsistency is two handlers giving different answers
    for one order AT RUNTIME. That is a different fact from two documents
    disagreeing, and merging the two words would be the real confusion.
    """
    text = GLOSSARY.read_text(encoding="utf-8")
    assert "Inconsistency" in text and "simulation" in text.lower(), (
        "the glossary must state why `Inconsistency` is allowed to exist "
        "alongside drift and conflict, or the next reader will 'fix' it"
    )


def test_drift_and_conflict_are_defined_by_time_ordering():
    """The definition in the glossary must be the definition in the code.

    Prose and predicate drifting apart is how a video comes to describe a
    system that does not exist.
    """
    text = GLOSSARY.read_text(encoding="utf-8")
    assert "MOI HON" in text or "mới hơn" in text.lower()

    from core.change_intelligence.alignment import Representation, classify
    from core.change_intelligence.states import AlignmentState

    def rep(name, value, at=None):
        return Representation(artifact=name, semantic_id="x", value=value,
                              locator="l", observed_at=at)

    assert classify([rep("a", 1), rep("b", 2)]) is AlignmentState.CONFLICT
    assert classify([rep("a", 1, "2020"), rep("b", 2, "2022")]) is AlignmentState.DRIFT


def test_the_hero_scenario_is_documented_as_a_conflict():
    """Saying DRIFT in the video would recommend editing an artifact we have
    no evidence is the wrong one."""
    text = GLOSSARY.read_text(encoding="utf-8")
    hero = text.split("Hero scenario")[1][:600] if "Hero scenario" in text else ""
    assert "CONFLICT" in hero, "the hero scenario must be labelled a CONFLICT"
