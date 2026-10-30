"""context/12 and config 29 (R23, after the config 28 development run): the later-steps fact follows whether THIS request continues (read from the task view's
structured codes, never its prose); what each decision choice does is a fact; the judgement rule states the contrasting recorded cases. Pure: no DB, no model
(the real routes are in tests/final_loop/test_request_assistant_context12_jobs.py). Prompt-text checks lock the contract; they are not evidence that a model
follows it."""
from __future__ import annotations

import json
import pathlib

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_integrity import ob, pack_of, tv

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "config" / "request_assistant.json").read_text(encoding="utf-8"))
LATER = [{"code": "commit", "label": "Knowledge committed", "state": "future"}, {"code": "readback", "label": "Source read back", "state": "future"}]


def view(stage_code, label, deliverables=(), stages=LATER, obligations=()):
    v = tv(list(obligations), stage=label)
    v["stage"]["code"] = stage_code
    v["expected_deliverables"] = list(deliverables)
    v["stages"] = [{"code": "decision", "label": "Knowledge decision", "state": "current", "phase": "awaiting_decision", "phase_label": "current step, waiting for a decision"}] + list(stages)
    return v


def later_fact(v):
    return next((f for f in ra.task_facts(v) if f and f.get("key") == "steps:later"), None)


def keys(v):
    return {f["key"] for f in ra.task_facts(v) if f}


@pytest.mark.parametrize("v, cont, starts, holders", [
    (view("AWAITING_DECISION", "Waiting for the knowledge decision", [{"code": "knowledge_decision"}]), "continues", "Steps expected after the current one if the request goes ahead", True),
    (view("CLARIFY", "Open questions must be answered first", [{"code": "answers"}]), "after_answers", "Steps expected after the current one once the open questions are answered", True),
    (view("CHANGES_REQUESTED", "Changes requested", [{"code": "response"}]), "after_revision", "Steps expected after the current one once a revised version answers", True),
    (view("REJECTED", "The knowledge change was rejected", [{"code": "none"}]), "ended", "This request has ended (The knowledge change was rejected): no further step", False),
    (view("KNOWLEDGE_MOVED", "Blocked: the knowledge moved after confirmation", [{"code": "new_request"}]), "new_request",
     "This request cannot continue: it has to be stated again as a new change request, which is a separate request.", False),
])
def test_the_later_steps_fact_follows_whether_this_request_continues(v, cont, starts, holders):
    assert ra.continuation(v) == cont
    f = later_fact(v)
    assert f["text"].startswith(starts) and f["tier"] == "mandatory"
    assert ("steps:later:holders" in keys(v)) is holders                                              # who acts later is offered only when later steps apply
    if cont == "new_request":
        assert "will not take place on this request" in f["text"] and "The steps and holders of a new request are not part of this record." in f["text"]
        assert "if the request goes ahead" not in f["text"] and "phases" not in (f.get("data") or {})          # no expected path is offered for this request


def test_an_unknown_future_offers_no_later_steps_and_an_intent_flow_keeps_the_generic_wording():
    assert later_fact(view("AWAITING_DECISION", "Waiting", [{"code": "knowledge_decision"}], stages=[])) is None    # nothing recorded later: nothing claimed
    intent = view("ASK_DRAFT", "Confirmed request, no AI draft yet")
    assert ra.continuation(intent) == "continues" and later_fact(intent)["text"].startswith("Steps expected after the current one if the request goes ahead")


def test_the_decision_choices_are_a_fact_only_while_the_decision_is_open_and_answer_how_and_next():
    open_ = view("AWAITING_DECISION", "Waiting", [{"code": "knowledge_decision"}], obligations=[ob(state="current")])
    f = next(x for x in ra.task_facts(open_) if x and x["key"] == "decision:options")
    assert "request changes returns it to the person who asked" in f["text"] and "reject ends the knowledge change" in f["text"] and len(f["text"]) <= ra.MAX_FACT_CHARS
    p = pack_of(open_)
    b = ra.basis_map(p["facts"])
    fid = next(x["id"] for x in p["facts"] if x["key"] == "decision:options")
    assert fid in b["how"] and fid in b["next"]
    done = view("REJECTED", "Rejected", [{"code": "none"}], obligations=[ob(state="done")])
    assert "decision:options" not in keys(done)


@pytest.mark.parametrize("held, text, refused", [
    (False, "It is waiting for the AI draft step, which has not started; no blocker is recorded and nothing is blocked or failed.", False),
    (True, "It is held: the knowledge decision step is blocked because the business knowledge moved.", False),
    (False, "It is held: the knowledge decision step is blocked.", True),                          # "held" without a blocker or blocked step: refused
    (False, "Progress is healthy.", True),
    (False, "Nothing has gone wrong.", True),
])
def test_the_contrasting_judgements_pass_the_checks_and_unsupported_certainty_does_not(held, text, refused):
    v = view("AWAITING_DECISION", "Waiting", [{"code": "knowledge_decision"}])
    if held:
        v = view("KNOWLEDGE_MOVED", "Blocked: the knowledge moved after confirmation", [{"code": "new_request"}])
        v["stages"][0].update(state="blocked", phase="blocked", phase_label="blocked")
        v["blockers"] = [{"code": "knowledge_moved", "text": "The business knowledge changed; it has to be stated again."}]
    p = pack_of(v)
    st = next(f["id"] for f in p["facts"] if f["kind"] == "state")
    out = run(p, [("did it go wrong", "status", [blk(text, [st], "interpretation")])], question="Did something go wrong?")
    assert (any(r["reason"] == "status_contradicts_record" for r in out["rejected"])) is refused, out["rejected"]


def test_config29_states_the_contrasting_cases_and_explains_choices_and_steps():
    t = CFG["prompt_template"]
    assert CFG["version"] == 32 and CFG["limits"]["max_prompt_chars"] == 20000 and CFG["recovery"]["primary_deadline_s"] == 19
    assert "a current step not started, no blocker, nothing blocked or failed = waiting for that step, not stuck" in t
    assert "a blocker or a blocked or failed step = held, as that fact says" in t
    assert "If only part is decided, give that part and limit only the rest." in t                       # config 30: the general form of this rule
    assert "if they do not decide it, say so" not in t                                                  # the config 28 wording that over-corrected
    assert "- A choice or a step (how to send it back, what a step involves): say what the option or the recorded step does, from its facts" in t
    assert "never present a workflow blocker as the motive for a rule change" in t and "never answer only \"not recorded\" when the facts describe the change" in t
    assert ra.CONTEXT_VERSION == "request-assistant-context/14" and ra.CHECKS_VERSION == "request-assistant-checks/30"
    for word in ("high-risk", "supplier", "operations director", "finance controller", "regional compliance", "priya", "h7-", "ai draft"):
        i = t.index("- Readings:")
        assert word not in t[i:t.index("- Status (part status)")].lower()
