# AI closed-loop evaluation metrics — locked provisional thresholds

Every threshold below is **locked before any prompt-selection run**: thresholds are fixed, with a stated reason, before held-out evaluation. No number in this file is a measured result. No metric here may be re-tuned after seeing held-out results without the holdout being retired and a fresh one drawn (the holdout is hash-locked).

Companion documents: `docs/evaluation/METRIC_DEFINITIONS.md` (matching-policy conventions) and `docs/product/ai_closed_loop_support.md` (format, authority and self-approval decisions).

## 1. Reuse of `docs/evaluation/METRIC_DEFINITIONS.md`

`METRIC_DEFINITIONS.md` already fixes the general TP/FP/FN convention (§1 of that file: `precision = TP/(TP+FP)`, `recall = TP/(TP+FN)`) and nine legacy-pipeline matching policies (§2 of that file, table rows 1–9: `evidence_grounding`, `hidden_rule_detection`, `drift_conflict_detection`, `impact_precision`/`impact_recall`, `intent_classification`, `simulation_accuracy`, `change_completeness`, `unnecessary_change_rate`, `post_apply_drift_count`).

This file **reuses that general convention unchanged** (§2 below restates it once, so this file is self-contained) and **reuses four of those nine matching policies directly**, renamed to the metric names they correspond to here:

| Metric | `METRIC_DEFINITIONS.md` row reused | Matching unit (unchanged from that file) |
|---|---|---|
| Evidence reference validity | #1 `evidence_grounding` | 1 claim; evidence points at the correct artifact **and** a location containing the claimed content |
| Impact quality | #4 `impact_precision`/`impact_recall` | 1 artifact path; **DIRECT and INDIRECT impact counted separately** |
| Behavioral correctness | #6 `simulation_accuracy` | 1 scenario; outcome matched on **both** current and shadow state |
| Minimal patch scope | #7/#8 `change_completeness`/`unnecessary_change_rate` | 1 required artifact present in `actions` (#7) / 1 action with no corresponding impact evidence (#8) |

The remaining metrics (semantic extraction, relation precision/recall, conflict detection, correct abstention, authorization/integrity, knowledge freshness, human workload, cost/latency) have **no existing row** in `METRIC_DEFINITIONS.md` — §3 below defines each from scratch, in the same style (matching unit + matching rule), so a reader of only this file has everything needed without cross-referencing.

## 2. General convention (restated from `METRIC_DEFINITIONS.md` §1, unchanged)

```text
precision = TP / (TP + FP)
recall    = TP / (TP + FN)
F1        = 2 * precision * recall / (precision + recall)
```

**Reporting granularity (binding for every metric below: numerator/denominator reported per group and macro per bundle, never a single overall accuracy):**

- Every metric reports its **raw numerator and denominator**, not only the resulting ratio (a ratio with no denominator is not verifiable).
- Every metric reports **per-group** breakdowns, where "group" is defined per metric below (e.g. conflict type, artifact impact direction).
- Every metric additionally reports a **macro-per-bundle** average (the metric computed once per bundle, then averaged across bundles, unweighted by per-bundle case count) alongside the corpus-wide micro figure — a single large bundle must never dominate the headline number.
- A metric with a zero denominator for a bundle is reported as `N/A (0 cases)` for that bundle, never as `0%` or omitted silently.

## 3. Per-metric definition: policy, numerator, denominator

### 3.1 Evidence reference validity — reused (`METRIC_DEFINITIONS.md` #1)

- **Numerator:** evidence references that resolve to the correct artifact revision **and** locator (file:line or sheet:cell) **and** contain the exact claimed text/value at that locator (spike's `check_evidence_quotes_in_lines`, now production in `core/agent/validate.py`, is the mechanical implementation this metric measures the outcome of).
- **Denominator:** all evidence references accepted into a candidate or finding.
- **Group:** by artifact format (§1 of the support matrix: Markdown/YAML/Python/XLSX).
- **Fabricated or cross-tenant references accepted** is reported as its own count, separately, and must be **exactly 0** in every run — this is a correctness invariant, not a rate to optimize.

### 3.2 Semantic extraction (new; no reused row — closest existing analogue is `hidden_rule_detection` #2, which matches by `semantic_id` alone; this metric additionally requires the fields to match)

- **Matching unit:** 1 extracted rule vs. 1 oracle rule.
- **Match rule:** same `subject` (semantic equivalence, not string identity — oracle reviewer judges, because a quote existing is not the same as the quote entailing the claim), same `operator`/comparator class (an oracle `>=` matched by an extracted `>` on the same threshold is a **partial match**, reported separately, never silently counted as TP or FN), same `value`+`unit` (exact, after currency/unit normalization), same `scope_text`/`exception`/`outcome` semantics (reviewer-judged equivalence, not string match).
- **TP:** all four match. **Partial match:** subject+value match but operator class or scope differs — reported as its own count, not folded into TP or FP. **FP:** extracted rule with no matching oracle rule. **FN:** oracle rule with no matching extracted rule.
- **Denominator (precision):** all extracted rules for the bundle. **Denominator (recall):** all oracle rules for the bundle.
- **Group:** by rule "hard group" (§4 defines the hard group used by the ablation) and by format.
- **Critical policy semantics** (an authority-tier-1 document per §2 of the support matrix) must never be auto-confirmed by this metric alone — a TP here still requires human sign-off before REMEDIATION per the support matrix's high-risk blockers.

### 3.3 Relation precision/recall (new)

- **Matching unit:** 1 directed relation edge (`(src, type, dst, scope)`), matching `METRIC_DEFINITIONS.md`'s general edge-matching spirit but scoped to this project's relation schema (not reused directly — no existing row covers directed typed edges with scope).
- **Match rule:** TP requires identical `src`, `type`, `dst` **and** `scope` — a relation with the right endpoints but wrong direction or wrong type is a **FP** (counted, never silently treated as "close enough"), and separately reported under **hard-negative false merges** (§4) when the oracle specifically constructed that pair as a hard negative.
- **Denominator (precision):** all relation edges the system proposed. **Denominator (recall):** all oracle relation edges (held-out: ≥ 80 labelled relation pairs, positive + hard negatives).
- **Group:** by relation type.

### 3.4 Conflict detection (new)

- **Matching unit:** 1 conflict finding, matched by the **set of involved rule ids** (order-independent) **and** conflict type (`value` vs. `condition/scope`, reported separately: value and condition/scope are reported on their own).
- **Match rule:** TP requires the same rule-id set (exact set match, not "at least one rule in common" — a finding naming rules {A,B} when the oracle says {A,B,C} is a **partial-set match**, reported separately, not TP) **and** the same conflict type.
- **De-duplication:** the same underlying conflict reported more than once (e.g. once per pairwise rule comparison instead of once per maximal conflicting set) is **counted once** against the denominator — a naive implementation that reports `n*(n-1)/2` pairwise findings for one n-way conflict must not inflate TP.
- **Denominator (precision):** all conflict findings raised. **Denominator (recall):** all oracle conflict cases (held-out: ≥ 24 conflict cases and 24 non-conflict controls).
- **Group:** by conflict type (value / condition-scope) — this is also the metric the ablation (§4 below) measures for the primary AI-value criterion.

### 3.5 Correct abstention (new)

- **Matching unit:** 1 insufficient-evidence case (an oracle-labelled GAP case where the correct system behavior is a question/`unresolved_gap`, not a confident answer).
- **Numerator:** insufficient-evidence cases where the system returned an appropriate question/GAP (not a confident, wrong, or fabricated answer).
- **Denominator:** all insufficient-evidence oracle cases (the held-out set has ≥ 24 GAP cases including ≥ 12 insufficient-evidence cases — this metric's denominator is specifically the insufficient-evidence subset, not all 24 GAP cases; the other GAP cases, where evidence IS sufficient but ambiguous, are scored by §3.2/§3.4 instead).
- **False abstention:** the complementary failure — an **answerable** case (sufficient evidence exists) where the system incorrectly abstained/asked a question instead of answering. Reported as its own rate over the answerable-case denominator, target **≤ 0.20**.

### 3.6 Impact quality — reused (`METRIC_DEFINITIONS.md` #4)

Unchanged matching unit and DIRECT/INDIRECT separation from `METRIC_DEFINITIONS.md` (1 artifact path; TP requires the correct path **and** the correct DIRECT/INDIRECT label, counted separately).

- **Numerator (precision):** TP artifact-impact predictions (correct path + correct direct/indirect label). **Numerator (recall):** same TP count, viewed against the oracle side.
- **Denominator (precision):** all artifact-impact predictions the system made for the change. **Denominator (recall):** the oracle's labelled impacted-artifact set per change scenario (≥ 12 change scenarios carry impacted/unimpacted artifact labels).
- **Missed required impacts** (an oracle-labelled DIRECT impact the system missed) is reported as its own count, separately from ordinary FN, because a missed DIRECT impact is a correctness failure of a different severity than a missed INDIRECT one.

### 3.7 Minimal patch scope — reused (`METRIC_DEFINITIONS.md` #7/#8)

Unchanged matching units (#7: 1 required artifact present in `actions`; #8: 1 action with no corresponding impact evidence).

- **Numerator (#7, change completeness):** required artifacts that are present in the proposed `actions`. **Denominator (#7):** all artifacts the oracle marks `required` for the change.
- **Numerator (#8, unnecessary-change rate):** actions with **no** corresponding impact evidence (out-of-scope writes). **Denominator (#8):** all proposed actions.
- **Source-already-compliant controls** (a bundle where the source already satisfies the intent and the correct patch is "no change") must produce **zero** proposed actions — any non-empty action set against such a control is a full FP against the #8 denominator, not a partial credit.

### 3.8 Behavioral correctness — reused (`METRIC_DEFINITIONS.md` #6)

Unchanged matching unit (1 scenario; outcome matched on **both** current and shadow state).

- **Numerator:** scenarios whose independently-expected outcome the system reproduced (on both current and shadow state) and, where the scenario requires execution, that the runner actually executed to completion (not merely attempted).
- **Denominator:** all mandatory acceptance/boundary scenarios in scope for the release check (target: 100% of mandatory acceptance/boundary cases before publish).
- **Coverage/excluded counts** (cases the runner could not execute at all — e.g. an `unsupported_simulation` format per the support matrix) are reported as their own denominator-adjustment line, never silently dropped from the denominator without being shown: a scenario excluded for `unsupported_simulation` is removed from this metric's denominator and counted separately, not folded into either TP or FN.

### 3.9 Authorization/integrity (new)

- **Matching unit:** 1 adversarial attempt (unauthorized publish, stale apply, cross-tenant read, fabricated approval — the same categories as spike A9's cross-workspace-404 test and the forbidden-transition tests, generalized to the full adversarial suite).
- **Numerator (the only number that matters):** attempts **accepted** (i.e., the security control failed). **Denominator:** total adversarial attempts run.
- **Target:** exactly **0 accepted** in the adversarial suite. This is stated explicitly as **not** a claim about real-world attack probability — it is a suite-coverage number, and the suite's own coverage (how many attack categories, how many attempts per category) must be reported alongside the 0, or the 0 is meaningless.

### 3.10 Knowledge freshness (new)

- **Matching unit:** 1 read (query) issued immediately after an apply/decision/refresh event in a scripted journey (J12/J14/J17-style scenarios).
- **Numerator:** reads that return the correct current source/KM/decision version (no stale-revision mixing).
- **Denominator:** all such reads in the critical-journey suite.
- **Target:** 100% for critical journeys (restated). A stale-but-labelled-stale response (the system explicitly says "rebuilding" or returns a revision id the caller can see is old) is **not** a failure of this metric — the failure mode this metric targets is a **silent, unlabelled** mix of revisions.

### 3.11 Human workload (new; secondary criterion, §0 of the support matrix)

- **Matching unit:** 1 completed human task (answering a question, reviewing a proposal, approving a change) in a scripted A2 (AI+HITL) trial.
- **Numerator (setup-time-reduction ratio, only computed when a real human trial exists):** A2 (AI+HITL) wall-clock time to resolution for a task set.
- **Denominator:** A0/manual-baseline wall-clock time to resolution for the **same** task set, performed without AI assistance (the secondary criterion is "≥25% reduction," i.e. `1 − numerator/denominator ≥ 0.25`).
- **Reported quantities regardless of whether a human trial has run:** count of questions asked, count of edits the human made to an AI proposal before accepting it, count of AI proposals rejected outright.
- **No percentage-saved claim (the numerator/denominator ratio above) is computed or published unless an actual human trial (A2 with real humans, not scripted) has run** — inventing a savings percentage without one is forbidden, and this file does not compute one from any simulated/scripted proxy; absent a trial, only the "reported quantities" line is published.

### 3.12 Cost/latency (new; `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` defines the exact computation)

- **Matching unit:** 1 completed `analysis_job`.
- **Numerator (cost):** the job's `actual_call_cost_usd` total across all its real provider calls (`docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` §2). **Denominator (cost):** the job's admitted `per_job_cap_usd` (so the reported ratio is "fraction of the per-job cap actually spent," alongside the raw USD figure).
- **Numerator (latency):** the job's measured wall-clock duration (claim to terminal status). **Denominator (latency):** the SLO target (below) — the reported ratio is "measured / target," alongside the raw seconds, at p50 and p95 over all jobs in the run.
- **Reported quantities per bundle/change:** provider tokens (input+output, per model), estimated cost (§ budget contract), wall-clock p50/p95 per job, and per-job failure counts by `error_code` (contracts §6).
- **Latency SLO targets** (targets to be measured against, never reported as already-achieved results):

  | Profile | p50 target | p95 target | Scope |
  |---|---|---|---|
  | H (demo profile) | ≤ 30 s | ≤ 90 s | One `analysis_job` (import → extraction → candidates), single bundle, replay-scale corpus (§4's dev/held-out bundles, 4–12 artifacts each) |
  | P (pilot) | ≤ 60 s | ≤ 180 s | One `analysis_job` at pilot-representative bundle size (larger, real documents) — wider budget acknowledging real documents are bigger and provider latency is less controlled outside a scripted replay |

  These are **targets**, stated as such in every report ("target: ≤30s p50 — measured: TBD"), never asserted as already-met without a dated run reference.

## 4. AI Essentialness / ablation protocol (D0 / A1 / A2)

Made concrete and locked here:

1. **D0 (deterministic-only):** explicit IDs, AST references, typed comparisons, lexical/regex matching and any heuristics already implemented in `core/` before this phase (e.g. `core.kernel`'s existing typed comparisons) — run over the **same** held-out corpus, parser, retrieval and source metadata as A1/A2. D0 must not be deliberately weakened: the baseline is never deliberately weakened and never drops a legitimate existing capability.
2. **A1 (AI-enabled):** D0 **plus** semantic extraction/linking/conflict/proposal, with a real provider (spike's `providers.py`/`extract.py` lineage) — no replay/stand-in for the measurement itself (live evidence is required for the acceptance journeys this metric feeds).
3. **A2 (AI + HITL):** the same A1 candidates, after human decisions, measuring workload/correctness separately (§3.11) and reporting what a human found in addition to A1's candidates (missed-then-caught-by-human cases), not folding that back into A1's own recall number.

**Hard group definition** (the group the primary criterion, §0.1 of the support matrix, is measured on): held-out **relation** and **condition/scope conflict** cases that satisfy at least one of:

- The two conflicting/related rules use **different vocabulary** for the same subject (renamed field/variable/column, a corpus manipulation that renames files/variables/columns).
- The relation/conflict requires reading **two different artifact formats** to establish (e.g. a Markdown policy threshold vs. an XLSX cell value) — this is the case a pure lexical/regex D0 baseline is least likely to bridge.
- The case is a **paraphrase** of a dev-set template, not a verbatim duplicate (a "paraphrase" corpus manipulation).
- The case is a **hard negative** (same numbers, different scope/subject — a "same-number negative" corpus manipulation) where the correct answer is "no relation/no conflict," and D0's typical failure mode is a false merge on the shared number.

This hard group is a **subset** of the full held-out relation/conflict corpus (≥80 relation pairs / ≥24 conflict cases); the primary criterion is measured on this subset specifically, and the full-corpus numbers are reported alongside it, not instead of it.

**Primary AI-value criterion (owner decision, restated exactly as the support matrix records it):** A1 recall on the hard group ≥ D0 recall on the hard group **+ 20 percentage points**, while A1 precision on the same hard group is **≥** the precision floor already set for that metric (relation precision ≥ 0.95, conflict precision ≥ 0.90 — i.e., the recall gain must not be bought by abandoning the precision floor).

**Live-run protocol:** at least **3 repeats** with a fixed model/prompt configuration and low temperature; the model may still be non-deterministic. Every run is reported individually, plus mean and range, plus failure counts. The semantic-quality floor must hold **across runs**, not only on a cherry-picked best run. Replay-based tests check domain determinism given fixed inputs (the spike's `ReplayProvider`/`prompt_key` mechanism) — this is a control-flow determinism check, not a claim that the live LLM itself is deterministic.

## 5. Split rules (locked before any evaluation run)

- **Split unit:** bundle/template family, never individual rows/rules within a bundle (split by bundle/template family, never a random split of rows from the same template). A dev bundle and its paraphrased/renamed variant are the **same family** and must land on the **same side** of the split (both dev, or both held-out) — never split across dev/held-out, or the paraphrase manipulation leaks the held-out answer key into a model that also saw the dev original.
- **Minimum corpus at H** (locked here as the target the corpus is built toward): ≥ 12 bundles, 6 dev / 6 held-out, each 4–12 artifacts, collectively covering Python/YAML/Markdown/XLSX plus observational CSV/JSONL; at least one bundle is documents-only (a deliberate `unsupported_simulation` case, matching the support matrix's Python-only EXECUTE column).
- **Holdout manifest hash is locked** once the split is finalized — this file does not itself hold that hash, but states the rule: any code/prompt change made **because** a specific held-out case failed retires that holdout; a fresh held-out set must be drawn before the next measurement claiming the locked thresholds in §3 are met.
- **Expected labels are stored outside** the upload bundle, embedding index, model prompt, and any seed manifest the runtime reads/caches — this is a corpus-construction rule, restated here because §3's metrics are meaningless if the oracle leaks into the path being measured.

## 6. What this file does not claim

This file contains no measured results. Every number in §3/§4 is a **target locked in advance**, before held-out evaluation, and every latency figure in §3.12 is explicitly labelled "target," not "measured." Measured results belong to evaluation reports that cite a dated run.
