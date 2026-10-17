# -*- coding: utf-8 -*-
"""Acme procurement — business rule tests.

Owned by the Procurement Operations team. These are the tests a change to
the approval rules is expected to be checked against.

Three of them assert behaviour the business has already moved past. They are
green today, and they are meant to be: a test suite in which every stale
expectation is already red would not resemble any real system, and would not
support the claim the product makes -- that green tests are not the same as
correct tests.

They are marked, listed in `seed_manifest.yaml` as TEST-OLD-001..003, and
they go red the moment BR-001 is lowered. P8-T07's regression guard exists
to notice that.
"""

from __future__ import annotations

import pytest

from reference_system.application.approval_service import (
    AUTO_APPROVED,
    DIRECTOR_APPROVAL,
    FINANCE_APPROVAL,
    MANAGER_APPROVAL,
    PurchaseRequest,
    evaluate_approval,
    route_request,
)
from reference_system.application.audit import AuditTrail
from reference_system.application.config import default_config
from reference_system.application.emergency_purchase import route_emergency
from reference_system.application.notification import NotificationLog
from reference_system.application.workflow_engine import load_routes, route_by_workflow

ROUTES_YAML = """
version: 3
currency: JPY
approval_routes:
  - id: PROCUREMENT_HIGH_RISK_SUPPLIER
    rule_id: BR-005
    condition: { field: supplier_risk, op: "==", value: high }
    approver_role: DIRECTOR
  - id: PROCUREMENT_STRATEGIC_CUSTOMER
    rule_id: BR-002
    condition: { field: customer_tier, op: "==", value: strategic }
    approver_role: NONE
  - id: PROCUREMENT_CAPEX
    rule_id: BR-004
    condition: { field: amount, op: ">=", value: 5000000 }
    approver_role: FINANCE
  - id: PROCUREMENT_STANDARD
    rule_id: BR-001
    condition: { field: amount, op: ">=", value: 1000000 }
    approver_role: MANAGER
"""


@pytest.fixture
def config():
    return default_config()


def request_for(amount, **kwargs):
    return PurchaseRequest(id=kwargs.pop("id", "PO-TEST"), amount=amount, **kwargs)


# --- BR-001 -----------------------------------------------------------------


def test_a_small_request_is_approved_automatically(config):
    assert route_request(request_for(250_000), config) == AUTO_APPROVED


def test_a_request_at_the_threshold_needs_a_manager(config):
    """At or above, not merely above. Off-by-one here is a real policy change."""
    assert route_request(request_for(1_000_000), config) == MANAGER_APPROVAL


def test_a_request_just_below_the_threshold_does_not(config):
    """Reads the threshold instead of restating it.

    The contrast with TEST-OLD-001..003 below is the point: this assertion
    survives a policy change because it asks the config what the policy is.
    A suite where every test did this would have nothing to seed.
    """
    assert route_request(request_for(config.threshold("BR-001") - 1), config) == AUTO_APPROVED


def test_the_decision_names_the_rule_that_produced_it(config):
    threshold = config.threshold("BR-001")
    decision = evaluate_approval(request_for(threshold + 500_000), config)
    assert decision.rule_id == "BR-001"
    assert f"{threshold:,}" in decision.reason


# --- BR-002, BR-004, BR-005 -------------------------------------------------


def test_a_strategic_customer_skips_approval(config):
    assert route_request(request_for(3_000_000, customer_tier="strategic"), config) == AUTO_APPROVED


def test_capital_expenditure_goes_to_finance(config):
    assert route_request(request_for(6_000_000, category="capex"), config) == FINANCE_APPROVAL


def test_a_high_risk_supplier_escalates_whatever_the_amount(config):
    assert route_request(request_for(50_000, supplier_risk="high"), config) == DIRECTOR_APPROVAL


def test_risk_escalation_beats_the_strategic_exemption(config):
    """Rule order, asserted. Reversing these two would let a strategic
    account route a high-risk supplier past the director."""
    assert route_request(
        request_for(50_000, customer_tier="strategic", supplier_risk="high"), config
    ) == DIRECTOR_APPROVAL


# --- workflow ---------------------------------------------------------------


def test_the_workflow_routes_the_same_way_as_the_code_for_a_standard_request(config):
    routes = load_routes(ROUTES_YAML)
    req = request_for(1_500_000)
    assert route_by_workflow(req, routes).outcome == route_request(req, config)


def test_first_matching_route_wins():
    routes = load_routes(ROUTES_YAML)
    req = request_for(6_000_000, supplier_risk="high")
    assert route_by_workflow(req, routes).outcome == DIRECTOR_APPROVAL


# --- side effects -----------------------------------------------------------


def test_an_auto_approved_request_notifies_nobody(config):
    log = NotificationLog()
    assert log.notify("PO-1", evaluate_approval(request_for(250_000), config)) is None
    assert log.count() == 0


def test_a_manager_approval_notifies_the_manager(config):
    log = NotificationLog()
    message = log.notify("PO-1", evaluate_approval(request_for(2_000_000), config))
    assert message["to"] == "procurement.manager@acme.example"


def test_the_audit_trail_records_the_rule_and_the_policy_version(config):
    trail = AuditTrail()
    req = request_for(2_000_000)
    entry = trail.record(req, evaluate_approval(req, config), policy_version=config.version)
    assert entry["rule_id"] == "BR-001"
    assert entry["policy_version"] == 3


# --- BR-006, the emergency path ---------------------------------------------


def test_an_emergency_purchase_is_fast_tracked(config):
    assert route_emergency(request_for(400_000, process="emergency_purchase"), config) == AUTO_APPROVED


def test_an_emergency_purchase_above_the_ceiling_needs_a_manager(config):
    assert route_emergency(
        request_for(2_000_000, process="emergency_purchase"), config
    ) == MANAGER_APPROVAL


# --- TEST-OLD-001..003: green today, wrong tomorrow -------------------------
#
# Each of these encodes "the threshold is one million" as a fact about the
# world rather than reading it from configuration. That is exactly how real
# suites rot: the assertion was true when it was written, and nothing about
# it looks like a liability until the policy moves.


@pytest.mark.seed_outdated
def test_old_001_requests_under_a_million_are_always_automatic(config):
    """TEST-OLD-001. Reads as a rule; is actually a snapshot of one."""
    for amount in (600_000, 750_000, 900_000):
        assert route_request(request_for(amount), config) == AUTO_APPROVED


@pytest.mark.seed_outdated
def test_old_002_a_seven_hundred_fifty_thousand_request_needs_no_approval(config):
    """TEST-OLD-002. The single value in the middle of the affected range."""
    assert route_request(request_for(750_000), config) == AUTO_APPROVED


@pytest.mark.seed_outdated
def test_old_003_managers_see_nothing_below_one_million(config):
    """TEST-OLD-003. Asserts the absence of a notification, which is the
    hardest kind of staleness to spot by reading."""
    log = NotificationLog()
    for amount in (500_000, 800_000, 999_000):
        log.notify("PO-X", evaluate_approval(request_for(amount), config))
    assert log.count() == 0
