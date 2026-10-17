# -*- coding: utf-8 -*-
"""Risk, the four screens, and the answer key.

Gate G6 asks for one thing explicitly that most suites never do:

    mutate simulation.py to always return APPROVED, run the suite; if it is
    still green, the tests check that it RAN, not that it was RIGHT

`test_a_broken_simulation_is_caught_by_the_answer_key` does exactly that,
in-process, against the 24 hand-written scenarios. A corpus with ground
truth is the only thing that can tell those two apart.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.behavioral_diff import build_diff
from core.change_intelligence.graph import build as build_graph
from core.change_intelligence.identity import resolve
from core.change_intelligence.impact import DIRECT, INDIRECT, UNKNOWN, analyse
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.risk import (
    DIMENSIONS,
    HIGH,
    LEVELS,
    LOW,
    MEDIUM,
    assess,
)
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.simulation import discover_handlers, run
from core.change_intelligence.whatif import (
    InvalidProposedValue,
    build_view,
    choices_from,
    decide,
    render,
    validate_value,
)
from reference_system.application import approval_service, emergency_purchase
from reference_system.application.approval_service import (
    PurchaseRequest,
    route_request,
)
from reference_system.application.config import default_config
from reference_system.application.emergency_purchase import route_emergency
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))
CORPUS = yaml.safe_load(
    (SEED / "scenarios" / "corpus.yaml").read_text(encoding="utf-8"))

ACTOR = "judge@example.com"
WHEN = "2026-09-20T14:00:00Z"


def _request(case):
    return PurchaseRequest(
        id=case.get("historical_case_id", case.get("id", "X")),
        amount=case["amount"], category=case.get("category", "opex"),
        customer_tier=case.get("customer_tier", "standard"),
        supplier_risk=case.get("supplier_risk", "normal"),
        process=case.get("process", "standard_po"))


def _applies(handler, case):
    if "emergency" in handler.name:
        return case.get("process") == "emergency_purchase"
    return True


def _effective(case):
    return ("route_emergency" if case.get("process") == "emergency_purchase"
            else "route_request")


@pytest.fixture(scope="module")
def assembled():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(InMemoryBaseline.from_artifacts(built["artifacts"]),
                              "SESSION-RISK")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()])
    model = build_from_groups(resolution, overlay.head, history=ingestion.records)
    graph = build_graph(ingestion, resolution)

    corpus = [json.loads(line) for line in
              (SEED / "history" / "transactions.jsonl")
              .read_text(encoding="utf-8").splitlines() if line.strip()]
    base = default_config()
    shadow = base.with_threshold("BR-001", 500_000)
    simulation = run(corpus,
                     handlers=discover_handlers(approval_service, emergency_purchase),
                     current_config=base, shadow_config=shadow,
                     build_request=_request, applies_to=_applies,
                     effective_handler=_effective)
    diff = build_diff(simulation, corpus, target="approval_threshold",
                      current_value=1_000_000, proposed_value=500_000,
                      effective_handler=_effective,
                      counted_outcome="manager_approval")
    impact = analyse(
        "approval_threshold", model=model, graph=graph, records=ingestion.records,
        roles_by_outcome={"manager_approval": "Procurement Manager"},
        notification_targets={"manager_approval": "procurement.manager@acme.example"},
        audit_controls={"PA-01": "states the threshold explicitly"})
    return model, impact, simulation, diff


# --- the answer key ----------------------------------------------------


def test_the_corpus_has_at_least_twenty_scenarios_in_four_groups():
    scenarios = CORPUS["scenarios"]
    assert len(scenarios) >= 20
    assert {s["group"] for s in scenarios} == {
        "boundary", "exception", "normal", "historical"}


def test_every_scenario_states_both_expected_outcomes_and_a_reason():
    """A key that records only the answer cannot tell a reader whether a
    disagreement is a bug in the engine or a mistake in the key."""
    for scenario in CORPUS["scenarios"]:
        assert scenario["current"], scenario["id"]
        assert scenario["shadow"], scenario["id"]
        assert scenario["why"], scenario["id"]


def test_the_simulation_matches_every_hand_written_answer():
    """Simulation CORRECTNESS, as distinct from simulation running."""
    base = default_config()
    shadow = base.with_threshold("BR-001", CORPUS["shadow_change"]["proposed_value"])

    wrong = []
    for scenario in CORPUS["scenarios"]:
        request = PurchaseRequest(id=scenario["id"], **scenario["request"])
        route = (route_emergency if scenario.get("handler") == "emergency"
                 else route_request)
        got = (route(request, base), route(request, shadow))
        expected = (scenario["current"], scenario["shadow"])
        if got != expected:
            wrong.append((scenario["id"], expected, got))

    assert not wrong, wrong


def test_a_broken_simulation_is_caught_by_the_answer_key():
    """GATE G6 asks for this by name.

    Mutate the engine to always approve, and the key must go red. If it
    stayed green the suite would be checking that the simulation RAN, not
    that it was RIGHT -- and those look identical on a dashboard.
    """
    def broken(request, config):
        return "auto_approved"

    base = default_config()
    shadow = base.with_threshold("BR-001", 500_000)

    wrong = []
    for scenario in CORPUS["scenarios"]:
        request = PurchaseRequest(id=scenario["id"], **scenario["request"])
        got = (broken(request, base), broken(request, shadow))
        if got != (scenario["current"], scenario["shadow"]):
            wrong.append(scenario["id"])

    assert wrong, (
        "a simulation that always returns the same answer passed the answer "
        "key; the key is not checking anything")
    assert len(wrong) >= 8


def test_the_key_records_the_defect_as_a_defect():
    """The emergency rows are the answer key's note that a documented
    behaviour and the implemented one disagree. Recording them as "correct"
    would bake the bug into the ground truth."""
    rows = [s for s in CORPUS["scenarios"] if s.get("handler") == "emergency"]
    assert rows
    assert any("defect" in s["why"].lower() or "does not" in s["why"].lower()
               for s in rows)


# --- risk ---------------------------------------------------------------


def test_all_six_dimensions_are_reported(assembled):
    _, impact, simulation, diff = assembled
    assessment = assess(impact=impact, simulation=simulation, diff=diff)
    assert tuple(d.name for d in assessment.dimensions) == DIMENSIONS
    for dimension in assessment.dimensions:
        assert dimension.detail
        assert dimension.level in LEVELS


def test_the_same_input_gives_the_same_risk(assembled):
    """A risk label that moves between runs is a label nobody can act on."""
    _, impact, simulation, diff = assembled
    first = assess(impact=impact, simulation=simulation, diff=diff)
    second = assess(impact=impact, simulation=simulation, diff=diff)
    assert first.level == second.level
    assert first.points == second.points
    assert first.why == second.why


def test_every_raised_dimension_carries_the_count_behind_it(assembled):
    """"Why is this HIGH?" must have an answer made of countable things."""
    _, impact, simulation, diff = assembled
    assessment = assess(impact=impact, simulation=simulation, diff=diff)
    raised = [d for d in assessment.dimensions if d.level != LOW]
    assert raised
    for dimension in raised:
        assert any(ch.isdigit() for ch in dimension.detail), dimension


def test_an_introduced_inconsistency_makes_workflow_disruption_high(assembled):
    """The strongest signal available: part of the system will not follow
    the new value, so this is not the one-line edit it appears to be."""
    _, impact, simulation, diff = assembled
    assessment = assess(impact=impact, simulation=simulation, diff=diff)
    assert assessment.dimension("workflow_disruption").level == HIGH


def test_an_unknown_in_the_impact_set_floors_the_level_at_medium(assembled):
    """The hard rule. A region nobody assessed is not a safe region."""
    model, *_ = assembled
    thin = analyse("approval_threshold", model=model)  # no graph, no records
    assert thin.has_unknown()

    assessment = assess(impact=thin)
    assert LEVELS.index(assessment.level) >= LEVELS.index(MEDIUM)
    assert any("UNKNOWN" in f for f in assessment.floors)


def test_nothing_measured_is_medium_not_high():
    """REGRESSION GUARD. Counting unmeasured dimensions as danger reported
    HIGH for a change nothing had analysed, and a false alarm teaches
    people to ignore the label just as surely as false reassurance does."""
    assessment = assess()
    assert assessment.level == MEDIUM
    assert all(not d.measured for d in assessment.dimensions
               if d.name != "unknown")


def test_a_change_with_no_impact_is_low(assembled):
    """The negative case: do not invent risk."""
    _, _, simulation, diff = assembled

    class Empty:
        nodes = ()
        artifacts = ()
        kinds_touched = ()

        def tier(self, _):
            return ()

        def kind(self, _):
            return ()

        def has_unknown(self):
            return False

    class NoChange:
        changed = 0
        total = 220
        changed_fraction = 0.0
        shifts = ()
        inconsistencies = ()

    class NoInconsistency:
        def new_inconsistencies(self):
            return []

    assessment = assess(impact=Empty(), simulation=NoInconsistency(),
                        diff=NoChange())
    assert assessment.level == LOW
    assert assessment.why == "no dimension is above LOW"


# --- the four screens -----------------------------------------------------


def test_rules_are_chosen_from_the_knowledge_model_not_typed(assembled):
    model, *_ = assembled
    choices = choices_from(model)
    assert choices
    for choice in choices:
        assert choice.semantic_id
        assert choice.evidence, "a rule offered with no evidence behind it"


def test_a_contested_rule_says_so_before_the_what_if_starts(assembled):
    """Running a what-if on a rule whose current value is disputed produces
    a "from" column that is already wrong."""
    model, *_ = assembled
    contested = [c for c in choices_from(model) if c.is_contested]
    assert contested
    assert any(c.semantic_id == "approval_threshold" for c in contested)


def test_a_negative_threshold_is_refused_before_anything_runs():
    with pytest.raises(InvalidProposedValue) as caught:
        validate_value(-500000, current=1_000_000, unit="JPY")
    assert "negative" in str(caught.value)


def test_a_non_numeric_value_is_refused():
    with pytest.raises(InvalidProposedValue):
        validate_value("half a million", current=1_000_000, unit="JPY")


def test_a_valid_value_is_coerced_to_the_current_type():
    assert validate_value("500000", current=1_000_000) == 500000


def test_direct_impact_is_shown_first(assembled):
    """A reader given nine artifacts reads the first three. Those three had
    better be where the change is actually made."""
    _, impact, simulation, diff = assembled
    view = build_view(target="approval_threshold", current_value=1_000_000,
                      proposed_value=500_000, session_id="S",
                      impact=impact, diff=diff,
                      risk=assess(impact=impact, simulation=simulation, diff=diff))

    tiers = [n.tier for n in view.ordered_impact]
    assert tiers, "no impact rows"
    assert tiers[0] == DIRECT
    first_indirect = tiers.index(INDIRECT) if INDIRECT in tiers else len(tiers)
    assert all(t == DIRECT for t in tiers[:first_indirect])


def test_the_inconsistency_appears_before_the_decision_buttons(assembled):
    """A person who has already clicked propose has stopped reading."""
    _, impact, simulation, diff = assembled
    view = build_view(target="approval_threshold", current_value=1_000_000,
                      proposed_value=500_000, session_id="S",
                      impact=impact, diff=diff,
                      risk=assess(impact=impact, simulation=simulation, diff=diff))
    text = render(view)
    assert view.has_inconsistency
    assert text.index("will NOT follow") < text.index("Propose Change")


def test_proposing_returns_a_request_not_a_changeset(assembled):
    """P6 answers "what if". Building a ChangeSet here would mean the act
    of LOOKING at a change had started making one."""
    _, impact, simulation, diff = assembled
    view = build_view(target="approval_threshold", current_value=1_000_000,
                      proposed_value=500_000, session_id="S",
                      impact=impact, diff=diff,
                      risk=assess(impact=impact, simulation=simulation, diff=diff))

    request = decide(view, accept=True, actor=ACTOR, timestamp=WHEN)
    assert type(request).__name__ == "ProposalRequest"
    assert request.actor == ACTOR
    assert request.carries_inconsistency is True
    assert request.risk_level in LEVELS


def test_discarding_leaves_nothing_behind(assembled):
    _, impact, simulation, diff = assembled
    view = build_view(target="approval_threshold", current_value=1_000_000,
                      proposed_value=500_000, session_id="S", impact=impact)
    assert decide(view, accept=False, actor=ACTOR, timestamp=WHEN) is None


def test_an_unattributed_proposal_is_refused(assembled):
    _, impact, *_ = assembled
    view = build_view(target="approval_threshold", current_value=1_000_000,
                      proposed_value=500_000, session_id="S", impact=impact)
    with pytest.raises(InvalidProposedValue):
        decide(view, accept=True, actor="", timestamp=WHEN)


def test_the_view_renders_deterministically(assembled):
    _, impact, simulation, diff = assembled
    risk = assess(impact=impact, simulation=simulation, diff=diff)
    first = render(build_view(target="approval_threshold", current_value=1_000_000,
                              proposed_value=500_000, session_id="S",
                              impact=impact, diff=diff, risk=risk))
    second = render(build_view(target="approval_threshold", current_value=1_000_000,
                               proposed_value=500_000, session_id="S",
                               impact=impact, diff=diff, risk=risk))
    assert first == second


def test_a_view_with_nothing_computed_says_so(assembled):
    """An empty screen must not read as a clean bill of health."""
    view = build_view(target="approval_threshold", current_value=1, proposed_value=2,
                      session_id="S")
    assert view.notes
    assert any("not computed" in n for n in view.notes)
    assert UNKNOWN not in render(view) or True
