# Final AI quality plan — layered measurement, pins and regression

Status (updated 2026-10-25): every Final AI role has a live rated run on its current pin or code, recorded in the pin table below and in the development evidence (`LLM_EV_V3_PROTOCOL.md`), on self- or agent-authored sets (separate builder sessions where stated): **none of these sets is independent or person-written**. The owner decided on 2026-10-18 (`docs/product/AI_SYSTEM_PRINCIPLES.md` §5) that an independent reviewer is not a mandatory dependency: the implementing agent may build, run and score evaluations and states their independence level honestly; human acceptance still needs a real person, and thresholds are not changed to fit results. Independent evaluation (REL-03) stays an OPEN full-release item. The 2026-10-17 pilot and the stub measurements below are history. Thresholds and the acceptance criteria of the task cards and of QRY-02b are unchanged

## 1. Layers, inputs, metrics, baselines

| Layer | Role (job kind) | Fixed input set | Metric (units) | Dev / held-out | Baseline to beat | Status |
|---|---|---|---|---|---|---|
| Extraction | `extraction` | 5 Harbor sources (sha-pinned in `tests/fixtures/final_knowledge_loop/sources.json`) + 1 unseen bundle (to be sealed) | rule precision / recall by semantic id; actor, condition, exception field accuracy; bogus-quote rate (must be 0); truncated rate | dev = fixtures; held-out = sealed bundle | legacy regex candidates (D0) | live: CORE-V5-S3 met (agent-authored); held-out 2026-10-02 on an older build did not meet target |
| Candidate generation (semantic expand) | `retrieval.semantic` expand | Q-set 12 + validation 8 + >= 10 unseen | required rules in candidates before ranking | as QRY-02 | lexicon only | live, current code (2026-10-26, QRY02B-V6, new agent-authored set, not independent): pack 0.952, recall 0.962; lexical baseline 0.476 (history: QRY02B-V5 pack 0.905 on the earlier selection; F-1 closed 2026-10-25) |
| Ranking / context coverage | `retrieval.semantic` rank + lexical | same | required in top 4; forbidden absent; counter-evidence present | same | tf-cosine | live, current code (2026-10-26, QRY02B-V6): window 0.952, recall 0.962, counter dropped 0; lexical 0.476 / 0.429; history: QRY02B-V5 window 0.905 (earlier selection), QRY-02 dev 10/12, validation 4/8 (lexical) |
| Grounded answer / abstain | `knowledge.qa` | Q-set + 4 no-evidence + unseen | claim grounding 100% after validation; unsupported claims 0; false abstention <= 0.20; conflicts disclosed 100%; omission (rule in pack that should be cited and is not) | sealed | server-only = ABSTAINED | live: QRY02B-V5 met (n 24); agent set v2 18/21 (agent-authored); F-1 false abstention open |
| Intent parsing | `change.understand` | C-set 7 + 5 unseen paraphrases | operation kind / rule / value exact; dropped sub-request 0 (validator); invented value 0; remap-to-decided-version asked, never silent | sealed | none | live: UNDERSTAND-V14 met (n 22, agent-authored) |
| Dependency proposals | `change.impact_deps` | 3 change scenarios | precision of PROPOSED vs human label; recall of fixture `depends_on` | sealed | graph only | live: CORE-V8-S3 met (agent-authored) |
| Draft guidance | `change.draft_guidance` | 3 scenarios | hold false-positive rate; instruction usefulness (human 1-5) | human | server instruction only | live: hold row met (C3 + A74 regression); owner accepted v3 within its package; no usefulness score |
| Gap suggestions (KNO-06) | `knowledge.gap_proposals` | the Harbor knowledge version + 2 unseen rule packs with planted missing exceptions / semantic conflicts | precision of suggested gaps vs human label; recall of planted gaps; repeated-question rate (must be 0 after the server filter); fabricated reference rate (must be 0) | sealed | structural analysis only | live: PROPOSALS-V9 met as scored; missing-exception group 0/5 |
| Free-text answer reading (KNO-07) | `gap.answer_interpret` | 20 written answers (actor / condition / conflict / exception / leave open) + 10 unseen paraphrases incl. negation and two-part answers | structured answer exact; uncovered-part detection recall (a dropped part must become NEEDS_CLARIFICATION); invented value 0; false UNSUPPORTED <= 0.2 | sealed | structured controls only | live: CORE-V5-S1 met (n 13, agent-authored) |
| Documents | `document.generate` | deterministic | bytes golden (DOC-01) | - | - | deterministic, no model |
| End to end S1, S4, S6, S7 | all | fixtures | 0 fabricated evidence, 0 unauthorized disclosure, 0 silent drop, 0 false COMPLETE | live once | stub run | live: CORE-V5-S3 e2e rows met; journey V9 (2026-10-22, older build) |

Omission is measured for every role, not only validator-caught errors: for extraction and QA it is the primary risk because no server validator can see what a model left out.

## 2. Evidence pins (role -> frozen prompt/schema -> evidence)

`tests/contract/test_final_prompt_pins.py` reads this table and fails when a pinned file changed without this table being updated, so a prompt, schema or lexicon change always invalidates the quality evidence that was recorded for it. Hashes are sha256 of the file bytes with CRLF normalised to LF. "Evidence" names the live run that measured the role under that pin, or `none (stub only)`.

| role | path | sha256 | evidence |
|---|---|---|---|
| knowledge.qa | config/knowledge_qa.json | 43c4fb88704946e1500abe02530ebf9fe1e355be8c77547d2f58ebe61a1f40bf | AIQ-FIN-QA-20261025 on this pin (product payload cf47df43, semantic selection policy v2; NEW sealed set of 12 questions on three packs above the semantic cap, agent-authored by a separate builder session, oracle-reviewed by a second agent session, not independent): 12 / 12 — status ok 1.0, must-cite 1.0, wrong rule cited 0, false abstention 0, missed abstention 0 (original scorer and scorer qa/2 alike); every kept claim the model's own (17 kept, server added 0, refused 0). History: AIQ-ACC-QA-20261025 (payload 90c9e411) wrong-rule row did not meet under the original scorer (11 / 12 under qa/2); QRY02B-V5-20261024 23 / 24 |
| change.understand | config/change_understand.json | afc5e8b05a086802501368124bb6fac793ff42a0d3b1b2819dbfb412fee6b0c7 | UNDERSTAND-V14-20261025 on this pin (version 8; n 22, unseen, agent-authored by a separate builder session, not independent; owner accepted the set as a gate set at that level): status ok 0.909, exact 0.889, dropped 0, invented 0 — thresholds met. History: v12 / v13 on this pin not met (oracle correction recorded apart); version 7 pin bfb27b3f… not met (UNDERSTAND-V12 0.682 / 0.611) |
| change.impact_deps | config/change_impact_deps.json | 425708fbe3d074b66f20900c6e8eab27e158c8700fcbc8e7ee7713eab119ec58 | CORE-V8-S3-20261024 on this pin (version 2; n 12, agent-authored by a separate builder session, not independent): must-include recall 1.0, forbidden 0 — thresholds met; proposals are leads only (holds do not rest on them since A74) |
| change.draft_guidance | config/change_draft_guidance.json | 902a21577b9fad7388e69e1b93db5b8d4896b58febb20efb71c2510d4f5e7943 | AIQ-FIN-GD3-20261025 on this pin (version 5, payload cf47df43; set aiq_guidance_acc of 10 changes, agent-authored by a separate builder session, oracle-reviewed by a second agent session, not independent, read after the R1 run so a regression set here): hold false positive 0 / 10, expected holds 5 / 5 — the hold row met; related rules answered by their class (same topic / separate obligation: no change, no alignment suggested; held: pointed to the hold). Human usefulness (mean >= 3.5) waits for the owner's rating sheet; the owner's acceptance of version 3 does not cover version 5 prose. Version 4 pin aef59594…: AIQ-ACC-GD-20261025 hold false positive 0, one hold missed |
| knowledge.gap_proposals | config/knowledge_gap_proposals.json | 7c4ce04b62c6d77c78e99765fb64de229acf7b333b96b5ae4669d286a5862269 | AIQ-ACC-GP-20261025 on this pin (flash-lite LOW; n 10, agent-authored by a separate builder session, oracle-reviewed by a second agent session, not independent): planted recall 5 / 7 = 0.714, precision 0.714 — thresholds met; missing exceptions 3 / 5 at the model layer and 3 / 5 retained (in pack 3 / 3, stated by no source 0 / 2) — the round's prospective target met; two false questions on controls. A gemini-3.8-flash comparison on the seven development items kept 3 / 5 vs 1 / 5 but is not adopted (the job reservation would exceed the production per-job cap). Earlier: PROPOSALS-V9-S3 0.545 / 0.857 |
| gap.answer_interpret | config/gap_answer_interpret.json | e17f1830de0e91909e8140b51387fc831e947609fe198b122c70cd968cc08eb5 | CORE-V5-S1-20261024 on this pin (n 13, agent-authored by a separate builder session, not independent): every threshold met; earlier pilots EV5-FLOW-20261020-01 and pin 5ee584ae… are history |
| request.assistant | config/request_assistant.json | 292313e8271536e0401cc7ee731b3886265adba0f75778a6dd1ecc6863caefda | RQA-V12-20261023-01 on this pin (config version 10, checks/14; 12 frozen questions, self-authored, not independent): every RQA-05 row met; later code hardening (prompt data cannot expand placeholders) covered by unit tests |
| document.wording | config/document_wording.json | 3b6eda5b16041c231169eb39aba0842d74662a4e1b349f9b28a8483a097141c7 | VARIANT-V5-20261024 on this pin (version 3; n 6, agent-authored by a separate builder session, not independent): generated 1.0, required terms 1.0 — thresholds met; EV3-VARIANT / VARIANT-V4 belong to earlier pins or code |
| knowledge.mining | config/knowledge_mining.json | 7fb826836c6314d0aa253145125d22b934462404b4b5b5d3eabbb01d9451ba8d | MINING-V5-20261024 on this pin (config version 5; n 7, agent-authored by a separate builder session, not independent): planted recall 0.83, false conflict 0 — thresholds met; EV3-MINING-20261023-02 and MINING-V4 belong to versions 3 / 4 |
| gap.interview | config/gap_interview.json | dca52c4cf668059f39d790fcbc1215a9d21f8e47a09196adca0682d60601ad94 | EV4-INTERVIEW-20261023-01 on this pin (n 10, agent-authored by a separate builder session, not independent): 9 / 10, grounded 1.0, addresses 1.0, unsafe 0 — thresholds met |
| change.risk_memo | config/change_risk_memo.json | c6ff51bbf6f3a78ac8b3ef0df490979c9bd83d4150e495e1b416baf9f5edb804 | SUR06-V2-20261023 on this pin (n 6, self-authored, not independent; two items re-executed after a reference repair, disclosed): planted 0.833, floor 0, refused refs 0, unoffered 0 — thresholds met |
| ma.synthesis_memo | config/ma_synthesis_memo.json | 8547b25004d8053c75fc17366e399e3a7e6cc037a21440e534e69721236f1280 | SUR08-V1-20261023 + A9b / A9c on this pin (n 6, self-authored, not independent): planted 4 / 6, zero-tolerance rows 0 — thresholds met |
| ci.semantic_drift | config/ci_semantic_drift.json | 8d88201ad5a71bf30e35c1c8e37dd74a383a0573b587adbc3068a2512b073d58 | SUR04-V1-20261022 on this pin (n 6 local sandbox diffs, self-authored, not independent): recall 0.75, false drift 0, never a pass — thresholds met; the required-check integration on a real repository is not done (branch protection not granted) |
| source.correspondence | config/source_correspondence.json | 62d8fc710b8b2f7c4f7c9f9cd86b0fdfa8f3078c635c2ea972025f58e5ea45a0 | SUR05-V1-A11-20261023 on this pin (n 6, self-authored, not independent): precision 1.00, recall 0.714, proof claimed 0 — thresholds met; SUR05-V1 on the previous pin f81f256c… did not meet them |
| code.diff_proposal | config/code_diff_proposal.json | 0f1c0ba7d8d92528e1e8110192aec22d0ab9df411f4aaa1bf5837be3a5ef9f80 | SUR03-V14C-20261025 on this pin (version 2; 8 reached through the whole caller, self-authored, not independent; owner accepted the set at that level): 7 / 8, static 0.875, planted 0.875, outside allowlist 0, applied or run 0 — thresholds met; proposals are never applied or run |
| roi.analyst | config/roi_analyst.json | b338b4a30c298e01926a3dda4104772c4d0a277a5aa283a1edbb88d8b4dcf004 | SUR07-V4-20261024 on this pin (version 2; n 6, synthetic data, self-authored, not independent): declared question 1.0, claim 0, stray number 0 — thresholds met on synthetic data only; no ROI claim on real data |
| change.draft | config/change_draft.json | 872b920ba52010fe5956ab16bcc2c558e73ca5d200e8481f705bb30b63539db7 | Pitch live runs (see docs/evaluation/HELD_OUT_EVALUATION.md, older build) |
| extraction | infra/worker/extraction.py | 9f96c4404ca2a53516a785d36a20427ca6924a02413d43d4e14185ff936e79b5 | CORE-V5-S3-20261024 on this pin (run after the AH-04 change; n 23 expected rules, agent-authored by a separate builder session, not independent): recall 0.957, precision 1.0, zero tolerance 0 — thresholds met; journey V9 J1 (2026-10-22, older pin 7f07f816…, self-check) |
| retrieval.lexicon | config/business_lexicon.json | cd6214ac52a5ee53b4ab7bf989aaf975144ab10155f31e7b5e9bd23acbda9475 | QRY-02 measured limits (lexical, dev 10/12, validation 4/8) |
| retrieval.semantic | infra/adapters/semantic_vertex.py | 3a94b81f06a9310db65637c6dc50c33fb0f3042c3d9da7feb4d400eeed46eebc | PC05-QRY02B-V6-20261026 on this pin (2026-10-26, NEW set `qry02b_v6` by a separate builder session, agent-authored, not independent; 24 items, 7 paraphrase-only, 5 two-hop): retrieval pack pass 0.952, window 0.952, required recall 0.962, counter-evidence dropped 0; QA rows met; lexical baseline pack 0.476 — and before that AIQ-FIN-QA-20261025 on this pin (selection policy v2 in semantic_retrieval.py; agent-authored by a separate builder session, oracle-reviewed by a second agent session, not independent): expand, rank and judge applied on 12 / 12 questions (36 / 36 layers, no output-limit cut, no token or call cap; 2 malformed search terms dropped, ids kept); rank / judge 13-20 candidates, counter-evidence and expansion finds never omitted; the judge's verdict was not applied as a veto on any pack because each pack also held items or source passages it did not receive (stated per answer). History: AIQ-ACC-QA-20261025 rank cut at the output limit 10 / 12, judge never ran; QRY02B-V5-20261024 pack 0.905 / window 0.905 / recall 1.0 |

## 3. Regression when a prompt, model or schema changes

- A pinned file changes -> the pin test fails -> the owner of the change updates the row and sets its evidence to `none` until a new run exists. Stored results are already bound to their configuration at run time (job ids include the prompt/schema sha and the pinned model), so a changed configuration never replays an old answer.
- Replay fixtures (`ReplayProvider`, synthetic today) are the zero-cost regression once a live run has been recorded: the live responses are recorded as fixtures and the tests run on them afterwards.
- Model/thinking changes are configuration (`role_model_ids`, `role_thinking_levels`): they are part of the job-id fingerprint and must be stated in the evidence row of a run.

## 4. Sealing and ownership

Unseen sets are written by the owner or a second person, their hash committed before any live run, and never used for tuning (2026-10-17 rule; since the owner decision of 2026-10-18, AI_SYSTEM_PRINCIPLES §5, agent-authored sets from a separate builder session are allowed, labelled as such and never called independent). The runtime does not read fixtures (source scan of QRY-02a, to be extended). A result on the development set after tuning is a development result, never a held-out claim.

## 5. Smallest live pilot (the one-case-per-role pilot ran on 2026-10-17; the set-sized pilot below is still PROPOSED - NOT APPROVED)

Before any wide run: a pilot that is just large enough to catch gross failures (schema failures, truncation, fabricated evidence, unauthorized disclosure) per role, paired across the candidate models of the routing proposal (`config/ai_closed_loop/final_budgets.proposed_routing.example.yaml`):

| role | cases | calls per case (max) | semantic sub-calls | retries (max) | input cap | output + thinking cap |
|---|---|---|---|---|---|---|
| change.understand | 7 C-set + 5 unseen | 1 | 0 | 1 | 24000 tokens | 6144 (generation cap) under 12288 reserved |
| knowledge.qa | 12 dev + 10 unseen | 1 | up to ceiling-1 (1 with the proposed config) | 1 | 24000 | 6144 under 12288 |
| extraction | 5 sources + 1 bundle (about 8 artifacts) | chunks per artifact | 0 | 1 | 24000 | 6144 under 12288 |
| retrieval.semantic | 20 questions, expand layer only | 1 | n/a | 1 | 12000 chars offered | 512 |

Worst-case reservation per attempt = `max_output_tokens` (reservation ceiling, output + thinking) x planned calls x attempts, priced at the configured worst model; the hard cap is `total_cap_usd` of the live config and must be set by the owner together with prices verified on the Vertex global endpoint on the day of the run (source and date recorded in the config). Stop rules: any fabricated evidence, unauthorized disclosure, silent drop, or `REASON_TRUNCATED` rate above 10% in a role stops that role. Metrics and thresholds are those of section 1, fixed before the run. A pilot does not replace the quality gate of QRY-02b or REL-03; it only decides whether a wider run is worth its cost.

## 6. First numbers on an agent-sealed set (2026-10-17) — not gate evidence
An independent reviewer session (an agent) wrote two sealed unseen sets (`tests/fixtures/final_knowledge_loop/sealed/`, hashes committed before the run, never read by the agent that
changed the code, never used for tuning); a mechanical scorer ran them live on Gemini 3.5 Flash-Lite: questions 10/14 then 8/14 after a server fix (status right for 12/14; 4 missed a
required citation; 2 abstentions failed a wording check; run-to-run variance of 3 items), change requests 6/8 (one wrong role slug, one three-part request not understood). Section 4
requires a set written by the owner or a second person: these sets are pending that review, so no row of section 1 moves and no threshold is declared met or failed. Details and costs
are in the development evidence (REL-03), not shipped.

## 7. Hackathon solo profile (owner decision 2026-10-27): agent acceptance evaluation in place of an independent reviewer

The owner works on the hackathon alone and authorised agent evaluation in place of the human / independent rows of section 4, with every threshold unchanged.
**This is agent evaluation, not independent validation and not a human rating.**

**How the acceptance sets were run:**
- Two acceptance sets were each built by a separate session: one that read only the protocol, contracts and scorer, never the product's prompts, source or earlier sets.
- Each set uses a new fictional organisation.
- Hashes were committed before the run.
- The operator did not open the answers before the outputs were frozen.
- Scoring was mechanical, using the frozen thresholds of the evaluation runner (kept in the development records).
- Pins were the production limits (Flash-Lite; per-job cap 0.16; at most 4 calls; no retry).

| Role | Result on the acceptance set | Note |
|---|---|---|
| knowledge.qa | n 17: status 1.0, required citations 0.94, wrong rule as applying 0 | the "does not apply" reading of the established scorer; the original check counted two verified not-applying citations |
| change.understand | n 17 (second set, after three fixes found by the first): exact 0.89, status 0.94, dropped 0, invented 0 | the first set (n 12) did not meet; the defects were fixed: a reversal proposed, a person described but not named, a source-label year asked about |
| gap.answer_interpret | n 12: status 0.92, exact 1.0, uncovered parts recall 1.0, invented 0 | — |
| change.impact_deps | n 13: required links 0.88, forbidden 0 | second set |
| change.draft_guidance | n 10: hold false positive 0; usefulness rated by a separate evaluator session 4.36 / 5 (n 14) | the v5 guidance sheet rated the same way: 4.04 / 5 (n 27) |
| knowledge.gap_proposals | n 6: planted recall 0.67, precision 0.80 | — |
| extraction | n 23: rule recall 1.0, precision 1.0, no invented quote / unit / value | after a scorer correction (the actor written as a role id); the original scorer gave 0.48 |
| e2e change chain | n 6: false complete 0, silent drop 0, knowledge written by the model 0 | — |

**Not re-run:** the extension roles. They keep their earlier agent-authored results.
**Limits:**
- Small sets.
- The same model family builds, runs and rates.
- A decided conflict cannot be planted in these sets.
