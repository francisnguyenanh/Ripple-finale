# -*- coding: utf-8 -*-
"""SIMULATED client for the Final knowledge-loop AI roles in the local rehearsal (`run_local`). NOT a model and NOT evidence of model quality.

Every Final role (business Q&A, change understanding, dependency and gap suggestions, draft guidance, free-text answer reading) gets a schema-valid answer that
DECLINES: ABSTAINED / CLARIFY / UNSURE / UNSUPPORTED with a message that says the model is simulated. Nothing is invented, no value is read from the prompt, so the
rehearsal shows the complete pipeline (job, admission, validation, UI states, fallbacks to the structured controls) without pretending to understand anything.
The role is recognised by the first sentence of its frozen prompt template (`config/*.json`), never by guessing. Prompts of other roles go to the wrapped client.
"""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config"
NOTE = "SIMULATED model (local rehearsal): it declines instead of guessing. Use the structured controls; a real model is only used in an approved live window."

DECLINES = {
    "knowledge_qa": {"status": "ABSTAINED", "answer": "", "claims": [], "uncertainty": NOTE, "clarifying_questions": []},
    "change_understand": {"status": "CLARIFY", "message": NOTE, "operations": [], "questions": ["Please name the rule and give the exact current and new value; the simulated model does not read requests."], "assumptions": []},
    "change_impact_deps": {"status": "UNSURE", "dependencies": [], "notes": [NOTE]},
    "change_draft_guidance": {"summary": NOTE, "items": [], "questions_for_human": []},
    "knowledge_gap_proposals": {"status": "UNSURE", "gaps": [], "relations": [], "notes": [NOTE]},
    "gap_answer_interpret": {"status": "UNSUPPORTED", "kind": None, "text": None, "condition": None, "quote": None, "uncovered": [], "message": NOTE},
}


# Extension roles (added 2026-10-19 for the owner's visual acceptance of the extension screens): the same schema-valid DECLINE, so each screen shows its labelled
# fallback / "nothing kept" / "insufficient" state. (config file, template key) -> decline; the simulation roles share one template and are told apart by their rubric.
EXTRA = {
    ("knowledge_mining", "prompt_template"): {"status": "UNSURE", "hypotheses": [], "notes": [NOTE]},
    ("knowledge_mining", "candidate_prompt_template"): {"status": "NONE_FOUND", "pairs": [], "notes": [NOTE]},
    ("knowledge_mining", "unbound_prompt_template"): {"status": "UNSURE", "items": [], "notes": [NOTE]},
    ("document_wording", "prompt_template"): {"status": "UNSUPPORTED", "sections": [], "omitted": []},
    ("gap_interview", "prompt_template"): {"status": "NONE", "question": "", "focus": "kind"},
    ("change_risk_memo", "prompt_template"): {"status": "insufficient", "statements": [], "open_questions": [NOTE]},
    ("ma_synthesis_memo", "prompt_template"): {"status": "insufficient", "statements": [], "open_questions": [NOTE]},
    # request assistant (2026-10-20): the panel shows "The facts do not answer this" with the simulated note; a real answer needs an approved live window
    ("request_assistant", "prompt_template"): {"outcome": "insufficient_context", "asks": [], "unrecorded": [], "limitations": [NOTE]},   # answer/6 (the answer/5 shape is a schema violation since R16)
}
RUBRIC_DECLINES = {"You are the SCENARIO DESIGNER": {"status": "UNSURE", "scenarios": [], "notes": [NOTE]},
                   "You are the ASSUMPTION CRITIC": {"status": "UNSURE", "findings": [], "disputes": [], "notes": [NOTE]},
                   "You are the RESULT INTERPRETER": {"status": "UNSURE", "statements": [], "notes": [NOTE]}}


def _markers() -> dict:
    out = {}
    for name in DECLINES:
        tpl = json.loads((CONFIG / f"{name}.json").read_text(encoding="utf-8"))["prompt_template"]
        out[name] = tpl.split(".", 1)[0][:80]                      # the first sentence of the frozen template identifies the role
    for (name, key), decline in EXTRA.items():
        tpl = json.loads((CONFIG / f"{name}.json").read_text(encoding="utf-8"))[key]
        out[f"{name}:{key}"] = tpl.split(".", 1)[0][:80]
    return out


EXTRA_BY_NAME = {f"{name}:{key}": decline for (name, key), decline in EXTRA.items()}


_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?![\w.])")


def _parts(prompt: str):
    try:
        request = prompt.split("<request>" + chr(10), 1)[1].split(chr(10) + "</request>", 1)[0].strip()
        rules_block = prompt.split("<rules>" + chr(10), 1)[1].split(chr(10) + "</rules>", 1)[0]
    except IndexError:
        return None, []
    rules = [(rid, text, text.split(" | ", 1)[0]) for rid, text in re.findall(r"- id=(\S+) authority=\S+ status=\S+" + chr(10) + r"  text: (.*)", rules_block)]
    return request, rules


def _words(s: str) -> set:
    return set(re.findall(r"[a-z]{5,}", s.lower())) - {"should", "become", "change", "raise", "lower", "please", "would", "which", "their", "there", "about"}


def rule_understanding(prompt: str) -> dict:
    """RULES, not a model (F-ACC-3, 2026-10-21), labelled SIMULATED in every result; nothing is invented, every number is copied from the request:
    - READY: one numeric change on ONE shown rule. The new value follows "to" / "become(s)" / "into"; the rule is the single shown rule that holds the other number of
      the request (the old value), or else the single rule whose title shares the most words with the request (at least two).
    - CLARIFY, with one concrete next sentence (the input stays open, so the person writes the request again): a new value with several candidate rules (they are
      named), or a rule without a new value.
    - UNSUPPORTED: anything that is not a change of one number of a shown rule (a change of actor, scope or exception needs a real model in an approved live window)."""
    request, rules = _parts(prompt)
    if request is None:
        return dict(DECLINES["change_understand"])
    nums = [n.replace(",", "") for n in _NUM.findall(request)]
    m = re.search(r"\b(?:to|becomes?|into)\s+" + _NUM.pattern, request, re.I)
    new = m.group(1).replace(",", "") if m else None
    others = [n for n in nums if n != new]
    hits = [r for r in rules if any(re.search(r"(?<![\d.])" + re.escape(o) + r"(?![\d])", r[1].replace(",", "")) for o in others)] if others else []
    if len(hits) != 1:
        ask = _words(request)
        scored = sorted(((len(ask & _words(title)), rid, title) for rid, _text, title in rules if _NUM.search(title)), reverse=True)
        best = [s for s in scored if s[0] >= 2 and s[0] == (scored[0][0] if scored else 0)]
        if len(best) == 1 and not hits:
            hits = [r for r in rules if r[0] == best[0][1]]
        elif len(hits) > 1 or len(best) > 1:
            names = [h[2] for h in hits] if len(hits) > 1 else [b[2] for b in best]
            return {"status": "CLARIFY", "message": "SIMULATED reading (rules, not a model): several rules match your words.", "operations": [],
                    "questions": [f"Which rule do you mean: {' or '.join(repr(n) for n in names[:3])}? Write the request again with words from that rule and the new value."],
                    "assumptions": []}
    if len(hits) == 1 and new is None:
        return {"status": "CLARIFY", "message": "SIMULATED reading (rules, not a model): the rule is clear, the new value is missing.", "operations": [],
                "questions": [f"What should the new value of {hits[0][2]!r} be? Write it as: change it to <number> JPY."], "assumptions": []}
    if len(hits) == 1:
        rid = hits[0][0]
        return {"status": "READY", "message": "SIMULATED reading (rules, not a model): one numeric change, numbers copied from your words. Please check it.",
                "operations": [{"kind": "set_value", "rule_id": rid, "candidate_rule_ids": [rid], "desired": {"value": int(new)}, "user_quote": request[:400]}],
                "questions": [], "assumptions": ["Only the number changes; scope, actor and conditions are read by a person."]}
    return {"status": "UNSUPPORTED", "operations": [], "questions": [], "assumptions": [],
            "message": ("SIMULATED reading (rules, not a model): it only understands a change of one number on one shown rule, e.g. "
                        "'Raise the manager approval threshold to 2,500,000 JPY'. Other changes need a real model in an approved live window.")}


class SimFinal:
    """Dispatch by prompt template; everything else goes to `fallback` (the Pitch wording stub)."""

    def __init__(self, model_id: str, fallback):
        self.model_id, self.fallback, self.calls = model_id, fallback, 0
        self.markers = _markers()
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        prompt = kw.get("contents") or ""
        for name, marker in self.markers.items():
            if marker and prompt.startswith(marker):
                self.calls += 1
                obj = rule_understanding(prompt) if name == "change_understand" else None
                return self._reply(prompt, obj or DECLINES.get(name) or EXTRA_BY_NAME[name])
        for rubric, decline in RUBRIC_DECLINES.items():
            if rubric in prompt:
                self.calls += 1
                return self._reply(prompt, decline)
        return self.fallback.models.generate_content(**kw)

    def _reply(self, prompt, obj):
        return SimpleNamespace(text=json.dumps(obj), model_version=self.model_id, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=len(prompt) // 4, candidates_token_count=60, thoughts_token_count=None, total_token_count=len(prompt) // 4 + 60))
