# -*- coding: utf-8 -*-
"""Model-proposed gaps and relations over one snapshot (KNO-06): what the server keeps of a model's suggestions (pure, no I/O).

The model only suggests. A gap suggestion is kept when its kind is one a person can act on here (`missing_exception`, `conflicting_facts`), every rule it names was
shown, its evidence references belong to the named rules (and the asker may read them), its question is plain text, and it does not repeat a question the
snapshot already asks. A relation suggestion is kept under the same rules as a dependency proposal (`business_impact.resolve_dependencies`). Kept items are
PROPOSED findings labelled as suggested by a model: they change nothing until a person answers through the existing answer path, and there is no path that
confirms a relation in this version (a proposed relation is read-only, like a structural conflict). `NONE_FOUND` is a coverage statement, never proof.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.business_change import safe_text
from core.change_intelligence.v2.business_graph import edge_id
from core.change_intelligence.v2.business_impact import DEP_RELATIONS, DEP_STATUSES, rule_refs
from core.change_intelligence.v2.serialize import canonical_hash

PROPOSAL_KEYS = {"status", "gaps", "relations", "notes"}
GAP_ITEM_KEYS = {"kind", "rule_ids", "question", "evidence_refs"}
REL_ITEM_KEYS = {"from_rule_id", "to_rule_id", "relation", "rationale", "evidence_refs"}
PROPOSABLE_GAP_KINDS = frozenset({"missing_exception", "conflicting_facts"})
ORIGIN = "model_proposal"
LABEL = "suggested by a model"


def validate_proposal_object(obj, limits: Mapping) -> dict:
    """Shape only; what the entries mean is decided by `resolve_proposals`."""
    if not isinstance(obj, dict) or set(obj) != PROPOSAL_KEYS or obj["status"] not in DEP_STATUSES:
        raise ValueError("keys_or_status")
    for key in ("gaps", "relations", "notes"):
        if not isinstance(obj[key], list):
            raise ValueError("lists")
    if not all(isinstance(n, str) for n in obj["notes"]):
        raise ValueError("notes")
    if len(obj["gaps"]) > limits["max_gaps"] or len(obj["relations"]) > limits["max_relations"] or len(obj["notes"]) > limits["max_notes"]:
        raise ValueError("sizes")
    for g in obj["gaps"]:
        if not isinstance(g, dict) or set(g) != GAP_ITEM_KEYS or not isinstance(g["kind"], str) or not isinstance(g["question"], str):
            raise ValueError("gap_shape")
        if not isinstance(g["rule_ids"], list) or not all(isinstance(x, str) for x in g["rule_ids"]):
            raise ValueError("gap_rules")
        if not isinstance(g["evidence_refs"], list) or not all(isinstance(x, str) for x in g["evidence_refs"]):
            raise ValueError("gap_refs")
    for d in obj["relations"]:
        if not isinstance(d, dict) or set(d) != REL_ITEM_KEYS or not all(isinstance(d[k], str) for k in ("from_rule_id", "to_rule_id", "relation", "rationale")):
            raise ValueError("relation_shape")
        if not isinstance(d["evidence_refs"], list) or not all(isinstance(x, str) for x in d["evidence_refs"]):
            raise ValueError("relation_refs")
    return obj


def _norm_question(text: str) -> str:
    return " ".join(text.lower().split())


def proposed_gap(kind: str, rule_ids: list, question: str, refs: list, rules: Mapping[str, Mapping]) -> dict:
    """The gap as the queue shows it and as the snapshot stores it once a person answers (contract shape; the model label is carried by the id key only)."""
    rids = sorted(rule_ids)
    gap = {"gap_id": bk.gap_id_for(kind, rids, "model:" + canonical_hash(_norm_question(question))[:12]), "kind": kind, "status": "OPEN", "affected_rule_ids": rids,
           "question": question, "evidence_refs": sorted(set(refs)), "requires_permission": "knowledge.confirm", "priority": "normal"}
    if kind == "conflicting_facts":
        gap["options"] = [{"rule_id": rid, "label": rules[rid]["title"]} for rid in rids]
    bk.validate_gap(gap)
    return gap


def resolve_proposals(obj: Mapping, rules: Mapping[str, Mapping], existing_gaps: Iterable[Mapping], existing_relations: Iterable[Mapping], *, limits: Mapping,
                      can_read_evidence: bool) -> dict:
    """What the server keeps. `rules` = the rules shown to the model (the only ids a proposal may name)."""
    asked = {(g["kind"], tuple(sorted(g["affected_rule_ids"]))) for g in existing_gaps}
    asked_questions = {_norm_question(g["question"]) for g in existing_gaps}
    known_edges = {(e["from"], e["to"], e["type"]) for e in existing_relations} | {(e["to"], e["from"], e["type"]) for e in existing_relations}
    gaps, relations, rejected, seen = [], [], [], set()
    for g in obj["gaps"]:
        kind, rids = g["kind"], list(dict.fromkeys(g["rule_ids"]))

        def no(reason):
            rejected.append({"item": "gap", "reason": reason, "kind": kind[:40], "rule_ids": [r[:80] for r in rids][:4]})

        question = safe_text(g["question"], limits["max_question_chars"])
        if kind not in PROPOSABLE_GAP_KINDS:
            no("kind_not_allowed")
        elif not rids or any(r not in rules for r in rids):
            no("invented_rule_id")
        elif (kind == "conflicting_facts") != (len(rids) == 2) or (kind == "missing_exception" and len(rids) != 1):
            no("rule_count_for_kind")
        elif question is None:
            no("question_not_plain_text")
        elif (kind, tuple(sorted(rids))) in asked or _norm_question(question) in asked_questions:
            no("already_asked")
        else:
            allowed = set().union(*(rule_refs(rules[r]) for r in rids)) if can_read_evidence else set(rids)
            refs = list(dict.fromkeys(g["evidence_refs"]))
            key = (kind, tuple(sorted(rids)), _norm_question(question))
            if not refs or len(refs) > limits["max_evidence_refs"]:
                no("evidence_refs_missing_or_too_many")
            elif not set(refs) <= allowed:
                no("evidence_ref_not_in_the_named_rules")
            elif key in seen:
                no("duplicate")
            else:
                seen.add(key)
                gaps.append(proposed_gap(kind, rids, question, refs, rules))
    for d in obj["relations"]:
        a, b, rel = d["from_rule_id"], d["to_rule_id"], d["relation"]

        def no_rel(reason):
            rejected.append({"item": "relation", "reason": reason, "from_rule_id": a[:80], "to_rule_id": b[:80], "relation": rel[:40]})

        if a not in rules or b not in rules:
            no_rel("invented_rule_id")
        elif a == b:
            no_rel("same_rule")
        elif rel not in DEP_RELATIONS:
            no_rel("relation_not_allowed")
        elif (a, b, rel) in known_edges:
            no_rel("already_in_the_graph")
        else:
            why = safe_text(d["rationale"], limits["max_rationale_chars"])
            allowed = rule_refs(rules[a]) | rule_refs(rules[b]) if can_read_evidence else {a, b}
            refs = list(dict.fromkeys(d["evidence_refs"]))
            if why is None:
                no_rel("rationale_not_plain_text")
            elif not refs or len(refs) > limits["max_evidence_refs"]:
                no_rel("evidence_refs_missing_or_too_many")
            elif not set(refs) <= allowed:
                no_rel("evidence_ref_not_in_the_named_rules")
            elif ("rel", a, b, rel) in seen:
                no_rel("duplicate")
            else:
                seen.add(("rel", a, b, rel))
                edge = {"edge_id": edge_id(rel, a, b), "from": a, "to": b, "type": rel, "status": "PROPOSED", "provenance_refs": refs}
                bk.validate_relation(edge)
                relations.append(dict(edge, rationale=why, provenance="ai_inference"))
    notes = [t for t in (safe_text(n, limits["max_rationale_chars"]) for n in obj["notes"]) if t]
    return {"gaps": gaps, "relations": relations, "rejected": rejected, "notes": notes}


def build_prompt(frozen: Mapping, rules: Iterable[Mapping], asked: Iterable[str], edges: Iterable[str], *, can_read_evidence: bool, max_chars: int) -> str:
    lines, used = [], 0
    for r in rules:
        cond = r.get("condition")
        line = (f"- id={r['rule_id']} actor={r.get('actor_ref')} outcome={(r.get('outcome') or {}).get('kind')} scope={r.get('scope')} status={r.get('status')}\n"
                f"  title: {r['title']}\n  condition: {cond}\n  exceptions: {[e['text'] for e in r.get('exceptions') or []]}")
        if can_read_evidence:
            line += "\n  references: " + "; ".join(sorted(rule_refs(r) - {r["rule_id"]}))
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return (frozen["prompt_template"].replace("{rules}", "\n".join(lines) or "(none)").replace("{asked}", "\n".join(asked) or "(none)")
            .replace("{edges}", "\n".join(edges) or "(none)"))
