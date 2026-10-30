"""checks/30 (R23, after the config 32 development run). Pure: no DB, no model (the routes and the browser are in tests/final_loop/). Expected outcomes are written
by hand from the fixture records. (1) A sentence-initial whether-clause that is the subject of "is not shown / recorded ..." is an embedded question, by its
span: a valid uncertainty is kept; a certainty, a claim in another clause and a sentence mixing a valid and an invalid statement are refused. (2) A limitation
kept through an honest-absence form that rests on a record fact carries that provenance: the fact for the later-steps form, the server's absence statement for
a verified-absent name; nothing for a time absence; nothing for a refused limitation; the model's words unchanged; only facts of this reader's pack."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_checks27 import P, Q_NAME, kept_limitations
from tests.unit.test_request_assistant_context13 import WAIT
from tests.unit.test_request_assistant_integrity import fid


@pytest.mark.parametrize("text, refused", [
    ("Whether the lack of progress is normal or expected at this stage is not shown in the record.", False),    # the H7-36 limitation
    ("Whether the request is stalled is not recorded.", False),
    ("It is not shown whether progress is normal.", False),
    ("Whether or not anyone acts, progress is normal.", True),                                   # checks/18: no embedded question, a claim
    ("Whether it is normal is not shown, but progress is healthy.", True),                     # a claim in another clause
    ("Progress is normal; whether it continues is not shown.", True),                          # a valid and an invalid statement mixed
    ("Whether progress is normal is shown by the record: it is on track.", True),             # "is shown" asserts
    ("Whether progress is normal is unclear, and the request is stalled.", True),
])
def test_a_whether_clause_that_is_the_subject_of_not_shown_is_an_embedded_question(text, refused):
    out = run(WAIT, [("is it normal", "status", [blk("The current step has not started yet.", [fid(WAIT, "state")])])], question="Is that normal?", limitations=[text])
    assert (any(r["reason"] == "status_contradicts_record" for r in out["rejected"])) is refused, out["rejected"]
    out = run(WAIT, [("is it normal", "status", [blk(text, [fid(WAIT, "state")], "interpretation")])], question="Is that normal?")
    assert (any(r["reason"] == "status_contradicts_record" for r in out["rejected"])) is refused, out["rejected"]


def _support(out):
    return {s["text"]: s for s in out.get("limitation_support") or []}


def test_a_kept_later_steps_absence_carries_the_fact_it_rests_on_and_the_words_are_unchanged():
    later = "The facts do not record who acts on later steps until each step starts."
    st = fid(P, "state")
    out = run(P, [("what happens next", "next", [blk("The request is at the knowledge decision stage.", [st])])], question="What happens next?", limitations=[later])
    holders = next(f["id"] for f in P["facts"] if f.get("key") == "steps:later:holders")
    assert later in out["limitations"] and _support(out)[later] == {"text": later, "basis": "later_step_holders", "fact_refs": [holders]}
    assert all(r in {f["id"] for f in P["facts"]} for s in out["limitation_support"] for r in s["fact_refs"])       # only this reader's facts


def test_a_verified_absent_name_carries_the_servers_statement_and_a_time_absence_or_a_refused_limitation_carries_nothing():
    st = fid(P, "state")
    asks = [("whether the Chief Risk Officer decides", "premise", [blk("The current step is the knowledge decision.", [st])])]
    name = "The facts do not show whether the Chief Risk Officer has the final word on this request."
    out = run(P, asks, question=Q_NAME, limitations=[name, "The facts do not show a date for the decision.", "The facts do not show who approves the current step."],
              unrecorded=["Chief Risk Officer"])
    sup = _support(out)
    assert sup[name]["basis"] == "verified_absent_name" and sup[name]["fact_refs"] == []
    assert sup[name]["server_statement"] == ["The record of this request does not name: Chief Risk Officer."]
    assert any(b.get("kind") == "absence" and b["text"] == sup[name]["server_statement"][0] for b in out["blocks"])   # the same statement the server shows
    assert "The facts do not show a date for the decision." in out["limitations"] and "The facts do not show a date for the decision." not in sup
    assert "The facts do not show who approves the current step." not in out["limitations"] and len(sup) == 1


def test_without_the_record_fact_there_is_no_support_and_the_limitation_is_refused():
    kept, refused = kept_limitations(["The facts do not record who acts on later steps until each step starts."])
    assert kept == ["The facts do not record who acts on later steps until each step starts."]          # the fixture P holds the holders fact
    from tests.unit.test_request_assistant_integrity import ob, pack_of, tv
    bare = pack_of(tv([ob()]))
    out = run(bare, [("next", "next", [blk("The request is at the knowledge decision stage.", [fid(bare, "state")])])], question="What next?",
              limitations=["The facts do not record who acts on later steps until each step starts."])
    assert out["limitation_support"] == [] and out["rejected"][0]["reason"] == "authority_in_free_text"


def test_the_version_is_checks30():
    assert ra.CHECKS_VERSION == "request-assistant-checks/30" and {"request-assistant-checks/28", "request-assistant-checks/29"} <= ra.COMPATIBLE_CHECKS
