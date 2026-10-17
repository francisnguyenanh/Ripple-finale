"""HTML document renderer for synthetic invoices and goods receipts."""

import html
import json
from pathlib import Path


INVOICE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Commercial Invoice - {invoice_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; margin: 40px; color: #222; }}
  .watermark {{ color: #cc0000; font-weight: bold; font-size: 14px; text-transform: uppercase; border: 2px dashed #cc0000; padding: 8px 16px; display: inline-block; margin-bottom: 20px; }}
  .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #1F497D; padding-bottom: 12px; margin-bottom: 24px; }}
  .title {{ font-size: 24px; font-weight: bold; color: #1F497D; }}
  .meta-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
  .meta-table td {{ padding: 6px 12px; font-size: 14px; }}
  .meta-label {{ font-weight: bold; width: 25%; background: #f4f6f9; }}
  .items-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
  .items-table th {{ background: #1F497D; color: #fff; text-align: left; padding: 10px 12px; font-size: 13px; }}
  .items-table td {{ border-bottom: 1px solid #e0e0e0; padding: 10px 12px; font-size: 13px; }}
  .text-right {{ text-align: right; }}
  .totals {{ width: 320px; margin-left: auto; border-collapse: collapse; }}
  .totals td {{ padding: 6px 12px; font-size: 14px; }}
  .totals .grand-total {{ font-weight: bold; font-size: 16px; border-top: 2px solid #1F497D; color: #1F497D; }}
</style>
</head>
<body>
  <div class="watermark">SYNTHETIC DEMO — NOT FOR PAYMENT</div>
  <div class="header">
    <div class="title">COMMERCIAL INVOICE</div>
    <div><strong>{supplier_id}</strong><br>Invoice No: {supplier_invoice_number}</div>
  </div>

  <table class="meta-table">
    <tr>
      <td class="meta-label">Invoice ID</td>
      <td>{invoice_id}</td>
      <td class="meta-label">Purchase Order ID</td>
      <td>{po_id}</td>
    </tr>
    <tr>
      <td class="meta-label">Issue Date</td>
      <td>{issue_date}</td>
      <td class="meta-label">Received Date</td>
      <td>{received_date}</td>
    </tr>
    <tr>
      <td class="meta-label">Buyer</td>
      <td>Harbor Office Supply Ltd</td>
      <td class="meta-label">Currency</td>
      <td>{currency}</td>
    </tr>
  </table>

  <table class="items-table">
    <thead>
      <tr>
        <th>Description</th>
        <th class="text-right">Quantity</th>
        <th class="text-right">Net Goods (JPY)</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>Procured Office Equipment & Supplies ({po_id})</td>
        <td class="text-right">{quantity}</td>
        <td class="text-right">{goods_net_jpy:,}</td>
      </tr>
    </tbody>
  </table>

  <table class="totals">
    <tr>
      <td>Net Goods:</td>
      <td class="text-right">{goods_net_jpy:,} JPY</td>
    </tr>
    <tr>
      <td>Freight:</td>
      <td class="text-right">{freight_jpy:,} JPY</td>
    </tr>
    <tr>
      <td>Estimated Tax (10%):</td>
      <td class="text-right">{tax_jpy:,} JPY</td>
    </tr>
    <tr class="grand-total">
      <td>Total Amount:</td>
      <td class="text-right">{gross_jpy:,} JPY</td>
    </tr>
  </table>
</body>
</html>
"""


RECEIPT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Goods Receipt Voucher - {receipt_id}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; margin: 40px; color: #222; }}
  .watermark {{ color: #cc0000; font-weight: bold; font-size: 14px; text-transform: uppercase; border: 2px dashed #cc0000; padding: 8px 16px; display: inline-block; margin-bottom: 20px; }}
  .header {{ display: flex; justify-content: space-between; border-bottom: 2px solid #2e7d32; padding-bottom: 12px; margin-bottom: 24px; }}
  .title {{ font-size: 24px; font-weight: bold; color: #2e7d32; }}
  .meta-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
  .meta-table td {{ padding: 6px 12px; font-size: 14px; }}
  .meta-label {{ font-weight: bold; width: 25%; background: #f4f6f9; }}
  .items-table {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
  .items-table th {{ background: #2e7d32; color: #fff; text-align: left; padding: 10px 12px; font-size: 13px; }}
  .items-table td {{ border-bottom: 1px solid #e0e0e0; padding: 10px 12px; font-size: 13px; }}
  .text-right {{ text-align: right; }}
</style>
</head>
<body>
  <div class="watermark">SYNTHETIC DEMO — NOT FOR PAYMENT</div>
  <div class="header">
    <div class="title">GOODS RECEIPT VOUCHER</div>
    <div>Receipt Ref: <strong>{receipt_id}</strong></div>
  </div>

  <table class="meta-table">
    <tr>
      <td class="meta-label">Receipt ID</td>
      <td>{receipt_id}</td>
      <td class="meta-label">Purchase Order ID</td>
      <td>{po_id}</td>
    </tr>
    <tr>
      <td class="meta-label">Received At</td>
      <td>{received_at}</td>
      <td class="meta-label">Received By</td>
      <td>{received_by}</td>
    </tr>
    <tr>
      <td class="meta-label">Inspection Condition</td>
      <td><strong>{condition}</strong></td>
      <td class="meta-label">Warehouse</td>
      <td>Harbor Central Dock #1</td>
    </tr>
  </table>

  <table class="items-table">
    <thead>
      <tr>
        <th>Item Description</th>
        <th>Condition</th>
        <th class="text-right">Quantity Received</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>Material Lot Received under PO {po_id}</td>
        <td>{condition}</td>
        <td class="text-right">{quantity_received}</td>
      </tr>
    </tbody>
  </table>
</body>
</html>
"""


def render_all_forms(data_dir: Path, forms_dir: Path) -> None:
    inv_dir = forms_dir / "invoices"
    rec_dir = forms_dir / "receipts"
    inv_dir.mkdir(parents=True, exist_ok=True)
    rec_dir.mkdir(parents=True, exist_ok=True)

    # 1. Render first 12 Invoices
    invoices_file = data_dir / "invoices.jsonl"
    count_inv = 0
    with open(invoices_file, "r", encoding="utf-8") as f:
        for line in f:
            if count_inv >= 12 or not line.strip():
                continue
            data = json.loads(line)
            content = INVOICE_TEMPLATE.format(
                invoice_id=html.escape(data["invoice_id"]),
                supplier_id=html.escape(data["supplier_id"]),
                supplier_invoice_number=html.escape(data["supplier_invoice_number"]),
                po_id=html.escape(data["po_id"]),
                issue_date=html.escape(data["issue_date"]),
                received_date=html.escape(data["received_date"]),
                currency=html.escape(data["currency"]),
                quantity=data["quantity"],
                goods_net_jpy=data["goods_net_jpy"],
                freight_jpy=data["freight_jpy"],
                tax_jpy=data["tax_jpy"],
                gross_jpy=data["gross_jpy"],
            )
            out_file = inv_dir / f"{data['invoice_id']}.html"
            out_file.write_text(content, encoding="utf-8")
            count_inv += 1

    # 2. Render first 12 Receipts
    receipts_file = data_dir / "receipts.jsonl"
    count_rec = 0
    with open(receipts_file, "r", encoding="utf-8") as f:
        for line in f:
            if count_rec >= 12 or not line.strip():
                continue
            data = json.loads(line)
            content = RECEIPT_TEMPLATE.format(
                receipt_id=html.escape(data["receipt_id"]),
                po_id=html.escape(data["po_id"]),
                received_at=html.escape(data["received_at"]),
                received_by=html.escape(data["received_by"]),
                condition=html.escape(data["condition"]),
                quantity_received=data["quantity_received"],
            )
            out_file = rec_dir / f"{data['receipt_id']}.html"
            out_file.write_text(content, encoding="utf-8")
            count_rec += 1

    print(f"Rendered {count_inv} HTML invoices and {count_rec} HTML receipts.")


if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent.parent
    target_data = base / "bundles" / "harbor_demo" / "input" / "data"
    target_forms = base / "bundles" / "harbor_demo" / "input" / "forms"
    render_all_forms(target_data, target_forms)
