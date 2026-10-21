"""Local rehearsal stub for change understanding (tools/judge_local/sim_final.py `rule_understanding`, F-ACC-3 2026-10-21): rules, not a model, labelled SIMULATED.
It must give a real success, a purposeful clarification with one concrete next sentence, and a refusal; never the old endless "more detail" loop. The prompt is
built by the REAL builder over the curated Harbor rules, and every answer passes the role's REAL validator."""
from __future__ import annotations

import json
import pathlib

from core.change_intelligence.v2 import business_change as bc
from core.change_intelligence.v2 import business_index as bi
from tests.final_loop.test_qry02_retrieval import KNOWLEDGE
from tools.judge_local.sim_final import rule_understanding

FROZEN = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "change_understand.json").read_text(encoding="utf-8"))
ITEMS = [i for i in bi.build_items(KNOWLEDGE["observed_rules"], [], []) if i["kind"] == "rule"]


def read(request: str) -> dict:
    out = rule_understanding(bc.build_prompt(FROZEN, request, ITEMS, max_chars=FROZEN["limits"]["max_prompt_chars"]))
    bc.validate_model_object(json.loads(json.dumps(out)), FROZEN["limits"])                    # schema-valid for the role, whatever the branch
    assert "SIMULATED" in out["message"]
    return out


def rule_of(text_part: str) -> str:
    return next(i["item_id"] for i in ITEMS if text_part in i["text"])


def test_a_change_of_one_number_on_one_rule_is_ready_with_numbers_copied_from_the_request():
    manager = rule_of("Purchases of 1,000,000 JPY or more need manager approval")
    for request in ("Raise the manager approval threshold to 2,500,000 JPY.", "Change the manager approval threshold from 1,000,000 to 2,500,000 JPY."):
        out = read(request)
        assert out["status"] == "READY" and [(o["rule_id"], o["desired"]) for o in out["operations"]] == [(manager, {"value": 2500000})], request
    capex_policy = rule_of("5,000,000")
    out = read("The 5,000,000 JPY Finance Controller figure in the written policy should become 6,000,000 JPY.")          # the old number names the rule
    assert out["status"] == "READY" and out["operations"][0]["rule_id"] == capex_policy and out["operations"][0]["desired"] == {"value": 6000000}


def test_clarification_names_the_candidates_or_asks_only_for_the_missing_value():
    many = read("Raise the CAPEX Finance threshold to 3,000,000 JPY.")                                                     # config and policy rules both match
    assert many["status"] == "CLARIFY" and many["operations"] == [] and len(many["questions"]) == 1 and " or " in many["questions"][0]
    no_value = read("Raise the manager approval threshold.")
    assert no_value["status"] == "CLARIFY" and "new value" in no_value["questions"][0] and "manager approval" in no_value["questions"][0]


def test_anything_else_is_refused_once_with_what_the_stub_can_do():
    for request in ("Let the Operations Director also approve emergency purchases.", "Write a poem about procurement."):
        out = read(request)
        assert out["status"] == "UNSUPPORTED" and out["operations"] == [] and out["questions"] == [] and "one number" in out["message"], request
