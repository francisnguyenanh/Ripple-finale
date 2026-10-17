# Stage 08 — Independent review and bounded repairs

Operator: preferably use a fresh conversation. Supply system instruction, frozen spec, accepted contracts, requested file subset and actual validation outputs. Reviews without executed outputs must explicitly say which tests remain NOT_RUN.

---

Review Harbor Procurement Mini as a reference dataset/software package. Your output is the normal file-bundle JSON, with findings saved under `reports/review/`. Default MODE=review: do not edit implementation/oracle. MODE=repair is allowed only when the operator provides approved finding IDs and editable paths.

Review lenses, with a separate finding list for each:

1. Business: precedence, scope/exception, approval vs routing, invoice validation order, duplicate history semantics, document-only SLA, source authority and unknown emergency future policy.
2. Implementation: standalone input runtime, no private imports/global config/LLM/network, Python3.11 compatibility, exact integer calculations, data schema consistency and actual commands.
3. Evidence/artifacts: genuine XLSX, preserved sheet/format/formula semantics, forms match source records, docs match actual modules, only planned C01/C02 differences plus G01/G02 uncertainty, no made-up evidence hashes/locators.
4. Evaluation: independent expected path, anchor assertions fixed, source truth not inferred from app outputs, actual-vs-normative distinction, public/hidden leakage, holdout contamination and computed denominators.
5. Demo: config-only change reveals emergency problem; complete approved change affects correct rows; second change uses S1 baseline; freight stays unchanged; restore does not imply authority reset; finance-guide conflict remains unless S5 included.

Finding schema: id, severity, category, file+symbol/locator, actual behavior, violated spec clause, reproducible check, suggested repair, expected side effects, status. Add `intentional_fixture: true/false`; do not classify known intentional conflict as accidental build defect.

If MODE=repair:

- Change only approved findings/paths; output complete replacement files in small batches, with before file digest if the operator provided one. Do not fabricate a digest.
- Never update expected oracle to fit current code unless there is an explicit human-approved policy amendment. Never delete hardcoded emergency S0 behavior or normalize all documents just to make policy checks green.
- A bad generator may require rebuilding derived data/forms; list exact affected builders/artifacts and revalidation steps. Do not manually patch 120 records independently from their generator.
- Do not mark resolved until actual rerun evidence supplied. Return NOT_RUN validation commands as needed.

Finish with a clear GO/NO_GO/CONDITIONAL report for dataset use, backed by each acceptance criterion. Model opinions alone cannot set executable checks to PASS or establish semantic gold without human review.
