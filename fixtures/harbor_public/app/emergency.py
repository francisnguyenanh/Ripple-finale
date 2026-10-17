"""Emergency requisition approval routing logic."""

from typing import Optional
from app.enums import CategoryType, RouteType, SupplierRisk, SupplierStatus
from app.models import ApprovalConfig, Requisition, RouteDecision, Supplier


def route_emergency(
    request: Requisition,
    config: ApprovalConfig,
    suppliers: Optional[dict[str, Supplier]] = None,
    supplier: Optional[Supplier] = None,
) -> RouteDecision:
    """Evaluates emergency requisition routing.
    
    Applies input validation, supplier restrictions, and high-value CAPEX rules,
    followed by the dedicated emergency approval threshold.
    """
    # Validation
    if isinstance(request.net_amount_jpy, bool) or not isinstance(request.net_amount_jpy, int):
        return RouteDecision(request.request_id, RouteType.REJECTED, "INVALID_AMOUNT", 0, config.version)
    if request.net_amount_jpy <= 0:
        return RouteDecision(request.request_id, RouteType.REJECTED, "NONPOSITIVE_AMOUNT", 0, config.version)
    if isinstance(request.quantity, bool) or not isinstance(request.quantity, int) or request.quantity <= 0:
        return RouteDecision(request.request_id, RouteType.REJECTED, "INVALID_QUANTITY", 0, config.version)
    if request.currency != "JPY":
        return RouteDecision(request.request_id, RouteType.REJECTED, "UNSUPPORTED_CURRENCY", 0, config.version)

    # Supplier constraints
    sup = supplier
    if sup is None and suppliers is not None:
        sup = suppliers.get(request.supplier_id)

    if sup is not None:
        if sup.status == SupplierStatus.BLOCKED:
            return RouteDecision(request.request_id, RouteType.REJECTED, "SUPPLIER_BLOCKED", request.net_amount_jpy, config.version)
        if sup.risk == SupplierRisk.HIGH:
            return RouteDecision(request.request_id, config.high_risk_route, "SUPPLIER_HIGH_RISK", request.net_amount_jpy, config.version)

    # Category Constraints (CAPEX >= 2,000,000 JPY -> NEEDS_FINANCE)
    if request.category == CategoryType.CAPEX and request.net_amount_jpy >= config.capex_finance_threshold_jpy:
        return RouteDecision(request.request_id, RouteType.NEEDS_FINANCE, "CAPEX_HIGH_VALUE", request.net_amount_jpy, config.version)

    # Emergency routing threshold
    if request.net_amount_jpy >= 1_000_000:
        return RouteDecision(request.request_id, RouteType.NEEDS_MANAGER, "EMERGENCY_MANAGER_REQUIRED", request.net_amount_jpy, config.version)

    return RouteDecision(request.request_id, RouteType.AUTO, "EMERGENCY_AUTO_APPROVED", request.net_amount_jpy, config.version)
