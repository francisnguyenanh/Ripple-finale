# -*- coding: utf-8 -*-
"""The seven verification questions, as tests rather than as a checklist.

These are grep-shaped claims about the codebase. They are cheap now and
expensive late, which is the whole argument for writing them early: a
boundary that was never asserted is a boundary that has already moved.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / "core" / "change_intelligence"
CORE = ROOT / "core"


def _py(root: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in root.rglob("*.py")
            if "__pycache__" not in p.parts and "tests" not in p.parts]


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Lines with comments stripped and docstring blocks skipped.

    A glossary rule or an architecture rule that fires on prose is a rule
    nobody can write documentation under; every one of these tests is about
    what the code DOES.
    """
    out, in_doc, delim = [], False, ""
    for n, raw in enumerate(_text(path).splitlines(), 1):
        line = raw
        if in_doc:
            if delim in line:
                in_doc = False
                line = line.split(delim, 1)[1]
            else:
                continue
        while True:
            m = re.search(r'("""|\'\'\')', line)
            if not m:
                break
            delim = m.group(1)
            rest = line[m.end():]
            if delim in rest:
                line = line[: m.start()] + rest.split(delim, 1)[1]
                continue
            line = line[: m.start()]
            in_doc = True
            break
        line = re.sub(r"#.*$", "", line)
        if line.strip():
            out.append((n, line))
    return out


# --- Q5: tier 2 owns no infrastructure --------------------------------------


@pytest.mark.parametrize("forbidden", ["sqlite3", "open(", "Path(", "psycopg", "boto3",
                                       "google.cloud", "requests."])
def test_q5_change_intelligence_never_names_infrastructure(forbidden):
    """ADR-001: tier 2 is served from Cloud Run and must not know what is
    behind a storage contract.

    This is the test that keeps the two-tier architecture honest. The moment
    `change_intelligence/` opens a file, it stops being deployable as a
    stateless service and the failure appears at D25, not here.
    """
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _py(CI)
        for n, line in _code_lines(p)
        if forbidden in line
    ]
    assert not offenders, (
        f"core/change_intelligence/ references {forbidden!r} at {offenders}; "
        f"tier 2 must reach storage only through the contracts in storage.py"
    )


def test_q5_change_intelligence_imports_no_provider():
    """I11, from the other side: contracts do not call models."""
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _py(CI)
        for n, line in _code_lines(p)
        if re.search(r"\b(provider|genai|vertexai|openai|anthropic)\b", line)
    ]
    assert not offenders, offenders


# --- Q1: one definition per enum --------------------------------------------


@pytest.mark.parametrize("member", ["PROPOSED", "SUPERSEDED", "APPLYING", "VERIFIED",
                                    "NO_CHANGE_REQUIRED", "HUMAN_REVIEW_REQUIRED"])
def test_q1_no_state_enum_is_defined_twice(member):
    """A second definition is how two halves of a system come to disagree
    about what APPROVED means without anyone noticing."""
    pattern = re.compile(rf"^\s*{member}\s*=\s*['\"]")
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _py(CI)
        if p.name != "states.py"
        for n, line in _code_lines(p)
        if pattern.match(line)
    ]
    assert not offenders, (
        f"{member} is defined outside states.py at {offenders}; every module "
        f"must import the vocabulary rather than restate it"
    )


# --- Q7: the two axes have not merged ---------------------------------------


def test_q7_no_change_required_is_never_treated_as_an_alignment_state():
    """The grep for verification question 7, run as a test.

    Looks for the two names within a few lines of each other in real code,
    which catches `AlignmentState.NO_CHANGE_REQUIRED` and also the subtler
    `if alignment == NO_CHANGE_REQUIRED`.
    """
    offenders = []
    for path in _py(CORE):
        lines = _code_lines(path)
        for i, (n, line) in enumerate(lines):
            if "NO_CHANGE_REQUIRED" not in line:
                continue
            window = " ".join(l for _, l in lines[max(0, i - 2): i + 3])
            if re.search(r"AlignmentState\s*[.\[]\s*['\"]?NO_CHANGE_REQUIRED", window):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, offenders


# --- Q4: no path from Intent into the Fact layer ----------------------------


# --- two boundary gaps closed by explicit tests -------------------------


def test_i9_renderers_never_reach_a_model():
    """I9: a document is a projection, never a source of truth.

    No earlier test asserted this. If a renderer could call a model, the
    rendered document would contain claims that exist nowhere in the
    knowledge model -- and evidence grounding would silently stop meaning
    anything.
    """
    renderers = CORE / "renderers"
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _py(renderers)
        for n, line in _code_lines(p)
        if re.search(r"\b(provider|complete_structured|genai|vertexai|agent)\b", line)
    ]
    assert not offenders, offenders


# --- storage contract shape -------------------------------------------------


def test_six_storage_contracts_are_declared_with_their_access_level():
    from core.change_intelligence.storage import CONTRACTS

    assert set(CONTRACTS) == {
        "BaselineArtifactStore", "SessionArtifactOverlay", "EvidenceStore",
        "GraphStore", "StateStore", "SessionStore",
    }
    assert CONTRACTS["BaselineArtifactStore"] == "read-only"
    assert CONTRACTS["GraphStore"] == "read-only"
    assert CONTRACTS["EvidenceStore"] == "read-only"


def test_physical_storage_is_frozen_and_forbids_binary_in_cloud_sql():
    from core.change_intelligence.storage import PHYSICAL_STORAGE

    assert PHYSICAL_STORAGE["binary_in_cloud_sql"] == "NEVER"
    assert "Cloud Storage" in PHYSICAL_STORAGE["artifact_bytes"]
    assert "Cloud SQL" in PHYSICAL_STORAGE["metadata"]
