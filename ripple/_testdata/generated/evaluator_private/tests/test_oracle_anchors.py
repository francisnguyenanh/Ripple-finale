"""Tests verifying that the Independent Oracle strictly matches FROZEN_SPEC anchors."""

import json
from pathlib import Path
import pytest
from evaluator_private.oracle import IndependentOracle


@pytest.fixture
def anchors():
    anchors_path = Path(__file__).resolve().parent.parent / "anchors.json"
    with open(anchors_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_routing_anchors(anchors):
    for scenario_id in ("S0", "S1", "S2"):
        oracle = IndependentOracle(scenario_id=scenario_id)
        for anchor in anchors["routing_anchors"]:
            expected_route = anchor["expected"][scenario_id]
            actual = oracle.evaluate_route(
                amount_jpy=anchor["amount"],
                process=anchor["process"],
                category=anchor["category"],
                supplier_status=anchor["supplier_status"],
                supplier_risk=anchor["supplier_risk"],
            )
            assert actual == expected_route, f"Mismatch in {anchor['name']} for scenario {scenario_id}: got {actual}, expected {expected_route}"


def test_invoice_anchors(anchors):
    oracle = IndependentOracle(scenario_id="S0")
    for anchor in anchors["invoice_anchors"]:
        status, due = oracle.evaluate_invoice(
            po_goods_net=anchor["po_goods"],
            inv_goods_net=anchor["inv_goods"],
            po_state=anchor["po_state"],
            po_quantity=anchor["po_qty"],
            inv_quantity=anchor["inv_qty"],
            accepted_receipt_qty=anchor["accepted_qty"],
            is_duplicate=anchor["is_duplicate"],
        )
        assert status == anchor["expected_status"], f"Mismatch in {anchor['name']}: got {status}, expected {anchor['expected_status']}"
        if "expected_due_date" in anchor:
            assert due == anchor["expected_due_date"], f"Due date mismatch in {anchor['name']}"
