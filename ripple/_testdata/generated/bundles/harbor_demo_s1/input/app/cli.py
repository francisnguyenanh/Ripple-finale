"""Command-line interface for Harbor Procurement Mini."""

import argparse
import sys
from pathlib import Path
from app.config_loader import load_approval_config
from app.data_loader import (
    load_employees,
    load_invoices,
    load_purchase_orders,
    load_receipts,
    load_requisitions,
    load_suppliers,
    save_invoice_decisions,
    save_routes,
)
from app.emergency import route_emergency
from app.enums import ProcessType
from app.matching import evaluate_invoice
from app.routing import route_request


def handle_route(args: argparse.Namespace) -> int:
    config = load_approval_config(args.config)
    reqs = load_requisitions(args.requests)

    # Optional supplier directory for enriched supplier risk/status lookup
    suppliers = {}
    sup_file = Path(args.requests).parent / "suppliers.csv"
    if sup_file.is_file():
        suppliers = load_suppliers(sup_file)

    routes = []
    for r in reqs:
        if r.process == ProcessType.EMERGENCY:
            decision = route_emergency(r, config, suppliers=suppliers)
        else:
            decision = route_request(r, config, suppliers=suppliers)
        routes.append(decision)

    save_routes(routes, args.output)
    print(f"Evaluated {len(routes)} requisitions. Output written to {args.output}")
    return 0


def handle_match(args: argparse.Namespace) -> int:
    config = load_approval_config(args.config)
    data_dir = Path(args.data_dir)

    pos = load_purchase_orders(data_dir / "purchase_orders.jsonl")
    receipts = load_receipts(data_dir / "receipts.jsonl")
    invoices = load_invoices(data_dir / "invoices.jsonl")

    # Sort invoices deterministically by (received_date, invoice_id)
    sorted_invoices = sorted(invoices, key=lambda inv: (inv.received_date, inv.invoice_id))

    seen_keys: set[str] = set()
    decisions = []

    for inv in sorted_invoices:
        po = pos.get(inv.po_id)
        if po is None:
            from app.enums import InvoiceStatus
            from app.models import InvoiceDecision
            dec = InvoiceDecision(
                inv.invoice_id,
                InvoiceStatus.REJECTED,
                "UNKNOWN_PO_REFERENCE",
                None,
                inv.po_id,
                {"error": f"Referenced PO {inv.po_id} not found"},
            )
        else:
            dec = evaluate_invoice(inv, po, receipts, seen_keys, config)
            # Update seen_keys after evaluation if supplier invoice identity is valid
            norm_key = f"{inv.supplier_id}:{inv.supplier_invoice_number.strip().upper()}"
            seen_keys.add(norm_key)
        decisions.append(dec)

    save_invoice_decisions(decisions, args.output)
    print(f"Evaluated {len(decisions)} invoices. Output written to {args.output}")
    return 0


def handle_inspect(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    entity = args.entity.lower()
    target_id = args.id

    if entity == "po":
        pos = load_purchase_orders(data_dir / "purchase_orders.jsonl")
        po = pos.get(target_id)
        if po:
            print(f"PO {po.po_id}: Supplier={po.supplier_id}, State={po.state.value}, Net={po.goods_net_jpy:,} JPY, Gross={po.gross_jpy:,} JPY")
            return 0
        print(f"PO with ID {target_id} not found.", file=sys.stderr)
        return 1

    if entity == "invoice":
        invoices = load_invoices(data_dir / "invoices.jsonl")
        for inv in invoices:
            if inv.invoice_id == target_id:
                print(f"Invoice {inv.invoice_id}: Supplier={inv.supplier_id}, PO={inv.po_id}, Net={inv.goods_net_jpy:,} JPY, Recv={inv.received_date}")
                return 0
        print(f"Invoice with ID {target_id} not found.", file=sys.stderr)
        return 1

    if entity == "requisition":
        reqs = load_requisitions(data_dir / "requisitions.jsonl")
        for req in reqs:
            if req.request_id == target_id:
                print(f"Requisition {req.request_id}: Requester={req.requester_id}, Process={req.process.value}, Category={req.category.value}, Net={req.net_amount_jpy:,} JPY")
                return 0
        print(f"Requisition with ID {target_id} not found.", file=sys.stderr)
        return 1

    print(f"Unknown entity type: {entity}", file=sys.stderr)
    return 1


def main(argv: list[str] = None) -> int:
    parser = argparse.ArgumentParser(description="Harbor Procurement Mini - Operations CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: route
    p_route = subparsers.add_parser("route", help="Route requisitions according to approval policy")
    p_route.add_argument("--requests", required=True, type=Path, help="Path to requisitions.jsonl")
    p_route.add_argument("--config", required=True, type=Path, help="Path to approval.yaml")
    p_route.add_argument("--output", required=True, type=Path, help="Output path for routes.jsonl")
    p_route.set_defaults(func=handle_route)

    # Subcommand: match
    p_match = subparsers.add_parser("match", help="Perform 3-way invoice matching")
    p_match.add_argument("--data-dir", required=True, type=Path, help="Directory containing purchase_orders, receipts, and invoices")
    p_match.add_argument("--config", required=True, type=Path, help="Path to approval.yaml")
    p_match.add_argument("--output", required=True, type=Path, help="Output path for invoice_decisions.jsonl")
    p_match.set_defaults(func=handle_match)

    # Subcommand: inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect an entity by ID")
    p_inspect.add_argument("--entity", required=True, choices=["po", "invoice", "requisition"])
    p_inspect.add_argument("--id", required=True, help="Entity ID")
    p_inspect.add_argument("--data-dir", required=True, type=Path, help="Directory containing data files")
    p_inspect.set_defaults(func=handle_inspect)

    parsed = parser.parse_args(argv)
    return parsed.func(parsed)


if __name__ == "__main__":
    sys.exit(main())
