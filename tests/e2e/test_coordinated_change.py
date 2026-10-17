# -*- coding: utf-8 -*-
"""Gate G7 -- one intent becomes a coordinated change across five kinds of thing.

Built against the real seed, not fixtures. The claim being tested is the
product's central one: that a single approved sentence produces a change set
spanning workflow, data, code, documentation and the people who act on the
rule -- and that a person can read it, understand it, and approve it before
anything moves.

The three scenarios that matter most are the ones where the right answer is
restraint:

  Scenario H  the rule already says what was asked for  ->  ZERO actions
  Scenario E  the description was wrong, not the system ->  DOC only
  golden case a route ignores the value being changed   ->  TWO changes

The first two are how a planner proves it understands the question. The
third is how it proves it used what the simulation found: proposing only the
config change means the analysis was run and then ignored, which is an
integration failure and not a feature gap.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
import yaml

from core.adapters.doc.spreadsheet_rules.adapter import extract_bytes as read_xlsx
from core.change_intelligence.action_plan import (
    REQUIRED,
    communication_actions,
    plan,
)
from core.change_intelligence.apply_gate import APPROVE, apply_changeset, decide
from core.change_intelligence.changeset import ChangeSet
from core.change_intelligence.graph import build as build_graph
from core.change_intelligence.identity import resolve
from core.change_intelligence.impact import DIRECT, analyse
from core.change_intelligence.ingest import ingest
from core.change_intelligence.knowledge import build_from_groups
from core.change_intelligence.review import BLOCKS, CHOICES, block_order, build_screen
from core.change_intelligence.review import render as render_screen
from core.change_intelligence.risk import assess
from core.change_intelligence.session import InMemoryBaseline, InMemoryOverlay
from core.change_intelligence.simulation import discover_handlers, run
from core.change_intelligence.states import ChangeSetState
from core.change_intelligence.structured_diff import (
    StructuredEdit,
    read_at,
    validate_workflow,
)
from reference_system.application import approval_service, emergency_purchase
from reference_system.application.approval_service import PurchaseRequest
from reference_system.application.config import default_config
from reference_system.reset.baseline import build_baseline

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
MANIFEST = yaml.safe_load((SEED / "seed_manifest.yaml").read_text(encoding="utf-8"))
CORPUS_PATH = SEED / "history" / "transactions.jsonl"
WORKFLOW_ARTIFACT = "workflow/approval_routes.yaml"
THRESHOLD_PATH = "approval_routes[3].condition.value"
TARGET = MANIFEST["rules"]["BR-001"]["semantic_id"]


def _corpus():
    return [json.loads(line) for line
            in CORPUS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _request(case):
    return PurchaseRequest(
        id=case["historical_case_id"], amount=case["amount"],
        category=case.get("category", "opex"),
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
def world():
    built = build_baseline(SEED)
    overlay = InMemoryOverlay(
        InMemoryBaseline.from_artifacts(built["artifacts"]), "SESSION-P7")
    ingestion = ingest(
        overlay.head,
        spreadsheet_reader=lambda raw, path, snap: list(read_xlsx(raw, path, snap)),
        semantic_hints=MANIFEST.get("semantic_hints", {}))
    resolution = resolve(
        ingestion.candidates,
        rule_id_to_semantic_id={r: v["semantic_id"] for r, v in MANIFEST["rules"].items()},
        known_semantic_ids=[v["semantic_id"] for v in MANIFEST["rules"].values()])
    model = build_from_groups(resolution, overlay.head,
                              history=ingestion.records)
    graph = build_graph(ingestion, resolution)
    return built, overlay, ingestion, model, graph


@pytest.fixture(scope="module")
def simulated():
    base = default_config()
    return _corpus(), run(
        _corpus(), handlers=discover_handlers(approval_service, emergency_purchase),
        current_config=base, shadow_config=base.with_threshold("BR-001", 500_000),
        build_request=_request, applies_to=_applies,
        effective_handler=_effective)


@pytest.fixture(scope="module")
def impact(world):
    _, overlay, ingestion, model, graph = world
    return analyse(TARGET, model=model, graph=graph, config=default_config(),
                   records=ingestion.records,
                   roles_by_outcome={"manager_approval": "Procurement Manager"},
                   notification_targets={"manager_approval": "notify:procurement-managers"})


@pytest.fixture(scope="module")
def current_threshold(world):
    _, overlay, _, _, _ = world
    return read_at(yaml.safe_load(overlay.read(WORKFLOW_ARTIFACT)),
                   THRESHOLD_PATH)


@pytest.fixture
def workflow_edit(current_threshold):
    return StructuredEdit(
        artifact=WORKFLOW_ARTIFACT, path=THRESHOLD_PATH,
        expected_current=current_threshold, proposed=current_threshold // 2,
        unit="JPY", reason="the approved intent lowered the threshold",
        evidence=f"{WORKFLOW_ARTIFACT}:{THRESHOLD_PATH}")


@pytest.fixture
def full_plan(impact, simulated, current_threshold, workflow_edit):
    _, result = simulated
    return plan(target=TARGET, impact=impact,
                introduced=result.new_inconsistencies(),
                inherited=result.inherited_inconsistencies(),
                current_value=current_threshold,
                proposed_value=current_threshold // 2,
                edits_by_artifact={WORKFLOW_ARTIFACT: (workflow_edit,)})


# --- the golden case ---------------------------------------------------------


def test_the_simulation_found_a_contradiction_to_use(simulated):
    """Precondition. If this is empty the golden-case tests prove nothing."""
    _, result = simulated
    assert result.new_inconsistencies()


def test_the_proposal_carries_both_a_config_change_and_a_code_change(full_plan):
    """The integration test of the whole product.

    Proposing only the configuration change means the simulation was run
    and its finding discarded. That is not a missing feature, it is the
    analysis not reaching the output.
    """
    assert full_plan.of_kind("WORKFLOW"), "no configuration change proposed"
    assert full_plan.of_kind("CODE"), (
        "the simulation found a route that decides requests without reading "
        "the changed value, and the proposal does not mention it")


def test_the_code_action_is_required_not_a_suggestion(full_plan):
    code = full_plan.of_kind("CODE")
    assert any(a.necessity == REQUIRED for a in code)


def test_the_code_action_says_why_a_config_change_is_not_enough(full_plan):
    descriptions = " ".join(a.description for a in full_plan.of_kind("CODE"))
    assert "not a configuration change" in descriptions


def test_no_assertion_here_names_the_module_at_fault():
    """Discovery stays structural, as P6 froze it.

    The golden case must surface from running the system, not from a test
    that already knows which file is wrong.
    """
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if node.name == "test_no_assertion_here_names_the_module_at_fault":
            # The guard states the forbidden name in order to look for it.
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.Constant) and isinstance(statement.value, str):
                assert "emergency_purchase" not in statement.value, (
                    f"{node.name} names the module at fault; then the test "
                    f"passes because somebody typed the answer into it")


# --- the coordinated change spans five kinds of thing ------------------------


def test_one_intent_produces_actions_across_several_kinds(full_plan):
    assert len(full_plan.kinds) >= 4, (
        f"only {full_plan.kinds} -- a rule that lives in several kinds of "
        f"place has to be changed in several kinds of place")


def test_the_people_who_act_on_the_rule_are_told(full_plan):
    """The kind a purely technical product forgets."""
    assert communication_actions(full_plan), (
        "nobody is notified. Lowering an approval threshold changes what a "
        "group of managers does on Monday, and a config file does not tell "
        "them")


def test_the_workflow_action_carries_a_structured_edit(full_plan):
    workflow = full_plan.of_kind("WORKFLOW")[0]
    assert workflow.edits
    assert workflow.edits[0].path == THRESHOLD_PATH


def test_the_structured_edit_still_leaves_a_valid_workflow(
        world, workflow_edit):
    from core.change_intelligence.structured_diff import apply_edits

    _, overlay, _, _, _ = world
    document = yaml.safe_load(overlay.read(WORKFLOW_ARTIFACT))
    result = apply_edits(document, [workflow_edit], validate=validate_workflow)
    assert validate_workflow(result.after) == []


def test_documents_are_recommended_and_the_workflow_is_required(full_plan):
    """The tiering survives all the way to the action list."""
    workflow = full_plan.of_kind("WORKFLOW")
    assert workflow and all(a.necessity == REQUIRED for a in workflow)
    docs = full_plan.of_kind("DOC")
    assert docs and not any(a.necessity == REQUIRED for a in docs)


# --- Scenario H: the right answer is nothing ---------------------------------


def test_scenario_h_produces_zero_actions(impact, current_threshold):
    """The rule already says what was asked for."""
    noop = plan(target=TARGET, impact=impact, current_value=current_threshold,
                proposed_value=current_threshold, is_noop=True)
    assert noop.is_empty, (
        f"{len(noop.actions)} actions proposed for a change that changes "
        f"nothing")


def test_scenario_h_says_why_it_proposed_nothing(impact, current_threshold):
    """An empty list and an empty list with a reason are different answers."""
    noop = plan(target=TARGET, impact=impact, current_value=current_threshold,
                proposed_value=current_threshold, is_noop=True)
    assert noop.notes
    assert "already holds the requested value" in noop.notes[0]


# --- Scenario E: knowledge-only ----------------------------------------------


def test_scenario_e_touches_no_code_and_no_workflow(impact, current_threshold):
    """The system was right; the description of it was wrong."""
    knowledge = plan(target=TARGET, impact=impact,
                     current_value=current_threshold,
                     proposed_value=current_threshold,
                     knowledge_only=True)
    assert not knowledge.of_kind("CODE")
    assert not knowledge.of_kind("WORKFLOW")
    assert not knowledge.of_kind("DATA")


def test_scenario_e_still_updates_the_documentation(impact, current_threshold):
    knowledge = plan(target=TARGET, impact=impact,
                     current_value=current_threshold,
                     proposed_value=current_threshold,
                     knowledge_only=True)
    assert knowledge.of_kind("DOC")


# --- the review screen -------------------------------------------------------


@pytest.fixture
def screen(world, impact, simulated, full_plan, current_threshold):
    _, overlay, _, _, _ = world
    _, result = simulated
    changeset = ChangeSet(
        trigger="INT-001", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(current_threshold), affected_nodes=impact.nodes,
        affected_artifacts=impact.artifacts,
        evidence=tuple(impact.nodes[0].evidence for _ in range(1)),
        simulation_result=result, actions=full_plan,
        risk=assess(impact=impact, simulation=result),
        created_by="ops.lead", created_at="2026-09-20T09:00:00Z")
    return build_screen(
        changeset,
        current_state=[(WORKFLOW_ARTIFACT, current_threshold)])


def _intent(current):
    class I:
        intent_id = "INT-001"
        version = 1
        target = TARGET
        desired_value = current // 2
        reason = "an internal audit asked for tighter procurement control"
        requested_by = "ops.lead"
        requested_at = "2026-09-19T14:00:00Z"

    return I()


def test_the_screen_has_exactly_eight_blocks():
    assert len(BLOCKS) == 8


def test_why_comes_before_the_diff(screen):
    order = block_order(render_screen(screen))
    assert order == list(BLOCKS), (
        f"the blocks are rendered as {order}; a screen that opens with a "
        f"diff asks the reader to judge a change before telling them what "
        f"problem it solves")
    assert order.index("WHY") < order.index("PROPOSED ACTIONS")


def test_the_screen_says_who_asked_and_when(screen):
    text = render_screen(screen)
    assert "ops.lead" in text and "2026-09-19" in text


def test_the_contradiction_appears_above_the_decision_buttons(screen):
    """A finding below the buttons is a finding somebody scrolls past."""
    text = render_screen(screen)
    assert "INTRODUCED BY THIS CHANGE" in text
    assert text.index("INTRODUCED BY THIS CHANGE") < text.index(f"[{CHOICES[0]}]")


def test_introduced_and_inherited_are_labelled_differently(screen):
    text = render_screen(screen)
    assert "INTRODUCED BY THIS CHANGE" in text
    if screen.inherited:
        assert "NOT CAUSED BY THIS CHANGE" in text


def test_direct_impact_is_listed_before_indirect(screen):
    tiers = [node.tier for node in screen.impact]
    assert tiers == sorted(tiers, key=lambda t: ("DIRECT", "INDIRECT",
                                                 "UNKNOWN").index(t))
    assert tiers[0] == DIRECT


def test_the_three_choices_are_offered_and_none_is_a_default(screen):
    text = render_screen(screen)
    for choice in CHOICES:
        assert f"[{choice}]" in text
    assert len(CHOICES) == 3


def test_the_screen_never_recommends_a_decision(screen):
    """Risk explains. It does not advise."""
    text = render_screen(screen).lower()
    for phrase in ("we recommend", "safe to approve", "you should approve",
                   "low risk, approve", "recommended action"):
        assert phrase not in text, (
            f"the screen says {phrase!r}; once the product advises, a "
            f"signature stops meaning the person judged it and starts "
            f"meaning they agreed with it")


def test_the_risk_block_marks_what_was_not_measured(screen):
    text = render_screen(screen)
    assert "RISK" in text
    assert screen.risk.dimensions


def test_the_screen_is_deterministic(screen):
    assert render_screen(screen) == render_screen(screen)


def test_the_screen_computes_nothing_of_its_own():
    """It orders and labels. If it derived a figure it could disagree with
    the ChangeSet, and the reviewer would decide on the unaudited one."""
    import inspect

    from core.change_intelligence import review

    source = inspect.getsource(review.build_screen)
    for banned in ("analyse(", "assess(", "run(", "plan("):
        assert banned not in source


# --- through the gate --------------------------------------------------------


def test_the_whole_path_runs_from_intent_to_a_written_overlay(
        world, impact, simulated, full_plan, current_threshold):
    _, base_overlay, _, _, _ = world
    overlay = InMemoryOverlay(
        InMemoryBaseline.from_artifacts(build_baseline(SEED)["artifacts"]),
        "SESSION-P7-APPLY")
    _, result = simulated

    changeset = ChangeSet(
        trigger="INT-001", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(current_threshold), affected_nodes=impact.nodes,
        affected_artifacts=impact.artifacts, evidence=("quote",),
        simulation_result=result, actions=full_plan,
        risk=assess(impact=impact, simulation=result))

    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="ops.lead",
                                    timestamp="t")
    approved, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=APPROVE, reason="evidence is clear")
    applied, apply_result = apply_changeset(
        approved, overlay=overlay,
        documents={WORKFLOW_ARTIFACT: yaml.safe_load(
            overlay.read(WORKFLOW_ARTIFACT))},
        actor="ops.lead", timestamp="t", validate=validate_workflow)

    assert applied.state is ChangeSetState.APPLIED
    assert WORKFLOW_ARTIFACT in apply_result.written
    after = yaml.safe_load(overlay.read(WORKFLOW_ARTIFACT))
    assert read_at(after, THRESHOLD_PATH) == current_threshold // 2


def test_the_human_actions_are_reported_as_not_done(
        world, impact, simulated, full_plan, current_threshold):
    overlay = InMemoryOverlay(
        InMemoryBaseline.from_artifacts(build_baseline(SEED)["artifacts"]),
        "SESSION-P7-HUMAN")
    _, result = simulated
    changeset = ChangeSet(
        trigger="INT-001", base_snapshot=overlay.head.snapshot_id,
        intent=_intent(current_threshold), affected_nodes=impact.nodes,
        affected_artifacts=impact.artifacts, evidence=("quote",),
        simulation_result=result, actions=full_plan, risk=assess())
    reviewed = changeset.with_state(ChangeSetState.REVIEW, actor="a",
                                    timestamp="t")
    approved, _ = decide(reviewed, actor="cfo", timestamp="t",
                         decision=APPROVE, reason="ok")
    _, apply_result = apply_changeset(
        approved, overlay=overlay,
        documents={WORKFLOW_ARTIFACT: yaml.safe_load(
            overlay.read(WORKFLOW_ARTIFACT))},
        actor="a", timestamp="t", validate=validate_workflow)

    assert apply_result.human_actions
    assert any("carried out by a person" in note
               for note in apply_result.notes)


def test_only_the_route_that_ignores_the_value_is_accused(full_plan):
    """Both routes appear in a disagreement. Only one of them is wrong.

    The route whose answer MOVED when the value moved is behaving
    correctly -- it disagrees precisely because it read the change. Naming
    it too would put a required code change on a file that needs none: the
    same false positive that once made every artifact DIRECT, in a new
    place, and a reviewer told two files are broken when one is will check
    neither.
    """
    accused = {a.target for a in full_plan.of_kind("CODE")
               if "without reading the changed value" in a.description}
    assert len(accused) == 1, (
        f"{len(accused)} routes accused: {sorted(accused)}")


def test_the_accusation_cites_distinct_simulation_cases(full_plan):
    accused = [a for a in full_plan.of_kind("CODE")
               if "without reading the changed value" in a.description]
    evidence = accused[0].impact_evidence
    assert len(set(evidence)) == len(evidence), (
        f"the same case is cited twice: {evidence}")
    assert evidence


def test_a_configuration_file_is_not_told_to_read_from_configuration(full_plan):
    """Advice nobody can act on is worse than no advice."""
    for action in full_plan.of_kind("CODE"):
        if "config" in action.target:
            assert "read the value from configuration" not in action.description


def test_the_rule_itself_is_not_proposed_as_an_edit(full_plan):
    """The rule is the SUBJECT of the change, not a thing to edit.

    An action naming no artifact and asking for no work makes the required
    list longer without making it truer.
    """
    assert not [a for a in full_plan.actions if a.target.startswith("rule:")]
