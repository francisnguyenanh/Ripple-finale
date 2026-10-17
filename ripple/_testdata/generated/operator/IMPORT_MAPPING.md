# Import Mapping & Ingestion Manifest — Harbor Procurement Mini

This document defines the eligibility, semantic roles, and ingestion boundaries for importing the public bundle into Ripple.

---

## 1. Import Boundaries & Zone Classification

| Directory / File | Ingestion Eligibility | Semantic Role | Notes |
|---|---|---|---|
| `bundles/harbor_demo/input/app/` | **YES** | Runnable Code / Application Logic | 11 Python modules implementing domain routing and matching |
| `bundles/harbor_demo/input/config/` | **YES** | Operative Configuration | `approval.yaml` and `workflow.yaml` |
| `bundles/harbor_demo/input/data/*.csv` | **YES** | Tabular Master Data | `suppliers.csv`, `employees.csv` |
| `bundles/harbor_demo/input/data/*.jsonl` | **YES** | Structured Transaction Streams | `requisitions.jsonl`, `purchase_orders.jsonl`, `receipts.jsonl`, `invoices.jsonl` |
| `bundles/harbor_demo/input/data/*.xlsx` | **YES** | Operational Workbooks | `approval_matrix.xlsx` (C01 conflict), `supplier_directory.xlsx`, `procurement_summary_202609.xlsx` |
| `bundles/harbor_demo/input/docs/*.md` | **YES** | Business Policies & SOPs | 8 Markdown documents (including C02, G01, G02) |
| `bundles/harbor_demo/input/forms/` | **OPTIONAL** | Presentation Renderings | 12 HTML invoices and 12 HTML receipts (`rendered_from` data records) |
| `bundles/harbor_demo/input/tests/` | **YES** | Characterization Tests | 4 test modules describing current application behavior |
| `bundles/harbor_demo/input/README.md` | **YES** | System Overview | Standalone application readme |
| `authoring/` | **STRICTLY EXCLUDED** | Private Builder & Contracts | Authoring environment only; must not enter Ripple |
| `evaluator_private/` | **STRICTLY EXCLUDED** | Ground Truth & Private Oracles | Private oracle, test vectors, and negative edge cases |
| `reports/` | **STRICTLY EXCLUDED** | Audit Reports | Private execution digests |
| `operator/` | **STRICTLY EXCLUDED** | Runbooks & Demonstrations | Human operator materials |

---

## 2. Presentation Deduplication (`rendered_from`)
- Files in `forms/invoices/INV-*.html` are visual presentations rendered directly from `data/invoices.jsonl`.
- Files in `forms/receipts/REC-*.html` are visual presentations rendered directly from `data/receipts.jsonl`.
- *Ingestion Rule*: If an adapter processes both the structured JSONL and HTML forms, the manifest prevents counting rendered forms as independent corroboration.
