"""SIMULATED interpreter for the local rehearsal (NOT a model, no network, no spend): turns the conversation in the prompt into the
same JSON shape the real `intent.interpret` role returns, with plain deterministic rules. It exists so a person can click through the
chat (question, answer, understanding, unsupported request) before any live call. The server still verifies everything it returns
exactly as it would for a real model (numbers must be in the person's words, quotes must be verbatim, one numeric change per request)."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from infra.chat_numbers import numbers_with_quotes

TAG = "[SIMULATED AI] "
_CASES = ("standard", "emergency", "capex")
_KINDS = [
    ("approver", r"approver|must also sign|sign[- ]?off|người duyệt|giám đốc|director|finance manager|承認者"),
    ("exception", r"\bexcept\b|\bexempt|\bexcluding\b|ngoại trừ|ngoại lệ|を除"),
    ("scope", r"\bscope\b|\bregion\b|\bchannel\b|phạm vi|khu vực|also (apply|cover)|áp dụng thêm"),
]
_FROM = re.compile(r"(from|từ|was|currently|now|hiện)\s*[:=]?\s*$", re.I)
_TO = re.compile(r"(to|xuống|lên|thành|còn|->|→|=|new)\s*[:=]?\s*$", re.I)


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n", text) if s.strip()]


def _blocks(prompt):
    convo = json.loads(prompt.split("<conversation>\n", 1)[1].rsplit("\n</conversation>", 1)[0])
    rules = []
    try:
        for line in prompt.split("<known_rules>\n", 1)[1].split("\n</known_rules>", 1)[0].splitlines():
            if line.strip().startswith("{"):
                rules.append(json.loads(line))
    except (IndexError, ValueError):
        pass
    return convo, rules


def interpret(prompt: str) -> dict:
    from tools.judge_local import sim_purposes
    purpose = sim_purposes.purpose_of(prompt)
    if purpose:
        return sim_purposes.interpret(purpose, prompt)
    convo, rules = _blocks(prompt)
    user = " \n".join(t["text"] for t in convo if t.get("from") == "person")
    low = user.lower()
    nums = numbers_with_quotes(user)
    changes, out_of_scope, assumptions, questions = [], [], [], []
    for kind, rx in _KINDS:
        for s in _sentences(user):
            if re.search(rx, s, re.I):
                changes.append({"target": kind, "kind": kind, "summary": {"approver": "a change of who approves", "exception": "an exception to the rule",
                                "scope": "a change of scope"}[kind], "quote": s[:200], "inferred": False})
                break
    # which number is the old and which the new value
    old = new = None
    for v, q in nums:
        pos = user.find(q)
        before = user[max(0, pos - 14):pos] if pos >= 0 else ""
        if _FROM.search(before) and old is None:
            old = (v, q)
        elif _TO.search(before):
            new = (v, q)
    if new is None and old is None and len(nums) == 1:
        questions.append(f"You wrote {nums[0][1]}. Is that the NEW value you want? What is the current value?")
    elif new is None and len(nums) >= 2 and old is None:
        questions.append("I see more than one number. Which one is the current value and which one is the new value?")
    elif new is None and old is not None:
        questions.append("What should the new value be?")
    subject = unit = None
    scope: dict = {}
    if "manager" in low or "quản lý" in low:
        subject, unit = "Manager approval limit", "net goods amount"
    else:
        words = set(re.findall(r"[a-zà-ỹ]{4,}", low))
        best = max(rules, key=lambda r: len(words & set(re.findall(r"[a-z]{4,}", str(r.get("subject", "")).lower()))), default=None)
        if best and words & set(re.findall(r"[a-z]{4,}", str(best.get("subject", "")).lower())):
            subject, unit = str(best["subject"]), best.get("unit") if isinstance(best.get("unit"), str) else None
    applies = [c for c in _CASES if c in low]
    if "standard" in low:
        scope = {"order_type": "standard"}
    currency = "JPY" if re.search(r"\bjpy\b|\byen\b|円", low) else None
    if old is None and subject:                               # the current value from a stored rule (the server checks it is stored)
        pool = [r for r in rules if "manager" in (str(r.get("subject", "")) + " " + str(r.get("predicate", ""))).lower()
                and isinstance(r.get("value"), (int, float)) and (new is None or r["value"] != new[0])]
        pool.sort(key=lambda r: ("standard" not in str(r.get("predicate", "")).lower(), "threshold" not in str(r.get("predicate", "")).lower()))
        hit = pool[0] if pool else None
        if hit and new is not None:
            old = (hit["value"], None)
    has_numeric = new is not None or bool(nums)
    non_numeric = [c for c in changes]
    if not has_numeric and non_numeric:
        return {"status": "UNSUPPORTED", "message": TAG + "I understood this as a change of " + ", ".join(sorted({c['kind'] for c in non_numeric})) +
                ", not a number. Ripple records it here but does not carry it out.", "questions": [], "assumptions": [],
                "fields": {"subject": None, "scope": {}, "unit": None, "currency": None, "old_value": None, "new_value": None, "applies_to": []},
                "changes": non_numeric, "out_of_scope": [c["summary"] for c in non_numeric]}
    if not has_numeric:
        return {"status": "CLARIFY", "message": TAG + "Tell me which rule you want to change and the new value.",
                "questions": ["Which rule or limit do you want to change?", "What should the new value be?"], "assumptions": [],
                "fields": {"subject": None, "scope": {}, "unit": None, "currency": None, "old_value": None, "new_value": None, "applies_to": []},
                "changes": [], "out_of_scope": []}
    if subject is None:
        questions.append("Which rule do you mean (for example the manager approval limit)?")
    if currency is None:
        questions.append("Which currency is the amount in?")
    if not applies:
        questions.append("Which kinds of orders should this cover (standard, emergency, CAPEX)?")
    ready = not questions and new is not None and old is not None
    if new is not None:
        changes.insert(0, {"target": subject or "rule", "kind": "value", "summary": f"set {subject or 'the rule'} to {new[1] or new[0]}",
                           "quote": new[1] or "", "inferred": False})
    fields = {"subject": subject, "scope": scope, "unit": unit, "currency": currency, "old_value": old[0] if old else None,
              "new_value": new[0] if new else None, "applies_to": applies}
    msg = (TAG + f"I understood: change {subject} from {old[0] if old else '?'} to {new[0] if new else '?'} {currency or ''} for {', '.join(applies)}."
           ) if ready else (TAG + "I need a little more before I can set this up.")
    return {"status": "READY" if ready else "CLARIFY", "message": msg, "questions": questions, "fields": fields, "assumptions": assumptions,
            "changes": changes, "out_of_scope": [c["summary"] for c in changes if c["kind"] != "value"]}


class SimInterpreter:
    """Stand-in for the provider client (same call shape the executor uses)."""

    def __init__(self, model_id):
        self.model_id, self.calls = model_id, 0
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        obj = interpret(kw["contents"])
        return SimpleNamespace(text=json.dumps(obj), model_version=self.model_id, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=60, candidates_token_count=40, thoughts_token_count=None,
                                                              total_token_count=100))
