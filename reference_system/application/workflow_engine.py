# -*- coding: utf-8 -*-
"""Executes approval_routes.yaml.

The same business rules as `approval_service.py`, expressed as declarative
routing rather than as code. Two representations of one policy, both of them
live, both of them executable -- which is precisely why they can disagree
without anyone noticing.

When they disagree, neither is the bug. The DIVERGENCE is.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import yaml

from reference_system.application.approval_service import Decision, PurchaseRequest

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


@dataclass(frozen=True)
class Route:
    id: str
    rule_id: str
    field: str
    op: str
    value: object
    approver_role: str

    def matches(self, request: PurchaseRequest) -> bool:
        actual = getattr(request, self.field, None)
        if actual is None:
            return False
        try:
            return _OPS[self.op](actual, self.value)
        except TypeError:
            # Comparing a string to a number is a routing-table mistake, not
            # a runtime crash the business should suffer. Say no and move on.
            return False


def load_routes(raw: bytes | str) -> list[Route]:
    """Parse the routing table from bytes.

    Bytes, not a path: the content may come from a session overlay that was
    never written to disk. Same constraint as C-1 in ADR-002.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    doc = yaml.safe_load(io.BytesIO(raw)) or {}
    return [
        Route(
            id=entry["id"],
            rule_id=entry.get("rule_id", ""),
            field=entry["condition"]["field"],
            op=entry["condition"]["op"],
            value=entry["condition"]["value"],
            approver_role=entry.get("approver_role", "NONE"),
        )
        for entry in doc.get("approval_routes", [])
    ]


_ROLE_TO_OUTCOME = {
    "NONE": "auto_approved",
    "MANAGER": "manager_approval",
    "DIRECTOR": "director_approval",
    "FINANCE": "finance_approval",
}


def route_by_workflow(request: PurchaseRequest, routes: list[Route]) -> Decision:
    """First matching route wins. Order in the file is the policy."""
    for route in routes:
        if route.matches(request):
            return Decision(
                _ROLE_TO_OUTCOME.get(route.approver_role, "auto_approved"),
                route.rule_id or route.id,
                f"workflow route {route.id}",
            )
    return Decision("auto_approved", "BR-001", "no workflow route matched")
