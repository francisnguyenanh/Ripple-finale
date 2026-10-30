"""answer/6, checks/20-21, context/7 (R16; contract REQUEST_TASKS_AND_ASSISTANT section 7). Pure: no DB, no model. The question is answered part by part; the server
names the facts that answer each kind of part; a name the question brings that the record does not hold is declared by the model, verified by the server
(provenance: a phrase of the question whose words occur in no fact) and stated by the server, only when the answer has a premise part (checks/21, A131: ordinary
question words were declared and honest sentences refused); the blocks of the premise part may not mention it, whatever the wording or case; a limitation or a
block of another part may; a part with no kept block is disclosed. No capital letter, role list or negation word decides anything here. Every test fails on checks/19 (no `asks`, no
`verify_unrecorded`, no `parties_fact`, no `basis_map`)."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_integrity import LIM5, LIMITS, fid, ob, pack_of, tv

Q = "The Treasury Manager also needs to sign this before it can close, don't they?"


def six(asks, unrecorded=(), limitations=(), outcome="answered"):
    return ra.validate_model_object({"outcome": outcome, "asks": [{"ask": a, "part": p, "blocks": list(b)} for a, p, b in asks], "unrecorded": list(unrecorded),
                                      "limitations": list(limitations)}, LIM5)


def blk(text, refs, kind="fact"):
    return {"text": text, "fact_refs": list(refs), "kind": kind}


def run(p, asks, question=Q, **kw):
    return ra.check_answer(six(asks, **kw), p, LIMITS, question=question)


# ---- shape -------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_live_shape_is_asks_with_parts_flattened_for_the_checks_and_anything_else_is_a_schema_violation():
    m = six([("where it stands", "status", [blk("a", ["F1"])]), ("what next", "next", [blk("b", ["F1"]), blk("c", ["F1"], "interpretation")])], unrecorded=["x"])
    assert [(b["text"], b["part"], b["ask"]) for b in m["answer_blocks"]] == [("a", "status", 0), ("b", "next", 1), ("c", "next", 1)]
    assert m["asks"] == [{"ask": "where it stands", "part": "status"}, {"ask": "what next", "part": "next"}] and m["unrecorded"] == ["x"] and m["authority_assertions"] == []
    for bad in ({"outcome": "answered", "answer_blocks": [], "limitations": []},                                   # answer/5
                {"outcome": "answered", "asks": [], "unrecorded": [], "limitations": [], "authority_assertions": []},
                {"outcome": "answered", "asks": [{"ask": "a", "part": "weather", "blocks": []}], "unrecorded": [], "limitations": []},
                {"outcome": "answered", "asks": [{"ask": "a", "part": "status"}], "unrecorded": [], "limitations": []},
                {"outcome": "answered", "asks": [{"ask": "a", "part": "status", "blocks": []}] * 5, "unrecorded": [], "limitations": []},
                {"outcome": "answered", "asks": [], "unrecorded": ["a", "b", "c", "d", "e"], "limitations": []},
                {"outcome": "answered", "asks": [], "unrecorded": [1], "limitations": []}):
        with pytest.raises(ValueError):
            ra.validate_model_object(bad, LIM5)
    assert ra.ANSWER_SCHEMA == "request-assistant-answer/6" and ra.CHECKS_VERSION == "request-assistant-checks/30" and {"request-assistant-checks/19", "request-assistant-checks/20", "request-assistant-checks/21"} <= ra.COMPATIBLE_CHECKS


# ---- the facts that answer each kind of part (deterministic from kinds and keys) ---------------------------------------------------------------------------------

def test_the_basis_names_the_facts_per_part_from_kinds_and_keys_and_the_prompt_carries_it():
    p = pack_of(tv([ob(me=True, may=True)], blockers=[{"code": "km", "text": "The business knowledge moved."}]),
                extra=[ra.fact("rule_change", "Computed by the server for the rule 'x', by order jpy, cases that would stop meeting the condition: none.", "computed", key="rule_change:draft:000:range"),
                       ra.fact("rule_change", "Before: who acts under the rule: logistics | applies when: order at least 5 | exceptions: none | applies to (scope): process: STANDARD", "draft", key="rule_change:draft:000"),
                       ra.decision_status_fact([]), ra.parties_fact(["logistics"])])
    b = ra.basis_map(p["facts"])
    ids = {k: fid(p, k) for k in ("state", "obligation", "action", "position", "blocker", "decision_status")} if any(f["kind"] == "action" for f in p["facts"]) else \
          {k: fid(p, k) for k in ("state", "obligation", "position", "blocker", "decision_status")}
    rng = next(f["id"] for f in p["facts"] if f["key"] == "rule_change:draft:000:range")
    draft = next(f["id"] for f in p["facts"] if f["key"] == "rule_change:draft:000")
    parties = next(f["id"] for f in p["facts"] if f["key"] == "parties")
    assert set(b) == set(ra.PARTS) - {"action", "other"}
    assert ids["state"] in b["status"] and ids["blocker"] in b["status"] and ids["obligation"] not in b["status"]
    assert {ids["position"], ids["obligation"]} <= set(b["obligation"]) and b["decision"] == [ids["decision_status"]]
    assert set(b["change"]) == {draft, rng, fid(p, "request")} and set(b["effect"]) == {draft, rng, fid(p, "request"), parties}  # context/10: the effect has the proposal (the after side) too
    assert {ids["position"], ids["state"], ids["decision_status"], ids["blocker"]} <= set(b["premise"]) and ids["position"] in b["next"] and ids["decision_status"] in b["next"]
    assert ids["blocker"] in b["reason"] and rng in b["reason"] and ids["blocker"] in b["how"] and ids["obligation"] in b["how"] and ids["state"] not in b["reason"]
    text = ra.basis_text(p["facts"])
    assert text.startswith("status (where the request stands") and f"effect (who or what would be affected and how (the rule before vs after)): {min(rng, draft, fid(p, 'request'), parties, key=lambda x: int(x[1:]))}" in text
    cfg = {"prompt_template": "<basis>\n{basis}\n</basis>\n{facts}\n{question}", "limits": {"max_prompt_chars": 20000}}
    prompt = ra.build_prompt(cfg, p, [], "Who is affected?", perspective="requester")
    assert "<basis>\n" + text + "\n</basis>" in prompt


def test_the_parties_fact_says_the_record_names_nobody_and_that_the_actor_applies_the_rule():
    f = ra.parties_fact(["logistics", "finance controller", "logistics"])
    assert f["kind"] == "impact" and f["key"] == "parties" and f["source"] == "computed" and f["tier"] == "support" and f["data"] == {"named": []}
    assert f["text"] == ("Who the record names as affected by the proposed change: no person, team or customer group is named; which cases would change is given by the "
                         "computed range fact. The actor of the changed rule(s), who applies the rule (finance controller, logistics), is not by that the one affected.")
    assert ra.parties_fact([])["text"].endswith("given by the computed range fact.") and len(f["text"]) <= ra.MAX_FACT_CHARS


# ---- names outside the record: declared, verified by provenance, stated by the server --------------------------------------------------------------------------

P = pack_of(tv([ob(me=True, may=True)]), extra=[ra.fact("rule_change", "Rule to change: Purchases from high-risk suppliers go to the Operations Director regardless of amount", "review", key="rule_change:scope:x"),
                                                 ra.fact("request", "The confirmed request: approval by the Finance Controller instead of the Operations Director.", "review", key="request:confirmed")])


@pytest.mark.parametrize("question, entries, verified, reasons", [
    (Q, ["Treasury Manager"], ["Treasury Manager"], []),
    (Q, ["treasury manager", "TREASURY MANAGER"], ["treasury manager"], []),                                     # case-insensitive, once
    ("will the treasury manager sign it?", ["the treasury manager"], ["the treasury manager"], []),              # a lower-case title: no capital needed
    (Q, ["sign this before it can close"], ["sign this before it can close"], []),                               # a phrase of the question whose words the record lacks
    (Q, ["Treasury Manager sign-off"], [], ["not_from_the_question"]),                                           # not the question's words: the model's own phrase
    ("Does the Finance Controller decide this?", ["Finance Controller"], [], ["record_may_hold_it"]),            # the record holds the title
    ("Does the finance controller's approval count?", ["finance controller's approval"], [], ["record_may_hold_it"]),   # every content word is in the record (approval ~ approved)
    ("Is the Operations Director still the approver?", ["Operations Director"], [], ["record_may_hold_it"]),
    ("Is the director still the approver?", ["the director"], [], ["record_may_hold_it"]),
    ("Does the CFO have the final say here?", ["CFO"], ["CFO"], []),
    ("Will Hanako approve this before Friday?", ["Hanako", "Friday"], ["Hanako", "Friday"], []),
    (Q, ["", "a" * 90, "Treasury <Manager>"], [], ["not_plain_text", "not_plain_text", "not_plain_text"]),
    (Q, ["the", "it"], [], ["record_may_hold_it", "record_may_hold_it"]),                                         # no content word: nothing to verify
])
def test_a_declared_name_is_verified_only_as_a_phrase_of_the_question_whose_words_occur_in_no_fact(question, entries, verified, reasons):
    kept, rejected, words = ra.verify_unrecorded(entries, question, P["facts"])
    assert kept == verified and [r["reason"] for r in rejected] == reasons and all(r["kind"] == "unrecorded" for r in rejected)
    for v in verified:
        assert words[v] and all(w not in ra._record_words(P["facts"]) for w in words[v])


@pytest.mark.parametrize("text", [
    "The Treasury Manager must sign before it can close.",
    "Without the treasury manager's signature it cannot close.",
    "There is no Treasury Manager signature required.",
    "The Treasury Manager does not need to sign.",
    "It is unknown whether the Treasury Manager needs to approve this request.",
    "The facts name no Treasury Manager for this request.",                                                    # honest, but redundant: the server states it
    "The facts name no Treasury Manager, but the Treasury Manager must sign.",
    "Treasury sign-off is handled elsewhere.",                                                                 # one word of the name the record lacks
])
def test_a_premise_block_that_mentions_a_verified_name_is_refused_whatever_the_wording_and_a_limitation_or_another_part_may_name_it(text):
    st = fid(P, "state")
    out = run(P, [("whether the Treasury Manager signs", "premise", [blk(text, [st])]), ("the stage", "status", [blk("The Treasury Manager is not part of the recorded steps; the stage is the knowledge decision.", [st])])],
              unrecorded=["Treasury Manager"], limitations=["Whether a Treasury Manager is involved elsewhere is not shown."])
    assert [(r["kind"], r["reason"]) for r in out["rejected"]][:1] == [("fact", "unrecorded_name_in_prose")] and all(r.get("part", "premise") == "premise" or r["kind"] != "fact" for r in out["rejected"])
    assert [b["text"] for b in out["blocks"] if b["kind"] == "absence"] == ["The record of this request does not name: Treasury Manager."]
    assert "Whether a Treasury Manager is involved elsewhere is not shown." in out["limitations"]                       # checks/21: a limitation may name it (authority guards still apply)
    assert any(b.get("part") == "status" for b in out["blocks"])                                                        # another part's block is not guarded by the name


def test_without_a_premise_part_nothing_is_stated_or_guarded_and_the_declaration_is_recorded_as_refused():
    st = fid(P, "state")
    out = run(P, [("whether it is parked", "status", [blk("The record shows no blocker, so nothing indicates it is parked.", [st])])], unrecorded=["parked"],
              limitations=["Whether the request has been parked is not shown."], question="Has this request been parked deliberately?")
    assert not [b for b in out["blocks"] if b["kind"] == "absence"] and out["rejected"] == [{"unrecorded": 0, "kind": "unrecorded", "reason": "no_premise_part"}]
    assert [b["text"] for b in out["blocks"] if b["kind"] != "authority"] == ["The record shows no blocker, so nothing indicates it is parked."]
    assert out["limitations"] == ["Whether the request has been parked is not shown."] and out["parts"]["unrecorded_verified"] == [] and out["status"] == "partial"


@pytest.mark.parametrize("text, refused", [
    ("No person, team or customer group is named as affected by the proposed change.", False),             # A131 H4-09: the parties sentence
    ("No. The request is not blocked.", True), ("Yes, it is waiting for the knowledge decision.", True), ("No", True), ("Yes - see the stage.", True),
    ("Nobody outside the record is named.", False), ("Yesterday's decision is no longer current.", False),
])
def test_yes_or_no_is_an_answer_token_only_when_bare(text, refused):
    st = fid(P, "state")
    out = run(P, [("the parties", "effect", [blk(text, [st])])])
    assert ([r["reason"] for r in out["rejected"]] == ["unbound_affirmation"]) is refused, out["rejected"]


def test_the_server_states_each_verified_absence_as_a_task_record_block_and_counts_it_apart_from_the_model():
    st = fid(P, "state")
    out = run(P, [("whether the Treasury Manager signs", "premise", [blk("The current step is the knowledge decision.", [st])]),
                  ("what closure involves", "next", [blk("Nothing is pending from an earlier decision.", [st], "interpretation")])],
              unrecorded=["Treasury Manager", "sign this before it can close", "Finance Controller"], question=Q + " And does the Finance Controller sign?")
    absences = [b for b in out["blocks"] if b["kind"] == "absence"]
    assert [b["text"] for b in absences] == ["The record of this request does not name: Treasury Manager.", "The record of this request does not name: sign this before it can close."]
    assert all(b["origin"] == "task_record" and b["fact_refs"] == [st] for b in absences)
    assert out["blocks"][0]["kind"] == "authority" and out["blocks"].index(absences[0]) < out["blocks"].index(next(b for b in out["blocks"] if b["kind"] == "fact"))
    assert [r for r in out["rejected"]] == [{"unrecorded": 2, "kind": "unrecorded", "reason": "record_may_hold_it"}]
    assert out["status"] == "partial" and {k: v for k, v in out["parts"].items() if k != "coverage"} == {"asked": ["premise", "next"], "not_answered": 0, "unrecorded_verified": ["Treasury Manager", "sign this before it can close"]}
    assert out["parts"]["coverage"] == [{"ask": "whether the Treasury Manager signs", "part": "premise", "blocks": 1, "cited": [st]}, {"ask": "what closure involves", "part": "next", "blocks": 1, "cited": [st]}]
    assert out["authority_accounting"]["absences_stated"] == 2 and out["authority_accounting"]["shown_authority"] == 3
    assert not ra.semantic_success({**out, "blocks": [b for b in out["blocks"] if b["kind"] in ("authority", "absence")], "explained_fact_kinds": []})   # server lines alone are no answer
    assert out["explained_fact_kinds"] == [] and ra.blocker_coverage(out["blocks"], P["facts"])["explained"] == []


def test_without_a_question_or_a_declaration_nothing_is_stated_and_the_older_guards_still_decide():
    st = fid(P, "state")
    quiet = run(P, [("whether the Treasury Manager signs", "premise", [blk("The Treasury Manager must sign before it can close.", [st]),
                                                                       blk("Closing needs the Treasury Manager.", [st])])])
    assert not [b for b in quiet["blocks"] if b["kind"] == "absence"] and [r["reason"] for r in quiet["rejected"]] == ["authority_in_free_text"]   # the frame "must sign before it can close" is a claim about this request
    assert [b["text"] for b in quiet["blocks"] if b["kind"] != "authority"] == ["Closing needs the Treasury Manager."]            # STATED LIMIT: an undeclared name in another frame is measured, not caught
    none = run(P, [("whether the Treasury Manager signs", "premise", [blk("The request is at the knowledge decision step.", [st])])], unrecorded=["Treasury Manager"], question="")
    assert none["parts"]["unrecorded_verified"] == [] and [r["reason"] for r in none["rejected"]] == ["not_from_the_question"] and none["status"] == "partial"


# ---- every part of the question gets a block or is disclosed ------------------------------------------------------------------------------------------------------

def test_an_ask_with_no_kept_block_is_disclosed_in_its_own_words_and_makes_the_answer_partial():
    st = fid(P, "state")
    out = run(P, [("who is affected", "effect", [blk("Orders between the two thresholds would lose free freight.", [st])]), ("how they are affected", "effect", []),
                  ("what happens next", "next", [blk("You must decide it next.", [st])])])
    assert out["status"] == "partial" and out["limitations"] == ["Not answered in this answer: how they are affected.", "Not answered in this answer: what happens next."]
    assert out["parts"]["not_answered"] == 2 and [r["reason"] for r in out["rejected"]] == ["contradicts_cited_obligation"]
    full = run(P, [("who is affected", "effect", [blk("Orders between the two thresholds would lose free freight.", [st])])])
    assert full["status"] == "answered" and full["limitations"] == [] and {k: v for k, v in full["parts"].items() if k != "coverage"} == {"asked": ["effect"], "not_answered": 0, "unrecorded_verified": []}


def test_an_ask_whose_words_carry_authority_or_a_wrong_status_is_disclosed_generically():
    st = fid(P, "state")
    out = run(P, [("whether you must decide it", "obligation", []), ("a <b> part", "other", [])])
    assert out["limitations"] == ["Not answered in this answer: a part of the question (what the person is asked to do or owes now).",
                                  "Not answered in this answer: a part of the question (another part about this request)."]
    assert out["status"] == "refused"                                                                   # nothing of the model's survived


def test_the_kept_blocks_carry_their_part_and_a_declined_or_out_of_scope_answer_gets_no_coverage_notes():
    st = fid(P, "state")
    out = run(P, [("the stage", "status", [blk("The request is at the knowledge decision step.", [st])])])
    assert [(b["kind"], b.get("part")) for b in out["blocks"] if b["kind"] != "authority"] == [("fact", "status")]
    oos = run(P, [("another request", "other", [])], outcome="out_of_scope")
    assert oos["status"] == "out_of_scope" and oos["limitations"] == [] and oos["parts"]["not_answered"] == 0
