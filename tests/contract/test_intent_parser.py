# -*- coding: utf-8 -*-
"""The five ambiguous statements, and the model that wants to help.

Every case in this file is a case where producing an answer would be worse
than producing a question. That makes it the easiest module in the product
to write badly and have it look like it works: a parser that guesses passes
a demo and fails the first time somebody types "change it to 500".
"""

from __future__ import annotations

import pytest

from core.change_intelligence.intent_parser import (
    INTENT_TYPES,
    UNKNOWN,
    MalformedProposal,
    build_prompt,
    extract,
    parse_proposal,
    split_requests,
)

TARGET = "approval_threshold"


def only(text, **kwargs):
    result = extract(text, target=TARGET, **kwargs)
    assert len(result.drafts) == 1, [d.source_text for d in result.drafts]
    return result.drafts[0], result


# --- the determinate case -------------------------------------------------------


def test_an_explicit_value_with_a_unit_is_extracted():
    draft, _ = only("Orders above JPY 500,000 should require manager approval.")
    assert draft.intent_type == "business_rule_change"
    assert draft.desired_value == 500000
    assert draft.unit == "JPY"
    assert draft.target == TARGET
    assert draft.is_actionable


def test_confidence_comes_with_a_basis_a_person_can_check():
    """A bare "high" is a claim about the model's feelings."""
    draft, _ = only("Set it to JPY 500,000.")
    assert draft.confidence == "high"
    assert "unit" in draft.confidence_basis


# --- the five ambiguous cases ------------------------------------------------


def test_a_direction_with_no_destination_abstains():
    """"It should be stricter." Abstaining is the correct answer here, not
    a failure -- and the question is the useful half of the output."""
    draft, result = only("It should be stricter.")
    assert draft.desired_value == UNKNOWN
    assert draft.intent_type == "unknown"
    assert not draft.is_actionable
    assert result.questions
    assert "value" in result.questions[0].text.lower()


def test_a_number_with_no_unit_abstains_and_asks():
    """"Change it to 500." 500? 500 thousand? 500,000 JPY? Reading it as
    500 is a decision the sentence does not support, and it is the kind of
    decision that reaches a payment system."""
    draft, result = only("Change it to 500.")
    assert draft.desired_value == UNKNOWN
    assert "no unit" in draft.confidence_basis
    assert any(q.about == "unit" for q in result.questions)
    assert "500" in result.questions[0].text


def test_removal_is_not_a_value_change():
    """"Drop this rule" and "set it to zero" produce completely different
    proposals. A parser that collapsed them would turn a deletion into an
    edit."""
    draft, _ = only("Drop this rule.")
    assert draft.intent_type == "rule_removal"
    assert draft.desired_value is None
    assert draft.desired_value != 0


def test_removal_is_checked_before_the_number_in_the_sentence():
    """REGRESSION GUARD. "Drop the JPY 1,000,000 threshold" contains an
    amount, and reading the amount first turns a deletion into an edit that
    sets the value to what it already is."""
    draft, _ = only("Drop the JPY 1,000,000 threshold.")
    assert draft.intent_type == "rule_removal"
    assert draft.desired_value is None


def test_two_asks_in_one_sentence_become_two_drafts():
    """Merging them forces a person to accept or reject both together,
    which is how the invalid half gets approved."""
    result = extract("Lower the threshold and also shorten the cancellation "
                     "deadline.", target=TARGET)
    assert len(result.drafts) == 2
    assert len({d.source_text for d in result.drafts}) == 2


def test_a_sentence_that_merely_mentions_two_things_is_not_split():
    """"The threshold and the deadline are documented in the SOP" is one
    statement, not two asks. Splitting on the conjunction alone would turn
    every descriptive sentence into two intents."""
    result = extract("The threshold and the deadline are documented in the SOP.",
                     target=TARGET)
    assert len(result.drafts) == 1


def test_a_value_equal_to_the_current_one_still_parses():
    """Scenario H is decided in reconciliation, not here. This module's job is to
    read the sentence; deciding that nothing needs to change is
    reconciliation's."""
    draft, _ = only("The threshold should be JPY 1,000,000.",
                    current_value=1000000)
    assert draft.desired_value == 1000000
    assert draft.current_value == 1000000
    assert draft.is_actionable


def test_several_amounts_in_one_clause_are_asked_about_not_ranked():
    draft, result = only("Raise it from JPY 500,000 to JPY 1,000,000.")
    assert draft.desired_value == UNKNOWN
    assert any("amounts" in q.text for q in result.questions)


# --- the schema gate ---------------------------------------------------------------


def test_malformed_json_is_blocked_not_repaired():
    """FAILURE INJECTION. A parser that patches malformed output produces a
    shape nobody downstream can rely on."""
    for rubbish in ("", "not json at all", "[1,2,3]", '{"intent_type": "vibes"}'):
        with pytest.raises(MalformedProposal):
            parse_proposal(rubbish)


def test_a_fenced_code_block_is_accepted():
    """Models wrap JSON in fences. Refusing that would be refusing the
    format rather than the content."""
    payload = parse_proposal(
        '```json\n{"intent_type": "business_rule_change", "target": "x"}\n```')
    assert payload["target"] == "x"


def test_every_declared_intent_type_is_accepted():
    for intent_type in INTENT_TYPES:
        assert parse_proposal(
            '{"intent_type": "%s"}' % intent_type)["intent_type"] == intent_type


# --- the model cannot smuggle a value in ---------------------------------------------


def test_a_proposer_inventing_a_value_is_overruled_by_the_sentence():
    """FAILURE INJECTION, and the whole defence of this module.

    The model is asked for a number and given a sentence with none. It
    supplies one confidently. No wording of the prompt reliably stops that,
    so the sentence -- not the model -- decides whether a value exists.
    """
    class Inventor:
        name = "inventor"

        def explain(self, prompt):
            return ('{"intent_type": "business_rule_change", '
                    '"target": "approval_threshold", "desired_value": 750000, '
                    '"unit": "JPY"}')

    draft, result = only("Please make the approval process appropriate.",
                         proposer=Inventor())
    assert draft.desired_value == UNKNOWN, (
        "the model's number was accepted although the sentence contains none")
    assert "whatever the proposer returned" in draft.confidence_basis
    assert result.questions


def test_a_proposer_that_fails_leaves_an_abstention_not_a_crash():
    class Broken:
        name = "broken"

        def explain(self, prompt):
            raise RuntimeError("Vertex AI unavailable")

    draft, _ = only("Please make the approval process appropriate.",
                    proposer=Broken())
    assert draft.desired_value == UNKNOWN
    assert "RuntimeError" in draft.confidence_basis


def test_the_prompt_says_unknown_is_a_valid_answer():
    """A model asked for a number and told nothing about abstaining will
    produce a number."""
    prompt = build_prompt("It should be stricter.", target=TARGET)
    assert "UNKNOWN" in prompt
    assert "not a failure" in prompt
    assert "Do not decide whether the change is a good idea" in prompt


# --- the architecture rule ------------------------------------------------------------


def test_the_parser_imports_no_store():
    """The plan states this as a grep. An extractor that could persist
    would make a parsed guess durable before anybody validated it."""
    import inspect

    from core.change_intelligence import intent_parser

    source = inspect.getsource(intent_parser)
    for forbidden in ("import sqlite", "from core.kernel", "session.commit",
                      "write_text", "json.dump", "open("):
        assert forbidden not in source, f"{forbidden} in intent_parser.py"


def test_splitting_is_exposed_for_its_own_test():
    assert split_requests("a and also b") == ["a and also b"]
