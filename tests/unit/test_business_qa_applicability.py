"""Applicability of validated QA claims to the case the question describes (pure). SYNTHETIC replays of the two live Q10 answers of 2026-10-17 (Gemini 3.5 Flash-Lite applied the
manager and Finance thresholds to a software subscription; Gemini 3.8 Flash cited them as context) are the regression; the other cases are variants: inside / outside the
policy, mixed, category missing, other scope, an exception. No provider, no DB."""
import json
import pathlib

import pytest

from core.change_intelligence.v2 import business_qa as qa

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = json.loads((ROOT / "config" / "knowledge_qa.json").read_text(encoding="utf-8"))
RULES = {r["rule_id"]: r for r in json.loads((ROOT / "tests" / "fixtures" / "final_knowledge_loop" / "expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"]}
DISTRACTORS = {r["rule_id"]: r for r in json.loads((ROOT / "tests" / "fixtures" / "final_knowledge_loop" / "retrieval_distractors.json").read_text(encoding="utf-8"))["rules"]}


def item(rule):
    return {"item_id": rule["rule_id"], "kind": "rule", "text": rule["title"], "authority": rule["authority"], "status": rule["status"], "title": rule["title"], "versions": [],
            "condition": rule["condition"], "scope": rule["scope"], "exceptions": rule["exceptions"], "outcome": rule["outcome"], "actor_ref": rule["actor_ref"], "implementation_status": "verified"}


def pack(question, *rule_ids, observation=None):
    items = [item((RULES | DISTRACTORS)[r]) for r in rule_ids]
    obs = {"status": "OK", "results": ([{"observation_id": "obs-finance-guide", "raw_value": observation}] if observation else [])}
    return {"query": question, "items": items, "counterevidence": [], "observations": obs, "pin": {"snapshot_id": "s", "snapshot_state": "ACCEPTED"}, "retrieval": {}, "semantic": {},
            "sufficiency": "unknown", "coverage": {}, "index": {}}


def model(status, claims, questions=()):
    return {"status": status, "answer": " ".join(c["text"] for c in claims), "claims": claims, "uncertainty": "", "clarifying_questions": list(questions)}


def run(question, claims, *rule_ids, status="ANSWERED", observation=None, evidence=True):
    p = pack(question, *rule_ids, observation=observation)
    m = model(status, claims)
    kept, dropped = qa.judge_claims(m["claims"], p, question, FROZEN, can_read_evidence=evidence)
    return qa.finalize(m, kept, dropped, p, FROZEN), kept, dropped


Q10 = "Who approves a purchase of a type the policy does not mention, like a software subscription of 4 million yen?"
LITE_Q10 = [{"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"},
            {"text": "All purchasing commitments of 2,000,000 JPY or higher must be reviewed and authorized by the Finance Department prior to purchase order commitment.", "refs": ["obs-finance-guide"], "basis": "observed"},
            {"text": "Software licence renewals are reviewed by IT governance each quarter.", "refs": ["rule-license-renewal"], "basis": "observed"}]
GUIDE = "all purchasing commitments of 2,000,000 JPY or higher must be reviewed and authorized by the Finance Department"


def test_facts_are_read_from_the_question_and_the_pinned_default_is_an_assumption_not_a_fact():
    f = qa.question_facts(Q10, FROZEN["fact_defaults"])
    assert f["facts"]["request.net_amount_jpy"] == 4000000 and f["assumed"] == {"process": "STANDARD"} and "request.category" not in f["facts"]
    assert qa.question_facts("A CAPEX request for a supplier on the risk register is 3,000,000 JPY.", {})["facts"] == {"request.net_amount_jpy": 3000000, "request.category": "CAPEX", "supplier.risk": "HIGH"}
    assert qa.question_facts("Is there an exception for emergency purchases?", FROZEN["fact_defaults"])["facts"] == {"process": "EMERGENCY"}
    assert "request.net_amount_jpy" not in qa.question_facts("Is it 1,000,000 JPY or 5,000,000 JPY?", {})["facts"]                 # two amounts: not one concrete case


def test_replay_of_the_lite_q10_answer_keeps_the_type_agnostic_manager_rule_as_assumed_and_marks_the_passage_unevaluable():
    final, kept, dropped = run(Q10, LITE_Q10, "rule-standard-manager-review", "rule-capex-finance-config", "rule-license-renewal", observation=GUIDE)
    by = {c["refs"][0]: c for c in kept}
    assert by["rule-standard-manager-review"]["applicability"] == "applies" and by["rule-standard-manager-review"]["assumptions"] == ["process=STANDARD"]     # the manager rule is type-agnostic
    assert by["obs-finance-guide"]["applicability"] == "not_evaluated"                                                                                       # a passage, no structured rule
    assert by["rule-license-renewal"]["applicability"] == "conditional" and by["rule-license-renewal"]["conditional_on"] == ["purchase.category"]            # a renewal? the question does not say
    assert final["status"] == "PARTIAL" and {"some_claims_conditional", "assumed_defaults", "answer_text_replaced_by_claims"} <= set(final["reasons"])
    assert "assuming a standard purchase" in final["answer"] and "could not be checked against a structured rule" in final["answer"] and "Assumed: a standard purchase." in final["uncertainty"]
    assert dropped == []


def test_a_claim_whose_rule_needs_a_category_the_question_lacks_is_conditional_and_asks_for_it():
    claims = [{"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config"], "basis": "observed"}]
    final, kept, _ = run(Q10, claims, "rule-capex-finance-config")
    assert kept[0]["applicability"] == "conditional" and kept[0]["conditional_on"] == ["request.category"]
    assert final["status"] == "NEEDS_CLARIFICATION" and final["clarifying_questions"] == ["Is this a capital purchase (CAPEX) or an operating expense?"] and "applicability_undecided" in final["reasons"]
    assert final["answer"].startswith("Only if it is decided whether")


def test_a_rule_that_does_not_reach_the_case_is_dropped_never_asserted():
    claims = [{"text": "Capital acquisitions of 2,000,000 JPY or more go to Finance.", "refs": ["rule-capex-finance-config"], "basis": "observed"}]
    final, kept, dropped = run("Who approves an OPEX purchase of 4 million yen?", claims, "rule-capex-finance-config", "rule-standard-manager-review")
    assert kept == [] and dropped[0]["reason"] == "claim_not_applicable" and final["status"] == "ABSTAINED" and "claims_not_applicable_dropped" in final["reasons"]
    claims = [{"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}]
    final, kept, dropped = run("Does a 900,000 JPY purchase need a person to approve it?", claims, "rule-standard-manager-review")
    assert kept == [] and dropped[0]["reason"] == "claim_not_applicable"                                                          # 900,000 < 1,000,000: the condition does not hold


def test_a_scope_that_names_every_process_the_pack_knows_is_universal_and_a_stated_emergency_excludes_standard_only_rules():
    q = "A CAPEX request for a supplier on the risk register is 3,000,000 JPY. Who ends up with it and why?"
    claims = [{"text": "Purchases from high-risk suppliers go to the Operations Director regardless of amount.", "refs": ["rule-high-risk-director"], "basis": "observed"},
              {"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config"], "basis": "observed"}]
    final, kept, _ = run(q, claims, "rule-high-risk-director", "rule-capex-finance-config", "rule-standard-manager-review")
    assert all(c["applicability"] == "applies" for c in kept) and final["status"] == "ANSWERED"
    assert kept[0]["assumptions"] == [] and kept[1]["assumptions"] == []                                                          # STANDARD+EMERGENCY = every value the pack names
    claims = [{"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}]
    final, kept, dropped = run("Who approves an emergency purchase of 1,200,000 JPY?", claims, "rule-standard-manager-review", "rule-emergency-routing")
    assert kept == [] and dropped[0]["reason"] == "claim_not_applicable" and dropped[0]["detail"] == ["scope_excludes_this_case"]


def test_a_question_without_a_concrete_amount_is_not_checked_for_applicability():
    claims = [{"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config"], "basis": "observed"}]
    final, kept, _ = run("From which CAPEX amount does Finance review a request?", claims, "rule-capex-finance-config")
    assert kept[0]["applicability"] == "not_checked" and final["status"] == "ANSWERED" and final["case_facts"]["concrete"] is False


def test_an_exception_that_holds_removes_the_rule_and_an_undecidable_one_makes_it_conditional():
    rule = dict(RULES["rule-standard-manager-review"], exceptions=[{"exception_id": "exc-1", "text": "except purchases from the approved catalogue", "condition": {"field": "catalogue.approved", "op": "eq", "value": "true"}}])
    p = pack("Who signs off a 1.2 million yen purchase from the approved catalogue?", "rule-standard-manager-review")
    p["items"][0] = item(rule)
    claims = [{"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}]
    kept, dropped = qa.judge_claims(claims, p, p["query"], FROZEN, can_read_evidence=True)
    assert kept[0]["applicability"] == "conditional" and kept[0]["conditional_on"] == ["catalogue.approved"]                      # the exception's fact is not readable from the text
    kept, dropped = qa.judge_claims(claims, p, p["query"], FROZEN, can_read_evidence=True, facts={"facts": {"request.net_amount_jpy": 1200000, "process": "STANDARD", "catalogue.approved": "true"}, "assumed": {}, "amounts": 1})
    assert kept == [] and dropped[0]["detail"][0].startswith("exception_applies")


def test_the_one_amount_of_the_question_answers_every_jpy_amount_field_a_rule_compares_but_never_a_difference_field():
    claims = [{"text": "Purchases of 500,000 JPY or more receive free freight delivery.", "refs": ["rule-free-freight"], "basis": "observed"},
              {"text": "Orders under 500,000 JPY pay a 2,000 JPY shipping and handling charge.", "refs": ["rule-shipping-fee"], "basis": "observed"}]
    final, kept, dropped = run("Does a 650,000 yen order get free delivery?", claims, "rule-free-freight", "rule-shipping-fee")
    assert [c["applicability"] for c in kept] == ["applies"] and dropped[0]["reason"] == "claim_not_applicable"              # 650,000 >= 500,000: free freight applies, the fee rule does not
    assert final["status"] == "ANSWERED" or final["status"] == "PARTIAL"
    tol = DISTRACTORS["rule-price-tolerance"]
    assert qa.case_facts_for_rule(tol, {"request.net_amount_jpy": 650000}) == {"request.net_amount_jpy": 650000}         # invoice.price_difference_jpy is not the amount of the case


def test_an_amount_in_another_currency_is_never_converted_and_the_answer_asks_for_the_jpy_amount():
    """Live finding 2026-10-17 (diagnostic run): a question in USD was answered as if decidable. Without a JPY amount the server asks; nothing is converted."""
    claims = [{"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}]
    out, kept, _ = run("Does a purchase of 12,000 USD need manager approval?", claims, "rule-standard-manager-review")
    assert out["status"] == "NEEDS_CLARIFICATION" and "amount_not_in_jpy" in out["reasons"] and out["clarifying_questions"][0] == qa.FOREIGN_QUESTION and kept
    same, _, _ = run("Does a purchase of 1,200,000 JPY need manager approval?", claims, "rule-standard-manager-review")
    assert "amount_not_in_jpy" not in same["reasons"]
    for q, foreign in (("A $5,000 laptop?", True), ("Costs EUR 900", True), ("3,000 dollars for chairs", True), ("Free freight from 500,000 yen", False), ("Version 3.2 of the policy", False)):
        assert qa.question_facts(q)["foreign_amount"] is foreign, q


# ---- Q10 after the owner's decision of 2026-10-17 (option B: answer what the evidence supports, ask for the classification that is missing). The oracle changed after a
# business decision, not because a model improved. The four cases tell an ordinary purchase request, a NEW subscription, a licence RENEWAL and CAPEX apart.
ORDINARY = "Who approves a purchase request of 4 million yen for office chairs?"
RENEWAL = "Who approves the renewal of our existing software licence for 4 million yen?"
CAPEX_Q = "Who approves a CAPEX purchase of 4 million yen?"
MANAGER = {"text": "Purchase requests of 1,000,000 JPY or more need the department manager's approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}
CAPEX_CLAIM = {"text": "CAPEX of 2,000,000 JPY or more goes to Finance.", "refs": ["rule-capex-finance-config"], "basis": "observed"}
LIC_REVIEWED = {"text": "Software licence renewals are reviewed by IT governance each quarter.", "refs": ["rule-license-renewal"], "basis": "observed"}
LIC_APPROVED = {"text": "Software licence renewals are approved by IT governance.", "refs": ["rule-license-renewal"], "basis": "observed"}
ALL = ("rule-standard-manager-review", "rule-capex-finance-config", "rule-license-renewal")


def test_ordinary_purchase_request_the_type_agnostic_manager_rule_answers_and_finance_is_only_conditional():
    final, kept, _ = run(ORDINARY, [MANAGER, CAPEX_CLAIM], *ALL)
    by = {c["refs"][0]: c for c in kept}
    assert by["rule-standard-manager-review"]["applicability"] == "applies" and by["rule-capex-finance-config"]["applicability"] == "conditional"
    assert final["status"] in ("PARTIAL", "NEEDS_CLARIFICATION") and FACT_Q_CAPEX in final["clarifying_questions"]


FACT_Q_CAPEX = qa.FACT_QUESTIONS["request.category"]
FACT_Q_RENEWAL = qa.FACT_QUESTIONS["purchase.category"]


def test_new_subscription_manager_rule_from_the_evidence_capex_and_renewal_stay_open_and_are_asked():
    final, kept, dropped = run(Q10, [MANAGER, CAPEX_CLAIM, LIC_REVIEWED], *ALL)
    by = {c["refs"][0]: c for c in kept}
    assert by["rule-standard-manager-review"]["applicability"] == "applies"                                     # a purchase request in scope at 4M meets the manager threshold
    assert by["rule-capex-finance-config"]["applicability"] == "conditional"                                    # Finance only if it is CAPEX: never concluded
    assert by["rule-license-renewal"]["applicability"] == "conditional"                                         # a new subscription is not a confirmed renewal
    assert qa.question_facts(Q10)["facts"].get("purchase.category") is None
    assert final["status"] in ("PARTIAL", "NEEDS_CLARIFICATION") and {FACT_Q_CAPEX, FACT_Q_RENEWAL} <= set(final["clarifying_questions"]) and dropped == []


def test_reviewed_by_it_governance_is_never_turned_into_approved_by_it_governance():
    final, kept, dropped = run(RENEWAL, [MANAGER, LIC_APPROVED], *ALL)
    assert [d["reason"] for d in dropped] == ["claim_overstates_outcome"] and [c["refs"] for c in kept] == [["rule-standard-manager-review"]]
    ok, kept2, dropped2 = run(RENEWAL, [MANAGER, LIC_REVIEWED], *ALL)
    assert dropped2 == [] and {c["refs"][0]: c["applicability"] for c in kept2}["rule-license-renewal"] == "applies"            # a renewal the question states: the review applies
    assert "approved by it governance" not in (ok["answer"] or "").lower()


def test_capex_question_finance_rule_applies_and_the_threshold_conflict_is_kept_not_resolved():
    final, kept, _ = run(CAPEX_Q, [CAPEX_CLAIM, {"text": "CAPEX of 5,000,000 JPY or more goes to the Finance Controller.", "refs": ["rule-capex-finance-policy"], "basis": "observed"}],
                         "rule-capex-finance-config", "rule-capex-finance-policy")
    by = {c["refs"][0]: c for c in kept}
    assert by["rule-capex-finance-config"]["applicability"] == "applies"                                         # config/code: 2M -> Finance
    assert "rule-capex-finance-policy" not in by                                                                 # policy: 5M not reached by 4M -> not asserted
    assert final["status"] != "ABSTAINED"



# ---- omission the server can see (2026-10-18): an approval gate that reaches the stated case but that no kept claim cites. Diagnostic finding H-QA-01 (spent item, Gemini 3.5
# Flash-Lite cited only a passage on Finance and left out the manager rule that applies to a 3,000,000 JPY standard OPEX purchase). SYNTHETIC replays; no provider, no DB.
H01 = "An OPEX purchase of 3,000,000 JPY on the standard process: does Finance have to review it?"
FIN_PASSAGE = {"text": "Finance reviews CAPEX of 2,000,000 JPY or more; this purchase is OPEX.", "refs": ["obs-finance-guide"], "basis": "observed"}
H01_PACK = ("rule-standard-manager-review", "rule-capex-finance-config", "rule-travel-limit", "rule-free-freight", "rule-maker-checker")


def test_an_applicable_approval_gate_left_out_by_the_model_is_added_as_a_server_claim_and_the_answer_is_partial():
    final, kept, dropped = run(H01, [FIN_PASSAGE], *H01_PACK, observation=GUIDE)
    added = [c for c in final["claims"] if c.get("origin") == "server_applicability"]
    assert [c["refs"] for c in added] == [["rule-standard-manager-review"]] and added[0]["applicability"] == "applies" and added[0]["basis"] == "observed"
    assert "1,000,000 JPY or more need manager approval" in added[0]["text"] and added[0]["text"] in final["answer"]
    assert final["status"] == "PARTIAL" and "applicable_rule_not_cited" in final["reasons"] and dropped == []
    # not added: the travel rule (its field is not stated; the case amount never stands in for it here), CAPEX (the case is OPEX), free freight (not an approval gate),
    # maker-checker (whether the approver is the requester is not stated)
    assert {r for c in added for r in c["refs"]} == {"rule-standard-manager-review"}


def test_nothing_is_added_when_the_gate_needs_a_pinned_default_or_is_already_cited_or_the_model_abstained():
    no_process = "An OPEX purchase of 3,000,000 JPY: does Finance have to review it?"                      # STANDARD only by the pinned default -> not stated
    final, _, _ = run(no_process, [FIN_PASSAGE], *H01_PACK, observation=GUIDE)
    assert not [c for c in final["claims"] if c.get("origin")] and "applicable_rule_not_cited" not in final["reasons"]
    manager = {"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}
    final, _, _ = run(H01, [FIN_PASSAGE, manager], *H01_PACK, observation=GUIDE)
    assert not [c for c in final["claims"] if c.get("origin")] and "applicable_rule_not_cited" not in final["reasons"]     # PARTIAL only because the passage is unevaluable
    final, _, _ = run(H01, [], *H01_PACK, status="ABSTAINED", observation=GUIDE)
    assert final["status"] == "ABSTAINED" and final["claims"] == []                                         # the server never answers on its own


def test_a_second_gate_the_question_states_is_added_and_a_rule_in_a_disagreement_is_left_to_the_conflict_disclosure():
    q = "A standard purchase of 6,000,000 JPY from a supplier on the high-risk register, category CAPEX: who must approve it?"
    manager = {"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}
    p = pack(q, "rule-standard-manager-review", "rule-high-risk-director", "rule-capex-finance-config", "rule-capex-finance-policy")
    p["counterevidence"] = [{"kind": "conflicting_rule", "rule_id": "rule-capex-finance-policy", "with": "rule-capex-finance-config", "title": "CAPEX threshold", "text": "x", "authority": "OBSERVED"}]
    m = model("ANSWERED", [manager])
    kept, dropped = qa.judge_claims(m["claims"], p, q, FROZEN, can_read_evidence=True)
    final = qa.finalize(m, kept, dropped, p, FROZEN)
    added = {c["refs"][0] for c in final["claims"] if c.get("origin") == "server_applicability"}
    assert added == {"rule-high-risk-director"}                                                            # both CAPEX rules reach 6,000,000 but disagree: no side is chosen here
    assert final["status"] == "PARTIAL" and "applicable_rule_not_cited" in final["reasons"]


# ---- audit of the server claim (2026-10-18): only settled, unquestioned, fully stated rules; never filling a gap -------------------------------------------------------
def _h01(mutate=None, question=H01, extra_counter=()):
    p = pack(question, *H01_PACK, observation=GUIDE)
    if mutate:
        mutate({i["item_id"]: i for i in p["items"]}["rule-standard-manager-review"])
    p["counterevidence"] = list(extra_counter)
    m = model("ANSWERED", [FIN_PASSAGE])
    kept, dropped = qa.judge_claims(m["claims"], p, question, FROZEN, can_read_evidence=True)
    final = qa.finalize(m, kept, dropped, p, FROZEN)
    return final, [c for c in final["claims"] if c.get("origin") == "server_applicability"]


def test_an_unconfirmed_rule_is_never_a_server_claim():
    for status in ("DRAFT", "PENDING_HUMAN"):
        final, added = _h01(lambda it: it.update(status=status))
        assert added == [] and "applicable_rule_not_cited" not in final["reasons"], status
    _, added = _h01(lambda it: it.update(status="CONFIRMED"))
    assert [c["refs"] for c in added] == [["rule-standard-manager-review"]]


def test_a_rule_with_an_unknown_field_or_an_open_question_is_left_unknown():
    final, added = _h01(lambda it: it.update(unknown_fields=["unit:request.net_amount_jpy"]))
    assert added == [] and "applicable_rule_not_cited" not in final["reasons"]                           # an unknown unit is never filled in
    q = {"kind": "open_question", "gap_id": "gap-x", "question": "Who approves?", "priority": "critical", "affected_rule_ids": ["rule-standard-manager-review"]}
    final, added = _h01(extra_counter=[q])
    assert added == []


def test_an_undecided_exception_or_an_amount_in_another_currency_adds_nothing():
    exc = [{"exception_id": "exc-1", "text": "except catalogue purchases", "condition": {"field": "catalogue.approved", "op": "eq", "value": "true"}}]
    final, added = _h01(lambda it: it.update(exceptions=exc))
    assert added == []                                                                                    # the exception's fact is not stated: conditional, not applies
    final, added = _h01(question="An OPEX purchase of 3,000,000 JPY (about 20,000 USD) on the standard process: does Finance have to review it?")
    assert added == []


def test_the_server_claim_carries_the_authority_of_the_version_the_view_serves():
    _, added = _h01()
    assert added[0]["basis"] == "observed" and added[0]["authority_labels"] == ["what the sources say"] and added[0]["uses_evidence"] is False
    _, added = _h01(lambda it: it.update(authority="TARGET", versions=[{"item_id": "rule-standard-manager-review-observed", "authority": "OBSERVED", "status": "VALIDATED"}]))
    assert added[0]["basis"] == "target" and added[0]["authority_labels"] == ["decided by a person"]
    _, added = _h01(lambda it: it.update(versions=[{"item_id": "rule-standard-manager-review-target", "authority": "TARGET", "status": "CONFIRMED"}]))
    assert added[0]["basis"] == "observed"                                                                # a TARGET version behind it does not lend its authority


MANAGER_ONLY = [{"text": "Purchases of 1,000,000 JPY or more need manager approval.", "refs": ["rule-standard-manager-review"], "basis": "observed"}]


def test_an_answer_that_needs_the_pinned_default_is_partial_and_asks_for_the_fact_owner_decision_g2_qa_10():
    """Owner decision G2-QA-10 (option B): the default was needed to reach the case, so the missing fact could change the result."""
    final, kept, _ = run("Who approves a consulting services contract of 1,800,000 JPY?", MANAGER_ONLY, "rule-standard-manager-review")
    assert kept[0]["applicability"] == "applies" and kept[0]["assumptions"] == ["process=STANDARD"]
    assert final["status"] == "PARTIAL" and {"assumed_defaults", "assumed_default_needs_fact"} <= set(final["reasons"])
    assert final["clarifying_questions"][0] == "Is this a standard purchase or an emergency purchase?" and "Assumed: a standard purchase." in final["uncertainty"]


@pytest.mark.parametrize("question", ["Who approves a standard purchase of consulting services of 1,800,000 JPY?",                    # a fact the asker states
                                      "If it were a standard purchase, who approves a consulting contract of 1,800,000 JPY?"])     # a hypothetical the asker sets
def test_a_fact_or_hypothetical_the_question_states_is_not_an_assumption_and_the_answer_stays_complete(question):
    final, kept, _ = run(question, MANAGER_ONLY, "rule-standard-manager-review")
    assert kept[0]["assumptions"] == [] and final["status"] == "ANSWERED" and "assumed_default_needs_fact" not in final["reasons"]
