"""Tests for 3-way invoice matching and duplicate prevention."""

import pytest
from app.enums import InvoiceStatus, POState, ReceiptCondition
from app.matching import evaluate_invoice
from app.models import ApprovalConfig, Invoice, PO, Receipt


@pytest.fixture
def base_config() -> ApprovalConfig:
    return ApprovalConfig(
        version="1.0.0",
        currency="JPY",
        standard_manager_threshold_jpy=1_000_000,
        capex_finance_threshold_jpy=2_000_000,
        high_risk_route="NEEDS_DIRECTOR",
        price_tolerance_fixed_jpy=10_000,
        price_tolerance_percent=2,
        payment_term_days=30,
    )


@pytest.fixture
def base_po() -> PO:
    return PO(
        po_id="PO-000001",
        request_id="REQ-000001",
        supplier_id="SUP-001",
        currency="JPY",
        quantity=10,
        goods_net_jpy=400_000,
        freight_jpy=2_000,
        tax_jpy=40_000,
        gross_jpy=442_000,
        state=POState.APPROVED,
        approved_by="EMP-002",
        approved_at="2026-09-01T11:00:00",
    )


@pytest.fixture
def base_receipts() -> list[Receipt]:
    return [
        Receipt("REC-000001", "PO-000001", 10, "2026-09-01T14:00:00", "EMP-003", ReceiptCondition.ACCEPTED)
    ]


def test_invoice_duplicate_detection(base_config, base_po, base_receipts):
    seen = {"SUP-001:INV-2026-001"}
    inv = Invoice(
        invoice_id="INV-000001",
        supplier_id="SUP-001",
        supplier_invoice_number=" inv-2026-001 ",  # whitespace and lowercase
        po_id="PO-000001",
        quantity=10,
        goods_net_jpy=400_000,
        freight_jpy=2_000,
        tax_jpy=40_000,
        gross_jpy=442_000,
        currency="JPY",
        received_date="2026-09-01",
        issue_date="2026-09-01",
    )
    decision = evaluate_invoice(inv, base_po, base_receipts, seen, base_config)
    assert decision.status == InvoiceStatus.REJECTED
    assert "DUPLICATE" in decision.reason_code


def test_unapproved_po_held(base_config, base_receipts):
    draft_po = PO(
        po_id="PO-000002",
        request_id="REQ-000002",
        supplier_id="SUP-001",
        currency="JPY",
        quantity=10,
        goods_net_jpy=400_000,
        freight_jpy=2_000,
        tax_jpy=40_000,
        gross_jpy=442_000,
        state=POState.DRAFT,
    )
    inv = Invoice(
        invoice_id="INV-000002",
        supplier_id="SUP-001",
        supplier_invoice_number="INV-NEW-001",
        po_id="PO-000002",
        quantity=10,
        goods_net_jpy=400_000,
        freight_jpy=2_000,
        tax_jpy=40_000,
        gross_jpy=442_000,
        currency="JPY",
        received_date="2026-09-01",
        issue_date="2026-09-01",
    )
    decision = evaluate_invoice(inv, draft_po, base_receipts, set(), base_config)
    assert decision.status == InvoiceStatus.HOLD_UNAPPROVED


def test_quantity_mismatch_and_damaged_receipts(base_config, base_po):
    # Receipts with damaged items: only 8 accepted, 2 damaged
    receipts = [
        Receipt("REC-000001", "PO-000001", 8, "2026-09-01T14:00:00", "EMP-003", ReceiptCondition.ACCEPTED),
        Receipt("REC-000002", "PO-000001", 2, "2026-09-01T14:05:00", "EMP-003", ReceiptCondition.DAMAGED),
    ]
    inv = Invoice(
        invoice_id="INV-000003",
        supplier_id="SUP-001",
        supplier_invoice_number="INV-NEW-002",
        po_id="PO-000001",
        quantity=10,  # invoice bills 10
        goods_net_jpy=400_000,
        freight_jpy=2_000,
        tax_jpy=40_000,
        gross_jpy=442_000,
        currency="JPY",
        received_date="2026-09-01",
        issue_date="2026-09-01",
    )
    decision = evaluate_invoice(inv, base_po, receipts, set(), base_config)
    assert decision.status == InvoiceStatus.HOLD_QUANTITY


def test_price_tolerance_boundary_anchors(base_config, base_po, base_receipts):
    # PO goods net = 400,000 JPY. 2% tolerance = 8,000 JPY. Fixed limit = 10,000 JPY.
    # Case 1: 408,000 JPY -> diff 8,000 <= 8,000 -> READY
    inv_pass = Invoice(
        invoice_id="INV-000004",
        supplier_id="SUP-001",
        supplier_invoice_number="INV-P-01",
        po_id="PO-000001",
        quantity=10,
        goods_net_jpy=408_000,
        freight_jpy=2_000,
        tax_jpy=40_800,
        gross_jpy=450_800,
        currency="JPY",
        received_date="2026-09-01",
        issue_date="2026-09-01",
    )
    decision_pass = evaluate_invoice(inv_pass, base_po, base_receipts, set(), base_config)
    assert decision_pass.status == InvoiceStatus.READY
    assert decision_pass.due_date == "2026-10-01"

    # Case 2: 408,001 JPY -> diff 8,001 > 8,000 -> HOLD_PRICE
    inv_fail = Invoice(
        invoice_id="INV-000005",
        supplier_id="SUP-001",
        supplier_invoice_number="INV-P-02",
        po_id="PO-000001",
        quantity=10,
        goods_net_jpy=408_001,
        freight_jpy=2_000,
        tax_jpy=40_800,
        gross_jpy=450_801,
        currency="JPY",
        received_date="2026-09-01",
        issue_date="2026-09-01",
    )
    decision_fail = evaluate_invoice(inv_fail, base_po, base_receipts, set(), base_config)
    assert decision_fail.status == InvoiceStatus.HOLD_PRICE
