"""SIMULATED interpretation for the other free-text surfaces of the local rehearsal (knowledge question, reply to an open point, draft
feedback, what-if). Plain deterministic rules, NOT a model: no network, no spend. The server verifies every code, number and quote it
returns exactly as it would for a real model."""
from __future__ import annotations

import json
import re

from infra.chat_numbers import numbers_with_quotes

TAG = "[SIMULATED AI] "


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n", text) if s.strip()]


def _blocks(prompt):
    convo = json.loads(prompt.split("<conversation>\n", 1)[1].rsplit("\n</conversation>", 1)[0])
    try:
        ctx = json.loads(prompt.split("<context>\n", 1)[1].split("\n</context>", 1)[0])
    except (IndexError, ValueError):
        ctx = {}
    return convo, ctx


def purpose_of(prompt: str) -> str | None:
    m = re.match(r"PURPOSE: (\w+)", prompt)
    return m.group(1) if m else None


def interpret(purpose: str, prompt: str) -> dict:
    convo, ctx = _blocks(prompt)
    user = " \n".join(t["text"] for t in convo if t.get("from") == "person")
    sentences = _sentences(user)
    base = {"questions": [], "assumptions": [], "out_of_scope": []}
    if purpose == "knowledge_qa":
        words = set(re.findall(r"[a-z]{4,}", user.lower()))
        picked = [f["file"] for f in ctx.get("files", []) if words & set(re.findall(r"[a-z]{4,}", f["name"].lower()))]
        q = (sentences[-1] if sentences else user).strip()
        if not picked:
            return {**base, "status": "CLARIFY", "message": TAG + "Which files should I search?",
                    "questions": ["Which files should I search for the answer?"], "fields": {"question": q, "files": []}}
        names = ", ".join(f["name"] for f in ctx["files"] if f["file"] in picked)
        return {**base, "status": "READY", "message": TAG + "I will search " + names + ".", "fields": {"question": q, "files": picked}}
    if purpose == "gap_reply":
        text = user.strip()
        wish = bool(re.search(r"\b(should|want|would like|need to|please|from now on)\b", text, re.I))
        return {**base, "status": "READY", "message": TAG + ("I will record this as a wish." if wish else "I will record this as a fact."),
                "fields": {"answer": text[:600], "kind": "comment", "wish_or_fact": "wish" if wish else "fact", "rationale": None,
                           "quote": (sentences[0] if sentences else text)[:300]}}
    if purpose == "question_reply":
        text = user.strip()
        wish = bool(re.search(r"\b(should|want|would like|need to|please|from now on)\b", text, re.I))
        claims = ctx.get("claims", [])
        words = set(re.findall(r"[a-z]{4,}", text.lower()))
        pick = next((c["claim"] for c in claims if words & set(re.findall(r"[a-z]{4,}", c["rule"].lower()))), claims[0]["claim"] if len(claims) == 1 else None)
        decide = bool(re.search(r"\b(confirm|is correct|is right|that is right|approve this)\b", text, re.I)) and not wish
        if decide and pick is None:
            return {**base, "status": "CLARIFY", "message": TAG + "Which rule are you confirming?", "questions": ["Which rule are you confirming?"],
                    "fields": {"answer": text[:600], "kind": "decision", "claim": None, "wish_or_fact": "fact", "rationale": None, "asks_source_change": False}}
        return {**base, "status": "READY", "message": TAG + ("I will record this as a decision." if decide else "I will record this as a comment."),
                "fields": {"answer": text[:600], "kind": "decision" if decide else "comment", "claim": pick if decide else None,
                           "wish_or_fact": "wish" if wish else "fact", "rationale": None, "asks_source_change": wish,
                           "quote": (sentences[0] if sentences else text)[:300]}}
    if purpose == "draft_feedback":
        reactions = []
        for it in ctx.get("items", []):
            stem = set(re.findall(r"[a-z]{4,}", it["file"].lower())) - {"yaml", "xlsx", "docx"}
            for s in sentences:
                if stem & set(re.findall(r"[a-z]{4,}", s.lower())):
                    r = ("exclude" if re.search(r"wrong|incorrect|do not change|don't change|leave", s, re.I)
                         else "manual" if re.search(r"by hand|manual", s, re.I)
                         else "ambiguous" if re.search(r"unsure|unclear|not sure", s, re.I)
                         else "question" if "?" in s else "keep")
                    reactions.append({"item": it["item"], "reaction": r, "note": s[:160], "quote": s[:300]})
                    break
        if not reactions:
            return {**base, "status": "CLARIFY", "message": TAG + "Which item of the draft is your feedback about?",
                    "questions": ["Which file or item of the draft do you mean?"], "fields": {"reactions": [], "clarification": None}}
        return {**base, "status": "READY", "message": TAG + f"I mapped your feedback to {len(reactions)} item(s).",
                "fields": {"reactions": reactions, "clarification": None}}
    nums = numbers_with_quotes(user)
    low = user.lower()
    rule = ("r2" if re.search(r"capex|finance", low) else "r5" if re.search(r"payment|terms|days", low)
            else "r4" if ("percent" in low or "%" in low) else "r3" if "tolerance" in low
            else "r1" if re.search(r"manager|approval|limit|threshold", low) else None)
    if rule is None:
        return {**base, "status": "UNSUPPORTED", "fields": {"rule": None, "value": None},
                "message": TAG + "That is not something the what-if can try. You can try the approval thresholds, the invoice tolerance or the payment terms.",
                "out_of_scope": ["a rule that the what-if does not support"]}
    if not nums:
        return {**base, "status": "CLARIFY", "message": TAG + "Which value do you want to try?", "questions": ["Which value should I try?"],
                "fields": {"rule": rule, "value": None}}
    return {**base, "status": "READY", "message": TAG + f"I will try {nums[-1][0]:g} for that rule.", "fields": {"rule": rule, "value": nums[-1][0]}}
