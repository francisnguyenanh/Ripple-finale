"""Pure 3-way invoice matching and duplicate detection logic."""

from typing import Any
from app.calculations import calculate_due_date, validate_integer_money
from app.enums import InvoiceStatus, POState, ReceiptCondition
from app.models import ApprovalConfig, Invoice, InvoiceDecision, PO, Receipt


def evaluate_invoice(
    invoice: Invoice,
    po: PO,
    receipts: list[Receipt],
    seen_keys: set[str],
    config: ApprovalConfig,
) -> InvoiceDecision:
    """Evaluates an invoice against PO, receipts, and duplicate history.
    
    This function is strictly pure and does NOT mutate seen_keys.
    """
    details: dict[str, Any] = {
        "invoice_id": invoice.invoice_id,
        "po_id": po.po_id,
        "supplier_id": invoice.supplier_id,
        "invoice_net_jpy": invoice.goods_net_jpy,
        "po_net_jpy": po.goods_net_jpy,
        "invoice_quantity": invoice.quantity,
        "po_quantity": po.quantity,
    }

    # 1. Structural validation
    try:
        validate_integer_money(invoice.goods_net_jpy, "goods_net_jpy", allow_zero=False)
        validate_integer_money(invoice.quantity, "quantity", allow_zero=False)
        validate_integer_money(invoice.freight_jpy, "freight_jpy", allow_zero=True)
        validate_integer_money(invoice.tax_jpy, "tax_jpy", allow_zero=True)
    except (TypeError, ValueError) as err:
        details["error"] = str(err)
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.REJECTED, "INVALID_INVOICE_DATA", None, po.po_id, details)

    if invoice.currency != "JPY":
        details["error"] = f"Unsupported currency: {invoice.currency}"
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.REJECTED, "UNSUPPORTED_CURRENCY", None, po.po_id, details)

    # Cross-reference consistency
    if invoice.supplier_id != po.supplier_id or invoice.po_id != po.po_id:
        details["error"] = "Supplier or PO ID mismatch between invoice and PO"
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.REJECTED, "PO_SUPPLIER_MISMATCH", None, po.po_id, details)

    # Duplicate key check (supplier_id + normalized invoice number)
    normalized_key = f"{invoice.supplier_id}:{invoice.supplier_invoice_number.strip().upper()}"
    details["normalized_key"] = normalized_key
    if normalized_key in seen_keys:
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.REJECTED, "DUPLICATE_INVOICE", None, po.po_id, details)

    # PO Approval state check
    if po.state != POState.APPROVED:
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.HOLD_UNAPPROVED, "PO_NOT_APPROVED", None, po.po_id, details)

    # Quantity check against PO and accepted receipts
    accepted_receipt_qty = sum(
        r.quantity_received
        for r in receipts
        if r.po_id == po.po_id and r.condition == ReceiptCondition.ACCEPTED
    )
    details["accepted_receipt_qty"] = accepted_receipt_qty

    if invoice.quantity != po.quantity or accepted_receipt_qty < invoice.quantity:
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.HOLD_QUANTITY, "QUANTITY_MISMATCH", None, po.po_id, details)

    # Price deviation tolerance check
    price_diff = abs(invoice.goods_net_jpy - po.goods_net_jpy)
    details["price_difference_jpy"] = price_diff

    # Condition: price_diff <= 10,000 JPY AND 100 * price_diff <= 2 * po_goods_net
    fixed_pass = price_diff <= config.price_tolerance_fixed_jpy
    pct_pass = (100 * price_diff) <= (config.price_tolerance_percent * po.goods_net_jpy)

    if not (fixed_pass and pct_pass):
        details["tolerance_check"] = {"fixed_pass": fixed_pass, "pct_pass": pct_pass}
        return InvoiceDecision(invoice.invoice_id, InvoiceStatus.HOLD_PRICE, "PRICE_OUT_OF_TOLERANCE", None, po.po_id, details)

    # All checks passed -> READY for review with due date
    due_date = calculate_due_date(invoice.received_date)
    return InvoiceDecision(invoice.invoice_id, InvoiceStatus.READY, "MATCHED_READY_FOR_PAYMENT", due_date, po.po_id, details)
