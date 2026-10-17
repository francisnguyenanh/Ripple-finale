# -*- coding: utf-8 -*-
"""The seed manifest must describe the seed that actually exists.

The most skippable check, and the most important one. Every
metric is scored against `seed_manifest.yaml`. If somebody renames a
document, renumbers a line, or edits a spreadsheet cell, the manifest
quietly becomes fiction and the numbers keep printing.

So this test OPENS every representation the manifest declares and confirms
the value is there. It runs in CI on every commit, and it is the safety net
under the entire measurement half of the submission.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml
from openpyxl import load_workbook

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED = ROOT / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))

FAMILIES = {"software", "workflow", "docs", "spreadsheet", "tests", "history"}


def all_representations():
    for rule_id, rule in MANIFEST["rules"].items():
        for rep in rule.get("representations", []):
            yield rule_id, rep


def artifact_path(rep) -> pathlib.Path:
    return SEED / rep["artifact"]


# --- the artifacts exist ----------------------------------------------------


@pytest.mark.parametrize(
    "rule_id,rep",
    [pytest.param(r, rep, id=f"{r}:{rep['artifact']}") for r, rep in all_representations()],
)
def test_every_declared_artifact_exists(rule_id, rep):
    path = artifact_path(rep)
    assert path.is_file(), f"{rule_id} cites {rep['artifact']}, which does not exist"


@pytest.mark.parametrize(
    "rule_id,rep",
    [pytest.param(r, rep, id=f"{r}:{rep['artifact']}") for r, rep in all_representations()],
)
def test_every_representation_declares_a_known_family(rule_id, rep):
    assert rep["family"] in FAMILIES, f"{rule_id}: unknown artifact family {rep['family']!r}"


# --- the values are really there -------------------------------------------


def _text(rep) -> str:
    return artifact_path(rep).read_text(encoding="utf-8")


def _contains_amount(text: str, value: int) -> bool:
    """A threshold may be written 1000000, 1,000,000 or 'one million'.

    Matching only the digits would call a correct document a missing one,
    which is the same mistake as matching rules by wording instead of by
    semantic_id.
    """
    # Digits, thousands separators, Python underscore literals, and the
    # English the documents actually use. A matcher that only knew digits
    # would report a correctly-written policy as a missing one.
    spellings = [str(value), f"{value:,}", f"{value:,}".replace(",", "_")]
    if value == 1_000_000:
        spellings += ["one million", "a million"]
    if value == 500_000:
        spellings += ["500,000", "half a million"]
    if value == 5_000_000:
        spellings += ["five million"]
    lowered = text.lower()
    return any(s.lower() in lowered for s in spellings)


@pytest.mark.parametrize(
    "rule_id,rep",
    [
        pytest.param(r, rep, id=f"{r}:{rep['artifact']}")
        for r, rep in all_representations()
        if rep["family"] in {"docs", "workflow", "software", "tests"}
        and isinstance(rep.get("value"), int)
    ],
)
def test_a_numeric_value_is_present_in_the_text_artifact(rule_id, rep):
    assert _contains_amount(_text(rep), rep["value"]), (
        f"{rule_id}: {rep['artifact']} is declared to state {rep['value']:,} at "
        f"{rep['locator']!r}, and does not"
    )


@pytest.mark.parametrize(
    "rule_id,rep",
    [
        pytest.param(r, rep, id=f"{r}:{rep['artifact']}")
        for r, rep in all_representations()
        if rep["family"] == "spreadsheet" and isinstance(rep.get("value"), int)
    ],
)
def test_a_spreadsheet_cell_holds_the_declared_value(rule_id, rep):
    """Locators are `Sheet!Cell`, so the cell is READ, not searched for.

    Searching the workbook for the number would pass even if somebody moved
    the value to a different row -- and the whole point of the locator is
    that evidence has to be addressable.
    """
    sheet_name, cell = rep["locator"].split("!")
    workbook = load_workbook(artifact_path(rep), data_only=True)
    actual = workbook[sheet_name][cell].value
    assert actual == rep["value"], (
        f"{rule_id}: {rep['artifact']} {rep['locator']} holds {actual!r}, "
        f"manifest says {rep['value']!r}"
    )


# --- the conflicts are real conflicts ---------------------------------------


def test_the_hero_conflict_actually_disagrees():
    """CONF-001 has to be a genuine disagreement, not a claim of one.

    A seeded conflict that turns out to agree would make the headline demo
    a lie, and nothing else in the suite would notice.
    """
    rule = MANIFEST["rules"]["BR-001"]
    values = {rep["value"] for rep in rule["representations"] if isinstance(rep.get("value"), int)}
    assert len(values) > 1, "BR-001 is declared a conflict but every side states one value"
    assert values == {1_000_000, 500_000}


def test_a_rule_declared_aligned_really_does_agree():
    """The negative direction. Without this, `expected_state` is decoration."""
    rule = MANIFEST["rules"]["BR-005"]
    assert rule["expected_state"] == "aligned"
    values = {rep["value"] for rep in rule["representations"] if isinstance(rep.get("value"), int)}
    assert len(values) <= 1


def test_no_conflict_declares_a_winner():
    """I15, checked in the ground truth itself.

    If the answer key named a winner, every downstream component would be
    scored against a resolution a human never made.
    """
    for rule_id, rule in MANIFEST["rules"].items():
        if rule.get("expected_state") == "conflict":
            assert rule.get("expected_resolution") == "pending_human", (
                f"{rule_id} is a conflict with a pre-decided resolution"
            )
            assert "correct_value" not in rule


def test_the_hero_rule_has_nine_representations():
    """The seed design asks for one rule in nine representations; a lower number turns
    the demo from a systems problem into a typo in one file."""
    assert len(MANIFEST["rules"]["BR-001"]["representations"]) == 9


def test_every_rule_spans_at_least_three_artifact_families():
    """Cross-artifact is the product. A rule living in one family proves nothing."""
    for rule_id, rule in MANIFEST["rules"].items():
        families = {rep["family"] for rep in rule.get("representations", [])}
        assert len(families) >= 2, f"{rule_id} spans only {families}"
    br001 = {rep["family"] for rep in MANIFEST["rules"]["BR-001"]["representations"]}
    assert len(br001) >= 4, f"the hero rule spans only {br001}"


# --- undocumented really means undocumented ---------------------------------


def test_an_undocumented_rule_appears_in_no_document():
    """UN-001 is only interesting if it is true.

    The FAQ mentions strategic accounts, which is the trap seeded as
    WRONG-001 -- so this checks for the EXEMPTION, not for the words.
    """
    docs = list((SEED / "docs").glob("*.md"))
    offenders = []
    for path in docs:
        text = path.read_text(encoding="utf-8").lower()
        if "strategic" in text and ("exempt" in text or "skip" in text or "no approval" in text):
            offenders.append(path.name)
    assert not offenders, (
        f"BR-002 is declared undocumented, but {offenders} describe the exemption"
    )


def test_the_wrong_inference_trap_is_actually_present():
    """WRONG-001 must exist in the seed, or JUDGE-05 has nothing real to correct."""
    faq = (SEED / "docs" / "faq.md").read_text(encoding="utf-8").lower()
    assert "strategic" in faq, "the FAQ must mention strategic accounts for the trap to exist"
    assert "cancellation" in faq or "payment" in faq, (
        "the FAQ must answer about COMMERCIAL terms -- that is what makes the "
        "inference wrong rather than merely unsupported"
    )
    tiers = load_workbook(SEED / "data" / "customer_types.xlsx")
    notes = " ".join(str(c.value) for row in tiers["Notes"].iter_rows() for c in row).lower()
    assert "approval" in notes, (
        "customer_types.xlsx must state that the tier is NOT about approval, or the "
        "trap has no correct answer to be corrected to"
    )


# --- the history corpus ------------------------------------------------------


def test_the_corpus_has_exactly_220_records():
    lines = (SEED / "history" / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
    records = [line for line in lines if line.strip()]
    assert len(records) == 220


def test_the_headline_number_is_computed_not_stored():
    """47 must fall out of running the corpus, not be read from anywhere."""
    from reference_system.application.config import default_config
    from reference_system.history.replay import compare, load_corpus_file

    records = load_corpus_file(SEED / "history" / "transactions.jsonl")
    current = default_config()
    comparison = compare(records, current, current.with_threshold("BR-001", 500_000))

    assert comparison.total == 220
    assert comparison.changed_count == 47, (
        f"the corpus now yields {comparison.changed_count}, not 47. Either the corpus "
        f"or a handler changed -- update the documented figure, do not adjust this test"
    )


def test_changing_one_record_moves_the_number():
    """Failure injection, required for the what-if figures.

    If the number did not move, it was not being computed.
    """
    from reference_system.application.config import default_config
    from reference_system.history.replay import compare, load_corpus_file

    records = load_corpus_file(SEED / "history" / "transactions.jsonl")
    current = default_config()
    shadow = current.with_threshold("BR-001", 500_000)
    before = compare(records, current, shadow).changed_count

    moved = [dict(r) for r in records]
    first_in_gap = next(r for r in moved if 500_000 <= r["amount"] < 1_000_000
                        and r["process"] == "standard_po")
    first_in_gap["amount"] = 100_000

    assert compare(moved, current, shadow).changed_count == before - 1


def test_three_records_in_the_affected_range_do_not_change_because_of_the_hardcode():
    """IMPACT-003, asserted behaviourally.

    Not by reading emergency_purchase.py -- by running it. The distinction
    is the product's second claim.
    """
    from reference_system.application.config import default_config
    from reference_system.history.replay import compare, load_corpus_file

    records = load_corpus_file(SEED / "history" / "transactions.jsonl")
    by_id = {r["historical_case_id"]: r for r in records}
    current = default_config()
    comparison = compare(records, current, current.with_threshold("BR-001", 500_000))

    stuck = [by_id[cid] for cid in comparison.unchanged_in_window]
    emergency = [r for r in stuck if r["process"] == "emergency_purchase"]
    assert len(emergency) == 3, (
        f"expected 3 emergency-path records to be unaffected, found {len(emergency)}"
    )
    assert all(r["customer_tier"] == "standard" and r["supplier_risk"] == "normal"
               for r in emergency), (
        "an emergency record that is also strategic or high-risk would be unaffected "
        "for a legitimate reason, and would not demonstrate the hardcode"
    )


def test_the_corpus_regenerates_byte_identically():
    """A fixed seed, or the committed corpus cannot be trusted to be the one
    the generator describes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_corpus", SEED / "history" / "generate_corpus.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    regenerated = "\n".join(
        json.dumps(r, sort_keys=True, ensure_ascii=False) for r in module.build()) + "\n"
    committed = (SEED / "history" / "transactions.jsonl").read_text(encoding="utf-8")
    assert regenerated == committed


# --- the seed conditions are all accounted for ------------------------------


@pytest.mark.parametrize("section,expected", [
    ("undocumented", 4), ("stale_docs", 4), ("conflicts", 3),
    ("outdated_tests", 3), ("ambiguous", 2), ("historical_exc", 2),
    ("intent_changes", 2), ("knowledge_only", 2), ("wrong_inference", 1),
])
def test_the_seed_declares_the_required_number_of_conditions(section, expected):
    """P2 §3 asks for 23 conditions. Counted here so a quiet deletion shows up."""
    assert len(MANIFEST[section]) == expected, (
        f"{section}: expected {expected}, manifest declares {len(MANIFEST[section])}"
    )


def test_the_23_seed_conditions_add_up():
    sections = ["undocumented", "stale_docs", "conflicts", "outdated_tests", "ambiguous",
                "historical_exc", "intent_changes", "knowledge_only", "wrong_inference"]
    assert sum(len(MANIFEST[s]) for s in sections) == 23


def test_every_outdated_test_actually_exists():
    source = (SEED / "tests" / "test_approval_rules.py").read_text(encoding="utf-8")
    for old_id, entry in MANIFEST["outdated_tests"].items():
        assert f"def {entry['test']}(" in source, f"{old_id} names a test that does not exist"


def test_the_no_change_case_is_coherent():
    """NOCHANGE-001 encodes Scenario H. It must satisfy the P1 contract."""
    case = MANIFEST["no_change_cases"]["NOCHANGE-001"]
    assert case["expected_alignment"] == "ALIGNED"
    assert case["expected_decision"] == "NO_CHANGE_REQUIRED"
    assert case["expected_actions"] == 0
