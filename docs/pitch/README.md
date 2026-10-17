# First live run (2026-10-05): from a change request to a verified change, with a GenAI draft you can audit

This folder records one live run of the closed loop on the six-file Harbor sample. Every statement below points at recorded evidence ([`LIVE_EVIDENCE.md`](LIVE_EVIDENCE.md), [`CLAIMS_TO_EVIDENCE.md`](CLAIMS_TO_EVIDENCE.md)). Approver and confirmer accounts in this run were **simulated**; this is not human acceptance.

## What Ripple does
Ripple keeps the business rules that live in your project files (YAML, Markdown, workbooks, code) and answers one question safely: *"If I change this rule, what else changes, and can I apply it with proof?"*

The closed loop, as built and run:

1. **Import** the documents; every file becomes stored observations with locators and an index.
2. **Extract** rules with a model (`gemini-3.8-flash`, Vertex global); candidates carry quotes that the server re-checks against the stored text.
3. **Request**: a person states the change in a structured form (subject, scope, unit, currency, the value they believe is current, the new value) and confirms it.
4. **GenAI draft** (`change.draft`): the model reads the request and the authorized evidence and classifies every affected location (edit / manual / no-op / exclude / ambiguity) with citations and questions for a human. The server verifies each citation, reads the **current value from the stored file**, takes the **proposed value from your confirmed request**, and only then materializes a typed edit for a simple YAML value. It can lower what the model says, never raise it. Anything the model skipped is shown as *unaddressed*.
5. **Proposal** generated from the draft (nothing retyped), impact analysis and simulation, a **verification contract** typed by a second person from the *request* (not from the proposal or the model), review, approval by a distinct approver, **apply** with exact-digest and stale checks, readback and verification.
6. **Feedback**: re-extraction of the changed sources, knowledge rebuild, a **loop status** that stays `PARTIAL` while a source is still old, ambiguous or unclassified, and a **query on the new revision** that must cite the new text.

AI is used where judgement is needed (extraction, draft analysis, the question at the end). Permissions, approval, apply, verification and persistence are ordinary code with tests.

## What the live run showed (Harbor demo, six public files, 2026-10-05)
Request: lower the Manager approval limit from 1,000,000 to 500,000 JPY for standard orders.
- The draft proposed the one YAML edit (`routing_rules.standard_manager_threshold_jpy` 1,000,000 → 500,000), left the policy document as **manual**, marked the workbook value (already 500,000, no header) **ambiguous**, **excluded** the unrelated 500,000 free-freight rule, and flagged `workflow.yaml`, the emergency scope and `calculations.py` as **not covered / unaddressed**.
- After review, approval and apply: readback matched, verification **PASSED**, 8/8 re-extraction jobs completed, and asking again returned **500000** citing the new revision.
- Overall status: **PARTIAL**. The Harbor request is **not** complete; only the supported YAML sub-loop is. See the screenshots in [`screens/`](screens).
- Cost of the whole live run: about 0.35 USD of a 12.82 USD cap (ledger-read), about 16 provider calls.

## Limits
- Only simple YAML values are applied automatically; Markdown, code and workbook changes stay manual.
- One model run on one scenario: no accuracy, superiority or generalization claim. The held-out evaluation ([`../evaluation/HELD_OUT_EVALUATION.md`](../evaluation/HELD_OUT_EVALUATION.md)) did not reach its targets and is not superseded by this run.
- The C02-style scope mismatch is covered by a fixture test; the live finding job did not produce it in this run, so it is not claimed live.
- Knowledge acceptance has open blockers after apply; they are listed, not hidden.
- The conservative input guard (bytes as a token bound) is deliberately strict; the real request was about 3k tokens against a 24k bound.

## Run the related tests
- Offline, needs a throwaway PostgreSQL DSN in `PITCH_BRIEF_TEST_DSN`: `pytest tests/pitch_brief tests/pitch_cmp`
- Browser test: `tests/pitch_brief/test_ui_flow.py` (Playwright + Chromium, stub provider).
- Deployment notes: [`DEPLOY_NOTES.md`](DEPLOY_NOTES.md); credentials only through the environment or a secret store, never in the repo.

Third-party notices: [`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).
