# -*- coding: utf-8 -*-
"""Shared machinery for the contract suite."""

from __future__ import annotations

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "core" / "schema" / "contracts"
EVENTS = ROOT / "core" / "schema" / "events"

#: The twelve product contracts, named rather than globbed. A
#: glob would quietly pass if somebody deleted a schema; this list is the
#: claim, and `test_exactly_twelve_contracts_exist` checks reality against it.
CONTRACT_NAMES = [
    "ActionProposal", "Approval", "ChangeSet", "Evidence", "Feedback",
    "ImpactEdge", "ImpactResult", "Intent", "KnowledgeRecord", "RealityFact",
    "ShadowChange", "VerificationResult",
]

EVENT_NAMES = [
    "ArtifactChanged", "ChangeApplied", "ChangeApproved", "ChangeProposed",
    "HumanFeedbackAdded", "ImpactCalculated", "IntentAccepted",
    "ShadowChangeCreated", "VerificationCompleted",
]


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    """Resolve `$ref` between sibling schemas without touching the network."""
    resources = []
    for path in list(CONTRACTS.glob("*.json")) + list(EVENTS.glob("*.json")):
        schema = _load(path)
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        resources.append((path.name, resource))
        # Events reference contracts as ../contracts/Name.json
        resources.append((f"../contracts/{path.name}", resource))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def validator_for(name: str, *, events: bool = False) -> Draft202012Validator:
    directory = EVENTS if events else CONTRACTS
    return Draft202012Validator(_load(directory / f"{name}.json"), registry=REGISTRY)


def is_valid(name: str, instance: dict, *, events: bool = False) -> bool:
    return validator_for(name, events=events).is_valid(instance)


def errors_for(name: str, instance: dict, *, events: bool = False) -> list[str]:
    return [e.message for e in validator_for(name, events=events).iter_errors(instance)]


# --- fixtures ---------------------------------------------------------------

EVIDENCE = {
    "schema_version": "1.0",
    "artifact": "approval_routes.yaml",
    "location": "rules[0].threshold",
    "quote": "threshold: 1000000",
    "snapshot_id": "SNAP-A0",
}


@pytest.fixture
def evidence() -> dict:
    return dict(EVIDENCE)


#: One valid instance per contract. Every negative test starts from one of
#: these and removes or corrupts exactly one thing, so a failure names the
#: field that caused it rather than "the fixture is wrong somewhere".
VALID: dict[str, dict] = {
    "Evidence": dict(EVIDENCE),
    "RealityFact": {
        "schema_version": "1.0",
        "kind": "threshold",
        "id": "FACT-001",
        "provenance": {"extractor": "workflow_adapter",
                       "artifact": "approval_routes.yaml",
                       "location": "rules[0].threshold"},
        "snapshot_id": "SNAP-A0",
        "value": 1000000,
        "unit": "JPY",
    },
    "KnowledgeRecord": {
        "schema_version": "1.0",
        "knowledge_type": "rule",
        "id": "KM-001",
        "semantic_id": "approval_threshold",
        "evidence": [dict(EVIDENCE)],
        "lineage": {"derived_from": ["FACT-001"], "km_version": 1},
        "state": "PROPOSED",
    },
    "Intent": {
        "schema_version": "1.0",
        "intent_id": "INTENT-042",
        "actor": "ops.lead@example.com",
        "target": "approval_threshold",
        "requested_change": "lower the manager sign-off threshold to 500,000 JPY",
        "rationale": "audit finding 2026-Q3",
        "scope": {"artifact_families": ["workflow", "spreadsheet"],
                  "operational_domain": "Procurement & Orders"},
        "evidence": [dict(EVIDENCE)],
        "confidence": 0.9,
        "status": "DRAFT",
        "version": 1,
    },
    "Feedback": {
        "schema_version": "1.0",
        "comment_id": "CMT-001",
        "semantic_type": "intent",
        "author": "ops.lead@example.com",
        "timestamp": "2026-09-19T10:00:00Z",
        "body": "We should lower the threshold to 500,000.",
    },
    "ImpactEdge": {
        "schema_version": "1.0",
        "from_node": "approval_threshold",
        "to_node": "approval_matrix.csv",
        "edge_type": "states_rule",
        "distance": "DIRECT",
        "reason": "the spreadsheet states the same rule and would contradict the new value",
        "evidence": [dict(EVIDENCE)],
        "confidence_basis": "deterministic",
    },
    "ImpactResult": {
        "schema_version": "1.0",
        "edges": [],
        "coverage_basis": "all 6 artifact families in the Procurement baseline were searched",
        "unknown": [],
        "risk": "LOW",
    },
    "ShadowChange": {
        "schema_version": "1.0",
        "shadow_id": "SHADOW-001",
        "base_snapshot": "SNAP-A0",
        "session_id": "SESSION-A",
        "target": "approval_threshold",
        "current_value": 1000000,
        "proposed_value": 500000,
        "intent": "INTENT-042",
        "impacted_artifacts": ["approval_matrix.csv"],
        "scenario_id": "SCN-001",
    },
    "ActionProposal": {
        "schema_version": "1.0",
        "kind": "workflow",
        "target": "approval_routes.yaml",
        "diff": "- threshold: 1000000\n+ threshold: 500000",
        "rationale": "aligns the workflow with the approved intent",
        "evidence": [dict(EVIDENCE)],
        "necessity": "required",
        "artifact_family": "workflow",
    },
    "Approval": {
        "schema_version": "1.0",
        "actor": "finance.controller@example.com",
        "timestamp": "2026-09-19T11:00:00Z",
        "changeset_id": "CS-001",
        "proposal_version": 1,
        "decision": "APPROVED",
        "reason": "impact reviewed; 47 orders affected, all within policy",
    },
    "VerificationResult": {
        "schema_version": "1.0",
        "changeset_id": "CS-001",
        "tests": {"passed": 12, "failed": 0},
        "evidence_freshness": "fresh",
        "alignment": "ALIGNED",
        "decision": "NO_CHANGE_REQUIRED",
        "unresolved": [],
    },
    "ChangeSet": {
        "schema_version": "1.0",
        "changeset_id": "CS-001",
        "trigger": "IntentAccepted:INTENT-042",
        "intent": "INTENT-042",
        "base_snapshot": "SNAP-A0",
        "state": "PROPOSED",
        "affected_nodes": ["approval_threshold"],
        "affected_artifacts": ["approval_routes.yaml", "approval_matrix.csv"],
        "evidence": [dict(EVIDENCE)],
        "simulation_result": None,
        "actions": [],
        "risk": "MEDIUM",
        "approval": None,
        "verification": None,
    },
}


@pytest.fixture(params=CONTRACT_NAMES)
def contract_name(request) -> str:
    return request.param


def valid_instance(name: str) -> dict:
    return json.loads(json.dumps(VALID[name]))
