# -*- coding: utf-8 -*-
"""Business rule graph and gap detection (pure, no I/O). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md.

Derives, from `business_rule` objects only:
  * typed relation proposals (`defines`, `approves`, `conflicts_with`) with their provenance;
  * gaps: missing actor / condition / outcome and conflicting facts, with stable ids (`gap_id_for`);
  * a bounded traversal that discloses what it left out.
Rules it keeps: a relation is CONFIRMED only when its rule is CONFIRMED and a human decision backs it, otherwise PROPOSED; the same number in
two different rules is never merged (a conflict needs the same actor, outcome, concepts, overlapping scope and the same other conditions with
a different value); nothing is inferred from keywords; an expression it cannot compare is simply not compared.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.business_knowledge import gap_id_for
from core.change_intelligence.v2.serialize import canonical_hash

MAX_HOPS = 2
MAX_NODES = 200
_OUTCOME_UNKNOWN = frozenset({"unknown", "unspecified"})
ALL_GAP_KINDS = ("missing_actor", "missing_condition", "missing_outcome", "conflicting_facts")


def edge_id(kind: str, source: str, target: str) -> str:
    return "edge-" + canonical_hash({"type": kind, "from": source, "to": target})[:16]


def _evidence_refs(rule: Mapping) -> list:
    refs = [b["evidence_ref"] for b in rule["source_bindings"]]
    refs += [p["evidence_ref"] for p in rule["provenance"] if p.get("kind") == "source_evidence"]
    return sorted(set(refs))


def _decision_refs(rule: Mapping) -> list:
    return [p["decision_id"] for p in rule["provenance"] if p.get("kind") == "human_decision"]


def _edge(kind: str, source: str, target: str, rule: Mapping) -> dict:
    decisions = _decision_refs(rule)
    confirmed = rule["status"] == "CONFIRMED" and bool(decisions)
    refs = decisions if confirmed else _evidence_refs(rule)
    return {"edge_id": edge_id(kind, source, target), "from": source, "to": target, "type": kind,
            "status": "CONFIRMED" if confirmed else "PROPOSED", "provenance_refs": list(refs)}


def _leaves(condition) -> Optional[list]:
    """The comparisons of a purely conjunctive condition, or None when it has any/not/in and so cannot be compared safely."""
    if condition is None:
        return None
    if "field" in condition:
        return [condition] if condition["op"] not in ("in", "not_in") else None
    if "all" in condition:
        out = []
        for child in condition["all"]:
            sub = _leaves(child)
            if sub is None:
                return None
            out.extend(sub)
        return out
    return None


def _scopes_overlap(a: Mapping, b: Mapping) -> bool:
    for key in set(a) & set(b):
        va, vb = a[key], b[key]
        sa = set(va) if isinstance(va, list) else {va}
        sb = set(vb) if isinstance(vb, list) else {vb}
        if not (sa & sb):
            return False
    return True


def _conflict(a: Mapping, b: Mapping) -> Optional[dict]:
    """The one differing field when two rules say different things about the same situation, else None."""
    if a["authority"] != b["authority"] or a["actor_ref"] is None or a["actor_ref"] != b["actor_ref"] or a["outcome"].get("kind") != b["outcome"].get("kind"):
        return None
    if not (set(a["concept_ids"]) & set(b["concept_ids"])) or not _scopes_overlap(a["scope"], b["scope"]):
        return None
    if _unit_unknown_fields(a) or _unit_unknown_fields(b):
        return None                                   # a number whose unit the sources did not settle is not comparable: its own gap asks for the unit first
    la, lb = _leaves(a["condition"]), _leaves(b["condition"])
    if la is None or lb is None:
        return None
    ma = {(l["field"], l["op"]): l for l in la}
    mb = {(l["field"], l["op"]): l for l in lb}
    if set(ma) != set(mb) or len(ma) != len(la) or len(mb) != len(lb):
        return None
    differing = [key for key in ma if (ma[key]["value"], ma[key].get("unit")) != (mb[key]["value"], mb[key].get("unit"))]
    if len(differing) != 1:
        return None
    field, op = differing[0]
    return {"field": field, "op": op, "a": ma[differing[0]], "b": mb[differing[0]]}


_INACTIVE = frozenset({"SUPERSEDED", "REJECTED"})


def derive_relations(rules: Iterable[Mapping]) -> list:
    rules = sorted((r for r in rules if r["status"] not in _INACTIVE), key=lambda r: r["rule_id"])
    edges = {}
    for rule in rules:
        for concept in rule["concept_ids"]:
            e = _edge("defines", concept, rule["rule_id"], rule)
            edges[e["edge_id"]] = e
        if rule["actor_ref"]:
            e = _edge("approves", rule["actor_ref"], rule["rule_id"], rule)
            edges[e["edge_id"]] = e
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            if _conflict(a, b):
                e = _edge("conflicts_with", a["rule_id"], b["rule_id"], a)
                e["provenance_refs"] = sorted(set(_evidence_refs(a)) | set(_evidence_refs(b)))
                e["status"] = "PROPOSED"                      # a conflict is a finding for a person, never an authority
                edges[e["edge_id"]] = e
    return [edges[k] for k in sorted(edges)]


def _unit_unknown_fields(rule: Mapping) -> list:
    return sorted(u.split(":", 1)[1] for u in rule.get("unknown_fields", []) if isinstance(u, str) and u.startswith("unit:"))


def derive_gaps(rules: Iterable[Mapping], kinds: Iterable[str] = ALL_GAP_KINDS) -> list:
    kinds = set(kinds)
    rules = sorted((r for r in rules if r["status"] not in _INACTIVE), key=lambda r: r["rule_id"])   # a withdrawn rule raises and joins nothing
    gaps = []

    def add(kind, rule_ids, question, evidence, priority="normal", options=None, key=""):
        gap = {"gap_id": gap_id_for(kind, rule_ids, key), "kind": kind, "status": "OPEN", "affected_rule_ids": sorted(rule_ids), "question": question,
               "evidence_refs": sorted(set(evidence)), "requires_permission": "knowledge.confirm", "priority": priority}
        if options:
            gap["options"] = options
        gaps.append(gap)

    for rule in rules:
        title = rule["title"]
        if rule["actor_ref"] is None and "missing_actor" in kinds:
            add("missing_actor", [rule["rule_id"]], f"Who acts on the rule \"{title}\"?", _evidence_refs(rule),
                "critical" if rule["outcome"].get("kind") == "review_required" else "normal")
        if rule["condition"] is None and "missing_condition" in kinds:
            add("missing_condition", [rule["rule_id"]], f"How exactly does the rule \"{title}\" decide when it applies?", _evidence_refs(rule))
        elif _unit_unknown_fields(rule) and "missing_condition" in kinds:
            fields = ", ".join(_unit_unknown_fields(rule))
            add("missing_condition", [rule["rule_id"]], f"The sources give the amount in the rule \"{title}\" without a unit ({fields}). In which unit is it, and how exactly does the rule decide?",
                _evidence_refs(rule), key="unit")
        if rule["outcome"].get("kind") in _OUTCOME_UNKNOWN and "missing_outcome" in kinds:
            add("missing_outcome", [rule["rule_id"]], f"What happens when the rule \"{title}\" applies?", _evidence_refs(rule))
    if "conflicting_facts" in kinds:
        for i, a in enumerate(rules):
            for b in rules[i + 1:]:
                c = _conflict(a, b)
                if c:
                    question = (f"The sources disagree about {c['field']}: \"{a['title']}\" says {c['a']['value']}, \"{b['title']}\" says {c['b']['value']}. "
                                "Which one is the business rule?")
                    add("conflicting_facts", [a["rule_id"], b["rule_id"]], question, _evidence_refs(a) + _evidence_refs(b), "critical",
                        [{"rule_id": a["rule_id"], "label": f"{c['a']['value']}"}, {"rule_id": b["rule_id"], "label": f"{c['b']['value']}"}], key=c["field"])
    return gaps


def traverse(edges: Iterable[Mapping], start: str, *, max_hops: int = MAX_HOPS, max_nodes: int = MAX_NODES) -> dict:
    """Bounded breadth-first walk (edges taken as undirected). Cycle-safe; says how many nodes it left out and why, and which nodes of the connected part it never
    reached (`unreached`)."""
    adj: dict = {}
    for e in edges:
        adj.setdefault(e["from"], []).append((e, e["to"]))
        adj.setdefault(e["to"], []).append((e, e["from"]))
    seen, order, paths = {start}, [start], {start: []}
    queue = deque([(start, 0)])
    omitted_nodes, too_deep = 0, 0
    while queue:
        node, depth = queue.popleft()
        for e, other in sorted(adj.get(node, []), key=lambda p: p[0]["edge_id"]):
            if other in seen:
                continue
            if depth + 1 > max_hops:
                too_deep += 1
                continue
            if len(order) >= max_nodes:
                omitted_nodes += 1
                continue
            seen.add(other)
            order.append(other)
            paths[other] = paths[node] + [e["edge_id"]]
            queue.append((other, depth + 1))
    rest, stack = set(), list(order)                         # the rest of the connected part: what the limits actually left out (a role or concept is not a rule)
    while stack:
        for _, other in adj.get(stack.pop(), []):
            if other not in seen and other not in rest:
                rest.add(other)
                stack.append(other)
    return {"start": start, "nodes": order, "paths": paths, "omitted": {"nodes_over_cap": omitted_nodes, "beyond_hops": too_deep, "max_hops": max_hops,
                                                                          "max_nodes": max_nodes}, "unreached": sorted(rest)}


MAX_UNBOUND_LISTED = 50
_CELL = __import__("re").compile(r"([A-Z]{1,3})([0-9]{1,7})")


def unbound_values(rules: Iterable[Mapping], observations: Iterable[Mapping], artifact_paths: Mapping[str, str]) -> dict:
    """Numbers the structured sources hold (a YAML key, a sheet cell) that no rule's binding or provenance points at: the extraction omission the server can see.
    A listed value may be metadata or a rule nobody extracted; a person decides. -> {"count", "values": [{"evidence_ref", "value"}] (first 50, sorted), "truncated"}."""
    from core.change_intelligence.v2.business_draft import _locator_label, _num
    bound = set()
    for r in rules:
        for ref in [b.get("evidence_ref") for b in r.get("source_bindings") or []] + [p.get("evidence_ref") for p in r.get("provenance") or [] if isinstance(p, Mapping)]:
            if isinstance(ref, str) and "#" in ref:
                path, _, label = ref.partition("#")
                bound.add((path.rsplit("/", 1)[-1], label))
    observations = list(observations)
    cells = {}
    for o in observations:                                   # sheet cells by (artifact, sheet, column, row): the header (row 1) and the row label (column A) give a cell its meaning
        loc = o.get("locator") if isinstance(o.get("locator"), Mapping) else {}
        m = _CELL.fullmatch(str(loc.get("cell") or ""))
        if loc.get("sheet") and m:
            cells[(o.get("artifact_id"), loc["sheet"], m.group(1), int(m.group(2)))] = o.get("raw_value")
    out = {}
    for o in observations:
        label = _locator_label(o.get("locator"))
        raw = o.get("raw_value")
        if label is None or isinstance(raw, bool) or not (isinstance(raw, (int, float)) or (isinstance(raw, str) and _num(raw) is not None)):
            continue
        path = str(artifact_paths.get(o.get("artifact_id")) or o.get("artifact_id") or "")
        if (path.rsplit("/", 1)[-1], label) in bound:
            continue
        ref = f"{path}#{label}"
        entry = {"evidence_ref": ref, "value": raw, "file": path, "location": label}
        loc = o.get("locator") if isinstance(o.get("locator"), Mapping) else {}
        m = _CELL.fullmatch(str(loc.get("cell") or ""))
        if loc.get("sheet") and m:
            col, row = m.group(1), int(m.group(2))
            header, row_label = cells.get((o.get("artifact_id"), loc["sheet"], col, 1)), cells.get((o.get("artifact_id"), loc["sheet"], "A", row))
            entry["sheet"], entry["cell"] = loc["sheet"], loc["cell"]
            entry["header"] = header if isinstance(header, str) and row != 1 else None
            entry["row_label"] = row_label if isinstance(row_label, str) and col != "A" else None
        out[ref] = entry
    listed = [out[k] for k in sorted(out)]
    return {"count": len(listed), "values": listed[:MAX_UNBOUND_LISTED], "truncated": len(listed) > MAX_UNBOUND_LISTED}
