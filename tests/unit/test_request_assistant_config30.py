"""config 30 (R23, after the config 29 development run): the readings contract over question variants of the "answer, then disclaim" class. Pure: no DB, no
model. For each variant, written by hand from the fixture record: which part of the question the facts state, which reading the cited facts decide and what is
not recorded. The test shows that the facts deciding the reading are offered for the part asked, that the server keeps the reading and the limitation of the
undecided rest, and that a certainty the record cannot give is refused. Prompt-text checks lock the contract; none of this is evidence that a model follows it."""
from __future__ import annotations

import json
import pathlib

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from infra.adapters import v2_request_tasks as V
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_integrity import ob, pack_of, tv

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "config" / "request_assistant.json").read_text(encoding="utf-8"))


def _waiting():
    """An intent request: two steps done, the current step (the AI draft) not started, no blocker."""
    v = V._dto("intent_flow", "x", None, "t", {"code": "ASK_DRAFT", "label": "Confirmed request, no AI draft yet"},
               [V._obligation("ask_draft", "Ask for the AI draft", holder="requester", holder_label="the requester", eligible_me=False)], [], [], [],
               V._steps(V.INTENT_STEPS, {"conversation": "done", "intent": "done", "draft": "current"}), {}, [], [])
    return ra.pack({"family": "intent_flow", "id": "x"}, "now", ra.task_facts(v), auth={})


WAIT = _waiting()
HELD = pack_of(tv([ob(state="blocked")], blockers=[{"code": "knowledge_moved", "text": "The business knowledge changed after this request was confirmed; it has to be "
                                                                                        "stated and confirmed again."}]),
               extra=[ra.fact("rule_change", "Rule to change: Purchases from high-risk suppliers go to the Operations Director regardless of amount.", "draft",
                              key="rule_change:scope:1"),
                      ra.fact("rule_change", "After the change (proposed): Purchases from high-risk suppliers go to the Finance Controller regardless of amount.", "draft",
                              key="rule_change:draft:1")])


def ids(p, *kinds):
    return [f["id"] for f in p["facts"] if f["kind"] in kinds]


def key(p, k):
    return next(f["id"] for f in p["facts"] if f.get("key") == k)


# (question, part, pack, deciding facts, the reading the facts decide, what is not recorded) - labels from the fixture records above
VARIANTS = [
    ("Is it stuck, or just not its turn yet?", "status", WAIT, lambda p: ids(p, "state", "step")[:2],
     "It is waiting for the AI draft step, not stuck: no blocker is recorded and that step has not started.", None),
    ("Would you call this healthy progress or a stall?", "status", WAIT, lambda p: ids(p, "state", "step")[:2],
     "It is not stalled on the record: no blocker is recorded and the current step has simply not started.",
     "The record does not show whether this pace is normal or how long it has waited."),                   # the decided half, and the rest limited
    ("Did something go wrong, or has it not reached its turn?", "status", WAIT, lambda p: ids(p, "state", "step")[:2],
     "Nothing recorded has gone wrong: it has not reached its turn, as the current step has not started and no blocker is recorded.", None),
    ("Is it moving or held up?", "status", HELD, lambda p: ids(p, "blocker"),
     "It is held: a blocker is recorded saying the business knowledge changed after confirmation.", "The record does not show when the blocker was raised."),
    ("Is the concern about what the change says or about how it was handled?", "reason", HELD, lambda p: ids(p, "blocker"),
     "The concern is about the procedure, not the content of the change: the blocker says the knowledge changed after confirmation, so it must be stated again.",
     None),
    ("Would this make purchase approvals stricter or looser?", "effect", HELD, lambda p: [key(p, "rule_change:scope:1"), key(p, "rule_change:draft:1")],
     "Neither stricter nor looser in reach: the request proposes the same cases, all high-risk supplier purchases, go to a different approver.",
     "The record does not assess whether the new approver is a stricter check."),
    ("Is this a risky thing to approve?", "effect", HELD, lambda p: [key(p, "rule_change:scope:1"), key(p, "rule_change:draft:1")],
     "If approved, high-risk supplier purchases would go to the Finance Controller instead of the Operations Director.",
     "The record does not assess the risk of this change or whether it is a disadvantage."),
    ("Is someone holding this up on purpose?", "status", WAIT, lambda p: ids(p, "state", "step")[:2],
     "No blocker is recorded and the current step has not started yet.", "The record does not show any intent behind the timing."),
]


@pytest.mark.parametrize("question, part, p, deciding, reading, unknown", VARIANTS, ids=[v[0][:40] for v in VARIANTS])
def test_the_deciding_facts_are_offered_and_the_reading_and_the_limited_rest_are_kept(question, part, p, deciding, reading, unknown):
    refs = deciding(p)
    assert refs and set(refs) <= set(ra.basis_map(p["facts"])[part]), (part, refs)          # the facts that decide the reading are offered for that part
    out = run(p, [(question[:30], part, [blk(reading, refs, "interpretation")])], question=question, limitations=[unknown] if unknown else [])
    assert out["rejected"] == [] and any(b["text"] == reading for b in out["blocks"]), out["rejected"]
    if unknown:
        assert unknown in out["limitations"]


@pytest.mark.parametrize("p, text", [
    (WAIT, "Progress is healthy: two steps are done."),                                            # health
    (WAIT, "The request is on track."),
    (WAIT, "The requester is deliberately holding it."),                                          # intent
    (WAIT, "It is stuck at the AI draft step."),                                                  # stuck with no blocker
    (WAIT, "There are no problems with this request."),
])
def test_a_certainty_or_a_status_the_record_cannot_give_is_refused(p, text):
    out = run(p, [("how is it going", "status", [blk(text, ids(p, "state", "step")[:2], "interpretation")])], question="How is it going?")
    assert [r["reason"] for r in out["rejected"]] == ["status_contradicts_record"], out["rejected"]


def test_config30_carries_one_general_readings_contract_and_no_rule_tells_the_model_to_withhold_a_decided_reading():
    t = CFG["prompt_template"]
    assert CFG["version"] == 32 and CFG["limits"]["max_prompt_chars"] == 20000 and CFG["recovery"]["primary_deadline_s"] == 19
    assert "- Readings: when a part asks which description fits" in t and "If only part is decided, give that part and limit only the rest." in t
    assert "Never use one to withhold or take back a reading the facts decide." in t
    assert "- Judgement questions" not in t and "if they do not decide it, say so" not in t           # the form list of config 29 / 28 is gone
    i = t.index("- Readings:")
    rule = t[i:t.index("\n", i)].lower() + t[t.index("5. limitations"):t.index("\n", t.index("5. limitations"))].lower()
    for word in ("high-risk", "supplier", "director", "controller", "capex", "jpy", "chief risk", "h7-", "substance", "healthy progress or", "priya"):
        assert word not in rule
    for kept in ("- Never write who holds, must, may, should or can act", "- Obligation parts (what this reader currently owes): copy the position fact's",
                 "as part action", "Declining to act does not mean the request has no return, revision or approval mechanism"):
        assert kept in t
