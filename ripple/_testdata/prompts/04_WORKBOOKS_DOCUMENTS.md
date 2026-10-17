# Stage 04 — Real Excel workbooks, invoices and vouchers

Operator inputs: system instruction, frozen spec, accepted workbook/data contracts, generation specs and example actual business records if already built. Never invent a new supplier/PO merely to make a prettier invoice.

---

Create Python builders and small template/specification files that generate actual binary XLSX and human-readable business forms. Do not output binary/base64 or rename CSV to XLSX. All builders are private under authoring; final files under public input as contracted.

Three required workbooks:

1. `data/approval_matrix.xlsx`: role/amount/category guidance, readable headings, monetary formats, freeze panes, filters, named threshold cell(s). Canonical private build manager row 1m; S0 controlled mutation later sets that row to 500k. Finance is CAPEX >=2m, risk priority clear. Record the exact sheet/cell in private materialized manifest, not embedded hidden expected labels.
2. `data/supplier_register.xlsx`: same 20 suppliers, risk/status matching structured master. Include neutral operational fields to make the artifact realistic without inventing extra approval policy.
3. `data/invoice_register.xlsx`: same 120 invoices with PO IDs, goods net/freight/tax/gross/dates; formula examples allowed but store independently Python-computed numerical fields for validation. Never use openpyxl formula cached results as truth; it does not calculate formulas.

Formatting should be professional but simple: fonts, fills, column widths, number formats, summary cells. Avoid macros, external workbook links, charts/images whose roundtrip preservation is not being validated. Include a small unrelated note/sheet/style to exercise preservation of unaffected workbook content.

Forms:

- Render 12 invoice HTML files and 12 receipt HTML files selected by private stable ID list; include at least one READY, one quantity hold and one price hold invoice where actual data supports them. Form content contains source transaction facts, not hidden expected status labels.
- Each form references source IDs, fictional parties, dates, item/quantity/prices/totals, watermark `SYNTHETIC DEMO — NOT FOR PAYMENT`; no usable payment instructions. Escape HTML fields to prevent content injection.
- A corresponding machine-readable JSONL/CSV record is the primary observed data; form is a rendering of it. Private manifest records `rendered_from`, preventing duplicate rendered copies from being counted as independent corroboration.
- Optional PDF output through a deterministic renderer such as ReportLab, optional DOCX manual through python-docx. Only implement optional formats if explicitly requested in BATCH_ID. Markdown/structured records remain available. Do not claim Ripple reads PDF/DOCX/OCR unless its adapters have been validated.

Verification outputs/scripts:

- Reopen workbooks with a parser, compare rows, cell types, formulas-as-text and expected styles/named ranges; verify only manager cell changes in C01 mutation later.
- Compare printed amounts to canonical JSONL records by IDs; subtotal+freight+tax=gross and date/currency consistency.
- Calculate actual raw SHA-256, file sizes and semantic workbook digests after generation. Metadata/ZIP timestamps may vary; normalize and test before claiming byte determinism.
- Integrate builder stages into build.py without import cycles or duplicate policy calculators. Arithmetic can reuse a neutral data arithmetic module, **not** the future oracle evaluator.

Return complete builder/template/spec files and execution commands; binary artifacts themselves will exist only after operator execution.
