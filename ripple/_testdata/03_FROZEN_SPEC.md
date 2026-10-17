# FROZEN_SPEC v1 — Harbor Procurement Mini

This is the private authoring specification. It is **not a file to ingest into Ripple**. Private IDs Rxx/Cxx/Gxx/Sxx are for generation/evaluation only. Public artifacts use natural business wording and ordinary local identifiers, never a shared answer map across every file.

## A. Product and scale

- Fictional company: Harbor Office Supply, a small Japanese office-equipment buyer. All names/addresses are fictional, emails use `.example`, no real banking/tax identifiers. Invoice documents visibly say `SYNTHETIC DEMO — NOT FOR PAYMENT`.
- Language: English baseline. Currency: integer JPY only, no currency conversion. All dates injected; dataset reference date `2026-09-01`. Money means net goods amount unless a field explicitly says freight/tax/gross. Tax fixture arithmetic is illustrative, not jurisdictional tax guidance.
- Stack: Python 3.11-compatible, pure domain functions, argparse CLI, YAML config, CSV/JSONL exchange, openpyxl XLSX, pytest. Optional read-only/minimal command web UI using FastAPI+HTML. No database, cloud, LLM calls or external payment in the reference application.
- Target size: 10–16 small Python modules, approximately 1,000–2,000 authored application LOC (not a hard metric to pad), 8–12 Markdown documents, 3 workbooks, 12 rendered invoices and 12 goods-receipt vouchers selected from records. Keep builders/oracles/private manifests outside public input.
- Demo data: seed `20260925`, 20 suppliers, 8 employees, 240 requisitions, 120 POs, 140 receipt events, 120 invoice records. Add 24 negative/edge-only cases in a **separate private validation file**, not silently counted in the 240/120 business denominators. All sizes must be verified after execution.
- Data counts are targets, affected counts are **not prescribed**. Calculate affected outcomes from actual generated records; never reuse "47/220" from Ripple's older seed.

## B. Required boundaries and layout

```text
generated/
  authoring/                       PRIVATE: contracts, build specs, builders, locks
  evaluator_private/              PRIVATE: oracle, expected, scenarios, answers, mutations
  reports/                        PRIVATE: actual executions/evidence/metrics
  operator/                       PRIVATE: setup, demo script, import allowlist
  bundles/
    harbor_demo/
      input/                      ONLY this is imported into Ripple
        app/                      runnable Python package
        config/                   operative config and workflow YAML
        data/                     raw business CSV/JSONL and 3 real XLSX workbooks
        docs/                     business/design/manual Markdown
        forms/                    HTML invoices/receipts (presentation, optional ingestion)
        tests/                    ordinary characterization tests of current application
        README.md                 normal system README, no hidden scenario clues
        requirements.txt          versions pinned after actual resolution/test
    harbor_dev_01/input/ ...
    harbor_holdout_01/input/ ...
```

The public app must work when copied alone out of `generated/`. No imports or path reads reaching authoring/evaluator/Ripple repository. Public artifacts must not mention expected conflict IDs, seeded bug explanations, desired scenario patches, oracle files, evaluation metadata or hidden impact mappings. `input/tests` may describe existing behavior as a normal legacy test suite; they do not reveal private future desired outcomes.

No full public file tree may expose a central manifest saying "these artifacts represent rule R04". Runtime config is ordinary application input and allowed; it must not contain hidden semantic equivalence/impact labels. Private manifest separately records exact artifact hashes/evidence locators after materialization.

## C. Data contracts to freeze in stage 01

Use stable IDs and explicit fields. Blueprint may add nonsemantic descriptive fields but cannot rename/remove these semantics without listing a human-approved amendment.

- Supplier: supplier_id, display_name, status ACTIVE/BLOCKED, risk NORMAL/HIGH, fictional contact.
- Employee: employee_id, display_name, department, roles; sample roles BUYER, MANAGER, FINANCE, DIRECTOR. These are reference business roles, **not Ripple's permission identities**.
- Requisition: request_id, requester_id, supplier_id, process STANDARD/EMERGENCY, category OPEX/CAPEX, net_amount_jpy, currency, requested_at, quantity, item_description. No expected outcome embedded in inputs.
- RouteDecision: request_id, route REJECTED/AUTO/NEEDS_MANAGER/NEEDS_FINANCE/NEEDS_DIRECTOR, reason_code, amount_used_jpy, applied_config_version. Route recommendation is not a record of human approval.
- PO: po_id, request_id, supplier_id, currency, quantity, goods_net_jpy, freight_jpy, tax_jpy, gross_jpy, state DRAFT/APPROVED, approved_by nullable, approved_at nullable. Validate state against the generated decision/approval events; AUTO may approve by a clearly labelled automatic system event.
- Receipt: receipt_id, po_id, quantity_received (>0), received_at, received_by, condition ACCEPTED/DAMAGED. Only ACCEPTED quantity counts; timestamps and units explicit.
- Invoice: invoice_id, supplier_id, supplier_invoice_number, po_id, quantity, goods_net_jpy, freight_jpy, tax_jpy, gross_jpy, currency, received_date, issue_date. No bank account/payment execution. Normalized duplicate key specified below.
- InvoiceDecision: invoice_id, status REJECTED/HOLD_UNAPPROVED/HOLD_QUANTITY/HOLD_PRICE/READY, reason_code, due_date nullable, matched_po_id, comparison_details. READY means eligible for AP review/payment scheduling, not money moved.
- AuditEvent: event_id, entity_type/id, action, actor_id, occurred_at, before/after fields where relevant; generated deterministic observations, never fake Git history.

Public call seam: `route_request(request, config)` and `route_emergency(request, config)` return route outcomes in a stable form; `evaluate_invoice(invoice, po, receipts, seen_keys, config)` is pure and does not mutate seen_keys. `approve_request(request, route, actor)` checks maker/checker and role before producing an event. Config loaded at boundary, passed explicitly; no import-time default global.

Receipt aggregation and duplicate history are explicit inputs. Corpus replay sorts by `(received_date, invoice_id)` and updates seen_keys only after the separate ingestion step accepts a nonduplicate invoice identity; financial HOLD still records identity. Reference evaluator must use the identical documented history convention, not call implementation helpers.

## D. Business contract and precedence

Private IDs are forbidden in public code/docs. These are **synthetic demo rules**, not legal/accounting advice.

| Private ID | Meaning and baseline S0 semantics |
|---|---|
| R01 | Invalid/nonpositive goods-net amounts or quantities, negative freight/tax, or unsupported currency are rejected before business routing. Zero freight/tax is valid. Boolean must not count as integer. No float money. |
| R02 | BLOCKED supplier -> REJECTED. Otherwise HIGH risk -> NEEDS_DIRECTOR regardless of category/process/amount. |
| R03 | Otherwise CAPEX with net goods amount >=2,000,000 JPY -> NEEDS_FINANCE. OPEX does not take this branch. |
| R04 | Otherwise STANDARD purchases with net goods amount >=1,000,000 JPY -> NEEDS_MANAGER; below -> AUTO. Authoritative policy and operative config agree on 1m at baseline. |
| R05 | EMERGENCY current implementation applies the same priorities R01/R02/R03, then a literal 1,000,000 JPY threshold in its own handler. Public emergency procedure says "obtain appropriate sign-off" without stating whether it must follow future general-limit changes. Authority for future emergency scope is intentionally UNKNOWN until a human decision. Current implementation is still observable. |
| R06 | Human approver cannot be requester; required role must match route (MANAGER/FINANCE/DIRECTOR). No unannounced role substitution. AUTO is a system event; do not invent a human approval. |
| R07 | Invoice duplicate key = supplier_id + trim().upper() supplier_invoice_number. Same supplier/normalized number already seen -> REJECTED; same number from different supplier is not duplicate. Validate invoice/currency/PO-supplier consistency first. |
| R08 | Invoice requires APPROVED PO, then invoice quantity equals PO quantity and sum of ACCEPTED received quantity is at least invoice quantity. Otherwise HOLD_UNAPPROVED or HOLD_QUANTITY in that order. Partial receipt policy allows holding; no automatic partial payment. |
| R09 | After R07/R08: abs(invoice goods net - PO goods net) <=10,000 JPY AND 100*abs(diff) <=2*PO goods net. Both inclusive; no division/float rounding. Otherwise HOLD_PRICE. |
| R10 | Invoice due date = received_date +30 calendar days, calculated only for READY; payment not executed. Synthetic tax = floor(goods_net_jpy*10/100), freight separate, gross=goods_net+freight+tax. Apply consistently across records/rendered forms. |
| R11 | SOP alone requires escalation of unanswered approval requests after 2 business days. Reference app has no holiday calendar/scheduler to implement it; do not silently add implementation. This is a document-only rule and an implementation GAP, not a fabricated running capability. |
| R12 | Freight is 0 for goods net >=500,000 JPY, otherwise 2,000 JPY. It is a distinct rule and an explicit negative control for semantic matching/threshold change. Never change this just because approval thresholds also become 500k. |

Routing is first-match priority R01→R02→R03→R04/R05. Invoice evaluation order: validation → duplicate → approval → quantity → price → READY with due date. Tests must cover precedence; counting manager approvals from every branch is incorrect.

## E. Intentionally inconsistent baseline representations

Start from a mechanically consistent clean build privately, then apply exactly these controlled artifact variations to materialize the public S0 bundle. Do not put mutation instructions in public input.

| Private ID | Public observation | Expected nature |
|---|---|---|
| C01 | `data/approval_matrix.xlsx`, manager row, says 500,000; policy, workflow, standard code use 1,000,000 | Value conflict; spreadsheet already satisfies future S1 manager limit |
| C02 | `docs/finance_desk_guide.md` says purchases of any category at/above 2m go to finance; policy, config and code say CAPEX only | Scope conflict with same numeric value |
| G01 | Emergency procedure vague about scope of future general limit changes; current handler hardcodes 1m | Human clarification needed; no forced assumption from hidden contract |
| G02 | 2-business-day escalation exists in SOP only | Document-only rule; unsupported execution/implementation gap |

There are no other intentional differences in the base demo. Add isolated variants for boundary operator errors, effective-date ambiguity, missing evidence, prompt injection and renamed vocabulary only in stage 07. Do not put injection content into the default judge bundle.

Workbook manager value is guidance, not the runtime config read by the application; describe data flow normally so it is credible. Finance guide C02 is an ordinary stale document, not a file labelled "wrong guide". Baseline public tests characterize code semantics and should pass; private policy/conformance checks must detect intended differences and report them separately from packaging/program failures.

## F. Data coverage and reproducibility

- 240 requisitions contain mandatory witnesses **before** pseudorandom filling: 12 STANDARD and 12 EMERGENCY, ACTIVE/NORMAL supplier, OPEX, 600,000 JPY; enough witnesses demonstrate S0→S1 and S1→S2 route changes without guessing counts.
- Include boundary values 499999/500000/500001, 749999/750000/750001, 999999/1000000/1000001, 1999999/2000000/2000001 across categories/processes as valid coverage permits. Keep fixed mandatory cases with stable IDs; random fill cannot overwrite them.
- Include high-risk/blocked/capex precedence controls; unrelated freight matches must remain unchanged across all approval-only changes. One same-number supplier identifier/description must not be mistaken for a threshold.
- 120 POs map to distinct non-rejected requisition IDs; state/events reflect actual baseline route and scripted valid human actions. Some DRAFT POs intentionally have arriving invoices, which should be held rather than magically approved. Do not create POs for blocked-supplier/rejected requests merely to hit the target count.
- 140 receipt records: 100 POs have one event, 20 have two; those 20 use quantities permitting split. Include a declared subset of partially received/damaged cases while keeping foreign keys and positive quantities valid. Document which cases are intentionally HOLD in private catalog.
- 120 business invoices map one per PO; duplicate/invalid/over-quantity adversarial cases live in private 24-case validation set. Business invoice price deviations include tolerance boundaries; freight copies the PO freight, invoice tax is calculated on its own goods net, and its gross is recomputed. Stage 03 defines exact case allocations and locks them, not leaves them to chance.
- Render 12 invoice +12 receipt vouchers drawn from actual records; no separate fabricated totals or names. Printed totals match CSV/JSONL and workbooks.
- Sort keys/rows explicitly, use local Random(seed), stable IDs and injected dates. No Python hash() to derive seeds. Confirm two runs have same logical records and semantic workbook digest; normalize ZIP timestamps/doc properties if claiming byte-identical XLSX.

## G. Scenarios and human decisions

| Scenario | Input/request | Expected constraint |
|---|---|---|
| S0 | Import baseline | Detect C01/C02, ask G01, surface G02; no automatic authority choice |
| S1 | Human confirms 500k manager limit for all STANDARD and EMERGENCY; preserve CAPEX/risk priority and freight rule | Standard config/workflow/docs update; emergency literal must become shared configurable limit; Excel already 500k is no-op. Finance guide conflict remains unless explicitly included in separate approved remediation. G02 stays unresolved. |
| S2 | After S1, human sets manager limit to 750k for both processes | Compare actual S1 vs S2, not original S0; Excel now needs update. Freight still 500k. Recompute counts. |
| S3 | Restore source content S0 after S1 or S2 | New revision event with historical bytes; authority decisions retained by Ripple; new knowledge may still conflict with active policy. Do not claim rollback erased decisions or made global alignment true. |
| S4 | From S1, external/manual refresh changes Excel manager threshold to 650k | Detect new drift/value conflict; source/KM/approval freshness behavior belongs to Ripple, generator only provides revised files. |
| S5 | Authority chooses CAPEX-only finance rule and approves correcting finance desk guide | Separate narrow document change; no update to 2m value or OPEX executable logic; baseline/S1 manager behavior unaffected. |

Operator supplies fixture human answers at the appropriate Ripple workflow step, not via automatic prompt injection or hidden answers in imported sources. Required decisions include G01; G02 may remain explicit scoped GAP and must not be labelled globally verified. A low-scope approval change may complete for its declared scope if Ripple policy allows, but the overall system still has open G02/C02.

## H. Hand-calculated anchors for independent oracle

Routing below uses ACTIVE/NORMAL, OPEX unless specified; expected S0 **actual implementation**, not unknown emergency policy authority:

| Case | S0 | S1 | S2 |
|---|---|---|---|
| STANDARD 499999 | AUTO | AUTO | AUTO |
| STANDARD 500000 | AUTO | NEEDS_MANAGER | AUTO |
| STANDARD 600000 | AUTO | NEEDS_MANAGER | AUTO |
| STANDARD 750000 | AUTO | NEEDS_MANAGER | NEEDS_MANAGER |
| STANDARD 1000000 | NEEDS_MANAGER | NEEDS_MANAGER | NEEDS_MANAGER |
| EMERGENCY 600000 | AUTO | NEEDS_MANAGER | AUTO |
| CAPEX 2000000 | NEEDS_FINANCE | NEEDS_FINANCE | NEEDS_FINANCE |
| OPEX 2000000 | NEEDS_MANAGER | NEEDS_MANAGER | NEEDS_MANAGER |
| HIGH-risk CAPEX 2000000 | NEEDS_DIRECTOR | NEEDS_DIRECTOR | NEEDS_DIRECTOR |
| BLOCKED supplier 100000 | REJECTED | REJECTED | REJECTED |

Invoice anchors, assuming approved PO, exact quantities, correct currency, new invoice key: PO 400000/invoice408000 -> READY; invoice408001 -> HOLD_PRICE; PO1000000/invoice1010000 -> READY; invoice1010001 -> HOLD_PRICE. Include symmetric negative deviation. Unapproved PO -> HOLD_UNAPPROVED even when price wrong; quantity mismatch -> HOLD_QUANTITY before price. Duplicate valid identity -> REJECTED before approval/matching. READY received 2026-09-01 -> due 2026-10-01.

Expected evaluator covers observable implemented behavior and approved scenario semantics separately from unresolved normative questions. It must return UNKNOWN for the unconfirmed future emergency policy scope, not silently treat hidden authoring knowledge as human approval.

## I. Definition of dataset-ready

App runs from input folder alone; characterization tests pass; private oracle anchors pass independently; exact planned conformance issues detected; binaries reopen; referential/arithmetic consistency holds; source/expected separation scanner passes; scenarios report computed counts and revision hashes; cleanup/build writes stay inside provided output root; all execution claims backed by actual logs. Human reviews policy/oracle and relevant ambiguous cases before using results as a benchmark.
