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
OPTIONAL_MODEL_KEYS = {"kept_as_is", "context", "parts"}        # contracts 2 / 8; an object without them (older stubs, stored answers) reads as empty lists
PART_KINDS = ("current_state", "reason", "heading", "aside", "unclear")   # contract 8: what a part of the request that asks for no change is
PART_KEYS = {"part", "kind", "rule_id"}
KEPT_KEYS = {"quote", "rule_id"}
OP_KEYS = {"kind", "rule_id", "candidate_rule_ids", "desired", "user_quote"}
OPTIONAL_OP_KEYS = {"condition", "parts"}                         # contract 7: the typed condition of a condition edit; contract 8: the parts it covers
_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")             # signed: "warmer than -18 C" is -18 (UNDERSTAND-V9 U9-18); "1-2" stays 1 and 2
_SENTENCE = re.compile(r"(?:(?<!\d)\.|\.(?!\d)|[;!?\n])+|\band\b|(?<!\d),|,(?!\d)|\bthen\b|\balso\b", re.I)   # "7.5%" is one number (U11-17)
_UNSAFE = re.compile(r"<[^>]*>|\]\(|https?://|www\.|`|javascript:|\x00", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


_SCALED = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(thousand|million|billion)\b", re.I)
_SCALE = {"thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}


def numbers_in(value) -> set:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    for n in re.findall(r"\bminus\s+(\d[\d,]*(?:\.\d+)?)", text, re.I):                    # "minus 120 pounds" is -120 (UNDERSTAND-V11 U11-07)
        try:
            out.add(-float(n.replace(",", "")))
        except ValueError:
            pass
    for n, scale in _SCALED.findall(text):                               # "3 million yen" is 3,000,000 (UNDERSTAND-V7 U7-14)
        out.add(float(n) * _SCALE[scale.lower()])
    return out


def safe_text(text, limit) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t if t and len(t) <= limit and not _UNSAFE.search(t) else None


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or not (MODEL_KEYS <= set(obj) <= MODEL_KEYS | OPTIONAL_MODEL_KEYS):
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
        if not isinstance(op, dict) or not (OP_KEYS <= set(op) <= OP_KEYS | OPTIONAL_OP_KEYS) or not isinstance(op["kind"], str) or not isinstance(op["user_quote"], str):
            raise ValueError("operation_shape")
        if op["rule_id"] is not None and not isinstance(op["rule_id"], str):
            raise ValueError("operation_rule_id")
        if not isinstance(op["candidate_rule_ids"], list) or not all(isinstance(x, str) for x in op["candidate_rule_ids"]):
            raise ValueError("operation_candidates")
    kept = obj.get("kept_as_is", [])
    if not isinstance(kept, list) or len(kept) > limits["max_questions"] * 2 or not all(
            isinstance(k, dict) and set(k) == KEPT_KEYS and all(isinstance(k[x], str) for x in KEPT_KEYS) for k in kept):
        raise ValueError("kept_as_is_shape")
    context = obj.get("context", [])
    if not isinstance(context, list) or len(context) > limits["max_questions"] * 2 or not all(isinstance(x, str) for x in context):
        raise ValueError("context_shape")
    parts = obj.get("parts", [])
    if not isinstance(parts, list) or len(parts) > MAX_PARTS or not all(isinstance(p, dict) and set(p) == PART_KEYS and isinstance(p["part"], str) and p["kind"] in PART_KINDS
                                                                       and (p["rule_id"] is None or isinstance(p["rule_id"], str)) for p in parts):
        raise ValueError("parts_shape")
    for op in obj["operations"]:
        if "parts" in op and not (isinstance(op["parts"], list) and all(isinstance(x, str) for x in op["parts"]) and len(op["parts"]) <= MAX_PARTS):
            raise ValueError("operation_parts")
    return obj


MAX_PARTS = 40


def build_prompt(frozen: Mapping, request: str, rules: Iterable[Mapping], *, max_chars: int, where: Optional[Mapping] = None) -> str:
    """`where` = {rule id: where the rule comes from (a code function, a document or config place)} so rules with the same wording in different functions can be told
    apart (SUR-03 v4: the route_request and route_emergency validations read alike and the model could only guess). One-pass substitution: the request never expands
    a placeholder."""
    lines, used = [], 0
    for r in rules:
        loc = (where or {}).get(r["item_id"])
        cond = r.get("condition")
        # contract 6: the condition also in the exact JSON form an operation must use, so the model copies objects, not the words of `text` (A50-A57: conditions
        # written as "pallet gross kg more than 1500 kg", "item category is one of ['drone', 'gimbal']", "<rule id>")
        cond_line = f"\n  condition: {json.dumps(cond, ensure_ascii=False)}" if isinstance(cond, Mapping) else ""
        line = (f"- id={r['item_id']} authority={r.get('authority')} status={r.get('status')}\n  text: {r['text']}" + cond_line
                + (f"\n  where: {loc}" if loc else ""))
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    numbered = "\n".join(f"{pid}: {text}" for pid, text in request_parts(request).items())
    parts = {"{request}": request, "{rules}": "\n".join(lines) or "(none)", "{parts}": numbered or "(none)"}
    return re.sub(r"\{request\}|\{rules\}|\{parts\}", lambda m: parts[m.group(0)], frozen["prompt_template"])


def request_parts(request: str) -> dict:
    """Contract 8: the server's own split of the request into parts, numbered P1.. in order. The model classifies these ids; the server verifies them. The same split
    the coverage check uses, so every part is accounted for exactly once."""
    return {f"P{n}": text for n, text in enumerate(_segments(request), 1)}


def rule_location(rule: Mapping, max_chars: int = 160) -> Optional[str]:
    """Where a stored rule comes from, from its own source bindings: a code span's function (`code route_emergency`), a document / config place (`path#label`)."""
    out = []
    for b in rule.get("source_bindings") or []:
        ref = str(b.get("evidence_ref") or "")
        if ref.startswith("{"):
            try:
                loc = json.loads(ref)
            except ValueError:
                continue
            if isinstance(loc, Mapping) and loc.get("qualname"):
                out.append(f"code {loc['qualname']}")
        elif ref:
            out.append(ref.split("/")[-1])
    text = "; ".join(dict.fromkeys(out))
    return text[:max_chars] if text else None


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


def _words(s: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", str(s).lower())) + " "


def _role(text: str) -> Optional[str]:
    t = str(text).strip()
    slug = bk._slug(t[5:] if t.startswith("role-") else t)
    return "role-" + slug if slug else None


# A qualifier is a scope phrase, not a part of its own ("In emergency routing, raise ...", "..., in addition to ..."): it starts with a preposition and carries no number
# and no change verb. It joins the part next to it, so the coverage check does not ask about it as a dropped request (live diagnostic SUR03-V2-20261023: every
# "In <place>, <change>" request was held for a false dropped part). A phrase with a number or a verb stays a part of its own and is still checked.
_QUALIFIER = re.compile(r"^\s*(in|for|on|at|within|under|across|during|inside|throughout|apart from|in addition to|as)\b", re.I)
_CHANGE_VERB = re.compile(r"\b(set|raise|lower|change|make|send|allow|require|add|remove|drop|reject|accept|approve|route|increase|decrease|reduce|apply|become|becomes|"
                          r"should|must|need|needs|only|replace|move|exempt|refer|referred|escalate)\b", re.I)


def _is_qualifier(s: str) -> bool:
    return bool(_QUALIFIER.match(s)) and not re.search(r"\d", s) and not _CHANGE_VERB.search(s)


# A value clause split off by a comma ("Change the CAPEX threshold, as people decided it, from 2,000,000 to 5,000,000 JPY") belongs to the part before it, and an
# "as ... decided" clause is a qualifier (live finding SUR08-V1 A9b, 2026-10-23: the head and the qualifier were asked about as dropped parts).
_VALUE_CLAUSE = re.compile(r"^\s*(from|to|by|up to|down to|at)\s+\d", re.I)


_MODAL_END = re.compile(r"\b(must|should|shall|can|will|may|has to|have to|needs? to)\s*$", re.I)


def _segments(request: str) -> list:
    raw = [s.strip() for s in _SENTENCE.split(request) if s and s.strip()]
    merged: list = []
    pending = ""
    for s in raw:
        if merged and _MODAL_END.search(merged[-1]):                # "X must | also reject ...": a head ending in a modal belongs to the part after it (grammar)
            merged[-1] = f"{merged[-1]} {s}"
            continue
        if _VALUE_CLAUSE.match(s) and (merged or pending):
            if merged:
                merged[-1] = f"{merged[-1]}, {s}"
            else:
                pending = f"{pending}, {s}"
            continue
        if _is_qualifier(s):
            if merged:
                merged[-1] = f"{merged[-1]}, {s}"                      # a trailing qualifier belongs to the part before it
            else:
                pending = f"{pending} {s}".strip()                     # a leading qualifier belongs to the part after it
            continue
        merged.append(f"{pending}, {s}" if pending else s)
        pending = ""
    if pending:
        merged.append(pending)
    return [s for s in merged if len(re.findall(r"[a-z]{2,}", s, re.I)) >= 3 and _content_words(s)]


# words that carry no request of their own ("while you are at it", "and please also"): a part made only of them is not a sub-request (CORE-V5-S1 V5-U-11)
_FILLER = set(("a an the and or but so then also too as well while you we they it its is are be am at in on of to for this that these those now please just "
               "by way meanwhile additionally furthermore besides moreover").split())


def _content_words(segment: str) -> list:
    return [w for w in re.findall(r"[a-z]{3,}", segment.lower()) if w not in _FILLER]


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
        val = d if not isinstance(d, Mapping) else f"{d['value']} {d.get('unit') or ''}".strip()
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


_DECIDED_PHRASE = re.compile(r"(?i)\b(?:as (?:people|we|they|it was|you) decided(?: it)?|as decided|the decided (?:rule|version|value)|the rule as decided|decided version)\b")


def _names_decided_version(quote: str, request: str) -> bool:
    """True when the quoted part of the request, or the request as a whole when it holds one change, says that the decided version is meant."""
    return bool(_DECIDED_PHRASE.search(quote) or _DECIDED_PHRASE.search(request))


def resolve(obj: Mapping, request: str, rules: Mapping[str, Mapping], relations: Iterable[Mapping], limits: Mapping, *, pin: Mapping,
            choices: Optional[Mapping] = None, where: Optional[Mapping] = None) -> dict:
    """The server's understanding. `rules` = offered stored rules by id (the only ids an operation may name). `choices` = {observed rule id: intent} the person picked
    for an edit of the losing side of a decided conflict (one of DECIDED_CONFLICT_INTENTS); nothing is retargeted without it."""
    req_norm = _norm(request)
    req_numbers = numbers_in(request)
    questions, refused, rejected, noops, ops = [], [], [], [], []
    quotes, conflict_choices, retargeted, normalized = [], [], [], []
    if len(obj["operations"]) > bk.MAX_OPERATIONS_PER_REQUEST:
        questions.append(f"The request holds more than {bk.MAX_OPERATIONS_PER_REQUEST} changes. Please split it into several smaller changes.")
        obj = dict(obj, operations=[])
        rejected.append({"reason": "too_many_operations"})
    for raw in obj["operations"]:
        quote = safe_text(raw["user_quote"], 400)
        if quote is None or _norm(quote) not in req_norm:
            rejected.append({"reason": "quote_not_in_request", "kind": raw["kind"], "quote": (str(raw.get("user_quote") or ""))[:200]})
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
        # the schema leaves `desired` open, so a structured value can arrive written as JSON text, whole or per part ({"any": ["{...}", "{...}"]}); it is read
        # as that structure, never guessed (2026-10-24, live runs SUR03-V8 01 / 03 and UNDERSTAND-V9 03 / 11 / 19: condition edits refused "must be an object")
        given = raw["desired"]
        if raw["kind"] == "add_or_change_condition" and isinstance(raw.get("condition"), Mapping) and raw["condition"]:
            given = {k: v for k, v in raw["condition"].items() if v not in (None, [], "")}       # contract 7: the typed condition is the value of a condition edit
            if any(k in given for k in ("any", "all", "not")):          # a composite with stray leaf keys filled too (the schema allows both): the composite is the condition
                given = {k: v for k, v in given.items() if k in ("any", "all", "not")}                 # (UNDERSTAND-V12-C8 U12-21: refused as "not a comparison")
            normalized.append({"rule_id": rid, "from": "condition_field", "to": raw["kind"]})
        desired, was_text = _json_text(given)
        if was_text:
            normalized.append({"rule_id": rid, "from": "json_text", "to": raw["kind"]})
        rule, kind, desired = rules[rid], raw["kind"], _clean_units(desired)     # an empty unit means "no unit" (SUR03-V5B: a quantity bound with unit "")
        if kind == "add_or_change_condition":
            desired, worded = _words_to_conditions(desired, rule)
            if worded:
                normalized.append({"rule_id": rid, "from": "condition_words", "to": kind})
            desired = _decimals(_rule_fields(desired, rule))              # "correction amount jpy" -> the rule's own field id (UNDERSTAND-V7: four requests refused)
        canon = _canonical_value_edit(kind, desired, rule)
        if canon is not None:                                            # same field and comparator, only the number changes: the same edit as set_value (marked, not hidden)
            normalized.append({"rule_id": rid, "from": kind, "to": "set_value"})
            kind, desired = "set_value", canon
        twins = _ambiguous_target(rid, rules, req_norm, where, relations)
        if twins:                                                        # contract 8: an indistinguishable sibling rule — the person chooses, the server never picks
            names = " or ".join(f"“{rules[x]['title']}”" for x in [rid] + twins)
            questions.append(f"“{quote}”: more than one rule fits — {names}. Which one do you mean?")
            rejected.append({"reason": "ambiguous_rule", "kind": kind, "rule_id": rid, "candidate_rule_ids": [rid] + twins})
            continue
        decided = rid + "-target"
        if not rid.endswith("-target") and decided in rules and _names_decided_version(quote, request):
            # The request itself says it means the decided version ("as people decided it", "the decided rule", ...): the operation is re-aimed at it, visibly
            # (`retargeted`), never silently (live finding SUR08-V1 items 05 / 06, 2026-10-23: the question below was asked although the request had already answered it).
            retargeted.append({"from": rid, "to": decided, "intent": "named_decided_version", "quote": quote})
            rid, rule = decided, rules[decided]
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
            rejected.append({"reason": f"desired_not_supported:{exc}", "kind": kind, "rule_id": rid, "desired_shape": _shape(desired), **_given(given)})
            if str(exc) == "reverses_the_rule":
                questions.append(f"“{quote}” was read as making “{rule['title']}” apply in exactly the opposite cases. Is that what you want, or is the rule already "
                                 "as it should be?")
            elif str(exc) == "actor_not_named":
                questions.append(f"“{quote}” does not name who should do it. Which role should it be?")
            else:
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
        try:
            bk.validate_change_operation(op, known_rule_ids=set(rules))
        except bk.ContractError as exc:                                  # a model value the contract refuses is one refused operation and a question, never a crash
            rejected.append({"reason": f"operation_invalid:{exc.code}", "kind": kind, "rule_id": rid, "desired_shape": _shape(desired), **_given(given)})
            questions.append(f"I could not safely read what should change for “{rule['title']}” (“{quote}”). Please say it again with the exact new value.")
            continue
        ops.append(op)
    kept_as_is, kept_refused, context, context_refused, relabelled = [], [], [], [], []
    for k in obj.get("kept_as_is") or []:
        quote = safe_text(k["quote"], 300)
        reason = _kept_reason(quote, k["rule_id"], rules, req_norm, request)
        if reason == "not_about_this_rule" and _context_reason(quote, rules, req_norm) is None:
            # "Everything else about emergency approvals stays the same" names no rule: as a remark it passes the stricter context check (no number, no change,
            # condition, exception or owner word, no actor), so it is read as context, visibly relabelled (2026-10-24, live run SUR03-V7 item 05)
            context.append(quote)
            relabelled.append({"quote": quote, "from": "kept_as_is", "to": "context"})
            continue
        if reason:
            kept_refused.append({"quote": quote, "rule_id": k["rule_id"] if k["rule_id"] in rules else None, "reason": reason})
            continue
        kept_as_is.append({"quote": quote, "rule_id": k["rule_id"], "rule_title": rules[k["rule_id"]]["title"]})
    for c in obj.get("context") or []:
        quote = safe_text(c, 300)
        reason = _context_reason(quote, rules, req_norm)
        # a part labelled context that states the CURRENT value of a rule this request changes ("the sign-off begins at the hard-coded 1,000,000 JPY; move it up
        # to ...") describes the state before the change: it is kept as is, visibly relabelled, when it passes every kept-as-is check against that rule (its numbers
        # are the rule's current values, no change verb). 2026-10-24, live run SUR03-V6 item 01: the right operation was held because this part stayed uncovered.
        again = next((rid for rid in sorted({o["rule_id"] for o in ops}) if reason == "has_a_number" and _kept_reason(quote, rid, rules, req_norm, request) is None), None)
        if again:
            kept_as_is.append({"quote": quote, "rule_id": again, "rule_title": rules[again]["title"], "relabelled_from": "context"})
        elif reason:
            context_refused.append({"quote": quote, "reason": reason})
        else:
            context.append(quote)
    parts_read, part_issues = [], []
    if "parts" in obj:
        # contract 8: every server-numbered part is accounted for exactly once — by an operation (the parts it lists, or the part its quote covers) or by the
        # model's classification, which the server verifies by grounding (numbers / actors against the offered rules and this request's operations). What is
        # left, or fails grounding, or is "unclear", is asked about. No word list decides whether a remark is a change.
        labels = request_parts(request)
        op_parts = set()
        listed = {p for raw in obj["operations"] if isinstance(raw, dict) for p in (raw.get("parts") or []) if p in labels}
        # a value the model tried to set, even in an operation the server refused, is a value this request names: a remark repeating it is grounded
        attempted = set().union(*(numbers_in(raw.get("desired")) | numbers_in(raw.get("condition")) for raw in obj["operations"] if isinstance(raw, dict))) if obj["operations"] else set()
        for pid, text in labels.items():
            if pid in listed or _covered(text, quotes):
                op_parts.add(pid)
        seen_parts = set()
        for p in obj["parts"]:
            pid = p["part"]
            if pid not in labels or pid in op_parts or pid in seen_parts:
                continue
            seen_parts.add(pid)
            reason = _part_grounding(labels[pid], p["kind"], p["rule_id"], rules, ops, request, attempted)
            if reason is None:
                row = {"part": pid, "quote": labels[pid], "kind": p["kind"], "rule_id": p["rule_id"] if p["rule_id"] in rules else None}
                if row["rule_id"]:
                    row["rule_title"] = rules[row["rule_id"]]["title"]
                parts_read.append(row)
                if p["kind"] == "current_state" and row["rule_id"]:
                    kept_as_is.append({"quote": labels[pid], "rule_id": row["rule_id"], "rule_title": row["rule_title"]})
                else:
                    context.append(labels[pid])
            else:
                part_issues.append({"part": pid, "quote": labels[pid], "kind": p["kind"], "reason": reason})
        uncovered = [labels[pid] for pid in labels if pid not in op_parts and pid not in seen_parts] + [i["quote"] for i in part_issues]
        for i in part_issues:
            if i["reason"] == "model_unclear":
                questions.append(f"I could not read this part: “{i['quote']}”. What should change, if anything?")
            else:
                questions.append(f"I could not turn this part into a change: “{i['quote']}”. What should change?")
        for pid in labels:
            if pid not in op_parts and pid not in seen_parts:
                questions.append(f"I could not turn this part into a change: “{labels[pid]}”. What should change?")
    else:
        uncovered = [s for s in _segments(request) if not _covered(s, quotes + [k["quote"] for k in kept_as_is] + context)]
        # contract 6 (answers without "parts"): a part nobody labelled is read as a remark only when it passes the strict context check; anything else is asked
        for part in list(uncovered):
            if _context_reason(part, rules, req_norm) is None:
                uncovered.remove(part)
                context.append(part)
                relabelled.append({"quote": part, "from": "unlabelled", "to": "context"})
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
              "decided_conflict_choices": conflict_choices, "retargeted": retargeted, "kept_as_is": kept_as_is, "kept_as_is_refused": kept_refused, "context": context, "context_refused": context_refused, "relabelled": relabelled, "parts_read": parts_read, "part_issues": part_issues,
              "normalized_operations": normalized}
    result["preview_hash"] = canonical_hash({"pin": result["pin"], "request": _norm(request), "operations": ops, "write_scope": write_scope, "status": status})
    return result


_NUMBER_WORDS = {"zero": "0"}          # "zero or less"; other number words are too often not numbers ("leave that one": UNDERSTAND-V6 U6-07)
_STOP = set("a an the of or and to in on for with by as is are be it its this that these those than less more no not any all also".split())


def _field_words(field: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]+", str(field).lower()) if w]


def _rule_fields(desired, rule):
    """A condition field the model wrote in words ("correction amount jpy") is replaced by the rule's own field id when its words are all words of exactly ONE of the
    rule's condition fields (two words at least); anything else is left as written (and the contract still decides). Exact: it never invents a field."""
    fields = sorted({leaf["field"] for leaf in _all_leaves(rule.get("condition"))})

    def fix(node):
        if isinstance(node, Mapping):
            out = {k: fix(v) for k, v in node.items()}
            f = out.get("field")
            if isinstance(f, str) and f not in fields:
                words = set(_field_words(f))
                hits = [x for x in fields if len(words) >= 2 and words <= set(_field_words(x))]
                if not hits and "." in f:                     # "request.subject_area" for the rule's "job.subject_area": the same field name under another prefix,
                    hits = [x for x in fields if x.rsplit(".", 1)[-1] == f.rsplit(".", 1)[-1]]   # one match only (UNDERSTAND-V10 U10-21)
                if len(hits) == 1:
                    out["field"] = hits[0]
            return out
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node
    return fix(desired)


_LIST_LEAF = re.compile(r"^\s*(?P<f>[a-z][a-z0-9 ._]*?)\s+(?P<op>is one of|is none of|not_in|not in|in)\s+(?P<v>\[.*\])\s*$")
_COUNT_WORDS = frozenset("unit units item items piece pieces pcs order orders".split())
_WORD_OPS = {"is not": "ne", "more than": "gt", "at least": "gte", "less than": "lt", "at most": "lte", "is": "eq",           # condition_text's words
             "greater than or equal to": "gte", "less than or equal to": "lte", "greater than": "gt", "above": "gt", "over": "gt", "below": "lt",
             "under": "lt",                                                                     # plain synonyms (UNDERSTAND-V10 U10-05: "greater than 29.5 C")
             "gte": "gte", "gt": "gt", "lte": "lte", "lt": "lt", "eq": "eq", "ne": "ne",                                             # the prompt's op codes
             ">=": "gte", ">": "gt", "<=": "lte", "<": "lt", "!=": "ne", "=": "eq"}
_WORD_LEAF = re.compile(r"^\s*(?P<f>[a-z][a-z0-9 ._]*?)\s*(?P<op>\bis not\b|\bgreater than or equal to\b|\bless than or equal to\b|\bgreater than\b|\bmore than\b|\bat least\b|\bless than\b|\bat most\b|\babove\b|\bover\b|\bbelow\b|\bunder\b|\bis\b|\bgte\b|\bgt\b|\blte\b|\blt\b|"
                        r"\beq\b|\bne\b|>=|<=|!=|>|<|=)\s*(?P<v>-?\d[\d,]*(?:\.\d+)?)(?:\s*(?P<u>[A-Za-z%]+))?\s*$")


def _words_to_conditions(desired, rule):
    """-> (desired with every condition written in the rule lines' own words — "pallet gross kg more than 1500 kg", the exact form `condition_text` shows the model —
    replaced by its comparison object, whether anything was replaced). A part is read only when it matches that form exactly, has a number, names a field of
    THIS rule's condition (its words equal the field's words, or are a unique subset of two words or more) and, when it writes a unit, the rule's unit for that
    field; anything else stays as written and the contract refuses it. 2026-10-24, observed on a fresh call (UNDERSTAND-V9-DIAG3): the model wrote
    {"any": ["pallet gross kg at most 0 kg", "pallet gross kg more than 1500 kg"]}."""
    leaves = _all_leaves(rule.get("condition"))
    fields = sorted({leaf["field"] for leaf in leaves})
    units = {leaf["field"]: leaf.get("unit") for leaf in leaves}

    def leaf_of(text):
        lm = _LIST_LEAF.match(text.lower())
        if lm:                                             # "item category is one of ['drone', 'gimbal']" — condition_text's list form (U11-02 / 16)
            words = _field_words(lm.group("f"))
            hits = [x for x in fields if words == _field_words(x)] or [x for x in fields if len(set(words)) >= 2 and set(words) <= set(_field_words(x))]
            vals = re.findall(r"'([^']*)'|\"([^\"]*)\"", text[lm.start("v"):])
            vals = [a or b for a, b in vals]
            if len(hits) == 1 and vals:
                known = {str(v).lower(): v for leaf in leaves if leaf["field"] == hits[0] and isinstance(leaf.get("value"), list) for v in leaf["value"]}
                return {"field": hits[0], "op": "not_in" if lm.group("op") in ("is none of", "not_in", "not in") else "in",
                        "value": [known.get(v.lower(), v.upper() if all(str(k).isupper() for k in known.values()) and known else v) for v in vals]}
            return None
        if text.strip() == rule.get("rule_id") and isinstance(rule.get("condition"), Mapping):
            return dict(rule["condition"])                 # the edited rule's own id stands for its current condition: {"any": ["<rule id>", "request.quantity > 320"]}
        m = _WORD_LEAF.match(text.lower())
        if not m:
            return None
        words = _field_words(m.group("f"))
        hits = [x for x in fields if words == _field_words(x)] or [x for x in fields if len(set(words)) >= 2 and set(words) <= set(_field_words(x))]
        if len(hits) != 1:
            return None
        unit = m.group("u")
        if unit and not units.get(hits[0]) and unit.lower() in _COUNT_WORDS:
            unit = None                                    # "request quantity more than 12000 units" on a count field without a unit (SUR03-V9 01 / 03 / 07)
        if unit and (units.get(hits[0]) or "").lower() != unit.lower():
            return None
        raw = m.group("v").replace(",", "")
        value = float(raw) if "." in raw else int(raw)
        return {"field": hits[0], "op": _WORD_OPS[m.group("op")], "value": value, **({"unit": units[hits[0]]} if units.get(hits[0]) else {})}

    changed = False

    def fix(node, top=False):
        nonlocal changed
        if isinstance(node, str) and not top:
            leaf = leaf_of(node)
            if leaf is not None:
                changed = True
                return leaf
            return node
        if isinstance(node, Mapping):
            return {k: ([fix(x) for x in v] if k in ("any", "all") and isinstance(v, list) else fix(v) if k == "not" else v) for k, v in node.items()}
        return node
    if isinstance(desired, str):
        leaf = leaf_of(desired)
        return (leaf, True) if leaf is not None else (desired, False)
    out = fix(desired, top=True)
    return out, changed


def _all_leaves(cond) -> list:
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        return [cond]
    return [leaf for key in ("all", "any") for c in cond.get(key, []) for leaf in _all_leaves(c)] + (_all_leaves(cond["not"]) if "not" in cond else [])


def _canonical_value_edit(kind, desired, rule) -> Optional[dict]:
    """An `add_or_change_condition` whose desired value is ONE comparison on the same field with the same comparator as the rule's only threshold, differing only in
    the number, is exactly a `set_value` of that threshold (CORE-V5-S1: "start above 40 hours instead of above 30" written as a new condition). Only when the threshold
    carries a unit (the contract requires one for set_value); otherwise the condition edit stays as it is (equally valid)."""
    if kind != "add_or_change_condition" or not isinstance(desired, Mapping) or "field" not in desired:
        return None
    cond = rule.get("condition")
    if not isinstance(cond, Mapping) or "field" not in cond:       # a composite condition: replacing it by one comparison would drop its other parts (not equivalent)
        return None
    leaves = _numeric_leaves(cond)
    if len(leaves) != 1:
        return None
    leaf = leaves[0]
    if desired.get("field") != leaf["field"] or desired.get("op") != leaf["op"] or not leaf.get("unit") or not _is_number(desired.get("value")):
        return None
    if desired.get("unit") and desired["unit"] != leaf["unit"]:
        return None
    return {"value": desired["value"], "unit": leaf["unit"]}


_UNIT_ALIASES = {"yen": "JPY", "jpy": "JPY", "\u5186": "JPY", "\u00a5": "JPY", "usd": "USD", "dollars": "USD", "eur": "EUR", "euro": "EUR", "euros": "EUR",
                 "°c": "C", "celsius": "C", "°f": "F", "fahrenheit": "F"}


def _clean_units(desired):
    """Remove `"unit": ""` (the model writes an empty unit for a count) and write a currency the usual way (`yen` -> `JPY`; UNDERSTAND-V6 U6-07: "8,000,000 yen"
    was refused as a different unit than the rule's JPY). Every other field is kept as given."""
    if isinstance(desired, Mapping):
        out = {}
        for k, v in desired.items():
            if k == "unit" and isinstance(v, str):
                if not v.strip():
                    continue
                v = _UNIT_ALIASES.get(v.strip().lower(), v)
            out[k] = _clean_units(v)
        return out
    if isinstance(desired, list):
        return [_clean_units(x) for x in desired]
    return desired


def _drop_empty_units(desired):
    """Kept name for the unit cleaning (see `_clean_units`)."""
    return _clean_units(desired)


def _kept_reason(quote, rule_id, rules: Mapping, req_norm: str, request: str = "") -> Optional[str]:
    """A `kept_as_is` entry is accepted only when it is an exact piece of the request, names an offered rule, states no change (no change verb), every number it
    writes (digits or a number word) is a value of that rule's CURRENT condition, and it - or the sentence of the request it stands in - shares a word with the
    rule's title or condition field. Otherwise the part stays uncovered and the person is asked, as before: a dropped change cannot hide here. (The sentence counts
    since 2026-10-24, live run SUR03-V6 item 08: "quantities larger than 600 are invalid quantities; zero or less is already handled" names the field once.)"""
    if quote is None or _norm(quote) not in req_norm:
        return "quote_not_in_request"
    rule = rules.get(rule_id)
    if rule is None:
        return "rule_not_offered"
    if _CHANGE_VERB.search(_NO_CHANGE.sub(" ", quote)):                                       # "no change there" / "should stay as it is" say the opposite
        return "states_a_change"
    current = {float(str(leaf["value"]).replace(",", "")) for leaf in _numeric_leaves(rule.get("condition"))}
    written = numbers_in(quote) | {float(v) for w, v in _NUMBER_WORDS.items() if re.search(rf"\b{w}\b", quote, re.I)}
    if not written <= current:
        return "number_not_current"
    fields = " ".join(str(leaf.get("field") or "") for leaf in _numeric_leaves(rule.get("condition")))
    actor = str(rule.get("actor_ref") or "").replace("role-", "").replace("-", " ")             # "the workshop planner still books it" (UNDERSTAND-V10 U10-17)
    fields += " " + " ".join(w for code, w in _CURRENCY_WORDS.items() if code in fields.lower())  # "the 20,000 yen line" meets a _jpy field (U11-20)
    rule_words = {_singular(w) for w in re.findall(r"[a-z]+", f"{rule.get('title', '')} {fields} {actor}".lower()) if w not in _STOP and len(w) > 2}
    quote_words = {_singular(w) for w in re.findall(r"[a-z]+", quote.lower()) if w not in _STOP and len(w) > 2}
    sentence_words = set()
    if request and quote_words <= _ELLIPTIC:                   # only a part with no subject of its own ("zero or less is already handled") borrows its sentence's
        sentence = next((x for x in re.split(r"(?<=[.!?])\s+|\n+", request) if _norm(quote) in _norm(x)), "")
        sentence_words = {_singular(w) for w in re.findall(r"[a-z]+", sentence.lower()) if w not in _STOP and len(w) > 2}
    if not rule_words & (quote_words | sentence_words):
        return "not_about_this_rule"
    return None


_CURRENCY_WORDS = {"jpy": "yen", "eur": "euro euros", "usd": "dollar dollars", "gbp": "pound pounds"}
_ELLIPTIC = set("zero none fewer greater smaller larger higher lower below above under over equal same already still handled covered rejected refused invalid stay "
                "remain unchanged case one them they there here now today kept keep".split())      # singular forms: the quote's words are singularised first


_CONTEXT_SIGNAL = re.compile(r"\b(decide|decides|decided|sign|signs|signed|handle|handles|review|reviews|check|checks|verify|verifies|own|owns|goes|go to|sent|"
                             r"exception|exceptions|exempt|except|instead|rather|not|never|no|without|unless|limit|threshold|cap|"
                             r"before|after|until|starting|start|stop|end)\b"
                             # "from" / "above" / "more" ... bound a number; a part reaching this check has none (it was refused before), so they only count where
                             # they name a time: "From the library committee:" is a heading, "From next quarter" is a date (2026-10-24, UNDERSTAND-V9 U9-07)
                             r"|\bfrom\s+(?:now|today|tomorrow|next|this|then|the\s+(?:start|beginning|end|first)|monday|tuesday|wednesday|thursday|friday|"
                             r"january|february|march|april|may|june|july|august|september|october|november|december|q[1-4])\b", re.I)


# statements that something does NOT change; their verbs ("should", "must") are not a change (2026-10-24, SUR03-V10 item 01: "Everything else in that function
# should stay as it is" was refused as a possible change)
_NO_CHANGE = re.compile(r"\bno changes?\b|\bunchanged\b|\bnot (?:be )?changed\b|\b(?:should|must|can|will|is to|are to)?\s*(?:stay|stays|remain|remains)\s+"
                        r"(?:as (?:it|they) (?:is|are)|the same|as before|untouched|unchanged)\b|\bleave (?:it|them|everything else|the rest[a-z ]*?) (?:as (?:it|they) (?:is|are)|alone)\b",
                        re.I)


_DATE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\b|"
                   r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b|\b\d{4}-\d{2}-\d{2}\b", re.I)


def _context_reason(quote, rules: Mapping, req_norm: str) -> Optional[str]:
    """A `context` quote (a heading, a greeting, a remark such as "that is the one I mean") is accepted only if it is an exact piece of the request, carries no
    number, no change verb, no word that could state a condition, exception or new owner, and names no actor of an offered rule as a whole phrase. Otherwise the part
    stays uncovered and the person is asked (a change cannot hide here)."""
    if quote is None or _norm(quote) not in req_norm:
        return "quote_not_in_request"
    if numbers_in(_DATE.sub(" ", quote)) or re.search(r"\bzero\b", quote, re.I):           # a date is not a value ("Ops sync, 14 Oct": U11-08)
        return "has_a_number"
    plain = _NO_CHANGE.sub(" ", quote)
    if _CHANGE_VERB.search(plain) or _CONTEXT_SIGNAL.search(plain):
        return "may_state_a_change"
    low = " " + " ".join(re.findall(r"[a-z0-9]+", quote.lower())) + " "
    for r in rules.values():
        actor = str(r.get("actor_ref") or "").replace("role-", "").replace("-", " ").strip()
        if actor and f" {actor} " in low:
            return "names_an_actor"
    return None


def _rule_numbers(rule: Mapping) -> set:
    return {float(str(leaf["value"]).replace(",", "")) for leaf in _numeric_leaves(rule.get("condition"))}


def _op_numbers(op: Mapping) -> set:
    return numbers_in(op.get("desired")) | numbers_in(op.get("condition")) | numbers_in(op.get("before") or {})


def _actor_phrases(rules: Mapping) -> dict:
    """{actor_ref: the role's words as a phrase} for every offered rule with an actor."""
    out = {}
    for r in rules.values():
        a = r.get("actor_ref")
        if a:
            out[a] = _words(a.replace("role-", "").replace("-", " ")).strip()
    return out


def _part_grounding(text: str, kind: str, rule_id, rules: Mapping, ops: list, request: str, attempted: set = frozenset()) -> Optional[str]:
    """Contract 8: why a part the model says asks for no change cannot be read as such (None = grounded). No word lists: a part is grounded when every number it
    writes is a current value of a rule (the named rule for a current-state part; any offered rule otherwise) or a value this request's operations set, and every
    offered role it names is the current actor of a rule (the named rule for a current-state part) or an actor an operation sets. A date is not a value. An
    "unclear" part is the model's own question."""
    if kind == "unclear":
        return "model_unclear"
    if kind == "current_state" and rule_id not in rules:
        # "the rest of standard routing is untouched": a restatement that names no rule and nothing to ground (no number, no offered role) is an aside, not a
        # refusal (SUR03-V13 item 05); with a number or a role it still needs the rule it is about
        if not (numbers_in(_DATE.sub(" ", text)) or re.search(r"zero", text, re.I)) and not any(f" {phrase} " in _words(text) for phrase in _actor_phrases(rules).values()):
            return None
        return "rule_not_offered"
    scope = {rule_id: rules[rule_id]} if kind == "current_state" else rules
    allowed_numbers = set().union(*(_rule_numbers(r) for r in scope.values())) | set().union(*(_op_numbers(o) for o in ops)) if scope or ops else set()
    allowed_numbers |= attempted
    if kind != "current_state":
        # HK-AEV U-08 (2026-10-27): "In the 2025 services circular, raise ..." names the edited rule by its source label; the year is in that rule's own title
        allowed_numbers |= set().union(*(numbers_in(_DATE.sub(" ", str(rules[o["rule_id"]].get("title") or ""))) for o in ops if o["rule_id"] in rules)) if ops else set()
    written = numbers_in(_DATE.sub(" ", text)) | ({0.0} if re.search(r"\bzero\b", text, re.I) else set())
    if written - allowed_numbers:
        return "number_not_grounded"
    low = _words(text)
    named = {a for a, phrase in _actor_phrases(rules).items() if f" {phrase} " in low}
    allowed_actors = {r.get("actor_ref") for r in scope.values()} | {o["desired"] for o in ops if o["kind"] == "set_actor"} | {o["before"] for o in ops if o["kind"] == "set_actor"}
    if named - allowed_actors:
        return "actor_not_grounded"
    return None


def _distinct_words(rule: Mapping, where: Optional[str]) -> set:
    text = " ".join(str(x) for x in (rule.get("title", ""), json.dumps(rule.get("scope") or {}), where or "", json.dumps(rule.get("outcome") or {})))
    return {_singular(w) for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP and len(w) > 2}


def _ambiguous_target(rule_id: str, rules: Mapping, req_norm: str, where: Optional[Mapping] = None, relations: Iterable[Mapping] = ()) -> list:
    """Contract 8: the offered rules that are indistinguishable from the chosen one for this request — same condition fields, same actor, same outcome kind — unless
    the request names a word that only one of them carries (title, scope, place, outcome). A sibling that disagrees with the chosen rule by a recorded relation
    (`conflicts_with`) is the same decision point, handled by the decided-version question, not an ambiguity. Empty when the choice is unambiguous. The server
    never picks."""
    rule = rules[rule_id]
    base = rule_id[:-len("-target")] if rule_id.endswith("-target") else rule_id
    disagreeing = set()
    for e in relations:
        # only a disagreement a person decided (CONFIRMED) settles which rule stands; an edge the analysis derived (PROPOSED / VALIDATED) is still two rules that fit
        if e.get("type") == "conflicts_with" and e.get("status") == "CONFIRMED" and base in (e.get("from"), e.get("to")):     # (UNDERSTAND-V13 U13-16)
            disagreeing.add(e["to"] if e["from"] == base else e["from"])
    fields = {leaf["field"] for leaf in _numeric_leaves(rule.get("condition"))} or {leaf["field"] for leaf in _all_leaves(rule.get("condition"))}
    if not fields:
        return []
    req_words = {_singular(w) for w in re.findall(r"[a-z]+", req_norm) if w not in _STOP and len(w) > 2}
    mine = _distinct_words(rule, (where or {}).get(rule_id))
    out = []
    for rid, r in rules.items():
        other_base = rid[:-len("-target")] if rid.endswith("-target") else rid
        if other_base == base or other_base in disagreeing or r.get("status") in ("REJECTED", "SUPERSEDED"):
            continue
        theirs_fields = {leaf["field"] for leaf in _numeric_leaves(r.get("condition"))} or {leaf["field"] for leaf in _all_leaves(r.get("condition"))}
        if not (fields & theirs_fields) or r.get("actor_ref") != rule.get("actor_ref"):     # siblings decide on the same measured field with the same actor
            continue
        if (r.get("outcome") or {}).get("kind") != (rule.get("outcome") or {}).get("kind"):
            continue
        theirs = _distinct_words(r, (where or {}).get(rid))
        if (mine - theirs) & req_words or (theirs - mine) & req_words:
            continue                                                 # the request names something only one of them has
        out.append(rid)
    return sorted(out)


_VAGUE_ACTOR = frozenset("somebody someone anybody anyone everybody everyone whoever person people someone-else somebody-else staff".split())
# personal pronouns name a person, never a role (shared with the answer reading); "us" is left out of the role-slug check only, where it may be the country
PERSONAL_PRONOUNS = frozenset({"he", "she", "they", "him", "her", "hers", "his", "them", "their", "theirs", "himself", "herself", "themselves", "themself", "i", "me",
                               "my", "myself", "we", "us", "our", "ourselves", "you", "your", "yourself"})


def _vague_actor(role: str) -> bool:
    """A role slug made only of indefinite words ("somebody", "someone else") names no one: asked, never accepted (UNDERSTAND-V12 U12-09)."""
    words = [w for w in role.replace("role-", "").split("-") if w not in ("a", "an", "the", "else")]
    # an indefinite pronoun anywhere ("someone senior", "somebody from finance") names no one; a role is a title, not a description of a person.
    # R5 (2026-10-28): nor does a personal pronoun ("She should close the slopes" written as role-she), the class fixed for answer reading in A110
    return not words or any(w in _VAGUE_ACTOR or (w in PERSONAL_PRONOUNS and w != "us") for w in words)


def _cond_fields(cond) -> set:
    if isinstance(cond, Mapping):
        if "field" in cond:
            return {cond["field"]}
        return set().union(*(_cond_fields(c) for k in ("all", "any") for c in cond.get(k, []))) | (_cond_fields(cond["not"]) if "not" in cond else set())
    return set()


def _flips_side(current, desired) -> bool:
    """A condition edit that is a bare NOT over fields the current condition (itself not a negation) already measures: the rule would apply on the other side
    of its own comparison. A NOT inside an all / any (a band, an exception-like carve-out) is not a flip and reads as before."""
    return (isinstance(desired, Mapping) and set(desired) == {"not"} and isinstance(current, Mapping) and set(current) != {"not"}
            and bool(_cond_fields(desired["not"])) and _cond_fields(desired["not"]) <= _cond_fields(current))


_UP, _DOWN = {"gte", "gt"}, {"lte", "lt"}


def _leaves_of(cond) -> list:
    if isinstance(cond, Mapping):
        if "field" in cond and "op" in cond:
            return [cond]
        return [l for v in cond.values() for l in _leaves_of(v)]
    if isinstance(cond, list):
        return [l for c in cond for l in _leaves_of(c)]
    return []


def _inverts_direction(current, desired) -> bool:
    """Hardening A109 (2026-10-27, items AEV7-U-04 / U-05): "close a slope when its score is NOT below 3" for a rule on "score < 3" was written directly as
    "score >= 3", and "approve only when the refund is NOT 200 CHF or more" for a rule on ">= 200" as "< 200": no NOT for `_flips_side` to see, yet the rule
    would apply in the opposite cases. A condition edit whose comparisons on a field the current condition measures all point the other way (up <-> down), and
    which keeps none of the current comparisons on that field, reverses the rule: asked, never proposed. A band that keeps the current limit and adds one on the
    other side (the documented "also invalid above 3,000" form) keeps the current comparison and is not a reversal; a moved threshold on the same side is not one."""
    if not isinstance(current, Mapping) or not isinstance(desired, Mapping) or set(current) == {"not"} or "not" in _cond_fields_keys(desired):
        return False
    cur = {}
    for l in _leaves_of(current):
        if l.get("op") in _UP | _DOWN:
            cur.setdefault(l.get("field"), set()).add("up" if l["op"] in _UP else "down")
    cur_keys = {(l.get("field"), l.get("op"), str(l.get("value"))) for l in _leaves_of(current)}
    for field, dirs in cur.items():
        new = [l for l in _leaves_of(desired) if l.get("field") == field and l.get("op") in _UP | _DOWN]
        if not new or any((l.get("field"), l.get("op"), str(l.get("value"))) in cur_keys for l in new):
            continue
        new_dirs = {"up" if l["op"] in _UP else "down" for l in new}
        if len(dirs) == 1 and len(new_dirs) == 1 and dirs != new_dirs:
            return True
    return False


_BOOL = {"true": "false", "false": "true"}


def _flips_boolean(current, desired) -> bool:
    """A113 (2026-10-28, AEV9-U-03): "inspect commercial connections in exactly the opposite case" for a rule on `certificate_valid eq false` was written as
    `eq "true"`: no NOT, no ordered comparison, yet the rule would apply in the opposite cases. A condition edit identical to the current condition except that
    yes / no comparisons on fields it already measures are turned around (the boolean value swapped, or eq <-> ne on the same value), and nothing else moved,
    reverses the rule: asked, never proposed. Values are compared as written (True / "true" alike)."""
    if not isinstance(current, Mapping) or not isinstance(desired, Mapping):
        return False
    flipped = [0]

    def same(a, b) -> bool:
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            if "field" in a and "op" in a and "field" in b and "op" in b:
                if a["field"] != b["field"] or (a.get("unit") or "") != (b.get("unit") or ""):
                    return False
                va, vb = str(a.get("value")).strip().lower(), str(b.get("value")).strip().lower()
                if a["op"] == b["op"] and va == vb:
                    return True
                if va in _BOOL and ((a["op"] == b["op"] and a["op"] in ("eq", "ne") and vb == _BOOL[va]) or ({a["op"], b["op"]} == {"eq", "ne"} and va == vb)):
                    flipped[0] += 1
                    return True
                return False
            return set(a) == set(b) and all(same(a[k], b[k]) for k in a)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return str(a).strip().lower() == str(b).strip().lower()
    return same(current, desired) and flipped[0] > 0


def _cond_fields_keys(cond) -> set:
    if isinstance(cond, Mapping):
        return set(cond) | {k for v in cond.values() for k in _cond_fields_keys(v)}
    if isinstance(cond, list):
        return {k for c in cond for k in _cond_fields_keys(c)}
    return set()


def _cond_key(cond):
    """A condition with every leaf value read as its written text (True / "true", 1 / "1" alike), for comparing two conditions' meaning."""
    if isinstance(cond, Mapping):
        return {k: (_cond_key(v) if k != "value" else str(v).strip().lower().replace(",", "")) for k, v in cond.items()}
    if isinstance(cond, list):
        return [_cond_key(c) for c in cond]
    return cond


def _vague_before(role: str, req_norm: str) -> bool:
    """HK-AEV U-09 (2026-10-27): "let someone from finance approve" was written as role-finance. An indefinite word followed by a preposition right before the
    role's words in the request ("someone from finance", "anybody in procurement") describes a person, not a role: asked, never accepted."""
    phrase = _words(role.replace("role-", "")).strip()
    for m in re.finditer(r"(?:^| )" + re.escape(phrase) + r"(?= |$)", _words(req_norm).strip()):
        before = _words(req_norm).strip()[: m.start()].split()[-3:]
        if len(before) >= 2 and before[-1] in ("from", "in", "at", "of", "within") and any(w in _VAGUE_ACTOR for w in before[:-1]):
            return True
    return False


def _unit_key(unit, rule_unit=None) -> Optional[str]:
    """One form for a unit, by class: aliases (yen -> JPY, celsius -> C), plural count words (hours -> hour), degree words on a rule kept in C / F (the rule's
    scale), case. None for no unit."""
    if unit is None:
        return None
    u = str(unit).strip()
    if not u:
        return None
    low = u.lower()
    if low in _UNIT_ALIASES:
        return _UNIT_ALIASES[low]
    if rule_unit in ("C", "F") and low in ("degree", "degrees", "deg", "\u00b0"):
        return rule_unit
    plain = lambda x: x[:-1] if x.endswith("s") and not x.endswith("ss") and len(x) > 3 else x   # noqa: E731  plural count words: hours / hour
    if rule_unit is not None and plain(low) == plain(str(rule_unit).lower()):
        return str(rule_unit)                                    # the same unit in another spelling ("Hour" for "hours", "jpy", "percent")
    if rule_unit is not None and plain(low.split()[-1]) == plain(str(rule_unit).lower()) and len(low.split()) > 1:
        return str(rule_unit)                                    # a qualified spelling of the rule's unit ("working days" for a rule kept in days): the rule's text defines it
    return u


def _singular(word: str) -> str:
    """quantities -> quantity, amounts -> amount (SUR03-V5D: "quantities" stripped to "quantitie" never met "quantity")."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _same(kind, before, desired) -> bool:
    if kind == "add_or_change_condition":
        return before is not None and bk.normalize_condition(before) == bk.normalize_condition(desired)
    if kind == "set_value" and isinstance(before, Mapping) and isinstance(desired, Mapping):
        return str(before["value"]) == str(desired["value"]) and before.get("unit") == desired.get("unit")
    return before == desired


def _decimals(node):
    """Condition values the contract takes: an integral float becomes an int, any other float its exact decimal string ("7.5"); the contract refuses floats
    (2026-10-24, live smoke UNDERSTAND-V11-C7: "lt 7.5" from the typed condition was refused)."""
    if isinstance(node, bool):
        return node
    if isinstance(node, float):
        return int(node) if node.is_integer() else repr(node)
    if isinstance(node, Mapping):
        return {k: (_decimals(v) if k in ("value", "any", "all", "not") else v) for k, v in node.items()}
    if isinstance(node, list):
        return [_decimals(x) for x in node]
    return node


def read_typed_condition(cond):
    """A condition given in the typed structured-output schema (contract 7), read the way a condition edit is read: unfilled keys dropped, a composite wins
    over stray leaf keys, an empty unit is no unit and a currency is written the usual way, and floats become contract values. Used by answer reading (R5)."""
    if not isinstance(cond, Mapping):
        return cond
    given = {k: v for k, v in cond.items() if v not in (None, [], "")}
    if any(k in given for k in ("any", "all", "not")):
        given = {k: v for k, v in given.items() if k in ("any", "all", "not")}
    return _decimals(_clean_units(given))


def _given(desired) -> dict:
    """The value the model gave for a refused operation, kept on the refusal (at most 300 characters of its JSON, only if it passes the plain-text check) so the
    next refusal of a new form is seen on the stored result, not inferred (A47-A54 needed extra diagnostic calls for each form)."""
    text = safe_text(json.dumps(desired, ensure_ascii=False), 300)
    return {"desired_given": text} if text else {}


def _json_text(value, depth: int = 0) -> tuple:
    """-> (value with every string that is JSON text of an object or list replaced by that structure, whether anything was replaced). Bounded depth."""
    if depth > 4:
        return value, False
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value, False
        if isinstance(parsed, (dict, list)):
            inner, _ = _json_text(parsed, depth + 1)
            return inner, True
        return value, False
    if isinstance(value, Mapping):
        pairs = {k: _json_text(v, depth + 1) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, any(c for _, c in pairs.values())
    if isinstance(value, list):
        items = [_json_text(v, depth + 1) for v in value]
        return [v for v, _ in items], any(c for _, c in items)
    return value, False


def _shape(value, depth: int = 0) -> str:
    """The form of a refused `desired` (types and keys, never values) so a refusal can be diagnosed from the stored result: `object{any:list[object{field,op,value}]}`."""
    if isinstance(value, Mapping):
        if depth >= 3:
            return "object"
        return "object{" + ",".join(f"{k}:{_shape(v, depth + 1)}" if isinstance(v, (Mapping, list)) else str(k) for k, v in sorted(value.items()))[:200] + "}"
    if isinstance(value, list):
        inner = sorted({_shape(v, depth + 1) for v in value})
        return "list[" + "|".join(inner)[:200] + "]"
    return type(value).__name__


_ABOVE = re.compile(r"\b(?:more than|above|over|greater than|larger than|higher than|exceeds?|exceeding|beyond)\s*$")
_BELOW = re.compile(r"\b(?:less than|below|under|fewer than|lower than|smaller than)\s*$")


def _bounds_the_other_side(req_norm: str, value: float, op, zero_bound: bool = False) -> bool:
    """True when the request puts the new number on the other side of the rule's comparison ("more than 7,500" for a rule that applies at "<= 0"): replacing only
    the number would turn the rule round (reject everything up to 7,500), so the edit is refused and the person asked. 2026-10-24, live run SUR03-V7 item 02:
    such a set_value was accepted. A refusal only; the server never rewrites the operation into another kind."""
    side = _ABOVE if op in ("lte", "lt") else _BELOW if op in ("gte", "gt") else None
    if side is None:
        return False
    for m in _NUM.finditer(req_norm):
        try:
            if float(m.group(0).replace(",", "")) != value:
                continue
        except ValueError:
            continue
        before = req_norm[max(0, m.start() - 40): m.start()]
        if side.search(before) or (zero_bound and _CAP.search(before)):
            return True
    return False


# a cap on a rule that rejects zero or less ("cap the quantity at 160", "a maximum of 160") is a limit on the other side too; only for that rule shape, because
# "at most 7 days" on a rule that applies at <= 5 days is the same side (2026-10-24, live run SUR03-V9 item 05: such a set_value 0 -> 160 was accepted)
_CAP = re.compile(r"\b(?:cap|caps|capped|maximum|max|ceiling|limit|at most|up to|no more than|not more than)\b[^.;]{0,30}$")


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
        if leaf.get("unit") and unit is not None:
            unit = _unit_key(unit, leaf.get("unit"))             # by class: aliases, plurals, degree words on the rule's scale (A68, contract 8)
        if leaf.get("unit") and unit != leaf.get("unit"):
            raise ValueError("unit_differs_from_the_rule")
        # only a check at exactly zero ("zero or less"): a rule at <= -2.5 C is an ordinary threshold (UNDERSTAND-V10 U10-01 was refused by the first version)
        zero_bound = leaf.get("op") in ("lte", "lt") and _is_number(leaf.get("value")) and float(str(leaf["value"]).replace(",", "")) == 0
        if _bounds_the_other_side(req_norm, float(str(value).replace(",", "")), leaf.get("op"), zero_bound):
            raise ValueError("limit_on_the_other_side")
        return {"value": leaf["value"], **({"unit": leaf["unit"]} if leaf.get("unit") else {})}, {"value": value if isinstance(value, int) else str(value).replace(",", ""), **({"unit": unit} if unit else {})}
    if kind == "set_actor":
        role = _role(desired) if isinstance(desired, str) else None
        # an apostrophe in the request ("the requester's own manager") is a hyphen in the slug: compare both with every non-word character read as a space
        # (live finding SUR08-V1 item 03, 2026-10-23: a readable actor was held as unreadable)
        if role is None or _words(role.replace("role-", "")) not in _words(req_norm):
            raise ValueError("actor_not_in_request")
        if _vague_actor(role) or _vague_before(role, req_norm):
            raise ValueError("actor_not_named")
        return rule["actor_ref"], role
    if kind == "add_or_change_condition":
        bk.validate_condition(desired)
        if numbers_in(desired) - (req_numbers | numbers_in(rule["condition"])):
            raise ValueError("number_not_in_request")
        # HK-AEV U-10 (2026-10-27): "keep rejecting deliveries warmer than 8 C" was written as NOT(the current condition), which makes the rule apply in exactly
        # the opposite cases; a reversal is asked about, never proposed (the person states it again if a reversal is really meant)
        current = rule.get("condition")
        # compared as written values (HK-AEV2 U-16: the model wrote "true" for the stored boolean true)
        if current is not None and (_cond_key(desired) == _cond_key({"not": current})
                                    or (isinstance(current, Mapping) and set(current) == {"not"} and _cond_key(desired) == _cond_key(current["not"]))):
            raise ValueError("reverses_the_rule")
        # R2-05b (2026-10-27, HK-AEV2 U-07): "should only go to the depot manager once it passes 30 hours" was written as NOT(hours > 30) for a rule on
        # "hours > 20": a bare negation over the field the rule already measures puts the rule on the other side of its comparison, whatever the new value. Asked.
        if _flips_side(current, desired) or _inverts_direction(current, desired) or _flips_boolean(current, desired):
            raise ValueError("reverses_the_rule")
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
                # compared as words: "SMALL_BUSINESS" is the request's "small business" (UNDERSTAND-V10 U10-12 was refused)
                if not isinstance(v, str) or not v.strip() or (_words(v) not in _words(req_norm) and _words(v) not in _words(known)):
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
