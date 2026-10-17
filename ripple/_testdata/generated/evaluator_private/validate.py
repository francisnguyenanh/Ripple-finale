"""Independent validation harness for Harbor Procurement Mini (Stage 06).

Executes reference app as an isolated subprocess to get actual outcomes,
compares them against independent oracle expectations, and produces a validation report.
"""

import json
import subprocess
import sys
from pathlib import Path

# Add root directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator_private.oracle import IndependentOracle


def run_app_cli(bundle_dir: Path, run_output_dir: Path) -> tuple[int, int]:
    run_output_dir.mkdir(parents=True, exist_ok=True)
    routes_out = run_output_dir / "routes.jsonl"
    match_out = run_output_dir / "invoice_decisions.jsonl"

    # 1. Run routing
    cmd_route = [
        sys.executable,
        "-m", "app.cli",
        "route",
        "--requests", "data/requisitions.jsonl",
        "--config", "config/approval.yaml",
        "--output", str(routes_out.resolve()),
    ]
    res_route = subprocess.run(cmd_route, cwd=bundle_dir, capture_output=True, text=True)
    if res_route.returncode != 0:
        print("CLI route failed:", res_route.stderr, file=sys.stderr)
        return 1, 0

    # 2. Run matching
    cmd_match = [
        sys.executable,
        "-m", "app.cli",
        "match",
        "--data-dir", "data",
        "--config", "config/approval.yaml",
        "--output", str(match_out.resolve()),
    ]
    res_match = subprocess.run(cmd_match, cwd=bundle_dir, capture_output=True, text=True)
    if res_match.returncode != 0:
        print("CLI match failed:", res_match.stderr, file=sys.stderr)
        return 0, 1

    return 0, 0


def validate_bundle(bundle_dir: Path, report_path: Path) -> dict[str, Any]:
    run_dir = bundle_dir / "run_output"
    rc_r, rc_m = run_app_cli(bundle_dir, run_dir)

    routes_out = run_dir / "routes.jsonl"
    match_out = run_dir / "invoice_decisions.jsonl"

    oracle = IndependentOracle(scenario_id="S0")

    # Load requisitions and suppliers
    reqs = []
    with open(bundle_dir / "data" / "requisitions.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                reqs.append(json.loads(line))

    suppliers = {}
    with open(bundle_dir / "data" / "suppliers.csv", "r", encoding="utf-8") as f:
        import csv
        for row in csv.DictReader(f):
            suppliers[row["supplier_id"]] = (row["status"], row["risk"])

    # Compare routing
    actual_routes = {}
    with open(routes_out, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                actual_routes[d["request_id"]] = d["route"]

    route_mismatches = []
    for r in reqs:
        r_id = r["request_id"]
        actual = actual_routes.get(r_id)
        sup_status, sup_risk = suppliers.get(r["supplier_id"], ("ACTIVE", "NORMAL"))
        expected = oracle.evaluate_route(
            amount_jpy=r["net_amount_jpy"],
            process=r["process"],
            category=r["category"],
            supplier_status=sup_status,
            supplier_risk=sup_risk,
            quantity=r["quantity"],
        )
        if actual != expected:
            route_mismatches.append({"request_id": r_id, "actual": actual, "expected": expected})

    # Compare invoices
    pos = {}
    with open(bundle_dir / "data" / "purchase_orders.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                po = json.loads(line)
                pos[po["po_id"]] = po

    receipts = []
    with open(bundle_dir / "data" / "receipts.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                receipts.append(json.loads(line))

    invoices = []
    with open(bundle_dir / "data" / "invoices.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                invoices.append(json.loads(line))

    actual_inv_decisions = {}
    with open(match_out, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                actual_inv_decisions[d["invoice_id"]] = d["status"]

    sorted_invs = sorted(invoices, key=lambda i: (i["received_date"], i["invoice_id"]))
    seen_keys: set[str] = set()
    invoice_mismatches = []

    for inv in sorted_invs:
        inv_id = inv["invoice_id"]
        actual = actual_inv_decisions.get(inv_id)
        po = pos.get(inv["po_id"])
        norm_key = f"{inv['supplier_id']}:{inv['supplier_invoice_number'].strip().upper()}"
        is_dup = norm_key in seen_keys

        accepted_qty = sum(
            r["quantity_received"]
            for r in receipts
            if r["po_id"] == po["po_id"] and r["condition"] == "ACCEPTED"
        ) if po else 0

        expected_status, _ = oracle.evaluate_invoice(
            po_goods_net=po["goods_net_jpy"] if po else 0,
            inv_goods_net=inv["goods_net_jpy"],
            po_state=po["state"] if po else "DRAFT",
            po_quantity=po["quantity"] if po else 0,
            inv_quantity=inv["quantity"],
            accepted_receipt_qty=accepted_qty,
            is_duplicate=is_dup,
        )
        seen_keys.add(norm_key)

        if actual != expected_status:
            invoice_mismatches.append({"invoice_id": inv_id, "actual": actual, "expected": expected_status})

    report = {
        "status": "PASS" if not route_mismatches and not invoice_mismatches else "FAIL",
        "routing_evaluated": len(reqs),
        "routing_mismatches": len(route_mismatches),
        "invoices_evaluated": len(invoices),
        "invoice_mismatches": len(invoice_mismatches),
        "details": {
            "route_mismatches": route_mismatches,
            "invoice_mismatches": invoice_mismatches,
        }
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report


if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent
    b_dir = base / "bundles" / "harbor_demo" / "input"
    rep_file = base / "reports" / "demo_validation.json"
    rep = validate_bundle(b_dir, rep_file)
    print("Validation Report Status:", rep["status"])
    if rep["status"] != "PASS":
        sys.exit(1)
