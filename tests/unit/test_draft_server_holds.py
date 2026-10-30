"""Server holds in the change draft (owner-approved flow / contract change, 2026-10-24): from the rules themselves, never from a rule id or a number written in
advance. A hold is an unresolved item a person clarifies; it blocks the knowledge decision and downgrades the automatic edits of the held rule. Pure: no database,
no model. Shapes are those of the five changes the guidance did not hold on CORE-V8 (four approval tiers inverted, one dependent rule losing its trigger) and of
the cases that must NOT hold (the order kept, the same actor, a widening change, a rule that merely shares a number, a different duty on the same number, a
dependency a model proposed without proof). Owner's conditional acceptance 2026-10-25: a hold rests on a proven basis — tiers of the same decision, a confirmed
relation, the dependent's own condition being the changed rule's act, a recorded disagreement — never on two thresholds that merely differ, two titles, or a
model-proposed edge."""
from __future__ import annotations

import copy
import json
import pathlib

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2 import business_impact as bi

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = json.loads((ROOT / "tests/fixtures/final_knowledge_loop/expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"][0]
PIN = {"snapshot_id": "s", "business_revision": 1, "source_revision_id": "r", "km_revision_id": "k", "decision_epoch": 0}


def rule(rid, title, field, op, value, actor, *, unit="JPY", concept="concept-x", outcome="review_required", scope=None):
    r = copy.deepcopy(BASE)
    r.update(rule_id=rid, title=title, actor_ref=actor, concept_ids=[concept], status="VALIDATED", authority="OBSERVED",
             condition={"field": field, "op": op, "value": value, **({"unit": unit} if unit else {})}, outcome={"kind": outcome, "predicate": ""}, exceptions=[], scope=scope or {})
    r["source_bindings"] = [{"artifact_id": f"art-{rid}", "revision_id": "rev-1", "evidence_ref": f"{rid}.yaml#x"}]
    return r


def change(rid, rules, desired, kind="set_value", before=None):
    r = rules[rid]
    leaf = r["condition"]
    op = {"op_id": "op-1", "kind": kind, "rule_id": rid, "before": before if before is not None else {"value": leaf["value"], **({"unit": leaf["unit"]} if leaf.get("unit") else {})},
          "desired": desired, "user_quote": "q", "evidence_refs": [], "status": "confirmed"}
    return bd.rule_changes([op], rules)


def holds(rules, changes, relations=(), proposed=(), counter=()):
    return bd.server_holds(changes, rules, relations, proposed, counter)


def test_a_threshold_moved_past_another_actors_threshold_on_the_same_field_is_held_in_both_directions():
    rules = {r["rule_id"]: r for r in [rule("rule-head", "Equipment of 1,000,000 JPY or more needs the Department Head", "equipment.cost_jpy", "gte", 1000000, "role-department-head"),
                                       rule("rule-director", "Equipment of 3,000,000 JPY or more is also approved by the Director", "equipment.cost_jpy", "gte", 3000000, "role-director", concept="concept-y")]}
    up = holds(rules, change("rule-head", rules, {"value": 4000000, "unit": "JPY"}))                        # 1M -> 4M passes the Director's 3M (G-03 shape)
    assert [h["hold"] for h in up] == ["approval_order_inverted"] and up[0]["basis"] == "rules" and up[0]["other_rule_id"] == "rule-director"
    assert up[0]["evidence"] == {"field": "equipment.cost_jpy", "before": 1000000, "after": 4000000, "other_value": 3000000, "actor": "role-department-head", "other_actor": "role-director",
                                 "outcome": "review_required", "relation": None}                                                           # the same decision: both tiers decide review_required
    assert "inverted" in up[0]["instruction"] and up[0]["ref"] == "H001" and up[0]["action"] == "UNRESOLVED"
    down = holds(rules, change("rule-director", rules, {"value": 800000, "unit": "JPY"}))                   # 3M -> 800k drops below the Head's 1M (G-04 shape)
    assert [h["hold"] for h in down] == ["approval_order_inverted"]
    kept = holds(rules, change("rule-head", rules, {"value": 2500000, "unit": "JPY"}))                      # 1M -> 2.5M stays below 3M: no hold
    assert kept == []
    equal = holds(rules, change("rule-head", rules, {"value": 3000000, "unit": "JPY"}))                     # landing exactly on the other threshold changes the order too
    assert [h["hold"] for h in equal] == ["approval_tiers_start_together"]                                 # R2-05a: held, but as tiers starting together, not an inversion


def test_no_hold_for_the_same_actor_a_different_field_or_a_rule_that_only_shares_the_number():
    rules = {r["rule_id"]: r for r in [rule("rule-a", "A", "amount.jpy", "gte", 100, "role-x"), rule("rule-b", "B", "amount.jpy", "gte", 300, "role-x"),
                                       rule("rule-c", "C", "other.jpy", "gte", 300, "role-y"), rule("rule-d", "D", "days.late", "gt", 300, "role-z", unit="days")]}
    assert holds(rules, change("rule-a", rules, {"value": 500, "unit": "JPY"})) == []                       # b: same actor; c / d: other fields


def test_a_different_duty_on_the_same_number_is_not_an_order_to_invert_unless_a_confirmed_relation_says_so():
    rules = {r["rule_id"]: r for r in [rule("rule-master", "Hazardous cargo of more than 500 kg needs the Ship Master's approval", "cargo.hazmat_quantity_kg", "gt", 500, "role-ship-master", unit="kg", outcome="approval_required"),
                                       rule("rule-notice", "Hazardous cargo of more than 500 kg is notified to the port authority by the Port Liaison", "cargo.hazmat_quantity_kg", "gt", 500, "role-port-liaison", unit="kg", outcome="port_notice_required", concept="concept-y")]}
    ch = change("rule-master", rules, {"value": 800, "unit": "kg"})                                            # G-01 shape: the notice applies on its own; nothing says it needs the master first
    assert holds(rules, ch) == []
    proposed = [{"dep_id": "dep-1", "from_rule_id": "rule-notice", "to_rule_id": "rule-master", "relation": "depends_on", "status": "PROPOSED"}]
    assert holds(rules, ch, proposed=proposed) == []                                                           # a model-proposed edge alone is no basis
    assert bd.dependency_checks(ch, rules, proposed) == {"dep-1": {"check": "independent_trigger", "text": bd.DEPENDENCY_CHECKS["independent_trigger"]}}
    confirmed = [{"edge_id": "e1", "type": "depends_on", "from": "rule-notice", "to": "rule-master", "status": "CONFIRMED", "provenance_refs": []}]
    out = holds(rules, ch, relations=confirmed)                                                                # a confirmed relation proves the order (and the lost trigger)
    assert [(h["hold"], h["basis"]) for h in out] == [("approval_order_inverted", "rules"), ("dependent_rule_loses_trigger", "accepted_relation")] and out[0]["evidence"]["relation"] == "confirmed"


def test_a_rule_whose_own_condition_is_the_changed_rules_act_is_held_when_cases_are_lost_and_a_bare_proposal_is_only_checked():
    rules = {r["rule_id"]: r for r in [rule("rule-master", "Hazardous cargo of more than 500 kg needs the Ship Master's approval", "cargo.hazmat_quantity_kg", "gt", 500, "role-ship-master", unit="kg", outcome="approval_required"),
                                       rule("rule-deck", "Hazardous cargo the Ship Master has approved is stowed on the open deck by the Deck Officer", "cargo.master_approved", "eq", True, "role-deck-officer", unit=None, outcome="open_deck_stowage", concept="concept-y"),
                                       rule("rule-notice", "Hazardous cargo of more than 500 kg is notified to the port authority", "cargo.hazmat_quantity_kg", "gt", 500, "role-port-liaison", unit="kg", outcome="port_notice_required", concept="concept-z"),
                                       rule("rule-gm", "Compensation the General Manager has approved is audited", "compensation.general_manager_approved", "eq", True, "role-auditor", unit=None, outcome="audit", concept="concept-w")]}
    ch = change("rule-master", rules, {"value": 800, "unit": "kg"})                                            # 500 -> 800 kg: 500-800 kg no longer reach the master
    out = holds(rules, ch)
    assert [(h["hold"], h["other_rule_id"], h["basis"], h["evidence"]["relation"]) for h in out] == [("dependent_rule_loses_trigger", "rule-deck", "rule_condition", "cargo.master_approved")]
    assert "between 500 and 800 kg" in out[0]["evidence"]["cases_lost"] and "is what this rule decides" in out[0]["instruction"]
    assert holds(rules, change("rule-master", rules, {"value": 300, "unit": "kg"})) == []                      # widened: nothing loses its trigger
    proposed = [{"dep_id": "d-deck", "from_rule_id": "rule-deck", "to_rule_id": "rule-master", "relation": "depends_on", "status": "PROPOSED"},
                {"dep_id": "d-notice", "from_rule_id": "rule-notice", "to_rule_id": "rule-master", "relation": "depends_on", "status": "PROPOSED"},
                {"dep_id": "d-gm", "from_rule_id": "rule-gm", "to_rule_id": "rule-master", "relation": "depends_on", "status": "PROPOSED"}]
    assert {k: v["check"] for k, v in bd.dependency_checks(ch, rules, proposed).items()} == {"d-deck": "trigger_verified", "d-notice": "independent_trigger", "d-gm": "unverified"}
    assert [h["other_rule_id"] for h in holds(rules, ch, proposed=proposed)] == ["rule-deck"]                   # the proposals add no hold: the deck rule is held by its own condition, the others not at all


def test_a_narrowed_rule_holds_every_dependent_rule_with_its_basis_told_apart():
    rules = {r["rule_id"]: r for r in [rule("rule-notice", "Interruptions of 4 hours or more need a customer notice", "interruption.hours", "gte", 4, "role-customer-service-lead", unit="hours", outcome="customer_notice_required"),
                                       rule("rule-tanker", "When a notice is issued for a district with a hospital, a tanker is dispatched", "interruption.customer_notice_issued", "eq", True, "role-coordinator", unit=None, outcome="tanker_dispatch", concept="concept-z"),
                                       rule("rule-report", "Interruptions of 12 hours or more are reported to the regulator", "interruption.hours", "gte", 12, "role-compliance", unit="hours", outcome="regulator_report", concept="concept-w")]}
    ch = change("rule-notice", rules, {"value": 14, "unit": "hours"})                                          # 4 -> 14 h (G-09 shape)
    accepted = [{"edge_id": "e1", "type": "depends_on", "from": "rule-tanker", "to": "rule-notice", "status": "CONFIRMED", "provenance_refs": []}]
    proposed = [{"dep_id": "dep-1", "from_rule_id": "rule-report", "to_rule_id": "rule-notice", "relation": "depends_on", "status": "PROPOSED"}]
    out = holds(rules, ch, relations=accepted, proposed=proposed)
    assert [(h["hold"], h["other_rule_id"], h["basis"]) for h in out] == [("dependent_rule_loses_trigger", "rule-tanker", "accepted_relation")]
    assert "between 4 and 14 hours" in out[0]["evidence"]["cases_lost"] and "confirmed depends_on relation" in out[0]["instruction"]
    assert bd.dependency_checks(ch, rules, proposed)["dep-1"]["check"] == "independent_trigger"                 # the regulator report decides on its own 12 h: no order, no lost trigger
    widened = holds(rules, change("rule-notice", rules, {"value": 2, "unit": "hours"}), relations=accepted, proposed=proposed)
    assert [h["hold"] for h in widened] == []                                                                  # more cases reach the notice: nothing loses its trigger
    rejected = [dict(accepted[0], status="REJECTED")]
    out = holds(rules, ch, relations=rejected)                                                                 # a rejected relation is no basis; the tanker's own condition still is (G-08 shape, 4 -> 6 h too)
    assert [(h["other_rule_id"], h["basis"], h["evidence"]["relation"]) for h in out] == [("rule-tanker", "rule_condition", "interruption.customer_notice_issued")]
    assert [h["basis"] for h in holds(rules, change("rule-notice", rules, {"value": 6, "unit": "hours"}))] == ["rule_condition"]


def test_a_recorded_disagreement_holds_and_the_draft_blocks_the_decision_until_a_person_clarifies():
    rules = {r["rule_id"]: r for r in [rule("rule-code", "CAPEX of 2,000,000 JPY or more goes to Finance", "request.net_amount_jpy", "gte", 2000000, "role-finance"),
                                       rule("rule-policy", "CAPEX of 5,000,000 JPY or more goes to Finance", "request.net_amount_jpy", "gte", 5000000, "role-finance")]}
    ch = change("rule-code", rules, {"value": 2500000, "unit": "JPY"})
    out = holds(rules, ch, counter=["rule-policy"])
    assert [h["hold"] for h in out] == ["conflicts_with_effective_rule"] and out[0]["basis"] == "recorded_disagreement"
    rel = [{"edge_id": "e-c", "type": "conflicts_with", "from": "rule-code", "to": "rule-policy", "status": "CONFIRMED", "provenance_refs": []}]
    intent = {"intent_id": "chg-1", "request": "Raise it to 2,500,000 JPY.", "operations": [{"op_id": "op-1", "kind": "set_value", "rule_id": "rule-code", "before": {"value": 2000000, "unit": "JPY"},
                                                                                            "desired": {"value": 2500000, "unit": "JPY"}, "user_quote": "q", "evidence_refs": [], "status": "confirmed"}]}
    report = bi.build_report(intent["operations"], rules, rel, pin=PIN, known_artifacts=set())
    draft = bd.build_draft(intent, report, rules, rel, pin=PIN, artifacts={}, observations=[])
    hold = [u for u in draft["unresolved"] if u["kind"] == "server_hold"]
    assert len(hold) == 1 and draft["coverage"]["server_holds"] == 1 and draft["status"] == "PARTIAL"
    blockers = bd.semantic_blockers(draft, acknowledged_flags=True)
    assert [b["code"] for b in blockers] == ["server_hold"] and blockers[0]["ref"] == hold[0]["ref"] and blockers[0]["about"] == "conflicts_with_effective_rule"
    assert hold[0]["ref"] in bd.open_refs(draft)                                                             # a person can clarify it
    cleared = bd.merge_clarifications(draft, [{"ref": hold[0]["ref"], "disposition": "resolved_by_person", "answer": "The policy figure stands; the code is corrected.", "actor_id": "u", "decision_id": "d1"}])
    assert bd.semantic_blockers(cleared, acknowledged_flags=True) == []
    shown = bd.render(draft, mode="business", can_read_evidence=False)
    assert shown["unresolved"][0]["hold"] == "conflicts_with_effective_rule" and "disagrees" in shown["unresolved"][0]["instruction"]   # the hold is business-level: shown without evidence rights


def test_the_automatic_edits_of_a_held_rule_wait_and_the_dependency_requirement_has_grounds():
    rules = {r["rule_id"]: r for r in [rule("rule-head", "Head", "equipment.cost_jpy", "gte", 1000000, "role-head"), rule("rule-director", "Director", "equipment.cost_jpy", "gte", 3000000, "role-director", concept="concept-y")]}
    intent = {"intent_id": "chg-2", "request": "Raise it.", "operations": [{"op_id": "op-1", "kind": "set_value", "rule_id": "rule-head", "before": {"value": 1000000, "unit": "JPY"},
                                                                           "desired": {"value": 4000000, "unit": "JPY"}, "user_quote": "q", "evidence_refs": [], "status": "confirmed"}]}
    rel = []
    report = bi.build_report(intent["operations"], rules, rel, pin=PIN, known_artifacts={"art-rule-head"})
    obs = [{"artifact_id": "art-rule-head", "kind": "yaml_value", "locator": {"type": "key_path", "key_path": "x"}, "raw_value": 1000000}]
    draft = bd.build_draft(intent, report, rules, rel, pin=PIN, artifacts={"art-rule-head": "rule-head.yaml"}, observations=obs)
    edits = [i for i in draft["file_actions"] if "rule-head" in (i.get("rule_ids") or [])]
    assert edits and all(i["action"] != "AI_EDIT_SUPPORTED" for i in edits) and any(i.get("reason") == "server_hold" and i.get("held_edit") for i in edits)
    assert bd.dependency_requirement(report, rules) == {"required": True, "reason": "1 live rule(s) lie outside the impact report", "rules_outside": 1}   # the Director rule: own concept, no relation
    from core.change_intelligence.v2 import business_graph as bg
    rules["rule-director"]["concept_ids"] = ["concept-x"]
    linked = bi.build_report(intent["operations"], rules, bg.derive_relations(rules.values()), pin=PIN, known_artifacts={"art-rule-head"})
    need = bd.dependency_requirement(linked, rules)
    assert need["required"] is False and "nothing is left" in need["reason"]                                   # now every live rule is in the report


def test_each_file_item_says_in_business_words_which_rule_it_concerns_and_how_a_check_item_is_linked():
    """AI quality round F-3 (2026-10-25): a bare "role=check" line made the guidance model write "check all related documentation". The item now carries an
    `about` line — the rule and its change, or the related rule and the kind of link — and the guidance prompt shows it; never a file, location or id."""
    rules = {r["rule_id"]: r for r in [rule("rule-master", "Hazardous cargo of more than 500 kg needs the Ship Master's approval", "cargo.kg", "gt", 500, "role-ship-master", unit="kg"),
                                       rule("rule-declare", "Cargo of hazard class 1, 2 or 3 is declared by the Cargo Clerk", "cargo.class", "eq", "1", "role-cargo-clerk", unit=None)]}
    intent = {"intent_id": "chg-a", "request": "Master approval only above 800 kg.", "operations": [{"op_id": "op-1", "kind": "set_value", "rule_id": "rule-master",
              "before": {"value": 500, "unit": "kg"}, "desired": {"value": 800, "unit": "kg"}, "user_quote": "q", "evidence_refs": [], "status": "confirmed"}]}
    from core.change_intelligence.v2 import business_graph as bg
    rel = bg.derive_relations(rules.values())                                                                  # same concept: a "defines" path, not confirmed
    report = bi.build_report(intent["operations"], rules, rel, pin=PIN, known_artifacts={"art-rule-master", "art-rule-declare"})
    draft = bd.build_draft(intent, report, rules, rel, pin=PIN, artifacts={"art-rule-master": "cargo.md", "art-rule-declare": "cargo.md"}, observations=[])
    by_role = {i["role"]: i for i in draft["file_actions"]}
    assert by_role["to_change"]["about"].startswith("changes “Hazardous cargo of more than 500 kg needs the Ship Master's approval”: when it applies:")
    assert "800" in by_role["to_change"]["about"]
    check = by_role["check"]["about"]
    assert "Cargo of hazard class 1, 2 or 3 is declared by the Cargo Clerk" in check and "shares only the topic" in check and "same business concept" in check
    assert "no action for this change" in check and by_role["check"]["link_kind"] == "same_topic_only"
    for it in draft["file_actions"]:
        assert "cargo.md" not in it["about"] and "rule-" not in it["about"] and "art-" not in it["about"]
    frozen = json.loads((ROOT / "config/change_draft_guidance.json").read_text(encoding="utf-8"))
    for evidence in (True, False):
        prompt = bd.build_guidance_prompt(frozen, draft, can_read_evidence=evidence, max_chars=16000)
        assert f"  about: {check}" in prompt and ("\n  file: " in prompt) is evidence                         # the about line for every reader; the file only with evidence rights
    confirmed = rel + [{"edge_id": "e-dep", "type": "depends_on", "from": "rule-declare", "to": "rule-master", "status": "CONFIRMED", "provenance_refs": []}]
    report2 = bi.build_report(intent["operations"], rules, confirmed, pin=PIN, known_artifacts={"art-rule-master", "art-rule-declare"})
    draft2 = bd.build_draft(intent, report2, rules, confirmed, pin=PIN, artifacts={"art-rule-master": "cargo.md", "art-rule-declare": "cargo.md"}, observations=[])
    about2 = next(i["about"] for i in draft2["file_actions"] if i["role"] == "check")
    item2 = next(i for i in draft2["file_actions"] if i["role"] == "check")
    assert item2["link_kind"] == "held" and "hold H00" in about2 and "a person decides" in about2                    # 500 -> 800 kg loses cases under the confirmed relation


def test_a_state_field_whose_subject_is_the_path_segment_before_the_state_word_is_read_too():
    """Acceptance item agd-05 (2026-10-25): "tenant_notice.issued" names the notice in the segment before the one-word state; the dependent posting rule loses
    its trigger when the notice threshold rises. A segment naming something else ("cargo.approved" against a notice rule) is no basis."""
    rules = {r["rule_id"]: r for r in [rule("rule-notice", "Shut-offs longer than 4 hours require a tenant notice", "shutoff.duration_hours", "gt", 4, "role-building-manager", unit="hours", outcome="tenant_notice_issued"),
                                       rule("rule-posting", "When a tenant notice is issued, the concierge posts it in the lobby", "tenant_notice.issued", "eq", True, "role-concierge", unit=None, outcome="posting_required", concept="concept-y"),
                                       rule("rule-other", "Approved cargo is stowed on deck", "cargo.approved", "eq", True, "role-deck", unit=None, outcome="stowage", concept="concept-z")]}
    out = holds(rules, change("rule-notice", rules, {"value": 8, "unit": "hours"}))
    assert [(h["hold"], h["other_rule_id"], h["basis"], h["evidence"]["relation"]) for h in out] == [("dependent_rule_loses_trigger", "rule-posting", "rule_condition", "tenant_notice.issued")]
    assert holds(rules, change("rule-notice", rules, {"value": 2, "unit": "hours"})) == []                     # widened: nothing loses its trigger


def test_related_rules_are_classed_by_what_the_knowledge_proves_and_every_one_stays_listed():
    """Continuation 2026-10-25 (acceptance agd-06 / agd-08): a rule reached only through the same concept or role is not something to check; a separate
    obligation with its own threshold on the same measure is not to be aligned; a held tier points to its hold; a rule whose own condition is the changed rule's
    act follows from it. Classes come from conditions, outcomes and holds — no rule id or sentence."""
    from core.change_intelligence.v2 import business_graph as bg
    rules = {r["rule_id"]: r for r in [
        rule("rule-cio", "Subscriptions of 1,000,000 JPY or more are approved by the CIO", "sub.cost_jpy", "gte", 1000000, "role-cio", outcome="approval_required"),
        rule("rule-register", "Subscriptions of 1,000,000 JPY or more are listed for Internal Audit", "sub.cost_jpy", "gte", 1000000, "role-audit-clerk", outcome="register_listing"),
        rule("rule-cfo", "Subscriptions of 5,000,000 JPY or more are approved by the CFO", "sub.cost_jpy", "gte", 5000000, "role-cfo", outcome="approval_required"),
        rule("rule-renewal", "Subscriptions the CIO has approved are renewed by procurement", "sub.cio_approved", "eq", True, "role-procurement", unit=None, outcome="renewal"),
        rule("rule-cards", "Corporate cards are used only by their holder", "card.holder_match", "eq", True, "role-cio", unit=None, outcome="card_rule", concept="concept-cards")]}
    rel = bg.derive_relations(rules.values())
    def kinds(new_value):
        intent = {"intent_id": "c", "request": "r", "operations": [{"op_id": "op-1", "kind": "set_value", "rule_id": "rule-cio", "before": {"value": 1000000, "unit": "JPY"},
                  "desired": {"value": new_value, "unit": "JPY"}, "user_quote": "q", "evidence_refs": [], "status": "confirmed"}]}
        arts = {f"art-{r}" for r in rules}
        report = bi.build_report(intent["operations"], rules, rel, pin=PIN, known_artifacts=arts)
        draft = bd.build_draft(intent, report, rules, rel, pin=PIN, artifacts={a: f"{a}.md" for a in arts}, observations=[])
        return {i["rule_ids"][0]: (i["link_kind"], i["about"]) for i in draft["file_actions"]}
    k = kinds(1500000)
    assert k["rule-register"][0] == "independent_obligation" and "nothing requires the two to match" in k["rule-register"][1]
    assert k["rule-cfo"][0] == "independent_obligation" and "keeps the order of the two tiers" in k["rule-cfo"][1]
    assert k["rule-renewal"][0] == "held"                                                                       # 1.0 -> 1.5 M drops cases the renewal rule followed
    lowered = kinds(800000)                                                                                       # more cases reach the CIO: nothing is held
    assert lowered["rule-renewal"][0] == "follows_from" and "sub.cio_approved" in lowered["rule-renewal"][1] and "only adds cases" in lowered["rule-renewal"][1] and "no case loses it" in lowered["rule-renewal"][1]
    assert k.get("rule-cards", ("same_topic_only",))[0] == "same_topic_only"
    held = kinds(6000000)                                                                                       # past the CFO tier: held
    assert held["rule-cfo"][0] == "held" and "hold H00" in held["rule-cfo"][1] and "no action" not in held["rule-cfo"][1]
    assert set(k) >= {"rule-cio", "rule-register", "rule-cfo", "rule-renewal"}                                 # every related rule stays listed


import pytest  # noqa: E402


@pytest.mark.parametrize("field, held", [
    ("interruption.tenant_notice_issued", True),         # subject before the state word in the last segment
    ("interruption.customer_notice_issued", False),      # a different notice than the one the changed rule issues
    ("tenant_notice.issued", True),                      # one-word state: subject in the segment before
    ("building.tenant_notice.issued", True),             # three segments, the same shape
    ("notice_issued", True),                             # a single segment
    ("cargo.approved", False),                           # the segment names something the changed rule does not decide
    ("tenant_notice.issued_count", False),               # not a boolean state (value is a number below)
])
def test_state_field_shapes_are_read_as_a_class(field, held):
    value = 3 if field.endswith("_count") else True
    rules = {"rule-notice": rule("rule-notice", "Shut-offs longer than 4 hours require a tenant notice", "shutoff.hours", "gt", 4, "role-building-manager", unit="hours", outcome="tenant_notice_issued"),
             "rule-dep": rule("rule-dep", "Dependent rule", field, "eq" if value is True else "gte", value, "role-concierge", unit=None, outcome="posting_required", concept="concept-y")}
    out = holds(rules, change("rule-notice", rules, {"value": 8, "unit": "hours"}))
    assert bool([h for h in out if h["other_rule_id"] == "rule-dep" and h["basis"] == "rule_condition"]) is held
