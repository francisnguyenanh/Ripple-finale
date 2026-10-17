# -*- coding: utf-8 -*-
"""Who gets told, when a request needs a human.

Recorded rather than sent. A reference system that emailed people during a
judge's what-if would be a reference system nobody could safely run twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reference_system.application.approval_service import (
    AUTO_APPROVED,
    DIRECTOR_APPROVAL,
    FINANCE_APPROVAL,
    MANAGER_APPROVAL,
    Decision,
)

RECIPIENTS = {
    MANAGER_APPROVAL: "procurement.manager@acme.example",
    DIRECTOR_APPROVAL: "operations.director@acme.example",
    FINANCE_APPROVAL: "finance.controller@acme.example",
}


@dataclass
class NotificationLog:
    sent: list[dict] = field(default_factory=list)

    def notify(self, request_id: str, decision: Decision) -> dict | None:
        """Auto-approved requests notify nobody -- that is the point of them."""
        if decision.outcome == AUTO_APPROVED:
            return None
        recipient = RECIPIENTS.get(decision.outcome)
        if recipient is None:
            return None
        message = {"request_id": request_id, "to": recipient,
                   "subject": f"Approval required: {request_id}",
                   "rule_id": decision.rule_id, "reason": decision.reason}
        self.sent.append(message)
        return message

    def count(self) -> int:
        return len(self.sent)
