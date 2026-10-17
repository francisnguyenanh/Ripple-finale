# Harbor Procurement Mini — Sequential Generation Plan (Stages 02–09)

This runbook specifies the exact batch sequencing, required input contexts, output artifacts, and human clarification checkpoints for generating Harbor Procurement Mini.

---

## Stage 02 — Core Application

- **Objective**: Author the runnable public Python package, configurations, and baseline pytest characterization tests.
- **Prerequisites**: Approved `authoring/contracts.json` and `authoring/blueprint.md`.

### Batch 02a: Foundations & Pure Domain Core
- **Inputs**: `authoring/contracts.json`.
- **Files**:
  - `bundles/harbor_demo/input/app/__init__.py`
  - `bundles/harbor_demo/input/app/enums.py`
  - `bundles/harbor_demo/input/app/models.py`
  - `bundles/harbor_demo/input/app/calculations.py`
  - `bundles/harbor_demo/input/app/config_loader.py`
- **Verification**: `python -m py_compile` on generated modules.

### Batch 02b: Business Logic Handlers
- **Inputs**: Batch 02a modules, `FROZEN_SPEC` sections C & D.
- **Files**:
  - `bundles/harbor_demo/input/app/routing.py` (R01-R04)
  - `bundles/harbor_demo/input/app/emergency.py` (R05 with 1M literal)
  - `bundles/harbor_demo/input/app/approval.py` (R06 maker-checker)
  - `bundles/harbor_demo/input/app/matching.py` (R07-R10 3-way matching)
- **Verification**: Unit checks for pure function exports.

### Batch 02c: CLI, Configs & Baseline Characterization Tests
- **Inputs**: Batches 02a & 02b modules.
- **Files**:
  - `bundles/harbor_demo/input/app/data_loader.py`
  - `bundles/harbor_demo/input/app/cli.py`
  - `bundles/harbor_demo/input/config/approval.yaml`
  - `bundles/harbor_demo/input/config/workflow.yaml`
  - `bundles/harbor_demo/input/tests/__init__.py`
  - `bundles/harbor_demo/input/tests/test_routing.py`
  - `bundles/harbor_demo/input/tests/test_matching.py`
  - `bundles/harbor_demo/input/tests/test_calculations.py`
  - `bundles/harbor_demo/input/tests/test_cli.py`
  - `bundles/harbor_demo/input/requirements.txt`
  - `bundles/harbor_demo/input/README.md`
- **Stop Condition**: Characterization tests must pass 100%. If any test fails, STOP and fix.

---

## Stage 03 — Deterministic Data Builders

- **Objective**: Author generator scripts producing the synthetic enterprise datasets with fixed seed `20260925`.
- **Prerequisites**: Stage 02 application code and contracts.

### Batch 03a: Master Entities & Requisitions
- **Inputs**: `authoring/contracts.json`.
- **Files**:
  - `authoring/builders/generate_records.py` (Part 1: 20 Suppliers, 8 Employees, 240 Requisitions with mandatory witnesses)
- **Verification**: Check presence of 12 STANDARD and 12 EMERGENCY 600k witnesses, boundary values (499999, 500000, 749999, etc.).

### Batch 03b: Downstream Flow & Private Edge Cases
- **Inputs**: Generated requisitions, matching logic.
- **Files**:
  - `authoring/builders/generate_records.py` (Part 2: 120 POs, 140 Receipts, 120 Invoices)
  - `evaluator_private/validation/edge_cases_24.jsonl` (24 negative edge cases: duplicate keys, over-quantities, unapproved POs, extreme price deviations)
  - `authoring/builders/case_catalog.json` (Catalog documenting case distributions and intended HOLD statuses)
- **Stop Condition**: Check entity counts: exactly 20 suppliers, 8 employees, 240 requisitions, 120 POs, 140 receipts, 120 invoices.

---

## Stage 04 — Workbooks & Operational Documents

- **Objective**: Generate 3 valid openpyxl Excel workbooks and render 12 invoice and 12 receipt HTML forms.

### Batch 04a: Excel Workbooks Builder
- **Inputs**: `authoring/contracts.json`, generated CSV/JSONL records.
- **Files**:
  - `authoring/builders/build_workbooks.py` (Generates `approval_matrix.xlsx` with C01 mutation 500k, `supplier_directory.xlsx`, `procurement_summary_202609.xlsx`)
- **Verification**: Reopen workbooks with openpyxl, assert cell B4 on `RoutingRules` in `approval_matrix.xlsx` equals `500000`.

### Batch 04b: Document Renderers
- **Inputs**: Sample POs, receipts, and invoices.
- **Files**:
  - `authoring/builders/render_forms.py` (Generates 12 HTML invoices and 12 HTML goods receipts with watermark `SYNTHETIC DEMO — NOT FOR PAYMENT`)
- **Verification**: Validate HTML syntax and ensure printed totals equal structured data.

---

## Stage 05 — Business Design & Manuals

- **Objective**: Provide realistic documentation reflecting operational policies, SOPs, and system architecture.

### Batch 05a: Core Policies & Procedures
- **Inputs**: `FROZEN_SPEC` Sections D & E.
- **Files**:
  - `bundles/harbor_demo/input/docs/procurement_policy.md` (Formal company policy: 1M manager threshold, CAPEX-only 2M finance threshold)
  - `bundles/harbor_demo/input/docs/requisition_sop.md` (Standard operating procedure; embeds G02 2-business-day escalation gap)
  - `bundles/harbor_demo/input/docs/finance_desk_guide.md` (Informal desk guide; embeds C02 conflict stating all >=2M go to finance)
  - `bundles/harbor_demo/input/docs/emergency_purchases.md` (Emergency guidance; embeds G01 ambiguity)

### Batch 05b: Technical Specifications & Data Dictionaries
- **Inputs**: Contracts, CLI schema, entity definitions.
- **Files**:
  - `bundles/harbor_demo/input/docs/system_architecture.md`
  - `bundles/harbor_demo/input/docs/data_dictionary.md`
  - `bundles/harbor_demo/input/docs/invoice_matching_manual.md`
  - `bundles/harbor_demo/input/docs/cli_guide.md`
- **Verification**: Check for absence of private IDs (`R01-R12`, `C01-C02`, `G01-G02`, `S0-S5`) in public markdown.

---

## Stage 06 — Independent Oracle & Validation Suite

- **Objective**: Build an independent verification oracle that does NOT import public application code.

### Batch 06a: Oracle Engine & Leakage Checker
- **Inputs**: `03_FROZEN_SPEC.md` Section H (anchors), `authoring/contracts.json`.
- **Files**:
  - `evaluator_private/oracle.py` (Independent rule implementation derived purely from spec)
  - `evaluator_private/anchors.json` (Golden hand-calculated test vectors)
  - `evaluator_private/check_leakage.py` (Scans public input tree for private ID leakage)
- **Stop Condition**: Oracle must validate all golden anchors. Leakage scanner must find 0 private IDs in `bundles/harbor_demo/input/`.

---

## Stage 07 — Scenarios & Variant Bundler

- **Objective**: Provide reproducible scenario mutations (S0–S5) and dev/holdout variant generators.

### Batch 07a: Scenario Mutation Scripts
- **Inputs**: Baseline data, scenario definitions from `FROZEN_SPEC` Section G.
- **Files**:
  - `authoring/builders/mutate_scenarios.py`
  - `evaluator_private/scenarios/s0_baseline.json`
  - `evaluator_private/scenarios/s1_manager_500k.json`
  - `evaluator_private/scenarios/s2_manager_750k.json`
  - `evaluator_private/scenarios/s3_restore_s0.json`
  - `evaluator_private/scenarios/s4_excel_drift_650k.json`
  - `evaluator_private/scenarios/s5_finance_capex_only.json`

### Batch 07b: Held-out & Dev Variants
- **Inputs**: Mutation engine, alternative seeds (`20260926`, `20260927`).
- **Files**:
  - `bundles/harbor_dev_01/input/...`
  - `bundles/harbor_holdout_01/input/...`

---

## Stage 08 — Review, Verification & Invariant Repair

- **Objective**: Run automated consistency checks across code, data, Excel, and docs.

### Batch 08a: Invariant Verification
- **Files**:
  - `authoring/verify_invariants.py`
  - `reports/s0_baseline_audit.md`
- **Stop Condition**: Intentional conflicts (C01, C02, G01, G02) must be flagged as DETECTED. No unintentional defects permitted.

---

## Stage 09 — Packaging, Runbooks & Demo

- **Objective**: Complete end-to-end build runner and operator demonstration script.

### Batch 09a: Orchestrator & Evidence
- **Files**:
  - `authoring/build.py`
  - `operator/demo_runbook.md`
  - `operator/evidence_manifest.json`
- **Verification**: Run `python authoring/build.py --bundle harbor_demo --seed 20260925 --output bundles/harbor_demo/input`.
