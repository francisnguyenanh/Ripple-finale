"""CHG-01 coverage check: a scope qualifier is not a part of its own (live diagnostic SUR03-V2-20261023); real sub-requests are still parts and still checked."""
from core.change_intelligence.v2 import business_change as B


def test_a_leading_or_trailing_qualifier_joins_its_part():
    assert B._segments("In emergency routing (route_emergency), raise the hard-coded threshold of 1,000,000 JPY to 1,200,000 JPY.") == [
        "In emergency routing (route_emergency), raise the hard-coded threshold of 1,000,000 JPY to 1,200,000 JPY"]
    assert B._segments("In standard routing, reject amounts above 100,000,000 JPY as invalid, in addition to amounts of zero or less.") == [
        "In standard routing, reject amounts above 100,000,000 JPY as invalid, in addition to amounts of zero or less"]


def test_parts_with_a_value_or_a_change_verb_stay_separate():
    multi = ("Raise the manager threshold to 1,500,000 JPY, send high-risk suppliers to Finance instead of the Director, "
             "and allow emergency purchases up to 500,000 JPY without Finance.")
    assert len(B._segments(multi)) == 3
    assert B._segments("For emergency purchases, require Finance approval, and in emergency routing set the cap to 2,000,000 JPY.") == [
        "For emergency purchases, require Finance approval", "in emergency routing set the cap to 2,000,000 JPY"]


def test_unrelated_text_after_a_qualifier_is_still_reported_as_not_covered():
    segs = B._segments("In the office, the coffee machine is broken and also lower the manager threshold to 900,000 JPY.")
    assert segs == ["In the office, the coffee machine is broken", "lower the manager threshold to 900,000 JPY"]
    assert not B._covered(segs[0], ["lower the manager threshold to 900,000 JPY"])


def test_a_value_clause_and_an_as_clause_stay_with_their_head():
    """Live finding SUR08-V1 A9b (2026-10-23): "Change the CAPEX Finance review threshold, as people decided it, from 2,000,000 to 5,000,000 JPY." was split into
    three parts and the first two were asked about as dropped requests."""
    from core.change_intelligence.v2 import business_change as bc
    assert bc._segments("Change the CAPEX Finance review threshold, as people decided it, from 2,000,000 to 5,000,000 JPY.") == \
        ["Change the CAPEX Finance review threshold, as people decided it, from 2,000,000 to 5,000,000 JPY"]
    assert bc._segments("Raise the threshold to 1,500,000 JPY, and send CAPEX above 3,000,000 JPY to Finance.") == \
        ["Raise the threshold to 1,500,000 JPY", "send CAPEX above 3,000,000 JPY to Finance"]


def test_a_head_ending_in_a_modal_joins_the_part_after_also():
    """SUR-03 v4: "Standard routing (route_request) must also reject ..." was split at "also" and the head "... must" asked about as a dropped part."""
    assert B._segments("Standard routing (route_request) must also reject an amount larger than 80,000,000 JPY as an invalid amount.") == [
        "Standard routing (route_request) must reject an amount larger than 80,000,000 JPY as an invalid amount"]


# ---- contract version 2: kept_as_is (model-labelled, server-verified) and the rule location ----------------------------------------------------------------

RULE = {"title": "request.net_amount_jpy - must be greater than zero", "condition": {"field": "request.net_amount_jpy", "op": "lte", "value": 0, "unit": "JPY"},
        "source_bindings": [{"evidence_ref": '{"line_end": 44, "line_start": 8, "qualname": "route_request", "type": "code_span"}'},
                            {"evidence_ref": "config/approval.yaml#thresholds.manager"}]}
REQ = B._norm("Standard routing must also reject an amount larger than 80,000,000 JPY as an invalid amount; amounts of zero or less stay rejected.")


def test_a_restatement_of_the_current_condition_is_accepted_and_anything_else_is_refused():
    rules = {"r1": RULE}
    assert B._kept_reason("amounts of zero or less stay rejected", "r1", rules, REQ) is None
    assert B._kept_reason("amounts of zero or less remain rejected", "r1", rules, REQ) == "quote_not_in_request"
    assert B._kept_reason("amounts of zero or less stay rejected", "r9", rules, REQ) == "rule_not_offered"
    req2 = B._norm("Raise it; amounts of 5 or less stay rejected; the CFO must sign.")
    assert B._kept_reason("amounts of 5 or less stay rejected", "r1", rules, req2) == "number_not_current"         # not the current value: a change, asked
    assert B._kept_reason("the CFO must sign", "r1", rules, req2) == "states_a_change"
    req3 = B._norm("Raise it; the coffee machine stays broken.")
    assert B._kept_reason("the coffee machine stays broken", "r1", rules, req3) == "not_about_this_rule"


def test_the_rule_location_comes_from_its_own_bindings_and_appears_in_the_prompt():
    assert B.rule_location(RULE) == "code route_request; approval.yaml#thresholds.manager"
    frozen = {"prompt_template": "R: {request}\n{rules}"}
    p = B.build_prompt(frozen, "change {rules} please", [{"item_id": "r1", "authority": "OBSERVED", "status": "VALIDATED", "text": "t"}], max_chars=1000,
                       where={"r1": "code route_request"})
    assert p == "R: change {rules} please\n- id=r1 authority=OBSERVED status=VALIDATED\n  text: t\n  where: code route_request"     # one pass: the request stays literal


def test_an_empty_unit_is_no_unit_and_a_contract_refusal_is_a_question_not_a_crash():
    """SUR03-V5B: the model wrote `"unit": ""` for a quantity bound; the contract refused it with an exception that escaped the executor (job left RUNNING)."""
    assert B._drop_empty_units({"any": [{"field": "request.quantity", "op": "gt", "value": 150, "unit": ""}, {"field": "a", "op": "eq", "value": 1, "unit": "JPY"}]}) == \
        {"any": [{"field": "request.quantity", "op": "gt", "value": 150}, {"field": "a", "op": "eq", "value": 1, "unit": "JPY"}]}
    assert B._drop_empty_units({"value": 5, "unit": " "}) == {"value": 5}


def test_plural_words_in_a_restatement_meet_the_rule_words():
    """SUR03-V5D: "quantities of zero or less stay invalid" was refused as not about the quantity rule (naive plural handling)."""
    rule = {"title": "request.quantity - must be valid quantity", "condition": {"field": "request.quantity", "op": "lte", "value": 0}}
    req = B._norm("Quantities above 150 are invalid too; quantities of zero or less stay invalid.")
    assert B._kept_reason("quantities of zero or less stay invalid", "q", {"q": rule}, req) is None
    assert B._singular("quantities") == "quantity" and B._singular("amounts") == "amount" and B._singular("process") == "process"


def test_a_filler_fragment_is_not_a_sub_request():
    """CORE-V5-S1 V5-U-11: "and while you are at it," was asked about as a dropped part."""
    segs = B._segments("Line managers should pre-approve overtime above 12 hours a month, and while you are at it, night overtime should be reported to the shift supervisor.")
    assert segs == ["Line managers should pre-approve overtime above 12 hours a month", "night overtime should be reported to the shift supervisor"]


def test_an_equivalent_single_threshold_condition_edit_is_recorded_as_set_value_and_nothing_else_is():
    hours = {"title": "t", "condition": {"field": "overtime.hours_per_month", "op": "gt", "value": 30, "unit": "hours"}}
    assert B._canonical_value_edit("add_or_change_condition", {"op": "gt", "field": "overtime.hours_per_month", "value": 40, "unit": "hours"}, hours) == {"value": 40, "unit": "hours"}
    assert B._canonical_value_edit("add_or_change_condition", {"op": "gte", "field": "overtime.hours_per_month", "value": 40}, hours) is None          # another comparator
    assert B._canonical_value_edit("add_or_change_condition", {"op": "gt", "field": "overtime.night_hours", "value": 40}, hours) is None               # another field
    months = {"title": "t", "condition": {"field": "lease.term.months", "op": "gt", "value": 36}}                                                     # no unit: stays a condition edit
    assert B._canonical_value_edit("add_or_change_condition", {"op": "gt", "field": "lease.term.months", "value": 48}, months) is None
    composite = {"title": "t", "condition": {"all": [{"field": "a.amount", "op": "gte", "value": 1, "unit": "JPY"}, {"field": "a.kind", "op": "eq", "value": "X"}]}}
    assert B._canonical_value_edit("add_or_change_condition", {"op": "gte", "field": "a.amount", "value": 2, "unit": "JPY"}, composite) is None   # would drop a part


# ---- contract version 4 (after UNDERSTAND-V6) -------------------------------------------------------------------------------------------------------------------

def test_a_currency_is_written_the_usual_way_and_only_zero_counts_as_a_number_word():
    assert B._clean_units({"value": 8000000, "unit": "yen"}) == {"value": 8000000, "unit": "JPY"}
    assert B._clean_units({"all": [{"field": "a", "op": "gte", "value": 1, "unit": "Yen"}]}) == {"all": [{"field": "a", "op": "gte", "value": 1, "unit": "JPY"}]}
    bank = {"title": "Vendor bank detail changes get a callback", "condition": {"field": "vendor.bank_changed", "op": "eq", "value": "true"}}
    req = B._norm("Raise it; bank detail changes get a callback from the vendor master team, which is already how it works, so leave that one.")
    assert B._kept_reason("bank detail changes get a callback from the vendor master team, which is already how it works, so leave that one", "b", {"b": bank}, req) is None


def test_a_context_remark_is_accepted_only_without_any_change_signal():
    rules = {"r": {"title": "Controller approves yen payments from 5,000,000 JPY", "actor_ref": "role-vendor-master-team", "condition": {"field": "payment.amount_jpy", "op": "gte", "value": 5000000}}}
    req = B._norm("Three points on vendor payments. That is the one I mean. The CFO decides from now on. The vendor master team handles it. Not the handbook figure. Pay 300 more.")
    assert B._context_reason("Three points on vendor payments", rules, req) is None
    assert B._context_reason("That is the one I mean", rules, req) is None
    assert B._context_reason("The CFO decides from now on", rules, req) == "may_state_a_change"
    assert B._context_reason("The vendor master team handles it", rules, req) == "may_state_a_change"
    assert B._context_reason("Not the handbook figure", rules, req) == "may_state_a_change"
    assert B._context_reason("Pay 300 more", rules, req) == "has_a_number"
    assert B._context_reason("A remark that is not in the request", rules, req) == "quote_not_in_request"


# ---- server fixes after UNDERSTAND-V7 ------------------------------------------------------------------------------------------------------------------------------

def test_a_field_written_in_words_maps_to_the_rule_own_field_only_when_unambiguous():
    rule = {"condition": {"all": [{"field": "payroll.retro_correction_amount_jpy", "op": "gte", "value": 100000, "unit": "JPY"}, {"field": "payroll.kind", "op": "eq", "value": "RETRO"}]}}
    assert B._rule_fields({"field": "correction amount jpy", "op": "gte", "value": 150000}, rule)["field"] == "payroll.retro_correction_amount_jpy"
    assert B._rule_fields({"field": "amount", "op": "gte", "value": 1}, rule)["field"] == "amount"                         # one word: not mapped
    assert B._rule_fields({"field": "bonus amount", "op": "gte", "value": 1}, rule)["field"] == "bonus amount"           # not words of a rule field: not mapped


def test_scaled_numbers_and_no_change_restatements():
    assert 3000000.0 in B.numbers_in("Sales director approval should start at 3 million yen.") and 1500000.0 in B.numbers_in("1.5 million JPY")
    rule = {"title": "Forklift loads above 1,500 kg need a certified driver", "condition": {"field": "load.weight_kg", "op": "gt", "value": 1500}}
    req = B._norm("Raise it; forklift loads stay at 1,500 kg, no change there.")
    assert B._kept_reason("forklift loads stay at 1,500 kg, no change there", "f", {"f": rule}, req) is None


# ---- server fixes after SUR03-V6 (2026-10-24) ---------------------------------------------------------------------------------------------------------------------

def test_a_restatement_may_name_its_rule_anywhere_in_its_own_sentence_but_not_in_another():
    rule = {"title": "request.quantity - must be valid quantity", "condition": {"field": "request.quantity", "op": "lte", "value": 0}}
    req = "In route_emergency, quantities larger than 600 are invalid quantities; zero or less is already handled."
    assert B._kept_reason("zero or less is already handled", "r", {"r": rule}, B._norm(req), req) is None                  # SUR03-V6 item 08
    assert B._kept_reason("zero or less is already handled", "r", {"r": rule}, B._norm(req)) == "not_about_this_rule"      # without the sentence: as before
    other = "Quantities matter a lot. Zero or less is already handled."
    assert B._kept_reason("Zero or less is already handled", "r", {"r": rule}, B._norm(other), other) == "not_about_this_rule"   # another sentence does not count
    money = {"title": "Purchases of 1,000,000 JPY or more need manager approval", "condition": {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000}}
    disguised = "Raise the standard manager approval threshold from 1,000,000 to 1,500,000 JPY; high-risk suppliers stay with Finance."
    assert B._kept_reason("high-risk suppliers stay with Finance", "r", {"r": money}, B._norm(disguised), disguised) == "not_about_this_rule"   # a part with its own subject


def test_a_context_part_stating_the_current_value_of_the_changed_rule_is_kept_as_is_and_marked():
    import copy
    import json
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    limits = json.loads((root / "config" / "change_understand.json").read_text(encoding="utf-8"))["limits"]
    base = json.loads((root / "tests/fixtures/final_knowledge_loop/expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"][0]
    rule = copy.deepcopy(base)
    rule.update(rule_id="rule-em-7", title="request.net_amount_jpy - must be greater than or equal to", condition={"field": "request.net_amount_jpy", "op": "gte", "value": 1000000})
    request = "In route_emergency the manager sign-off begins at the hard-coded 1,000,000 JPY; move it up to 1,250,000 JPY."
    head = "In route_emergency the manager sign-off begins at the hard-coded 1,000,000 JPY"

    def obj(context, value=1250000):
        return {"status": "READY", "message": "Ready.", "operations": [{"kind": "set_value", "rule_id": "rule-em-7", "candidate_rule_ids": [], "desired": {"value": value, "unit": "JPY"},
                "user_quote": "1,000,000 JPY; move it up to 1,250,000 JPY"}], "questions": [], "assumptions": [], "kept_as_is": [], "context": context}
    out = B.resolve(obj([head]), request, {"rule-em-7": rule}, [], limits, pin={"snapshot_id": "s"})
    assert out["status"] == "READY" and out["uncovered_parts"] == [] and out["context_refused"] == []                   # SUR03-V6 item 01
    assert out["kept_as_is"] == [{"quote": head, "rule_id": "rule-em-7", "rule_title": rule["title"], "relabelled_from": "context"}]
    other = "In route_emergency the manager sign-off begins at the hard-coded 900,000 JPY; move it up to 1,250,000 JPY."
    bad = B.resolve(obj(["In route_emergency the manager sign-off begins at the hard-coded 900,000 JPY"]), other, {"rule-em-7": rule}, [], limits, pin={"snapshot_id": "s"})
    assert bad["status"] == "CLARIFY" and bad["kept_as_is"] == [] and bad["context_refused"][0]["reason"] == "has_a_number"   # not the current value: still asked


def _resolve(rule, request, operations, kept=(), context=(), others=()):
    import json
    import pathlib
    limits = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json").read_text(encoding="utf-8"))["limits"]
    obj = {"status": "READY", "message": "Ready.", "operations": list(operations), "questions": [], "assumptions": [], "kept_as_is": list(kept), "context": list(context)}
    return B.resolve(obj, request, {r["rule_id"]: r for r in [rule, *others]}, [], limits, pin={"snapshot_id": "s"})


def _rule(rule_id, title, condition):
    import copy
    import json
    import pathlib
    base = json.loads((pathlib.Path(__file__).resolve().parents[2] / "tests/fixtures/final_knowledge_loop/expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"][0]
    r = copy.deepcopy(base)
    r.update(rule_id=rule_id, title=title, condition=condition)
    return r


def test_a_new_number_on_the_other_side_of_the_rule_comparison_is_refused_not_rewritten():
    qty = _rule("rule-qty", "request.quantity - must be valid integer greater than zero", {"field": "request.quantity", "op": "lte", "value": 0})
    req = "Standard routing should no longer accept orders for more than 7,500 units; treat those as an invalid quantity."      # SUR03-V7 item 02
    op = {"kind": "set_value", "rule_id": "rule-qty", "candidate_rule_ids": [], "desired": {"value": 7500, "unit": "units"}, "user_quote": req}
    out = _resolve(qty, req, [op])
    assert out["operations"] == [] and out["status"] == "CLARIFY" and out["rejected"][0]["reason"] == "desired_not_supported:limit_on_the_other_side"
    money = _rule("rule-mgr", "Purchases need manager approval", {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"})
    raise_req = "Requests above 1,250,000 JPY need the manager; move it up to 1,250,000 JPY."                                   # same side: an ordinary value change
    up = {"kind": "set_value", "rule_id": "rule-mgr", "candidate_rule_ids": [], "desired": {"value": 1250000, "unit": "JPY"}, "user_quote": raise_req}
    assert _resolve(money, raise_req, [up])["operations"][0]["desired"] == {"value": 1250000, "unit": "JPY"}
    below_req = "Manager approval for requests below 900,000 JPY."
    down = {"kind": "set_value", "rule_id": "rule-mgr", "candidate_rule_ids": [], "desired": {"value": 900000, "unit": "JPY"}, "user_quote": below_req}
    assert _resolve(money, below_req, [down])["rejected"][0]["reason"] == "desired_not_supported:limit_on_the_other_side"


def test_a_kept_part_that_names_no_rule_is_read_as_context_only_when_it_passes_the_context_check():
    mgr = _rule("rule-em-7", "request.net_amount_jpy - must be greater than or equal to", {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000, "unit": "JPY"})
    req = "Change the hard-coded manager approval limit in route_emergency to 1,400,000 JPY. Everything else about emergency approvals stays the same."   # SUR03-V7 item 05
    op = {"kind": "set_value", "rule_id": "rule-em-7", "candidate_rule_ids": [], "desired": {"value": 1400000, "unit": "JPY"},
          "user_quote": "Change the hard-coded manager approval limit in route_emergency to 1,400,000 JPY"}
    rest = "Everything else about emergency approvals stays the same."
    out = _resolve(mgr, req, [op], kept=[{"quote": rest, "rule_id": "rule-em-7"}])
    assert out["status"] == "READY" and out["context"] == [rest] and out["relabelled"] == [{"quote": rest, "from": "kept_as_is", "to": "context"}]
    req2 = "Change the hard-coded manager approval limit in route_emergency to 1,400,000 JPY. High-risk suppliers stay with Finance."
    finance = dict(_rule("rule-risk", "High-risk suppliers are reviewed", {"field": "sup.risk", "op": "eq", "value": "HIGH"}), actor_ref="role-finance")
    out2 = _resolve(mgr, req2, [op], kept=[{"quote": "High-risk suppliers stay with Finance.", "rule_id": "rule-em-7"}], others=[finance])
    assert out2["status"] == "CLARIFY" and out2["context"] == [] and out2["kept_as_is_refused"][0]["reason"] == "not_about_this_rule"   # a disguised change stays asked


def test_a_condition_written_as_json_text_is_read_as_that_structure_and_a_refusal_records_the_shape():
    qty = _rule("rule-qty", "request.quantity - must be valid quantity", {"field": "request.quantity", "op": "lte", "value": 0})
    req = "Anything above 120 units in route_emergency should be rejected as an invalid quantity."                         # SUR03-V8 item 01
    cond = '{"any": [{"field": "request.quantity", "op": "lte", "value": 0}, {"field": "request.quantity", "op": "gt", "value": 120}]}'
    op = {"kind": "add_or_change_condition", "rule_id": "rule-qty", "candidate_rule_ids": [], "desired": cond, "user_quote": req}
    out = _resolve(qty, req, [op])
    assert out["status"] == "READY" and out["operations"][0]["desired"]["any"][1] == {"field": "request.quantity", "op": "gt", "value": 120}
    assert {"rule_id": "rule-qty", "from": "json_text", "to": "add_or_change_condition"} in out["normalized_operations"]
    bad = _resolve(qty, req, [dict(op, desired="quantity above 120")])                                                       # plain text stays refused
    assert bad["operations"] == [] and bad["rejected"][0]["desired_shape"] == "str"
    listed = _resolve(qty, req, [dict(op, desired=[{"field": "request.quantity", "op": "gt", "value": 120}])])               # a bare list is not guessed into any / all
    assert listed["operations"] == [] and listed["rejected"][0]["desired_shape"] == "list[object{field,op,value}]"


def test_conditions_written_as_json_text_inside_any_are_read_signed_numbers_count_and_a_heading_is_not_a_date():
    pallet = _rule("rule-pallet", "A pallet record with a gross weight of zero or less is rejected", {"field": "pallet.gross_weight_kg", "op": "lte", "value": 0})
    req = "Zero or less is already caught on pallet weights. What we also need is to reject any pallet record above 1,500 kg."    # UNDERSTAND-V9 U9-19
    parts = ['{"field": "pallet.gross_weight_kg", "op": "lte", "value": 0}', '{"field": "pallet.gross_weight_kg", "op": "gt", "value": 1500}']
    op = {"kind": "add_or_change_condition", "rule_id": "rule-pallet", "candidate_rule_ids": [], "desired": {"any": parts},
          "user_quote": "What we also need is to reject any pallet record above 1,500 kg."}
    out = _resolve(pallet, req, [op], kept=[{"quote": "Zero or less is already caught on pallet weights.", "rule_id": "rule-pallet"}])
    assert out["status"] == "READY" and out["operations"][0]["desired"]["any"][1]["value"] == 1500
    assert B.numbers_in("warmer than -18 C, not -15 C") == {-18.0, -15.0} and B.numbers_in({"value": -18}) == {-18.0} and B.numbers_in("items 1-2") == {1.0, 2.0}
    cold = _rule("rule-cold", "Inbound frozen pallets warmer than -15 C are rejected", {"field": "pallet.temperature_c", "op": "gt", "value": -15, "unit": "C"})
    req18 = "Frozen pallets warmer than -18 C should be rejected at inbound, not -15 C."                                       # UNDERSTAND-V9 U9-18
    op18 = {"kind": "set_value", "rule_id": "rule-cold", "candidate_rule_ids": [], "desired": {"value": -18, "unit": "C"}, "user_quote": req18}
    assert _resolve(cold, req18, [op18])["operations"][0]["desired"]["value"] == -18
    rules = {"r": {"title": "Interlibrary loan approval", "actor_ref": "role-subject-librarian", "condition": {"field": "loan.cost_jpy", "op": "gt", "value": 5000}}}
    text = B._norm("From the library committee: loans change. From next quarter, rooms close.")
    assert B._context_reason("From the library committee:", rules, text) is None                                              # UNDERSTAND-V9 U9-07: a heading
    assert B._context_reason("From next quarter", rules, text) == "may_state_a_change"                                        # SUR03-V8 08: a date stays asked


def test_conditions_written_in_the_rule_lines_own_words_are_read_back_only_for_the_rules_own_fields():
    pallet = _rule("rule-pallet", "A pallet record with a gross weight of zero or less is rejected", {"field": "pallet.gross_kg", "op": "lte", "value": 0, "unit": "kg"})
    observed = {"any": ["pallet gross kg at most 0 kg", "pallet gross kg more than 1500 kg"]}                               # UNDERSTAND-V9-DIAG3, raw model answer
    got, changed = B._words_to_conditions(observed, pallet)
    assert changed and got == {"any": [{"field": "pallet.gross_kg", "op": "lte", "value": 0, "unit": "kg"}, {"field": "pallet.gross_kg", "op": "gt", "value": 1500, "unit": "kg"}]}
    assert B._words_to_conditions({"any": ["pallet weight at most 0 kg"]}, pallet) == ({"any": ["pallet weight at most 0 kg"]}, False)      # not this rule's field
    assert B._words_to_conditions({"any": ["pallet gross kg more than 1500 lb"]}, pallet)[1] is False                               # another unit
    assert B._words_to_conditions({"any": ["pallet gross kg is heavy"]}, pallet)[1] is False                                        # no number
    req = "Zero or less is already caught on pallet weights. What we also need is to reject any pallet record above 1,500 kg."
    op = {"kind": "add_or_change_condition", "rule_id": "rule-pallet", "candidate_rule_ids": [], "desired": observed,
          "user_quote": "What we also need is to reject any pallet record above 1,500 kg."}
    out = _resolve(pallet, req, [op], kept=[{"quote": "Zero or less is already caught on pallet weights.", "rule_id": "rule-pallet"}])
    assert out["status"] == "READY" and {"rule_id": "rule-pallet", "from": "condition_words", "to": "add_or_change_condition"} in out["normalized_operations"]


def test_the_read_back_takes_the_prompt_op_codes_symbols_and_field_ids_but_nothing_vague():
    vet = {"condition": {"field": "patient.weight_kg", "op": "lte", "value": 0, "unit": "kg"}}
    ch = {"condition": {"field": "charter.passengers", "op": "lte", "value": 0}}
    assert B._words_to_conditions({"any": ["patient weight kg gt 120"]}, vet)[0]["any"][0] == {"field": "patient.weight_kg", "op": "gt", "value": 120, "unit": "kg"}   # DIAG5
    assert B._words_to_conditions({"any": ["charter.passengers more than 12"]}, ch)[0]["any"][0] == {"field": "charter.passengers", "op": "gt", "value": 12}
    assert B._words_to_conditions({"any": ["charter passengers >12"]}, ch)[1] is True
    for vague in ("charter passengers is heavy", "boat passengers more than 12", "charter passengers gte12x"):
        assert B._words_to_conditions({"any": [vague]}, ch)[1] is False


def test_a_count_word_on_a_unitless_field_is_read_and_a_cap_on_a_zero_or_less_rule_is_the_other_side():
    qty = _rule("rule-qty", "request.quantity - must be valid quantity", {"field": "request.quantity", "op": "lte", "value": 0})
    got, changed = B._words_to_conditions({"any": ["request quantity at most 0", "request quantity more than 240 units"]}, qty)        # SUR03-V9 01 form
    assert changed and got["any"][1] == {"field": "request.quantity", "op": "gt", "value": 240}
    assert B._words_to_conditions({"any": ["request quantity more than 240 kg"]}, qty)[1] is False                                 # not a count word
    req = "route_emergency: cap the quantity at 160 units. Anything beyond that is an invalid quantity."                         # SUR03-V9 item 05
    op = {"kind": "set_value", "rule_id": "rule-qty", "candidate_rule_ids": [], "desired": {"value": 160, "unit": "units"}, "user_quote": "cap the quantity at 160 units"}
    assert _resolve(qty, req, [op])["rejected"][0]["reason"] == "desired_not_supported:limit_on_the_other_side"
    days = _rule("rule-days", "Turnaround is at most 5 days", {"field": "request.turnaround_days", "op": "lte", "value": 5, "unit": "days"})
    same = "Turnaround should be at most 7 days."                                                                                  # same side on a rule that is not zero-or-less
    op7 = {"kind": "set_value", "rule_id": "rule-days", "candidate_rule_ids": [], "desired": {"value": 7, "unit": "days"}, "user_quote": same}
    assert _resolve(days, same, [op7])["operations"][0]["desired"] == {"value": 7, "unit": "days"}


def test_after_understand_v10_synonyms_field_names_scope_words_actor_restatements_and_negative_thresholds():
    proof = {"condition": {"field": "proofing.room_temp_c", "op": "lt", "value": 24, "unit": "C"}}
    got, _ = B._words_to_conditions({"any": ["proofing room temp c less than 24 C", "proofing room temp c greater than 29.5 C"]}, proof)     # U10-05 raw
    assert got["any"][1] == {"field": "proofing.room_temp_c", "op": "gt", "value": 29.5, "unit": "C"}
    xlat = {"condition": {"field": "job.subject_area", "op": "in", "value": ["LEGAL", "MEDICAL"]}}
    assert B._rule_fields({"field": "request.subject_area", "op": "in", "value": ["LEGAL"]}, xlat)["field"] == "job.subject_area"           # U10-21 raw
    two = {"condition": {"all": [{"field": "a.subject_area", "op": "eq", "value": "X"}, {"field": "b.subject_area", "op": "eq", "value": "Y"}]}}
    assert B._rule_fields({"field": "c.subject_area", "op": "eq", "value": "Z"}, two)["field"] == "c.subject_area"                          # two matches: left as written
    plan = dict(_rule("rule-plan", "Payment plans longer than 12 months go to the lead", {"field": "plan.months", "op": "gt", "value": 12, "unit": "months"}),
                scope={"customer_type": ["RESIDENTIAL"]})
    req = "Small business accounts should get the same payment-plan rule as residential customers."
    op = {"kind": "set_scope", "rule_id": "rule-plan", "candidate_rule_ids": [], "desired": {"customer_type": ["RESIDENTIAL", "SMALL_BUSINESS"]}, "user_quote": req}
    assert _resolve(plan, req, [op])["operations"][0]["desired"] == {"customer_type": ["RESIDENTIAL", "SMALL_BUSINESS"]}                    # U10-12 raw
    fleet = dict(_rule("rule-fleet", "Service booking is due every 40,000 km", {"field": "truck.km_since_service", "op": "gte", "value": 40000, "unit": "km"}),
                 actor_ref="role-workshop-planner")
    req17 = "The service booking can wait until 50,000 km since the last service. The workshop planner still books it."
    assert B._kept_reason("The workshop planner still books it.", "rule-fleet", {"rule-fleet": fleet}, B._norm(req17), req17) is None        # U10-17
    snow = _rule("rule-snow", "Snowmaking runs at a wet-bulb of -2.5 C or colder", {"field": "air.wet_bulb_c", "op": "lte", "value": -2.5, "unit": "C"})
    req01 = "Let them run up to a wet-bulb temperature of -1.5 C instead of -2.5 C."
    op01 = {"kind": "set_value", "rule_id": "rule-snow", "candidate_rule_ids": [], "desired": {"value": -1.5, "unit": "C"}, "user_quote": req01}
    assert str(_resolve(snow, req01, [op01])["operations"][0]["desired"]["value"]) == "-1.5"                                                       # U10-01: not a zero check


def test_a_refused_operation_keeps_the_value_the_model_gave_when_it_is_plain_text():
    qty = _rule("rule-qty", "request.quantity - must be valid quantity", {"field": "request.quantity", "op": "lte", "value": 0})
    req = "Anything above 120 units should be rejected as an invalid quantity."
    op = {"kind": "add_or_change_condition", "rule_id": "rule-qty", "candidate_rule_ids": [], "desired": {"any": ["quantity way too large"]}, "user_quote": req}
    rej = _resolve(qty, req, [op])["rejected"][0]
    assert rej["desired_given"] == '{"any": ["quantity way too large"]}' and rej["desired_shape"] == "object{any:list[str]}"
    unsafe = _resolve(qty, req, [dict(op, desired="<script>x</script>")])["rejected"][0]
    assert "desired_given" not in unsafe                                                                                         # unsafe text is not kept


def test_after_sur03_v10_the_rule_id_stands_for_its_condition_and_stay_as_it_is_is_no_change():
    qty = dict(_rule("rule-qty", "request.quantity - must be valid quantity", {"field": "request.quantity", "op": "lte", "value": 0}))
    got, changed = B._words_to_conditions({"any": ["rule-qty", "request.quantity > 320"]}, qty)                                    # SUR03-V10 03 desired_given
    assert changed and got == {"any": [{"field": "request.quantity", "op": "lte", "value": 0}, {"field": "request.quantity", "op": "gt", "value": 320}]}
    assert B._words_to_conditions({"any": ["rule-other", "request.quantity > 320"]}, qty)[0]["any"][0] == "rule-other"            # another rule's id: not read
    rules = {"r": {"title": "request.net_amount_jpy - must be greater than or equal to", "actor_ref": "role-manager",
                   "condition": {"field": "request.net_amount_jpy", "op": "gte", "value": 1000000}}}
    req = B._norm("Could you move the manager sign-off point in route_emergency up to 1,230,000 JPY? Everything else in that function should stay as it is.")
    assert B._context_reason("Everything else in that function should stay as it is.", rules, req) is None                       # SUR03-V10 01
    req2 = B._norm("Raise it. Everything else should change to the director.")
    assert B._context_reason("Everything else should change to the director.", rules, req2) == "may_state_a_change"
    disguised = B._norm("Raise it; high-risk suppliers should stay with Finance.")
    assert B._context_reason("high-risk suppliers should stay with Finance.", rules, disguised) == "may_state_a_change"           # not a no-change phrase


def test_contract_6_rule_lines_show_the_condition_json_and_a_signal_free_unlabelled_part_is_a_shown_remark():
    import json
    import pathlib
    frozen = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json").read_text(encoding="utf-8"))
    item = {"item_id": "rule-plan", "authority": "OBSERVED", "status": "VALIDATED", "text": "Plans | plan months more than 12 months",
            "condition": {"field": "plan.months", "op": "gt", "value": 12, "unit": "months"}}
    prompt = B.build_prompt(frozen, "Make it 18 months.", [item], max_chars=4000)
    assert 'condition: {"field": "plan.months", "op": "gt", "value": 12, "unit": "months"}' in prompt and "never write a condition in words" in prompt
    hours = _rule("rule-quiet", "Amplified sound stops at 19", {"field": "floor.quiet_from_hour", "op": "gte", "value": 19, "unit": "hour"})
    req = "Please make the rule say 18. Nobody has complained about it."
    op = {"kind": "set_value", "rule_id": "rule-quiet", "candidate_rule_ids": [], "desired": {"value": 18, "unit": "hour"}, "user_quote": "Please make the rule say 18"}
    out = _resolve(hours, req, [op])
    assert out["status"] == "READY" and {"quote": "Nobody has complained about it", "from": "unlabelled", "to": "context"} in out["relabelled"]
    req2 = "Please make the rule say 18. The front desk decides from now on."
    assert _resolve(hours, req2, [op])["status"] == "CLARIFY"                                                                     # a part with a change signal is still asked
    assert -120.0 in B.numbers_in("Let members go down to minus 120 pounds")
    refund = _rule("rule-refund", "Refunds need approval", {"field": "refund.amount_jpy", "op": "gte", "value": 20000, "unit": "JPY"})
    q = "the 20,000 yen line stays where it is"
    full = "Refund approvals should move to the clinic owner; the 20,000 yen line stays where it is."
    assert B._kept_reason(q, "rule-refund", {"rule-refund": refund}, B._norm(full), full) is None                                 # U11-20


def test_decimals_do_not_split_parts_dates_are_not_values_and_list_membership_is_read_back():
    assert B._segments("Loads under 7.5% moisture should be turned away too. Readings that low mean a faulty meter.") == [
        "Loads under 7.5% moisture should be turned away too", "Readings that low mean a faulty meter"]                         # U11-17: no part lost
    assert B._segments("Raise it to 1,500,000. Change the owner to finance.") == ["Raise it to 1,500,000", "Change the owner to finance"]
    assert B._context_reason("Ops sync, 14 Oct - actions for the rulebook:", {}, B._norm("Ops sync, 14 Oct - actions for the rulebook: x")) is None   # U11-08
    assert B._context_reason("Pay 300 more", {}, B._norm("Pay 300 more")) == "has_a_number"
    gear = {"rule_id": "rule-gear", "condition": {"field": "item.category", "op": "in", "value": ["drone", "gimbal"]}}
    got, changed = B._words_to_conditions({"any": ["item category is one of ['drone', 'gimbal']", "item category is one of ['anamorphic lens kit']"]}, gear)
    assert changed and got["any"][1] == {"field": "item.category", "op": "in", "value": ["anamorphic lens kit"]}                     # U11-02 raw form
    crop = {"rule_id": "rule-crop", "condition": {"field": "load.crop", "op": "in", "value": ["MAIZE"]}}
    assert B._words_to_conditions("load crop is one of ['maize', 'barley']", crop)[0] == {"field": "load.crop", "op": "in", "value": ["MAIZE", "BARLEY"]}
    assert B._words_to_conditions("trailer type is one of ['flatbed']", crop)[1] is False                                         # not this rule's field


def test_contract_7_a_condition_edit_takes_the_typed_condition_field_and_older_answers_without_it_still_read():
    import json
    import pathlib
    limits = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json").read_text(encoding="utf-8"))["limits"]
    qty = _rule("rule-qty", "request.quantity - must be valid quantity", {"field": "request.quantity", "op": "lte", "value": 0})
    req = "Emergency orders for more than 210 units should be rejected as an invalid quantity."                                   # SUR03-V11 03
    cond = {"any": [{"field": "request.quantity", "op": "lte", "value": 0}, {"field": "request.quantity", "op": "gt", "value": 210}], "field": None, "op": None}
    op = {"kind": "add_or_change_condition", "rule_id": "rule-qty", "candidate_rule_ids": [], "desired": None, "condition": cond, "user_quote": req}
    obj = {"status": "READY", "message": "Ready.", "operations": [op], "questions": [], "assumptions": [], "kept_as_is": [], "context": []}
    B.validate_model_object(obj, limits)
    out = B.resolve(obj, req, {"rule-qty": qty}, [], limits, pin={"snapshot_id": "s"})
    assert out["status"] == "READY" and out["operations"][0]["desired"]["any"][1] == {"field": "request.quantity", "op": "gt", "value": 210}
    assert {"rule_id": "rule-qty", "from": "condition_field", "to": "add_or_change_condition"} in out["normalized_operations"]
    old = dict(op, desired=cond["any"][1])
    old.pop("condition")
    B.validate_model_object(dict(obj, operations=[old]), limits)                                                                 # an answer without the key still reads
    frozen = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json").read_text(encoding="utf-8"))
    assert "condition" in frozen["output_schema"]["properties"]["operations"]["items"]["properties"] and frozen["version"] >= 7


def test_a_decimal_in_a_typed_condition_is_kept_as_the_contract_decimal_string():
    moist = _rule("rule-moist", "Loads above 20% moisture are turned away", {"field": "load.moisture_pct", "op": "gt", "value": 20, "unit": "%"})
    req = "Loads under 7.5% moisture should be turned away too."
    cond = {"any": [{"field": "load.moisture_pct", "op": "gt", "value": 20, "unit": "%"}, {"field": "load.moisture_pct", "op": "lt", "value": 7.5, "unit": "%"}]}  # C7 smoke
    op = {"kind": "add_or_change_condition", "rule_id": "rule-moist", "candidate_rule_ids": [], "desired": None, "condition": cond, "user_quote": req}
    out = _resolve(moist, req, [op])
    assert out["status"] == "READY" and out["operations"][0]["desired"]["any"][1]["value"] == "7.5"
    assert B._decimals({"value": 3000.0}) == {"value": 3000} and B._decimals({"value": True}) == {"value": True}


def test_a_value_change_on_a_rule_whose_number_has_no_unit_is_accepted():
    pets = _rule("rule-pets", "Passengers travelling with more than 2 pets need the Purser's approval", {"field": "booking.pets", "op": "gt", "value": 2})
    req = "Passengers travelling with more than 3 pets need the purser's approval."                                              # GUIDANCE-CAND V8-G-11C
    op = {"kind": "set_value", "rule_id": "rule-pets", "candidate_rule_ids": [], "desired": {"value": 3}, "user_quote": req}
    out = _resolve(pets, req, [op])
    assert out["status"] == "READY" and out["operations"][0]["desired"] == {"value": 3} and out["descriptions"][0].endswith("to 3.")


def test_degrees_on_a_rule_kept_in_celsius_is_the_rule_scale():
    alarm = _rule("rule-alarm", "The cold room alarm sounds below -1 C", {"field": "room.temp_c", "op": "lt", "value": -1, "unit": "C"})
    req = "The cold room alarm should go off below minus 1.5 degrees, not minus 1."                                              # UNDERSTAND-V12-PF U12-19
    op = {"kind": "set_value", "rule_id": "rule-alarm", "candidate_rule_ids": [], "desired": {"value": -1.5, "unit": "degrees"}, "user_quote": req}
    out = _resolve(alarm, req, [op])
    assert out["status"] == "READY" and out["operations"][0]["desired"] == {"value": "-1.5", "unit": "C"}
    yen = _rule("rule-yen", "Refunds from 20,000 JPY", {"field": "refund.amount_jpy", "op": "gte", "value": 20000, "unit": "JPY"})
    bad = {"kind": "set_value", "rule_id": "rule-yen", "candidate_rule_ids": [], "desired": {"value": 25000, "unit": "degrees"}, "user_quote": "Refunds from 25,000 degrees."}
    assert _resolve(yen, "Refunds from 25,000 degrees.", [bad])["rejected"][0]["reason"] == "desired_not_supported:unit_differs_from_the_rule"


# ---- contract 8: server-numbered parts, model classification, server grounding (2026-10-24, after UNDERSTAND-V9..V12) -----------------------------------------

def _c8(rule, request, operations, parts=(), others=()):
    import json
    import pathlib
    limits = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json").read_text(encoding="utf-8"))["limits"]
    obj = {"status": "READY", "message": "Ready.", "operations": list(operations), "questions": [], "assumptions": [], "parts": list(parts)}
    B.validate_model_object(obj, limits)
    return B.resolve(obj, request, {r["rule_id"]: r for r in [rule, *others]}, [], limits, pin={"snapshot_id": "s"})


def test_contract_8_every_part_is_accounted_for_and_grounded_remarks_do_not_hold_a_ready_change():
    rent = _rule("rule-inspect", "A property is inspected when 6 months have passed since the last inspection", {"field": "tenancy.months_since_inspection", "op": "gte", "value": 6, "unit": "months"})
    req = "The six-monthly inspection should only be needed where the monthly rent is 900 EUR or more. Cheaper flats get checked at check-out."   # U12-13
    assert list(B.request_parts(req)) == ["P1", "P2"]
    cond = {"all": [{"field": "tenancy.months_since_inspection", "op": "gte", "value": 6, "unit": "months"}, {"field": "tenancy.monthly_rent_eur", "op": "gte", "value": 900, "unit": "EUR"}]}
    op = {"kind": "add_or_change_condition", "rule_id": "rule-inspect", "candidate_rule_ids": [], "desired": None, "condition": cond, "parts": ["P1"],
          "user_quote": "The six-monthly inspection should only be needed where the monthly rent is 900 EUR or more"}
    out = _c8(rent, req, [op], parts=[{"part": "P2", "kind": "aside", "rule_id": None}])
    assert out["status"] == "READY" and out["parts_read"] == [{"part": "P2", "quote": "Cheaper flats get checked at check-out", "kind": "aside", "rule_id": None}]
    assert out["context"] == ["Cheaper flats get checked at check-out"] and out["uncovered_parts"] == []
    missing = _c8(rent, req, [op], parts=[])                                                                                  # a part nobody accounted for is asked
    assert missing["status"] == "CLARIFY" and missing["uncovered_parts"] == ["Cheaper flats get checked at check-out"]
    unclear = _c8(rent, req, [op], parts=[{"part": "P2", "kind": "unclear", "rule_id": None}])                                # the model's own doubt is asked
    assert unclear["status"] == "CLARIFY" and unclear["part_issues"][0]["reason"] == "model_unclear"


def test_contract_8_grounding_numbers_and_actors_not_word_lists():
    repair = _rule("rule-repair", "Repairs estimated above 750 EUR need the landlord's written approval", {"field": "repair.estimate_eur", "op": "gt", "value": 750, "unit": "EUR"})
    req = "The handbook says repairs over 750 EUR need the landlord's sign-off, but most of our landlords agreed to 1,000 EUR in their management contracts. Make 1,000 EUR the rule."  # U12-11
    labels = B.request_parts(req)
    change = next(k for k, v in labels.items() if v.startswith("Make 1,000"))
    rest = [k for k in labels if k != change]
    op = {"kind": "set_value", "rule_id": "rule-repair", "candidate_rule_ids": [], "desired": {"value": 1000, "unit": "EUR"}, "parts": [change], "user_quote": "Make 1,000 EUR the rule"}
    parts = [{"part": rest[0], "kind": "current_state", "rule_id": "rule-repair"}] + [{"part": k, "kind": "reason", "rule_id": "rule-repair"} for k in rest[1:]]
    out = _c8(repair, req, [op], parts=parts)
    assert out["status"] == "READY", (out["part_issues"], out["uncovered_parts"])                   # 750 = the rule's value, 1,000 = the value this request sets
    comps = dict(_rule("rule-comps", "More than 4 complimentary tickets for one performance need the theatre manager's approval", {"field": "comps.count_per_show", "op": "gt", "value": 4}),
                 actor_ref="role-theatre-manager")
    req2 = "At the moment the theatre manager signs off any comps above four per show. She's only in two days a week, so the front-of-house manager should take that over."  # U12-08
    labels2 = B.request_parts(req2)
    change2 = [k for k, v in labels2.items() if "front-of-house manager should" in v]
    op2 = {"kind": "set_actor", "rule_id": "rule-comps", "candidate_rule_ids": [], "desired": "front-of-house manager", "parts": change2,
           "user_quote": "the front-of-house manager should take that over"}
    parts2 = [{"part": k, "kind": "current_state" if v.startswith("At the moment") else "reason", "rule_id": "rule-comps"} for k, v in labels2.items() if k not in change2]
    out2 = _c8(comps, req2, [op2], parts=parts2)
    assert out2["status"] == "READY", (out2["part_issues"], out2["uncovered_parts"], out2["rejected"])
    stray = _c8(repair, "Make 1,000 EUR the rule. The 2,500 EUR cases are another matter.", [dict(op, parts=["P1"])], parts=[{"part": "P2", "kind": "aside", "rule_id": None}])
    assert stray["status"] == "CLARIFY" and stray["part_issues"][0]["reason"] == "number_not_grounded"           # an unexplained number cannot hide in a remark
    other = dict(_rule("rule-till", "A till variance above 20 GBP is reported to the front-of-house manager", {"field": "till.variance_gbp", "op": "gt", "value": 20, "unit": "GBP"}), actor_ref="role-foh-manager")
    op3 = {"kind": "set_value", "rule_id": "rule-comps", "candidate_rule_ids": [], "desired": {"value": 6}, "parts": ["P1"], "user_quote": "Make 6 the comps limit"}
    sneaky = _c8(comps, "Make 6 the comps limit. The theatre manager now signs off till variances too.", [op3], parts=[{"part": "P2", "kind": "current_state", "rule_id": "rule-till"}], others=[other])
    assert sneaky["status"] == "CLARIFY" and sneaky["part_issues"][0]["reason"] == "actor_not_grounded"           # another rule's actor named as the current state of this one


def test_contract_8_an_indistinguishable_sibling_rule_is_asked_about_and_a_vague_actor_is_refused():
    senior = dict(_rule("rule-senior", "Patrons aged 65 or over get the senior rate", {"field": "customer.age", "op": "gte", "value": 65}), actor_ref="role-box-office-assistant", outcome={"kind": "senior_rate", "predicate": ""})
    matinee = dict(_rule("rule-matinee", "At matinees patrons aged 60 or over get the senior rate", {"all": [{"field": "customer.age", "op": "gte", "value": 60}, {"field": "show.slot", "op": "eq", "value": "matinee"}]}),
                   actor_ref="role-box-office-assistant", outcome={"kind": "senior_rate", "predicate": ""})
    req = "Can we lower the senior age to 62? Quite a few of our regulars are just under the line."                                                      # U12-07
    op = {"kind": "set_value", "rule_id": "rule-senior", "candidate_rule_ids": [], "desired": {"value": 62}, "parts": ["P1"], "user_quote": "Can we lower the senior age to 62"}
    out = _c8(senior, req, [op], parts=[{"part": "P2", "kind": "reason", "rule_id": "rule-senior"}], others=[matinee])
    assert out["status"] == "CLARIFY" and out["rejected"][0]["reason"] == "ambiguous_rule" and out["rejected"][0]["candidate_rule_ids"] == ["rule-senior", "rule-matinee"]
    assert any("Which one do you mean" in q for q in out["questions"])
    named = _c8(senior, "Lower the matinee senior age to 58.", [dict(op, rule_id="rule-matinee", desired={"value": 58}, user_quote="Lower the matinee senior age to 58")], others=[matinee])
    assert named["status"] == "READY"                                                                                              # "matinee" names one of them
    alone = _c8(senior, "Lower the senior age to 62.", [dict(op, user_quote="Lower the senior age to 62")])
    assert alone["status"] == "READY"
    group = dict(_rule("rule-group", "Bookings of 10 or more tickets get the group rate", {"field": "order.ticket_count", "op": "gte", "value": 10}), actor_ref="role-box-office-assistant")
    req9 = "Before the group rate goes on a booking of ten or more, somebody should check that it is genuine."                                             # U12-09
    vague = {"kind": "set_actor", "rule_id": "rule-group", "candidate_rule_ids": [], "desired": "somebody", "parts": ["P1"], "user_quote": "somebody should check that it is genuine"}
    out9 = _c8(group, req9, [vague])
    assert out9["status"] == "CLARIFY" and out9["rejected"][0]["reason"] == "desired_not_supported:actor_not_named"


def test_contract_8_units_are_compared_by_class():
    alarm = _rule("rule-alarm", "The cold room alarm sounds below -1 C", {"field": "room.temp_c", "op": "lt", "value": -1, "unit": "C"})
    req = "The cold room alarm should go off below minus 1.5 degrees, not minus 1."
    op = {"kind": "set_value", "rule_id": "rule-alarm", "candidate_rule_ids": [], "desired": {"value": -1.5, "unit": "degrees"}, "parts": ["P1"], "user_quote": req}
    assert _c8(alarm, req, [op])["operations"][0]["desired"] == {"value": "-1.5", "unit": "C"}
    hours = _rule("rule-room", "Rooms can be booked for 4 hours at most", {"field": "booking.hours", "op": "lte", "value": 4, "unit": "hours"})
    op2 = {"kind": "set_value", "rule_id": "rule-room", "candidate_rule_ids": [], "desired": {"value": 5, "unit": "Hour"}, "parts": ["P1"], "user_quote": "group study rooms can be booked for 5 hours at most"}
    assert _c8(hours, "group study rooms can be booked for 5 hours at most", [op2])["operations"][0]["desired"] == {"value": 5, "unit": "hours"}


def test_a_typed_composite_condition_with_stray_leaf_keys_is_read_as_the_composite():
    export = _rule("rule-export", "Only Braeburn, Jazz and Envy apples go to the export line", {"field": "fruit.variety", "op": "in", "value": ["braeburn", "jazz", "envy"]})
    req = "Gala passed the trial, so it can go on the export line along with the other three varieties."
    cond = {"field": None, "op": "in", "value": ["braeburn", "jazz", "envy", "gala"], "any": [{"field": "fruit.variety", "op": "in", "value": ["braeburn", "jazz", "envy", "gala"]}]}   # U12-21C8 raw
    op = {"kind": "add_or_change_condition", "rule_id": "rule-export", "candidate_rule_ids": [], "desired": None, "condition": cond, "parts": ["P1", "P2"], "user_quote": req}
    out = _c8(export, req, [op])
    assert out["status"] == "READY" and out["operations"][0]["desired"] == {"any": [{"field": "fruit.variety", "op": "in", "value": ["braeburn", "jazz", "envy", "gala"]}]}


def test_a_current_state_part_naming_no_rule_is_an_aside_when_there_is_nothing_to_ground():
    qty = _rule("rule-qty", "request.quantity - must be valid quantity", {"field": "request.quantity", "op": "lte", "value": 0})
    req = "In route_request, an order larger than 5,700 units is an invalid quantity from now on; the rest of standard routing is untouched."    # SUR03-V13 05
    cond = {"any": [{"field": "request.quantity", "op": "lte", "value": 0}, {"field": "request.quantity", "op": "gt", "value": 5700}]}
    op = {"kind": "add_or_change_condition", "rule_id": "rule-qty", "candidate_rule_ids": [], "desired": None, "condition": cond, "parts": ["P1"], "user_quote": "an order larger than 5,700 units is an invalid quantity"}
    out = _c8(qty, req, [op], parts=[{"part": "P2", "kind": "current_state", "rule_id": None}])
    assert out["status"] == "READY" and out["context"] == ["the rest of standard routing is untouched"]
    numbered = _c8(qty, "Raise it to 5,700. The 300 limit stays as it is.", [dict(op, user_quote="Raise it to 5,700")], parts=[{"part": "P2", "kind": "current_state", "rule_id": None}])
    assert numbered["status"] == "CLARIFY" and numbered["part_issues"][0]["reason"] == "rule_not_offered"                        # a number needs its rule


def test_after_understand_v13_indefinite_actors_unit_qualifiers_and_attempted_values():
    assert B._vague_actor("role-someone-senior") and B._vague_actor("role-somebody-from-finance") and not B._vague_actor("role-senior-studio-manager")     # U13-10
    assert B._unit_key("working days", "day") == "day" and B._unit_key("calendar days", "days") == "days" and B._unit_key("days", "working days") == "days"   # plain days against a qualified unit: refused
    assert B._unit_key("lb", "kg") == "lb"
    copy = _rule("rule-copy", "Ad copy received fewer than 2 working days before the first airing is held", {"field": "copy.working_days_before", "op": "lt", "value": 2, "unit": "day"})
    req = "The handbook still says 2 working days for ad copy, but traffic has been holding anything under 3 working days for months. Make the rule 3."     # U13-20
    labels = B.request_parts(req)
    change = [k for k, v in labels.items() if v.startswith("Make the rule")]
    op = {"kind": "set_value", "rule_id": "rule-copy", "candidate_rule_ids": [], "desired": {"value": 3, "unit": "working days"}, "parts": change, "user_quote": "Make the rule 3"}
    parts = [{"part": k, "kind": "current_state" if v.startswith("The handbook") else "reason", "rule_id": "rule-copy"} for k, v in labels.items() if k not in change]
    out = _c8(copy, req, [op], parts=parts)
    assert out["status"] == "READY", (out["rejected"], out["part_issues"])
    assert out["operations"][0]["desired"] == {"value": 3, "unit": "day"}
    general = dict(_rule("rule-general", "Advertising discounts of 20 percent or more are approved by the sales director", {"field": "order.discount_percent", "op": "gte", "value": 20, "unit": "percent"}),
                   actor_ref="role-sales-director", outcome={"kind": "approval_required", "predicate": ""})
    agency = dict(_rule("rule-agency", "Agency orders: discounts of 35 percent or more are approved by the sales director", {"field": "order.discount_percent", "op": "gte", "value": 35, "unit": "percent"}),
                  actor_ref="role-sales-director", outcome={"kind": "approval_required", "predicate": ""})
    derived = [{"edge_id": "e1", "type": "conflicts_with", "from": "rule-general", "to": "rule-agency", "status": "PROPOSED", "provenance_refs": []}]
    req16 = "Discount approval by the sales director should start at 25 percent."                                                                  # U13-16
    assert B._ambiguous_target("rule-general", {"rule-general": general, "rule-agency": agency}, B._norm(req16), None, derived) == ["rule-agency"]  # a derived edge settles nothing
    assert B._ambiguous_target("rule-general", {"rule-general": general, "rule-agency": agency}, B._norm(req16), None, [dict(derived[0], status="CONFIRMED")]) == []
