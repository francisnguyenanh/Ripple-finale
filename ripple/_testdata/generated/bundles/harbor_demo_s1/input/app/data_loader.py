"""Data ingest and persistence helpers for CSV and JSONL formats."""

import csv
import json
from pathlib import Path
from typing import Union
from app.enums import (
    CategoryType,
    InvoiceStatus,
    POState,
    ProcessType,
    ReceiptCondition,
    Role,
    RouteType,
    SupplierRisk,
    SupplierStatus,
)
from app.models import (
    Employee,
    Invoice,
    InvoiceDecision,
    PO,
    Receipt,
    Requisition,
    RouteDecision,
    Supplier,
)


def load_suppliers(csv_path: Union[str, Path]) -> dict[str, Supplier]:
    suppliers: dict[str, Supplier] = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sup = Supplier(
                supplier_id=row["supplier_id"],
                display_name=row["display_name"],
                status=SupplierStatus(row["status"]),
                risk=SupplierRisk(row["risk"]),
                contact_email=row["contact_email"],
            )
            suppliers[sup.supplier_id] = sup
    return suppliers


def load_employees(csv_path: Union[str, Path]) -> dict[str, Employee]:
    employees: dict[str, Employee] = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            roles = [Role(r.strip()) for r in row["roles"].split(",") if r.strip()]
            emp = Employee(
                employee_id=row["employee_id"],
                display_name=row["display_name"],
                department=row["department"],
                roles=roles,
            )
            employees[emp.employee_id] = emp
    return employees


def load_requisitions(jsonl_path: Union[str, Path]) -> list[Requisition]:
    requisitions: list[Requisition] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            req = Requisition(
                request_id=data["request_id"],
                requester_id=data["requester_id"],
                supplier_id=data["supplier_id"],
                process=ProcessType(data["process"]),
                category=CategoryType(data["category"]),
                net_amount_jpy=data["net_amount_jpy"],
                currency=data["currency"],
                requested_at=data["requested_at"],
                quantity=data["quantity"],
                item_description=data["item_description"],
            )
            requisitions.append(req)
    return requisitions


def load_purchase_orders(jsonl_path: Union[str, Path]) -> dict[str, PO]:
    pos: dict[str, PO] = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            po = PO(
                po_id=data["po_id"],
                request_id=data["request_id"],
                supplier_id=data["supplier_id"],
                currency=data["currency"],
                quantity=data["quantity"],
                goods_net_jpy=data["goods_net_jpy"],
                freight_jpy=data["freight_jpy"],
                tax_jpy=data["tax_jpy"],
                gross_jpy=data["gross_jpy"],
                state=POState(data["state"]),
                approved_by=data.get("approved_by"),
                approved_at=data.get("approved_at"),
            )
            pos[po.po_id] = po
    return pos


def load_receipts(jsonl_path: Union[str, Path]) -> list[Receipt]:
    receipts: list[Receipt] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            rec = Receipt(
                receipt_id=data["receipt_id"],
                po_id=data["po_id"],
                quantity_received=data["quantity_received"],
                received_at=data["received_at"],
                received_by=data["received_by"],
                condition=ReceiptCondition(data["condition"]),
            )
            receipts.append(rec)
    return receipts


def load_invoices(jsonl_path: Union[str, Path]) -> list[Invoice]:
    invoices: list[Invoice] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            inv = Invoice(
                invoice_id=data["invoice_id"],
                supplier_id=data["supplier_id"],
                supplier_invoice_number=data["supplier_invoice_number"],
                po_id=data["po_id"],
                quantity=data["quantity"],
                goods_net_jpy=data["goods_net_jpy"],
                freight_jpy=data["freight_jpy"],
                tax_jpy=data["tax_jpy"],
                gross_jpy=data["gross_jpy"],
                currency=data["currency"],
                received_date=data["received_date"],
                issue_date=data["issue_date"],
            )
            invoices.append(inv)
    return invoices


def save_routes(routes: list[RouteDecision], output_path: Union[str, Path]) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in routes:
            record = {
                "request_id": r.request_id,
                "route": r.route.value,
                "reason_code": r.reason_code,
                "amount_used_jpy": r.amount_used_jpy,
                "applied_config_version": r.applied_config_version,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_invoice_decisions(decisions: list[InvoiceDecision], output_path: Union[str, Path]) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for d in decisions:
            record = {
                "invoice_id": d.invoice_id,
                "status": d.status.value,
                "reason_code": d.reason_code,
                "due_date": d.due_date,
                "matched_po_id": d.matched_po_id,
                "comparison_details": d.comparison_details,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
