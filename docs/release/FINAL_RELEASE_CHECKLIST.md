# Final build: release checklist (what is done, what the owner decides)

| Step | State on this build | Where |
|---|---|---|
| Final knowledge loop implemented and verified locally with synthetic model stubs through the real routes, worker, database and browser | done (self-check) | [`FINAL_CLAIMS_EVIDENCE_LIMITS.md`](FINAL_CLAIMS_EVIDENCE_LIMITS.md) rows 1-13 |
| Contracts, quality plan with prompt pins, recovery runbook | done | [`../contracts/FINAL_KNOWLEDGE_LOOP.md`](../contracts/FINAL_KNOWLEDGE_LOOP.md), [`../evaluation/FINAL_AI_QUALITY_PLAN.md`](../evaluation/FINAL_AI_QUALITY_PLAN.md), [`../runbooks/FINAL_RECOVERY.md`](../runbooks/FINAL_RECOVERY.md) |
| README and judge documents rewritten for the Final build; broken links fixed | done | [`../../README.md`](../../README.md), [`../README.md`](../README.md) |
| Local rehearsal (simulated models) walks the loop from a fresh database | done (recorded in the development records) | [`../../README.md`](../../README.md) section 9 |
| Clean export of the committed tree: no development files, no unclassified path, publication scan clean, no broken relative link, contract and unit suites pass inside the exported tree | done for the export of this commit (see the manifest of that export) | `SUBMISSION_MANIFEST.json` at the export root |
| Real-model quality of every AI role | **OPEN**: a one-case-per-role live pilot ran on 2026-10-17 (self-check, 28 calls, three harness defects fixed, no rate, no sealed set); the gate needs sealed unseen sets, thresholds and an approved window | quality plan sections 1, 4, 5 |
| Extension packages (Git read, code change, policy decision, audit package, CI drift check, replay engine, multi-agent workflows, interview over one question, audience variants of documents, conflict mining, Firebase hybrid satellite with mock adapters) | done locally with fixtures/mocks/synthetic providers; every real integration, real-model quality and human acceptance **OPEN** | [`FINAL_CLAIMS_EVIDENCE_LIMITS.md`](FINAL_CLAIMS_EVIDENCE_LIMITS.md) rows 15-27, [`../contracts/EXTENSION_FIREBASE_HYBRID.md`](../contracts/EXTENSION_FIREBASE_HYBRID.md), [`../contracts/EXTENSION_MULTI_AGENT.md`](../contracts/EXTENSION_MULTI_AGENT.md), [`../contracts/EXTENSION_GOVERNANCE.md`](../contracts/EXTENSION_GOVERNANCE.md) |
| Human acceptance of documents, code instructions and suggestions | **OPEN**: a person, not an agent | quality plan section 4 |
| Publish, submit, deploy | **owner's decision**; nothing in this repository publishes or deploys | — |

The exported tree is produced by the development tool from a committed state; its manifest records the source commit, every file hash and the checks above. Self-check only; not independent validation.
