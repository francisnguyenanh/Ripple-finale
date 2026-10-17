"""End-to-end characterization tests for CLI commands."""

import json
from pathlib import Path
from app.cli import main


def test_cli_route_command(tmp_path: Path):
    req_file = tmp_path / "requisitions.jsonl"
    cfg_file = tmp_path / "approval.yaml"
    out_file = tmp_path / "routes.jsonl"

    req_file.write_text(
        json.dumps({
            "request_id": "REQ-0001",
            "requester_id": "EMP-001",
            "supplier_id": "SUP-001",
            "process": "STANDARD",
            "category": "OPEX",
            "net_amount_jpy": 500000,
            "currency": "JPY",
            "requested_at": "2026-09-01T10:00:00",
            "quantity": 1,
            "item_description": "Office Chairs"
        }) + "\n",
        encoding="utf-8"
    )

    cfg_file.write_text(
        "version: '1.0.0'\n"
        "currency: 'JPY'\n"
        "routing_rules:\n"
        "  standard_manager_threshold_jpy: 1000000\n"
        "  capex_finance_threshold_jpy: 2000000\n"
        "  high_risk_route: 'NEEDS_DIRECTOR'\n"
        "matching_rules:\n"
        "  price_tolerance_fixed_jpy: 10000\n"
        "  price_tolerance_percent: 2\n"
        "  payment_term_days: 30\n",
        encoding="utf-8"
    )

    ret = main(["route", "--requests", str(req_file), "--config", str(cfg_file), "--output", str(out_file)])
    assert ret == 0
    assert out_file.is_file()

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    decision = json.loads(lines[0])
    assert decision["request_id"] == "REQ-0001"
    assert decision["route"] == "AUTO"


def test_cli_match_command(tmp_path: Path):
    cfg_file = tmp_path / "approval.yaml"
    cfg_file.write_text(
        "version: '1.0.0'\n"
        "currency: 'JPY'\n"
        "routing_rules:\n"
        "  standard_manager_threshold_jpy: 1000000\n"
        "  capex_finance_threshold_jpy: 2000000\n"
        "  high_risk_route: 'NEEDS_DIRECTOR'\n"
        "matching_rules:\n"
        "  price_tolerance_fixed_jpy: 10000\n"
        "  price_tolerance_percent: 2\n"
        "  payment_term_days: 30\n",
        encoding="utf-8"
    )

    po_file = tmp_path / "purchase_orders.jsonl"
    po_file.write_text(
        json.dumps({
            "po_id": "PO-0001",
            "request_id": "REQ-0001",
            "supplier_id": "SUP-001",
            "currency": "JPY",
            "quantity": 5,
            "goods_net_jpy": 500000,
            "freight_jpy": 0,
            "tax_jpy": 50000,
            "gross_jpy": 550000,
            "state": "APPROVED"
        }) + "\n",
        encoding="utf-8"
    )

    rec_file = tmp_path / "receipts.jsonl"
    rec_file.write_text(
        json.dumps({
            "receipt_id": "REC-0001",
            "po_id": "PO-0001",
            "quantity_received": 5,
            "received_at": "2026-09-01T12:00:00",
            "received_by": "EMP-003",
            "condition": "ACCEPTED"
        }) + "\n",
        encoding="utf-8"
    )

    inv_file = tmp_path / "invoices.jsonl"
    inv_file.write_text(
        json.dumps({
            "invoice_id": "INV-0001",
            "supplier_id": "SUP-001",
            "supplier_invoice_number": "INV-SUP-01",
            "po_id": "PO-0001",
            "quantity": 5,
            "goods_net_jpy": 500000,
            "freight_jpy": 0,
            "tax_jpy": 50000,
            "gross_jpy": 550000,
            "currency": "JPY",
            "received_date": "2026-09-01",
            "issue_date": "2026-09-01"
        }) + "\n",
        encoding="utf-8"
    )

    out_file = tmp_path / "invoice_decisions.jsonl"
    ret = main(["match", "--data-dir", str(tmp_path), "--config", str(cfg_file), "--output", str(out_file)])
    assert ret == 0
    assert out_file.is_file()

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    decision = json.loads(lines[0])
    assert decision["invoice_id"] == "INV-0001"
    assert decision["status"] == "READY"
    assert decision["due_date"] == "2026-10-01"
