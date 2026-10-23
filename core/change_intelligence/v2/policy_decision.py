# -*- coding: utf-8 -*-
"""Typed policy decision over the accepted business knowledge (MON-POL, pure, no I/O, no model).

A caller states typed facts (`{"request.net_amount_jpy": 1200000, "request.category": "CAPEX", "process": "STANDARD"}`). Every live rule of one accepted snapshot whose scope
matches is evaluated with the Final condition grammar. The decision is one of `ALLOW | DENY | REQUIRES_APPROVAL | INDETERMINATE` and is **conservative**: a missing fact, a rule
without a condition, an exception the grammar cannot evaluate, an unclassified outcome kind or an open question on an applicable rule makes it INDETERMINATE and says why. A
TARGET rule (decided by people) replaces the OBSERVED rule of the same root. Nothing is inferred from retrieval ranking or a model; the deterministic evaluator decides.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.serialize import canonical_hash

DECISIONS = ("ALLOW", "DENY", "REQUIRES_APPROVAL", "INDETERMINATE")
# how an outcome kind of a triggered rule counts; a kind outside these lists is UNCLASSIFIED and makes the decision indeterminate
DENY_WORDS = ("rejected", "refused", "blocked", "denied", "forbidden")
APPROVAL_WORDS = ("review", "approval", "required", "onboarding", "audit", "needs_")   # "needs_": a code route value such as RouteType.NEEDS_MANAGER (C-11, 2026-10-22)
EFFECT_WORDS = ("free_", "charge", "allowed", "accepted", "due", "applies")
MAX_FACTS = 50


def classify_outcome(kind: Optional[str]) -> str:
    k = (kind or "").lower()
    if not k or k == "unknown" or "_if_" in k:                      # "rejected_if_invalid" names a condition, not the effect of the rule as stored: ambiguous
        return "UNCLASSIFIED"
    if any(w in k for w in DENY_WORDS):
        return "DENY"
    if any(w in k for w in APPROVAL_WORDS):
        return "REQUIRES_APPROVAL"
    if any(w in k for w in EFFECT_WORDS):
        return "EFFECT"
    return "UNCLASSIFIED"


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        try:
            return Decimal(v.replace(",", ""))
        except InvalidOperation:
            return None
    return None


def _compare(op: str, left, right) -> Optional[bool]:
    if op in ("eq", "ne"):
        if isinstance(left, bool) or isinstance(right, bool) or (_num(left) is None) != (_num(right) is None):
            same = str(left).strip().lower() == str(right).strip().lower()
        else:
            same = (_num(left) == _num(right)) if _num(left) is not None else left == right
        return same if op == "eq" else not same
    l, r = _num(left), _num(right)
    if l is None or r is None:
        return None
    return {"gt": l > r, "gte": l >= r, "lt": l < r, "lte": l <= r}[op]


def evaluate_condition(expr, facts: Mapping) -> tuple[Optional[bool], list]:
    """(True | False | None, missing/unevaluable reasons). None = cannot be decided from these facts."""
    if expr is None:
        return None, ["rule_condition_unknown"]
    if "all" in expr or "any" in expr:
        key = "all" if "all" in expr else "any"
        results, reasons = [], []
        for child in expr[key]:
            r, why = evaluate_condition(child, facts)
            results.append(r)
            reasons += why
        if key == "all":
            if any(r is False for r in results):
                return False, []                                  # one false child decides, whatever the others
            return (True, []) if all(r is True for r in results) else (None, reasons)
        if any(r is True for r in results):
            return True, []
        return (False, []) if all(r is False for r in results) else (None, reasons)
    if "not" in expr:
        r, why = evaluate_condition(expr["not"], facts)
        return (None, why) if r is None else (not r, [])
    field = expr["field"]
    if field not in facts:
        return None, [f"fact_missing:{field}"]
    value, op = facts[field], expr["op"]
    if op in ("in", "not_in"):
        members = [str(m).strip().lower() for m in expr["value"]]
        hit = str(value).strip().lower() in members or (_num(value) is not None and any(_num(m) == _num(value) for m in expr["value"]))
        return (hit if op == "in" else not hit), []
    r = _compare(op, value, expr["value"])
    return (r, []) if r is not None else (None, [f"fact_not_comparable:{field}"])


def _scope_applies(rule: Mapping, facts: Mapping) -> tuple[Optional[bool], list]:
    scope = rule.get("scope") or {}
    reasons = []
    for key, allowed in scope.items():
        if not isinstance(allowed, list) or not allowed:
            continue
        if key not in facts:
            reasons.append(f"scope_fact_missing:{key}")
            continue
        if str(facts[key]).strip().lower() not in {str(a).strip().lower() for a in allowed}:
            return False, []
    return (None, reasons) if reasons else (True, [])


def effective_rules(rules: Iterable[Mapping]) -> tuple[list, list]:
    """Live rules with a TARGET replacing the OBSERVED rule of the same root; (effective, superseded_observed_ids)."""
    live = [r for r in rules if r.get("status") not in ("SUPERSEDED", "REJECTED")]
    targets = {r["rule_id"][: -len("-target")] for r in live if r["rule_id"].endswith("-target")}
    superseded = sorted(r["rule_id"] for r in live if r["rule_id"] in targets)
    return [r for r in live if r["rule_id"] not in targets], superseded


def decide(rules: Iterable[Mapping], gaps: Iterable[Mapping], facts: Mapping, *, pin: Mapping) -> dict:
    """The typed decision for `facts` against one accepted snapshot's rules and open questions."""
    if not isinstance(facts, Mapping) or not facts or len(facts) > MAX_FACTS or not all(isinstance(k, str) and k for k in facts):
        raise ValueError("facts must be a non-empty object of at most 50 named values")
    eff, superseded = effective_rules(rules)
    open_by_rule: dict = {}
    for g in gaps:
        if g.get("status") in ("OPEN", "REOPENED"):
            for rid in g.get("affected_rule_ids", []):
                open_by_rule.setdefault(rid, []).append(g["gap_id"])
    applicable, reasons, triggered = [], [], []
    for r in sorted(eff, key=lambda x: x["rule_id"]):
        scoped, why = _scope_applies(r, facts)
        if scoped is False:
            continue
        row = {"rule_id": r["rule_id"], "title": r["title"], "authority": r["authority"], "outcome_kind": (r.get("outcome") or {}).get("kind"), "actor_ref": r.get("actor_ref"),
               "condition": None, "exceptions": "none", "open_questions": open_by_rule.get(r["rule_id"], []), "reasons": list(why)}
        held, cwhy = evaluate_condition(r.get("condition"), facts)
        row["condition"] = {True: "holds", False: "does_not_hold", None: "undecidable"}[held]
        row["reasons"] += cwhy
        exc_block = None
        for e in r.get("exceptions") or []:
            if e.get("condition") is None:
                row["exceptions"] = "unevaluable"
                row["reasons"].append("exception_without_condition:" + str(e.get("text"))[:80])
            else:
                eh, ewhy = evaluate_condition(e["condition"], facts)
                if eh is True:
                    exc_block = e.get("text")
                elif eh is None:
                    row["exceptions"] = "unevaluable"
                    row["reasons"] += ewhy
        if row["open_questions"]:
            row["reasons"].append("open_question")
        if r.get("unknown_fields"):
            row["reasons"].append("rule_fields_unknown:" + ",".join(r["unknown_fields"]))
        row["outcome_class"] = classify_outcome(row["outcome_kind"])
        if held is True and exc_block is None and row["outcome_class"] == "UNCLASSIFIED":
            row["reasons"].append("outcome_kind_unclassified")
        row["triggered"] = bool(held is True and exc_block is None and not row["reasons"])
        if exc_block:
            row["exception_applied"] = exc_block
        applicable.append(row)
        if row["reasons"]:
            reasons += [f"{r['rule_id']}: {x}" for x in row["reasons"]]
        elif row["triggered"]:
            triggered.append(row)
    obligations = [{"kind": "approval", "by": t["actor_ref"], "rule_id": t["rule_id"], "title": t["title"]} for t in triggered if t["outcome_class"] == "REQUIRES_APPROVAL"]
    effects = [{"kind": t["outcome_kind"], "rule_id": t["rule_id"], "title": t["title"]} for t in triggered if t["outcome_class"] == "EFFECT"]
    if reasons:
        decision = "INDETERMINATE"
    elif any(t["outcome_class"] == "DENY" for t in triggered):
        decision = "DENY"
    elif obligations:
        decision = "REQUIRES_APPROVAL"
    else:
        decision = "ALLOW"
    out = {"schema": "ripple-policy-decision/1", "decision": decision, "obligations": obligations, "effects": effects, "applicable_rules": applicable,
           "superseded_observed_rules": superseded, "indeterminate_reasons": reasons, "facts_digest": canonical_hash(dict(facts)), "pin": dict(pin),
           "statement": {"ALLOW": "No evaluated rule of the accepted knowledge requires an approval or forbids this; rules the facts do not reach were not evaluated.",
                         "DENY": "An evaluated rule of the accepted knowledge forbids this.",
                         "REQUIRES_APPROVAL": "An evaluated rule requires the listed approval before this action.",
                         "INDETERMINATE": "The accepted knowledge cannot decide this from the given facts; treat it as not allowed until a person decides."}[decision],
           "binding_note": "Bound to this knowledge version and these facts. The caller must re-check the version at the moment it acts; an old decision is not an authorization."}
    out["decision_hash"] = canonical_hash({k: out[k] for k in ("decision", "obligations", "effects", "indeterminate_reasons", "facts_digest", "pin")})
    return out
