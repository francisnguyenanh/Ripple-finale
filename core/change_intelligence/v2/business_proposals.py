# -*- coding: utf-8 -*-
"""Model-proposed gaps and relations over one snapshot (KNO-06): what the server keeps of a model's suggestions (pure, no I/O).

The model only suggests. A gap suggestion is kept when its kind is one a person can act on here (`missing_exception`, `conflicting_facts`), every rule it names was
shown, its evidence references belong to the named rules (and the asker may read them), its question is plain text, and it does not repeat a question the
snapshot already asks. A relation suggestion is kept under the same rules as a dependency proposal (`business_impact.resolve_dependencies`). Kept items are
PROPOSED findings labelled as suggested by a model: they change nothing until a person answers through the existing answer path, and there is no path that
confirms a relation in this version (a proposed relation is read-only, like a structural conflict). `NONE_FOUND` is a coverage statement, never proof.
"""
from __future__ import annotations

import json
import re
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


def _ref_key(ref) -> str:
    """One comparable form of a reference: a locator written as JSON (`{"line_end": 44, "line_start": 8, ...}`) or as a bare key list (`line_end: 44, line_start: 8,
    ...`, as a model copies it) gives the same sorted key=value form; anything else compares as its own text. Only used to recognise an OFFERED reference."""
    if not isinstance(ref, str):
        return ""
    text = ref.strip()
    pairs = None
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            pairs = sorted((str(k), str(v)) for k, v in obj.items()) if isinstance(obj, dict) else None
        except ValueError:
            pairs = None
    elif re.fullmatch(r"(?:\w+\s*:\s*[^,]+)(?:\s*,\s*\w+\s*:\s*[^,]+)*", text):
        pairs = sorted((k.strip(), v.strip().strip("\"'")) for k, v in (part.split(":", 1) for part in text.split(",")))
    return "|".join(f"{k}={v}" for k, v in pairs) if pairs else text


def _canonical_refs(given, allowed: set) -> list:
    """The model's references mapped onto the offered ones they denote (2026-10-22, live run JOURNEY-V7P-J2: a correct suggestion was refused because the model copied the
    offered JSON locators without their braces and quotes). A reference that denotes no offered one stays as given, so the existing check still refuses it; what is
    stored is always the server's own form, never the model's rewrite."""
    by_key = {}
    for a in allowed:
        by_key.setdefault(_ref_key(a), a)
    out = []
    for r in given:
        r = r if not isinstance(r, str) else r.strip()
        out.append(by_key.get(_ref_key(r), r))
    return list(dict.fromkeys(out))


RAW_QUESTION_LIMIT = 2000                        # characters of an over-long question kept whole on the result (the gap shows the cut text)


def _question_text(text, limit: int) -> tuple:
    """(question, trimmed, reason): plain text within the limit -> (text, False, None); over-length plain text -> (cut text, True, "question_over_length"); unsafe or
    empty text -> (None, False, "question_not_plain_text"). The cut keeps what a person needs to decide: the head of the question and its LAST sentence — the one that
    asks (the decision clause usually comes last) — with an ellipsis between; the rules the question is about are named on the gap itself (`affected_rule_ids`,
    `options`), never only in the text. 2026-10-22 (closure C-06, live run JOURNEY-V7-J2)."""
    if not isinstance(text, str):
        return None, False, "question_not_plain_text"
    whole = safe_text(text, RAW_QUESTION_LIMIT)
    if whole is None:
        return None, False, "question_not_plain_text"
    if len(whole) <= limit:
        return whole, False, None
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", whole) if s]
    asking = max((i for i, s in enumerate(sentences) if s.endswith("?")), default=len(sentences) - 1)   # the sentence that asks, wherever it stands (the last one if none asks)
    if len(sentences) > 1 and len(sentences[asking]) + 4 <= limit:
        room = limit - len(sentences[asking]) - 2                               # the asking sentence stays whole; the others share what is left, in their order
        others = [s for i, s in enumerate(sentences) if i != asking]
        rest = " ".join(others)
        if len(rest) > room:
            rest = rest[:room]
            rest = (rest[: rest.rfind(" ")] if " " in rest else rest).rstrip() + "…"
        parts = [rest, sentences[asking]] if asking == len(sentences) - 1 else [sentences[asking], rest]
        return " ".join(p for p in parts if p), True, "question_over_length"
    return whole[: max(1, limit - 1)].rstrip() + "…", True, "question_over_length"


def resolve_proposals(obj: Mapping, rules: Mapping[str, Mapping], existing_gaps: Iterable[Mapping], existing_relations: Iterable[Mapping], *, limits: Mapping,
                      can_read_evidence: bool) -> dict:
    """What the server keeps. `rules` = the rules shown to the model (the only ids a proposal may name)."""
    asked = {(g["kind"], tuple(sorted(g["affected_rule_ids"]))) for g in existing_gaps}
    asked_questions = {_norm_question(g["question"]) for g in existing_gaps}
    known_edges = {(e["from"], e["to"], e["type"]) for e in existing_relations} | {(e["to"], e["from"], e["type"]) for e in existing_relations}
    gaps, relations, rejected, seen, trimmed = [], [], [], set(), []
    for g in obj["gaps"]:
        kind, rids = g["kind"], list(dict.fromkeys(g["rule_ids"]))

        def no(reason):
            rejected.append({"item": "gap", "reason": reason, "kind": kind[:40], "rule_ids": [r[:80] for r in rids][:4]})

        # a question longer than the limit is kept, cut to the limit and marked (the rule ids it names are structured on the gap already); unsafe text is still
        # refused (2026-10-22, live run JOURNEY-V7-J2: the model's correct CAPEX suggestion was refused as "not plain text" because its question, naming two
        # 45-character rule ids, overran the 300-character limit)
        question, question_trimmed, question_reason = _question_text(g["question"], limits["max_question_chars"])
        if kind not in PROPOSABLE_GAP_KINDS:
            no("kind_not_allowed")
        elif not rids or any(r not in rules for r in rids):
            no("invented_rule_id")
        elif (kind == "conflicting_facts") != (len(rids) == 2) or (kind == "missing_exception" and len(rids) != 1):
            no("rule_count_for_kind")
        elif question is None:
            no(question_reason)
        elif (kind, tuple(sorted(rids))) in asked or _norm_question(question) in asked_questions:
            no("already_asked")
        else:
            named = set().union(*(rule_refs(rules[r]) for r in rids)) if can_read_evidence else set(rids)
            # a missing exception may cite the shown rule that is in fact the unstated exception (contract section 20), as long as it also cites the named rule
            # (2026-10-24, live run PROPOSALS-V9-S3: two such correct suggestions were refused because the second reference belonged to the exception rule)
            allowed = named | (set().union(*(rule_refs(r) for r in rules.values())) if can_read_evidence else set(rules)) if kind == "missing_exception" else named
            refs = _canonical_refs(g["evidence_refs"], allowed)
            key = (kind, tuple(sorted(rids)))                                     # one question per pair and kind in a proposal: another wording of the same disagreement is a duplicate
            if not refs or len(refs) > limits["max_evidence_refs"]:
                no("evidence_refs_missing_or_too_many")
            elif not set(refs) <= allowed or not set(refs) & named:
                no("evidence_ref_not_in_the_named_rules")
            elif key in seen:
                no("duplicate")
            else:
                seen.add(key)
                gaps.append(proposed_gap(kind, rids, question, refs, rules))
                if question_trimmed:                                                  # the gap keeps its contract shape; the cut is disclosed on the result
                    full = safe_text(g["question"], RAW_QUESTION_LIMIT) or ""
                    trimmed.append({"kind": kind, "rule_ids": sorted(rids), "reason": question_reason, "shown_chars": len(question), "cut_chars": max(0, len(full) - len(question)),
                                    "full_question": full})
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
    return {"gaps": gaps, "relations": relations, "rejected": rejected, "notes": notes, "questions_trimmed": trimmed}


_AMOUNT_FIELD = re.compile(r"(amount|_jpy|price|value|total|cost)", re.I)
_CATEGORY_WORDS = {"capex": "CAPEX", "capital": "CAPEX", "opex": "OPEX", "operational": "OPEX", "emergency": "EMERGENCY"}
_ORDER = {"gt", "gte", "lt", "lte"}
MAX_PAIRS = 12


def _leaves(cond):
    if not isinstance(cond, Mapping):
        return []
    if "all" in cond or "any" in cond:
        return [x for c in cond.get("all") or cond.get("any") or [] for x in _leaves(c)]
    if "not" in cond:
        return []
    return [cond]


def _case_key(rule: Mapping):
    """What kind of case a rule decides, named the same way whatever the source called the fields: (category marker, 'amount') for a rule that compares a monetary
    amount; (category marker, measure, concepts) for a rule that compares another ordered quantity (days, hours, months, litres...: the unit, else the last word of
    the field) — such pairs are offered only inside a shared concept (CORE-V5-S2 P-07: two return windows in days were never offered); None otherwise. The category
    comes from a condition literal (CAPEX), the scope or the title words; absent = ''."""
    leaves = _leaves(rule.get("condition"))
    ordered = [l for l in leaves if l.get("op") in _ORDER and isinstance(l.get("field"), str) and _is_number(l.get("value"))]
    amount = next((l for l in ordered if _AMOUNT_FIELD.search(l["field"])), None)
    cat, _basis = _case_category(rule, leaves)
    if amount is not None:
        return (cat, "amount"), amount
    if not ordered:
        return None, None
    leaf = ordered[0]
    measure = str(leaf.get("unit") or "").strip().lower() or re.split(r"[._]", leaf["field"].lower())[-1]
    return (cat, measure, tuple(sorted(rule.get("concept_ids") or []))), leaf


def _is_number(v) -> bool:
    try:
        float(str(v).replace(",", ""))
        return not isinstance(v, bool)
    except ValueError:
        return False


def _case_category(rule: Mapping, leaves: list) -> tuple:
    """(category marker, basis) read from the rule's structured facts first — a condition literal (`request.category == CAPEX`), a scope value, a segment of the
    compared field's path (`capital_expenditure.amount`) — and only last from a title word, labelled as such (2026-10-22, closure C-02: the basis is recorded on
    the pair so the model and the record see on what grounds the pair is offered; candidate generation only, never a confirmation)."""
    sources = (("condition literal", " ".join(str(l.get("value")) for l in leaves if l.get("op") == "eq")),
               ("scope", json.dumps(rule.get("scope") or {})),
               ("condition field", " ".join(str(l.get("field") or "").replace("_", " ").replace(".", " ") for l in leaves)),
               ("title word", str(rule.get("title") or "")))
    for basis, text in sources:
        low = text.lower()
        cat = next((v for k, v in _CATEGORY_WORDS.items() if re.search(r"\b" + k, low)), "")
        if cat:
            return cat, basis
    return "", "none"


def candidate_pairs(rules: Iterable[Mapping]) -> list:
    """Pairs of ACTIVE rules that decide the same kind of case (same category marker, both on an amount, same direction) yet state a different threshold or a
    different actor. Candidate generation only: it decides no relation; the model (and then a person) reads the pair. Kept small and sorted."""
    items = []
    for r in rules:
        if r.get("status") in ("SUPERSEDED", "REJECTED"):
            continue
        key, leaf = _case_key(r)
        if key is None:
            continue
        items.append((key, leaf, r))
    items.sort(key=lambda x: x[2]["rule_id"])                                 # deterministic wording: the pair is described in rule-id order
    out = []
    for i, (k1, l1, r1) in enumerate(items):
        for k2, l2, r2 in items[i + 1:]:
            if k1 != k2 or l1.get("op") != l2.get("op"):
                continue
            if len(k1) == 3 and not k1[2]:                                      # a non-money measure needs a shared concept (else the pair is noise)
                continue
            same_value = str(l1.get("value")) == str(l2.get("value"))
            same_actor = (r1.get("actor_ref") or "") == (r2.get("actor_ref") or "")
            if same_value and same_actor:
                continue
            why = []
            if not same_value:
                why.append(f"thresholds {l1.get('value')} and {l2.get('value')}")
            if not same_actor:
                why.append(f"actors {r1.get('actor_ref') or 'unknown'} and {r2.get('actor_ref') or 'unknown'}")
            basis = sorted({_case_category(r, _leaves(r.get("condition")))[1] for r in (r1, r2)} - {"none"})
            out.append({"rule_ids": sorted([r1["rule_id"], r2["rule_id"]]), "case": k1[0] or "any category", "why": "; ".join(why), "case_from": basis,
                        "measure": k1[1]})
    out.sort(key=lambda x: (x["case"], x["rule_ids"]))
    return out[:MAX_PAIRS]


def build_prompt(frozen: Mapping, rules: Iterable[Mapping], asked: Iterable[str], edges: Iterable[str], *, can_read_evidence: bool, max_chars: int) -> str:
    return build_prompt_with_coverage(frozen, rules, asked, edges, can_read_evidence=can_read_evidence, max_chars=max_chars)[0]


def build_prompt_with_coverage(frozen: Mapping, rules: Iterable[Mapping], asked: Iterable[str], edges: Iterable[str], *, can_read_evidence: bool, max_chars: int) -> tuple:
    """(prompt, coverage): what the prompt holds of what the snapshot holds — rules, candidate pairs, open questions and relations shown / total, the body's
    character budget and length. A section cut short is marked in the prompt and counted here; the result that stores it never presents a cut context as complete."""
    rules = list(rules)
    asked, edges = list(asked), list(edges)
    all_pairs = candidate_pairs(rules)
    pairs = [(f"- {p['rule_ids'][0]} and {p['rule_ids'][1]}: both decide a {p['case']} purchase by its amount but state different {p['why']}" if p.get("measure", "amount") == "amount"
              else f"- {p['rule_ids'][0]} and {p['rule_ids'][1]}: both decide the same kind of case by its {p['measure']} but state different {p['why']}")
             + (f" (case read from: {', '.join(p['case_from'])})" if p.get("case_from") else "") for p in all_pairs]
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
    # one budget for the whole prompt body (2026-10-22, live run JOURNEY-V6-J2: 34 rules at the cap plus 22 open questions and 52 relation lines were refused by
    # the input ceiling before any call): the rules come first, then the candidate pairs, the open questions and the relations, each cut with a note when the
    # remaining budget is used up, so the admission bound stays within the ceiling the configuration promises
    def fit(items: list) -> list:
        nonlocal used
        kept = []
        for s in items:
            if used + len(s) > max_chars:
                kept.append("- (more not shown)")
                break
            kept.append(s)
            used += len(s)
        return kept
    shown_pairs, shown_asked, shown_edges = fit(pairs), fit(asked), fit(edges)
    prompt = (frozen["prompt_template"].replace("{rules}", "\n".join(lines) or "(none)").replace("{asked}", "\n".join(shown_asked) or "(none)")
              .replace("{edges}", "\n".join(shown_edges) or "(none)").replace("{pairs}", "\n".join(shown_pairs) or "(none)"))
    count = lambda shown: sum(1 for s in shown if s != "- (more not shown)")                                   # noqa: E731
    coverage = {"rules": {"shown": len(lines), "total": len(rules)}, "pairs": {"shown": count(shown_pairs), "total": len(all_pairs)},
                "questions": {"shown": count(shown_asked), "total": len(asked)}, "relations": {"shown": count(shown_edges), "total": len(edges)},
                "budget_chars": max_chars, "body_chars": used, "prompt_chars": len(prompt)}
    coverage["complete"] = all(v["shown"] == v["total"] for v in (coverage["rules"], coverage["pairs"], coverage["questions"], coverage["relations"]))
    return prompt, coverage
