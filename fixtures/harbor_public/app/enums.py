"""Domain enumeration types for Harbor Procurement Mini."""

from enum import Enum


class ProcessType(str, Enum):
    STANDARD = "STANDARD"
    EMERGENCY = "EMERGENCY"


class CategoryType(str, Enum):
    OPEX = "OPEX"
    CAPEX = "CAPEX"


class SupplierStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class SupplierRisk(str, Enum):
    NORMAL = "NORMAL"
    HIGH = "HIGH"


class Role(str, Enum):
    BUYER = "BUYER"
    MANAGER = "MANAGER"
    FINANCE = "FINANCE"
    DIRECTOR = "DIRECTOR"


class RouteType(str, Enum):
    REJECTED = "REJECTED"
    AUTO = "AUTO"
    NEEDS_MANAGER = "NEEDS_MANAGER"
    NEEDS_FINANCE = "NEEDS_FINANCE"
    NEEDS_DIRECTOR = "NEEDS_DIRECTOR"


class POState(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"


class ReceiptCondition(str, Enum):
    ACCEPTED = "ACCEPTED"
    DAMAGED = "DAMAGED"


class InvoiceStatus(str, Enum):
    REJECTED = "REJECTED"
    HOLD_UNAPPROVED = "HOLD_UNAPPROVED"
    HOLD_QUANTITY = "HOLD_QUANTITY"
    HOLD_PRICE = "HOLD_PRICE"
    READY = "READY"
