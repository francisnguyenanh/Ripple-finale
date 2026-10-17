# -*- coding: utf-8 -*-
"""The reference system and the product must not know each other.

`reference_system/` is the enterprise being examined. `core/` is the thing
doing the examining. If either learns the other's name, the demo stops
being a demonstration and becomes a special case: a judge would be right to
ask whether the product works on anything but this seed.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from _srcscan import code_lines

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _py(root: pathlib.Path):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("word", ["reference_system", "acme", "procurement", "approval_threshold"])
def test_the_product_never_names_the_reference_system(word):
    """Reads CODE, not prose.

    A comment explaining why a domain term was REMOVED from the product is
    not the product knowing the term. Firing on prose would mean the only
    way to record that reasoning is to not record it -- and this repository
    depends on the reasoning being written where the next person reads it.
    """
    offenders = [
        f"{p.relative_to(ROOT)}:{n}"
        for p in _py(ROOT / "core")
        if "tests" not in p.parts
        for n, line in code_lines(p)
        if re.search(rf"\b{word}\b", line, re.I)
    ]
    assert not offenders, (
        f"core/ mentions {word!r} in {offenders}; the product must reach a project "
        f"only through its profile, never by knowing its name"
    )


def test_the_reference_system_never_imports_the_product():
    """The enterprise being examined cannot depend on the examiner.

    If it did, the reference system would no longer be a stand-in for a
    real customer codebase -- no real one imports us.
    """
    offenders = []
    for path in _py(ROOT / "reference_system"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"\s*(from|import)\s+core\b", line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, offenders


def test_the_reference_application_never_reads_its_own_location():
    """ADR-002 C-1. A module imported from snapshot bytes has no real
    `__file__`, so any artifact that reads one cannot be simulated."""
    offenders = []
    for path in _py(ROOT / "reference_system" / "application"):
        text = path.read_text(encoding="utf-8")
        for token in ["__file__", "os.getcwd", "Path(__file__)"]:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert not offenders, (
        f"{offenders} -- the application must be runnable from bytes that were "
        f"never written to disk (ADR-002 C-1)"
    )


def test_the_reference_application_never_reads_configuration_itself():
    """C-1 again, from the other direction: config arrives as an argument."""
    offenders = []
    for path in _py(ROOT / "reference_system" / "application"):
        if path.name == "config.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in ["os.environ", "getenv", "open(", "yaml.safe_load(open"]:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert not offenders, offenders


def test_the_headline_number_is_not_a_constant_anywhere_in_the_product():
    """`47` must never appear as a literal.

    It is the length of a list produced by running 220 records. A constant
    would be a claim; a computation is a fact.
    """
    offenders = []
    for root in [ROOT / "core" / "change_intelligence", ROOT / "reference_system" / "application"]:
        for path in _py(root):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = re.sub(r"#.*$", "", line)
                if re.search(r"(?<![\w.])47(?![\w.])", code):
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {code.strip()}")
    assert not offenders, offenders
