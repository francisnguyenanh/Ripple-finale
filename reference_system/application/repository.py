# -*- coding: utf-8 -*-
"""In-memory stores for purchase requests and decisions.

A dict, not a database. The reference system exists to be READ and RUN, not
to be operated; a real persistence layer would add nothing a judge can see
and would give the adapters at P3 a great deal of noise to wade through.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reference_system.application.approval_service import Decision, PurchaseRequest


@dataclass
class PurchaseRequestRepository:
    _requests: dict[str, PurchaseRequest] = field(default_factory=dict)

    def save(self, request: PurchaseRequest) -> None:
        self._requests[request.id] = request

    def get(self, request_id: str) -> PurchaseRequest | None:
        return self._requests.get(request_id)

    def all(self) -> list[PurchaseRequest]:
        return [self._requests[k] for k in sorted(self._requests)]


@dataclass
class DecisionLog:
    """Append-only. A decision log you can edit is not a decision log."""

    _entries: list[tuple[str, Decision]] = field(default_factory=list)

    def record(self, request_id: str, decision: Decision) -> None:
        self._entries.append((request_id, decision))

    def entries(self) -> list[tuple[str, Decision]]:
        return list(self._entries)

    def outcomes_by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, decision in self._entries:
            counts[decision.rule_id] = counts.get(decision.rule_id, 0) + 1
        return counts
