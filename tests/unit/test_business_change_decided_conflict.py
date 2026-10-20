"""Change understanding around a DECIDED conflict (pure). Invariant: a request to edit the losing observed rule of a conflict a person already decided is not a reversal of
that decision; the server asks whether the person means to correct the observed record, to bring the source text in line with the decided rule, or to propose changing the
decision, and never remaps the request or changes authority silently. Diagnostic finding H-U-03 (2026-10-17, non-independent): the model read such a request as READY on the
observed policy rule. The evaluation label of H-U-03 is unchanged; this test protects the invariant, not the label."""
import json
import pathlib

from core.change_intelligence.v2 import business_change as bc

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "final_knowledge_loop"
LIMITS = json.loads((ROOT / "config" / "change_understand.json").read_text(encoding="utf-8"))["limits"]
OBSERVED = {r["rule_id"]: r for r in json.loads((FIX / "expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"]}
TARGET = dict(json.loads((FIX / "contract_examples.json").read_text(encoding="utf-8"))["target_rule"], rule_id="rule-capex-finance-config-target")
RULES = {**OBSERVED, TARGET["rule_id"]: TARGET}
DECIDED = [{"type": "conflicts_with", "from": "rule-capex-finance-config", "to": "rule-capex-finance-policy", "status": "CONFIRMED"}]
PIN = {"snapshot_id": "accepted-test"}
REQUEST = "Change the CAPEX Finance threshold written in the procurement policy from 5,000,000 to 4,000,000 JPY."


def model(rule_id, value, quote=REQUEST):
    return {"status": "READY", "message": "Ready.", "operations": [{"kind": "set_value", "rule_id": rule_id, "candidate_rule_ids": [], "desired": {"value": value, "unit": "JPY"},
                                                                     "user_quote": quote}], "questions": [], "assumptions": []}


def test_editing_the_losing_rule_of_a_decided_conflict_asks_which_change_is_meant_and_changes_nothing():
    out = bc.resolve(model("rule-capex-finance-policy", 4000000), REQUEST, RULES, DECIDED, LIMITS, pin=PIN)
    assert out["status"] == "CLARIFY" and out["operations"] == [] and out["write_scope"] == []
    assert out["rejected"][0]["reason"] == "observed_rule_lost_a_decided_conflict"
    assert out["rejected"][0]["candidate_rule_ids"] == ["rule-capex-finance-policy", "rule-capex-finance-config-target"]
    q = out["questions"][0]
    assert "decided" in q and "sources" in q and "decision" in q                                     # plain words; the exact sentence is not fixed (owner, 2026-10-18)
    choice = out["decided_conflict_choices"][0]
    assert choice["rule_id"] == "rule-capex-finance-policy" and choice["decided_rule_id"] == "rule-capex-finance-config-target"
    assert [o["intent"] for o in choice["options"]] == ["fix_observed_record", "sync_source_to_decision", "change_decision"]
    assert [o["available"] for o in choice["options"]] == [False, False, True] and all(o["reason"].startswith("needs_human") for o in choice["options"][:2])


def test_without_a_decision_the_same_edit_stays_an_ordinary_change_of_the_observed_rule():
    rules = dict(OBSERVED)                                                                                              # no decided version exists
    out = bc.resolve(model("rule-capex-finance-policy", 4000000), REQUEST, rules, DECIDED, LIMITS, pin=PIN)
    assert out["status"] == "READY" and out["operations"][0]["rule_id"] == "rule-capex-finance-policy"


def test_a_rejected_conflict_edge_is_not_a_decided_conflict():
    out = bc.resolve(model("rule-capex-finance-policy", 4000000), REQUEST, RULES, [dict(DECIDED[0], status="REJECTED")], LIMITS, pin=PIN)
    assert out["status"] == "READY" and out["operations"][0]["rule_id"] == "rule-capex-finance-policy"


def test_the_decided_rule_itself_and_the_observed_winner_keep_their_existing_handling():
    quote = "Change the CAPEX Finance threshold from 2,000,000 to 4,000,000 JPY."
    target = bc.resolve(model("rule-capex-finance-config-target", 4000000, quote), quote, RULES, DECIDED, LIMITS, pin=PIN)
    assert target["status"] == "READY" and target["operations"][0]["rule_id"] == "rule-capex-finance-config-target"         # a change of the decided rule is ordinary
    winner = bc.resolve(model("rule-capex-finance-config", 4000000, quote), quote, RULES, DECIDED, LIMITS, pin=PIN)
    assert winner["rejected"][0]["reason"] == "observed_rule_has_decided_version"                                            # the existing which-version question


# ---- owner decision H-U-03 (2026-10-18): the person's choice decides; nothing is retargeted without it -----------------------------------------------------------
def test_choosing_to_change_the_decision_starts_an_ordinary_change_of_the_decided_rule():
    out = bc.resolve(model("rule-capex-finance-policy", 4000000), REQUEST, RULES, DECIDED, LIMITS, pin=PIN, choices={"rule-capex-finance-policy": "change_decision"})
    assert out["status"] == "READY" and [o["rule_id"] for o in out["operations"]] == ["rule-capex-finance-config-target"]
    assert out["operations"][0]["desired"]["value"] == 4000000 and out["retargeted"] == [{"from": "rule-capex-finance-policy", "to": "rule-capex-finance-config-target", "intent": "change_decision"}]
    assert out["write_scope"] == ["rule-capex-finance-config-target"] and out["decided_conflict_choices"] == []


def test_the_two_source_intents_are_handed_to_a_person_with_the_reason_and_change_nothing():
    for intent, words in (("fix_observed_record", "follows the sources"), ("sync_source_to_decision", "no file proposal")):
        out = bc.resolve(model("rule-capex-finance-policy", 4000000), REQUEST, RULES, DECIDED, LIMITS, pin=PIN, choices={"rule-capex-finance-policy": intent})
        assert out["operations"] == [] and out["write_scope"] == [] and out["status"] == "UNSUPPORTED", intent
        assert out["refused"][0]["intent"] == intent and out["refused"][0]["reason"] == "needs_human" and words in out["refused"][0]["why"], intent


def test_a_choice_for_another_rule_or_an_unknown_intent_retargets_nothing():
    for choices in ({"rule-standard-manager-review": "change_decision"}, {"rule-capex-finance-policy": "flip_authority"}):
        out = bc.resolve(model("rule-capex-finance-policy", 4000000), REQUEST, RULES, DECIDED, LIMITS, pin=PIN, choices=choices)
        assert out["status"] == "CLARIFY" and out["operations"] == [] and out["retargeted"] == []

