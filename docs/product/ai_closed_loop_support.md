# AI closed-loop support matrix — product contract

> **Scope note.** This document defines the support contract: what each format and capability is meant to support. It is not a statement of what the shipped build delivers. In the shipped build, patches, review and simulation are deterministic (no AI-drafted patch, no multi-model review); AI is used only in stages with provider evidence (extraction, gap answer, impact judge); relation linking and conflict findings are deterministic stages over extracted candidates; **Python and Markdown are proposal-only** (not applied, verified or simulated), so the PATCH and EXECUTE cells below that say Yes for `.py`/`.md` describe contract intent, not delivery; apply, verify and simulate exist for YAML and XLSX only; EXPORT is claimed only for committed YAML/XLSX revision bytes (`tests/ai10t03/test_tester_scope_export.py`). The PATCH/EXPORT cells are intentionally left as the contract states them.

This is a **contract**, not a report: every number and cell here is a design decision that acceptance and negative tests are written against and every product surface (API/UI/worker) must enforce with the exact error code named. No number in this file is a measured result — measured numbers live in `docs/evaluation/ai_closed_loop_metrics.md` and future run reports.

## 0. Owner decisions (project owner, 2026-09-25)

These were made by the project owner and are binding unless a later ADR amends them. "H" below denotes the current delivered scope (demo profile):

1. **Primary AI-value criterion:** A1 (AI-enabled) raises semantic relation/condition-conflict recall by **≥ 20 percentage points** over D0 (deterministic-only) on the defined hard group, while holding the precision floor for that same metric (relation precision ≥ 0.95, conflict precision ≥ 0.90). Human-workload reduction (≥ 25% setup-time reduction) is the **secondary** criterion and is reported **only if** a human trial actually happens; it is never substituted for the primary criterion, and the primary criterion is chosen now, not after seeing results (the primary criterion is fixed up front, never chosen afterwards for being favourable).
2. **Advertised source language at H: English only.** Vietnamese and Japanese source documents are roadmap items — not evaluated, not advertised as supported, and refused with `unsupported_format` if uploaded with a declared non-English language tag (§2.3 below spells out the detection rule, since language is not always declared).
3. **Cost budget at H:** ≤ **1.00 USD** per analysis job, ≤ **50.00 USD** total for evaluation plus demo. Exceeding either refuses the job with `budget_exceeded` (contracts §6). The exact admission arithmetic, config keys and failure-closed rules are in `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md`.
4. **Self-approval:** allowed in the demo profile (H), with a mandatory audit record naming the actor as both requester and approver. The pilot default is `require_distinct_approver=true` (self-approval refused with `forbidden` unless an explicit per-workspace override is set — see §5).

## 1. Format support matrix (READ / INFER / PATCH / EXECUTE / EXPORT)

**Column definitions** (a format's row is read left to right; a capability not offered returns the named error code, never a silent no-op):

- **READ** — bytes can be imported as a `SourceRevision`/`ArtifactRevision` (the spike's `source.py`/`db.py` shape) and cited as evidence by file + line/cell locator.
- **INFER** — the AI extraction pipeline is allowed to propose rules/relations/conflicts grounded in this format's content. A format can be READ-only (evidence source, e.g. an invoice) without being INFER-eligible (never treated as a policy document).
- **PATCH** — the AI may propose an exact, reviewable diff against this format ; always requires human approval before write (J06/J09).
- **EXECUTE** — a runner (ADR-005 §2.6 sandbox) can execute or simulate this artifact directly (J06/J12; e.g. running a Python routing script). Config/data formats that are only *consumed by* an executable are marked N/A here, not "yes" — the executable that reads them is what is executed. **Current implementation:** the shipped code does NOT execute user-derived artifacts at all; `infra/worker/sandbox_runner.py` is a resource-limited subprocess (not a security boundary) used only for fixed internal programs, and the review check `simulation_exact_patch` is UNAVAILABLE (blocking) for a python proposal. The ADR-005 §2.6 isolation described in this table is a design target, not shipped behaviour.
- **EXPORT** — the system can serialize an edited artifact back to valid bytes of that format that the native tool opens without corruption (J18).

| Format | READ | INFER | PATCH | EXECUTE | EXPORT | Notes |
|---|---|---|---|---|---|---|
| Markdown (`.md`) | Yes | Yes (policy prose; the spike's `extract.py` is the seed) | Yes (line-anchored diff) | N/A | Yes | Primary policy-document format. |
| YAML (`.yml`/`.yaml`) | Yes | Yes (typed config-as-rule: thresholds, routes, approval maps) | Yes (key-path diff) | N/A (consumed by the Python route/workflow simulator, which is what executes) | Yes | Must parse as a single YAML document (multi-document streams are `unsupported_format`); anchors/aliases resolved before evidence citation, cited by resolved key path. |
| Python (`.py`, no C-extension modules) | Yes | Yes (AST-level literal/threshold extraction, not free-text inference over the source) | Yes (exact contextual diff; never freeform rewrite) | **Yes** — pinned-digest sandbox only (ADR-005 §2.6): network none, read-only rootfs, `--user 65534:65534`, wall-clock timeout then kill | Yes (must remain syntactically valid; a patch that fails `ast.parse` is refused before proposal, `unsupported_format` is not used for this — it is a validation error at proposal time, not a format-support gap) | `import`/`exec`/`eval` of any input file content is forbidden everywhere outside the sandboxed runner (spike C09 rule, unchanged for production). |
| XLSX, **no macros** (`.xlsx`, VBA project absent) | Yes (cell values + formulas via a formula-preserving reader; named ranges resolved) | Yes (cell/threshold rule extraction, scoped to cells the extractor is pointed at — never "read every cell as a candidate rule") | Yes, **cell-scoped only**: a patch touches only the reviewed cell(s); every other cell/style/named range/formula is byte-for-byte unaffected (J18) | N/A | Yes — the exported workbook must open in the native tool; a cell whose formula result depends on a value Ripple changed is marked "recalculation status unknown" rather than claimed fresh (J18; no engine here recomputes Excel formulas) | A workbook whose formula graph the reader cannot resolve (a `#REF!`/circular reference reachable from a target cell) is `unsupported_format` for that cell's PATCH, not for the whole file — other cells stay usable. |
| XLSX **with a VBA/macro project** (`.xlsm`, or `.xlsx` containing a `vbaProject.bin` part) | **No** — refused at import | No | No | **No — never**, in any release. Macro execution is out of the ADR-005 §2.6 sandbox's threat model (it sandboxes *our* runner, not Excel's own macro host) | No | Import of any file with a detected macro project returns `unsupported_format` immediately, before any parsing of sheet content. This is a permanent exclusion, not a roadmap item — re-admitting macros requires a new ADR. |
| CSV (`.csv`) | Yes | **No** — CSV rows are treated as **observations** (transactions, master data), never as policy/rule statements, matching Harbor's own `data/*.csv` (employees, suppliers) and the J02 rule ("transactions are observations; do not promote them to confirmed policy") | No (bulk data files are not AI-patch targets at H; a future bulk-observation-correction feature is roadmap) | N/A | N/A (no export path defined at H; requesting one returns `unsupported_format`) | Row order and any duplicate rows are preserved verbatim on READ — CSV is evidence, and evidence must not be silently deduplicated or reordered. |
| JSONL (`.jsonl`, one JSON object per line) | Yes | **No**, same reasoning as CSV (Harbor's `data/*.jsonl`: invoices, purchase orders, receipts, are observations) | No | N/A | N/A | A line that fails to parse as a JSON object does not fail the whole file: that one record is excluded from evidence with a per-line warning recorded in the import result, and the import as a whole still succeeds (partial-file tolerance is for observation logs, not policy documents, where it would be dangerous — Markdown/YAML/Python/XLSX import is all-or-nothing). |
| Any other format (Word, PDF, PowerPoint, HTML, plain text, images, ...) | **No — not supported**, refused per file: listed as `skip_required` / `unsupported_format`; the batch cannot be committed until each file is acknowledged as skipped; a skipped file is never read | No | No | N/A | N/A | The build ships no parser, adapter or registry for these formats. Test: `tests/pitch_brief/test_source_scope.py`. |
| Harbor HTML invoice forms (`forms/invoices/*.html`) | **No — explicit decision, roadmap** | No | No | N/A | N/a | Harbor ships 24 of these (`ripple/_testdata/generated/bundles/harbor_demo/input/forms/invoices/*.html`, counted by `find … -iname '*.html' | wc -l` = 24). They duplicate the same transactions already present as structured JSONL (`data/invoices.jsonl`), so there is no coverage loss in refusing them at H: the JSONL is READ+evidence-eligible, the HTML rendering of the same data is not. Building an HTML-forms parser is out of scope for the English-only, ≤1.00 USD/job budget at H. Uploading one returns `unsupported_format`. Promoting HTML support is a roadmap item for whichever phase adds a second observation-ingestion format. |

### 1.1 Byte, file, token, job and concurrency limits

| Limit | Value at H | Rationale / source |
|---|---|---|
| Single artifact byte size | **1 MiB** (`1_048_576` bytes), exactly the spike's `source.py` limit (`MAX_BYTES`) — unchanged | Spike-proven; a production bundle's individual files are all well under this (Harbor's largest file is checked below). |
| Files per source bundle (one import batch) | **200** | Matches Harbor's own artifact count order of magnitude (`find ripple/_testdata/generated/bundles/harbor_demo -type f | wc -l`, counted below) with headroom; a bundle over this is refused with `unsupported_format` at the batch level (not a per-file rejection — the whole batch is atomic, matching the spike's "manifest hash over the sorted artifact list"). |
| Total bundle byte size (one import batch) | **20 MiB** | Chosen to keep one import well under the `RIPPLE_V2` worker's single-job wall-clock budget (§1.2) even for a slow XLSX parse; re-tuned only with a new ADR if a real pilot bundle exceeds it. |
| Max output tokens per provider call | **2048** (the spike's own `--max-output-tokens` default used in tests; ADR-005 §2.9 "no budget default" — the worker refuses to start without an explicit value, unchanged) | See `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` §3 for how this is enforced end to end (spike gap, now closed as a contract). |
| Max provider retries per job attempt | **1** (`complete_structured`'s existing `retry_limit=1`, unchanged; total ≤ 2 real model calls per job attempt) | Reused unchanged from `core/agent/provider.py::complete_structured` — do not add a second, worker-level retry loop on top of it. |
| Max job attempts (`analysis_job.max_attempts`) | **3** (spike default, unchanged) | Matches the spike's `jobs.enqueue(..., max_attempts=3)` default. |
| Concurrent jobs per workspace | **4** | A demo-scale number; the spike's `claim()` has no workspace filter (documented spike limitation), so workspace-scoped concurrency accounting must be added before this limit is enforceable — until then this is a **contract value with no enforcement**, tracked as an open dependency. |
| Concurrent jobs, whole deployment (H) | **8** | Matches the ≤ 50.00 USD total evaluation+demo budget (§0.3): at the per-job cap of 1.00 USD, 8 concurrent long-running jobs is a deliberately conservative ceiling, not a throughput target. |

Harbor bundle sizing actually measured (recount, not asserted from memory):

```
find ripple/_testdata/generated/bundles/harbor_demo -type f | wc -l   -> 79
find ripple/_testdata/generated/bundles/harbor_demo -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn
  24 html, 16 pyc, 16 py, 9 md, 6 jsonl, 3 xlsx, 2 yaml, 2 csv, 1 txt   (sums to 79, matches the plain count)
```

`.pyc` files are compiled artifacts, never imported as source, so the importable file count for this bundle is 79 − 16 = 63, well under the 200-file limit above.

### 1.2 Language

**English only**, advertised, at H (owner decision §0.2). Detection rule: a bundle's manifest may declare `language: en` explicitly; if it does not, the importer runs no language auto-detection at H (that would itself be an AI capability requiring evaluation) and instead **assumes English** for READ/INFER purposes, but the product page and demo script must say "English source documents only; non-English content is not evaluated and results are undefined" rather than silently attempting extraction. A bundle manifest that declares a non-English `language` value is refused at import with `unsupported_format`. This is a documentation/gating decision, not a language-ID feature — there is no code to build for H beyond checking one optional manifest field.

### 1.3 Supported rule expressions, currency and effective dates

- **Comparators:** `==`, `!=`, `<`, `<=`, `>`, `>=` — matches the spike `extract.py` item shape (`operator` field) and `core.kernel` money/threshold conventions.
- **Values:** integers and decimal strings for money/thresholds (ADR-005 §2.7: v2 canonical form **refuses `float`** for money — this product contract restates that as a rule-expression constraint: a rule whose `value` is a JSON float, not an integer or a string parseable as a `Decimal`, is rejected at extraction time with a per-item rejection reason, the same mechanism the spike's `extract.py::_reject_reason` already uses for malformed evidence).
- **Currency:** JPY only at H, matching every Harbor bundle's `unit` field (`"JPY"`, verified: `grep -c '"unit": "JPY"' ` over the pinned Harbor fixture's extracted rules — all 3 verified rules in `tests/spike_ai0t02/fixtures/extract_rules_verified.json` use `"unit": "JPY"`). A rule whose extracted `unit` is anything else is not refused (the model may still be right about a mixed-currency document — J03/boundary-corpus intent), but is flagged `currency_not_evaluated` in the extraction result for human review, not silently treated as JPY.
- **Effective dates:** a rule's evidence may carry an `effective_date`/`superseded_date` pair (ISO-8601 `YYYY-MM-DD`); a rule with no effective-date evidence is treated as effective from its source revision's `created_at`. Two rules covering the same subject/scope with overlapping effective windows is itself a **conflict** (drift/condition-conflict detection target), not silently resolved by "latest wins."
- **Scope expressions:** free-text `scope_text` (as in the spike) plus a structured `exception` list (roadmap; not built at H, but the schema must reserve the field under the rule that UNKNOWN requires a reason/GAP ref — an absent structured scope is `UNKNOWN`, never defaulted to "applies to everything").

## 2. Sources of authority

**Never** file order, upload order, or model confidence. Authority is determined **only** by an explicit, project-configured policy record (`authority_policy`, one per workspace, itself a versioned, immutable revision like every other artifact in the schema set):

1. A named **role/title-based hierarchy** (e.g. "Board policy > Finance Department SOP > Manager memo > individual email"), configured per workspace before any import.
2. Within the same authority tier, the **most recently effective** revision wins (by `effective_date`, §1.3), never by import recency.
3. An **explicit supersession record** (a human decision, per the decision schema) always outranks tier + date — a person can mark "document X supersedes document Y" even if Y's tier or date would otherwise win, and that decision is itself audited and revocable (J19).
4. If no authority policy is configured for a workspace, or if two sources tie on every rule above, the conflict is surfaced as **unresolved** (contracts §6 `unresolved_gap`), never silently picked by which file was read first or which the model was more confident about. This is the literal test of "never file order or model confidence": the evaluation oracle must include at least one case where the deterministic-only baseline (D0) and the AI path see the same two conflicting sources in different `os.listdir`/import order and must produce the identical authority decision either way.

### 2.1 High-risk blockers

A finding is **high-risk** (blocks unattended publish/apply, forces a human gate regardless of confidence) when any of:

- The conflict involves a **currency or threshold amount** difference where the higher amount exceeds the lower by more than an order of magnitude (a likely unit/currency-mismatch bug, not a real policy disagreement) — surfaced, never auto-resolved.
- The proposed change **widens** an approval-bypass condition (e.g. raising a self-approval or auto-clear ceiling) — always requires a **distinct approver** regardless of the self-approval setting (§0.4), even in the demo profile.
- The evidence for a rule comes from a **single source with no corroboration** and contradicts an existing, previously-confirmed rule (single-source override) — flagged for human review before REMEDIATION or supersession.
- Any change to the **authority policy** itself (§2) — never proposed or applied by the AI path; authority policy is human-authored and human-edited only (it is the referee, not a player).
- A patch would touch a file **outside the declared minimal scope** for the intent (the "minimal patch scope" metric; 0 out-of-scope writes is the H target) — such a patch is refused before proposal, `invalid_evidence`.

### 2.2 Self-approval policy

- **Demo profile (H):** self-approval (the same actor is both requester and approver) is **allowed**, but every self-approved decision writes an explicit audit record with `self_approved: true` and the actor id in both the `requested_by` and `approved_by` fields — never indistinguishable from a distinct-approver decision in the audit trail.
- **Pilot default:** `require_distinct_approver=true` per workspace. A self-approval attempt under this setting is refused with `forbidden` (contracts §6), not silently downgraded to "pending." Switching a workspace to allow self-approval is itself an authority-policy-tier action (§2), audited, and reversible.
- A **high-risk** finding (§2.1) always requires a distinct approver, in every profile, regardless of the workspace's `require_distinct_approver` setting — the high-risk gate cannot be bypassed by the self-approval setting.

## 3. UI/API behaviour per unsupported cell — error codes (contracts §6)

Every "No"/"N/A" cell in §1's matrix, and every limit breach in §1.1–§1.3, resolves to exactly one of the ten error codes contracts §6 already names (`forbidden`, `stale_revision`, `invalid_evidence`, `unresolved_gap`, `unsupported_format`, `unsupported_simulation`, `provider_unavailable`, `budget_exceeded`, `runner_failed`, `recovery_required`). This contract does not invent new codes; it maps every unsupported cell onto one of the ten:

| Unsupported cell / condition | Error code | UI message intent |
|---|---|---|
| Macro-enabled XLSX/XLSM upload | `unsupported_format` | "Workbooks containing macros are not supported. Remove the macro project (Excel: Developer → Visual Basic → remove modules, or Save As `.xlsx`) and re-upload." |
| Harbor HTML invoice form upload | `unsupported_format` | "HTML invoice forms are not a supported source format. Import the equivalent JSONL/CSV transaction data instead." |
| Non-English `language` manifest field | `unsupported_format` | "This source is declared as a non-English language, which is not yet supported. English-only sources are supported in this release." |
| Bundle exceeds file-count or byte-size limit (§1.1) | `unsupported_format` | "This import exceeds the [N files / N MiB] limit. Split it into smaller bundles." |
| A CSV/JSONL INFER (rule/policy extraction) request | `unsupported_format` | "Observation data (CSV/JSONL) is never treated as a policy source; rule extraction/inference does not run over these formats." |
| A CSV/JSONL PATCH or EXPORT request | `unsupported_format` | "Observation data files (CSV/JSONL) cannot be edited or exported by the AI proposal flow." |
| A simulation/execution request against a non-Python artifact (YAML, XLSX, Markdown) | `unsupported_simulation` | "Only Python routing/workflow scripts can be simulated; [format] is not executable." |
| Authority tie with no configured policy, or an unresolvable GAP | `unresolved_gap` | "This conflict needs a human decision: [named conflict], no authority policy resolves it." |
| A patch proposal reaching outside its declared minimal scope | `invalid_evidence` | "This proposal touches files outside its declared scope and was refused before review." |
| Self-approval attempt with `require_distinct_approver=true` | `forbidden` | "This workspace requires a different person to approve than the one who requested the change." |
| Job/estimate would exceed the per-job or total budget cap | `budget_exceeded` | "This job would exceed the [per-job / total] cost budget and was not started." See `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md`. |
| Sandbox/runner crash, timeout, or non-zero exit not itself a semantic finding | `runner_failed` | "The simulation could not complete (runner error). No result was produced; retry or escalate." |
| Provider outage, refusal, or unparseable output (spike A6/A7, unchanged) | `provider_unavailable` (outage) — `invalid_evidence` is NOT used for this; unparseable output surfaces as the job's terminal `error_code=invalid_output`/`no_verified_evidence` per the spike, exposed through the same `provider_unavailable`-shaped "AI could not complete this analysis" UI state | "The AI analysis could not complete this run. No conflicts were found (this is not the same as zero conflicts existing) — retry later." |
| A stale/superseded revision is referenced by an action | `stale_revision` | "This action targets a revision that is no longer current. Refresh and retry." |
| Apply/verify blocked pending a required manual step (J11) | `recovery_required` | "A required manual action is not yet complete; this change cannot be marked resolved until it is." |

## 4. Every product claim mapped to a gate

| Product claim | Gate it must map to | Evidence location |
|---|---|---|
| "The AI finds rules and conflicts a keyword search would miss" | AI-value ablation (D0 vs A1, ≥20pp recall) | `docs/evaluation/ai_closed_loop_metrics.md` §4 (ablation protocol); run report, once it exists |
| "Evidence always points at real text" | Evidence reference validity = 100% | Spike's `check_evidence_quotes_in_lines`/rework R1 gate (`core/agent/validate.py`, `spike_ai0t02/extract.py::_reject_reason`); promotion tests |
| "A bad model answer never gets silently published" | C19 / "AI publishes nothing on outage/refusal" | Spike A6/A7/A8/rework-R1-R2 tests; `provider_unavailable`/`invalid_output`/`no_verified_evidence` codes (§3 above) |
| "Cost is bounded per job and per demo" | Budget admission, §0.3 | `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` |
| "The system supports English procurement documents in these formats" | Format support matrix, §1 | This file |
| "A person always approves before anything is written to a real file" | J06/J09, human-approval journeys | permission/state-machine tests (future) |
| "Self-approval in the demo is visible in the audit log, not hidden" | §2.2 | Self-approval audit record schema |
| "Nothing is claimed about Vietnamese/Japanese support" | §0.2/§1.2 | This file |
| "The system never claims 0 conflicts when the AI failed to run" | J17 | §3 table row for `provider_unavailable` |
| "Non-conflict, same-number transactions are not silently promoted to policy" | J02, §1 CSV/JSONL INFER=No | §1 table |

## 5. Recount ledger

Every count in this file, recomputed from a command immediately before this line was written:

- HTML invoice forms: `find ripple/_testdata/generated/bundles/harbor_demo -iname '*.html' | wc -l` → **24**.
- Total files in the Harbor demo bundle: `find ripple/_testdata/generated/bundles/harbor_demo -type f | wc -l` → **79**.
- File-type breakdown (`find … -type f | sed 's/.*\.//' | sort | uniq -c | sort -rn`): 24 html, 16 pyc, 16 py, 9 md, 6 jsonl, 3 xlsx, 2 yaml, 2 csv, 1 txt — sums to **79**, matching the plain-file count exactly (cross-checked, no discrepancy).
- Pinned Harbor CAPEX rule fixture currency: `grep -c '"unit": "JPY"' tests/spike_ai0t02/fixtures/extract_rules_verified.json` → **3** (all three verified rules).
