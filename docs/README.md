# Documentation map

Read in this order if you want to judge what Ripple is, what was verified and what was not.

| Read | What it settles |
|---|---|
| [`../README.md`](../README.md) | what Ripple is, how to try it, supported scope, limits, setup and tests |
| [`product/AI_SYSTEM_PRINCIPLES.md`](product/AI_SYSTEM_PRINCIPLES.md) | enterprise future-of-work objective; when LLM reasoning is required, deterministic responsibilities, model selection and task acceptance policy; design intent, not delivery evidence |
| [`contracts/FINAL_KNOWLEDGE_LOOP.md`](contracts/FINAL_KNOWLEDGE_LOOP.md) | the Final knowledge loop: shapes, grammar, lifecycle, permissions, limits, retrieval adapter, carry-over, model suggestions and readings |
| [`evaluation/FINAL_AI_QUALITY_PLAN.md`](evaluation/FINAL_AI_QUALITY_PLAN.md) | what AI quality would be measured, the pinned prompts, the one-case live pilot of 2026-10-17 (a self-check, not a gate) and the not-approved set-sized pilot; no pin carries gate evidence on this build |
| [`runbooks/FINAL_RECOVERY.md`](runbooks/FINAL_RECOVERY.md) | operator recovery steps of the Final loop |
| [`runbooks/FINAL_DEPLOY_ROLLBACK.md`](runbooks/FINAL_DEPLOY_ROLLBACK.md) | how a deploy would be built, verified and rolled back (procedure only; nothing deployed) |
| [`runbooks/FINAL_PRODUCTION_WINDOWS.md`](runbooks/FINAL_PRODUCTION_WINDOWS.md) | operating in windows: switch on, check, stop, back up, switch off, switch on again; what keeps billing |
| [`judges/START_HERE.md`](judges/START_HERE.md) | the Phase 1 judge guide (Markdown source of the static pages in `../site/judges/`), kept as history |
| [`release/FINAL_RELEASE_CHECKLIST.md`](release/FINAL_RELEASE_CHECKLIST.md) | the Final build: what is done (self-check), what is OPEN (live quality, human acceptance) and what the owner decides |
| [`release/FINAL_CLAIMS_EVIDENCE_LIMITS.md`](release/FINAL_CLAIMS_EVIDENCE_LIMITS.md) | every Final claim, the tests that back it, its limit; what is **not** claimed |
| [`release/CLAIMS_EVIDENCE_LIMITS.md`](release/CLAIMS_EVIDENCE_LIMITS.md) | Phase 1: every claim, its evidence, its limit and what is **not** claimed; open defects and known limits |
| [`release/SCENARIO_STATUS.md`](release/SCENARIO_STATUS.md) | both scenarios stage by stage: which run backs which stage |
| [`evaluation/HELD_OUT_EVALUATION.md`](evaluation/HELD_OUT_EVALUATION.md) | the held-out AI-quality evaluation (targets not met), with data, method and disclosures |
| [`pitch/README.md`](pitch/README.md) | the first live run (2026-10-05): chain of jobs, costs, screenshots |
| [`release/PACKAGE_PROVENANCE.md`](release/PACKAGE_PROVENANCE.md) | how this repository was produced, what differs from the serving image, test results by snapshot |

Architecture and reference: [`decisions/`](decisions) (architecture decision records), [`contracts/`](contracts) (API, storage, lifecycle and budget contracts), [`domain/`](domain) (glossary, state machines, events, semantics), [`product/`](product) (support matrix, boundaries), [`evaluation/`](evaluation) (metric definitions and thresholds), [`runbooks/`](runbooks) (budget and sandbox configuration templates), [`verification/`](verification) (cloud readiness and two design decisions with evidence). Third-party notices: [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
