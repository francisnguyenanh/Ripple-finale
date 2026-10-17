# -*- coding: utf-8 -*-
"""Audit trail for procurement decisions.

Every entry names the rule that produced it. An audit trail that records
outcomes without recording WHICH rule produced them cannot answer the only
question an auditor asks -- "under what policy was this approved?" -- which
is also the question the product answers at scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reference_system.application.approval_service import Decision, PurchaseRequest


@dataclass
class AuditTrail:
    entries: list[dict] = field(default_factory=list)

    def record(self, request: PurchaseRequest, decision: Decision, *, policy_version: int) -> dict:
        entry = {
            "request_id": request.id,
            "amount": request.amount,
            "process": request.process,
            "outcome": decision.outcome,
            "rule_id": decision.rule_id,
            "reason": decision.reason,
            "policy_version": policy_version,
        }
        self.entries.append(entry)
        return entry

    def for_rule(self, rule_id: str) -> list[dict]:
        return [e for e in self.entries if e["rule_id"] == rule_id]
