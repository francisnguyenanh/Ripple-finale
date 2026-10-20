# -*- coding: utf-8 -*-
"""Understanding a business change request (pure, no I/O). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md (change_operation, change_set).

A model proposes operations; the server decides which of them stand. The person's own words are the only source of what changes: every operation quotes the request verbatim,
every number and every scope value must appear in the request (or already be in the rule), rule ids must belong to the pinned snapshot's offered rules, and `before` always comes
from the stored rule, never from the model. A part of the request that no operation accounts for is reported, never dropped. Nothing is READY while any part is unclear, refused or
unsupported, and nothing here writes knowledge: a confirmed intent is a request, not a decision about the rule.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.serialize import canonical_hash

STATUSES = ("READY", "CLARIFY", "UNSUPPORTED")
MODEL_KEYS = {"status", "message", "operations", "questions", "assumptions"}
OP_KEYS = {"kind", "rule_id", "candidate_rule_ids", "desired", "user_quote"}
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_SENTENCE = re.compile(r"[.;!?\n]+|\band\b|(?<!\d),|,(?!\d)|\bthen\b|\balso\b", re.I)
_UNSAFE = re.compile(r"<[^>]*>|\]\(|https?://|www\.|`|javascript:|\x00", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def numbers_in(value) -> set:
    out = set()
    for m in _NUM.findall(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def safe_text(text, limit) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t if t and len(t) <= limit and not _UNSAFE.search(t) else None


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != MODEL_KEYS:
        raise ValueError("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["message"], str):
        raise ValueError("status_or_message")
    if not isinstance(obj["operations"], list) or not isinstance(obj["questions"], list) or not isinstance(obj["assumptions"], list):
        raise ValueError("lists")
    if len(obj["operations"]) > limits["max_model_operations"] or len(obj["questions"]) > limits["max_questions"] or len(obj["assumptions"]) > limits["max_assumptions"]:
        raise ValueError("sizes")
    if not all(isinstance(x, str) for x in obj["questions"] + obj["assumptions"]):
        raise ValueError("text_lists")
    for op in obj["operations"]:
        if not isinstance(op, dict) or set(op) != OP_KEYS or not isinstance(op["kind"], str) or not isinstance(op["user_quote"], str):
            raise ValueError("operation_shape")
        if op["rule_id"] is not None and not isinstance(op["rule_id"], str):
            raise ValueError("operation_rule_id")
        if not isinstance(op["candidate_rule_ids"], list) or not all(isinstance(x, str) for x in op["candidate_rule_ids"]):
            raise ValueError("operation_candidates")
    return obj


def build_prompt(frozen: Mapping, request: str, rules: Iterable[Mapping], *, max_chars: int) -> str:
    lines, used = [], 0
    for r in rules:
        line = f"- id={r['item_id']} authority={r.get('authority')} status={r.get('status')}\n  text: {r['text']}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return frozen["prompt_template"].replace("{request}", request).replace("{rules}", "\n".join(lines) or "(none)")


# ---- resolving the proposal against the stored snapshot ------------------------------------------------------------------------------------

def _numeric_leaves(cond) -> list:
    if cond is None:
        return []
    if "field" in cond:
        return [cond] if cond["op"] in ("gt", "gte", "lt", "lte", "eq", "ne") and _is_number(cond["value"]) else []
    out = []
    for key in ("all", "any"):
        for c in cond.get(key, []):
            out += _numeric_leaves(c)
    if "not" in cond:
        out += _numeric_leaves(cond["not"])
    return out


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(str(v).replace(",", ""))
        return isinstance(v, str)
    except ValueError:
        return False


def _role(text: str) -> Optional[str]:
    t = str(text).strip()
    slug = bk._slug(t[5:] if t.startswith("role-") else t)
    return "role-" + slug if slug else None


def _segments(request: str) -> list:
    return [s.strip() for s in _SENTENCE.split(request) if s and len(re.findall(r"[a-z]{2,}", s, re.I)) >= 3]


def _covered(segment: str, quotes: list) -> bool:
    seg = _norm(segment)
    seg_tokens = set(re.findall(r"[a-z0-9]+", seg))
    union = set()
    for q in quotes:
        qn = _norm(q)
        if qn in seg or seg in qn:
            return True
        union |= set(re.findall(r"[a-z0-9]+", qn))
    return bool(seg_tokens) and len(seg_tokens & union) / len(seg_tokens) >= 0.6


def describe(op: Mapping, title: str) -> str:
    k, d = op["kind"], op["desired"]
    if k == "set_value":
        val = d if not isinstance(d, Mapping) else f"{d['value']} {d['unit']}"
        return f"Change the value of “{title}” to {val}."
    if k == "set_actor":
        return f"Make {d.replace('role-', '').replace('-', ' ')} the person who acts on “{title}”."
    if k == "add_or_change_condition":
        from core.change_intelligence.v2.business_index import condition_text
        return f"Change when “{title}” applies to: {condition_text(d)}."
    if k == "set_exception":
        return f"Set the exceptions of “{title}” to: " + ("; ".join(e["text"] for e in d) if d else "none") + "."
    return f"Change where “{title}” applies to: " + "; ".join(f"{a}: {', '.join(v) if isinstance(v, list) else v}" for a, v in sorted(d.items())) + "."


DECIDED_CONFLICT_INTENTS = ("fix_observed_record", "sync_source_to_decision", "change_decision")


def decided_conflict_options(rule: Mapping, decided: Mapping) -> list:
    """The three things an edit of the losing side of a decided conflict can mean (owner decision H-U-03, 2026-10-18), in plain words, with what happens next.
    Only `change_decision` is handled by this product's change flow; the other two are honest hand-offs to a person."""
    return [
        {"intent": "fix_observed_record", "label": f"The record of what the sources say is wrong (“{rule['title']}” was read incorrectly)",
         "next": "A person corrects the source document (or reports the misreading); a source refresh then re-reads the record.", "available": False,
         "reason": "needs_human: an observed record always follows the sources; this product does not edit it directly."},
        {"intent": "sync_source_to_decision", "label": f"Bring the source text in line with the decided rule (“{decided['title']}”)",
         "next": "A person edits the source text to say what was decided, then refreshes the sources; the record then shows as aligned.", "available": False,
         "reason": "needs_human: this decision came from resolving a conflict, not from a change request, so there is no file proposal to review for it yet."},
        {"intent": "change_decision", "label": f"Propose changing the decided rule itself (“{decided['title']}”)",
         "next": "A new change request on the decided rule: impact, draft, review and approval as for any change; the earlier decision stays in force until then.",
         "available": True, "reason": None},
    ]


def resolve(obj: Mapping, request: str, rules: Mapping[str, Mapping], relations: Iterable[Mapping], limits: Mapping, *, pin: Mapping,
            choices: Optional[Mapping] = None) -> dict:
    """The server's understanding. `rules` = offered stored rules by id (the only ids an operation may name). `choices` = {observed rule id: intent} the person picked
    for an edit of the losing side of a decided conflict (one of DECIDED_CONFLICT_INTENTS); nothing is retargeted without it."""
    req_norm = _norm(request)
    req_numbers = numbers_in(request)
    questions, refused, rejected, noops, ops = [], [], [], [], []
    quotes, conflict_choices, retargeted = [], [], []
    if len(obj["operations"]) > bk.MAX_OPERATIONS_PER_REQUEST:
        questions.append(f"The request holds more than {bk.MAX_OPERATIONS_PER_REQUEST} changes. Please split it into several smaller changes.")
        obj = dict(obj, operations=[])
        rejected.append({"reason": "too_many_operations"})
    for raw in obj["operations"]:
        quote = safe_text(raw["user_quote"], 400)
        if quote is None or _norm(quote) not in req_norm:
            rejected.append({"reason": "quote_not_in_request", "kind": raw["kind"]})
            continue
        quotes.append(quote)
        if raw["kind"] in bk.UNSUPPORTED_OPERATION_KINDS or raw["kind"] not in bk.OPERATION_KINDS:
            refused.append({"quote": quote, "reason": "unsupported_operation", "why": "Changes that take effect on a date, or any other kind of change this product does not handle yet, are not supported."})
            continue
        rid = raw["rule_id"]
        if rid is None:
            cands = [c for c in raw["candidate_rule_ids"] if c in rules]
            if len(cands) >= 2:
                questions.append(f"Which rule do you mean by “{quote}”: " + " or ".join(f"“{rules[c]['title']}”" for c in cands[:4]) + "?")
            else:
                questions.append(f"I could not tell which rule “{quote}” is about. Please name the rule.")
            continue
        if rid not in rules:
            rejected.append({"reason": "invented_rule_id", "kind": raw["kind"]})
            questions.append(f"I could not match “{quote}” to a known rule. Please name the rule.")
            continue
        rule, kind, desired = rules[rid], raw["kind"], raw["desired"]
        decided = rid + "-target"
        if not rid.endswith("-target") and decided in rules:
            # The sources state one thing, a person decided another: the request must say which one it changes. Nothing is remapped silently; the preview shows the question.
            questions.append(f"“{quote}”: “{rule['title']}” has a decided version. Do you mean the rule as people decided it (“{rules[decided]['title']}”) or the rule as the "
                             "sources state it? Please name the one to change.")
            rejected.append({"reason": "observed_rule_has_decided_version", "kind": kind, "rule_id": rid, "candidate_rule_ids": [rid, decided]})
            continue
        winners = sorted({(e["to"] if e["from"] == rid else e["from"]) for e in relations
                          if e.get("type") == "conflicts_with" and e.get("status") != "REJECTED" and rid in (e.get("from"), e.get("to"))})
        winners = [w + "-target" if w + "-target" in rules else w for w in winners if w + "-target" in rules or (w.endswith("-target") and w in rules)]
        if not rid.endswith("-target") and winners:
            # A person decided this conflict for the other side. Editing the losing observed rule is not a reversal of that decision: the person says which change is meant;
            # nothing is remapped and no authority changes silently.
            chosen = (choices or {}).get(rid)
            options = decided_conflict_options(rule, rules[winners[0]])
            if chosen == "change_decision":
                retargeted.append({"from": rid, "to": winners[0], "intent": chosen})
                rid, rule = winners[0], rules[winners[0]]                     # the person chose it explicitly: a new change on the decided rule (impact, draft, approval follow)
            elif chosen in DECIDED_CONFLICT_INTENTS:
                opt = next(o for o in options if o["intent"] == chosen)
                refused.append({"quote": quote, "reason": opt["reason"].split(":", 1)[0], "intent": chosen, "why": f"{opt['label']}: {opt['next']} ({opt['reason'].split(': ', 1)[1]})"})
                continue
            else:
                questions.append(f"“{quote}”: “{rule['title']}” is the side of a conflict a person already decided against (decided: “{rules[winners[0]]['title']}”). "
                                 "Which do you mean: the record of what the sources say is wrong, the source text should follow the decision, or you want to change the decision itself?")
                rejected.append({"reason": "observed_rule_lost_a_decided_conflict", "kind": kind, "rule_id": rid, "candidate_rule_ids": [rid, winners[0]]})
                conflict_choices.append({"rule_id": rid, "decided_rule_id": winners[0], "quote": quote, "options": options})
                continue
        try:
            before, desired = _check_desired(kind, desired, rule, req_numbers, req_norm)
        except ValueError as exc:
            rejected.append({"reason": f"desired_not_supported:{exc}", "kind": kind, "rule_id": rid})
            questions.append(f"I could not safely read what should change for “{rule['title']}” (“{quote}”). Please say it again with the exact new value.")
            continue
        if before is None:
            questions.append(f"“{rule['title']}” has more than one value or none: which one do you want to change?")
            continue
        if _same(kind, before, desired):
            noops.append({"rule_id": rid, "kind": kind, "reason": "already_the_case"})
            continue
        op = {"op_id": f"op-{len(ops) + 1}", "kind": kind, "rule_id": rid, "before": before, "desired": desired, "user_quote": quote,
              "evidence_refs": sorted({b["evidence_ref"] for b in rule["source_bindings"]}), "status": "proposed"}
        bk.validate_change_operation(op, known_rule_ids=set(rules))
        ops.append(op)
    uncovered = [s for s in _segments(request) if not _covered(s, quotes)]
    for s in uncovered:
        questions.append(f"I could not turn this part into a change: “{s}”. What should change?")
    for q in obj["questions"]:
        t = safe_text(q, 300)
        if t:
            questions.append(t)
    questions = list(dict.fromkeys(questions))[: limits["max_questions"] + 6]
    write_scope = sorted({o["rule_id"] for o in ops})
    related = set()
    ws_set = set(write_scope)
    for e in relations:
        if e["type"] == "conflicts_with" and e["status"] != "REJECTED":
            if e["from"] in ws_set:
                related.add(e["to"])
            if e["to"] in ws_set:
                related.add(e["from"])
    concepts = {c for rid in write_scope for c in rules[rid]["concept_ids"]}
    related |= {rid for rid, r in rules.items() if set(r["concept_ids"]) & concepts}
    read_scope = sorted((related | ws_set) & set(rules))
    status = "READY" if (ops and not questions and not refused and not rejected and not uncovered and obj["status"] == "READY") else ("UNSUPPORTED" if (refused and not ops and not questions) else "CLARIFY")
    if not ops and not refused and not questions and noops:
        status = "CLARIFY"
        questions.append("Everything you asked for is already the case. Is there something else to change?")
    message = safe_text(obj["message"], limits["max_message_chars"]) or {"READY": "Please check that this is what you mean.", "CLARIFY": "I need a little more to understand this request.", "UNSUPPORTED": "This request cannot be handled."}[status]
    titles = {rid: rules[rid]["title"] for rid in rules}
    result = {"status": status, "message": message, "operations": ops, "descriptions": [describe(o, titles[o["rule_id"]]) for o in ops], "questions": questions, "refused": refused,
              "rejected": rejected, "noops": noops, "uncovered_parts": uncovered, "assumptions": [a for a in (safe_text(x, 300) for x in obj["assumptions"]) if a][: limits["max_assumptions"]],
              "write_scope": write_scope, "read_scope": read_scope, "request": request.strip(), "pin": dict(pin),
              "decided_conflict_choices": conflict_choices, "retargeted": retargeted}
    result["preview_hash"] = canonical_hash({"pin": result["pin"], "request": _norm(request), "operations": ops, "write_scope": write_scope, "status": status})
    return result


def _same(kind, before, desired) -> bool:
    if kind == "add_or_change_condition":
        return before is not None and bk.normalize_condition(before) == bk.normalize_condition(desired)
    if kind == "set_value" and isinstance(before, Mapping) and isinstance(desired, Mapping):
        return str(before["value"]) == str(desired["value"]) and before.get("unit") == desired.get("unit")
    return before == desired


def _check_desired(kind, desired, rule, req_numbers, req_norm):
    """-> (before from the STORED rule, normalized desired). Raises ValueError(reason) when the model's `desired` is not supported by the request."""
    if kind == "set_value":
        leaves = _numeric_leaves(rule["condition"])
        if len(leaves) != 1:
            return None, desired
        leaf = leaves[0]
        value = desired.get("value") if isinstance(desired, Mapping) else desired
        unit = (desired.get("unit") if isinstance(desired, Mapping) else None) or leaf.get("unit")
        if isinstance(value, bool) or not _is_number(value) or float(str(value).replace(",", "")) not in req_numbers:
            raise ValueError("number_not_in_request")
        if leaf.get("unit") and unit != leaf.get("unit"):
            raise ValueError("unit_differs_from_the_rule")
        return {"value": leaf["value"], **({"unit": leaf["unit"]} if leaf.get("unit") else {})}, {"value": value if isinstance(value, int) else str(value).replace(",", ""), **({"unit": unit} if unit else {})}
    if kind == "set_actor":
        role = _role(desired) if isinstance(desired, str) else None
        if role is None or role.replace("role-", "").replace("-", " ") not in req_norm.replace("-", " "):
            raise ValueError("actor_not_in_request")
        return rule["actor_ref"], role
    if kind == "add_or_change_condition":
        bk.validate_condition(desired)
        if numbers_in(desired) - (req_numbers | numbers_in(rule["condition"])):
            raise ValueError("number_not_in_request")
        return rule["condition"], desired
    if kind == "set_exception":
        if not isinstance(desired, list):
            raise ValueError("exceptions_not_a_list")
        out = []
        for e in desired:
            text = safe_text(e.get("text") if isinstance(e, Mapping) else None, 300)
            if text is None or numbers_in(text) - req_numbers:
                raise ValueError("exception_text")
            if isinstance(e, Mapping) and e.get("condition") is not None:
                bk.validate_condition(e["condition"])
                if numbers_in(e["condition"]) - req_numbers:
                    raise ValueError("number_not_in_request")
            out.append({"text": text, "condition": e.get("condition") if isinstance(e, Mapping) else None})
        return [{"text": x["text"], "condition": x.get("condition")} for x in rule["exceptions"]], out
    if kind == "set_scope":
        if not isinstance(desired, Mapping) or not desired:
            raise ValueError("scope_not_an_object")
        known = " ".join(_norm(v) for vs in rule["scope"].values() for v in (vs if isinstance(vs, list) else [vs]))
        for key, vals in desired.items():
            for v in (vals if isinstance(vals, list) else [vals]):
                if not isinstance(v, str) or not v.strip() or (_norm(v) not in req_norm and _norm(v) not in known):
                    raise ValueError("scope_value_not_in_request")
        return rule["scope"], dict(desired)
    raise ValueError("kind")


# ---- the numeric adapter ---------------------------------------------------------------------------------------------------------------------

def to_brief_form(op: Mapping, rule: Mapping) -> dict:
    """The structured form the existing numeric change flow takes (`normalise_brief_form`), for a set_value operation on a rule that came from an extraction candidate."""
    if op["kind"] != "set_value":
        raise ValueError("only set_value has a numeric form")
    unit = op["desired"].get("unit") if isinstance(op["desired"], Mapping) else None
    form = {"subject": rule["title"], "currency": unit or "JPY", "old_value": str(op["before"]["value"]), "new_value": str(op["desired"]["value"]),
            "scope": {k: (v[0] if isinstance(v, list) and v else str(v)) for k, v in rule["scope"].items() if isinstance(v, (str, list))} or None}
    if form["scope"] is None:
        del form["scope"]
    if rule.get("candidate_ids"):
        form["target_candidate_id"] = rule["candidate_ids"][0]
    return form
