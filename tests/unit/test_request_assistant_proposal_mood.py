"""checks/14: a block citing only the proposal must say it is proposed (live finding RQA-V10-20261023-01, P1)."""
from core.change_intelligence.v2 import request_assistant as ra

TITLE = {"id": "F2", "kind": "request", "key": "request:title", "text": "The request: Purchases from high-risk suppliers should be approved by the Finance Controller instead of the Operations Director."}
DRAFT = {"id": "F17", "kind": "rule_change", "key": "rule_change:draft:000", "text": "Before: actor: director | when: supplier risk is HIGH | After: actor: finance controller | when: supplier risk is HIGH"}
SCOPE = {"id": "F18", "kind": "rule_change", "key": "rule_change:scope:Purchases from high-risk suppliers go to the Operations Director regardless of amount",
         "text": "Rule to change: Purchases from high-risk suppliers go to the Operations Director regardless of amount"}
STATE = {"id": "F1", "kind": "state", "key": "state", "text": "Waiting for a knowledge decision"}


def test_the_v10_p1_sentence_is_refused():
    assert ra.proposal_stated_as_in_effect("Purchases from high-risk suppliers are routed to the Finance Controller instead of the Operations Director", [TITLE, DRAFT])


def test_proposal_sentences_of_the_same_run_are_kept():
    for text, cited in [("The proposed change modifies high-risk supplier approvals to the Finance Controller", [TITLE, DRAFT]),
                        ("High-risk supplier purchases are proposed to be approved by the Finance Controller instead of the Operations Director.", [TITLE, DRAFT]),
                        ("The request changes the manager approval limit from 1,000,000 to 1,500,000 JPY.", [TITLE]),
                        ("Changes are requested to raise the free freight threshold from 500,000 to 600,000 JPY.", [TITLE]),
                        ("The free freight threshold is proposed to be raised from 500,000 to 600,000 JPY.", [DRAFT])]:
        assert not ra.proposal_stated_as_in_effect(text, cited), text


def test_a_block_that_also_cites_the_current_rule_or_the_state_is_not_affected():
    assert not ra.proposal_stated_as_in_effect("Purchases from high-risk suppliers are routed to the Operations Director regardless of amount", [TITLE, SCOPE])
    assert not ra.proposal_stated_as_in_effect("The workflow is waiting for a knowledge decision", [STATE])
    assert not ra.proposal_stated_as_in_effect("anything", [])


def test_check_answer_refuses_the_block_and_the_answer_is_not_a_success():
    p = {"facts": [TITLE, DRAFT, SCOPE, STATE], "coverage": {}}
    model = {"outcome": "answered", "limitations": [], "authority_assertions": [],
             "answer_blocks": [{"kind": "fact", "text": "Purchases from high-risk suppliers are routed to the Finance Controller instead of the Operations Director", "fact_refs": ["F2", "F17"]}]}
    out = ra.check_answer(model, p, {"max_block_chars": 600, "max_limitations": 3})
    assert [r["reason"] for r in out["rejected"]] == ["proposal_stated_as_in_effect"] and out["status"] == "refused"
    assert not any(b["kind"] == "fact" for b in out["blocks"])
