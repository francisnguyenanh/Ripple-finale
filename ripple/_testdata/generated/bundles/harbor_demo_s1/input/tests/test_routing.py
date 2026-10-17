"""Tests for requisition approval routing and maker-checker validation."""

import pytest
from app.approval import approve_request
from app.emergency import route_emergency
from app.enums import CategoryType, ProcessType, Role, RouteType, SupplierRisk, SupplierStatus
from app.models import ApprovalConfig, Employee, Requisition, Supplier
from app.routing import route_request


@pytest.fixture
def base_config() -> ApprovalConfig:
    return ApprovalConfig(
        version="1.0.0",
        currency="JPY",
        standard_manager_threshold_jpy=500_000,
        capex_finance_threshold_jpy=2_000_000,
        high_risk_route=RouteType.NEEDS_DIRECTOR,
        price_tolerance_fixed_jpy=10_000,
        price_tolerance_percent=2,
        payment_term_days=30,
    )


@pytest.fixture
def make_req():
    def _maker(
        request_id="REQ-000001",
        requester_id="EMP-001",
        supplier_id="SUP-001",
        process=ProcessType.STANDARD,
        category=CategoryType.OPEX,
        amount=100_000,
        currency="JPY",
        quantity=1,
    ):
        return Requisition(
            request_id=request_id,
            requester_id=requester_id,
            supplier_id=supplier_id,
            process=process,
            category=category,
            net_amount_jpy=amount,
            currency=currency,
            requested_at="2026-09-01T10:00:00",
            quantity=quantity,
            item_description="Office supplies",
        )
    return _maker


def test_routing_input_validation(base_config, make_req):
    # Non-positive amount
    decision = route_request(make_req(amount=0), base_config)
    assert decision.route == RouteType.REJECTED

    # Unsupported currency
    decision = route_request(make_req(currency="USD"), base_config)
    assert decision.route == RouteType.REJECTED

    # Invalid quantity
    decision = route_request(make_req(quantity=0), base_config)
    assert decision.route == RouteType.REJECTED


def test_supplier_priority_overrides(base_config, make_req):
    blocked_sup = Supplier("SUP-BLK", "Blocked Vendor", SupplierStatus.BLOCKED, SupplierRisk.NORMAL, "b@v.example")
    high_risk_sup = Supplier("SUP-RSK", "Risky Vendor", SupplierStatus.ACTIVE, SupplierRisk.HIGH, "r@v.example")

    # Blocked supplier is rejected regardless of amount
    decision = route_request(make_req(supplier_id="SUP-BLK", amount=100_000), base_config, supplier=blocked_sup)
    assert decision.route == RouteType.REJECTED

    # High risk requires Director even for low value OPEX
    decision = route_request(make_req(supplier_id="SUP-RSK", amount=100_000), base_config, supplier=high_risk_sup)
    assert decision.route == RouteType.NEEDS_DIRECTOR

    # High risk requires Director even for high value CAPEX
    decision = route_request(make_req(supplier_id="SUP-RSK", amount=3_000_000, category=CategoryType.CAPEX), base_config, supplier=high_risk_sup)
    assert decision.route == RouteType.NEEDS_DIRECTOR


def test_category_and_threshold_routing(base_config, make_req):
    # Standard OPEX below 500k -> AUTO
    decision = route_request(make_req(amount=499_999, category=CategoryType.OPEX), base_config)
    assert decision.route == RouteType.AUTO

    # Standard OPEX at 500k -> NEEDS_MANAGER
    decision = route_request(make_req(amount=500_000, category=CategoryType.OPEX), base_config)
    assert decision.route == RouteType.NEEDS_MANAGER

    # Standard OPEX just above 500k -> NEEDS_MANAGER
    decision = route_request(make_req(amount=500_001, category=CategoryType.OPEX), base_config)
    assert decision.route == RouteType.NEEDS_MANAGER

    # Standard OPEX at 2M -> still NEEDS_MANAGER (CAPEX rule does not apply to OPEX)
    decision = route_request(make_req(amount=2_000_000, category=CategoryType.OPEX), base_config)
    assert decision.route == RouteType.NEEDS_MANAGER

    # Standard CAPEX at 2M -> NEEDS_FINANCE
    decision = route_request(make_req(amount=2_000_000, category=CategoryType.CAPEX), base_config)
    assert decision.route == RouteType.NEEDS_FINANCE


def test_emergency_routing_baseline(base_config, make_req):
    # Emergency below 500k -> AUTO
    decision = route_emergency(make_req(process=ProcessType.EMERGENCY, amount=499_999), base_config)
    assert decision.route == RouteType.AUTO

    # Emergency at 500k -> NEEDS_MANAGER
    decision = route_emergency(make_req(process=ProcessType.EMERGENCY, amount=500_000), base_config)
    assert decision.route == RouteType.NEEDS_MANAGER

    # Emergency just above 500k -> NEEDS_MANAGER
    decision = route_emergency(make_req(process=ProcessType.EMERGENCY, amount=500_001), base_config)
    assert decision.route == RouteType.NEEDS_MANAGER


def test_maker_checker_and_role_approval(base_config, make_req):
    req = make_req(requester_id="EMP-001", amount=1_000_000)
    decision = route_request(req, base_config)
    assert decision.route == RouteType.NEEDS_MANAGER

    manager = Employee("EMP-002", "Jane Manager", "Ops", [Role.MANAGER])
    buyer = Employee("EMP-003", "Bob Buyer", "Ops", [Role.BUYER])

    # Requester self-approval violation
    requester_as_manager = Employee("EMP-001", "Requester Who Is Also Manager", "Ops", [Role.MANAGER])
    with pytest.raises(ValueError, match="Maker-checker"):
        approve_request(req, decision, requester_as_manager)

    # Actor missing required manager role
    with pytest.raises(PermissionError, match="Role mismatch"):
        approve_request(req, decision, buyer)

    # Valid approval
    event = approve_request(req, decision, manager)
    assert event.action == "HUMAN_APPROVE"
    assert event.actor_id == "EMP-002"
