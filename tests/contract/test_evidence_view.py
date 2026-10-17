# -*- coding: utf-8 -*-
"""The six-column evidence view.

The plan puts most of the UX weight of the product here: a reader clicks one
finding and sees the whole chain at once. So the tests are mostly about what
the view refuses to do -- drop a claim, blank an empty cell, or let a test
file count as documentation.

The `test` column is the one worth the trouble. A green test asserting
behaviour no document describes is the strongest available evidence that the
behaviour is INTENDED, which is the input P5 needs. Before this view nothing
in the product read a test file at all.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.discovery import discover
from core.change_intelligence.evidence_view import (
    COLUMNS,
    UnrepresentedEvidence,
    build_evidence_view,
    locate_value,
    render,
)
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ingested():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(InMemoryBaseline.from_artifacts(built["artifacts"]),
                              "SESSION-EVIDENCE-VIEW")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()])
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    report = discover(model, history=ingestion.records)
    return ingestion, report


@pytest.fixture(scope="module")
def hero(ingested):
    ingestion, report = ingested
    return report.by_semantic_id("approval_threshold")[0], ingestion


# --- shape ---------------------------------------------------------------------


def test_the_view_has_six_columns_in_the_plan_s_order(hero):
    """Left to right is a reading order, not a set. Business claim first
    because it is the question; history last because it is the answer to
    "how did this happen"."""
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    assert tuple(c.name for c in view.columns) == COLUMNS
    assert COLUMNS[0] == "business claim" and COLUMNS[-1] == "history"


def test_every_column_of_the_hero_finding_is_populated(hero):
    """The demo's central screen. Two of these columns had no data source in
    the product at all before this task."""
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    assert view.empty_columns == ()


def test_no_claim_is_silently_dropped(hero):
    """Every claim on the finding reaches a cell.

    A six-column table that quietly omits a seventh family shows a reader
    something complete-looking that is not complete, which is worse than
    showing nothing.
    """
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    shown = {cell.artifact for column in view.columns for cell in column.cells
             if not cell.corroborating and cell.artifact}
    assert {c.artifact for c in finding.claims} == shown


def test_a_family_with_no_column_is_an_error_not_a_silent_drop(hero):
    """FAILURE INJECTION."""
    finding, ingestion = hero
    alien = dataclasses.replace(finding.claims[0], family="mainframe")
    mutated = dataclasses.replace(
        finding, claims=(alien,) + tuple(finding.claims[1:]))

    with pytest.raises(UnrepresentedEvidence) as caught:
        build_evidence_view(mutated, records=ingestion.records)
    assert "mainframe" in str(caught.value)


# --- the highlight --------------------------------------------------------------


def test_the_five_hundred_thousand_cell_highlights_its_value(hero):
    """REGRESSION GUARD, and the reason this task found a real defect.

    The spreadsheet candidate carried the rule NAME as its quote --
    "high_value_requires_manager". It verified, because the name really is
    in that row, and it showed a reader the one cell in the product that
    most needs to show a number with no number in it. Trying to highlight
    the value is what noticed.
    """
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    cells = [c for c in view.column("spreadsheet/workflow").cells
             if c.artifact.endswith(".xlsx")]
    assert cells, "the 500,000 side of the hero conflict is missing"
    cell = cells[0]
    assert cell.value == 500000
    assert cell.highlight.found, cell.highlight.absent_because
    assert cell.quote[cell.highlight.start:cell.highlight.end] == "500000"


def test_a_value_written_with_separators_is_still_found():
    """`1000000`, `1,000,000` and `1_000_000` are one number to everyone but
    a substring search -- and the underscore form is how the implementation
    writes it, so missing it would leave the column a reviewer trusts most
    as the only one with no highlight."""
    for quote in ("threshold = 1_000_000", "JPY 1,000,000 or more", "value: 1000000"):
        assert locate_value(quote, 1000000).found, quote


def test_a_missing_highlight_is_explained_not_faked(hero):
    """A cell that silently highlights nothing teaches a reader the
    highlight means nothing. Where the artifact states the rule without
    stating the number, saying so IS the information: it distinguishes a
    document that fixes the value from one that defers to wherever the
    value lives."""
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    silent = [c for column in view.columns for c in column.cells
              if c.quote and not c.highlight.found and c.value is not None]
    assert silent, "expected at least one qualitative claim in the seed"
    for cell in silent:
        assert cell.highlight.absent_because


# --- empty cells say why ---------------------------------------------------------


def test_an_empty_column_always_carries_a_reason(hero):
    """The rule that makes this a module rather than a template.

    A blank under `test` could mean "no test asserts this", "no test file
    was read", or "a test asserts it and the values did not match". One of
    those is a finding, one is a bug in this product, and rendered as
    whitespace they are the same pixel.
    """
    finding, _ = hero
    view = build_evidence_view(finding, records=None)
    for name in ("test", "history"):
        column = view.column(name)
        assert column.is_empty
        assert "reader" in column.absent_because, (
            "with no records supplied the view must blame the reader, not "
            "claim the enterprise has no tests")


def test_a_finding_with_no_test_says_the_behaviour_runs_unguarded(ingested):
    """The absence worth reporting, and a distinction the seed makes on
    purpose.

    The suite DOES contain `test_capital_expenditure_goes_to_finance`. It
    asserts 6,000,000 -- a value above the 5,000,000 threshold, not the
    threshold itself -- so it will still pass if the threshold moves to
    4,000,000. Matching on value rather than on rule name is what lets the
    view say "nothing will go red when this changes" about a rule that
    visibly has a test.
    """
    ingestion, report = ingested
    findings = report.by_semantic_id("capex_finance_threshold")
    assert findings
    view = build_evidence_view(findings[0], records=ingestion.records)
    column = view.column("test")
    assert column.is_empty
    assert "unguarded" in column.absent_because


def test_an_undocumented_finding_explains_its_own_empty_doc_column(ingested):
    """The view and the detector agreeing, in the reader's words.

    `strategic_customer_exemption` is UNDOCUMENTED, and the cell where a
    document would go says so rather than sitting blank. That coherence is
    the difference between a table and an explanation.
    """
    ingestion, report = ingested
    undocumented = [f for f in report.findings
                    if f.kind == "UNDOCUMENTED"
                    and f.semantic_id == "strategic_customer_exemption"]
    assert undocumented
    view = build_evidence_view(undocumented[0], records=ingestion.records)
    column = view.column("policy doc")
    assert column.is_empty
    assert "undocumented" in column.absent_because


# --- corroboration is not a vote --------------------------------------------------


def test_test_and_history_cells_are_marked_corroborating(hero):
    """I15 at the view boundary. A test is evidence ABOUT intent, not a
    statement of policy -- nothing downstream may read these as claims."""
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    for name in ("test", "history"):
        assert all(cell.corroborating for cell in view.column(name).cells)
    for name in ("implementation", "policy doc", "spreadsheet/workflow"):
        assert not any(cell.corroborating for cell in view.column(name).cells)


def test_reading_test_files_created_no_candidates(ingested):
    """FAILURE INJECTION, and the constraint that shaped the adapter.

    If a test could document a rule, UN-003 -- behaviour a green test
    asserts and no document describes -- would stop being a finding at the
    moment the test column was filled in. Twenty-one assertions are read and
    not one becomes a candidate.
    """
    ingestion, _ = ingested
    assert any(r.kind == "test_assertion" for r in ingestion.records)
    assert not [c for c in ingestion.candidates if c.family == "tests"]


def test_the_undocumented_findings_survived_the_test_column(ingested):
    """The same guarantee, measured end to end rather than by construction."""
    _, report = ingested
    assert len([f for f in report.findings if f.kind == "UNDOCUMENTED"]) == 3


# --- the history column ------------------------------------------------------------


def test_the_history_column_carries_the_unsynchronised_change(hero):
    """The single most persuasive cell in the demo: a recorded decision to
    change one representation and leave the others to somebody else."""
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    notes = " ".join(cell.note for cell in view.column("history").cells)
    assert "unsynchronised" in notes


def test_the_business_claim_cell_is_not_citable(hero):
    """It is the question, not evidence. Giving it an artifact would make a
    statement the product made about itself quotable as though an artifact
    had said it."""
    finding, ingestion = hero
    view = build_evidence_view(finding, records=ingestion.records)
    cell = view.column("business claim").cells[0]
    assert cell.artifact == "" and cell.locator == ""
    assert "500000" in cell.quote and "1000000" in cell.quote


# --- rendering ---------------------------------------------------------------------


def test_the_text_rendering_is_deterministic(hero):
    """A table whose only renderer is a web page can only be reviewed by
    looking at it."""
    finding, ingestion = hero
    first = render(build_evidence_view(finding, records=ingestion.records))
    second = render(build_evidence_view(finding, records=ingestion.records))
    assert first == second
    assert all(name.upper() in first for name in COLUMNS)


def test_a_test_file_is_still_read_as_ordinary_python_too(ingested):
    """REGRESSION GUARD for a silent coverage loss.

    Routing test files to the assertion reader ALONE cost 105 records: a
    test file is ordinary Python as well, and dropping the generic reader
    removed its CALLS, CONTAINS and IMPLEMENTS edges from the enterprise
    graph. Evidence coverage fell from 377 refs to 272, every suite stayed
    green, and nothing anywhere said so -- which is how a coverage loss
    normally travels.
    """
    ingestion, _ = ingested
    from_test_file = [r for r in ingestion.records
                      if r.provenance and r.provenance[0].file
                      and "test_approval_rules" in r.provenance[0].file]
    kinds = {r.kind for r in from_test_file}
    assert "test_assertion" in kinds, "the assertion reader stopped running"
    assert kinds & {"entity", "relation"}, (
        "the generic Python reader stopped running on test files; the graph "
        "has silently lost this artifact")
