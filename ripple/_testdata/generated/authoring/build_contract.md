# Harbor Procurement Mini — Build Contract (Stage 01)

## 1. Principles of Isolation and Materialization

The authoring environment enforces strict boundaries between private design/canonical assets and the public demonstration bundles.

### 1.1 Zone Separation Rules
1. **Canonical Authoring Root (`authoring/templates/input/`)**: Contains clean, conflict-free templates for code, configs, and baseline documents.
2. **Private Evaluator Root (`evaluator_private/`)**: Contains ground-truth oracles, scenario manifests, expected outputs, and private validation sets (`edge_cases_24.jsonl`). **Never** copied into or accessible by public bundles.
3. **Public Bundle Root (`bundles/harbor_demo/input/`)**: The sole directory imported into Ripple. Must run independently without relative paths or imports referencing parent or sibling private roots.
4. **No Direct Copying of Authoring Folders**: Private builder scripts or templates must never be copied as wholesale folders into public input. Materialization is performed strictly by an explicit builder step.

---

## 2. Read and Write Allowlists

| Process | Allowed Read Paths | Allowed Write Paths | Denied Operations |
|---|---|---|---|
| **Authoring Build** (`authoring/build.py`) | `authoring/`, `authoring/templates/`, `evaluator_private/scenarios/` | `bundles/harbor_demo/input/`, `bundles/harbor_dev_*/`, `bundles/harbor_holdout_*/` | Writing outside `bundles/` or modifying `Ripple/` parent repository |
| **Data Generator** (`authoring/builders/`) | `authoring/contracts.json` | `bundles/harbor_demo/input/data/`, `bundles/harbor_demo/input/forms/`, `evaluator_private/validation/` | Network calls, random without seed, floating point calculations |
| **Public Application** (`app/cli.py`) | Specified `--data-dir`, `--config`, `--requests` within public bundle | Specified `--output` directory (caller-provided output directory) | Modifying input data in-place, accessing `evaluator_private/` or `authoring/` |
| **Private Oracle** (`evaluator_private/oracle.py`) | `authoring/contracts.json`, `bundles/harbor_demo/input/data/` | `reports/` | Importing `app/` implementation modules to determine expected outcomes |

---

## 3. Canonical Baseline and S0 Mutation Strategy

To avoid circular logic or untracked drifts, generation follows a two-tier process:
1. **Tier 1 (Canonical Clean State)**: Author templates where policies, configs, and code are internally consistent (e.g. manager threshold is 1,000,000 JPY everywhere; finance review is CAPEX-only).
2. **Tier 2 (S0 Inconsistency Injection)**:
   - **C01 Mutation**: In `approval_matrix.xlsx`, set Manager threshold to `500,000 JPY` (cell B4 in sheet `RoutingRules`). Config, policy document, and routing code remain `1,000,000 JPY`.
   - **C02 Mutation**: In `docs/finance_desk_guide.md`, insert informal instruction that "all purchases of 2,000,000 JPY or higher require Finance review" (omitting the CAPEX restriction). Formal policy `docs/procurement_policy.md` and code retain CAPEX-only restriction.
   - **G01 & G02 Gaps**: Retained natively in the canonical templates as described in the blueprint (no additional mutations needed).

---

## 4. Execution Commands to Freeze

### 4.1 Bundle Materialization
Executed from `generated/`:
```bash
python authoring/build.py --bundle harbor_demo --seed 20260925 --output bundles/harbor_demo/input
```

### 4.2 Application CLI Invocations (from public bundle root)
Executed from `generated/bundles/harbor_demo/input`:
```bash
# Requisition Routing
python -m app.cli route --requests data/requisitions.jsonl --config config/approval.yaml --output run_output/routes.jsonl

# 3-Way Invoice Matching
python -m app.cli match --data-dir data --config config/approval.yaml --output run_output/invoice_decisions.jsonl

# Entity Inspection
python -m app.cli inspect --entity po --id PO-000001 --data-dir data
```

### 4.3 Independent Evaluation & Leakage Verification
Executed from `generated/`:
```bash
# Validate bundle compliance, run oracle comparison, and ensure no private ID leakage
python -m evaluator_private.validate --bundle bundles/harbor_demo/input --profile demo --report reports/demo_validation.json
```

---

## 5. Determinism and Environment Standards
- **Seed**: Must be initialized to `20260925` using `random.Random(20260925)`. Global `random.seed()` or non-deterministic hash iteration is forbidden.
- **File Ordering**: All CSV and JSONL records must be sorted by primary key (`request_id`, `po_id`, `receipt_id`, `invoice_id`).
- **Binary Normalization**: Workbook properties (`created`, `modified`) and zip timestamps must be fixed to the reference date `2026-09-01T00:00:00Z` to guarantee repeatable hash checks.
