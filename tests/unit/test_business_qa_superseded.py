"""Q&A in the accepted-target view when an observed rule lost a decided conflict (pure). Invariant: the losing rule may be cited as history or as what the sources say
when the question asks about it, but never presented as the current, decided rule. Diagnostic finding H-QA-06 (2026-10-17, non-independent): the model cited the policy rule
as what the sources say next to the decided rule; the evaluation label of H-QA-06 is unchanged and awaits the owner. These tests protect the invariant, not the label."""
from core.change_intelligence.v2 import business_qa as qa
from tests.unit.test_business_qa_applicability import FROZEN, RULES, item

QUESTION = "The policy document says CAPEX goes to the Finance Controller only from 5,000,000 JPY. Is that still the rule for a 3,000,000 JPY CAPEX request?"
TARGET = dict(item(RULES["rule-capex-finance-config"]), item_id="rule-capex-finance-config-target", authority="TARGET", status="CONFIRMED", decision_ids=["decision-capex"],
              implementation_status="pending",
              versions=[{"item_id": "rule-capex-finance-config", "authority": "OBSERVED", "status": "VALIDATED"},
                        {"item_id": "rule-capex-finance-config-target", "authority": "TARGET", "status": "CONFIRMED"}])


def pack():
    return {"query": QUESTION, "items": [TARGET, item(RULES["rule-capex-finance-policy"])],
            "counterevidence": [{"kind": "conflicting_rule", "rule_id": "rule-capex-finance-policy", "with": "rule-capex-finance-config", "title": RULES["rule-capex-finance-policy"]["title"],
                                 "authority": "OBSERVED", "text": RULES["rule-capex-finance-policy"]["title"]}],
            "observations": {"status": "OK", "results": []}, "pin": {"snapshot_id": "s", "snapshot_state": "ACCEPTED", "view": "target"}, "retrieval": {}, "semantic": {},
            "sufficiency": "unknown", "coverage": {}, "index": {}}


def judge(claims):
    return qa.judge_claims(claims, pack(), QUESTION, FROZEN, can_read_evidence=False)


def test_the_losing_rule_cited_as_what_the_sources_say_is_kept_with_the_observed_label_only():
    kept, dropped = judge([{"text": "The policy document states that CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "observed"},
                           {"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config-target"], "basis": "target"}])
    by = {c["refs"][0]: c for c in kept}
    assert dropped == [] and by["rule-capex-finance-policy"]["authority_labels"] == ["what the sources say"]
    assert by["rule-capex-finance-config-target"]["authority_labels"] == ["decided by a person"]


def test_the_losing_rule_can_never_be_presented_as_the_decided_rule():
    kept, dropped = judge([{"text": "CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "target"},
                           {"text": "CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "decision"}])
    assert kept == [] and {d["reason"] for d in dropped} == {"basis_does_not_match_cited_authority"}


def test_a_conflict_with_only_one_side_cited_is_disclosed_in_the_final_answer():
    kept, dropped = judge([{"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config-target"], "basis": "target"}])
    final = qa.finalize({"status": "ANSWERED", "answer": "Finance from 2,000,000 JPY.", "claims": [], "uncertainty": "", "clarifying_questions": []}, kept, dropped, pack(), FROZEN)
    assert final["conflicts"] and final["conflicts"][0]["rules"] == ["rule-capex-finance-config", "rule-capex-finance-policy"]


# ---- owner decision H-QA-06, option B (2026-10-18): a disagreement a valid decision settled does not by itself make a target-view answer partial ----------------
DECIDED = {"text": "CAPEX of 2,000,000 JPY or more goes to Finance (decided).", "refs": ["rule-capex-finance-config-target"], "basis": "target"}
HISTORY = {"text": "The policy document states that CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "observed"}


def _final(claims, *, question=QUESTION, view="target", target=None):
    pk = pack()
    pk["query"], pk["view"] = question, view
    if target is not None:
        pk["items"][0] = target
    kept, dropped = qa.judge_claims(claims, pk, question, FROZEN, can_read_evidence=False)
    return qa.finalize({"status": "ANSWERED", "answer": "Finance from 2,000,000 JPY.", "claims": claims, "uncertainty": "", "clarifying_questions": []}, kept, dropped, pk, FROZEN)


def test_target_view_answer_with_the_decided_rule_is_answered_and_still_discloses_the_old_source():
    final = _final([DECIDED, HISTORY], question="From which CAPEX amount does Finance review a request of 3,000,000 JPY?")
    assert final["status"] == "ANSWERED" and "sources_disagree_decided" in final["reasons"] and "sources_disagree" not in final["reasons"]
    assert final["conflicts"][0]["decided_by"] == "rule-capex-finance-config-target"
    rendered = qa.render(dict(final, view="target", pack={"pin": {}, "items": [], "retrieval": {}, "semantic": {}, "sufficiency": "unknown", "coverage": {}, "index": {}}),
                         mode="business", can_read_evidence=False, titles={"rule-capex-finance-config-target": "CAPEX decided"}, newer_knowledge=False)
    assert rendered["conflicts"][0]["decided"] is True and rendered["conflicts"][0]["decided_title"] == "CAPEX decided"


def test_what_is_in_force_in_the_files_still_reflects_the_unsynchronised_source():
    final = _final([DECIDED], question="Which CAPEX threshold is currently configured in the system for a 3,000,000 JPY request?")
    assert final["status"] == "PARTIAL" and "implementation_pending" in final["reasons"] and final["implementation_note"]


def test_observed_view_undecided_or_unstated_decisions_stay_partial():
    assert _final([DECIDED, HISTORY], view="observed")["status"] == "PARTIAL"                                  # the observed view reflects the sources as they are
    both_observed = [{"text": "One statement says 2,000,000 JPY.", "refs": ["rule-capex-finance-config"], "basis": "observed"}, HISTORY]
    final = _final(both_observed)
    assert final["status"] == "PARTIAL" and "sources_disagree" in final["reasons"]                                # the answer never states the decision
    undecided = dict(TARGET, decision_ids=[])
    assert _final([DECIDED, HISTORY], target=undecided)["status"] == "PARTIAL"                                   # no human decision behind the target



def test_a_claim_citing_the_decision_also_cites_the_rule_that_decision_decided():
    """Gate set v2 finding class (2026-10-18): an answer cited the person's decision and a passage but not the decided rule; the stored provenance links them."""
    pk = pack()
    pk["items"].append({"item_id": "decision-capex", "kind": "decision", "text": "Finance reviews CAPEX from 2,000,000 JPY.", "authority": "TARGET", "versions": []})
    kept, dropped = qa.judge_claims([{"text": "A person decided Finance reviews CAPEX from 2,000,000 JPY.", "refs": ["decision-capex"], "basis": "decision"}], pk,
                                    "Does a 3,000,000 JPY CAPEX purchase go to Finance?", FROZEN, can_read_evidence=False)
    assert dropped == [] and kept[0]["refs"] == ["decision-capex", "rule-capex-finance-config-target"] and kept[0]["refs_from_decision"] == ["rule-capex-finance-config-target"]
    other, _ = qa.judge_claims([{"text": "CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "observed"}], pk,
                               "What does the policy say about CAPEX?", FROZEN, can_read_evidence=False)
    assert "refs_from_decision" not in other[0] and other[0]["refs"] == ["rule-capex-finance-policy"]
