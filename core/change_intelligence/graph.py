# -*- coding: utf-8 -*-
"""The Enterprise Graph.

Relationships between artifacts and the semantic rules they state. P6's
impact analysis runs over this graph, so a missing edge type is not a
cosmetic gap: it is impact recall lost, and lost invisibly.

Eleven edge types. Five of them carry the product:

    IMPLEMENTS   software  -> rule
    DOCUMENTS    document  -> rule
    CONFIGURES   workflow  -> rule
    TESTS        test      -> rule
    EXEMPTS      exception -> rule

Those five are what make "this rule changed, so what else moves?" a
question with an answer. The rest describe structure inside one family.

No business inference happens here. An edge is drawn because an adapter
observed something or because identity resolution grouped two statements;
the graph never decides that two things MEAN the same, it records that
something else already decided so, and cites it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The eleven. CONTRADICTS is produced at P4 from conflict detection, not
#: here -- it is a conclusion, and P3 only records observations.
EDGE_TYPES = (
    "CALLS", "READS", "WRITES", "CONTAINS", "DEPENDS_ON",
    "IMPLEMENTS", "DOCUMENTS", "CONFIGURES", "TESTS",
    "TRIGGERS", "APPROVES", "EXEMPTS", "SUPERSEDES", "CONTRADICTS",
)

#: The subset that connects an artifact to a semantic rule. Impact analysis
#: starts from a rule and walks these outward; everything else is how a
#: single artifact hangs together internally.
SEMANTIC_EDGE_TYPES = ("IMPLEMENTS", "DOCUMENTS", "CONFIGURES", "TESTS", "EXEMPTS")


@dataclass(frozen=True)
class Node:
    id: str
    kind: str           # artifact | rule | function | module | change | role
    family: str = ""
    label: str = ""
    attributes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    type: str
    #: Never optional. A list of affected files persuades nobody and cannot
    #: be checked; the reason is what a person reads when deciding whether
    #: to believe the impact set.
    reason: str
    evidence: tuple = ()
    distance: str = "DIRECT"

    def __post_init__(self) -> None:
        if self.type not in EDGE_TYPES:
            raise ValueError(f"{self.type!r} is not one of the {len(EDGE_TYPES)} edge types")
        if not self.reason:
            raise ValueError(f"edge {self.src} -{self.type}-> {self.dst} has no reason")


class EnterpriseGraph:
    """Nodes and edges, with the queries impact analysis needs.

    Deliberately not a general graph database: every query here exists
    because a named task needs it, which keeps the shape honest.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []

    # --- construction --------------------------------------------------------

    def add_node(self, node: Node) -> None:
        existing = self._nodes.get(node.id)
        if existing is not None and existing != node:
            merged = dict(existing.attributes)
            merged.update(node.attributes)
            node = Node(id=node.id, kind=node.kind or existing.kind,
                        family=node.family or existing.family,
                        label=node.label or existing.label, attributes=merged)
        self._nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge not in self._edges:
            self._edges.append(edge)

    # --- queries -------------------------------------------------------------

    def node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def nodes(self, kind: str | None = None) -> list[Node]:
        return sorted(
            (n for n in self._nodes.values() if kind is None or n.kind == kind),
            key=lambda n: n.id)

    def edges(self, edge_type: str | None = None) -> list[Edge]:
        return sorted(
            (e for e in self._edges if edge_type is None or e.type == edge_type),
            key=lambda e: (e.type, e.src, e.dst))

    def edges_from(self, node_id: str, types: tuple[str, ...] | None = None) -> list[Edge]:
        return [e for e in self.edges() if e.src == node_id
                and (types is None or e.type in types)]

    def edges_to(self, node_id: str, types: tuple[str, ...] | None = None) -> list[Edge]:
        return [e for e in self.edges() if e.dst == node_id
                and (types is None or e.type in types)]

    def artifacts_stating(self, rule_id: str) -> list[str]:
        """Every artifact that states a rule, via the five semantic edges."""
        return sorted({
            e.src for e in self.edges_to(f"rule:{rule_id}", SEMANTIC_EDGE_TYPES)
        })

    def impact_scope(self, rule_id: str, *, max_depth: int = 2) -> dict[str, list[str]]:
        """What moves if this rule changes, DIRECT and INDIRECT kept apart.

        Merging the two is how precision dies: an indirect neighbour four
        hops out would count the same as the artifact that literally states
        the rule, and every impact set becomes the whole graph (the impact
        metrics report them separately for the same reason).
        """
        direct = {e.src for e in self.edges_to(f"rule:{rule_id}", SEMANTIC_EDGE_TYPES)}

        # Impact travels in two directions, and they are not the same edge.
        #
        #   OUTWARD   a changed artifact CONTAINS functions that may change
        #   INWARD    whatever CALLS or TESTS a changed thing is affected by it
        #
        # An earlier version walked only inward, which asked "what points at
        # this file?" -- and nothing points at a document, so INDIRECT came
        # back empty and the impact set stopped at the file boundary.
        indirect: set[str] = set()
        frontier = set(direct)
        for _ in range(max(0, max_depth - 1)):
            nxt: set[str] = set()
            for node_id in sorted(frontier):
                for edge in self.edges_from(node_id, ("CONTAINS", "READS", "DEPENDS_ON")):
                    nxt.add(edge.dst)
                for edge in self.edges_to(node_id, ("CALLS", "TESTS", "CONTAINS")):
                    nxt.add(edge.src)
            nxt -= direct | indirect
            indirect |= nxt
            frontier = nxt
            if not frontier:
                break

        return {"direct": sorted(direct), "indirect": sorted(indirect - direct)}

    def dangling_edges(self) -> list[Edge]:
        """Edges pointing at nodes that do not exist.

        Measured on the inherited framework's real project: 41 of 82
        endpoints pointed at nothing, and every coverage metric computed
        over that graph measured the empty set and reported 100%.
        """
        return [e for e in self.edges()
                if e.src not in self._nodes or e.dst not in self._nodes]

    def counts(self) -> dict[str, int]:
        counts = {t: 0 for t in EDGE_TYPES}
        for edge in self._edges:
            counts[edge.type] += 1
        return counts


# --- building from ingestion ---------------------------------------------------


def build(ingestion, resolution) -> EnterpriseGraph:
    """Assemble the graph from ingested facts and resolved identities.

    Two inputs, two roles: ingestion supplies what was OBSERVED, resolution
    supplies which observations refer to one rule. The graph joins them and
    invents nothing.
    """
    graph = EnterpriseGraph()

    # Rules first, so edges have somewhere to land.
    for group in resolution.groups:
        graph.add_node(Node(
            id=f"rule:{group.semantic_id}", kind="rule", label=group.semantic_id,
            attributes={"tier": group.tier, "basis": group.basis,
                        "values": sorted(str(v) for v in group.values)}))

    for record in ingestion.records:
        _add_record(graph, record)

    # A module that defines functions is itself a node; adapters emit the
    # CONTAINS edge from it without ever declaring it.
    for edge in list(graph.edges()):
        _ensure_referenced_nodes(graph, edge.src, edge.dst)
    for edge in list(graph.edges()):
        if graph.node(edge.src) is None and edge.src.startswith("python:"):
            graph.add_node(Node(id=edge.src, kind="module", family="software",
                                label=edge.src.split(":", 1)[1]))

    # An artifact and the module parsed out of it are the same file under two
    # ids -- `artifact:application/config.py` from identity resolution,
    # `python:application/config.py` from the parser. Without an edge between
    # them the graph is two disconnected halves, and impact analysis stops at
    # the file boundary: it can say WHICH file states a rule and never which
    # function inside it does. Every INDIRECT result depends on this join.
    for node in graph.nodes("module"):
        if not node.id.startswith("python:"):
            continue
        path = node.id.split(":", 1)[1]
        artifact_id = f"artifact:{path}"
        if graph.node(artifact_id) is None:
            continue
        graph.add_edge(Edge(
            src=artifact_id, dst=node.id, type="CONTAINS",
            reason=f"{path} parsed as a module", evidence=((path, ""),)))

    # The five semantic edges, one per grouped statement.
    for group in resolution.groups:
        for member in group.members:
            graph.add_node(Node(id=f"artifact:{member.artifact}", kind="artifact",
                                family=member.family, label=member.artifact))
            graph.add_edge(Edge(
                src=f"artifact:{member.artifact}",
                dst=f"rule:{group.semantic_id}",
                type=_semantic_edge_for(member.family),
                reason=(f"{member.artifact} states {group.semantic_id} at "
                        f"{member.locator} ({group.tier} match: {group.basis})"),
                evidence=((member.artifact, member.locator),),
            ))

    return graph


#: family -> which of the five semantic edges it draws. A document DOCUMENTS
#: a rule; a workflow CONFIGURES it; code IMPLEMENTS it. Same relationship
#: in the graph's shape, different in what it obliges anyone to do about it.
_EDGE_FOR_FAMILY = {
    "software": "IMPLEMENTS",
    "workflow": "CONFIGURES",
    "docs": "DOCUMENTS",
    "spreadsheet": "DOCUMENTS",
    "tests": "TESTS",
    "history": "SUPERSEDES",
}


def _semantic_edge_for(family: str) -> str:
    return _EDGE_FOR_FAMILY.get(family, "DOCUMENTS")


def _add_record(graph: EnterpriseGraph, record) -> None:
    payload = record.payload
    provenance = record.provenance[0] if record.provenance else None
    artifact = (getattr(provenance, "file", None) or getattr(provenance, "doc", None) or "")

    if record.kind == "relation":
        edge_type = payload.get("type", "")
        if edge_type not in EDGE_TYPES:
            return
        if edge_type in SEMANTIC_EDGE_TYPES:
            # An adapter may NOTICE that a claim looks like it is about a
            # rule; identity resolution DECIDES it. Keeping both would draw
            # two edges to the same rule -- one from the artifact and one
            # from the claim inside it -- and the artifact would be counted
            # twice in its own impact set. One decision, one edge.
            return
        graph.add_edge(Edge(
            src=payload["src"], dst=payload["dst"], type=edge_type,
            reason=payload.get("reason") or f"{edge_type} observed in {artifact}",
            evidence=((artifact, _locator(provenance)),)))
        _ensure_referenced_nodes(graph, payload["src"], payload["dst"])
        return

    kind = payload.get("type", "")
    if kind == "function":
        graph.add_node(Node(id=record.id, kind="function", family="software",
                            label=payload.get("name", ""),
                            attributes={"accepts_config": payload.get("accepts_config")}))
    elif kind == "class":
        graph.add_node(Node(id=record.id, kind="class", family="software",
                            label=payload.get("name", "")))
    elif kind == "change_event":
        graph.add_node(Node(id=record.id, kind="change", family="history",
                            label=payload.get("change_id", ""),
                            attributes={"date": payload.get("date", ""),
                                        "declares_temporary": payload.get("declares_temporary")}))
    elif kind == "exception":
        graph.add_node(Node(id=record.id, kind="exception", family="workflow",
                            label=payload.get("exception_id", "")))
    elif kind == "approval_route":
        graph.add_node(Node(id=record.id, kind="route", family="workflow",
                            label=payload.get("route_id", "")))
        role = payload.get("approver_role") or ""
        if role and role != "NONE":
            graph.add_node(Node(id=f"role:{role}", kind="role", label=role))
            graph.add_edge(Edge(
                src=f"role:{role}", dst=record.id, type="APPROVES",
                reason=f"{role} approves requests routed by {payload.get('route_id', '')}",
                evidence=((artifact, _locator(provenance)),)))
    elif kind in {"threshold", "predicate", "hardcoded_threshold", "config_threshold",
                  "doc_claim", "spreadsheet_claim", "rule_declaration"}:
        graph.add_node(Node(id=record.id, kind="claim",
                            family="", label=kind,
                            attributes={"value": payload.get("value")}))


#: Prefixes an adapter uses for something it REFERS to without having
#: parsed it: a function called from elsewhere, an imported module, an
#: artifact named in a changelog. They are real endpoints, and leaving
#: them absent makes every such edge dangle -- which is how a graph comes
#: to report 100% coverage of the empty set.
_REFERENCE_KINDS = {"callable": "function", "module": "module",
                    "artifact": "artifact", "rule": "rule", "role": "role"}

#: Bare endpoint names an adapter emits for something that is not addressed
#: by a prefix. `config` is the injected policy object -- a real thing every
#: correct handler reads, and the node a READS edge has to land on.
_BARE_NODES = {"config": ("config", "the injected policy configuration")}


def _ensure_referenced_nodes(graph: EnterpriseGraph, *node_ids: str) -> None:
    for node_id in node_ids:
        if graph.node(node_id) is not None:
            continue
        if node_id in _BARE_NODES:
            kind, label = _BARE_NODES[node_id]
            graph.add_node(Node(id=node_id, kind=kind, label=label))
            continue
        prefix = node_id.split(":", 1)[0]
        kind = _REFERENCE_KINDS.get(prefix)
        if kind is None:
            continue
        graph.add_node(Node(id=node_id, kind=kind,
                            label=node_id.split(":", 1)[1],
                            attributes={"referenced_only": True}))


def _locator(provenance) -> str:
    if provenance is None:
        return ""
    if getattr(provenance, "file", None):
        return f"L{provenance.line_start}"
    if getattr(provenance, "doc", None):
        return f"{provenance.sheet}!{provenance.row}"
    return ""
