# -*- coding: utf-8 -*-
"""Business-analyst interview over ONE open question (AIX-K2, extends KNO-07): pure, no I/O, no model.

A finite conversation: the server asks one clarifying question per turn (deterministic from the question's kind and the previous reading; every question offers "I do not know"),
the person answers in their own words, the KNO-07 reading turns the answer into the structured form with a preview, uncovered parts and contradictions between turns are shown
instead of letting the last message win, and the person confirms through the existing decision path with their own words. At the turn cap the session is `UNRESOLVED` and the
structured controls take over. The questioner is deterministic in this build (a model questioner is a later, pinned role); the only model call per turn is the KNO-07 reading.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-gap-interview/1"
STATES = ("OPEN", "READY_TO_CONFIRM", "UNRESOLVED", "CONFIRMED", "STALE", "CLOSED")
DEFAULT_CAP = 5
# An EXPLICIT "do not know" stops the interview; a hedge ("I am not sure which") does not (owner decision DEC-0006): it is read, judged incomplete by the completeness step
# and gets a bounded follow-up while a useful question remains.
UNKNOWN = re.compile(r"\b(i|we)\s+(do\s+not|don't|dont|cannot|can't)\s+(know|say)\b|\bunknown\b|\bno idea\b|\bi\s+have\s+no\s+idea\b", re.I)
UNRESOLVED_REASONS = ("unknown_declared", "stopped_by_person", "turn_cap_reached", "no_remaining_clarification")
_KIND_QUESTION = {
    "missing_actor": "Who acts on the rule \"{title}\"? Name the role (for example the manager or the finance controller), or say that you do not know.",
    "missing_condition": "When exactly does the rule \"{title}\" apply? Give the amount, category or condition in your own words, or say that you do not know.",
    "missing_outcome": "What happens when the rule \"{title}\" applies (an approval, a review, a block, a charge)? Or say that you do not know.",
    "missing_exception": "Is there an exception to the rule \"{title}\"? Describe when it does not apply, or say there is none, or that you do not know.",
    "conflicting_facts": "The sources disagree about \"{title}\". Which value is the business rule: {options}? Or say that you do not know.",
}


def session_id_for(workspace_id: str, gap_id: str, snapshot_id: str) -> str:
    return "ivw-" + canonical_hash({"ws": workspace_id, "gap": gap_id, "snap": snapshot_id})[:20]


def is_unknown(text: str) -> bool:
    return bool(UNKNOWN.search(text or ""))


def first_question(gap: Mapping) -> dict:
    title = ", ".join(a.get("title") or a.get("rule_id", "") for a in gap.get("affected", [])) or ", ".join(gap.get("affected_rule_ids", []))
    options = " or ".join(f"{o.get('label')}" for o in gap.get("options", [])) if gap.get("options") else "one of the values shown"
    text = _KIND_QUESTION.get(gap.get("kind"), "Please answer the open question \"{title}\" in your own words, or say that you do not know.").format(title=title, options=options)
    return {"turn": 0, "question": text, "decision": gap.get("kind"), "offers_unknown": True}


def next_question(gap: Mapping, turns: Iterable[Mapping]) -> Optional[dict]:
    """The next question after the last ANSWERED turn with a reading; None when nothing more should be asked (ready to confirm, unknown declared, or cap)."""
    turns = [t for t in turns if t.get("text") is not None]
    if not turns:
        return first_question(gap)
    last = turns[-1]
    if last.get("unknown"):
        return None
    reading = last.get("reading") or {}
    status = reading.get("status")
    n = len(turns)
    contradiction = contradictions(turns)
    if contradiction:
        c = contradiction[-1]
        return {"turn": n, "question": f"Earlier you said {c['values'][0]!r} and now {c['values'][1]!r} for {c['field']}. Which one is the rule? Or say that you do not know.", "decision": "contradiction", "offers_unknown": True}
    a = assess(last)
    if a["confirmable"]:
        return None
    problems = a["problems"]
    codes = {p.get("code") for p in problems} | set(reading.get("reasons") or [])
    if status in ("READY", "NEEDS_CLARIFICATION") and "hedged_answer" in codes:
        words = next((p["words"] for p in problems if p.get("code") == "hedged_answer" and p.get("words")), "not sure")
        read_as = _answer_words(reading.get("proposed_answer"))
        if read_as:                                                                              # name the uncertain reading; do not make the person guess to finish
            return {"turn": n, "question": f"You said \"{words}\", so \"{read_as}\" is not certain yet. Is that the rule? Confirm it in your own words, name the one that applies, "
                    "or say that you do not know.", "decision": "uncovered_part", "offers_unknown": True}
        return {"turn": n, "question": f"You said \"{words}\". Which exactly is it? Name the one answer that applies, or say that you do not know.", "decision": "uncovered_part",
                "offers_unknown": True}
    if status in ("READY", "NEEDS_CLARIFICATION") and "vague_reference" in codes:
        words = next((p["words"] for p in problems if p.get("code") == "vague_reference" and p.get("words")), "that")
        return {"turn": n, "question": f"You wrote \"{words}\". What exactly does that mean for this rule? Say it in concrete words (who acts, what happens or when it applies), or say that you do not know.",
                "decision": "uncovered_part", "offers_unknown": True}
    if status == "NEEDS_CLARIFICATION":
        unc = "; ".join(reading.get("uncovered") or [])
        return {"turn": n, "question": f"You also wrote: \"{unc}\". Is that an exception to the rule, a condition of it, or a separate rule? If it is an exception, say exactly when it applies, or say that you do not know.",
                "decision": "uncovered_part", "offers_unknown": True}
    if status == "UNSUPPORTED":
        q = first_question(gap)
        return {"turn": n, "question": "That answer did not fit the question. " + q["question"], "decision": gap.get("kind"), "offers_unknown": True}
    return {"turn": n, "question": first_question(gap)["question"], "decision": gap.get("kind"), "offers_unknown": True}


_ANSWER_FIELD = {"actor": "who", "condition": "when", "outcome": "what happens", "value": "value"}


def _answer_words(answer) -> str:
    """A proposed answer in words ("who: finance director"); empty when there is none."""
    if not isinstance(answer, Mapping):
        return ""
    return "; ".join(f"{_ANSWER_FIELD.get(k, k.replace('_', ' '))}: {v}" for k, v in answer.items() if isinstance(v, (str, int, float)) and k != "accept_unknown")


def assess(turn: Mapping) -> dict:
    """What one answered turn is worth now (finding V4-IF-02 / V4-IF-03): `{role, confirmable, problems}`.

    role `proposed_answer`: a READY, complete reading of the person's plain words, the only kind of reading that can be confirmed; `partial_answer`: the value itself is
    stated plainly but another part of the answer (an exception, a second request) is not covered yet; `unresolved_interpretation`: the model proposed a value but the
    words of that value are hedged, vague or themselves not covered (kept as history, never a fact); `no_reading`: unknown declared, refused, failed or pending.
    Only proposed and partial answers are compared for a contradiction: the person said those values plainly. Completeness is checked again here with the CURRENT rules on the stored words, so a reading stored
    under an older completeness version (or a forged `complete` flag) cannot become confirmable; problems recorded at reading time (including the model's own report) stay."""
    from core.change_intelligence.v2 import business_answer_interpret as bai
    reading = turn.get("reading") or {}
    answer = reading.get("proposed_answer")
    stored = reading.get("completeness") or {}
    problems = list(stored.get("problems") or [])
    if turn.get("text") and answer and "accept_unknown" not in answer:
        known = {(p.get("code"), str(p.get("words") or "").lower()) for p in problems}
        problems += [p for p in bai.uncertainty_problems(turn["text"], answer, quote=reading.get("quote")) if (p["code"], str(p["words"] or "").lower()) not in known]
    confirmable = (not turn.get("unknown") and reading.get("status") == "READY" and bool(answer) and stored.get("complete", True) is not False and not problems
                   and not reading.get("uncovered"))
    ref = " ".join(str(reading.get("quote") or " ".join(str(v) for v in (answer or {}).values() if isinstance(v, str))).lower().split())   # the words the value was read from
    value_uncovered = not ref or any(ref in u or u in ref for u in (" ".join(str(x).lower().split()) for x in reading.get("uncovered") or []) if u)
    if confirmable:
        role = "proposed_answer"
    elif not answer or turn.get("unknown"):
        role = "no_reading"
    elif reading.get("status") in ("READY", "NEEDS_CLARIFICATION") and not problems and not value_uncovered:
        role = "partial_answer"
    else:
        role = "unresolved_interpretation"
    return {"role": role, "confirmable": confirmable, "problems": problems}


def contradictions(turns: Iterable[Mapping]) -> list:
    """Plainly stated values (proposed or partial answers) of different turns that disagree on the same field; the last message never silently wins. An unresolved
    interpretation (a hedged or vague value, or one whose own words were not covered) is not a fact, so it never contradicts the clarification that replaced it
    (V4-IF-02); it stays in the history."""
    turns = list(turns)
    stated = ("proposed_answer", "partial_answer")
    resolved = [i for i, t in enumerate(turns) if t.get("decision") == "contradiction" and assess(t)["role"] in stated]
    if resolved:
        turns = turns[resolved[-1]:]                                                     # the person settled the earlier disagreement in that turn: only readings from there on count
    answers = [(t["turn"], (t.get("reading") or {}).get("proposed_answer") or {}) for t in turns if assess(t)["role"] in stated]
    out = []
    for i in range(1, len(answers)):
        prev_turn, prev = answers[i - 1]
        cur_turn, cur = answers[i]
        for field in sorted(set(prev) & set(cur)):
            if prev[field] != cur[field]:
                out.append({"field": field, "turns": [prev_turn, cur_turn], "values": [str(prev[field]), str(cur[field])]})
    return out


def _stuck(answered: list) -> bool:
    """No useful question remains: the last two answers left the SAME clarification problem (same reasons and same unclear words) unresolved."""
    if len(answered) < 2:
        return False
    a, b = ((t.get("reading") or {}) for t in answered[-2:])
    if a.get("status") != "NEEDS_CLARIFICATION" or b.get("status") != "NEEDS_CLARIFICATION":
        return False
    return set(a.get("reasons") or []) == set(b.get("reasons") or []) and bool(a.get("reasons")) and sorted(a.get("uncovered") or []) == sorted(b.get("uncovered") or [])


def unresolved_reason(turns: Iterable[Mapping], *, cap: int) -> Optional[str]:
    """Why an interview ends UNRESOLVED (explicit do-not-know, the person stopped, the turn cap, or no remaining clarification); None when it is not unresolved."""
    turns = list(turns)
    if any(t.get("stopped") for t in turns):
        return "stopped_by_person"
    answered = [t for t in turns if t.get("text") is not None]
    if answered and answered[-1].get("unknown"):
        return "unknown_declared"
    if len(answered) >= cap:
        return "turn_cap_reached"
    if _stuck(answered):
        return "no_remaining_clarification"
    return None


def session_status(turns: Iterable[Mapping], *, cap: int, pin_moved: bool, confirmed: bool) -> str:
    turns = list(turns)
    if confirmed:
        return "CONFIRMED"
    if pin_moved:
        return "STALE"
    answered = [t for t in turns if t.get("text") is not None]
    if unresolved_reason(turns, cap=cap):
        return "UNRESOLVED"
    last = answered[-1] if answered else None
    if last and assess(last)["confirmable"] and not contradictions(answered):     # completeness rechecked on the stored words, whatever version read them
        return "READY_TO_CONFIRM"
    return "OPEN"


MODEL_KEYS = {"status", "question", "focus"}
FOCUSES = ("kind", "uncovered_part", "contradiction", "confirm")
_UNSAFE = re.compile(r"[<>`#*_\[\]|{}]|https?://|www\.|[\x00-\x08\x0b-\x1f\x7f]", re.I)
_WORD = re.compile(r"[a-z][a-z\-]{3,}", re.I)
UNKNOWN_OFFER = " Or say that you do not know."


def validate_question_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, Mapping) or set(obj) != MODEL_KEYS:
        raise ValueError("object keys")
    if obj["status"] not in ("OK", "NONE") or not isinstance(obj["question"], str) or obj["focus"] not in FOCUSES:
        raise ValueError("status, question or focus")
    if len(obj["question"]) > limits["max_question_chars"]:
        raise ValueError("question too long")
    return dict(obj)


def build_question_prompt(frozen: Mapping, gap: Mapping, rules: Iterable[Mapping], transcript: Iterable[Mapping], deterministic: Mapping) -> str:
    from core.change_intelligence.v2.business_index import condition_text
    lines = []
    for r in rules:
        lines.append(f"- id={r['rule_id']} | {r.get('title')} | who acts: {r.get('actor_ref') or 'not stated'} | when: {condition_text(r['condition']) if r.get('condition') is not None else 'not stated'} | "
                     f"what happens: {(r.get('outcome') or {}).get('kind', 'unknown')} | exceptions: {'; '.join(e.get('text', '') for e in r.get('exceptions') or []) or 'none recorded'}")
    turns = list(transcript)[-int(frozen["limits"]["max_transcript_turns"]):]
    tr = "\n".join(f"Q{t.get('turn')}: {t.get('question')}\nA{t.get('turn')}: {t.get('text')} (reading: {t.get('reading_status')})" for t in turns) or "(no answer yet)"
    last = turns[-1] if turns else {}
    unc = "; ".join(last.get("uncovered") or []) or "none"
    contra = "; ".join(f"{c.get('field')}: {c.get('values')}" for c in (last.get("contradictions") or [])) or "none"
    prompt = frozen["prompt_template"].replace("{gap_kind}", str(gap.get("kind"))).replace("{question}", str(gap.get("question"))).replace("{rules}", "\n".join(lines) or "(none)") \
        .replace("{transcript}", tr).replace("{reading_status}", str(last.get("reading_status"))).replace("{uncovered}", unc).replace("{contradictions}", contra) \
        .replace("{deterministic}", str(deterministic.get("question", "")))
    if len(prompt) > frozen["limits"]["max_prompt_chars"]:
        raise ValueError("context_too_large")
    return prompt


def ground_question(model: Mapping, gap: Mapping, rules: Iterable[Mapping], transcript: Iterable[Mapping], deterministic: Mapping) -> dict:
    """Keep the model's question only when it is plain and grounded in the conversation; otherwise the deterministic question, labelled as the fallback with the reason.
    The option to say 'I do not know' is always appended by the server."""
    det = dict(deterministic)
    det_q = str(det.get("question") or "")
    if not det_q.rstrip().endswith("know."):
        det_q = det_q.rstrip() + UNKNOWN_OFFER
    fallback = {"question": det_q, "decision": det.get("decision"), "offers_unknown": True, "questioner": "deterministic_fallback"}
    if model["status"] != "OK" or not model["question"].strip():
        return {**fallback, "fallback_reason": "model_declined"}
    text = " ".join(model["question"].split())
    if _UNSAFE.search(text):
        return {**fallback, "fallback_reason": "not_plain_text"}
    low = text.lower()
    anchors = [str(r.get("title") or "").lower() for r in rules]
    turns = list(transcript)
    last = turns[-1] if turns else {}
    anchors += [str(u).lower() for u in (last.get("uncovered") or [])] + [str(c.get("field") or "").lower().replace("_", " ") for c in (last.get("contradictions") or [])]
    det_words = {w.lower() for w in _WORD.findall(det_q)} - {"please", "rule", "that", "what", "when", "which", "know", "exactly", "your", "words", "there", "describe", "name"}
    words = {w.lower() for w in _WORD.findall(text)}
    anchor_hit = any(a and (a in low or len({w.lower() for w in _WORD.findall(a)} & words) >= 2) for a in anchors)     # the anchor phrase itself, or two of its content words
    grounded = anchor_hit or len(words & det_words) >= 2
    if not grounded:
        return {**fallback, "fallback_reason": "not_grounded"}
    offer = " ".join(_WORD.findall(UNKNOWN_OFFER.lower()))
    norm = lambda q: " ".join(_WORD.findall(str(q).lower())).removesuffix(offer).strip()                        # noqa: E731  (the server's "or say that you do not know" is not content)
    asked = {norm(t.get("question")) for t in turns if t.get("question")}
    if norm(text) in asked - {""}:
        return {**fallback, "fallback_reason": "repeats_earlier_question"}                             # asking the same again gives the person nothing new to answer
    if "do not know" not in low and "don't know" not in low:
        text = text.rstrip() + UNKNOWN_OFFER
    return {"question": text, "decision": model["focus"], "offers_unknown": True, "questioner": "model", "fallback_reason": None}


def transcript_hash(turns: Iterable[Mapping]) -> str:
    return canonical_hash([{k: t.get(k) for k in ("turn", "question", "text", "job_id", "reading_status", "unknown")} for t in turns])
