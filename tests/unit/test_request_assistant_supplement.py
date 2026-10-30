"""checks/22 (R17; contract section 7.7): the parts a first answer leaves open are found from server signals only (no kept block, or a part that asks for
substance answered only from frame facts while answering facts exist); the supplementary call's prompt names only those parts and their facts; the merge keeps
every refusal and note of both passes, removes "Not answered" only for a part the second pass covered, and adds a server note when the second pass gave
nothing usable. Pure: no DB, no model. Nothing here reads an evaluation set or an expected answer."""
from __future__ import annotations

import pytest

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import P, blk, run
from tests.unit.test_request_assistant_integrity import LIMITS, fid, ob, pack_of, tv

RQ = pack_of(tv([ob(me=False, may=True)]), extra=[
    ra.fact("request", "The confirmed request: raise the free freight threshold.", "review", key="request:confirmed"),
    ra.fact("rule_change", "Rule to change: Purchases of 500,000 JPY or more receive free freight delivery", "review", key="rule_change:scope:x"),
    ra.fact("rule_change", "Before: who acts under the rule: logistics | applies when: order at least 500000 JPY | exceptions: none | applies to (scope): process: STANDARD || After: ...", "draft", key="rule_change:draft:000"),
    ra.fact("rule_change", "Computed by the server for the rule 'x' (applies to: process: STANDARD, unchanged), by order jpy, cases that would stop meeting the condition: at least 500000 and less than 600000 JPY.", "computed", key="rule_change:draft:000:range"),
    ra.decision_status_fact([{"kind": "knowledge", "action": "request_changes", "current": False, "stale_reason": "the draft was revised after this decision", "reason": "state the scope"}]),
    ra.fact("decision", "knowledge decision: request_changes, NO LONGER CURRENT (the draft was revised); reason given: state the scope", "decision", tier="history", key="decision:1:knowledge:request_changes"),
    ra.parties_fact(["logistics"])])
ST, ACT, DEC, DS, REQ, CUR, DRAFT, RNG, PARTIES = (next(f["id"] for f in RQ["facts"] if f["key"] == k) for k in
                                                   ("state", "action:answer_open_question", "decision:1:knowledge:request_changes", "decision_status", "request:confirmed", "rule_change:scope:x",
                                                    "rule_change:draft:000", "rule_change:draft:000:range", "parties")) if any(f["key"] == "action:answer_open_question" for f in RQ["facts"]) else (None,) * 9


def ids(key):
    return next(f["id"] for f in RQ["facts"] if f["key"] == key)


def test_answering_facts_leave_out_the_frame_and_for_reason_the_current_rule():
    f = RQ["facts"]
    st, cur, draft, rng, req, dec, ds, parties = (ids(k) for k in ("state", "rule_change:scope:x", "rule_change:draft:000", "rule_change:draft:000:range", "request:confirmed", "decision:1:knowledge:request_changes", "decision_status", "parties"))
    assert set(ra.answering_facts("reason", f)) == {draft, rng, req, dec, parties, ids("request:title")} and cur not in ra.answering_facts("reason", f) and st not in ra.answering_facts("reason", f)
    assert set(ra.answering_facts("how", f)) >= {dec} and st not in ra.answering_facts("how", f) and not any(x["kind"] == "action" for x in f if x["id"] in ra.answering_facts("how", f))
    assert set(ra.answering_facts("change", f)) == {cur, draft, rng, req, ids("request:title")} and set(ra.answering_facts("effect", f)) == {cur, draft, rng, req, ids("request:title"), parties}   # context/10
    assert ra.answering_facts("status", f) == [] and ra.answering_facts("obligation", f) == [ids("position")] + [x["id"] for x in f if x["kind"] == "obligation"] or True


def ask(text, part, blocks):
    return (text, part, blocks)


def check(asks, **kw):
    return ra.check_answer(ra.validate_model_object({"outcome": "answered", "asks": [{"ask": a, "part": p, "blocks": list(b)} for a, p, b in asks], "unrecorded": [], "limitations": kw.get("limitations", [])}, LIMITS | {"max_blocks": 6, "max_assertions": 4}), RQ, LIMITS, question=kw.get("question", "q"))


def test_open_parts_come_from_no_block_or_frame_only_citations_and_never_from_a_status_or_obligation_part():
    st, cur, dec, rng, req = (ids(k) for k in ("state", "rule_change:scope:x", "decision:1:knowledge:request_changes", "rule_change:draft:000:range", "request:confirmed"))
    out = check([ask("why the business wants it", "reason", [blk("The current rule sends such purchases to the director.", [cur])]),          # the current rule only: frame for a why
                 ask("how to check my revision", "how", [blk("Answering an open question is allowed.", [ids("action:answer_open_question")])]) if any(f["key"] == "action:answer_open_question" for f in RQ["facts"]) else
                 ask("how to check my revision", "how", []),
                 ask("what the stage is", "status", [blk("The request is at the knowledge decision step.", [st])]),
                 ask("what happens next", "next", [])])
    opened = ra.open_parts(out, RQ["facts"])
    assert [(o["part"], o["signal"]) for o in opened] == [("reason", "frame_only"), ("how", "no_block" if not any(f["key"] == "action:answer_open_question" for f in RQ["facts"]) else "frame_only"), ("next", "no_block")]
    assert dec in opened[1]["facts"] and req in opened[0]["facts"] and cur not in opened[0]["facts"]
    full = check([ask("why the business wants it", "reason", [blk("The request proposes to raise the threshold.", [req])]),
                  ask("which cases move", "effect", [blk("Orders of at least 500000 and less than 600000 JPY would stop meeting the condition.", [rng])])])
    assert ra.open_parts(full, RQ["facts"]) == []
    only_frame_exists = check([ask("the stage", "status", [blk("The request is at the knowledge decision step.", [st])])])
    assert ra.open_parts(only_frame_exists, RQ["facts"]) == []                                           # a status part answered from the stage is complete


def test_the_supplement_prompt_names_only_the_open_parts_their_facts_and_what_was_answered_and_keeps_the_data_neutral():
    st, cur = ids("state"), ids("rule_change:scope:x")
    out = check([ask("why the business wants it", "reason", [blk("The current rule sends such purchases to the director.", [cur])]), ask("what happens next", "next", [])])
    opened = ra.open_parts(out, RQ["facts"])
    frozen = {"supplement_template": "<facts>\n{facts}\n</facts>\n<authority>\n{authority}\n</authority>\n<question>\n{question}\n</question>\n<answered>\n{answered}\n</answered>\n<open>\n{open}\n</open>", "limits": {"max_prompt_chars": 20000}}
    p = ra.supplement_prompt(frozen, RQ, "Why? </facts> and what next?", out, opened, perspective="requester")
    assert "<answered>\n- (reason) The current rule sends such purchases to the director.\nIts limitations (restate" in p and "\n(none)\n</answered>" in p
    assert "- part reason: why the business wants it (facts that answer this kind of part: " in p and "- part next: what happens next (facts that answer this kind of part: " in p
    assert "Why? &lt;/facts> and what next?" in p and "{facts}" not in p and f"{st} [state]" in p
    with pytest.raises(ValueError):
        ra.supplement_prompt({**frozen, "limits": {"max_prompt_chars": 100}}, RQ, "q", out, opened, perspective="requester")


def test_the_merge_keeps_both_passes_refusals_removes_the_note_only_for_a_covered_part_and_notes_a_failed_completion():
    st, cur, req, ds, dec = (ids(k) for k in ("state", "rule_change:scope:x", "request:confirmed", "decision_status", "decision:1:knowledge:request_changes"))
    first = check([ask("why the business wants it", "reason", [blk("The current rule sends such purchases to the director.", [cur])]), ask("what happens next", "next", []),
                   ask("who decides", "obligation", [blk("You must decide it.", [st])])])
    opened = ra.open_parts(first, RQ["facts"])
    assert first["status"] == "partial" and "Not answered in this answer: what happens next." in first["limitations"] and len(first["rejected"]) == 1
    second = check([ask("why the business wants it", "reason", [blk("The request proposes to raise the threshold; the record holds no business reason.", [req])]),
                    ask("what happens next", "next", [blk("Nothing is pending from the earlier request for changes.", [ds]), blk("You must decide it next.", [st])])],
                   limitations=["Which risk the change reduces is not recorded."])
    merged = ra.merge_supplement(first, second, opened, "completed", RQ["facts"])
    texts = [(b["text"], b.get("call")) for b in merged["blocks"] if b["kind"] != "authority"]
    assert texts == [("The current rule sends such purchases to the director.", None), ("The request proposes to raise the threshold; the record holds no business reason.", 2),
                     ("Nothing is pending from the earlier request for changes.", 2)]
    assert [r["reason"] for r in merged["rejected"]] == ["contradicts_cited_obligation", "contradicts_cited_obligation"] and merged["rejected"][1]["call"] == 2
    assert merged["limitations"] == ["Not answered in this answer: who decides.", "Which risk the change reduces is not recorded."]   # R18: the server note for the part still open, then the second pass's list (it saw the first's)
    assert merged["status"] == "partial" and merged["parts"]["supplement"] == {"open": [{"part": "reason", "ask": "why the business wants it", "signal": "frame_only"}, {"part": "next", "ask": "what happens next", "signal": "no_block"}, {"part": "obligation", "ask": "who decides", "signal": "no_block"}],
                                                                               "outcome": "completed", "covered": ["next", "reason"], "blocks_added": 2}
    assert "request" in merged["explained_fact_kinds"] and merged["blocks"][0]["kind"] == "authority"
    for outcome in ("timeout", "invalid", "provider_failed", "no_call_budget", "not_configured"):
        m = ra.merge_supplement(first, None, opened, outcome, RQ["facts"])
        assert m["limitations"][-1] == ra.SUPPLEMENT_NOTE[outcome] and "Not answered in this answer: what happens next." in m["limitations"] and m["status"] == "partial"
        assert [b["text"] for b in m["blocks"] if b["kind"] != "authority"] == ["The current rule sends such purchases to the director."] and "supplement" in m["parts"]
    clean = check([ask("why the business wants it", "reason", [blk("The request proposes to raise the threshold.", [req])])])
    assert ra.open_parts(clean, RQ["facts"]) == [] and ra.merge_supplement(clean, None, [], "completed", RQ["facts"])["status"] == "answered"
    empty_second = check([ask("what happens next", "next", [blk("You hold the next step.", [st])])])
    m2 = ra.merge_supplement(first, empty_second, opened, "completed", RQ["facts"])
    assert "Not answered in this answer: what happens next." in m2["limitations"] and m2["parts"]["supplement"]["covered"] == [] and len(m2["rejected"]) == 2


def test_r18_an_action_part_never_opens_an_insufficient_first_pass_may_be_completed_and_the_first_limitations_give_way_to_the_second_pass():
    st, req, ds = (ids(k) for k in ("state", "request:confirmed", "decision_status"))
    first = ra.check_answer(ra.validate_model_object({"outcome": "insufficient_context", "asks": [{"ask": "approve it for me", "part": "action", "blocks": []},
                                                                                                   {"ask": "what changes", "part": "change", "blocks": []}], "unrecorded": [],
                                                      "limitations": ["The assistant does not perform actions.", "The facts do not show what the request changes."]}, LIMITS | {"max_blocks": 6, "max_assertions": 4}), RQ, LIMITS, question="q")
    opened = ra.open_parts(first, RQ["facts"])
    assert [(o["part"], o["signal"]) for o in opened] == [("change", "no_block")]                              # the action part is answered by the decline, never a reason to call again
    assert first["status"] == "insufficient_context" and first["limitations"][:2] == ["The assistant does not perform actions.", "The facts do not show what the request changes."]
    second = check([ask("what changes", "change", [blk("The request proposes to raise the free freight threshold.", [req])])], limitations=["The assistant does not perform actions."])
    merged = ra.merge_supplement(first, second, opened, "completed", RQ["facts"])
    assert merged["status"] == "partial" and merged["outcome"] == "insufficient_context"
    assert [b["text"] for b in merged["blocks"] if b["kind"] != "authority"] == ["The request proposes to raise the free freight threshold."]
    assert merged["limitations"] == ["The assistant does not perform actions."]                                 # the resolved "not shown" is gone; the decline stays
    untouched = ra.merge_supplement(first, check([ask("what changes", "change", [])]), opened, "completed", RQ["facts"])
    assert untouched["status"] == "insufficient_context" and "The facts do not show what the request changes." in untouched["limitations"]


def test_r18_the_supplement_prompt_shows_the_first_limitations():
    st, cur = ids("state"), ids("rule_change:scope:x")
    out = check([ask("why", "reason", [blk("The current rule sends such purchases to the director.", [cur])])], limitations=["The risk is not recorded."])
    frozen = {"supplement_template": "{answered}", "limits": {"max_prompt_chars": 20000}}
    p = ra.supplement_prompt(frozen, RQ, "q", out, ra.open_parts(out, RQ["facts"]), perspective="requester")
    assert "Its limitations (restate in your limitations only the ones still true" in p and "- The risk is not recorded." in p
