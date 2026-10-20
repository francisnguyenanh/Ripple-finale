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
    ("request_assistant", "prompt_template"): {"outcome": "insufficient_context", "answer_blocks": [], "limitations": [NOTE], "authority_assertions": []},
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


def numeric_understanding(prompt: str):
    """RULES, not a model: a request of the form "... <rule words> ... from <old> to <new> ..." becomes ONE set_value operation on the single shown rule whose text holds
    <old>; anything else returns None (the generic decline). Numbers are copied from the request; nothing is invented. Labelled SIMULATED in every result."""
    try:
        request = prompt.split("<request>" + chr(10), 1)[1].split(chr(10) + "</request>", 1)[0].strip()
        rules_block = prompt.split("<rules>" + chr(10), 1)[1].split(chr(10) + "</rules>", 1)[0]
    except IndexError:
        return None
    m = re.search(r"from\s+" + _NUM.pattern + r"(?:\s*[A-Za-z]{2,4})?\s+to\s+" + _NUM.pattern, request, re.I)
    if not m:
        return None
    old, new = m.group(1).replace(",", ""), m.group(2).replace(",", "")
    rules = re.findall(r"- id=(\S+) authority=\S+ status=\S+" + chr(10) + r"  text: (.*)", rules_block)
    hits = [(rid, text) for rid, text in rules if re.search(r"(?<![\d.])" + re.escape(old) + r"(?![\d])", text.replace(",", ""))]
    if len(hits) > 1:                                                # several rules hold the old number: keep the ones sharing the most words with the request
        words = set(re.findall(r"[a-z]{5,}", request.lower()))
        scored = sorted(((len(words & set(re.findall(r"[a-z]{5,}", t.split(" | ", 1)[0].lower()))), rid) for rid, t in hits), reverse=True)   # title words only (scope/actor words are shared by many rules)
        hits = [h for h in hits if h[0] == scored[0][1]] if scored[0][0] > (scored[1][0] if len(scored) > 1 else -1) else hits
    if len(hits) != 1:
        return None
    hits = [hits[0][0]]
    return {"status": "READY", "message": "SIMULATED reading (rules, not a model): one numeric change, numbers copied from your words. Please check it.",
            "operations": [{"kind": "set_value", "rule_id": hits[0], "candidate_rule_ids": [hits[0]], "desired": {"value": int(new)}, "user_quote": request[:400]}],
            "questions": [], "assumptions": ["Only the number changes; scope, actor and conditions are read by a person."]}


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
                obj = numeric_understanding(prompt) if name == "change_understand" else None
                return self._reply(prompt, obj or DECLINES.get(name) or EXTRA_BY_NAME[name])
        for rubric, decline in RUBRIC_DECLINES.items():
            if rubric in prompt:
                self.calls += 1
                return self._reply(prompt, decline)
        return self.fallback.models.generate_content(**kw)

    def _reply(self, prompt, obj):
        return SimpleNamespace(text=json.dumps(obj), model_version=self.model_id, candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=len(prompt) // 4, candidates_token_count=60, thoughts_token_count=None, total_token_count=len(prompt) // 4 + 60))
