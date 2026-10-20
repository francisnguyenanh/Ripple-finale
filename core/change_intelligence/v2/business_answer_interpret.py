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
MODEL_KEYS = {"status", "kind", "text", "condition", "quote", "uncovered", "ruled_out", "message", "uncertainty"}
# `uncertainty` (completeness/2): the model's own report of hedged / guessed / approximate / alternative wording. Absent only in answers recorded before it existed
# (replays); absent means "not reported", never "certain": the server's own uncertainty check runs either way.
OPTIONAL_MODEL_KEYS = frozenset({"uncertainty"})
# words that may differ between the person's wording of a role and the model's ("director of operations" for "operations director"): never a name or a number
_FUNCTION_WORDS = frozenset({"the", "a", "an", "of", "for", "to", "in", "on", "at", "by"})
_NEGATION = frozenset({"not", "no", "never", "nor", "isn", "aren", "doesn", "don", "won", "t"})
# what a sentence that only rules out a party may contain besides that party and the question's subject (the verb of acting, pronouns, auxiliaries)
_RULE_OUT_FILLER = frozenset({"is", "are", "was", "were", "be", "it", "its", "them", "they", "this", "that", "who", "which", "do", "does", "did", "decide", "decides",
                              "decided", "approve", "approves", "approved", "sign", "signs", "signed", "off", "handle", "handles", "handled", "act", "acts", "approver",
                              "decider", "one", "person", "party", "team", "but", "and", "so"}) | _FUNCTION_WORDS
KINDS = ("actor", "condition", "outcome", "choose_rule_id", "exception", "accept_unknown")
_WORD = re.compile(r"[a-z0-9]+")


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or not (MODEL_KEYS - OPTIONAL_MODEL_KEYS <= set(obj) <= MODEL_KEYS) or obj["status"] not in STATUSES:
        raise ValueError("keys_or_status")
    if "uncertainty" in obj and (not isinstance(obj["uncertainty"], list) or not all(isinstance(u, str) for u in obj["uncertainty"])
                                 or len(obj["uncertainty"]) > limits["max_uncovered"]):
        raise ValueError("uncertainty")
    if obj["kind"] is not None and obj["kind"] not in KINDS:
        raise ValueError("kind")
    for k in ("text", "quote", "message"):
        if obj[k] is not None and not isinstance(obj[k], str):
            raise ValueError(k)
    if obj["condition"] is not None and not isinstance(obj["condition"], dict):
        raise ValueError("condition")
    if not isinstance(obj["uncovered"], list) or not all(isinstance(u, str) for u in obj["uncovered"]) or len(obj["uncovered"]) > limits["max_uncovered"]:
        raise ValueError("uncovered")
    if not isinstance(obj["ruled_out"], list) or not all(isinstance(u, str) for u in obj["ruled_out"]) or len(obj["ruled_out"]) > limits["max_uncovered"]:
        raise ValueError("ruled_out")
    return obj


_NOT_A_PARTY_START = frozenset({"on", "in", "at", "during", "before", "after", "above", "below", "over", "under", "until", "when", "if", "unless", "except", "between",
                                "from", "within", "without", "more", "less", "than"})
_TIME_OR_LIMIT = frozenset({"weekend", "weekends", "holiday", "holidays", "day", "days", "week", "weeks", "month", "months", "year", "years", "night", "nights",
                            "morning", "evening", "closing", "amount", "amounts", "jpy", "yen", "limit", "threshold", "emergency", "standard"})


def _is_party(phrase: str) -> bool:
    """A ruled-out phrase must name a party, not a time, an amount or a condition: the model's label alone never hides an exception (it then stays uncovered)."""
    words = _WORD.findall(_norm(phrase))
    while words and words[0] in ("by", "the", "a", "an"):
        words = words[1:]
    return bool(words) and words[0] not in _NOT_A_PARTY_START and not set(words) & _TIME_OR_LIMIT


def _only_rules_out(segment: str, ruled_out: list, subject_words: set) -> bool:
    """True when `segment` only says which party does NOT act: it holds a negation and a ruled-out party, and nothing else but the question's subject and the
    words of acting. A negation that limits when or how ("not on weekends", "not above 1,000,000 JPY") keeps a word of its own and stays uncovered."""
    words = _words(segment)
    if not words & _NEGATION or numbers_in(segment):
        return False
    parties = [_words(r) for r in ruled_out if _words(r) and _words(r) <= words]
    if not parties:
        return False
    rest = words - _NEGATION - _RULE_OUT_FILLER - subject_words - set().union(*parties)
    return not rest


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


def _refused_condition(cond, detail: str, limits: Mapping) -> dict:
    """A condition the grammar refused, as plain JSON text with the refusal detail (bounded): the person's own words are already in the job; nothing here is applied."""
    import json as _json
    try:
        shown = _json.dumps(cond, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        shown = repr(cond)
    cap = int(limits["max_quote_chars"])
    return {"kind": "condition", "condition_json": shown[:cap], "detail": " ".join(detail.split())[:cap]}


def resolve(obj: Mapping, text: str, gap: Mapping, rules: Mapping[str, Mapping], limits: Mapping, *, known_fields: Optional[set] = None) -> dict:
    """The server's reading: `{status, answer, quote, uncovered, reasons, message}`. `answer` is the object for `/v2/business/gaps/answer-preview`, or None.
    `known_fields`: the condition fields offered to the model (`select_context`); by default the fields of `rules`."""
    reasons, uncovered = [], []
    rejected_reading = None
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
            if not isinstance(value, str) or not (_words(value) - _FUNCTION_WORDS) or not (_words(value) - _FUNCTION_WORDS) <= text_words:
                reasons.append("value_not_in_text")                              # every name word comes from the person; only function words may differ
                rejected_reading = {"kind": kind, "text": safe_text(value, limits["max_quote_chars"]) if isinstance(value, str) else None}
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
                    rejected_reading = _refused_condition(cond, str(exc), limits)      # kept for diagnosis (v5 V5-IF-04: the refused form was not recorded)
                else:
                    unknown = sorted(condition_fields([{"condition": cond}]) - (condition_fields(rules.values()) if known_fields is None else set(known_fields)))
                    bad = _text_values_in(cond, text_words, text_numbers)
                    if unknown:
                        reasons.append("field_not_used_by_any_rule:" + ",".join(unknown))
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
    ruled_out = []
    if answer is not None and "actor" in answer:
        actor_words = _words(answer["actor"])
        ruled_out = [t for t in (safe_text(r, limits["max_quote_chars"]) for r in obj["ruled_out"])
                     if t and _norm(t) in text_norm and not numbers_in(t) and _is_party(t)
                     and not (_words(t) - _FUNCTION_WORDS) <= (actor_words - _FUNCTION_WORDS)]   # a party other than the actor read; never an amount, time or condition
        subject = set().union(*[_words(rules[r].get("title") or "") for r in gap.get("affected_rule_ids", []) if r in rules] or [set()])
        uncovered = [u for u in uncovered if not _only_rules_out(u, ruled_out, subject)]
    if answer is not None:
        for s in _segments(text):
            if not _covered(s, [quote]) and not any(_norm(u) in _norm(s) or _norm(s) in _norm(u) for u in uncovered):
                if ruled_out and _only_rules_out(s, ruled_out, subject):
                    continue                                                     # "not procurement": who does NOT act is answered by naming who does
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
    model_uncertainty = None if "uncertainty" not in obj else [t for t in (safe_text(u, limits["max_quote_chars"]) for u in obj["uncertainty"]) if t]
    return {"status": status, "answer": answer, "quote": quote if answer is not None else None, "uncovered": uncovered, "reasons": reasons, "message": message,
            "ruled_out": ruled_out, "rejected_reading": rejected_reading, "model_uncertainty": model_uncertainty}


# ---- completeness validation (owner decision DEC-0006, 2026-10-19): a separate server step after the model's reading -----------------------------------------------------
# The model interprets; this step decides whether the person's words are a fact complete enough to CONFIRM; the person confirms. Uncertain wording or a vague reference
# ("the usual way", "as usual", "the standard process") is not a confirmable fact: the reading stays visible, its status becomes NEEDS_CLARIFICATION with the reason and
# the words that need clarifying. No confirmed business default exists that could resolve a vague reference in this build, so a vague reference is never resolved by the
# server; it always needs the person's clarification.
# completeness/2 (finding V4-IF-03, "Possibly the duty producer." read READY): uncertainty is checked as CLASSES of wording, not as a list of a few words, and from two
# independent sides: the model's own `uncertainty` report and the server's detector below (belief / memory, "not sure", modal adverbs, modal verbs of possibility,
# approximations, open alternatives, an answer given as a question). Text the person only cites in quotation marks, a denial of doubt ("no doubt", "not a guess") and an
# earlier answer the person explicitly takes back ("I said maybe before, but ...") are not uncertainty of this answer. Either side can block READY; neither can make an
# uncertain answer confirmable. The same check runs again when the session status is derived and when the person confirms (`gap_interview.assess`).
COMPLETENESS_VERSION = "kno07-completeness/2"
_VAGUE = re.compile(r"(?i)\b(the\s+usual(?:\s+(?:way|process|route|procedure|people|person|steps?|thing|rules?|team))?|as\s+(?:usual|always|normal)|like\s+(?:always|usual|before|normal)|"
                    r"the\s+(?:normal|standard|regular)\s+(?:way|process|procedure|route)|same\s+as\s+(?:before|always|usual)|(?:the\s+)?same\s+thing\s+(?:as\s+)?(?:we|they)\s+(?:always|usually|normally|typically)\s+do|"
                    r"what(?:ever)?\s+(?:we|they)\s+(?:always|usually|normally|typically)\s+do|in\s+the\s+usual\s+manner|whatever\s+is\s+usual|it\s+depends|and\s+so\s+on|"
                    r"the\s+(?:relevant|appropriate|responsible)\s+(?:person|people|team|department|manager))\b")
_QUOTED = re.compile(r"\"[^\"\n]{1,200}\"|“[^”\n]{1,200}”|‘[^’\n]{1,200}’|(?<![A-Za-z0-9])'[^'\n]{1,200}'(?![A-Za-z0-9])")
_DENIED_DOUBT = re.compile(r"(?i)\b(?:no\s+doubt|without\s+(?:a\s+|any\s+)?doubt|beyond\s+(?:any\s+)?doubt|not\s+(?:a\s+)?guess(?:ing)?|not\s+(?:just\s+)?(?:an?\s+)?assumption|"
                           r"(?:i\s*'?\s*a?m|we\s*'?\s*re|we\s+are)\s+not\s+(?:unsure|uncertain|guessing))\b")
_RETRACTED = re.compile(r"(?i)\b(?:(?:earlier|before|first|previously)\s+)?(?:i|we)\s+(?:said|wrote|answered|told\s+you)\b[^.;!?]*?\bbut\b")
_UNCERTAINTY = (
    ("belief_or_memory", re.compile(r"(?i)\b(?:(?:i|we)\s+(?:do\s+not\s+|don\s*'?\s*t\s+)?(?:think|guess|believe|suppose|assume|reckon|suspect|imagine|presume|gather|feel\s+like)|"
                                    r"i\s*'?\s*d\s+(?:say|guess|think)|i\s+would\s+(?:say|guess|think)|(?:as|so)\s+far\s+as\s+(?:i|we)\s+(?:know|remember|recall|can\s+tell)|"
                                    r"if\s+i\s+(?:remember|recall)(?:\s+(?:correctly|right(?:ly)?))?|from\s+(?:my\s+)?memory|"
                                    r"(?:i|we)\s+(?:forget|can\s*'?\s*t\s+(?:remember|recall)|cannot\s+(?:remember|recall)|do\s+not\s+(?:remember|recall)|don\s*'?\s*t\s+(?:remember|recall))|"
                                    r"last\s+(?:i|we)\s+(?:heard|checked)|(?:i|we)\s+(?:was|were|have\s+been)\s+told|(?:i|we)\s+heard)\b")),
    ("not_sure", re.compile(r"(?i)\b(?:(?:i\s*'?\s*a?m|we\s*'?\s*re|we\s+are)\s+)?(?:not\s+(?:entirely\s+|completely\s+|totally\s+|really\s+|quite\s+|a\s+hundred\s+percent\s+)?(?:sure|certain|positive|confident)"
                            r"|unsure|uncertain|not\s+100\s*%|(?:pretty|fairly|quite|almost|reasonably|mostly)\s+(?:sure|certain|confident))(?:\s+which(?:\s+one)?)?")),
    ("modal_adverb", re.compile(r"(?i)\b(?:possibly|probably|perhaps|maybe|presumably|likely|unlikely|conceivably|seemingly|apparently|supposedly|reportedly|allegedly|"
                                r"potentially|arguably|hopefully|in\s+theory|in\s+principle)\b")),
    ("modal_verb", re.compile(r"(?i)\b(?:might|(?:may|could)\s+(?:well\s+)?(?:be|have|also|possibly|depend|vary|apply|change|need))\b")),
    ("approximation", re.compile(r"(?i)(?:\b(?:around|about|roughly|approximately|circa|nearly|almost|something\s+like|in\s+the\s+region\s+of|somewhere\s+(?:around|between)|"
                                 r"give\s+or\s+take)\s+(?:[$¥€£]\s*)?\d(?:[\d,]*\d)?|\d[\d,.]*\s*(?:-\s*ish\b|ish\b|or\s+so\b|give\s+or\s+take\b)|\bmore\s+or\s+less\b|\bsort\s+of\b|\bsorta\b|\bkinda\b|\bor\s+so\b)")),
    ("open_alternative", re.compile(r"(?i)\b(?:or\s+(?:something|someone|somebody|whoever|whatever|similar|thereabouts)(?:\s+like\s+that)?|one\s+of\s+(?:them|those|the\s+two)|either\s+one|"
                                    r"i\s+forget\s+which)\b")),
)
_EMPTY_REPORT = frozenset({"", "none", "n/a", "na", "no", "nothing", "null", "-"})


def _masked(text: str) -> str:
    """The person's words with cited quotations, denials of doubt and explicitly retracted earlier answers blanked out (same length, so positions still map)."""
    out = text
    for pat in (_QUOTED, _DENIED_DOUBT, _RETRACTED):
        out = pat.sub(lambda m: " " * len(m.group(0)), out)
    return out


def uncertainty_problems(text: str, answer: Optional[Mapping] = None, *, quote: Optional[str] = None, model_uncertainty: Optional[list] = None) -> list:
    """Every reason the person's words are not a confirmable fact: `[{code, signal, words}]` sorted by position; [] when the words state it plainly.
    `code` is hedged_answer (uncertainty of any class) or vague_reference; `words` is the exact text (None only for a model report that quotes nothing of the answer)."""
    text = text or ""
    masked = _masked(text)
    found = []
    for signal, pat in _UNCERTAINTY:
        found += [(m.start(), {"code": "hedged_answer", "signal": signal, "words": text[m.start():m.end()].strip()}) for m in pat.finditer(masked)]
    quote_words = _words(quote) if quote else set()
    for m in re.finditer(r"[^.!?;\n]*\?", masked):                                     # an answer given as a question ("The duty producer?") is not a statement
        seg = text[m.start():m.end()].strip()
        if seg and (not quote_words or _words(seg) & quote_words):
            found.append((m.start(), {"code": "hedged_answer", "signal": "question_form", "words": seg}))
    for m in _VAGUE.finditer(masked):
        found.append((m.start(), {"code": "vague_reference", "signal": "vague_reference", "words": text[m.start():m.end()].strip()}))
    values = " ".join(str(v) for v in (answer or {}).values() if isinstance(v, str))
    for m in _VAGUE.finditer(values):                                                  # a vague value the model put into the reading itself
        if _norm(m.group(0)) not in _norm(text):
            found.append((len(text), {"code": "vague_reference", "signal": "vague_reference", "words": m.group(0).strip()}))
    text_n, masked_n = _norm(text), _norm(masked)
    for u in model_uncertainty or []:
        un = _norm(u)
        if un in _EMPTY_REPORT:
            continue
        if un in text_n and un not in masked_n:
            continue                                                                   # the model flagged words the person only cites in quotes / denies / took back
        pos = text_n.find(un)
        found.append((pos if pos >= 0 else len(text), {"code": "hedged_answer", "signal": "model_reported", "words": u if pos >= 0 else None}))
    out, seen = [], set()
    for _, p in sorted(found, key=lambda x: x[0]):
        key = (p["code"], _norm(p["words"]) if p["words"] else None)
        if key[1] and any(k[0] == key[0] and k[1] and key[1] in k[1] for k in seen):
            continue                                                                   # already covered by a longer phrase of the same kind
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _problem_text(p: Mapping) -> str:
    if p["code"] == "vague_reference":
        return f"\"{p['words']}\" does not say exactly what happens or who acts"
    if p["words"] is None:
        return "the reading found the answer uncertain"
    return f"you were not sure (\"{p['words']}\")"


def completeness(out: Mapping, text: str) -> dict:
    """`out` of `resolve` -> the same with `completeness` set; a reading over uncertain or vague words is never READY (it becomes NEEDS_CLARIFICATION). A reading that was
    already NEEDS_CLARIFICATION keeps its status but also lists the problems, so the follow-up asks about them."""
    out = dict(out)
    answer = out.get("answer")
    if not answer or "accept_unknown" in answer:
        out["completeness"] = {"version": COMPLETENESS_VERSION, "complete": out.get("status") == "READY", "problems": []}
        return out
    problems = uncertainty_problems(text, answer, quote=out.get("quote"), model_uncertainty=out.get("model_uncertainty"))
    was_ready = out.get("status") == "READY"
    out["completeness"] = {"version": COMPLETENESS_VERSION, "complete": was_ready and not problems, "problems": problems}
    if problems:
        out["status"] = "NEEDS_CLARIFICATION"
        out["reasons"] = list(dict.fromkeys(list(out.get("reasons") or []) + [p["code"] for p in problems]))
        out["uncovered"] = list(dict.fromkeys(list(out.get("uncovered") or []) + [p["words"] for p in problems if p["words"] and _norm(p["words"]) in _norm(text)]))
        if was_ready or not out.get("message"):
            out["message"] = ("The reading is shown, but it cannot be confirmed: " + "; ".join(dict.fromkeys(_problem_text(p) for p in problems))
                              + ". Please say it exactly, or say that you do not know.")
    return out


# ---- reading context (finding V4-IF-04 / V3-IV-07): which rules and condition fields the model sees ----------------------------------------------------------
# Before: the gap's rules + the other rules sorted by id, cut at `max_known_rules`, and only the cut rules' fields offered, so a field the answer needed
# (`booking.guests`) could vanish without a sign. Now: the field vocabulary is taken from EVERY active rule of the knowledge version (it is small; a cap still applies and is
# reported), the gap's own rules always come first, the other rules are ranked by relevance to the question and the person's words (field and title words they use),
# by dependency (a shared condition field or concept with the gap's rules, where counter-evidence lives) and by scope, and every cut is reported in `coverage` so the
# reading and the screen can say that the model saw only part of the knowledge. Nothing is invented: only existing rules and fields are offered.
CONTEXT_VERSION = "kno07-context/2"
_STOP = frozenset({"the", "a", "an", "of", "for", "to", "in", "on", "at", "by", "is", "are", "be", "it", "this", "that", "and", "or", "with", "when", "who", "what", "which", "rule",
                   "rules", "does", "do", "than", "more", "less", "there", "if", "only", "all", "any", "has", "have", "say", "you", "your", "not", "know"})


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _tokens(text) -> set:
    return {_stem(w) for w in _WORD.findall(_norm(str(text or "")).replace("_", " ").replace(".", " ")) if w not in _STOP and not w.isdigit()}


def _rule_line(r: Mapping) -> str:
    return (f"- id={r['rule_id']} actor={r.get('actor_ref')} outcome={(r.get('outcome') or {}).get('kind')}\n  title: {r['title']}\n  condition: {r.get('condition')}\n"
            f"  exceptions: {[e['text'] for e in r.get('exceptions') or []]}")


def _scope_key(r: Mapping) -> str:
    return str(sorted((r.get("scope") or {}).items()))


def select_context(gap: Mapping, rules: Mapping[str, Mapping], text: str, limits: Mapping) -> tuple:
    """-> (shown rules in prompt order, offered field vocabulary, coverage). `rules` = the active rules of the version the person answers against."""
    max_rules, max_chars = int(limits["max_known_rules"]), int(limits["max_prompt_chars"])
    max_fields = int(limits.get("max_fields", 80))
    affected = [rules[rid] for rid in gap.get("affected_rule_ids", []) if rid in rules]
    aff_ids = {r["rule_id"] for r in affected}
    aff_fields = condition_fields(affected)
    aff_concepts = set().union(*[set(r.get("concept_ids") or []) for r in affected] or [set()])
    aff_scopes = {_scope_key(r) for r in affected}
    asked = _tokens(text)
    topic = _tokens(gap.get("question")) | set().union(*[_tokens(r.get("title")) for r in affected] or [set()])

    def score(r: Mapping) -> tuple:
        fields = condition_fields([r])
        ftoks = set().union(*[_tokens(f) for f in fields] or [set()])
        title = _tokens(r.get("title"))
        s = 3 * len(asked & ftoks) + 2 * len(asked & title)                                    # the person's own words name the rule's field or subject
        s += 2 * bool(fields & aff_fields) + 2 * bool(set(r.get("concept_ids") or []) & aff_concepts)    # dependency / counter-evidence of the gap's rules
        s += len(topic & (title | ftoks)) + (1 if _scope_key(r) in aff_scopes else 0)
        return (-s, r["rule_id"])
    others = sorted((r for rid, r in rules.items() if rid not in aff_ids), key=score)
    shown, used, cut_by_chars = [], 0, False
    for r in affected + others:
        line = len(_rule_line(r))
        if len(shown) >= max_rules or used + line > max_chars:
            cut_by_chars = cut_by_chars or (len(shown) < max_rules)
            if r["rule_id"] in aff_ids:
                continue                                                                        # counted below as an affected rule not shown
            break
        shown.append(r)
        used += line
    vocab = condition_fields(rules.values())
    shown_fields = condition_fields(shown)
    ranked = sorted(vocab, key=lambda f: (f not in shown_fields, -len(asked & _tokens(f)), f not in aff_fields, f))
    offered = sorted(ranked[:max_fields])
    shown_ids = {r["rule_id"] for r in shown}
    coverage = {"version": CONTEXT_VERSION, "rules_total": len(rules), "rules_shown": len(shown), "rules_truncated": len(shown) < len(rules),
                "cut_by": ("prompt_chars" if cut_by_chars else "max_known_rules") if len(shown) < len(rules) else None,
                "affected_rules_not_shown": sorted(aff_ids - shown_ids), "fields_total": len(vocab), "fields_offered": len(offered),
                "fields_truncated": len(offered) < len(vocab), "fields_offered_without_shown_rule": sorted(set(offered) - shown_fields)}
    return shown, set(offered), coverage


def coverage_note(coverage: Optional[Mapping]) -> str:
    if not coverage:
        return "All rules of the knowledge version are shown."
    parts = [f"Rules shown: {coverage['rules_shown']} of {coverage['rules_total']} (the question's own rules first, then the most relevant)."]
    if coverage["affected_rules_not_shown"]:
        parts.append(f"{len(coverage['affected_rules_not_shown'])} of the question's own rules did not fit and are not shown.")
    parts.append(f"Condition fields listed: {coverage['fields_offered']} of {coverage['fields_total']}"
                 + (" (the list is partial)." if coverage["fields_truncated"] else " (the complete list; a field may belong to a rule not shown)."))
    if coverage["rules_truncated"] or coverage["fields_truncated"]:
        parts.append("If the answer needs a rule or field that is not here, set status NEEDS_CLARIFICATION and say so in message; never invent it.")
    return " ".join(parts)


def build_prompt(frozen: Mapping, gap: Mapping, rules: Iterable[Mapping], text: str, *, max_chars: int, fields: Optional[set] = None, coverage: Optional[Mapping] = None) -> str:
    rules = list(rules)
    lines, used = [], 0
    for r in rules:
        line = _rule_line(r)
        if used + len(line) > max_chars:
            raise ValueError("context_selection_exceeds_prompt_chars")                    # select_context sizes the rules; a silent cut here would hide a rule
        lines.append(line)
        used += len(line)
    options = "\n".join(f"- {o['rule_id']}: {o.get('label')}" for o in gap.get("options") or []) or "(none)"
    fields = ", ".join(sorted(condition_fields(rules) if fields is None else fields)) or "(none)"
    return (frozen["prompt_template"].replace("{gap_kind}", gap["kind"]).replace("{question}", gap["question"]).replace("{options}", options)
            .replace("{fields}", fields).replace("{coverage}", coverage_note(coverage)).replace("{rules}", "\n".join(lines) or "(none)").replace("{text}", text))
