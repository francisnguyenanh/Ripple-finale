# -*- coding: utf-8 -*-
"""A proposal is a path and a value, and it is checkable.

The whole of this file is the difference between a diff you can review and
a diff you can only read. Each test asks a question of a proposed change
that a text diff cannot answer.
"""

from __future__ import annotations

import copy
import pathlib

import pytest
import yaml

from core.change_intelligence.structured_diff import (
    InvalidAfterEdit,
    StaleDiff,
    StructuredEdit,
    NoSuchLocation,
    apply_edits,
    cell_edit,
    dry_run,
    read_at,
    render,
    unreachable_routes,
    validate_workflow,
)

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
WORKFLOW_PATH = SEED / "workflow" / "approval_routes.yaml"

THRESHOLD_PATH = "approval_routes[3].condition.value"


@pytest.fixture
def workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def current_threshold(workflow):
    return read_at(workflow, THRESHOLD_PATH)


def _edit(current, proposed, path=THRESHOLD_PATH):
    return StructuredEdit(
        artifact="workflow/approval_routes.yaml", path=path,
        expected_current=current, proposed=proposed, unit="JPY",
        reason="the approval threshold was lowered by an approved intent",
        evidence="workflow/approval_routes.yaml:approval_routes[3]")


# --- the edit is structured --------------------------------------------------


def test_an_edit_names_a_path_not_two_lines_of_text(current_threshold):
    edit = _edit(current_threshold, current_threshold // 2)
    assert edit.path == THRESHOLD_PATH
    assert "\n" not in edit.rendered()
    assert "-" not in edit.rendered().split(":")[0]


def test_an_edit_with_no_path_is_refused(current_threshold):
    with pytest.raises(ValueError):
        StructuredEdit(artifact="w.yaml", path="", expected_current=1,
                       proposed=2, reason="r")


def test_an_edit_with_no_reason_is_refused(current_threshold):
    with pytest.raises(ValueError, match="no reason"):
        StructuredEdit(artifact="w.yaml", path="a.b", expected_current=1,
                       proposed=2, reason="")


def test_the_path_reaches_the_real_value_in_the_seed(current_threshold):
    assert isinstance(current_threshold, int)


def test_a_path_that_does_not_exist_raises_rather_than_returning_none(workflow):
    with pytest.raises(NoSuchLocation):
        read_at(workflow, "approval_routes[99].condition.value")


def test_a_missing_key_is_distinguished_from_a_none_value(workflow):
    """"There is no such place" and "the value is None" are different facts."""
    workflow["approval_routes"][0]["condition"]["value"] = None
    assert read_at(workflow, "approval_routes[0].condition.value") is None
    with pytest.raises(NoSuchLocation):
        read_at(workflow, "approval_routes[0].condition.nonexistent")


# --- applying ----------------------------------------------------------------


def test_applying_does_not_touch_the_original(workflow, current_threshold):
    before = copy.deepcopy(workflow)
    apply_edits(workflow, [_edit(current_threshold, current_threshold // 2)])
    assert workflow == before, (
        "showing what a change would look like modified the document; that "
        "makes 'preview' and 'do it' the same operation")


def test_the_new_document_carries_the_new_value(workflow, current_threshold):
    proposed = current_threshold // 2
    result = apply_edits(workflow, [_edit(current_threshold, proposed)])
    assert read_at(result.after, THRESHOLD_PATH) == proposed


def test_an_edit_whose_expected_value_is_wrong_is_stale(workflow,
                                                        current_threshold):
    with pytest.raises(StaleDiff, match="the snapshot moved"):
        apply_edits(workflow, [_edit(current_threshold + 1, 1)])


def test_a_stale_diff_is_not_silently_reconciled(workflow, current_threshold):
    """Refusing is the point. Applying anyway would apply a different change."""
    stale = _edit(current_threshold + 1, 42)
    result, problem = dry_run(workflow, [stale])
    assert result is None and "snapshot moved" in problem
    assert read_at(workflow, THRESHOLD_PATH) == current_threshold


def test_an_edit_that_changes_nothing_is_recorded_rather_than_applied(
        workflow, current_threshold):
    result = apply_edits(workflow, [_edit(current_threshold, current_threshold)])
    assert result.notes and "nothing to do" in result.notes[0]


# --- the post-conditions -----------------------------------------------------


def test_the_seed_workflow_has_no_unreachable_route(workflow):
    """The ground truth. If this ever fails the seed itself is broken."""
    assert unreachable_routes(workflow) == []


def test_lowering_one_threshold_past_another_kills_the_later_route(
        workflow, current_threshold):
    """The failure a text diff cannot see.

    First match decides. Drop the earlier route's bound below the later
    one's and the later route is dead -- still in the file, still reviewed,
    still believed to work.
    """
    capex = read_at(workflow, "approval_routes[2].condition.value")
    edit = StructuredEdit(
        artifact="workflow/approval_routes.yaml",
        path="approval_routes[2].condition.value",
        expected_current=capex, proposed=current_threshold // 2,
        reason="deliberately breaking the ordering")
    with pytest.raises(InvalidAfterEdit, match="can never be reached"):
        apply_edits(workflow, [edit], validate=validate_workflow)


def test_the_dead_route_is_named_along_with_what_shadows_it(
        workflow, current_threshold):
    workflow["approval_routes"][2]["condition"]["value"] = 1
    dead = unreachable_routes(workflow)
    assert len(dead) == 1
    assert "approval_routes[3]" in dead[0]
    assert "approval_routes[2]" in dead[0], (
        "the finding must say WHICH earlier route shadows it; a reader "
        "cannot fix 'this is unreachable' on its own")


def test_routes_on_different_fields_never_shadow_each_other(workflow):
    """Two routes testing different things are not in conflict."""
    assert unreachable_routes(workflow) == []
    fields = {r["condition"].get("field") for r in workflow["approval_routes"]}
    assert len(fields) > 1, "the seed must exercise more than one field here"


def test_a_workflow_with_no_routes_left_is_invalid():
    assert validate_workflow({"approval_routes": []}) == [
        "there are no approval routes left"]


def test_a_route_missing_its_operator_is_invalid():
    problems = validate_workflow(
        {"approval_routes": [{"id": "X", "condition": {"value": 1}}]})
    assert any("operator" in p for p in problems)


def test_validation_accepts_both_spellings_of_the_operator_key():
    """`op` in the seed, `operator` in the schema docs. Both are real."""
    for key in ("op", "operator"):
        route = {"id": "X", "condition": {"field": "amount", key: ">=",
                                          "value": 1}}
        assert validate_workflow({"approval_routes": [route]}) == []


# --- spreadsheets ------------------------------------------------------------


def test_a_spreadsheet_edit_is_a_cell_edit():
    edit = cell_edit("data/rules.xlsx", "Sheet1!C4", expected_current=1,
                     proposed=2, reason="matching the approved threshold")
    assert edit.kind == "spreadsheet"
    assert edit.path == "Sheet1!C4"


def test_replacing_a_spreadsheet_wholesale_is_refused():
    with pytest.raises(NoSuchLocation, match="cannot be reviewed"):
        cell_edit("data/rules.xlsx", "the whole file", expected_current=1,
                  proposed=2, reason="r")


@pytest.mark.parametrize("bad", ["C4", "Sheet1!", "Sheet1!4C", "!C4"])
def test_a_malformed_cell_reference_is_caught_while_building(bad):
    with pytest.raises(NoSuchLocation):
        cell_edit("data/rules.xlsx", bad, expected_current=1, proposed=2,
                  reason="r")


# --- rendering ---------------------------------------------------------------


def test_the_rendering_is_deterministic(workflow, current_threshold):
    edits = [_edit(current_threshold, current_threshold // 2)]
    first = render(apply_edits(workflow, edits))
    second = render(apply_edits(workflow, edits))
    assert first == second


def test_the_rendering_carries_the_reason_and_the_evidence(
        workflow, current_threshold):
    text = render(apply_edits(workflow, [_edit(current_threshold, 1)]))
    assert "why:" in text and "evidence:" in text


def test_nothing_proposed_renders_as_nothing_proposed(workflow):
    from core.change_intelligence.structured_diff import DiffResult

    assert render(DiffResult(artifact="x")) == "no structured edit proposed"
