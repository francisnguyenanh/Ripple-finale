"""Domain data models and typed schemas for Harbor Procurement Mini."""

from dataclasses import dataclass, field
from typing import Any, Optional
from app.enums import (
    CategoryType,
    InvoiceStatus,
    POState,
    ProcessType,
    ReceiptCondition,
    Role,
    RouteType,
    SupplierRisk,
    SupplierStatus,
)


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    display_name: str
    status: SupplierStatus
    risk: SupplierRisk
    contact_email: str


@dataclass(frozen=True)
class Employee:
    employee_id: str
    display_name: str
    department: str
    roles: list[Role]


@dataclass(frozen=True)
class Requisition:
    request_id: str
    requester_id: str
    supplier_id: str
    process: ProcessType
    category: CategoryType
    net_amount_jpy: int
    currency: str
    requested_at: str
    quantity: int
    item_description: str


@dataclass(frozen=True)
class RouteDecision:
    request_id: str
    route: RouteType
    reason_code: str
    amount_used_jpy: int
    applied_config_version: str


@dataclass(frozen=True)
class PO:
    po_id: str
    request_id: str
    supplier_id: str
    currency: str
    quantity: int
    goods_net_jpy: int
    freight_jpy: int
    tax_jpy: int
    gross_jpy: int
    state: POState
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    po_id: str
    quantity_received: int
    received_at: str
    received_by: str
    condition: ReceiptCondition


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    supplier_id: str
    supplier_invoice_number: str
    po_id: str
    quantity: int
    goods_net_jpy: int
    freight_jpy: int
    tax_jpy: int
    gross_jpy: int
    currency: str
    received_date: str
    issue_date: str


@dataclass(frozen=True)
class InvoiceDecision:
    invoice_id: str
    status: InvoiceStatus
    reason_code: str
    due_date: Optional[str]
    matched_po_id: str
    comparison_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    entity_type: str
    entity_id: str
    action: str
    actor_id: str
    occurred_at: str
    before_state: Optional[dict[str, Any]] = None
    after_state: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ApprovalConfig:
    version: str
    currency: str
    standard_manager_threshold_jpy: int
    capex_finance_threshold_jpy: int
    high_risk_route: RouteType
    price_tolerance_fixed_jpy: int
    price_tolerance_percent: int
    payment_term_days: int
