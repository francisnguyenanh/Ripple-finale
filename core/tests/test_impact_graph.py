import pytest

from core.kernel.impact_graph import Edge, ImpactTouchesIRError, Node, impact_scope


def test_small_decision_touches_only_a_couple_records_not_the_whole_project():
    nodes = {
        "DEC-018": Node("DEC-018", "km"), "BR-22": Node("BR-22", "km"),
        "L3-04": Node("L3-04", "doc"),
    }
    graph = {
        "DEC-018": [Edge("DEC-018", "BR-22", "DERIVED_FROM")],
        "BR-22": [Edge("BR-22", "L3-04", "RENDERED_IN")],
        "L3-04": [],
    }
    result = impact_scope(
        ["DEC-018"], nodes, edges_of=lambda n: graph.get(n, []),
        semantic_changed=lambda n, e: True,
    )
    assert set(result.affected) == {"BR-22", "L3-04"}
    assert ["DEC-018", "BR-22", "L3-04"] in result.paths


def test_cycle_does_not_infinite_loop():
    nodes = {"A": Node("A", "km"), "B": Node("B", "km")}
    graph = {"A": [Edge("A", "B", "DERIVED_FROM")], "B": [Edge("B", "A", "DERIVED_FROM")]}
    result = impact_scope(["A"], nodes, edges_of=lambda n: graph.get(n, []), semantic_changed=lambda n, e: True)
    assert set(result.affected) == {"B"}  # terminates, doesn't hang


def test_exceeding_max_depth_marks_review_required_not_infinite():
    nodes = {f"N{i}": Node(f"N{i}", "km") for i in range(10)}
    graph = {f"N{i}": [Edge(f"N{i}", f"N{i+1}", "DERIVED_FROM")] for i in range(9)}
    graph["N9"] = []
    result = impact_scope(["N0"], nodes, edges_of=lambda n: graph.get(n, []),
                           semantic_changed=lambda n, e: True, max_depth=3)
    assert any(e["reason"] == "review_required_max_depth" for e in result.excluded)


def test_asserts_never_touches_ir_layer():
    nodes = {"BR-1": Node("BR-1", "km"), "PGM:A": Node("PGM:A", "ir")}
    graph = {"BR-1": [Edge("BR-1", "PGM:A", "DERIVED_FROM")], "PGM:A": []}
    with pytest.raises(ImpactTouchesIRError):
        impact_scope(["BR-1"], nodes, edges_of=lambda n: graph.get(n, []), semantic_changed=lambda n, e: True)


def test_soft_edge_only_propagates_when_semantic_change_detected():
    nodes = {"BR-1": Node("BR-1", "km"), "BP-1": Node("BP-1", "km")}
    graph = {"BR-1": [Edge("BR-1", "BP-1", "PART_OF_PROCESS")], "BP-1": []}
    result = impact_scope(["BR-1"], nodes, edges_of=lambda n: graph.get(n, []), semantic_changed=lambda n, e: False)
    assert result.affected == []
    assert result.excluded[0]["reason"] == "soft_edge_not_triggered"


def test_confirmed_by_edge_blocks_and_reports_conflict_for_human():
    nodes = {"BR-1": Node("BR-1", "km"), "BR-2": Node("BR-2", "km")}
    graph = {"BR-1": [Edge("BR-1", "BR-2", "CONFIRMED_BY")], "BR-2": []}
    result = impact_scope(["BR-1"], nodes, edges_of=lambda n: graph.get(n, []), semantic_changed=lambda n, e: True)
    assert result.blocked == [{"id": "BR-2", "edge": "CONFIRMED_BY"}]
    assert result.affected == []


def test_excluded_explains_why_something_was_not_touched():
    nodes = {"BR-1": Node("BR-1", "km"), "BR-2": Node("BR-2", "km")}
    graph = {"BR-1": [Edge("BR-1", "BR-2", "CALLS")], "BR-2": []}
    result = impact_scope(["BR-1"], nodes, edges_of=lambda n: graph.get(n, []), semantic_changed=lambda n, e: False)
    assert result.excluded == [{"id": "BR-2", "reason": "soft_edge_not_triggered"}]
