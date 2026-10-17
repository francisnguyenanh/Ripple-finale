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
UNKNOWN = re.compile(r"\b(i|we)\s+(do\s+not|don't|dont)\s+know\b|\bunknown\b|\bno idea\b|\bnot sure\b", re.I)
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
    if status == "READY":
        return None
    if status == "NEEDS_CLARIFICATION":
        unc = "; ".join(reading.get("uncovered") or [])
        return {"turn": n, "question": f"You also wrote: \"{unc}\". Is that an exception to the rule, a condition of it, or a separate rule? If it is an exception, say exactly when it applies, or say that you do not know.",
                "decision": "uncovered_part", "offers_unknown": True}
    if status == "UNSUPPORTED":
        q = first_question(gap)
        return {"turn": n, "question": "That answer did not fit the question. " + q["question"], "decision": gap.get("kind"), "offers_unknown": True}
    return {"turn": n, "question": first_question(gap)["question"], "decision": gap.get("kind"), "offers_unknown": True}


def contradictions(turns: Iterable[Mapping]) -> list:
    """Readings of different turns that disagree on the same field; the last message never silently wins."""
    turns = list(turns)
    resolved = [i for i, t in enumerate(turns) if t.get("decision") == "contradiction" and (t.get("reading") or {}).get("status") in ("READY", "NEEDS_CLARIFICATION")]
    if resolved:
        turns = turns[resolved[-1]:]                                                     # the person settled the earlier disagreement in that turn: only readings from there on count
    answers = [(t["turn"], (t.get("reading") or {}).get("proposed_answer") or {}) for t in turns if (t.get("reading") or {}).get("status") in ("READY", "NEEDS_CLARIFICATION")]
    out = []
    for i in range(1, len(answers)):
        prev_turn, prev = answers[i - 1]
        cur_turn, cur = answers[i]
        for field in sorted(set(prev) & set(cur)):
            if prev[field] != cur[field]:
                out.append({"field": field, "turns": [prev_turn, cur_turn], "values": [str(prev[field]), str(cur[field])]})
    return out


def session_status(turns: Iterable[Mapping], *, cap: int, pin_moved: bool, confirmed: bool) -> str:
    turns = list(turns)
    if confirmed:
        return "CONFIRMED"
    if pin_moved:
        return "STALE"
    answered = [t for t in turns if t.get("text") is not None]
    if answered and answered[-1].get("unknown"):
        return "UNRESOLVED"
    if len(answered) >= cap:
        return "UNRESOLVED"
    last = answered[-1] if answered else None
    if last and (last.get("reading") or {}).get("status") == "READY" and not contradictions(answered):
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
    if "do not know" not in low and "don't know" not in low:
        text = text.rstrip() + UNKNOWN_OFFER
    return {"question": text, "decision": model["focus"], "offers_unknown": True, "questioner": "model", "fallback_reason": None}


def transcript_hash(turns: Iterable[Mapping]) -> str:
    return canonical_hash([{k: t.get(k) for k in ("turn", "question", "text", "job_id", "reading_status", "unknown")} for t in turns])
