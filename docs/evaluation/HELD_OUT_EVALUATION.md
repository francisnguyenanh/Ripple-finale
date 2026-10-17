# Held-out AI-quality evaluation (snapshot 2026-10-02)

This is the verification record of the one held-out evaluation of Ripple's AI-assisted analysis stages. **The evaluation did not reach its acceptance targets** (section 4). It was run on an earlier build (commit `f953ebe`) with model `gemini-3.5-flash-lite`; **it has not been re-run on the current code or on the model the live roles use now**, so it says nothing about current quality in either direction. Nothing in this repository claims production readiness, accuracy or superiority over a non-AI baseline.

The internal identifier of the acceptance criterion that was not met is **G-AI10**; other documents in `docs/release/` use that name.

## 1. What was evaluated

The stages below existed in the evaluated build. Relation linking and conflict findings are **deterministic stages that run over AI-extracted candidates**; they are not AI judgement, and their quality is bounded by what extraction produced.

| Step | Where | Provider call? |
|---|---|---|
| Import sources into a workspace, coverage | `infra/worker/source_ingest.py` | no |
| Extract rule candidates with evidence | `infra/worker/extraction.py` | **yes** |
| Link related rules (relations) | `infra/worker/relations.py` | no, deterministic identity graph over the extracted candidates |
| Conflict and gap findings | `infra/worker/findings.py` | no, deterministic over the extracted candidates |
| Answer a question about a gap | `infra/worker/gap_answer.py` | **yes** |
| Impact analysis | `infra/worker/impact.py` | no |
| Impact judge | `infra/worker/impact_judge.py` | **yes** |
| What-if preview, simulation | `infra/worker/whatif.py`, `simulation.py` | no |
| Proposal (typed edits), review, approval digest, apply, verification | `proposal.py`, `review.py`, `approval.py`, `apply.py`, `verification.py` | no |

Properties of the evaluated build that remain true of the code:

- A person approves an exact digest; apply recomputes it from the stored proposal, review and approval rows and refuses on mismatch (`approved_rows_digest_mismatch`).
- Apply, simulation and verification support YAML and XLSX only. Python and Markdown changes are shown as proposals for a person to make by hand.
- Source refresh happens only when someone calls the refresh routes (`POST /v2/sources/refresh/prepare`, then a human `.../commit`); it is not triggered by Git, and there is no webhook or background watcher.

## 2. Data and labels

- The evaluation corpus is **synthetic** (generated bundles), not real orders or customer data. Source language is English only.
- The held-out labels were **adjudicated by AI agents, not by humans**, and are not claimed to be equivalent to human review; agents can share correlated errors. Decisions in the scripted journeys (confirmer, approver) were scripted.

## 3. How it was run

A locked metrics contract fixed 227 labelled cases, 5 excluded as oracle-ambiguous, leaving **222 scored** (relation 77, rule representation 60, conflict 24, non-conflict control 25, gap 24, change scenario 12). Model `gemini-3.5-flash-lite` (Vertex, region global), commit `f953ebe`. Arms: D0 (AI off, the deterministic engine; definition in [`pitch/d0/DEFINITION.md`](../../pitch/d0/DEFINITION.md)) and A1 (AI on). A human-review arm was not run. Metric and matching definitions: [`METRIC_DEFINITIONS.md`](METRIC_DEFINITIONS.md) and [`ai_closed_loop_metrics.md`](ai_closed_loop_metrics.md). The per-run record with hashes was kept outside this repository, and the harness is not part of the routine test suites.

## 4. Results: the acceptance targets were not met

Three valid A1 runs (r1b, r2, r3) and one D0 run. Numerator/denominator.

| Metric (target) | D0 | A1 r1b | A1 r2 | A1 r3 |
|---|---|---|---|---|
| Hard-group recall, A1 vs D0 (A1 at least D0 + 20 pp) | 0.000 | 0.000 | 0.000 | 0.000 |
| Relation precision (>= 0.95) | 0/0 | 0/2 | 0/2 | 0/2 |
| Relation recall (>= 0.80) | 0/44 | 0/44 | 0/44 | 0/44 |
| Conflict recall (>= 0.85) | 0/24 | 0/24 | 0/24 | 0/24 |
| Conflict precision (>= 0.90) | 0/0 | 0/0 | 0/0 | 0/0 |
| Extraction precision (>= 0.90) | 10/17 | 37/98 | 32/95 | 37/100 |
| Extraction recall (>= 0.80) | 10/60 | 37/60 | 32/60 | 37/60 |
| Impact precision (>= 0.90) | 0/0 | 8/14 | 13/20 | 13/20 |
| Impact recall (>= 0.90) | 0/33 | 8/33 | 13/33 | 13/33 |
| False abstention (<= 0.20) | 12/12 | 4/12 | 4/12 | 3/12 |
| Correct abstention on insufficient evidence (>= 0.90) | 12/12 | 12/12 | 12/12 | 12/12 |

- In every A1 run the relation stage produced 2 false positives and 0 true positives and the conflict stage predicted no conflicts. These zeros are the measured result. Because relation and conflict stages are deterministic over extracted candidates, the weak extraction result is one input to them; this evaluation does not separate the causes.
- Correct abstention was 12/12 and hallucinated answers on insufficient evidence were 0. The figure is also 12/12 for D0 (which abstains on gap questions), so it is not evidence of AI value.
- **Executed journeys.** 12 change scenarios; 8 are YAML/XLSX and therefore eligible, 4 (Python 1, Markdown 3) have no apply path and were not run. Executed: 0 (r1b), 1 (r2), 1 (r3). In r2 and r3 the one executed YAML journey was behaviourally correct with 0 out-of-scope writes. That is one case; it supports no general write-safety claim.
- Ablation gain is not measurable (A1 hard-group recall 0.000 against D0 0.000).

### Not measured (no value claimed)

Evidence-reference validity, fabricated or cross-tenant evidence accepted, unauthorized acceptance, and knowledge freshness were **not measured**. Latency was computed from job durations in the database (extraction p95 about 21 s, gap answer and impact judge a few seconds) and is not the harness metric.

### Attempts and disclosures

- A1-r1 was disqualified (the worker spawned `gcloud` children during Google credential lookup, so its isolation trace was invalid) and is excluded from scoring; its cost counts. A1-r1b and A1-r2 each needed a second invocation that resumed only the one failed extraction unit (rate limit exhausted in r1b, invalid JSON in r2); the 37 successes were reused without a call. A1-r3 ran once.
- Retry backoff (base 20 s, max 120 s, via `RIPPLE_RETRY_BACKOFF_BASE_SECONDS` and `RIPPLE_RETRY_BACKOFF_MAX_SECONDS`) was set for r1b invocation 2, r2 and r3; r1 and r1b invocation 1 used the product defaults (6 / 60). This reliability change was made after two invalid live attempts.
- In r2 and r3 one executed journey stored an internal artifact id instead of a path; a deterministic, label-free tool mapped it to the logical path before scoring (one id each). The cause in the driver is recorded as a follow-up.
- Held-out booked cost (estimate, not billing): 0.96 USD of a 183.20 USD cap.

## 5. Limitations of this evaluation and of the evaluated build

- The relation stage omits comparisons when a bundle is large (a comparison cap): the single large bundle omitted 95 to 203 comparisons. This can hide relations.
- Evidence validity was not measured; extraction precision was below 0.40 in A1 runs, so extracted candidates need review.
- Only one very large bundle in the corpus; scale, concurrency capacity and pilot behaviour were not measured.
- English only, synthetic data, AI-adjudicated labels.
- No AI-drafted patches and no multi-model review in the evaluated build.
- Tenant and permission checks have unit and integration tests; **no comprehensive security audit or penetration test** is claimed. Backup and disaster-recovery drills were not completed.

## 6. Evaluated build versus later builds

The AI-path prompts for the 38 evaluated artifacts are byte-identical between the evaluated commit `f953ebe` and release commit `cb8e539`. Metrics were measured at `f953ebe` only. `cb8e539` differs by security fixes outside the AI path, disabling review-time execution of user Python (Python proposals are blocked), a conditional `<source` escape with no effect on the evaluated inputs, and an import-order refactor. Later builds, including the current code and the live roles' current model, were **not** re-measured; no metric is claimed for them until they are.

## 7. Earlier-build smoke test (2026-10-03), scope and provenance only

An authenticated smoke test of an earlier deployed build used test accounts: the approval was an automated approval simulation by a test account (user-authorized test; human acceptance not run). It covered browser login, UI import, analysis of one tiny synthetic file, review, approval, apply, verification PASSED and a downloaded YAML whose hash equalled the expected hash (one scalar changed), at 3 provider calls and 0.0038354 USD. After apply the change was still re-analysing, so it was not a finished result. n = 1: **no quality, accuracy, closed-loop or human-acceptance claim.** Observations recorded for that build and not re-checked on the current code: login throttling is per instance; there is no general request rate limit; auto re-analysis after apply spends provider budget; a 50 KB input can pass the input ceiling and then fail on a truncated provider response.

## 8. Direction for Phase 2

The measured gap is the first thing to address: better extraction and relation/conflict results, a re-evaluation of the current build against the D0 baseline recorded here, a human-review arm, and the criteria listed under "Not measured".
