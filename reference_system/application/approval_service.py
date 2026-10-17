# -*- coding: utf-8 -*-
"""Procurement approval — the standard path.

Where BR-001 through BR-005 actually take effect. Every decision is made
from the `config` handed in, so the same code can be run against the current
policy and a proposed one in the same process.

This is the file a business analyst would be shown if they asked "what does
the system actually do?" -- which is why the rule order below is explicit
rather than clever.
"""

from __future__ import annotations

from dataclasses import dataclass

AUTO_APPROVED = "auto_approved"
MANAGER_APPROVAL = "manager_approval"
DIRECTOR_APPROVAL = "director_approval"
FINANCE_APPROVAL = "finance_approval"


@dataclass(frozen=True)
class PurchaseRequest:
    id: str
    amount: int
    supplier_id: str = "SUP-001"
    customer_tier: str = "standard"
    process: str = "standard_po"
    category: str = "opex"
    requested_by: str = "buyer@acme.example"
    supplier_risk: str = "normal"


@dataclass(frozen=True)
class Decision:
    outcome: str
    rule_id: str
    reason: str

    def __str__(self) -> str:
        return f"{self.outcome} ({self.rule_id}: {self.reason})"


def evaluate_approval(request: PurchaseRequest, config) -> Decision:
    """Route one purchase request.

    Order matters and is stated, not implied: escalation beats exemption,
    exemption beats the threshold. Reversing the first two would let a
    strategic customer route a high-risk supplier past a director, which is
    the kind of rule interaction that only shows up when you run it.
    """
    # BR-005 -- risk escalates regardless of who is asking or how much.
    if request.supplier_risk == "high":
        return Decision(DIRECTOR_APPROVAL, "BR-005",
                        "high-risk supplier escalates regardless of amount")

    # BR-004 -- capital expenditure is Finance's call, not the line manager's.
    capex_threshold = config.threshold("BR-004")
    if request.category == "capex" and capex_threshold is not None \
            and request.amount >= capex_threshold:
        return Decision(FINANCE_APPROVAL, "BR-004",
                        f"capex at or above {capex_threshold:,} {config.currency}")

    # BR-002 -- the 2020 Q4 campaign exemption. Still on.
    #
    # No SOP mentions it and no document describes it; it exists only here
    # and in approval_routes.yaml. That is what makes it an UNDOCUMENTED
    # behaviour rather than a documented exception, and it is seeded
    # deliberately (UN-001).
    if request.customer_tier == "strategic":
        return Decision(AUTO_APPROVED, "BR-002",
                        "strategic customer exemption (2020 Q4 campaign)")

    # BR-001 -- the hero rule.
    threshold = config.threshold("BR-001")
    if threshold is not None and request.amount >= threshold:
        return Decision(MANAGER_APPROVAL, "BR-001",
                        f"at or above {threshold:,} {config.currency}")

    return Decision(AUTO_APPROVED, "BR-001",
                    f"below {threshold:,} {config.currency}")


def route_request(request: PurchaseRequest, config) -> str:
    """Outcome only. The shape the simulator discovers and calls."""
    return evaluate_approval(request, config).outcome
