"""Maker-checker validation and approval event execution."""

from typing import Optional
from app.enums import Role, RouteType
from app.models import AuditEvent, Employee, Requisition, RouteDecision


def approve_request(
    request: Requisition,
    route: RouteDecision,
    actor: Employee,
    occurred_at: Optional[str] = None,
    event_id: Optional[str] = None,
) -> AuditEvent:
    """Validates maker-checker separation and actor role authority before approving.
    
    Raises ValueError or PermissionError if maker-checker or role constraints are violated.
    """
    if route.route == RouteType.REJECTED:
        raise ValueError(f"Cannot approve requisition {request.request_id} with REJECTED route")

    # AUTO approval is handled by automated system authority
    if route.route == RouteType.AUTO:
        return AuditEvent(
            event_id=event_id or f"EVT-AUTO-{request.request_id}",
            entity_type="REQUISITION",
            entity_id=request.request_id,
            action="AUTO_APPROVE",
            actor_id="SYSTEM_AUTO",
            occurred_at=occurred_at or request.requested_at,
            before_state={"route": route.route.value},
            after_state={"approval_status": "APPROVED", "approver": "SYSTEM_AUTO"},
        )

    # Maker-Checker separation: requester cannot approve own requisition
    if actor.employee_id == request.requester_id:
        raise ValueError(
            f"Maker-checker violation: Requester {actor.employee_id} cannot approve their own requisition {request.request_id}"
        )

    # Required Role verification
    required_role = None
    if route.route == RouteType.NEEDS_MANAGER:
        required_role = Role.MANAGER
    elif route.route == RouteType.NEEDS_FINANCE:
        required_role = Role.FINANCE
    elif route.route == RouteType.NEEDS_DIRECTOR:
        required_role = Role.DIRECTOR

    if required_role is not None and required_role not in actor.roles:
        raise PermissionError(
            f"Role mismatch: Approver {actor.employee_id} lacks required {required_role.value} role (has {[r.value for r in actor.roles]})"
        )

    return AuditEvent(
        event_id=event_id or f"EVT-APPR-{request.request_id}",
        entity_type="REQUISITION",
        entity_id=request.request_id,
        action="HUMAN_APPROVE",
        actor_id=actor.employee_id,
        occurred_at=occurred_at or request.requested_at,
        before_state={"route": route.route.value},
        after_state={"approval_status": "APPROVED", "approver": actor.employee_id, "role": required_role.value if required_role else "UNKNOWN"},
    )
