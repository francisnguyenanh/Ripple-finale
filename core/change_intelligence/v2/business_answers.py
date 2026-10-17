# -*- coding: utf-8 -*-
"""A user's answer to a gap, as a plan of exactly what a confirmation would change (pure, no I/O). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md.

The same plan is the preview shown to the person and the change applied on confirmation; the confirmation carries the hash of the preview, so what is confirmed
is what was shown. An answer never edits an OBSERVED rule: it creates or updates a TARGET rule backed by the human decision. Nothing here guesses.
"""

from __future__ import annotations

import copy
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.business_graph import derive_gaps
from core.change_intelligence.v2.serialize import canonical_hash

PREVIEW_DECISION_ID = "decision-preview"
ANSWER_KEYS = {"missing_actor": "actor", "missing_condition": "condition", "missing_outcome": "outcome", "conflicting_facts": "choose_rule_id", "missing_exception": "exception"}


def target_id(rule_id: str) -> str:
    return rule_id if rule_id.endswith("-target") else rule_id + "-target"


def decision_id_for(gap_id: str, normalized: Mapping, quote: str) -> str:
    return "decision-" + canonical_hash({"gap": gap_id, "answer": normalized, "quote": quote.strip()})[:16]


def normalize_answer(gap: Mapping, answer: Mapping) -> dict:
    """The answer in its stored form, or a ContractError. Exactly one of the gap's answer key or `accept_unknown`."""
    if not isinstance(answer, Mapping):
        raise bk.ContractError("invalid_field", "answer must be an object")
    if answer.get("accept_unknown") is True:
        if set(answer) - {"accept_unknown", "reason"}:
            raise bk.ContractError("invalid_field", "accept_unknown takes only a reason")
        reason = answer.get("reason")
        if gap.get("priority") == "critical" and not (isinstance(reason, str) and reason.strip()):
            raise bk.ContractError("invalid_field", "silent accept_unknown: a critical gap needs a stated reason")
        return {"accept_unknown": True, "reason": (reason or "").strip()}
    key = ANSWER_KEYS[gap["kind"]] if gap["kind"] in ANSWER_KEYS else None
    if key is None:
        raise bk.ContractError("unsupported_operation", f"gaps of kind {gap['kind']} cannot be answered here")
    if set(answer) != {key}:
        raise bk.ContractError("invalid_field", f"a {gap['kind']} gap is answered with exactly '{key}' (or accept_unknown)")
    value = answer[key]
    if key == "actor":
        slug = bk._slug(value) if isinstance(value, str) else ""
        if not slug:
            raise bk.ContractError("invalid_field", "actor must be a plain name")
        return {"actor": "role-" + slug}
    if key == "outcome":
        slug = bk._slug(value) if isinstance(value, str) else ""
        if not slug:
            raise bk.ContractError("invalid_field", "outcome must be a plain name")
        return {"outcome": slug.replace("-", "_")}
    if key == "condition":
        bk.validate_condition(value)
        return {"condition": value}
    if key == "exception":
        text = " ".join(value.split()) if isinstance(value, str) else ""
        if not text or len(text) > 300 or "<" in text or "http" in text.lower():
            raise bk.ContractError("invalid_field", "exception must be a short plain sentence")
        return {"exception": text}
    if value not in gap["affected_rule_ids"]:
        raise bk.ContractError("invented_rule_id", f"{value!r} is not one of the rules in this conflict")
    return {"choose_rule_id": value}


def _describe(rule: Mapping) -> str:
    return f"\"{rule['title']}\""


def plan_answer(base_rules: Iterable[Mapping], base_gaps: Iterable[Mapping], gap_id: str, answer: Mapping, *, decision_id: str,
                prior_answers: Iterable[Mapping] = ()) -> dict:
    """What confirming `answer` for `gap_id` does. `prior_answers` are the earlier recorded normalized answers to this same gap (body of the decisions)."""
    rules = {r["rule_id"]: r for r in base_rules}
    gaps = {g["gap_id"]: g for g in base_gaps}
    gap = gaps.get(gap_id)
    if gap is None:
        raise bk.ContractError("invalid_field", f"unknown gap {gap_id!r}")
    normalized = normalize_answer(gap, answer)
    prior = [dict(p) for p in prior_answers]
    if gap["status"] in ("RESOLVED", "ACCEPTED_UNKNOWN") and prior:
        if prior[-1] == normalized:
            return {"noop": True, "normalized": normalized, "summary": ["This is the answer already recorded for this question; nothing changes."]}
    plan = {"noop": False, "normalized": normalized, "gap_id": gap_id, "decision_id": decision_id, "target_rule": None, "gap_updates": {gap_id: "RESOLVED"},
            "new_gaps": [], "summary": []}
    if normalized.get("accept_unknown"):
        plan["gap_updates"] = {gap_id: "ACCEPTED_UNKNOWN"}
        plan["summary"].append("The question stays unanswered by choice" + (f": {normalized['reason']}" if normalized["reason"] else "") +
                               ". It remains visible as an accepted unknown.")
        return plan
    if gap["status"] in ("RESOLVED", "ACCEPTED_UNKNOWN") and prior and prior[-1] != normalized:
        # a different answer to a question that was already answered: nothing is overwritten; both answers go to a person as a new question
        rids = sorted(gap["affected_rule_ids"])
        new_gap = {"gap_id": bk.gap_id_for("conflicting_facts", rids, "answer:" + canonical_hash({"was": prior[-1], "now": normalized})[:12]),
                   "kind": "conflicting_facts", "status": "OPEN", "affected_rule_ids": rids,
                   "question": f"This question was answered differently twice ({_flat(prior[-1])} and {_flat(normalized)}). Which answer is the business rule?",
                   "evidence_refs": [decision_id], "requires_permission": "knowledge.confirm", "priority": "critical",
                   "options": [{"label": _flat(prior[-1])}, {"label": _flat(normalized)}]}
        plan["gap_updates"] = {gap_id: "REOPENED"}
        plan["new_gaps"] = [new_gap]
        plan["summary"].append("This question already has a different recorded answer. Nothing is overwritten: the question is reopened and a new question asks which answer is right.")
        return plan
    base_id = normalized["choose_rule_id"] if "choose_rule_id" in normalized else sorted(gap["affected_rule_ids"])[0]
    base = rules.get(base_id)
    if base is None:
        raise bk.ContractError("invented_rule_id", f"{base_id!r} is not a rule of this snapshot")
    tid = target_id(base_id)
    target = copy.deepcopy(rules.get(tid) or base)
    target.update(rule_id=tid, authority="TARGET", status="CONFIRMED", implementation_status="pending")
    target["provenance"] = [p for p in target.get("provenance", []) if p.get("kind") == "human_decision"] + [{"kind": "human_decision", "decision_id": decision_id}]
    unknown = [u for u in target.get("unknown_fields", [])]
    if "actor" in normalized:
        target["actor_ref"] = normalized["actor"]
        unknown = [u for u in unknown if u != "actor_ref"]
        plan["summary"].append(f"{_describe(base)}: the person who acts becomes {normalized['actor']}.")
    elif "condition" in normalized:
        target["condition"] = normalized["condition"]
        unknown = [u for u in unknown if u != "condition"]
        plan["summary"].append(f"{_describe(base)}: it applies when the stated condition holds.")
    elif "outcome" in normalized:
        target["outcome"] = dict(target["outcome"], kind=normalized["outcome"])
        plan["summary"].append(f"{_describe(base)}: when it applies, the result is {normalized['outcome']}.")
    elif "exception" in normalized:
        existing = [e for e in target.get("exceptions", [])]
        if any(" ".join(e["text"].split()).lower() == normalized["exception"].lower() for e in existing):
            return {"noop": True, "normalized": normalized, "summary": ["This exception is already stated for this rule; nothing changes."]}
        target["exceptions"] = existing + [{"exception_id": f"exc-{len(existing) + 1}-" + decision_id[-8:], "text": normalized["exception"], "condition": None}]
        plan["summary"].append(f"{_describe(base)}: an exception is added: {normalized['exception']}")
    else:
        plan["summary"].append(f"The business rule is {_describe(base)} as the source states it. The other statement stays what the source says and is marked as a difference to fix in the files.")
    target["unknown_fields"] = unknown
    bk.validate_business_rule(target)
    plan["target_rule"] = target
    plan["summary"].append("The source files are not changed; the code and files still show what they showed.")
    return plan


def _flat(normalized: Mapping) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(normalized.items()))


def preview_hash(base_content_hash: str, plan: Mapping) -> str:
    return canonical_hash({"base": base_content_hash, "gap_id": plan.get("gap_id"), "normalized": plan["normalized"], "noop": plan["noop"],
                           "target": plan.get("target_rule"), "gap_updates": plan.get("gap_updates"), "new_gaps": plan.get("new_gaps")})


def apply_plan(base_gaps: Iterable[Mapping], plan: Mapping) -> list:
    out = []
    for g in base_gaps:
        status = plan["gap_updates"].get(g["gap_id"])
        out.append(dict(g, status=status) if status else dict(g))
    return out + [dict(g) for g in plan["new_gaps"]]
