"""context/10 and config 26 (R23, chain A-US acceptance H7-01 / H7-07 / H7-28): three omission classes, fixed by class. Pure: no DB, no model.
- what would be different: the effect basis holds the proposal (request, Before / After, current rule, computed range), not the impact facts alone;
- a downside: read from the before / after only, as an interpretation in the proposal mood; a proposal stated as in force is still refused;
- what to check before deciding: a change part (the substance under review), not the controls sentence.
The prompt rules carry no title, actor, number or wording of any evaluation set. A local test does not show that the model follows them."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import request_assistant as ra
from tests.unit.test_request_assistant_answer6 import blk, run
from tests.unit.test_request_assistant_integrity import fid, ob, pack_of, tv

ROOT = pathlib.Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "config" / "request_assistant.json").read_text(encoding="utf-8"))

P = pack_of(tv([ob(me=True, may=True)]), extra=[
    ra.fact("request", "The confirmed request: orders from new vendors should be checked by the buyer instead of the auditor.", "review", key="request:confirmed"),
    ra.fact("rule_change", "Rule to change: Orders from new vendors go to the auditor", "review", key="rule_change:scope:x"),
    ra.fact("rule_change", "Before: who acts under the rule: auditor | applies when: vendor is NEW | exceptions: none | applies to (scope): process: STANDARD || "
                           "After: who acts under the rule: buyer | applies when: vendor is NEW | exceptions: none | applies to (scope): process: STANDARD", "draft",
            key="rule_change:draft:000"),
    ra.fact("impact", "Affected rule: Orders from new vendors go to the auditor (changed by this request)", "impact", key="impact:rk-1"),
    ra.parties_fact(["auditor", "buyer"], ranged=False)])


def key_id(key):
    return next(f["id"] for f in P["facts"] if f.get("key") == key)


def test_the_effect_basis_holds_the_proposal_so_the_after_side_can_be_cited():
    b = ra.basis_map(P["facts"])
    proposal = {key_id("request:confirmed"), key_id("rule_change:scope:x"), key_id("rule_change:draft:000")}
    assert proposal <= set(b["effect"]) and {key_id("impact:rk-1"), key_id("parties")} <= set(b["effect"])
    assert proposal <= set(ra.answering_facts("effect", P["facts"]))                         # also the server's signal of an effect part answered from the frame only
    assert set(b["how"]).isdisjoint(proposal) and key_id("rule_change:draft:000") in b["change"]
    assert ra.CONTEXT_VERSION in ("request-assistant-context/10", "request-assistant-context/11", "request-assistant-context/12", "request-assistant-context/13", "request-assistant-context/14")   # context/11 keeps the effect basis


def test_the_parties_fact_points_to_a_range_fact_only_when_the_pack_has_one():
    assert ra.parties_fact(["buyer"])["text"].startswith("Who the record names as affected by the proposed change: no person, team or customer group is named; "
                                                         "which cases would change is given by the computed range fact.")
    f = ra.parties_fact(["buyer"], ranged=False)
    assert "computed range" not in f["text"] and "given by the rule's before / after." in f["text"] and f["key"] == "parties" and len(f["text"]) <= ra.MAX_FACT_CHARS


def test_a_before_and_after_effect_block_in_the_proposal_mood_is_kept_and_the_after_rule_stated_as_in_force_is_refused():
    draft = key_id("rule_change:draft:000")
    ok = run(P, [("what would be different", "effect", [blk("Today the auditor checks orders from new vendors; the request proposes that the buyer would check them instead, "
                                                             "in the STANDARD process.", [draft])])], question="What would be different?")
    assert ok["rejected"] == [] and ok["status"] == "answered" and ok["parts"]["coverage"][0]["cited"] == [draft]
    bad = run(P, [("what would be different", "effect", [blk("The buyer checks orders from new vendors.", [draft])])], question="What would be different?")
    assert [r["reason"] for r in bad["rejected"]] == ["proposal_stated_as_in_effect"]


def test_a_downside_read_from_the_before_and_after_is_kept_as_an_interpretation_and_a_number_no_fact_gives_is_refused():
    draft = key_id("rule_change:draft:000")
    ok = run(P, [("downside of going ahead", "effect", [blk("If approved, the auditor would no longer check these orders; that oversight would move to the buyer.", [draft], "interpretation")])],
             limitations=["The record does not assess further risks of the change."], question="Is there a downside?")
    assert ok["rejected"] == [] and ok["limitations"] == ["The record does not assess further risks of the change."]
    bad = run(P, [("downside of going ahead", "effect", [blk("If approved, about 30 orders a month would no longer be checked by the auditor.", [draft], "interpretation")])],
              question="Is there a downside?")
    assert [r["reason"] for r in bad["rejected"]] == ["number_not_in_cited_facts"]


def test_config27_rules_name_each_class_neutrally_and_carry_no_evaluation_set_wording():
    t = CFG["prompt_template"]
    assert CFG["version"] == 32 and CFG["recovery"]["primary_deadline_s"] == 19 and CFG["limits"]["max_prompt_chars"] == 20000
    assert "\"what would be different\" gets both sides from the Before / After fact or the request" in t
    assert "Asked for a downside or risk of going ahead: describe neutrally what that difference would change" in t
    assert "without calling it good or bad" in t and "does not assess whether it is a disadvantage" in t      # config 27: a change of actor is not presumed a downside
    assert "gives up" not in t
    assert "\"What to check before deciding\" is a change part" in t and "The controls sentence does not answer it." in t
    assert "condition.  say" not in t                                                           # the stray fragment of version 22 is gone
    i = t.index("- Change and effect (parts change, effect)")
    rules = t[i:t.index("- Next (part next)")].lower()
    for word in ("high-risk", "supplier", "operations director", "finance controller", "capex", "jpy", "budget code", "h7-"):
        assert word not in rules
