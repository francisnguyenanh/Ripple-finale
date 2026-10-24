"""knowledge.mining contract version 5: the server computes which rule pairs can apply to ONE case (with a witness verified by the condition evaluator) and lists
them for the model; the text guard refuses markup and links but keeps comparators and field names. Pure functions."""
from core.change_intelligence.v2 import business_mining as bm


def rule(rid, cond, outcome, actor="role-finance"):
    return {"rule_id": rid, "title": rid, "condition": cond, "outcome": {"kind": outcome}, "actor_ref": actor, "scope": {}, "exceptions": [], "authority": "OBSERVED",
            "status": "VALIDATED"}


F = "rental.cost_jpy"
RULES = {
    "cap": rule("cap", {"field": F, "op": "gte", "value": 400000, "unit": "JPY"}, "rejected"),
    "review": rule("review", {"field": F, "op": "gt", "value": 250000, "unit": "JPY"}, "review_required", "role-fleet-manager"),
    "second": rule("second", {"all": [{"field": F, "op": "gte", "value": 100000, "unit": "JPY"}, {"field": F, "op": "lt", "value": 400000, "unit": "JPY"}]},
                   "review_required", "role-safety-officer"),
    "small": rule("small", {"field": F, "op": "lt", "value": 15000, "unit": "JPY"}, "allowed"),
}


def test_only_pairs_that_can_apply_to_one_case_are_listed_each_with_a_verified_case():
    found = {tuple(o["rule_ids"]): o["case"] for o in bm.overlaps(RULES)}
    assert set(found) == {("cap", "review"), ("review", "second")}                   # the boundary pair (cap / second) and the disjoint small range are not listed
    for (a, b), case in found.items():
        assert bm._applies(RULES[a], case)[0] is True and bm._applies(RULES[b], case)[0] is True
    assert found[("cap", "review")][F] >= 400000


def test_the_prompt_lists_the_overlaps_and_data_never_expands_a_placeholder():
    frozen, _sha = __import__("infra.worker.knowledge_mining", fromlist=["load_config"]).load_config()
    assert frozen["version"] == 5 and "{overlaps}" in frozen["prompt_template"]
    rules = [dict(r, title="Rental {overlaps} rule") if r["rule_id"] == "small" else r for r in RULES.values()]
    prompt = bm.build_prompt(frozen, rules, [], bm.allowed_fields(rules))
    assert "- cap and review both apply to the case rental.cost_jpy=" in prompt and "- review and second both apply" in prompt
    assert "cap and second" not in prompt and "Rental {overlaps} rule" in prompt


def test_the_text_guard_keeps_comparators_and_field_names_and_refuses_markup():
    assert not bm._UNSAFE.search("Rentals with rental.cost_jpy > 400,000 are rejected but need the fleet manager at >= 250,000")
    for bad in ("<b>x</b>", "see **this**", "[x](y)", "http://x.example", "`code`", "# heading"):
        assert bm._UNSAFE.search(bad), bad


def test_a_not_equal_condition_can_be_met_by_a_value_outside_the_literals():
    """The exception pack of mining_v5: director (amount >= 1,200,000 and kind not SAFETY_CRITICAL) and a report rule (amount >= 250,000) overlap for an ordinary repair."""
    rules = {"director": rule("director", {"all": [{"field": "yard.repair_jpy", "op": "gte", "value": 1200000}, {"field": "yard.repair_kind", "op": "ne", "value": "SAFETY_CRITICAL"}]},
                              "approval_required"),
             "safety": rule("safety", {"field": "yard.repair_kind", "op": "eq", "value": "SAFETY_CRITICAL"}, "approval_required", "role-yard-supervisor"),
             "report": rule("report", {"field": "yard.repair_jpy", "op": "gte", "value": 250000}, "report_required", "role-site-owner")}
    found = {tuple(o["rule_ids"]) for o in bm.overlaps(rules)}
    assert found == {("director", "report"), ("report", "safety")}                     # director / safety never apply together (the exception is written into both)
