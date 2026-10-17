# Stage 02 — Runnable Python application

Operator inputs: system instruction, frozen spec, accepted blueprint/contracts, BATCH_ID and already generated dependency modules. Request 2–4 small modules per turn; use continuation prompt for more.

---

Implement only the requested core application batch for Harbor Procurement Mini. All authored public files first live in `authoring/templates/input/`; final builder copies them into the public input root. Required contracts must be present; otherwise NEEDS_INPUT, do not guess imports/signatures.

Deliver, over bounded batches:

- `app/__init__.py`, typed models, config loader/validator, standard routing, separate emergency routing, human approval checks, invoice matching, CLI and output serializers; exact filenames follow accepted contracts.
- `config/approval.yaml` and workflow YAML. Preserve R01→R02→R03→R04/R05 precedence. Source implementation baseline manager limit 1m; emergency handler literally uses 1m after the shared risk/capex checks, without a comment revealing this is a test trap. Runtime config explicit; no reading XLSX manager row as a runtime override.
- `tests/` ordinary application characterization tests: invalid inputs, blocked/high-risk/capex priorities, threshold boundary, maker/checker, invoice duplicate normalization, quantity/tolerance/due-date behavior. Test current source, not private future S1 expectations. No private scenario IDs or expected Ripple conflicts in public tests.
- `README.md`, local requirements (unverified lock state clearly labelled), and optional FastAPI mini UI **after** CLI works. UI limited to viewing requests/invoices and submitting deterministic simulations to the local reference core, no public auth/security claims. CLI/core must remain independent of FastAPI.

Constraints:

1. Functions pure with explicit config; never read source defaults at import. Separate route recommendation from actual approval state. A NEEDS_MANAGER decision is not APPROVED.
2. All amounts integer JPY and exact synthetic tax/freight arithmetic. Validate bool-as-int, nonpositive goods net/quantities, negative freight/tax, unsupported currency, supplier/PO mismatches; zero freight/tax is valid. No truthy fallback for missing required fields.
3. Reference app inputs/outputs are JSON-serializable. CLI processes explicit file paths supplied by an operator and writes only to an explicit separate run directory; no arbitrary network or script execution. This CLI is not a public server endpoint.
4. File output contains observed actual decisions only. No hidden canonical expected labels, oracle imports, Ripple imports or vertex calls. Manual 2-business-day escalation is described as unsupported automation; do not implement a pretend scheduler.
5. Produce no stubs, `pass` implementations, ellipses or swallowed exceptions returning success. Diagnostics distinguish invalid data from intentional held invoices.
6. Do not fix C01/C02 here: they are document/workbook mutations later. Do not preemptively make emergency threshold follow config; this observed S0 hardcode is needed to expose partial-change consequences.

Return complete files for the batch, commands for syntax/tests, statuses NOT_RUN unless tools really executed. List dependency files needed next. Implementation must run from a copy of final `input/` alone, without sibling private directories.
