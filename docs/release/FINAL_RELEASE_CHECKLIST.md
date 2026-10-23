# Final build: release checklist (what is done, what the owner decides)

| Step | State on this build | Where |
|---|---|---|
| Final knowledge loop implemented and verified locally with synthetic model stubs through the real routes, worker, database and browser | done (self-check) | [`FINAL_CLAIMS_EVIDENCE_LIMITS.md`](FINAL_CLAIMS_EVIDENCE_LIMITS.md) rows 1-13 |
| Contracts, quality plan with prompt pins, recovery runbook | done | [`../contracts/FINAL_KNOWLEDGE_LOOP.md`](../contracts/FINAL_KNOWLEDGE_LOOP.md), [`../evaluation/FINAL_AI_QUALITY_PLAN.md`](../evaluation/FINAL_AI_QUALITY_PLAN.md), [`../runbooks/FINAL_RECOVERY.md`](../runbooks/FINAL_RECOVERY.md) |
| README and judge documents rewritten for the Final build; broken links fixed | done | [`../../README.md`](../../README.md), [`../README.md`](../README.md) |
| Local rehearsal (simulated models) walks the loop from a fresh database | done (recorded in the development records) | [`../../README.md`](../../README.md) section 9 |
| Clean export of the committed tree: no development files, no unclassified path, publication scan clean, no broken relative link, contract and unit suites pass inside the exported tree | done for the export of this commit (see the manifest of that export) | `SUBMISSION_MANIFEST.json` at the export root |
| Real-model quality of every AI role | **OPEN**: a one-case-per-role live pilot ran on 2026-10-17 (self-check, 28 calls); five live journey runs 2026-10-21/22 (self-check, seen questions, one workspace) exercised source → knowledge → decision → answers → change → document → refresh with Gemini 3.5 Flash-Lite; the gate needs sealed unseen sets, thresholds and an approved window; model omissions remain (see the status table below) | quality plan sections 1, 4, 5; [`FINAL_PREVIEW_EVIDENCE.md`](FINAL_PREVIEW_EVIDENCE.md) |
| Extension packages (Git read, code change, policy decision, audit package, CI drift check, replay engine, multi-agent workflows, interview over one question, audience variants of documents, conflict mining, Firebase hybrid satellite with mock adapters) | done locally with fixtures/mocks/synthetic providers; every real integration, real-model quality and human acceptance **OPEN** | [`FINAL_CLAIMS_EVIDENCE_LIMITS.md`](FINAL_CLAIMS_EVIDENCE_LIMITS.md) rows 15-27, [`../contracts/EXTENSION_FIREBASE_HYBRID.md`](../contracts/EXTENSION_FIREBASE_HYBRID.md), [`../contracts/EXTENSION_MULTI_AGENT.md`](../contracts/EXTENSION_MULTI_AGENT.md), [`../contracts/EXTENSION_GOVERNANCE.md`](../contracts/EXTENSION_GOVERNANCE.md) |
| Human acceptance of documents, code instructions and suggestions | **OPEN**: a person, not an agent | quality plan section 4 |
| Publish, submit, deploy | **owner's decision**; nothing in this repository publishes or deploys | — |

The exported tree is produced by the development tool from a committed state; its manifest records the source commit, every file hash and the checks above. Self-check only; not independent validation.

## Release status by criterion (2026-10-22; evidence layered, nothing counted twice)

Four kinds of evidence are kept apart: **built** = the function exists and is verified locally with stub models; **journey** = exercised in a live journey run with a real model (self-check, seen questions, one workspace, operator standing in for the person through the real routes); **replay** = a defect found in a run was fixed afterwards and shown on the run's own stored output through the real server callers (no new call); **model** = the model's own output met the frozen check. A criterion is DONE only when every layer it needs is met; owner acceptance of a limit does not close a quality gate.

| Criterion | Built | Journey (live) | After fix, replay | Model | Status |
|---|---|---|---|---|---|
| Sources → observed knowledge with traced code / configuration links | yes | v3 J1 passed (4 / 4 required rules, CAPEX route traced) | — | partial: code outcomes / actors came from the server's code facts, not the model | PARTIAL (model omissions labelled) |
| Model-suggested disagreement → person's decision → accepted knowledge | yes | v4 J2 passed the frozen gate (no planted pair) | — | the suggestion named the right pair; band / actor precision not measured | PARTIAL |
| Grounded answers on a concrete case (A4a, A4b, A5, A19) | yes | v4: 1 / 5; v5 re-ask: 0 / 2; v6c: 5 / 5 shown; v8: 4 / 5; **v9 (fresh, one version): 5 / 5 shown** (no safety breach in any run) | A19 (both views), A5, A4a, A4b pass on the stored v5 outputs | v6c model-only 3 / 5: A4a, A19T, A19O on the model's own output; A4b notices the missing category but does not ask; A5 abstains without its own JPY question | **PARTIAL** (shown answers pass once; model quality below the shown answers; not independent) |
| Change request → guidance → person's decision → commit → document → refresh | yes | v5 J4 passed to the refresh (change reused from v4; guidance v2; decision and commit by the operator for the person; document; refresh read back; honest `pending` for the prose rule) | — | guidance: one advisory question, no blocker | PARTIAL (passed in the recorded synthetic scope; not human acceptance; not runtime verified) |
| One continuous end-to-end live run on the final version | yes | **v9 (2026-10-22, new workspace, one version `0b410d23`): J1, J2, J3 (5 / 5 shown), J4 to the refresh (`verified`, source-aligned) — every gate passed.** Earlier attempts v6, v7, v8 stopped on product defects, all fixed (history kept) | — | v9 model-only answers 2 / 5 (the server binds passages and asks the fact questions) | **DONE at B** (synthetic, self-authored, not independent; operator-simulated decisions; behaviour not tested) |
| Independent evaluation on unseen, reviewer-built sets | — | — | — | — | **OPEN** |
| Human acceptance of AI quality | — | — | — | — | **OPEN** (owner accepted the layered evidence as recorded, not the quality) |

Total recorded provider cost for all live work under the 2.00 USD mandate: 1.22117030 USD (ledger reconciled with the database after every run).

## Extension roles: live diagnostics of 2026-10-22 (self-authored sets, frozen before collect, Flash-Lite; synthetic data or the owner's Harbor sandbox)

| Role | Live result | Threshold | Status |
|---|---|---|---|
| SUR-04 semantic drift advisory (on local diffs of the owner's Harbor sandbox, never pushed) | 5 / 6; planted drift recall 0.75; false drift 0.00; never a pass | met (diagnostic) | PARTIAL — quality evidence self-authored; required-check integration on a real repository OPEN (remote branch-protection write not granted) |
| SUR-05 proposed correspondence on refresh | 4 / 6 (scorer corrected after viewing, both reported); precision 0.571, recall 0.571 | not met | OPEN (quality) |
| SUR-07 ROI analyst (synthetic data) | 4 / 6; declared-question 0.667; no kept claim word or stray number | not met | OPEN (quality); real data / data-owner acceptance OPEN |
| SUR-03 proposal-only coder | not measured (set design: configured thresholds never reach the coder) | — | OPEN |
| SUR-08 review synthesis memo | not run (a board run of several specialist calls per item; worst case above the remaining allowance for six items) | — | OPEN |
| MON-ROI replay on extracted knowledge | defect found and fixed (scope marker read as a field; route values needing an approver not read as approvals): reports "unsupported" honestly now | — | PARTIAL (engine); real data OPEN |

Deploy procedure: [`../runbooks/FINAL_DEPLOY_ROLLBACK.md`](../runbooks/FINAL_DEPLOY_ROLLBACK.md) (nothing deployed; target platform is the owner's decision).
