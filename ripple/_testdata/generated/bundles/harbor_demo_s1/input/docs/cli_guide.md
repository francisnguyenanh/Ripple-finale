# CLI User & Operations Guide — Harbor Procurement Mini

## 1. Overview
The Harbor Procurement Mini CLI provides operational subcommands to process requisitions, evaluate invoices, and inspect system records.

---

## 2. Command Reference

### 2.1 Requisition Routing (`route`)
Evaluates approval routing for a batch of input requisitions:
```bash
python -m app.cli route --requests data/requisitions.jsonl --config config/approval.yaml --output run_output/routes.jsonl
```
- **Arguments**:
  - `--requests`: Path to `requisitions.jsonl`
  - `--config`: Path to `approval.yaml`
  - `--output`: Destination path for route decisions JSONL

### 2.2 3-Way Matching (`match`)
Performs 3-way reconciliation across purchase orders, receipts, and incoming invoices:
```bash
python -m app.cli match --data-dir data --config config/approval.yaml --output run_output/invoice_decisions.jsonl
```
- **Arguments**:
  - `--data-dir`: Directory containing `purchase_orders.jsonl`, `receipts.jsonl`, and `invoices.jsonl`
  - `--config`: Path to `approval.yaml`
  - `--output`: Destination path for invoice decisions JSONL

### 2.3 Entity Inspection (`inspect`)
Inspects individual records across the dataset:
```bash
python -m app.cli inspect --entity po --id PO-000001 --data-dir data
python -m app.cli inspect --entity invoice --id INV-000001 --data-dir data
python -m app.cli inspect --entity requisition --id REQ-000001 --data-dir data
```
- **Arguments**:
  - `--entity`: One of `po`, `invoice`, or `requisition`
  - `--id`: Specific record identifier
  - `--data-dir`: Path to directory containing JSONL files
