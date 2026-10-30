"""Gap-proposal context (hardening H9, HK-AEV P-01 / P-02, 2026-10-27): rules of the same outcome kind with different actors on different fields can both apply to
one case and are listed for the model; complementary obligations (another outcome kind) and rules sharing a field are not. Pure: no database, no model."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import business_proposals as bp
from tests.unit.test_draft_server_holds import rule

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _cat(rid, title, field, value, actor, outcome):
    r = rule(rid, title, field, "eq", value, actor, unit=None, outcome=outcome)
    return r


def test_two_only_deciders_on_different_fields_are_listed_and_complementary_obligations_are_not():
    imaging = _cat("rule-img", "Imaging purchases are decided only by the imaging committee", "item.class", "IMAGING", "role-imaging-committee", "review_required")
    board = rule("rule-board", "Purchases of 6,000,000 JPY or more are decided only by the executive board", "request.net_amount_jpy", "gte", 6000000, "role-executive-board")
    quotes = rule("rule-quotes", "Purchases of 1,000,000 JPY or more need three quotes", "request.net_amount_jpy", "gte", 1000000, "role-purchasing-office")
    tag = rule("rule-tag", "Equipment of 100,000 JPY or more gets an asset tag", "equipment.value_jpy", "gte", 100000, "role-estates", outcome="asset_tagged")
    got = bp.overlapping_deciders([imaging, board, quotes, tag])
    assert {tuple(o["rule_ids"]) for o in got} == {("rule-board", "rule-img")}                      # only rules that each claim the decision ("only") are listed
    other = rule("rule-journals", "Journal subscriptions of 200,000 JPY or more need the library manager", "subscription.annual_value_jpy", "gte", 200000, "role-library")
    assert bp.overlapping_deciders([quotes, other]) == []                                           # A108 P-05: two separate requirements are not listed
    assert not any("rule-tag" in o["rule_ids"] for o in got)                                     # another outcome kind: complementary, never listed
    assert not any(set(o["rule_ids"]) == {"rule-board", "rule-quotes"} for o in got)               # the same field: the threshold pairs cover them


def test_the_overlap_lines_reach_the_prompt_with_the_complement_and_without_rules():
    frozen = json.loads((ROOT / "config/knowledge_gap_proposals.json").read_text(encoding="utf-8"))
    imaging = _cat("rule-img", "Imaging purchases are decided only by the imaging committee", "item.class", "IMAGING", "role-imaging-committee", "review_required")
    board = rule("rule-board", "Purchases of 6,000,000 JPY or more are decided only by the executive board", "request.net_amount_jpy", "gte", 6000000, "role-executive-board")
    prompt, cov = bp.build_prompt_with_coverage(frozen, [imaging, board], [], [], can_read_evidence=False, max_chars=20000)
    assert "- rule-board and rule-img: both give the same kind of outcome (review_required)" in prompt and cov["pairs"]["total"] == 1
    assert "complement each other" in prompt and "\"without\"" in prompt and "a general business practice is not a source" in prompt


def _resolve(rules, gaps):
    frozen = json.loads((ROOT / "config/knowledge_gap_proposals.json").read_text(encoding="utf-8"))
    obj = bp.validate_proposal_object({"status": "OK", "gaps": gaps, "relations": [], "notes": []}, frozen["limits"])
    return bp.resolve_proposals(obj, {r["rule_id"]: r for r in rules}, [], [], limits=frozen["limits"], can_read_evidence=False)


def test_r5_an_exception_needs_another_shown_rule_as_its_source():
    """R5 (P-02 class, AEV8-P-08 / agp-04): "does the journal rule have exceptions for urgent transactions?" cites only the rule itself — no source — and is refused
    and recorded; an exception sourced by the shown rule that acts without the requirement (AEV8-P-03 form) is kept; a real conflict is untouched."""
    journals = rule("rule-journals", "Journals of 500,000 JPY or more are approved by the financial controller", "journal.amount_jpy", "gte", 500000, "role-financial-controller")
    sig = rule("rule-two-sig", "Every supplier payment needs two signatures", "payment.supplier", "eq", True, "role-finance-office", unit=None)
    petty = rule("rule-petty", "Petty cash pays suppliers up to 30,000 JPY without a second signature", "payment.amount_jpy", "lte", 30000, "role-cashier")
    out = _resolve([journals, sig, petty], [
        {"kind": "missing_exception", "rule_ids": ["rule-journals"], "question": "Are there exceptions for urgent transactions?", "evidence_refs": ["rule-journals"]},
        {"kind": "missing_exception", "rule_ids": ["rule-two-sig"], "question": "Should rule-two-sig state that petty cash payments (rule-petty) need one signature?",
         "evidence_refs": ["rule-two-sig", "rule-petty"]}])
    assert [g["affected_rule_ids"] for g in out["gaps"]] == [["rule-two-sig"]]
    assert out["rejected"] == [{"item": "gap", "reason": "exception_without_source", "kind": "missing_exception", "rule_ids": ["rule-journals"]}]
    refund = rule("rule-refund", "Withdrawals within 14 days get a full refund", "withdrawal.days", "lte", 14, "role-registry", outcome="refund_full")
    norefund = rule("rule-norefund", "No refund after 7 days", "withdrawal.days", "gt", 7, "role-finance", outcome="refund_denied")
    kept = _resolve([refund, norefund], [{"kind": "conflicting_facts", "rule_ids": ["rule-refund", "rule-norefund"], "question": "Days 8 to 14: full refund or none?",
                                          "evidence_refs": ["rule-refund", "rule-norefund"]}])
    assert len(kept["gaps"]) == 1 and not kept["rejected"]                                       # different outcome kinds can be a real conflict: never refused by kind


def test_r5_pairs_state_each_outcome_and_a_narrower_rule_under_a_stated_exception_is_a_lead():
    frozen = json.loads((ROOT / "config/knowledge_gap_proposals.json").read_text(encoding="utf-8"))
    insure = rule("rule-insure", "Equipment of 1,000,000 JPY or more is added to the insurance schedule", "request.net_amount_jpy", "gte", 1000000, "role-finance-office", outcome="insurance_listed")
    quotes = rule("rule-quotes", "Purchases of 1,000,000 JPY or more need three quotes", "request.net_amount_jpy", "gte", 1000000, "role-purchasing-office", outcome="quotes_required")
    prompt, cov = bp.build_prompt_with_coverage(frozen, [insure, quotes], [], [], can_read_evidence=False, max_chars=20000)
    assert "- rule-insure and rule-quotes: both compare the amount of a any category purchase and state different actors role-finance-office and role-purchasing-office; outcomes insurance_listed / quotes_required" in prompt
    assert "can one case receive what both rules give?" in prompt and "insurance listing and three quotes" in prompt
    report = dict(rule("rule-report", "Every outgoing loan needs a facility report", "loan.outgoing", "eq", True, "role-head-of-conservation", unit=None),
                  exceptions=[{"text": "An alliance borrower with a report under 24 months needs no new report", "condition": None}])
    alliance = rule("rule-alliance", "Alliance loans with a recent report are approved without a new report", "loan.outgoing", "eq", True, "role-registrar", unit=None)
    alliance["condition"] = {"all": [{"field": "loan.outgoing", "op": "eq", "value": True}, {"field": "borrower.alliance_member", "op": "eq", "value": True}]}
    leads = bp.stated_exception_leads([report, alliance])
    assert leads == [{"rule_id": "rule-report", "narrower_rule_id": "rule-alliance", "stated": ["An alliance borrower with a report under 24 months needs no new report"]}]
    assert bp.stated_exception_leads([dict(report, exceptions=[]), alliance]) == []                 # nothing stated: no lead (a missing exception stays possible)
    assert bp.stated_exception_leads([report, dict(alliance, condition={"field": "borrower.alliance_member", "op": "eq", "value": True})]) == []   # not narrower
    prompt2, cov2 = bp.build_prompt_with_coverage(frozen, [report, alliance], [], [], can_read_evidence=False, max_chars=20000)
    assert "- rule-alliance acts on a narrower case of rule-report, which already states the exception" in prompt2 and cov2["pairs"]["total"] == 1
