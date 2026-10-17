"""Impact Graph + impact_scope() — plan Step 6.1, 6.2, A2 §9.

Built from IR + KM, rebuildable at any time — it is never a separate
source of truth. Four edge classes, each with its own propagation rule;
`cross_system` defaults OFF (A2 §15) so one decision doesn't silently
reach across the whole enterprise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

HARD_EDGES = {"EVIDENCED_BY", "DERIVED_FROM", "RENDERED_IN", "REALIZED_BY"}
SOFT_EDGES = {"CALLS", "READS", "WRITES", "PART_OF_PROCESS", "MATCHED_FROM_DOC"}
BLOCKING_EDGES = {"CONFIRMED_BY"}


@dataclass(frozen=True)
class Node:
    node_id: str
    layer: str  # "ir" | "km" | "doc"


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    edge_type: str
    semantic_trigger: Optional[str] = None  # condition_changed | actor_changed | meaning_changed

    @property
    def propagation(self) -> str:
        if self.edge_type in HARD_EDGES:
            return "hard"
        if self.edge_type in BLOCKING_EDGES:
            return "blocking"
        if self.edge_type in SOFT_EDGES:
            return "soft"
        raise ValueError(f"unknown edge_type {self.edge_type!r}")


class ImpactTouchesIRError(Exception):
    """Raised if impact_scope() would ever mark an IR-layer node affected —
    Fact must never be silently rewritten by a feedback loop.
    """


@dataclass
class ImpactResult:
    changed: list[str]
    affected: list[str] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    excluded: list[dict] = field(default_factory=list)


def impact_scope(
    changed_ids: list[str],
    nodes: dict[str, Node],
    edges_of: Callable[[str], list[Edge]],
    semantic_changed: Callable[[str, Edge], bool],
    max_depth: int = 6,
) -> ImpactResult:
    result = ImpactResult(changed=list(changed_ids))
    visited: set[str] = set()
    frontier: list[tuple[str, list[str], int]] = [(cid, [cid], 0) for cid in changed_ids]

    while frontier:
        node_id, path, depth = frontier.pop()
        if node_id in visited:
            continue  # cycle: visit-once
        visited.add(node_id)

        for edge in edges_of(node_id):
            if edge.propagation == "blocking":
                result.blocked.append({"id": edge.dst, "edge": edge.edge_type})
                continue
            if edge.propagation == "soft" and not semantic_changed(node_id, edge):
                result.excluded.append({"id": edge.dst, "reason": "soft_edge_not_triggered"})
                continue
            if depth + 1 > max_depth:
                result.excluded.append({"id": edge.dst, "reason": "review_required_max_depth"})
                continue
            if edge.dst not in result.reasons:
                result.reasons[edge.dst] = "semantic_change" if edge.propagation == "soft" else "hard_dependency"
            new_path = path + [edge.dst]
            result.paths.append(new_path)
            frontier.append((edge.dst, new_path, depth + 1))

    result.affected = sorted(visited - set(changed_ids))

    # Never let Fact be silently touched.
    touched_ir = [n for n in visited if nodes.get(n, Node(n, "km")).layer == "ir"]
    if touched_ir:
        raise ImpactTouchesIRError(
            f"impact_scope reached IR-layer node(s) {touched_ir} — this must never happen; "
            f"a fact_correction_request should have been raised instead"
        )

    return result
