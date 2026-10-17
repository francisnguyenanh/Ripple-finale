# Harbor Procurement Mini

Harbor Procurement Mini is an internal procurement reference application for Harbor Office Supply. It manages purchase requisitions, approval routing, purchase orders, goods receipts, and 3-way invoice matching.

## Architecture & Structure

- `app/`: Pure Python domain package and CLI interface.
  - `routing.py`: Standard approval matrix evaluation.
  - `emergency.py`: Emergency purchase process routing.
  - `matching.py`: 3-way matching of invoices against purchase orders and receipts.
  - `approval.py`: Maker-checker authorization verification.
  - `calculations.py`: Deterministic financial arithmetic, tax calculation, and payment terms.
  - `cli.py`: Operational command-line entrypoint.
- `config/`: Operative configuration files (`approval.yaml`, `workflow.yaml`).
- `data/`: Procurement records (CSV, JSONL, and operational Excel spreadsheets).
- `docs/`: Standard operating procedures, policies, and system manuals.
- `forms/`: Rendered HTML invoice documents and goods receipt vouchers.
- `tests/`: Test suite for business logic verification.

## CLI Usage

### Requisition Approval Routing
Evaluate pending purchase requisitions:
```bash
python -m app.cli route --requests data/requisitions.jsonl --config config/approval.yaml --output run_output/routes.jsonl
```

### 3-Way Invoice Matching
Match incoming vendor invoices against purchase orders and goods receipt records:
```bash
python -m app.cli match --data-dir data --config config/approval.yaml --output run_output/invoice_decisions.jsonl
```

### Entity Inspection
Inspect individual purchase orders, invoices, or requisitions:
```bash
python -m app.cli inspect --entity po --id PO-000001 --data-dir data
```

## Running Tests
Run the pytest test suite:
```bash
pytest tests/
```
