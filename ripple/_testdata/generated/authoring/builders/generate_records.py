"""Deterministic synthetic enterprise data generator for Harbor Procurement Mini.

Generates:
- 20 Suppliers (2 Blocked, 3 High Risk, 15 Normal Active)
- 8 Employees
- 240 Requisitions (with mandatory S0->S1->S2 witnesses and threshold boundaries)
- 120 Purchase Orders (derived from non-rejected requisitions)
- 140 Receipt events (100 single-event POs, 20 two-event split POs)
- 120 Business Invoices (including price tolerance boundaries)
- 24 Private validation edge cases (saved separately outside public input)
- Private case allocation catalog
"""

import csv
import json
import random
from pathlib import Path
from typing import Any


def generate_all_data(
    output_dir: Path,
    private_val_path: Path,
    catalog_path: Path,
    seed: int = 20260925,
    ref_date: str = "2026-09-01",
) -> dict[str, int]:
    rng = random.Random(seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    private_val_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)

    catalog: dict[str, Any] = {
        "seed": seed,
        "reference_date": ref_date,
        "counts": {},
        "witness_groups": {},
        "hold_cases": {},
    }

    # 1. Suppliers (20 total: 2 BLOCKED, 3 HIGH risk, 15 NORMAL active)
    suppliers = []
    supplier_status_map = {}
    for i in range(1, 21):
        sup_id = f"SUP-{i:03d}"
        if i in (19, 20):
            status = "BLOCKED"
            risk = "NORMAL"
        elif i in (16, 17, 18):
            status = "ACTIVE"
            risk = "HIGH"
        else:
            status = "ACTIVE"
            risk = "NORMAL"

        name = f"Harbor Partner {i:02d} Ltd"
        email = f"contact@partner{i:02d}.example"
        suppliers.append({
            "supplier_id": sup_id,
            "display_name": name,
            "status": status,
            "risk": risk,
            "contact_email": email,
        })
        supplier_status_map[sup_id] = (status, risk)

    sup_csv = output_dir / "suppliers.csv"
    with open(sup_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["supplier_id", "display_name", "status", "risk", "contact_email"])
        writer.writeheader()
        writer.writerows(suppliers)

    # 2. Employees (8 total)
    employees = [
        {"employee_id": "EMP-001", "display_name": "Taro Tanaka", "department": "Procurement", "roles": "BUYER"},
        {"employee_id": "EMP-002", "display_name": "Hanako Sato", "department": "Operations", "roles": "BUYER,MANAGER"},
        {"employee_id": "EMP-003", "display_name": "Kenji Suzuki", "department": "Logistics", "roles": "BUYER"},
        {"employee_id": "EMP-004", "display_name": "Yuka Takahashi", "department": "Finance", "roles": "FINANCE"},
        {"employee_id": "EMP-005", "display_name": "Ichiro Watanabe", "department": "Executive", "roles": "DIRECTOR"},
        {"employee_id": "EMP-006", "display_name": "Naomi Ito", "department": "Procurement", "roles": "BUYER"},
        {"employee_id": "EMP-007", "display_name": "Shigeru Kobayashi", "department": "Operations", "roles": "MANAGER"},
        {"employee_id": "EMP-008", "display_name": "Emi Yamamoto", "department": "Finance", "roles": "FINANCE"},
    ]
    emp_csv = output_dir / "employees.csv"
    with open(emp_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["employee_id", "display_name", "department", "roles"])
        writer.writeheader()
        writer.writerows(employees)

    # 3. Requisitions (240 total)
    # Mandatory Witness Groups:
    # - 12 STANDARD 600,000 JPY (OPEX, ACTIVE/NORMAL supplier)
    # - 12 EMERGENCY 600,000 JPY (OPEX, ACTIVE/NORMAL supplier)
    # - Boundaries: 499999, 500000, 500001, 749999, 750000, 750001, 999999, 1000000, 1000001, 1999999, 2000000, 2000001
    requisitions = []
    req_idx = 1

    # Group 1: 12 STANDARD 600k witnesses
    witness_std_600k = []
    for _ in range(12):
        r_id = f"REQ-{req_idx:06d}"
        sup_id = f"SUP-{(req_idx % 15) + 1:03d}"
        requisitions.append({
            "request_id": r_id,
            "requester_id": "EMP-001",
            "supplier_id": sup_id,
            "process": "STANDARD",
            "category": "OPEX",
            "net_amount_jpy": 600_000,
            "currency": "JPY",
            "requested_at": f"{ref_date}T09:{req_idx % 60:02d}:00",
            "quantity": 10,
            "item_description": f"Standard witness office pack {req_idx}",
        })
        witness_std_600k.append(r_id)
        req_idx += 1
    catalog["witness_groups"]["STANDARD_600K_WITNESSES"] = witness_std_600k

    # Group 2: 12 EMERGENCY 600k witnesses
    witness_emg_600k = []
    for _ in range(12):
        r_id = f"REQ-{req_idx:06d}"
        sup_id = f"SUP-{(req_idx % 15) + 1:03d}"
        requisitions.append({
            "request_id": r_id,
            "requester_id": "EMP-003",
            "supplier_id": sup_id,
            "process": "EMERGENCY",
            "category": "OPEX",
            "net_amount_jpy": 600_000,
            "currency": "JPY",
            "requested_at": f"{ref_date}T09:{req_idx % 60:02d}:00",
            "quantity": 10,
            "item_description": f"Emergency witness office pack {req_idx}",
        })
        witness_emg_600k.append(r_id)
        req_idx += 1
    catalog["witness_groups"]["EMERGENCY_600K_WITNESSES"] = witness_emg_600k

    # Group 3: Boundary witnesses (24 boundary items)
    boundary_values = [
        499_999, 500_000, 500_001,
        749_999, 750_000, 750_001,
        999_999, 1_000_000, 1_000_001,
        1_999_999, 2_000_000, 2_000_001
    ]
    boundary_reqs = []
    for b_val in boundary_values:
        # Standard OPEX
        r_id1 = f"REQ-{req_idx:06d}"
        requisitions.append({
            "request_id": r_id1,
            "requester_id": "EMP-001",
            "supplier_id": f"SUP-{(req_idx % 15) + 1:03d}",
            "process": "STANDARD",
            "category": "OPEX",
            "net_amount_jpy": b_val,
            "currency": "JPY",
            "requested_at": f"{ref_date}T10:{req_idx % 60:02d}:00",
            "quantity": 5,
            "item_description": f"Boundary Standard OPEX {b_val}",
        })
        boundary_reqs.append(r_id1)
        req_idx += 1

        # Standard CAPEX
        r_id2 = f"REQ-{req_idx:06d}"
        requisitions.append({
            "request_id": r_id2,
            "requester_id": "EMP-002",
            "supplier_id": f"SUP-{(req_idx % 15) + 1:03d}",
            "process": "STANDARD",
            "category": "CAPEX",
            "net_amount_jpy": b_val,
            "currency": "JPY",
            "requested_at": f"{ref_date}T10:{req_idx % 60:02d}:00",
            "quantity": 5,
            "item_description": f"Boundary Standard CAPEX {b_val}",
        })
        boundary_reqs.append(r_id2)
        req_idx += 1
    catalog["witness_groups"]["BOUNDARY_REQS"] = boundary_reqs

    # Group 4: High risk & Blocked supplier controls (12 items)
    control_reqs = []
    for i in range(16, 19):  # High risk
        r_id = f"REQ-{req_idx:06d}"
        requisitions.append({
            "request_id": r_id,
            "requester_id": "EMP-001",
            "supplier_id": f"SUP-{i:03d}",
            "process": "STANDARD",
            "category": "OPEX",
            "net_amount_jpy": 300_000,
            "currency": "JPY",
            "requested_at": f"{ref_date}T11:00:00",
            "quantity": 2,
            "item_description": "High risk supplier control OPEX",
        })
        control_reqs.append(r_id)
        req_idx += 1

        r_id2 = f"REQ-{req_idx:06d}"
        requisitions.append({
            "request_id": r_id2,
            "requester_id": "EMP-001",
            "supplier_id": f"SUP-{i:03d}",
            "process": "STANDARD",
            "category": "CAPEX",
            "net_amount_jpy": 2_500_000,
            "currency": "JPY",
            "requested_at": f"{ref_date}T11:10:00",
            "quantity": 2,
            "item_description": "High risk supplier control CAPEX",
        })
        control_reqs.append(r_id2)
        req_idx += 1

    for i in (19, 20):  # Blocked
        for amt in (150_000, 2_200_000, 50_000):
            r_id = f"REQ-{req_idx:06d}"
            requisitions.append({
                "request_id": r_id,
                "requester_id": "EMP-001",
                "supplier_id": f"SUP-{i:03d}",
                "process": "STANDARD",
                "category": "OPEX",
                "net_amount_jpy": amt,
                "currency": "JPY",
                "requested_at": f"{ref_date}T11:20:00",
                "quantity": 2,
                "item_description": "Blocked supplier control",
            })
            control_reqs.append(r_id)
            req_idx += 1
    catalog["witness_groups"]["CONTROL_REQS"] = control_reqs

    # Fill remaining requisitions up to 240
    categories = ["OPEX", "CAPEX"]
    processes = ["STANDARD", "EMERGENCY"]
    requesters = ["EMP-001", "EMP-002", "EMP-003", "EMP-006"]
    while req_idx <= 240:
        r_id = f"REQ-{req_idx:06d}"
        sup_num = rng.randint(1, 15)  # Use normal active suppliers
        cat = rng.choice(categories)
        proc = rng.choice(processes)
        amt = rng.randint(5, 300) * 10_000
        qty = rng.randint(1, 20)
        requisitions.append({
            "request_id": r_id,
            "requester_id": rng.choice(requesters),
            "supplier_id": f"SUP-{sup_num:03d}",
            "process": proc,
            "category": cat,
            "net_amount_jpy": amt,
            "currency": "JPY",
            "requested_at": f"{ref_date}T13:{req_idx % 60:02d}:00",
            "quantity": qty,
            "item_description": f"General supplies item {req_idx}",
        })
        req_idx += 1

    req_jsonl = output_dir / "requisitions.jsonl"
    with open(req_jsonl, "w", encoding="utf-8") as f:
        for r in requisitions:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 4. Purchase Orders (120 total)
    # Selected strictly from non-rejected requisitions
    # Blocked suppliers (SUP-019, SUP-020) must NEVER get POs!
    eligible_reqs = [
        r for r in requisitions
        if supplier_status_map[r["supplier_id"]][0] == "ACTIVE"
    ]
    # Pick 120 eligible requisitions
    selected_reqs = eligible_reqs[:120]

    pos = []
    po_approver_map = {
        "MANAGER": "EMP-002",
        "FINANCE": "EMP-004",
        "DIRECTOR": "EMP-005",
    }

    draft_pos_set = set()
    for idx, r in enumerate(selected_reqs, start=1):
        po_id = f"PO-{idx:06d}"
        goods_net = r["net_amount_jpy"]
        freight = 0 if goods_net >= 500_000 else 2_000
        tax = (goods_net * 10) // 100
        gross = goods_net + freight + tax

        # 10 intentional DRAFT POs (to test HOLD_UNAPPROVED when invoices arrive)
        if idx in range(111, 121):
            state = "DRAFT"
            approved_by = None
            approved_at = None
            draft_pos_set.add(po_id)
        else:
            state = "APPROVED"
            # Assign appropriate approver
            sup_risk = supplier_status_map[r["supplier_id"]][1]
            if sup_risk == "HIGH":
                approved_by = po_approver_map["DIRECTOR"]
            elif r["category"] == "CAPEX" and goods_net >= 2_000_000:
                approved_by = po_approver_map["FINANCE"]
            elif goods_net >= 1_000_000:
                approved_by = po_approver_map["MANAGER"]
            else:
                approved_by = "SYSTEM_AUTO"
            approved_at = f"{ref_date}T14:{idx % 60:02d}:00"

        pos.append({
            "po_id": po_id,
            "request_id": r["request_id"],
            "supplier_id": r["supplier_id"],
            "currency": "JPY",
            "quantity": r["quantity"],
            "goods_net_jpy": goods_net,
            "freight_jpy": freight,
            "tax_jpy": tax,
            "gross_jpy": gross,
            "state": state,
            "approved_by": approved_by,
            "approved_at": approved_at,
        })
    catalog["hold_cases"]["DRAFT_PO_IDS"] = sorted(list(draft_pos_set))

    po_jsonl = output_dir / "purchase_orders.jsonl"
    with open(po_jsonl, "w", encoding="utf-8") as f:
        for po in pos:
            f.write(json.dumps(po, ensure_ascii=False) + "\n")

    # 5. Goods Receipts (140 total: 100 single-event POs, 20 two-event POs)
    # The 20 two-event POs must have quantity >= 2
    receipts = []
    rec_idx = 1

    # First 20 POs get 2 receipt events
    split_pos = pos[:20]
    single_pos = pos[20:120]  # 100 POs

    damaged_receipt_pos = set()

    for po in split_pos:
        qty = po["quantity"]
        q1 = max(1, qty // 2)
        q2 = qty - q1
        # Event 1
        r_id1 = f"REC-{rec_idx:06d}"
        receipts.append({
            "receipt_id": r_id1,
            "po_id": po["po_id"],
            "quantity_received": q1,
            "received_at": f"{ref_date}T15:00:00",
            "received_by": "EMP-003",
            "condition": "ACCEPTED",
        })
        rec_idx += 1

        # Event 2 (inject 2 damaged cases)
        r_id2 = f"REC-{rec_idx:06d}"
        cond = "DAMAGED" if po["po_id"] in ("PO-000001", "PO-000002") else "ACCEPTED"
        if cond == "DAMAGED":
            damaged_receipt_pos.add(po["po_id"])
        receipts.append({
            "receipt_id": r_id2,
            "po_id": po["po_id"],
            "quantity_received": q2,
            "received_at": f"{ref_date}T15:30:00",
            "received_by": "EMP-003",
            "condition": cond,
        })
        rec_idx += 1

    for po in single_pos:
        r_id = f"REC-{rec_idx:06d}"
        cond = "DAMAGED" if po["po_id"] in ("PO-000025", "PO-000026") else "ACCEPTED"
        if cond == "DAMAGED":
            damaged_receipt_pos.add(po["po_id"])
        receipts.append({
            "receipt_id": r_id,
            "po_id": po["po_id"],
            "quantity_received": po["quantity"],
            "received_at": f"{ref_date}T16:00:00",
            "received_by": "EMP-003",
            "condition": cond,
        })
        rec_idx += 1

    catalog["hold_cases"]["DAMAGED_RECEIPT_PO_IDS"] = sorted(list(damaged_receipt_pos))

    rec_jsonl = output_dir / "receipts.jsonl"
    with open(rec_jsonl, "w", encoding="utf-8") as f:
        for rec in receipts:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 6. Business Invoices (120 total, 1 per PO)
    # Deliberately seed price tolerance edge cases:
    # Anchor 1: PO goods net 400,000 -> inv 408,000 (+8,000, 2%) -> READY
    # Anchor 2: PO goods net 400,000 -> inv 408,001 (+8,001, >2%) -> HOLD_PRICE
    # Anchor 3: PO goods net 1,000,000 -> inv 1,010,000 (+10,000, <=10k & 1%) -> READY
    # Anchor 4: PO goods net 1,000,000 -> inv 1,010,001 (+10,001, >10k) -> HOLD_PRICE
    invoices = []
    price_hold_invs = set()

    for idx, po in enumerate(pos, start=1):
        inv_id = f"INV-{idx:06d}"
        po_goods = po["goods_net_jpy"]
        po_qty = po["quantity"]

        # Synthetic price deviation injection
        if idx == 5:
            # Boundary exact 2% pass (assume po_goods is adjusted or tested)
            diff = min(10_000, (po_goods * 2) // 100)
            inv_goods = po_goods + diff
        elif idx == 6:
            # Boundary fail (+diff + 1)
            diff = min(10_000, (po_goods * 2) // 100) + 1
            inv_goods = po_goods + diff
            price_hold_invs.add(inv_id)
        elif idx in (7, 8):
            # Moderate price deviation fail
            inv_goods = po_goods + 15_000
            price_hold_invs.add(inv_id)
        else:
            inv_goods = po_goods

        inv_freight = po["freight_jpy"]
        inv_tax = (inv_goods * 10) // 100
        inv_gross = inv_goods + inv_freight + inv_tax

        invoices.append({
            "invoice_id": inv_id,
            "supplier_id": po["supplier_id"],
            "supplier_invoice_number": f"INV-HARBOR-{po['supplier_id']}-{idx:04d}",
            "po_id": po["po_id"],
            "quantity": po_qty,
            "goods_net_jpy": inv_goods,
            "freight_jpy": inv_freight,
            "tax_jpy": inv_tax,
            "gross_jpy": inv_gross,
            "currency": "JPY",
            "received_date": ref_date,
            "issue_date": ref_date,
        })
    catalog["hold_cases"]["PRICE_HOLD_INVOICE_IDS"] = sorted(list(price_hold_invs))

    inv_jsonl = output_dir / "invoices.jsonl"
    with open(inv_jsonl, "w", encoding="utf-8") as f:
        for inv in invoices:
            f.write(json.dumps(inv, ensure_ascii=False) + "\n")

    # 7. Private Validation Set (24 edge cases in evaluator_private/validation/edge_cases_24.jsonl)
    edge_cases = []
    # 6 Duplicate key tests
    for i in range(1, 7):
        edge_cases.append({
            "case_type": "DUPLICATE_KEY",
            "invoice_id": f"VAL-DUP-{i:03d}",
            "supplier_id": "SUP-001",
            "supplier_invoice_number": f"inv-harbor-sup-001-0001",  # Duplicate of first invoice
            "po_id": "PO-000001",
            "quantity": 10,
            "goods_net_jpy": 400_000,
            "currency": "JPY",
            "expected_status": "REJECTED",
        })
    # 6 Non-existent or mismatched POs
    for i in range(1, 7):
        edge_cases.append({
            "case_type": "PO_MISMATCH",
            "invoice_id": f"VAL-MIS-{i:03d}",
            "supplier_id": "SUP-002",
            "supplier_invoice_number": f"INV-MIS-{i:03d}",
            "po_id": "PO-999999",
            "quantity": 5,
            "goods_net_jpy": 100_000,
            "currency": "JPY",
            "expected_status": "REJECTED",
        })
    # 6 Currency and negative value violations
    for i in range(1, 7):
        curr = "USD" if i % 2 == 0 else "EUR"
        edge_cases.append({
            "case_type": "CURRENCY_VIOLATION",
            "invoice_id": f"VAL-CURR-{i:03d}",
            "supplier_id": "SUP-003",
            "supplier_invoice_number": f"INV-CURR-{i:03d}",
            "po_id": "PO-000003",
            "quantity": 5,
            "goods_net_jpy": 100_000,
            "currency": curr,
            "expected_status": "REJECTED",
        })
    # 6 Extreme quantity / price mismatch
    for i in range(1, 7):
        edge_cases.append({
            "case_type": "EXTREME_PRICE_MISMATCH",
            "invoice_id": f"VAL-PRC-{i:03d}",
            "supplier_id": "SUP-004",
            "supplier_invoice_number": f"INV-PRC-{i:03d}",
            "po_id": "PO-000004",
            "quantity": 1000,
            "goods_net_jpy": 50_000_000,
            "currency": "JPY",
            "expected_status": "HOLD_QUANTITY",
        })

    with open(private_val_path, "w", encoding="utf-8") as f:
        for ec in edge_cases:
            f.write(json.dumps(ec, ensure_ascii=False) + "\n")

    # Record final counts
    catalog["counts"] = {
        "suppliers": len(suppliers),
        "employees": len(employees),
        "requisitions": len(requisitions),
        "purchase_orders": len(pos),
        "receipts": len(receipts),
        "invoices": len(invoices),
        "private_edge_cases": len(edge_cases),
    }

    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    return catalog["counts"]


if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent.parent
    data_out = base / "bundles" / "harbor_demo" / "input" / "data"
    val_out = base / "evaluator_private" / "validation" / "edge_cases_24.jsonl"
    cat_out = base / "authoring" / "builders" / "case_catalog.json"
    res = generate_all_data(data_out, val_out, cat_out)
    print("Generated data counts:", res)
