"""Change effects in the draft (revision 2 PC-05a, 2026-10-27): facts the server proves from the pinned rules for the person and the guidance model — the range a
threshold move drops and who still covers it (or that no rule names anyone), the range it adds and the rules that follow from it, and the rule whose condition names
an old actor's act. Never holds. Shapes of the guidance findings agd-02 / agd-09 / agd-10 / AEV-G-05 and their counterparts. Pure: no database, no model."""
from __future__ import annotations

import copy

from core.change_intelligence.v2 import business_draft as bd
from tests.unit.test_draft_server_holds import BASE, change, holds, rule


def effects(rules, changes):
    return bd.change_effects(changes, rules, [])


def test_a_raised_tier_that_lands_on_the_next_tier_is_held_as_starting_together_not_inverted_and_the_dropped_range_is_stated():
    """agd-02: Division Director 300,000 -> 800,000 JPY, the Executive Vice President tier starts at 800,000."""
    rules = {r["rule_id"]: r for r in [rule("rule-dd", "Business-class trips of 300,000 JPY or more need the Division Director", "trip.cost_jpy", "gte", 300000, "role-division-director"),
                                       rule("rule-evp", "Business-class trips of 800,000 JPY or more need the Executive Vice President", "trip.cost_jpy", "gte", 800000, "role-executive-vice-president")]}
    ch = change("rule-dd", rules, {"value": 800000, "unit": "JPY"})
    h = holds(rules, ch)
    assert [x["hold"] for x in h] == ["approval_tiers_start_together"] and "inverted" not in h[0]["instruction"] and "same value" in h[0]["instruction"]
    assert h[0]["reason"] == "approval_tiers_start_together" and h[0]["gap_text"] == "approval tiers start at the same value"
    e = effects(rules, ch)
    assert [x["effect"] for x in e] == ["range_vacated"] and e[0]["covered_by"] == []
    assert "from 300000 below 800000 JPY" in e[0]["text"] and "no other rule of the same kind in the knowledge applies to them" in e[0]["text"]                     # no lower tier: no other rule applies (the model reads whether someone must act)


def test_a_raised_bottom_tier_drops_a_range_no_other_tier_covers_and_a_higher_tier_is_not_a_cover():
    """agd-10: Line Manager 50,000 -> 100,000 JPY; the Cost Centre Owner starts at 200,000 (order kept, no hold)."""
    rules = {r["rule_id"]: r for r in [rule("rule-lm", "Expense claims of 50,000 JPY or more are approved by the Line Manager", "claim.amount_jpy", "gte", 50000, "role-line-manager"),
                                       rule("rule-cco", "Expense claims of 200,000 JPY or more are approved by the Cost Centre Owner", "claim.amount_jpy", "gte", 200000, "role-cost-centre-owner")]}
    ch = change("rule-lm", rules, {"value": 100000, "unit": "JPY"})
    assert holds(rules, ch) == []
    e = effects(rules, ch)
    assert e[0]["effect"] == "range_vacated" and e[0]["covered_by"] == [] and "from 50000 below 100000 JPY" in e[0]["text"] and "no other rule of the same kind in the knowledge applies to them" in e[0]["text"]


def test_a_dropped_range_still_covered_by_a_lower_tier_names_that_tier_and_a_partial_cover_says_from_where():
    rules = {r["rule_id"]: r for r in [rule("rule-low", "Purchases of 100,000 JPY or more are approved by the Team Lead", "p.jpy", "gte", 100000, "role-team-lead"),
                                       rule("rule-mid", "Purchases of 500,000 JPY or more are approved by the Manager", "p.jpy", "gte", 500000, "role-manager"),
                                       rule("rule-mid2", "Purchases of 700,000 JPY or more are checked by the Controller", "p.jpy", "gte", 700000, "role-controller"),
                                       rule("rule-top", "Purchases of 5,000,000 JPY or more are approved by the Director", "p.jpy", "gte", 5000000, "role-director")]}
    e = effects(rules, change("rule-mid", rules, {"value": 1000000, "unit": "JPY"}))                         # 500k -> 1M: the Team Lead still covers, the Controller from 700k
    cov = {c["rule_id"]: c["whole_range"] for c in e[0]["covered_by"]}
    assert cov == {"rule-low": True, "rule-mid2": False} and "Team Lead" in e[0]["text"] and "from 700000 JPY" in e[0]["text"]
    assert "applies to them" not in e[0]["text"]
    gap = {r["rule_id"]: r for r in [rules["rule-mid"], rules["rule-mid2"]]}
    e2 = effects(gap, change("rule-mid", gap, {"value": 1000000, "unit": "JPY"}))                            # only a partial cover: the rest is said to reach no other rule
    assert "No other rule of the same kind in the knowledge reaches cases from 500000 below 700000 JPY." in e2[0]["text"]
    assert e2[0]["uncovered"] == [{"from": 500000, "to": 700000}] and e[0]["uncovered"] == []                   # the Team Lead covers the whole range: no gap


def _band(rid, title, field, lo, hi, actor, unit, outcome):
    r = rule(rid, title, field, "gte", lo, actor, unit=unit, outcome=outcome)
    r["condition"] = {"all": [{"field": field, "op": "gte", "value": lo, "unit": unit}, {"field": field, "op": "lt", "value": hi, "unit": unit}]}
    return r


def test_a_band_below_is_never_a_cover_above_its_upper_bound_and_every_uncovered_part_is_stated_with_its_figures():
    """A103 items AEV5-G-09 / G-08: the lower tier is a band ("from 5 below 25 t"); the server said the whole dropped range 25-90 t still reached it, and the
    guidance repeated that invented cover. Now the band covers nothing above 25 t; the top tier covers from 80 t; 25-80 t is stated as reached by no rule."""
    lift = {r["rule_id"]: r for r in [_band("rule-sup", "Lifts from 5 below 25 t need the lift supervisor", "lift.t", 5, 25, "role-lift-supervisor", "tonnes", "lift_ok"),
                                      rule("rule-mc", "Lifts of 25 t or more need the marine coordinator", "lift.t", "gte", 25, "role-marine-coordinator", unit="tonnes", outcome="lift_ok"),
                                      rule("rule-oim", "Lifts of 80 t or more need the installation manager", "lift.t", "gte", 80, "role-oim", unit="tonnes", outcome="lift_ok")]}
    e = effects(lift, change("rule-mc", lift, {"value": 90, "unit": "tonnes"}))
    assert [c["rule_id"] for c in e[0]["covered_by"]] == ["rule-oim"] and e[0]["uncovered"] == [{"from": 25, "to": 80}]
    assert "lift supervisor" not in e[0]["text"] and "from 80 tonnes" in e[0]["text"]
    assert "No other rule of the same kind in the knowledge reaches cases from 25 below 80 tonnes." in e[0]["text"]
    # a band that does reach into the dropped range covers only its own part (from the old value up to below its upper bound)
    wide = dict(lift, **{"rule-sup": _band("rule-sup", "Lifts from 5 below 40 t need the lift supervisor", "lift.t", 5, 40, "role-lift-supervisor", "tonnes", "lift_ok")})
    w = effects(wide, change("rule-mc", wide, {"value": 90, "unit": "tonnes"}))
    sup = next(c for c in w[0]["covered_by"] if c["rule_id"] == "rule-sup")
    assert sup["whole_range"] is False and "from 25 below 40 tonnes" in w[0]["text"] and w[0]["uncovered"] == [{"from": 40, "to": 80}]
    # the AEV4-G-09 shape: a single upper tier from 5,000 covers part of 500-7,500; the gap 500-5,000 is named with its figures, never as "the rest"
    tools = {r["rule_id"]: r for r in [rule("rule-ws", "Tools of 500 AUD or more: workshop supervisor", "t.aud", "gte", 500, "role-workshop-supervisor", unit="AUD"),
                                       rule("rule-wm", "Tools of 5,000 AUD or more: workshop manager", "t.aud", "gte", 5000, "role-workshop-manager", unit="AUD"),
                                       rule("rule-ed", "Tools of 40,000 AUD or more: engineering director", "t.aud", "gte", 40000, "role-engineering-director", unit="AUD")]}
    g = effects(tools, change("rule-ws", tools, {"value": 7500, "unit": "AUD"}))
    assert g[0]["uncovered"] == [{"from": 500, "to": 5000}] and "reaches cases from 500 below 5000 AUD." in g[0]["text"] and "the rest" not in g[0]["text"]


def test_a_cover_of_a_different_kind_or_direction_is_not_a_cover_and_other_conditions_are_said():
    rules = {r["rule_id"]: r for r in [rule("rule-a", "Orders of 100 JPY or more are approved by A", "o.jpy", "gte", 100, "role-a"),
                                       rule("rule-notice", "Orders of 50 JPY or more are notified by N", "o.jpy", "gte", 50, "role-n", outcome="notice_sent"),
                                       rule("rule-cap", "Orders of 40 JPY or less are refused by C", "o.jpy", "lte", 40, "role-c")]}
    e = effects(rules, change("rule-a", rules, {"value": 300, "unit": "JPY"}))
    assert e[0]["covered_by"] == [] and "no other rule of the same kind in the knowledge applies to them" in e[0]["text"]                                     # a notice is another duty; an upper bound another direction
    capex = copy.deepcopy(rules["rule-notice"])
    capex.update(rule_id="rule-b", title="CAPEX orders of 10 JPY or more are approved by B", actor_ref="role-b", outcome={"kind": "review_required", "predicate": ""},
                 condition={"all": [{"field": "o.category", "op": "eq", "value": "CAPEX"}, {"field": "o.jpy", "op": "gte", "value": 10, "unit": "JPY"}]})
    rules["rule-b"] = capex
    e2 = effects(rules, change("rule-a", rules, {"value": 300, "unit": "JPY"}))
    assert [c["rule_id"] for c in e2[0]["covered_by"]] == ["rule-b"] and "when its other conditions hold" in e2[0]["text"]


def test_a_lowered_threshold_adds_a_range_and_the_rule_that_follows_from_this_rules_act_is_said_to_reach_those_cases_without_a_hold():
    """agd-09: the Warehouse Safety Lead inspects pallets over 1,000 kg -> over 800 kg; pallets the Safety Lead inspected are racked at ground level."""
    lead = rule("rule-insp", "Inbound pallets over 1,000 kg are inspected by the Warehouse Safety Lead", "pallet.weight_kg", "gt", 1000, "role-warehouse-safety-lead", unit="kg",
                outcome="inspected")
    bay = copy.deepcopy(BASE)
    bay.update(rule_id="rule-bay", title="Pallets the Warehouse Safety Lead has inspected are racked only in ground-level bays", actor_ref="role-racking-team", status="VALIDATED",
               authority="OBSERVED", condition={"field": "pallet.safety_lead_inspected", "op": "eq", "value": True}, outcome={"kind": "racked_ground_level", "predicate": ""},
               exceptions=[], scope={})
    rules = {"rule-insp": lead, "rule-bay": bay}
    ch = change("rule-insp", rules, {"value": 800, "unit": "kg"})
    assert holds(rules, ch) == []                                                                               # a widening change loses no case: no hold
    e = effects(rules, ch)
    assert [x["effect"] for x in e] == ["range_added"] and e[0]["followers"] == ["rule-bay"]
    assert "above 800 up to 1000 kg" in e[0]["text"] and "ground-level bays" in e[0]["text"] and "reach it too" in e[0]["text"]
    alone = effects({"rule-insp": lead}, change("rule-insp", {"rule-insp": lead}, {"value": 800, "unit": "kg"}))
    assert alone[0]["followers"] == [] and alone[0]["text"] == "Cases above 800 up to 1000 kg now reach “Inbound pallets over 1,000 kg are inspected by the Warehouse Safety Lead” (warehouse safety lead)."
    assert "no other rule" not in alone[0]["text"]                                     # A101: not finding a follower proves nothing, so nothing is denied


def _state_rule(rid, title, field, actor, outcome):
    r = copy.deepcopy(BASE)
    r.update(rule_id=rid, title=title, actor_ref=actor, status="VALIDATED", authority="OBSERVED", condition={"field": field, "op": "eq", "value": True},
             outcome={"kind": outcome, "predicate": ""}, exceptions=[], scope={})
    return r


def test_a_state_named_act_by_actor_is_a_follower_and_another_act_or_another_person_is_not():
    """A101 items AEV4-G-11 / G-12: "incident.reported_by_duty_officer" follows from the duty officer's incident report; the server had missed it and said
    no rule follows. The same person's other act, or the same act by someone else, is never taken for this rule's."""
    report = rule("rule-report", "Service delays of 15 minutes or more are reported by the Control Room Duty Officer", "service.delay_minutes", "gte", 15,
                  "role-control-room-duty-officer", unit="minutes", outcome="incident_report_required")
    notice = _state_rule("rule-notice", "Incidents reported by the duty officer get a passenger service notice", "incident.reported_by_duty_officer",
                         "role-passenger-information-officer", "service_notice_required")
    debrief = _state_rule("rule-debrief", "Road collisions get a driver debrief", "incident.road_collision", "role-depot-safety-officer", "debrief_required")
    rules = {r["rule_id"]: r for r in (report, notice, debrief)}
    e = effects(rules, change("rule-report", rules, {"value": 10, "unit": "minutes"}))
    assert [x["effect"] for x in e] == ["range_added"] and e[0]["followers"] == ["rule-notice"] and "passenger service notice" in e[0]["text"]
    ground = rule("rule-ground", "Defects of 7 points or more ground the bus (Workshop Foreman)", "defect.severity_points", "gte", 7, "role-workshop-foreman",
                  unit="points", outcome="vehicle_grounded")
    spare = _state_rule("rule-spare", "A bus the foreman grounded gets a replacement", "vehicle.grounded_by_workshop_foreman", "role-service-planner", "replacement_required")
    g = {r["rule_id"]: r for r in (ground, spare)}
    assert effects(g, change("rule-ground", g, {"value": 5, "unit": "points"}))[0]["followers"] == ["rule-spare"]
    # negatives: another act of the same person; the same act by another person
    other_act = _state_rule("rule-x", "Vehicles the duty officer cleared return to service", "vehicle.cleared_by_duty_officer", "role-depot-clerk", "return_required")
    other_who = _state_rule("rule-y", "Incidents reported by the driver are logged", "incident.reported_by_driver", "role-depot-clerk", "logging_required")
    neg = {r["rule_id"]: r for r in (report, other_act, other_who)}
    assert effects(neg, change("rule-report", neg, {"value": 10, "unit": "minutes"}))[0]["followers"] == []
    # the same proof feeds the hold: a raised threshold drops cases the follower depended on
    h = holds(rules, change("rule-report", rules, {"value": 20, "unit": "minutes"}))
    assert [(x["hold"], x["other_rule_id"]) for x in h] == [("dependent_rule_loses_trigger", "rule-notice")]


def test_an_upper_bound_moved_down_drops_the_range_below_it_reversed_inequality():
    rules = {r["rule_id"]: r for r in [rule("rule-fast", "Claims of 30,000 JPY or less are paid without review by Payroll", "c.jpy", "lte", 30000, "role-payroll", outcome="paid")]}
    e = effects(rules, change("rule-fast", rules, {"value": 20000, "unit": "JPY"}))
    assert e[0]["effect"] == "range_vacated" and "above 20000 up to 30000 JPY" in e[0]["text"]
    up = effects(rules, change("rule-fast", rules, {"value": 40000, "unit": "JPY"}))
    assert up[0]["effect"] == "range_added" and "above 30000 up to 40000 JPY" in up[0]["text"]


def test_an_actor_change_states_a_rule_whose_condition_names_the_old_actors_act_and_nothing_for_an_unrelated_rule():
    """AEV-G-05: implant orders signed off by the lead surgeon -> the clinical director of surgery; the release rule is met when the lead surgeon signed off."""
    sign = rule("rule-sign", "Implantable device orders need sign-off by the lead surgeon", "item.class", "eq", "IMPLANT", "role-lead-surgeon", unit=None)
    rel = copy.deepcopy(BASE)
    rel.update(rule_id="rule-release", title="Implant orders that the lead surgeon has signed off are released by the theatre stores supervisor",
               actor_ref="role-theatre-stores-supervisor", status="VALIDATED", authority="OBSERVED", exceptions=[], scope={}, outcome={"kind": "released", "predicate": ""},
               condition={"all": [{"field": "item.class", "op": "eq", "value": "IMPLANT"}, {"field": "order.lead_surgeon_signed_off", "op": "eq", "value": True}]})
    other = rule("rule-stock", "Implant stock below 5 units is reordered by the stores supervisor", "stock.units", "lt", 5, "role-theatre-stores-supervisor", unit="units", outcome="reordered")
    rules = {"rule-sign": sign, "rule-release": rel, "rule-stock": other}
    ch = change("rule-sign", rules, "role-clinical-director-of-surgery", kind="set_actor", before="role-lead-surgeon")
    assert holds(rules, ch) == []                                                                               # stays a clean change: no hold
    e = effects(rules, ch)
    assert [(x["effect"], x["other_rule_id"]) for x in e] == [("actor_dependency", "rule-release")] and "order.lead_surgeon_signed_off" in e[0]["text"]
    assert "clinical director of surgery acts instead" in e[0]["text"]


def test_no_effect_for_an_unchanged_value_or_a_non_numeric_change_and_effects_reach_the_prompt():
    rules = {r["rule_id"]: r for r in [rule("rule-x", "X of 10 JPY or more is approved by X", "x.jpy", "gte", 10, "role-x")]}
    assert effects(rules, change("rule-x", rules, {"value": 10, "unit": "JPY"})) == []
    draft = {"operations": [], "rule_changes": [], "file_actions": [], "unresolved": [], "request": "r", "proposed_dependencies": [],
             "effects": [{"effect": "range_vacated", "ref": "E1", "text": "Cases from 10 below 20 JPY no longer reach X."}]}
    frozen = {"prompt_template": "{request}|{changes}|{rules}|{items}"}
    assert "- effect ref=E1 (server, proven from the rules): Cases from 10 below 20 JPY no longer reach X." in bd.build_guidance_prompt(frozen, draft, can_read_evidence=False, max_chars=1000)


def test_an_actor_change_classes_the_rule_whose_condition_names_the_old_actors_act_as_follows_from_never_no_action():
    """A101 item AEV4-G-14: the `actor_dependency` effect asked a person to check the roster-return rule while its file item said "shares only the topic ...
    no action". Both now rest on the same proof; an unrelated rule keeps its label."""
    nurse = rule("rule-fit", "Drivers back from 5 days of sick leave or more are assessed by the Occupational Health Nurse", "driver.sick_leave_days", "gte", 5,
                 "role-occupational-health-nurse", unit="days", outcome="fitness_assessment_required")
    roster = _state_rule("rule-roster", "Drivers the nurse cleared return to the roster", "driver.cleared_by_occupational_health_nurse", "role-roster-coordinator", "roster_return")
    other = _state_rule("rule-test", "Drivers in a collision get a substance test", "driver.involved_in_collision", "role-depot-medical-officer", "substance_test_required")
    rules = {r["rule_id"]: r for r in (nurse, roster, other)}
    ch = change("rule-fit", rules, "role-depot-medical-officer", kind="set_actor", before="role-occupational-health-nurse")
    assert [(x["effect"], x["other_rule_id"]) for x in effects(rules, ch)] == [("actor_dependency", "rule-roster")]
    kind, about = bd._item_link({"role": "related", "rule_ids": ["rule-roster"]}, rules, ch, {}, {})
    assert kind == "follows_from" and "driver.cleared_by_occupational_health_nurse" in about and "depot medical officer acts instead" in about
    assert bd._item_link({"role": "related", "rule_ids": ["rule-test"]}, rules, ch, {}, {})[0] == "same_topic_only"


def _added_draft():
    added = {"effect": "range_added", "ref": "E1", "range": {"from": 60, "to": 90, "unit": "days"},
             "text": "Cases from 60 below 90 days now reach “Coolant check” (maintenance technician)."}
    return {"file_actions": [{"ref": "F001", "action": "AI_EDIT_SUPPORTED"}], "unresolved": [], "effects": [added]}


def test_ai_sentences_that_call_newly_reached_cases_not_due_are_removed_with_attribution_and_the_rest_is_kept():
    """Hardening H2 (A105 AEV6-G-12, AEV5-G-04): after a recurring step was lowered the model said the added cases "now reach" the step and also "are simply not
    due yet". A note beside it left the reader two contradicting texts. Now the contradicting sentence or instruction is removed and recorded (by the server, with
    the reason); every other sentence of the model stays its own. Negatives: no removal when nothing contradicts, or for a dropped recurring range."""
    from tests.final_loop.test_chg03_draft import LIMITS, gitem, gobj
    obj = gobj(items=[gitem("F001", instruction="Update the rule to 60 days; those cases are simply not due yet.")],
               summary="The check moves from 90 to 60 days. Cases from 60 below 90 days are simply not due yet. File F001 needs a wording update.",
               effects=[{"ref": "E1", "statement": "Chillers with 60 below 90 days since the last check now reach the coolant check earlier."}])
    out = bd.resolve_guidance(obj, _added_draft(), LIMITS)
    assert out["summary"] == "The check moves from 90 to 60 days. File F001 needs a wording update."
    assert [r["where"] for r in out["removed"]] == ["instruction", "summary"] and all(r["by"] == "server" and r["reason"] == "contradicts_server_fact" for r in out["removed"])
    assert out["accepted"] == [] and {"ref": "F001", "reason": "contradicts_server_fact"} in out["rejected"]
    assert out["effect_statements"] == [{"ref": "E1", "statement": "Chillers with 60 below 90 days since the last check now reach the coolant check earlier.",
                                         "status": "kept", "reason": None}]
    clean = bd.resolve_guidance(gobj(items=[gitem("F001")], summary="Cases from 60 below 90 days now reach the technician earlier.",
                                     effects=[{"ref": "E1", "statement": "Cases from 60 below 90 days now reach the check."}]), _added_draft(), LIMITS)
    assert clean["removed"] == [] and clean["summary"].endswith("earlier.")
    vac = {"file_actions": [{"ref": "F001", "action": "AI_EDIT_SUPPORTED"}], "unresolved": [], "effects": [
        {"effect": "range_vacated", "ref": "E1", "range": {"from": 20000, "to": 25000}, "covered_by": [], "uncovered": [{"from": 20000, "to": 25000}],
         "text": "Cases from 20000 below 25000 km no longer reach “Brake check” (technician), and no other rule of the same kind in the knowledge applies to them."}]}
    nd = bd.resolve_guidance(gobj(items=[gitem("F001")], summary="Those buses are simply not due yet.",
                                  effects=[{"ref": "E1", "statement": "Buses from 20000 below 25000 km are not due yet."}]), vac, LIMITS)
    assert nd["removed"] == [] and nd["effect_statements"][0]["status"] == "kept"                   # "not due" is right for a dropped recurring range


def test_each_effect_statement_is_checked_against_its_fact_and_the_model_coverage_is_counted_apart():
    """Hardening H1 (A105 AEV6-G-09): an uncovered part of a dropped range must be stated with its figures; a figure the effect does not hold is refused; an
    effect the model left out is counted missing. These are the model's statements only; the effect itself is always shown by the server."""
    from tests.final_loop.test_chg03_draft import LIMITS, gitem, gobj
    eff = {"effect": "range_vacated", "ref": "E1", "range": {"from": 40, "to": 120, "unit": "tonnes"},
           "covered_by": [{"rule_id": "rule-gm", "from_value": 100, "whole_range": False}], "uncovered": [{"from": 40, "to": 100}],
           "text": "Cases from 40 below 120 tonnes no longer reach “Head brewer” (head brewer). They still reach “GM” (general manager) from 100 tonnes. "
                   "No other rule of the same kind in the knowledge reaches cases from 40 below 100 tonnes."}
    added = {"effect": "range_added", "ref": "E2", "range": {"from": 5, "to": 7}, "text": "Cases from 5 below 7 points now reach “Grounding” (foreman)."}
    draft = {"file_actions": [{"ref": "F001", "action": "AI_EDIT_SUPPORTED"}], "unresolved": [], "effects": [eff, added]}

    def run(statements):
        return bd.resolve_guidance(gobj(items=[gitem("F001")], effects=statements), draft, LIMITS)
    ok = run([{"ref": "E1", "statement": "From 100 t the general manager approves; contracts of 40 below 100 t reach no rule."},
              {"ref": "E2", "statement": "Defects of 5 below 7 points now reach grounding earlier."}])
    assert [x["status"] for x in ok["effect_statements"]] == ["kept", "kept"] and ok["effects_coverage"]["kept"] == 2 and ok["effects_coverage"]["missing"] == []
    gap = run([{"ref": "E1", "statement": "From 100 t the general manager approves."}])
    assert gap["effect_statements"][0]["status"] == "incomplete" and gap["effect_statements"][0]["reason"] == "uncovered_part_not_stated"
    assert gap["effects_coverage"]["missing"] == ["E2"]
    bad = run([{"ref": "E1", "statement": "Contracts of 40 below 90 t reach no rule."}, {"ref": "E2", "statement": "Defects of 5 below 7 points are not due."},
               {"ref": "E9", "statement": "Anything."}])
    assert [x["reason"] for x in bad["effect_statements"]] == ["number_not_in_the_effect", "contradicts_server_fact"]
    assert {"ref": "E9", "reason": "effect_ref_not_in_draft"} in bad["rejected"]
    for o in (ok, gap, bad):
        assert not any(r.get("where") == "summary" for r in o["removed"])                          # the server writes nothing in the model's name


def test_a109_a_sentence_claiming_cover_for_a_partly_uncovered_range_is_removed_and_a_correct_one_stays():
    """A109 (AEV7-G-08): the summary said cases from 5,000 to 30,000 CHF "now fall under the workshop foreman or operations director tiers"; the fact proves
    5,000 to under 20,000 reached by no rule. The sentence is removed (by the server, with the reason); a sentence naming the uncovered part stays, and so does a
    sentence that only says the cases no longer reach the changed rule."""
    from tests.final_loop.test_chg03_draft import LIMITS, gitem, gobj
    eff = {"effect": "range_vacated", "ref": "E1", "range": {"from": 5000, "to": 30000, "unit": "CHF"},
           "covered_by": [{"rule_id": "rule-dir", "from_value": 20000, "whole_range": False}], "uncovered": [{"from": 5000, "to": 20000}],
           "text": "Cases from 5000 below 30000 CHF no longer reach “Fleet” (fleet supervisor). They still reach “Director” (operations director) from 20000 CHF. "
                   "No other rule of the same kind in the knowledge reaches cases from 5000 below 20000 CHF."}
    draft = {"file_actions": [{"ref": "F001", "action": "AI_EDIT_SUPPORTED"}], "unresolved": [], "effects": [eff]}
    bad = ("Raising the threshold to 30,000 CHF inverts the order. Cases from 5,000 to 30,000 CHF no longer reach the fleet supervisor approval step and now fall "
           "under the workshop foreman or operations director tiers. A person decides how the tiers fit.")
    out = bd.resolve_guidance(gobj(items=[gitem("F001")], summary=bad), draft, LIMITS)
    assert out["summary"] == "Raising the threshold to 30,000 CHF inverts the order. A person decides how the tiers fit."
    assert [r["where"] for r in out["removed"]] == ["summary"] and out["removed"][0]["by"] == "server"
    ok = ("Cases from 5,000 below 30,000 CHF no longer reach the fleet supervisor. From 20,000 CHF they still reach the operations director; cases from 5,000 below "
          "20,000 CHF reach no rule.")
    assert bd.resolve_guidance(gobj(items=[gitem("F001")], summary=ok), draft, LIMITS)["removed"] == []
    plain = "Cases from 5,000 to 30,000 CHF no longer reach the fleet supervisor."
    assert bd.resolve_guidance(gobj(items=[gitem("F001")], summary=plain), draft, LIMITS)["removed"] == []


def test_r5_cover_claim_variants_both_ends_named_no_cover_at_all_and_negatives():
    """R5 (2026-10-28) variants of the A109 G-08 class: a sentence naming both ends of a range no rule reaches, a dropped range with no cover at all, one figure
    inside the gap. Negatives: a true cover of the covered part, a sentence that states the gap, a boundary figure, a sentence without a cover phrase."""
    from tests.final_loop.test_chg03_draft import LIMITS, gitem, gobj
    nocover = {"effect": "range_vacated", "ref": "E1", "range": {"from": 1000000, "to": 1500000, "unit": "JPY"}, "covered_by": [], "uncovered": [{"from": 1000000, "to": 1500000}],
               "text": "Cases from 1000000 below 1500000 JPY no longer reach “Manager” (manager), and no other rule of the same kind in the knowledge applies to them."}
    draft = {"file_actions": [{"ref": "F001", "action": "AI_EDIT_SUPPORTED"}], "unresolved": [], "effects": [nocover]}
    def removed(summary, d=draft):
        return [r["text"] for r in bd.resolve_guidance(gobj(items=[gitem("F001")], summary=summary), d, LIMITS)["removed"]]
    assert removed("Raise the threshold. Cases from 1,000,000 to 1,500,000 JPY now fall under the finance review.") == [
        "Cases from 1,000,000 to 1,500,000 JPY now fall under the finance review."]                                              # both ends named, no cover at all
    assert removed("Purchases of 1,200,000 JPY are handled by the CFO now.") != []                                              # one figure inside the gap
    assert removed("Cases from 1,000,000 to 1,500,000 JPY no longer reach the manager, and no rule covers them now.") == []    # states the gap
    assert removed("Purchases of 1,500,000 JPY or more still reach the manager.") == []                                        # the boundary: still the manager's
    assert removed("Cases from 1,000,000 to 1,500,000 JPY no longer need manager approval.") == []                              # no cover phrase
    part = {"effect": "range_vacated", "ref": "E1", "range": {"from": 5000, "to": 30000, "unit": "CHF"},
            "covered_by": [{"rule_id": "rule-fore", "from_value": 5000, "to_value": 10000, "whole_range": False}], "uncovered": [{"from": 10000, "to": 30000}], "text": "x"}
    d2 = dict(draft, effects=[part])
    assert removed("Cases from 5,000 below 10,000 CHF now fall under the workshop foreman.", d2) == []                         # true: the covered part
    assert removed("Cases from 5,000 to 30,000 CHF now fall under the workshop foreman.", d2) != []                             # claims the whole range
    assert removed("Cases from 12,000 CHF now go to the workshop foreman.", d2) != []                                          # inside the gap


def test_r5_the_figures_that_qualify_the_cover_phrase_decide_and_a_true_partial_cover_stays():
    """Replay of every stored guidance sentence (1,280) through the clause check: one verdict changes, AEV5-G-09 ("cases from 25 below 80 tonnes ... still reach
    the lift supervisor" while no rule reaches 25 to under 80) — a false cover the figure test missed. True sentences naming a cover from its own figure stay."""
    from tests.final_loop.test_chg03_draft import LIMITS, gitem, gobj
    eff = {"effect": "range_vacated", "ref": "E1", "range": {"from": 25, "to": 90, "unit": "tonnes"},
           "covered_by": [{"rule_id": "rule-oim", "from_value": 80, "to_value": None, "whole_range": False}], "uncovered": [{"from": 25, "to": 80}], "text": "x"}
    draft = {"file_actions": [{"ref": "F001", "action": "AI_EDIT_SUPPORTED"}], "unresolved": [], "effects": [eff]}
    def removed(summary):
        return bool(bd.resolve_guidance(gobj(items=[gitem("F001")], summary=summary), draft, LIMITS)["removed"])
    assert removed("Cases from 25 below 80 tonnes no longer reach the marine coordinator and still reach the lift supervisor, while cases from 80 below 90 tonnes "
                   "reach the offshore installation manager.")
    assert not removed("Cases from 25 below 90 tonnes no longer reach the marine coordinator and still reach the offshore installation manager from 80 tonnes.")
    assert not removed("Cases from 25 below 90 tonnes no longer reach the marine coordinator, but cases of 80 tonnes or more still reach the offshore installation manager.")
