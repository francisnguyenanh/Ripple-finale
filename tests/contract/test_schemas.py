# -*- coding: utf-8 -*-
"""Contract tests for the twelve product schemas.

Gate G1 asks for contracts "implementable without semantic ambiguity". A
schema that only ever sees well-formed input proves nothing about that, so
every contract gets one valid fixture and at least three invalid ones, and
the invalid ones are derived by breaking exactly one thing.
"""

from __future__ import annotations

import json

import pytest
from conftest import (
    CONTRACT_NAMES,
    CONTRACTS,
    VALID,
    errors_for,
    is_valid,
    valid_instance,
)


def test_exactly_twelve_contracts_exist():
    on_disk = sorted(p.stem for p in CONTRACTS.glob("*.json"))
    assert on_disk == sorted(CONTRACT_NAMES), (
        "the twelve contracts of P1-T03 are a fixed list; adding or removing "
        "one is a contract change and must be a deliberate edit here too"
    )


def test_every_contract_has_a_valid_fixture(contract_name):
    assert contract_name in VALID, f"{contract_name} has no valid fixture"
    assert is_valid(contract_name, valid_instance(contract_name)), errors_for(
        contract_name, valid_instance(contract_name)
    )


# --- the seven negative cases, applied to every contract --------------------


def test_missing_a_required_field_is_rejected(contract_name):
    """Every required field, one at a time. Not just the first one."""
    schema = json.loads((CONTRACTS / f"{contract_name}.json").read_text(encoding="utf-8"))
    for field in schema["required"]:
        instance = valid_instance(contract_name)
        instance.pop(field, None)
        assert not is_valid(contract_name, instance), (
            f"{contract_name} accepted an instance with {field!r} removed, "
            f"though it is declared required"
        )


def test_an_unknown_field_is_rejected(contract_name):
    """additionalProperties: false, checked rather than assumed.

    A contract that silently swallows extra keys cannot detect a producer
    that has drifted ahead of it -- which is the whole job of schema_version.
    """
    instance = valid_instance(contract_name)
    instance["a_field_no_contract_declares"] = "…"
    assert not is_valid(contract_name, instance)


def test_a_bad_enum_value_is_rejected(contract_name):
    schema = json.loads((CONTRACTS / f"{contract_name}.json").read_text(encoding="utf-8"))
    enums = [
        name
        for name, spec in schema["properties"].items()
        if "enum" in spec and name in VALID[contract_name]
    ]
    if not enums:
        pytest.skip(f"{contract_name} declares no top-level enum field")
    for field in enums:
        instance = valid_instance(contract_name)
        instance[field] = "NOT_A_MEMBER_OF_THIS_ENUM"
        assert not is_valid(contract_name, instance), (
            f"{contract_name}.{field} accepted a value outside its enum"
        )


def test_schema_version_is_required_everywhere(contract_name):
    schema = json.loads((CONTRACTS / f"{contract_name}.json").read_text(encoding="utf-8"))
    assert "schema_version" in schema["required"], (
        f"{contract_name} has no schema_version; P8 cannot detect a stale record without it"
    )
    instance = valid_instance(contract_name)
    instance["schema_version"] = "not-a-version"
    assert not is_valid(contract_name, instance)


# --- the invariants, each against the contract that carries it --------------


@pytest.mark.parametrize("contract", ["KnowledgeRecord", "ImpactEdge", "ActionProposal", "ChangeSet"])
def test_i2_evidence_may_never_be_empty(contract):
    """I2: every knowledge record carries evidence and lineage."""
    instance = valid_instance(contract)
    instance["evidence"] = []
    assert not is_valid(contract, instance), (
        f"{contract} accepted an empty evidence list; a claim with no evidence "
        f"is indistinguishable from an assertion"
    )


def test_i1_reality_fact_refuses_interpretive_keys():
    """I1: the Fact layer is machine-observed. No meaning, no confidence.

    Enforced in the schema rather than by convention, because the writer who
    adds `confidence` to a fact will be doing it for a good reason and will
    not think of it as crossing a layer boundary.
    """
    for key in ["meaning", "confidence", "purpose", "intent", "rationale",
                "interpretation", "implication"]:
        instance = valid_instance("RealityFact")
        instance[key] = "anything at all"
        assert not is_valid("RealityFact", instance), (
            f"RealityFact accepted the interpretive key {key!r} -- the Fact and "
            f"Inference layers have merged (I3)"
        )


def test_i5_a_gap_record_must_carry_a_gap_id_and_a_reason():
    """I5: UNKNOWN never travels without a Gap ID and a reason."""
    instance = valid_instance("KnowledgeRecord")
    instance["knowledge_type"] = "gap"
    assert not is_valid("KnowledgeRecord", instance), (
        "a gap without gap_id/reason was accepted; an unexplained UNKNOWN is "
        "indistinguishable from a bug"
    )
    instance["gap_id"] = "GAP-001"
    instance["reason"] = "no artifact states the threshold for emergency purchases"
    assert is_valid("KnowledgeRecord", instance)


def test_unknown_impact_forbids_a_low_risk_verdict():
    """A system that says LOW while admitting it did not look everywhere is lying politely."""
    instance = valid_instance("ImpactResult")
    instance["unknown"] = ["emergency_purchase.py: threshold source not resolved"]
    instance["risk"] = "LOW"
    assert not is_valid("ImpactResult", instance)

    for acceptable in ["MEDIUM", "HIGH", "UNKNOWN"]:
        instance["risk"] = acceptable
        assert is_valid("ImpactResult", instance), f"{acceptable} should be permitted"


def test_necessity_is_required_and_bounded():
    """The field that makes the unnecessary-change metric measurable."""
    instance = valid_instance("ActionProposal")
    del instance["necessity"]
    assert not is_valid("ActionProposal", instance)

    for value in ["required", "recommended", "optional", "unsupported"]:
        instance = valid_instance("ActionProposal")
        instance["necessity"] = value
        assert is_valid("ActionProposal", instance)


def test_verification_alignment_takes_exactly_the_four_alignment_states():
    """NO_CHANGE_REQUIRED is a ChangeDecision. It must not be accepted here."""
    for state in ["ALIGNED", "DRIFT", "CONFLICT", "UNKNOWN"]:
        instance = valid_instance("VerificationResult")
        instance["alignment"] = state
        assert is_valid("VerificationResult", instance)

    instance = valid_instance("VerificationResult")
    instance["alignment"] = "NO_CHANGE_REQUIRED"
    assert not is_valid("VerificationResult", instance), (
        "VerificationResult.alignment accepted a ChangeDecision value -- the two "
        "axes have been merged, and P8's contract is broken"
    )


def test_a_changeset_without_a_base_snapshot_is_not_replayable():
    instance = valid_instance("ChangeSet")
    del instance["base_snapshot"]
    assert not is_valid("ChangeSet", instance)


def test_an_approval_missing_any_single_field_is_invalid():
    """I12: six fields, and none of them is optional."""
    for field in ["actor", "timestamp", "changeset_id", "proposal_version", "decision", "reason"]:
        instance = valid_instance("Approval")
        del instance[field]
        assert not is_valid("Approval", instance), (
            f"Approval accepted an instance without {field!r}"
        )


def test_intent_scope_is_required():
    """The field most often forgotten."""
    instance = valid_instance("Intent")
    del instance["scope"]
    assert not is_valid("Intent", instance)

    instance = valid_instance("Intent")
    instance["scope"] = {"artifact_families": []}
    assert not is_valid("Intent", instance), "an empty scope bounds nothing"


# --- malformed model output -------------------------------------------------


MALFORMED_LLM_OUTPUTS = [
    {},
    {"result": "I could not determine the threshold."},
    {"schema_version": "1.0", "answer": None},
    {"schema_version": "1.0", "evidence": "approval_routes.yaml line 8"},
    {"thinking": "Let me consider…", "final": {"threshold": 500000}},
]


@pytest.mark.parametrize("payload", MALFORMED_LLM_OUTPUTS, ids=range(len(MALFORMED_LLM_OUTPUTS)))
def test_q3_no_contract_accepts_malformed_model_output(payload, contract_name):
    """Q3 of P1 §6: run malformed output through all twelve; all must reject.

    The fourth case is the interesting one -- evidence as a STRING rather
    than a list of Evidence objects is what a model produces when asked for
    evidence without being given the shape.
    """
    assert not is_valid(contract_name, payload), (
        f"{contract_name} accepted malformed model output: {payload!r}"
    )
