"""Independent expected evaluator for Harbor Procurement Mini (Stage 06).

Implements declarative decision tables and pure spec predicates without
importing application code or public modules.
"""

from datetime import date, timedelta
from typing import Any, Optional


class IndependentOracle:
    """Independent expected evaluator derived directly from FROZEN_SPEC v1."""

    def __init__(self, scenario_id: str = "S0"):
        self.scenario_id = scenario_id.upper()

        # Resolve Manager Threshold based on scenario
        if self.scenario_id == "S0":
            # Baseline observed implementation: standard 1M, emergency hardcoded 1M
            self.standard_manager_threshold = 1_000_000
            self.emergency_manager_threshold = 1_000_000
        elif self.scenario_id == "S1":
            # S1 human approval sets 500k for both standard and emergency
            self.standard_manager_threshold = 500_000
            self.emergency_manager_threshold = 500_000
        elif self.scenario_id == "S2":
            # S2 human approval sets 750k for both
            self.standard_manager_threshold = 750_000
            self.emergency_manager_threshold = 750_000
        elif self.scenario_id == "S3":
            # Restore S0
            self.standard_manager_threshold = 1_000_000
            self.emergency_manager_threshold = 1_000_000
        elif self.scenario_id in ("S4", "S5"):
            # S4/S5 inherit S1 managerial threshold
            self.standard_manager_threshold = 500_000
            self.emergency_manager_threshold = 500_000
        else:
            raise ValueError(f"Unknown scenario ID: {scenario_id}")

        self.capex_finance_threshold = 2_000_000
        self.price_tolerance_fixed = 10_000
        self.price_tolerance_percent = 2

    def evaluate_route(
        self,
        amount_jpy: int,
        process: str,
        category: str,
        supplier_status: str = "ACTIVE",
        supplier_risk: str = "NORMAL",
        currency: str = "JPY",
        quantity: int = 1,
    ) -> str:
        """Declarative routing evaluation."""
        # 1. Validation check
        if isinstance(amount_jpy, bool) or not isinstance(amount_jpy, int) or amount_jpy <= 0:
            return "REJECTED"
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return "REJECTED"
        if currency != "JPY":
            return "REJECTED"

        # 2. Supplier Constraints
        if supplier_status == "BLOCKED":
            return "REJECTED"
        if supplier_risk == "HIGH":
            return "NEEDS_DIRECTOR"

        # 3. Category Constraints
        if category == "CAPEX" and amount_jpy >= self.capex_finance_threshold:
            return "NEEDS_FINANCE"

        # 4. Threshold check
        if process == "EMERGENCY":
            if amount_jpy >= self.emergency_manager_threshold:
                return "NEEDS_MANAGER"
            return "AUTO"
        else:
            if amount_jpy >= self.standard_manager_threshold:
                return "NEEDS_MANAGER"
            return "AUTO"

    def evaluate_invoice(
        self,
        po_goods_net: int,
        inv_goods_net: int,
        po_state: str,
        po_quantity: int,
        inv_quantity: int,
        accepted_receipt_qty: int,
        is_duplicate: bool,
        received_date: str = "2026-09-01",
    ) -> tuple[str, Optional[str]]:
        """Declarative 3-way matching evaluation returning (status, due_date)."""
        # Duplicate check
        if is_duplicate:
            return "REJECTED", None

        # PO state
        if po_state != "APPROVED":
            return "HOLD_UNAPPROVED", None

        # Quantity
        if inv_quantity != po_quantity or accepted_receipt_qty < inv_quantity:
            return "HOLD_QUANTITY", None

        # Price tolerance
        diff = abs(inv_goods_net - po_goods_net)
        fixed_ok = diff <= self.price_tolerance_fixed
        pct_ok = (100 * diff) <= (self.price_tolerance_percent * po_goods_net)
        if not (fixed_ok and pct_ok):
            return "HOLD_PRICE", None

        # Due date calculation
        dt = date.fromisoformat(received_date)
        due = (dt + timedelta(days=30)).isoformat()
        return "READY", due
