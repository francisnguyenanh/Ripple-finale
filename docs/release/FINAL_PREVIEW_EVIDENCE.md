# Final development preview: curated evidence

This file is a curated summary of the project's own runs (self-checks). It is **not** independent validation and **not** an acceptance record. Raw run records stay in the private development repository; nothing here is a measured quality rate of a model. Claims and their limits: [`FINAL_CLAIMS_EVIDENCE_LIMITS.md`](FINAL_CLAIMS_EVIDENCE_LIMITS.md).

Dates follow the development machine clock (JST). Some run identifiers contain `20261018` although the clock read 2026-10-17; the identifiers are kept as recorded.

## How to read a row
Each row names the scope, the source snapshot it was run on, the provider mode, the result as recorded, and whether it can be reused. Results from overlapping runs are never added together.

## Test runs (provider mode: synthetic stubs, no provider call, disposable local PostgreSQL unless stated)

| Scope | Snapshot (dev commit) | Result as recorded | Notes |
|---|---|---|---|
| Exported tree stands alone: `python -m pytest tests/contract tests/unit -q` | `8f0cc8b` (earlier export) | 1238 passed, 3 skipped, 1 deselected, 12 s | Historical, **not** a result for this snapshot, and no timing promise on other machines |
| Contract suite after the Git connector and public docs | `c24bcb8` | 1154 contract passed | one targeted run |
| Unit suite after the decided-conflict guard | `49b88f6` | 114 unit passed | targeted |
| Product Git connector, mocked transport | `2dd3bdb` | 12 passed | pull-request path is mocked only |
| Code change and git sync | `2dd3bdb` | 12 passed, 1 skipped | |
| Listener tokens and projection, no emulator | `91476e2` | 11 passed, 4 skipped | the skipped cases need the emulator |
| Listener tokens and projection, local Firestore emulator (loopback, `demo-` project) | `91476e2` | 12 passed | unsigned emulator tokens; no cloud |
| Change understanding, impact dependencies, commit, routing, reconciliation | `49b88f6` | 23, 34, 5, 5, 9 passed (per file) | |
| Multi-agent review board and arbiter | `3ae5d38` | 6 passed | synthetic specialists |
| QA, retrieval, answer reading, interview, variants | `7d33889` | QA 15; retrieval 22 passed + 6 xfail; reading 23; interview 6; variants 5 | |
| Export of the committed tree + verification | `539cdb0` | kept 1293, secret-pattern scan 0, verify problems 0 | a scan is not a guarantee that no secret exists |

A skipped, interrupted or helper-only run is not a pass. One earlier bundle run was stopped by the host (low memory) and was never counted as a pass; its files were rerun individually afterwards.

## Live model runs (self-check, one provider, hard cost cap)
- **Pilot, 2026-10-17:** every Final AI role ran end to end once or a few times (28 calls, Gemini 3.5 Flash-Lite; one paired case on Gemini 3.8 Flash). No rate was measured; no gate was closed.
- **Agent-authored sealed sets, scored mechanically (self-check):** questions 10/14, then 8/14 after a server fix; change requests 6/8. The sets were written by the project's own agent, so this is **not independent**. Observed weaknesses: required citations missing on 4 of 14 questions with the lighter model; unreliable on three-part requests; abstention wording varies.
- **Diagnostics on a held-out set (non-independent, 2026-10-17/18):** failures were classified and several server guards added. The set is spent for gating; it is not used as a gate set. Two disputed labels remain with the owner, unchanged.
- **Live journey runs, 2026-10-21/22 (self-check, Gemini 3.5 Flash-Lite, one workspace built from seven Harbor source files, operator standing in for the person through the real routes; frozen inputs / expected answers / manifest per set; owner allocation per run):** v1 J1 stopped at its gate (the CAPEX route was not read; product cause fixed); v2 J1 stopped (code outcomes missing; product cause fixed: code facts); v3 J1 **passed** (4 / 4 required rules, CAPEX route traced to `approval.yaml` and `approval.py`), J2 no suggestion (product cause fixed: candidate pairs shown, prompt v2); v4 J2 **passed** (the model suggested the CAPEX disagreement; synthetic decision 2,000,000), J3 1 / 5, J4 held by a model-raised advisory question (product cause fixed: typed questions); v5 J3 re-ask 0 / 2 (model omissions; two server limits found and fixed after the run, shown on the stored outputs by replay through the real callers), J4 **passed** to the refresh (reused change, guidance, decision, commit, business document, refresh with an honest `pending` status); v6 (one continuous attempt on a new workspace): J1 **passed** (34 of 42 candidates with an outcome or actor from the model; the CAPEX route traced), J2 refused before any model call by the prompt's input ceiling (34 rules plus 22 open questions and 52 relations; fixed afterwards) and the journey stopped by its frozen rules; v6c (continuation on the v6 knowledge after the fix, 9 calls): J2 suggested the CAPEX disagreement by content with its prompt coverage recorded, J3 5 / 5 shown answers (model-only 3 / 5), J4 **passed** to the refresh; v7 (fresh attempt on a new workspace, one version): J1 **passed** (35 / 35 candidates with outcome or actor from the model), J2 the model suggested the right disagreement and the server refused its question as over the length limit (fixed afterwards), stopped. No zero-tolerance breach in any run (wrong rule cited 0, converted amount 0, fabricated reference 0). Not independent, seen questions, n = 1 per step.
- Total recorded provider cost for all live work under the mandate: **1.09885270 USD** of a 2.00 USD cap (0.17475295 at the earlier snapshot; the journey runs and the request-assistant evaluations since).
- The earlier Pitch-era held-out evaluation (2026-10-02) did **not** reach its targets ([`../evaluation/HELD_OUT_EVALUATION.md`](../evaluation/HELD_OUT_EVALUATION.md)).

## Local-service evidence beyond tests
- A private test repository was created for a **read-only** product fetch and REST reads (0 writes from the product). One pull request and one advisory commit status were created manually under the owner's authorisation; nothing was merged. The product-created pull request path itself is tested on a mocked transport only.
- A local Firestore emulator verified the Firestore sink, rules and listener tokens. No cloud Firebase/GCP resource exists for the Final build.

## Not evidenced
Any human acceptance of AI quality; real-model quality rates; one continuous end-to-end live run on the final version (the journey runs were staged across code versions); an independent reviewer-built gate set (a protocol exists, no result); real Firebase, Firestore, hosting or listener latency; a deployed Final build; a product-created pull request on a real repository; fresh-clone behaviour of every README command beyond the rehearsal below.

## Fresh-setup rehearsal of the public tree
Recorded in the release notes of the public repository (section "Rehearsal") for the exact public commit; it is a small rehearsal (installation, contract/unit subset, rehearsal-server start), not a full run.
