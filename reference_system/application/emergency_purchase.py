# -*- coding: utf-8 -*-
"""Emergency purchase path — added 2020-03 during a supply disruption.

The threshold below was copied from the procurement policy at the time and
never wired back to configuration. Nothing in this file reads `config`.

This is deliberate seed content (UN-002 / IMPACT-003). It is the artifact a
person doing a careful manual review misses -- the code reads as correct,
the number is the number the policy had, and nothing about it looks stale.
It stops being invisible only when the policy changes and somebody RUNS it.

That is the crux of the product's second claim: a static reader of
configuration cannot see this, and a reader of code sees a plausible
constant. Only executing both paths under the proposed policy shows that
the business would start behaving inconsistently with itself.
"""

from __future__ import annotations

from reference_system.application.approval_service import (
    AUTO_APPROVED,
    DIRECTOR_APPROVAL,
    MANAGER_APPROVAL,
    Decision,
    PurchaseRequest,
)


def evaluate_emergency(request: PurchaseRequest, config) -> Decision:
    """Route an emergency purchase.

    `config` is accepted so this has the same shape as the standard path,
    and is then ignored -- which is exactly the defect.
    """
    if request.supplier_risk == "high":
        return Decision(DIRECTOR_APPROVAL, "BR-005",
                        "high-risk supplier escalates regardless of amount")

    if request.amount <= 1_000_000:  # noqa: PLR2004 -- copied 2020-03, never updated
        return Decision(AUTO_APPROVED, "BR-006", "emergency fast-track")

    return Decision(MANAGER_APPROVAL, "BR-006", "emergency above the fast-track ceiling")


def route_emergency(request: PurchaseRequest, config) -> str:
    """Outcome only. Same shape as the standard path, on purpose.

    The simulator discovers handlers by shape, not by name -- so this one is
    found and run without anything having to know it is the suspect.
    """
    return evaluate_emergency(request, config).outcome
