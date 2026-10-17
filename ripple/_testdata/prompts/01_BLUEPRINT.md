# Stage 01 — Blueprint and locked interfaces

Operator: use SYSTEM_INSTRUCTION_EN; attach FROZEN_SPEC. For the first batch use BATCH_ID=01a. Continue with explicit batch ID after reviewing the prior output.

---

Design Harbor Procurement Mini exactly according to FROZEN_SPEC. Do not implement Ripple or produce all application code yet. Return the standard file-bundle JSON.

Inputs required: complete FROZEN_SPEC; any explicitly approved amendments. If missing, NEEDS_INPUT.

Create these private authoring outputs in batches:

1. `authoring/blueprint.md`: system boundary, business flows, precedence, concrete public artifact tree, private tree, build dependency DAG, supported vs unsupported behavior. Keep 10–16 small app modules, no framework overengineering.
2. `authoring/contracts.json`: exact record schemas/enums/field types, function signatures and return types, CSV columns, workbook sheet names/cells, CLI commands, config keys, file ownership per generation stage and module dependency graph. Include a contract_version. Do not put semantic truth IDs into public config.
3. `authoring/build_contract.md`: build commands and read/write allowlists. Author application and docs into `authoring/templates/input/`; builders materialize `bundles/harbor_demo/input/`. Private canonical templates/build specs must never be copied as folders into public input. Build a conflict-free canonical representation where possible, then mutate only C01/C02 for final S0. G01/G02 remain explicitly incomplete capabilities as specified.
4. `operator/generation_batches.md`: exact sequential file batches for stages 02–09; necessary inputs for each; conditions to stop for human clarification.

Freeze proposed command surfaces, implemented later:

- From `generated/`: `python authoring/build.py --bundle harbor_demo --seed 20260925 --output bundles/harbor_demo/input`.
- From public `input/`: `python -m app.cli route --requests data/requisitions.jsonl --config config/approval.yaml --output <separate-run-dir>/routes.jsonl`.
- From public `input/`: `python -m app.cli match --data-dir data --config config/approval.yaml --output <separate-run-dir>/invoice_decisions.jsonl`.
- From `generated/`: `python -m evaluator_private.validate --bundle bundles/harbor_demo/input --profile demo --report reports/demo_validation.json`.
- Scenario generation and independent evaluation commands must take explicit source-state/scenario IDs; no default source version guessing.

Approval config stores routing limits/precedence parameters; workflow YAML expresses routing flow and intentionally duplicates some business values to permit cross-source comparison. Explicitly state which source app code executes. Excel is operational guidance, not a secret authoritative config loader. Include policy-vs-observed-vs-authority separation.

No external service, model ID, pricing, credential, database or package version may be fabricated. Dependency ranges may be proposed; exact lock is pending real installation/testing.

Before ending, identify contract ambiguities with exact questions. If none, set questions=[] and list next batch. All execution checks NOT_RUN. Do not begin stage 02 until the operator accepts this blueprint/contract version.
