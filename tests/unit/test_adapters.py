# -*- coding: utf-8 -*-
"""The four adapters, one test per construct that matters.

Each adapter is judged on what the product needs from it, not on parse
coverage. The two that carry weight:

    the workflow adapter must produce a value WITH A UNIT
    the python adapter must tell a hardcode from a config read
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.policy_text.adapter import ADAPTER as POLICY
from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.adapters.source.history.adapter import ADAPTER as HISTORY
from core.adapters.source.python.adapter import ADAPTER as PYTHON
from core.adapters.workflow.yaml_routes.adapter import ADAPTER as WORKFLOW

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"


HINTS = yaml.safe_load(
    (SEED / "seed_manifest.yaml").read_text(encoding="utf-8")).get("semantic_hints", {})


def records(adapter, relative: str, *, doc: bool = False):
    raw = (SEED / relative).read_bytes()
    if doc:
        # Hints come from the project, never from the product (P2 §5). A
        # test that omitted them would exercise a path no deployment takes.
        return list(adapter.extract_bytes(raw, relative, "SNAP-TEST", HINTS))
    return list(adapter.parse_bytes(raw, relative, "SNAP-TEST"))


def of_type(recs, type_name):
    return [r for r in recs if r.payload.get("type") == type_name]


# --- workflow ---------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow():
    return records(WORKFLOW, "workflow/approval_routes.yaml")


def test_a_threshold_carries_its_unit_not_just_its_number(workflow):
    """Without the unit, identity resolution is string matching.

    `1000000`, `¥1,000,000` and "one million yen" are one rule; a threshold
    extracted as a bare string can never be compared with the others.
    """
    thresholds = of_type(workflow, "threshold")
    assert thresholds, "no thresholds extracted from the workflow"
    for record in thresholds:
        assert record.payload["unit"] == "JPY"
        assert isinstance(record.payload["value"], int)
        assert record.payload["op"] in {">", ">=", "<", "<=", "==", "!="}


def test_the_hero_threshold_is_extracted_with_its_rule_id(workflow):
    br001 = [r for r in of_type(workflow, "threshold") if r.payload["rule_id"] == "BR-001"]
    assert len(br001) == 1
    assert br001[0].payload["value"] == 1_000_000


def test_a_non_numeric_condition_is_a_predicate_not_a_threshold(workflow):
    """`customer_tier == strategic` is a rule boundary with no magnitude.

    Calling it a threshold would put it in the same structural bucket as an
    amount comparison, which is how two unrelated rules get merged.
    """
    predicates = of_type(workflow, "predicate")
    assert any(r.payload["field"] == "customer_tier" for r in predicates)
    assert not any(r.payload.get("unit") for r in predicates)


def test_an_exception_that_states_no_value_says_so(workflow):
    """UN-002's first half: the config delegates the ceiling to code."""
    exceptions = of_type(workflow, "exception")
    assert len(exceptions) == 1
    assert exceptions[0].payload["handler"] == "emergency_purchase"
    assert exceptions[0].payload["states_value"] is False


def test_every_workflow_record_carries_a_line_number(workflow):
    """Evidence has to be addressable, or it cannot be verified (I4)."""
    for record in workflow:
        assert record.provenance
        assert record.provenance[0].line_start >= 1


# --- python -----------------------------------------------------------------


def test_the_seeded_hardcode_is_found_and_labelled_as_one():
    recs = records(PYTHON, "application/emergency_purchase.py")
    hardcodes = of_type(recs, "hardcoded_threshold")
    assert len(hardcodes) == 1
    assert hardcodes[0].payload["value"] == 1_000_000
    assert hardcodes[0].payload["from_config"] is False


def test_a_handler_that_reads_the_policy_is_not_reported_as_a_hardcode():
    """The negative direction, and the one that decides precision.

    An adapter that flagged every integer would find the defect and bury it
    among every correct handler in the application.
    """
    recs = records(PYTHON, "application/approval_service.py")
    assert of_type(recs, "hardcoded_threshold") == []
    assert of_type(recs, "config_threshold"), "the correct handler reads config and must say so"


def test_a_threshold_read_through_a_local_variable_still_counts_as_config():
    """`t = config.threshold(...)` then `amount >= t` is the ordinary shape.

    Missing it would make every correct handler look like one with no
    policy at all.
    """
    recs = records(PYTHON, "application/approval_service.py")
    attributes = {r.payload["attribute"] for r in of_type(recs, "config_threshold")}
    assert "threshold" in attributes


def test_a_function_that_takes_a_config_and_ignores_it_is_named():
    recs = records(PYTHON, "application/emergency_purchase.py")
    usages = {r.payload["function"]: r.payload for r in of_type(recs, "config_usage")}
    assert usages["evaluate_emergency"]["ignores_config"] is True
    assert usages["evaluate_emergency"]["reads_config"] is False


def test_passing_the_config_on_is_not_ignoring_it():
    """Otherwise every dispatcher in the application joins the finding."""
    recs = records(PYTHON, "application/emergency_purchase.py")
    usages = {r.payload["function"]: r.payload for r in of_type(recs, "config_usage")}
    assert usages["route_emergency"]["forwards_config"] is True
    assert usages["route_emergency"]["ignores_config"] is False


def test_a_rule_declared_in_a_constructor_is_extracted():
    """config.py states the policy in a constructor, not a comparison.

    Without this the software family is absent from the hero rule, and the
    artifact a change must be applied to never appears in its own impact.
    """
    recs = records(PYTHON, "application/config.py")
    declared = {r.payload["rule_id"]: r.payload for r in recs if r.payload.get("rule_id")}
    assert declared["BR-001"]["value"] == 1_000_000
    assert declared["BR-004"]["value"] == 5_000_000
    assert declared["BR-002"]["value"] is None, "BR-002 has no threshold; inventing one would be a fact"


def test_a_small_literal_is_not_treated_as_a_business_threshold():
    """`len(x) > 3` must not arrive as a candidate business rule."""
    source = b"def f(items, config):\n    if len(items) > 3:\n        return 1\n    return 0\n"
    recs = list(PYTHON.parse_bytes(source, "x.py", "S"))
    assert of_type(recs, "hardcoded_threshold") == []


# --- history ------------------------------------------------------------------


@pytest.fixture(scope="module")
def history():
    return records(HISTORY, "history/changelog.jsonl")


def test_the_unreverted_temporary_change_is_found(history):
    """The finding that turns a diff into a forgotten decision."""
    unreverted = of_type(history, "unreverted_temporary_change")
    assert len(unreverted) == 1
    assert unreverted[0].payload["change_id"] == "CHG-2020-011"
    assert unreverted[0].payload["semantic_id"] == "strategic_customer_exemption"


def test_a_change_recording_that_something_ENDED_is_not_a_temporary_change(history):
    """"campaign closed; routing left in place" is the opposite of a
    temporary declaration, and an earlier version of the marker list fired
    on it -- one finding reported as two."""
    ids = {r.payload["change_id"] for r in of_type(history, "unreverted_temporary_change")}
    assert "CHG-2021-001" not in ids


def test_an_unsynchronised_change_is_flagged(history):
    events = of_type(history, "change_event")
    unsynced = [e.payload["change_id"] for e in events if e.payload["declares_unsynchronised"]]
    assert "CHG-2022-006" in unsynced


def test_the_last_change_for_a_rule_is_the_recency_marker(history):
    """This is what decides DRIFT against CONFLICT."""
    from core.adapters.source.history.adapter import HistoryAdapter

    latest = HistoryAdapter.last_change_for(history, "approval_threshold")
    assert latest["date"] == "2022-06-14"
    assert HistoryAdapter.last_change_for(history, "a_rule_nobody_ever_touched") is None


# --- documents ------------------------------------------------------------------


def test_a_document_claim_is_about_the_DOCUMENT_not_about_the_world():
    """I9. The single most load-bearing distinction in the ingestion layer."""
    recs = records(POLICY, "docs/procurement-policy.md", doc=True)
    claims = of_type(recs, "doc_claim")
    assert claims
    for record in claims:
        assert record.payload["type"] == "doc_claim"
        assert record.payload["document"].endswith(".md")
        assert record.payload["claim_text"]
        assert "rule" != record.payload["type"]


def test_an_amount_spelled_in_words_is_read_as_the_same_amount():
    """A policy saying "one million yen" states the rule a policy saying
    1,000,000 states. Digits-only reading reports it as undocumented."""
    recs = records(POLICY, "docs/manager-guide.md", doc=True)
    assert any(r.payload["value"] == 1_000_000 for r in of_type(recs, "doc_claim"))


def test_a_section_number_is_not_mistaken_for_an_amount():
    source = b"# Policy\n\nSee section 2.1 and clause 4 for details.\n"
    claims = of_type(list(POLICY.extract_bytes(source, "x.md", "S", HINTS)), "doc_claim")
    assert claims == []


def test_the_sentence_outranks_the_heading_for_a_hint():
    """The SOP's capex row sits under a heading called "Approval thresholds".

    Taking the hint from the heading labels a Finance rule as the manager
    approval rule -- the mis-grouping R5 calls the worst failure of the phase.
    """
    recs = records(POLICY, "docs/procurement-sop.md", doc=True)
    capex = [r for r in of_type(recs, "doc_claim") if r.payload["value"] == 5_000_000]
    assert capex and capex[0].payload["semantic_hint"] == "capex_finance_threshold"


def test_a_claim_whose_sentence_names_no_subject_gets_no_hint():
    """"The figure is one million yen" is genuinely ambiguous alone.

    Producing a hint here would be a guess dressed as an extraction. The
    honest output is no hint, and a Gap downstream.
    """
    recs = records(POLICY, "docs/faq.md", doc=True)
    # Select the sentence under test by its text rather than by position.
    # The FAQ also carries a qualitative claim ("the system approves small
    # requests") that legitimately hints at approval_threshold, and indexing
    # into claims[0] silently made this test about a different sentence.
    ambiguous = [r for r in of_type(recs, "doc_claim")
                 if "The figure is one million yen" in r.payload["sentence"]]
    assert ambiguous, "the ambiguous FAQ sentence must still be extracted"
    assert ambiguous[0].payload["semantic_hint"] == ""


# --- spreadsheet -----------------------------------------------------------------


def test_the_spreadsheet_value_is_read_from_its_cell():
    raw = (SEED / "data" / "approval_matrix.xlsx").read_bytes()
    claims = list(read_xlsx(raw, "data/approval_matrix.xlsx", "S"))
    br001 = [c for c in claims if c.payload["rule_id"] == "BR-001"]
    assert len(br001) == 1
    assert br001[0].payload["value"] == 500_000
    assert br001[0].payload["cell"] == "C2"
    assert br001[0].payload["last_reviewed"] == "2022-06-14"


def test_a_sheet_with_no_threshold_column_yields_nothing():
    """customer_types.xlsx describes commercial terms, not rules.

    Extracting from it would put payment windows in the same layer as
    approval thresholds.
    """
    raw = (SEED / "data" / "customer_types.xlsx").read_bytes()
    assert list(read_xlsx(raw, "data/customer_types.xlsx", "S")) == []


# --- determinism ------------------------------------------------------------------


@pytest.mark.parametrize("adapter,relative,doc", [
    (WORKFLOW, "workflow/approval_routes.yaml", False),
    (PYTHON, "application/approval_service.py", False),
    (HISTORY, "history/changelog.jsonl", False),
    (POLICY, "docs/procurement-sop.md", True),
])
def test_parsing_twice_gives_the_same_records(adapter, relative, doc):
    """P3-T05's contract. Anything that consults a clock or a set iteration
    order breaks replay, and replay is what P8 is built on."""
    first = [(r.kind, r.id, r.payload) for r in records(adapter, relative, doc=doc)]
    second = [(r.kind, r.id, r.payload) for r in records(adapter, relative, doc=doc)]
    assert first == second
