# -*- coding: utf-8 -*-
"""A person's free-text answer to one gap, read by a model into the structured answer of that gap (KNO-07, pure, no I/O).

The model proposes one structured reading and quotes the words it read it from. The server keeps the reading only if the quote is in the text, the reading fits the
gap's kind, every value comes from the person's words (names, numbers, rule options) or from the condition fields the shown rules already use, and the grammar is the
Final condition grammar. Words of the answer that no quote covers make the result NEEDS_CLARIFICATION with those words shown: a partial reading is never silently
applied. The kept reading is exactly the `answer` object the existing preview/confirm path takes, so the person sees the same preview and confirms the same way.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_answers as ba
from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.business_change import _covered, _norm, _segments, numbers_in, safe_text

STATUSES = ("OK", "NEEDS_CLARIFICATION", "UNSUPPORTED")
MODEL_KEYS = {"status", "kind", "text", "condition", "quote", "uncovered", "message"}
KINDS = ("actor", "condition", "outcome", "choose_rule_id", "exception", "accept_unknown")
_WORD = re.compile(r"[a-z0-9]+")


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != MODEL_KEYS or obj["status"] not in STATUSES:
        raise ValueError("keys_or_status")
    if obj["kind"] is not None and obj["kind"] not in KINDS:
        raise ValueError("kind")
    for k in ("text", "quote", "message"):
        if obj[k] is not None and not isinstance(obj[k], str):
            raise ValueError(k)
    if obj["condition"] is not None and not isinstance(obj["condition"], dict):
        raise ValueError("condition")
    if not isinstance(obj["uncovered"], list) or not all(isinstance(u, str) for u in obj["uncovered"]) or len(obj["uncovered"]) > limits["max_uncovered"]:
        raise ValueError("uncovered")
    return obj


def condition_fields(rules: Iterable[Mapping]) -> set:
    """The condition fields the shown rules already use: the only fields a read condition may name (a person does not type field names)."""
    out = set()

    def walk(node):
        if not isinstance(node, Mapping):
            return
        if "all" in node or "any" in node:
            for c in node.get("all") or node.get("any") or []:
                walk(c)
        elif "not" in node:
            walk(node["not"])
        elif "field" in node:
            out.add(node["field"])
    for r in rules:
        walk(r.get("condition"))
        for e in r.get("exceptions") or []:
            walk(e.get("condition"))
    return out


def _words(text: str) -> set:
    return set(_WORD.findall(_norm(text)))


def _mentions_amount(numbers: set, text_words: set, text_numbers: set) -> bool:
    """True when the person's words name one of `numbers`, written out or as "5 million" / "500 thousand" / "2k"."""
    for n in numbers:
        if n in text_numbers:
            return True
        if ("million" in text_words or "m" in text_words) and n / 1e6 in text_numbers:
            return True
        if ("thousand" in text_words or "k" in text_words) and n / 1e3 in text_numbers:
            return True
    return False


def _text_values_in(cond, text_words: set, text_numbers: set) -> Optional[str]:
    """None when every literal of the condition is in the person's words; else the first literal that is not."""
    if "all" in cond or "any" in cond:
        for c in cond.get("all") or cond.get("any") or []:
            bad = _text_values_in(c, text_words, text_numbers)
            if bad:
                return bad
        return None
    if "not" in cond:
        return _text_values_in(cond["not"], text_words, text_numbers)
    values = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
    for v in values:
        if isinstance(v, bool):
            continue                                                 # yes/no readings have no literal to find
        if isinstance(v, (int, float)):
            if float(v) not in text_numbers:
                return str(v)
        else:
            if not _words(str(v)) <= text_words:
                return str(v)
    return None


_PRONOUNS = frozenset({"he", "she", "they", "it", "him", "her", "them", "someone", "somebody", "whoever", "this person", "that person", "the person"})
_ALTERNATIVE = __import__("re").compile(r"(?i)(?<![a-z])(or|either|and/or)(?![a-z])")


def actor_ambiguity(value: str) -> str | None:
    """A named actor must be one identifiable role: a bare pronoun has no referent in the answer; 'X or Y' / 'either' names two candidates for one slot.
    (Live finding 2026-10-17: a reading took 'He' and 'plant manager or the logistics lead' as actors.)"""
    v = " ".join(str(value).lower().split())
    for art in ("the ", "a ", "an "):
        if v.startswith(art) and v[len(art):] in _PRONOUNS:
            return "ambiguous_pronoun"
    if v in _PRONOUNS:
        return "ambiguous_pronoun"
    if _ALTERNATIVE.search(v):
        return "alternative_actors"
    return None


def resolve(obj: Mapping, text: str, gap: Mapping, rules: Mapping[str, Mapping], limits: Mapping) -> dict:
    """The server's reading: `{status, answer, quote, uncovered, reasons, message}`. `answer` is the object for `/v2/business/gaps/answer-preview`, or None."""
    reasons, uncovered = [], []
    message = safe_text(obj["message"], limits["max_message_chars"])
    quote = safe_text(obj["quote"], limits["max_quote_chars"]) if obj["quote"] else None
    text_norm, text_words, text_numbers = _norm(text), _words(text), numbers_in(text)
    for u in obj["uncovered"]:
        t = safe_text(u, limits["max_quote_chars"])
        if t and _norm(t) in text_norm:
            uncovered.append(t)
    answer = None
    if obj["status"] == "UNSUPPORTED":
        reasons.append("model_unsupported")
    elif obj["kind"] is None:
        reasons.append("no_reading")
    elif quote is None or _norm(quote) not in text_norm:
        reasons.append("quote_not_in_text")
    else:
        kind, value, cond = obj["kind"], obj["text"], obj["condition"]
        expected = "accept_unknown" if kind == "accept_unknown" else ba.ANSWER_KEYS.get(gap["kind"])
        if expected is None:
            reasons.append("gap_kind_not_answerable")
        elif kind != expected:
            reasons.append("reading_does_not_fit_the_question")
        elif kind == "accept_unknown":
            answer = {"accept_unknown": True, "reason": quote}
        elif kind in ("actor", "outcome", "exception"):
            if not isinstance(value, str) or not _words(value) or not _words(value) <= text_words:
                reasons.append("value_not_in_text")
            elif kind == "actor" and actor_ambiguity(value):
                reasons.append(actor_ambiguity(value))                         # NEEDS_CLARIFICATION: the person names the one role, the reader never picks
            else:
                answer = {kind: " ".join(value.split())}
        elif kind == "choose_rule_id":
            options = {o["rule_id"]: o for o in gap.get("options") or []}
            if value not in options or value not in gap["affected_rule_ids"]:
                reasons.append("rule_not_offered")
            else:
                label = options[value].get("label") or ""
                if numbers_in(label) and not _mentions_amount(numbers_in(label), text_words, text_numbers) and not _words(label) & text_words:
                    reasons.append("value_not_in_text")                      # the chosen option is not what the person said
                else:
                    answer = {"choose_rule_id": value}
        elif kind == "condition":
            if not isinstance(cond, Mapping):
                reasons.append("condition_missing")
            else:
                try:
                    bk.validate_condition(cond)
                except bk.ContractError as exc:
                    reasons.append(f"unsupported_grammar:{exc.code}")
                else:
                    unknown = sorted(condition_fields([{"condition": cond}]) - condition_fields(rules.values()))
                    bad = _text_values_in(cond, text_words, text_numbers)
                    if unknown:
                        reasons.append("field_not_used_by_any_shown_rule:" + ",".join(unknown))
                    elif bad is not None:
                        reasons.append("value_not_in_text")
                    else:
                        answer = {"condition": cond}
    if answer is not None:
        try:
            ba.normalize_answer(gap, answer)
        except bk.ContractError as exc:
            reasons.append(f"not_a_valid_answer:{exc.code}")
            answer = None
    if answer is not None:
        for s in _segments(text):
            if not _covered(s, [quote]) and not any(_norm(u) in _norm(s) or _norm(s) in _norm(u) for u in uncovered):
                uncovered.append(s)
        if "accept_unknown" in answer:
            uncovered = []                                                   # leaving the question open covers everything that was said
    uncovered = list(dict.fromkeys(uncovered))
    if answer is None:
        status = "UNSUPPORTED" if reasons and reasons[0].startswith(("model_unsupported", "unsupported_grammar", "gap_kind_not_answerable")) else "NEEDS_CLARIFICATION"
    else:
        status = "NEEDS_CLARIFICATION" if uncovered or obj["status"] != "OK" else "READY"
    if status != "READY" and message is None:
        message = {"NEEDS_CLARIFICATION": "Part of the answer could not be read safely. Please answer with the controls or say it again more plainly.",
                   "UNSUPPORTED": "This answer cannot be turned into the question's structured form."}[status]
    return {"status": status, "answer": answer, "quote": quote if answer is not None else None, "uncovered": uncovered, "reasons": reasons, "message": message}


def build_prompt(frozen: Mapping, gap: Mapping, rules: Iterable[Mapping], text: str, *, max_chars: int) -> str:
    lines, used = [], 0
    for r in rules:
        line = (f"- id={r['rule_id']} actor={r.get('actor_ref')} outcome={(r.get('outcome') or {}).get('kind')}\n  title: {r['title']}\n  condition: {r.get('condition')}\n"
                f"  exceptions: {[e['text'] for e in r.get('exceptions') or []]}")
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    options = "\n".join(f"- {o['rule_id']}: {o.get('label')}" for o in gap.get("options") or []) or "(none)"
    fields = ", ".join(sorted(condition_fields(rules))) or "(none)"
    return (frozen["prompt_template"].replace("{gap_kind}", gap["kind"]).replace("{question}", gap["question"]).replace("{options}", options)
            .replace("{fields}", fields).replace("{rules}", "\n".join(lines) or "(none)").replace("{text}", text))
