"""context/11, checks/27 and config 28 (R23 mandate, A-US + caps stage 1): a next part is offered the current state; four narrow honest-absence forms pass the
unstated-authority guard and nothing else does; a certainty the record cannot give is refused unless negated. Pure: no DB, no model (the worker path is in
tests/final_loop/test_request_assistant_recovery.py). Positive and negative cases cover negation, passive voice, several clauses, a current actor, a historical
actor and a time the record does not give."""
from __future__ import annotations

import json
import pathlib

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_integrity import fid, ob, pack_of, tv

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "config" / "request_assistant.json").read_text(encoding="utf-8"))

P = pack_of(tv([ob(me=False, may=False, holder="requester", holder_label="the person who asked")], blockers=[{"code": "knowledge_moved", "text": "The business knowledge changed; it has to be stated again."}]),
            extra=[ra.fact("step", "Steps expected after the current one if the request goes ahead: Knowledge committed; Source read back.", "task_view", key="steps:later"),
                   ra.fact("step", "Who acts on each later step is recorded only when that step starts.", "task_view", key="steps:later:holders"),
                   ra.fact("decision", "knowledge decision: approve, earlier, by an approver", "decision", tier="history", key="decision:1:knowledge:approve"),
                   ra.fact("impact", "Who the record names as affected by the proposed change: no person, team or customer group is named.", "computed", {"named": []}, key="parties"),
                   ra.decision_status_fact([])])
Q_NAME = "Doesn't the Chief Risk Officer have the final word on this?"


def key_id(key):
    return next(f["id"] for f in P["facts"] if f.get("key") == key)


def kept_limitations(lims, question="What happens next?", asks=None, unrecorded=()):
    st = fid(P, "state")
    out = run(P, asks or [("what happens next", "next", [blk("The request is at the knowledge decision stage.", [st])])], question=question, limitations=list(lims),
              unrecorded=list(unrecorded))
    model_lims = [x for x in out["limitations"] if not x.startswith(ra.SERVER_NOTE_PREFIXES)]              # the server's own notes (an unexplained blocker) apart
    return model_lims, [r for r in out["rejected"] if r["kind"] == "limitation"]


# ---- context/11: a next part is offered where the request is now -----------------------------------------------------------------------------------------------

def test_the_next_basis_holds_the_current_state_the_blocker_and_the_later_steps():
    b = set(ra.basis_map(P["facts"])["next"])
    assert {fid(P, "state"), fid(P, "blocker"), key_id("steps:later"), key_id("decision_status"), fid(P, "position")} <= b
    assert key_id("steps:later:holders") not in b and key_id("parties") not in b
    assert ra.CONTEXT_VERSION == "request-assistant-context/14" and ra.CHECKS_VERSION == "request-assistant-checks/31"


# ---- checks/27: honest absences --------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The facts do not show a timeline or estimate for when decisions or approvals will occur.",                 # a time, no actor
    "The facts do not show a date for the approval.",
    "The facts do not show who participates in or signs off on later steps until each step starts.",          # later steps, holders fact in the pack
    "Who approves the later steps is not recorded until each step starts.",                                    # passive voice
    "The facts do not show who signs off on later steps until each step starts.",                              # the checks/11 "who acts is not stated" form
])
def test_an_honest_absence_of_a_time_or_of_later_step_holders_is_kept(text):
    kept, refused = kept_limitations([text])
    assert kept == [text] and refused == []


@pytest.mark.parametrize("text", [
    "The facts do not show who approves the current step.",                                                    # the current step: the server states it
    "The facts do not show who decides now.",
    "The facts do not show whether the approver has final approval.",                                         # a current role
    "The facts do not show when the approver will approve it.",                                               # a time with an actor
    "The facts do not show a timeline for approval, but the approver cannot decide yet.",                     # several clauses: a denied permission
    "The facts do not show a date, and you do not have to decide it.",                                        # negates an existing obligation
    "The facts do not show who approved the earlier draft.",                                                  # a historical actor
    "Who decides the next steps is not stated.",                                                              # "next steps" is not the later-steps scope
    "The facts do not show who signs off on later steps, and the approver must decide this now.",            # an obligation asserted in another clause
    "It is unclear who approves this step.",                                                                  # the current step (deictic rule, unchanged)
])
def test_other_unstated_authority_stays_refused(text):
    kept, refused = kept_limitations([text])
    assert kept == [] and [r["reason"] for r in refused] == ["authority_in_free_text"]


def test_a_later_step_absence_needs_the_holders_fact_in_the_pack():
    bare = pack_of(tv([ob()]))
    out = run(bare, [("next", "next", [blk("The request is at the knowledge decision stage.", [fid(bare, "state")])])], question="What next?",
              limitations=["The facts do not show who participates in or signs off on later steps until each step starts."])
    assert [x for x in out["limitations"] if not x.startswith(ra.SERVER_NOTE_PREFIXES)] == [] and out["rejected"][0]["reason"] == "authority_in_free_text"


def test_a_verified_absent_name_may_be_named_in_a_limitation_and_an_unverified_one_may_not():
    st = fid(P, "state")
    asks = [("whether the Chief Risk Officer has the final word", "premise", [blk("The current step is the knowledge decision.", [st])])]
    ok = "The facts do not show whether the Chief Risk Officer has final approval or any role in changes like this."
    kept, refused = kept_limitations([ok], question=Q_NAME, asks=asks, unrecorded=["Chief Risk Officer"])
    assert kept[0] == ok and refused == []
    kept, refused = kept_limitations([ok], question=Q_NAME, asks=asks, unrecorded=[])                           # not declared, not verified by the server
    assert ok not in kept and [r["reason"] for r in refused] == ["authority_in_free_text"]
    bad = "The facts do not show whether the Chief Risk Officer or the approver has final approval."         # a record role in the same sentence
    kept, refused = kept_limitations([bad], question=Q_NAME, asks=asks, unrecorded=["Chief Risk Officer"])
    assert bad not in kept


def test_a_block_restating_the_parties_fact_it_cites_is_kept_and_other_actor_prose_is_not():
    parties = key_id("parties")
    ok = "The actors who apply the rule (director, finance controller) are not recorded as affected parties."
    out = run(P, [("who is affected", "effect", [blk(ok, [parties])])], question="Who loses something?")
    assert out["rejected"] == [] and any(b["text"] == ok for b in out["blocks"])
    out = run(P, [("who is affected", "effect", [blk(ok, [fid(P, "state")])])], question="Who loses something?")   # without the parties fact cited
    assert [r["reason"] for r in out["rejected"]] == ["authority_in_free_text"]
    out = run(P, [("who is affected", "effect", [blk("Who approves this step is not recorded as the affected party.", [parties])])], question="Who loses something?")
    assert [r["reason"] for r in out["rejected"]] == ["authority_in_free_text"]


def test_a_proposal_stated_as_in_force_and_a_declared_name_the_record_may_hold_stay_refused():
    out = run(P, [("what changes", "effect", [blk("The finance controller approves these purchases.", [fid(P, "request")])])], question="What changes?")
    assert [r["reason"] for r in out["rejected"]] == ["proposal_stated_as_in_effect"]
    st = fid(P, "state")
    out = run(P, [("whether the approver signs", "premise", [blk("The current step is the knowledge decision.", [st])])], question="Does the approver sign off?",
              unrecorded=["approver"])
    assert any(r.get("reason") == "record_may_hold_it" for r in out["rejected"])


# ---- judgement questions: a certainty the record cannot give -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, refused", [
    ("Nothing in the record shows a failure: no blocker is recorded and the current step has not started yet.", False),
    ("It has not reached its turn yet: the current step has not started.", False),
    ("The record does not show that nothing is wrong.", False),
    ("Nothing is wrong with the request; it is simply waiting.", True),
    ("Progress is healthy: two steps are done.", True),
    ("The request is on track.", True),
    ("The person who asked is deliberately delaying it.", True),
    ("There are no problems with this request.", True),
])
def test_a_judgement_is_answered_within_what_the_record_supports(text, refused):
    out = run(P, [("did it go wrong", "status", [blk(text, [fid(P, "state")], "interpretation")])], question="Did something go wrong?")
    assert (any(r["reason"] == "status_contradicts_record" for r in out["rejected"])) is refused, out["rejected"]


# ---- config 28 --------------------------------------------------------------------------------------------------------------------------------------------------

def test_config28_rules_are_present_neutral_and_carry_no_evaluation_set_wording():
    t = CFG["prompt_template"]
    assert CFG["version"] == 32 and CFG["recovery"]["primary_deadline_s"] == 19 and CFG["limits"]["max_prompt_chars"] == 20000   # config 30 keeps the next rule
    assert "- Readings: when a part asks which description fits" in t                                    # config 30: the judgement rule is the readings rule
    assert "- Next (part next): start from the current or blocked step and what any offered blocker or open question says must be resolved first" in t
    assert "Name no actor or order the facts do not give." in t
    assert t.count("as part action") == 1 and "Do not infer absence of a product control" not in t                # the repeated sentences are gone
    i, j = t.index("- Readings:"), t.index("- Status (part status)")
    k = t.index("- Next (part next)")
    rules = (t[i:j] + t[k:t.index("\n", k)]).lower()
    for word in ("high-risk", "supplier", "operations director", "finance controller", "capex", "jpy", "objection", "priya", "chief risk", "h7-"):
        assert word not in rules
