# -*- coding: utf-8 -*-
"""Discovery, measured against the seed's ground truth.

The seed states, in `seed_manifest.yaml`, exactly which findings a correct
system produces. These tests read that file rather than restating it, so a
change to the seed cannot silently stop being checked.

The failure-injection tests at the bottom are the ones that matter most.
Every detector here would look healthy on a system that quietly picked a
winner for each conflict -- the finding counts would be identical. Only an
attempt to close a conflict without a human decision record tells the two
apart (I15).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.discovery import (
    CONFLICT_TYPES,
    PENDING_HUMAN,
    Claim,
    Finding,
    HumanDecision,
    UnresolvedWithoutHuman,
    classify_conflict,
    discover,
    resolve_with_human,
)
from core.change_intelligence.evidence import EvidenceRef, verify
from core.change_intelligence.identity import resolve
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.states import AlignmentState
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def world():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(InMemoryBaseline.from_artifacts(built["artifacts"]),
                              "SESSION-P4")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()])
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    return overlay, model, discover(model, history=ingestion.records)


# --- the three seeded conflicts --------------------------------------


def test_every_seeded_conflict_is_found(world):
    """The seed declares three. Finding two is finding a demo."""
    _, _, report = world
    found = {f.semantic_id for f in report.conflicts}
    expected = {c["semantic_id"] for c in MANIFEST["conflicts"].values()}
    assert expected <= found, f"missed: {sorted(expected - found)}"


def test_the_hero_conflict_carries_every_representation_not_just_two(world):
    """A conflict shown with two sides when four exist understates the
    problem, and the fourth artifact is the one the reader works in."""
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]
    assert hero.kind == "CONFLICT"
    assert hero.conflict_type == "value_mismatch"
    assert hero.values == {1_000_000, 500_000}
    assert len(hero.families) >= 4, sorted(hero.families)
    assert len(hero.claims) >= 8


def test_the_hero_conflict_is_not_called_drift(world):
    """One dated side does not order two sides.

    Calling this DRIFT would assert the spreadsheet is authoritative because
    it happens to carry a date -- and the seed's whole point is that the
    2022 spreadsheet edit may be the unapproved one.
    """
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]
    assert hero.alignment is AlignmentState.CONFLICT
    assert hero.kind != "DRIFT"


def test_the_three_conflict_kinds_are_distinguished(world):
    """Change planning repairs them three different ways, so an unclassified conflict
    cannot be acted on."""
    _, _, report = world
    kinds = {f.semantic_id: f.conflict_type for f in report.conflicts}
    assert kinds["approval_threshold"] == "value_mismatch"
    assert kinds["capex_finance_threshold"] == "scope_mismatch"
    assert kinds["emergency_purchase_ceiling"] == "existence_mismatch"
    assert set(kinds.values()) <= set(CONFLICT_TYPES)


def test_agreeing_on_every_number_is_not_agreeing(world):
    """capex: the value matches in four artifacts and the code applies it
    only to `category == "capex"`. Value comparison alone reports ALIGNED,
    and a five-million non-capex request then goes to a line manager in one
    artifact and to Finance in another."""
    _, _, report = world
    scoped = [f for f in report.conflicts if f.conflict_type == "scope_mismatch"]
    assert scoped, "a scope mismatch that no number reveals must still be found"
    assert len(scoped[0].values) == 1, "the values agree; that is the point"
    assert "capex" in scoped[0].why


def test_the_impact_line_is_derived_from_the_data(world):
    """The line that turns a technical finding into a business problem.

    It names the two values and the two artifacts, all of which come from
    the claims -- so it can be checked. A sentence a model composed here
    could not be.
    """
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]
    assert "500,000" in hero.impact and "1,000,000" in hero.impact
    cited = {c.artifact for c in hero.claims}
    assert any(artifact in hero.impact for artifact in cited)


# --- undocumented, in both directions --------------------------------


def test_the_seeded_undocumented_behaviours_are_found(world):
    _, _, report = world
    found = {f.semantic_id for f in report.of_kind("UNDOCUMENTED")}
    for key in ("UN-001", "UN-002", "UN-003"):
        entry = MANIFEST["undocumented"].get(key)
        if entry is None:
            continue
        semantic_id = entry["semantic_id"]
        if semantic_id == "capex_condition_scope":
            semantic_id = "capex_finance_threshold"  # the rule the scope narrows
        assert semantic_id in found, f"{key} ({semantic_id}) not reported"


def test_a_rule_documented_in_prose_is_not_reported_undocumented(world):
    """BR-005 names no amount anywhere and is stated completely in two
    documents. Extracting only numbers made it look undocumented -- the
    most expensive kind of false positive, because a reader who checks one
    and finds it wrong stops checking the rest.
    """
    _, _, report = world
    undocumented = {f.semantic_id for f in report.of_kind("UNDOCUMENTED")}
    assert "high_risk_supplier_escalation" not in undocumented


def test_a_guard_on_the_rules_own_field_is_not_an_extra_restriction(world):
    """`supplier_risk == "high"` in code is BR-005's own condition, not a
    narrowing of it. Reporting it would mean every correctly implemented
    conditional rule is also an undocumented one."""
    _, _, report = world
    scope_findings = [f for f in report.of_kind("UNDOCUMENTED")
                      if f.finding_id.endswith("-scope")]
    assert all(f.semantic_id != "high_risk_supplier_escalation"
               for f in scope_findings)


def test_an_undocumented_finding_carries_its_origin(world):
    """"This is undocumented" invites "so what?". "Introduced in 2020 as a
    temporary measure, never reverted" does not."""
    _, _, report = world
    exemption = [f for f in report.of_kind("UNDOCUMENTED")
                 if f.semantic_id == "strategic_customer_exemption"][0]
    assert "2020" in exemption.impact


def test_every_finding_cites_evidence_that_verifies(world):
    """I4 at the boundary where findings become a screen.

    A finding is the first thing a person reads, and the first place a
    fabricated quote would be believed.
    """
    overlay, _, report = world
    checked = 0
    for finding in report.findings:
        for claim in finding.claims:
            # The workbook reader is supplied deliberately. The 500,000 side
            # of the hero conflict is a spreadsheet cell, so a grounding
            # check with no reader would skip the single most important
            # claim in the product and still report a perfect score.
            failure = verify(claim.evidence, overlay.head,
                             binary_reader=read_xlsx)
            assert failure is None, f"{finding.finding_id}: {failure}"
            checked += 1
    assert checked > 20, "a grounding check over almost nothing proves nothing"


# --- failure injection -- the conflict must not close ----------------


def test_no_finding_arrives_already_resolved(world):
    _, _, report = world
    assert all(f.resolution_status == PENDING_HUMAN for f in report.conflicts)


def test_no_finding_names_a_winning_side(world):
    """I15. There is no field to put a winner in, which is stronger than
    having one and leaving it empty."""
    _, _, report = world
    for finding in report.conflicts:
        assert not hasattr(finding, "resolved_value")
        assert not hasattr(finding, "winner")
        assert len(finding.values) >= 1


def test_the_module_offers_no_way_to_pick_a_winner():
    """FAILURE INJECTION.

    A newer spreadsheet is not a more correct spreadsheet: it may be the
    record of somebody changing a threshold without approval, which is the
    scenario the product exists to catch. Any of these helpers would convert
    that finding into a fact, and each is the kind of convenience that
    arrives in a hurry the week before a demo.
    """
    from core.change_intelligence import discovery
    for forbidden in ("auto_resolve", "pick_newest", "prefer_source",
                      "resolve_automatically", "choose_winner", "most_recent"):
        assert not hasattr(discovery, forbidden), (
            f"discovery.{forbidden} exists: a conflict that a script can "
            f"close is not a conflict (I15)")


def test_resolving_without_a_decision_record_is_impossible(world):
    """FAILURE INJECTION: force the system to close a conflict."""
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]

    for missing in ("actor", "timestamp", "session_id"):
        fields = dict(actor="kenji", timestamp="2026-09-19T10:00:00Z",
                      finding_id=hero.finding_id, answer="yes",
                      reason="approved at the change board", session_id="S-1")
        fields[missing] = ""
        with pytest.raises(UnresolvedWithoutHuman):
            HumanDecision(**fields)


def test_a_decision_for_a_different_finding_is_refused(world):
    """Audit records get copied. One that closes the wrong finding is worse
    than none, because it looks like due process."""
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]
    decision = HumanDecision(
        actor="kenji", timestamp="2026-09-19T10:00:00Z",
        finding_id="CONF-something-else", answer="yes",
        reason="approved", session_id="S-1")
    with pytest.raises(UnresolvedWithoutHuman):
        resolve_with_human(hero, decision)


def test_a_human_decision_closes_the_finding_and_names_who(world):
    """The gate must let a real decision through, or it is a wall."""
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]
    decision = HumanDecision(
        actor="kenji", timestamp="2026-09-19T10:00:00Z",
        finding_id=hero.finding_id, answer="yes",
        reason="the spreadsheet edit was never approved", session_id="S-1")
    closed = resolve_with_human(hero, decision)
    assert closed.resolution_status == "RESOLVED_BY:kenji"
    assert closed.values == hero.values, "closing a finding must not drop evidence"


def test_unclear_opens_a_gap_rather_than_closing_anything(world):
    """I5. "Unclear" is an answer, and it is not "skip"."""
    _, _, report = world
    hero = report.by_semantic_id("approval_threshold")[0]
    decision = HumanDecision(
        actor="kenji", timestamp="2026-09-19T10:00:00Z",
        finding_id=hero.finding_id, answer="unclear",
        reason="need to ask the 2022 approver", session_id="S-1")
    result = resolve_with_human(hero, decision)
    assert result.resolution_status == PENDING_HUMAN
    assert result.gaps and result.gaps[-1].reason


# --- the model's own guards ---------------------------------------------------


def test_a_conflict_with_one_side_is_refused():
    claim = Claim(artifact="a.yaml", family="workflow", locator="L1", value=1,
                  evidence=EvidenceRef("a.yaml", "L1", "1", "snap"))
    with pytest.raises(ValueError, match="not a conflict"):
        Finding(finding_id="X", kind="CONFLICT", semantic_id="s",
                claims=(claim,), why="", conflict_type="value_mismatch")


def test_an_unclassified_conflict_is_refused():
    claims = tuple(
        Claim(artifact=f"{n}.yaml", family="workflow", locator="L1", value=n,
              evidence=EvidenceRef(f"{n}.yaml", "L1", str(n), "snap"))
        for n in (1, 2))
    with pytest.raises(ValueError, match="conflict_type"):
        Finding(finding_id="X", kind="CONFLICT", semantic_id="s",
                claims=claims, why="")


def test_a_finding_that_cites_nothing_is_refused():
    with pytest.raises(ValueError, match="cites nothing"):
        Finding(finding_id="X", kind="UNDOCUMENTED", semantic_id="s",
                claims=(), why="")


def test_classify_conflict_distinguishes_silence_from_disagreement():
    def claim(artifact, value):
        return Claim(artifact=artifact, family="workflow", locator="L1",
                     value=value,
                     evidence=EvidenceRef(artifact, "L1", "q", "snap"))

    assert classify_conflict((claim("a", 1), claim("b", 2))) == "value_mismatch"
    assert classify_conflict((claim("a", 1), claim("b", None))) == "existence_mismatch"
    assert classify_conflict((claim("a", 1), claim("b", 1))) == "scope_mismatch"
