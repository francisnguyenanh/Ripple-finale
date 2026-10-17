# -*- coding: utf-8 -*-
"""Procurement configuration — Acme Industrial Components.

Every threshold the business can change lives here, as data. Nothing in this
package reads a global, an environment variable, or a file: a config is
something you are HANDED.

That is not a style preference. P6 has to run the current policy and a
proposed policy side by side, in one process, and compare what the business
would actually do under each. A module-level constant would force a module
reload per scenario -- fragile, order-dependent, and slow enough that the
what-if stops feeling live. SPIKE-T03 tested this and ADR-002 made it a
requirement (C-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    threshold: int | None = None
    unit: str = "JPY"
    approver_role: str = ""
    note: str = ""


@dataclass(frozen=True)
class ProcurementConfig:
    """The policy, as the business states it."""

    rules: dict[str, Rule]
    currency: str = "JPY"
    version: int = 3

    def threshold(self, rule_id: str) -> int | None:
        return self.rules[rule_id].threshold

    def with_threshold(self, rule_id: str, value: int) -> "ProcurementConfig":
        """A copy with one threshold changed -- the shadow config of P6.

        Returns a new config rather than mutating: a what-if that could
        alter the config it was branched from would make the "current"
        column of the comparison meaningless.
        """
        rules = dict(self.rules)
        rules[rule_id] = replace(rules[rule_id], threshold=value)
        return replace(self, rules=rules)


def default_config() -> ProcurementConfig:
    """The policy as of the current baseline.

    BR-001 is 1,000,000 here. The spreadsheet says 500,000. Neither is
    marked authoritative, and that is the conflict the product exists to
    surface -- not a bug in this file.
    """
    return ProcurementConfig(rules={
        "BR-001": Rule("BR-001", "high_value_requires_manager", 1_000_000,
                       approver_role="MANAGER",
                       note="Procurement requests above the threshold need manager sign-off."),
        "BR-002": Rule("BR-002", "strategic_customer_exemption", None,
                       approver_role="NONE",
                       note="Strategic-tier customers skip approval. Q4 2020 campaign; never reverted."),
        "BR-003": Rule("BR-003", "shipment_lock", None,
                       note="A shipment in transit cannot be amended."),
        "BR-004": Rule("BR-004", "cancellation_window", 5_000_000,
                       approver_role="FINANCE",
                       note="Capital expenditure always routes to Finance."),
        "BR-005": Rule("BR-005", "high_risk_supplier_escalation", None,
                       approver_role="DIRECTOR",
                       note="High-risk suppliers escalate regardless of amount."),
        "BR-006": Rule("BR-006", "emergency_purchase_exception", 1_000_000,
                       approver_role="DIRECTOR",
                       note="Emergency purchases are fast-tracked below the threshold."),
    })
