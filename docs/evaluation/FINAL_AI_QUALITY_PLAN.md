# Final AI quality plan — layered measurement, pins and regression

Status: plan and pins; **no layer has gate evidence on this build**. Every row below is measured with synthetic stubs that prove the pipeline and the server guards, not model quality. On 2026-10-17 a bounded live **pilot** (owner mandate, 2.00 USD cap, one representative case per role on Gemini 3.5 Flash-Lite, one paired case on Gemini 3.8 Flash; recorded in the development evidence, not shipped) showed every role runs end to end with a real model and found three harness defects that are fixed; it measures no rate and closes no gate. A quality run still needs an owner-approved window (sealed sets, caps, human acceptance). The acceptance criteria of the task cards and of QRY-02b are unchanged by this document; a layer that does not meet its criterion stays REWORK or pending, it is never rebranded.

## 1. Layers, inputs, metrics, baselines

| Layer | Role (job kind) | Fixed input set | Metric (units) | Dev / held-out | Baseline to beat | Status |
|---|---|---|---|---|---|---|
| Extraction | `extraction` | 5 Harbor sources (sha-pinned in `tests/fixtures/final_knowledge_loop/sources.json`) + 1 unseen bundle (to be sealed) | rule precision / recall by semantic id; actor, condition, exception field accuracy; bogus-quote rate (must be 0); truncated rate | dev = fixtures; held-out = sealed bundle | legacy regex candidates (D0) | stub only; held-out 2026-10-02 on an older build did not meet target |
| Candidate generation (semantic expand) | `retrieval.semantic` expand | Q-set 12 + validation 8 + >= 10 unseen | required rules in candidates before ranking | as QRY-02 | lexicon only | provider not live; synthetic replay |
| Ranking / context coverage | `retrieval.semantic` rank + lexical | same | required in top 4; forbidden absent; counter-evidence present | same | tf-cosine | QRY-02 measured limits: dev 10/12, validation 4/8 (lexical) |
| Grounded answer / abstain | `knowledge.qa` | Q-set + 4 no-evidence + unseen | claim grounding 100% after validation; unsupported claims 0; false abstention <= 0.20; conflicts disclosed 100%; omission (rule in pack that should be cited and is not) | sealed | server-only = ABSTAINED | stub only |
| Intent parsing | `change.understand` | C-set 7 + 5 unseen paraphrases | operation kind / rule / value exact; dropped sub-request 0 (validator); invented value 0; remap-to-decided-version asked, never silent | sealed | none | stub only |
| Dependency proposals | `change.impact_deps` | 3 change scenarios | precision of PROPOSED vs human label; recall of fixture `depends_on` | sealed | graph only | stub only |
| Draft guidance | `change.draft_guidance` | 3 scenarios | hold false-positive rate; instruction usefulness (human 1-5) | human | server instruction only | stub only |
| Gap suggestions (KNO-06) | `knowledge.gap_proposals` | the Harbor knowledge version + 2 unseen rule packs with planted missing exceptions / semantic conflicts | precision of suggested gaps vs human label; recall of planted gaps; repeated-question rate (must be 0 after the server filter); fabricated reference rate (must be 0) | sealed | structural analysis only | stub only |
| Free-text answer reading (KNO-07) | `gap.answer_interpret` | 20 written answers (actor / condition / conflict / exception / leave open) + 10 unseen paraphrases incl. negation and two-part answers | structured answer exact; uncovered-part detection recall (a dropped part must become NEEDS_CLARIFICATION); invented value 0; false UNSUPPORTED <= 0.2 | sealed | structured controls only | stub only |
| Documents | `document.generate` | deterministic | bytes golden (DOC-01) | - | - | deterministic, no model |
| End to end S1, S4, S6, S7 | all | fixtures | 0 fabricated evidence, 0 unauthorized disclosure, 0 silent drop, 0 false COMPLETE | live once | stub run | stub run only |

Omission is measured for every role, not only validator-caught errors: for extraction and QA it is the primary risk because no server validator can see what a model left out.

## 2. Evidence pins (role -> frozen prompt/schema -> evidence)

`tests/contract/test_final_prompt_pins.py` reads this table and fails when a pinned file changed without this table being updated, so a prompt, schema or lexicon change always invalidates the quality evidence that was recorded for it. Hashes are sha256 of the file bytes with CRLF normalised to LF. "Evidence" names the live run that measured the role under that pin, or `none (stub only)`.

| role | path | sha256 | evidence |
|---|---|---|---|
| knowledge.qa | config/knowledge_qa.json | 4c1c26248eb65c574360702e5cf8112e0a5c41fd3db41602dd76b28b5e98f01d | none for this pin (fact_defaults added 2026-10-17 after the pilot); pilot FNL-LIVE-20261017 ran on the previous pin b7503842… (one case each, self-check, not a quality gate; recorded in the development evidence, not shipped) — gate evidence none |
| change.understand | config/change_understand.json | c483c850fa0badbe5031fa98f2baf2d010de90b1c9013c52c043f063ebba2d16 | pilot FNL-LIVE-20261017 (one case each, self-check, not a quality gate; recorded in the development evidence, not shipped) — gate evidence none |
| change.impact_deps | config/change_impact_deps.json | 764ef642fbe2d525a8cbb341fff5e4eea28803ce5db19358956d4300a73f5fa8 | pilot FNL-LIVE-20261017 (one case each, self-check, not a quality gate; recorded in the development evidence, not shipped) — gate evidence none |
| change.draft_guidance | config/change_draft_guidance.json | e444afdd145cbf2c20b84e4a16897b832600e4afaf101b89d9564ed829e720d6 | pilot FNL-LIVE-20261017 (one case each, self-check, not a quality gate; recorded in the development evidence, not shipped) — gate evidence none |
| knowledge.gap_proposals | config/knowledge_gap_proposals.json | fde421acd3427668e420d39ecba463ca8844f2615dea4b2f1e33a990b5f4ba42 | pilot FNL-LIVE-20261017 (one case each, self-check, not a quality gate; recorded in the development evidence, not shipped) — gate evidence none |
| gap.answer_interpret | config/gap_answer_interpret.json | e17f1830de0e91909e8140b51387fc831e947609fe198b122c70cd968cc08eb5 | this pin (config changed in `70b450d`, uncertainty classes): only the v5 pilot EV5-FLOW-20261020-01 ran on it (self-authored, n = 6 diagnostic, one condition reading refused by the grammar), a pilot that is not a quality gate; the previous pins e98448e4… (none) and 5ee584ae… (pilot, S2, gate set v2: self-built, not independent) are history for their own runs; quality on this pin OPEN — gate evidence none |
| request.assistant | config/request_assistant.json | dd196b9bbd218bc643b4ebf668eb254e98eb7fb6c7d56281e72968cd7dda7cac | none for this pin (config version 9, 2026-10-21: version 8 plus the recorded stage reason / reason gap of context/3, a status only as the facts state it, and who acts left to the server section; checks/12, checks/13 since 2026-10-21; schema unchanged; live regression RQA-V9-20261021-01 on 12 already-seen self-authored questions: supported 25 / 25, server correct, shown useful 9 / 10, one borderline wrong shown sentence (critical row not met), that class refused since checks/13); earlier pins: a6c81124… (v8) by RQA-V8-20261021-01 (Flash-Lite, 12 self-authored questions: usefulness 7 / 10 and one wrong shown sentence, not met) and the stopped paired comparison RQA-V8F-20261021-01 (gemini-3.8-flash, 2 calls); before that (config version 8, 2026-10-21: version 7 plus the per-request list of facts that record a reason, the stage and waits as facts, and no authority in limitations; schema unchanged; answer/5, checks/10 for the check RQA-V8-20261021-01 (12 questions, self-authored: usefulness and critical-wrong rows not met), checks/11 since); earlier pins: e5a9be6e… (v7) by the decline check RQA-DECLINE-20261021-01 (8 questions, self-authored; checks/8), 00c9b7b2… (v6) by E1 (self-authored, non-independent; all frozen criteria met under the corrected scorer v6), 62624d50… (v5, typed kinds, never collected), 838e1ed9… (v4) by Q4, 44263c7d… (v3) by Q2 and Q3, 3fb6391e… by R1 / Q1; all self-authored, non-independent |
| document.wording | config/document_wording.json | be79a42147e78d430d373c250d4fc4b42c216cc8cb33457a6bea03bb8ae5734b | none (extension AIX-D1, built 2026-10-17 with stub providers only; audience variants, server-checked; version 2 of 2026-10-18 adds claim rows and clause checks, LHA-D1) |
| knowledge.mining | config/knowledge_mining.json | b7ea40df856ccef312eb4b00e06d864582ea74b815c530d136b8fa867005c657 | none (extension AIX-K1, built 2026-10-17 with stub providers only; version 2 of 2026-10-18 adds semantic candidate pairs and the compatibility rubric, LHA-K1, link checks SUR-01 and the unbound-value triage stage SUR-02; typed hypotheses with server-verified cases) |
| gap.interview | config/gap_interview.json | dca52c4cf668059f39d790fcbc1215a9d21f8e47a09196adca0682d60601ad94 | none (extension AIX-K2 model questioner, built 2026-10-17 with stub providers only; server-grounded, deterministic fallback) |
| change.risk_memo | config/change_risk_memo.json | c6ff51bbf6f3a78ac8b3ef0df490979c9bd83d4150e495e1b416baf9f5edb804 | none (extension SUR-06, built 2026-10-19 with stub providers only; advisory memo, server-checked refs and figures, algorithm severity floor kept) |
| ma.synthesis_memo | config/ma_synthesis_memo.json | 8547b25004d8053c75fc17366e399e3a7e6cc037a21440e534e69721236f1280 | none (extension SUR-08, built 2026-10-19 with stub providers only; synthesis memo, every minority / unresolved finding listed by the server) |
| ci.semantic_drift | config/ci_semantic_drift.json | 8d88201ad5a71bf30e35c1c8e37dd74a383a0573b587adbc3068a2512b073d58 | none (extension SUR-04, built 2026-10-19 with stub providers only; advisory findings with server-checked witness lines next to the unchanged exact MON-CI check; never a PASS) |
| source.correspondence | config/source_correspondence.json | f81f256cfc8610bec8bc58ed68f0a747fe4814df350e4ab58e483e1e636ed359 | none (extension SUR-05, built 2026-10-19 with stub providers only; proposed old/new correspondence of a refresh, verbatim quotes server-checked, never a rename proof) |
| code.diff_proposal | config/code_diff_proposal.json | af6e392a9dfc3bdfa9684207e376009526e93a8f865ed528dafb591f42aa9b1e | none (extension SUR-03 proposal-only, built 2026-10-19 with stub providers only; edits checked and diff built in memory by the server, never applied or run) |
| roi.analyst | config/roi_analyst.json | 5d3a7c5b0e8f73027d6c693da7914c2e69e4301cecd3469898d0b44c407a7ed0 | none (extension SUR-07, built 2026-10-19 with stub providers only; analyst memo over a SYNTHETIC replay, every number from the replay engine, real data refused) |
| change.draft | config/change_draft.json | 872b920ba52010fe5956ab16bcc2c558e73ca5d200e8481f705bb30b63539db7 | Pitch live runs (see docs/evaluation/HELD_OUT_EVALUATION.md, older build) |
| extraction | infra/worker/extraction.py | 3543c091beddd85c88c997ee807e8df4e30499ec715603afe216ba64d222d16b | held-out 2026-10-02 on build f953ebe did not meet target; pilot FNL-LIVE-20261017-04 (one artifact, Lite, self-check, not a quality gate) — gate evidence none on this build |
| retrieval.lexicon | config/business_lexicon.json | cd6214ac52a5ee53b4ab7bf989aaf975144ab10155f31e7b5e9bd23acbda9475 | QRY-02 measured limits (lexical, dev 10/12, validation 4/8) |
| retrieval.semantic | infra/adapters/semantic_vertex.py | cbc10b734338fb2380f5178ac337e128e4298c52366f30cd5024ddf89e4e5989 | pilot FNL-LIVE-20261017-13/-15 (expand, then all three layers applied live on Lite; self-check, not a quality gate) — gate evidence none |

## 3. Regression when a prompt, model or schema changes

- A pinned file changes -> the pin test fails -> the owner of the change updates the row and sets its evidence to `none` until a new run exists. Stored results are already bound to their configuration at run time (job ids include the prompt/schema sha and the pinned model), so a changed configuration never replays an old answer.
- Replay fixtures (`ReplayProvider`, synthetic today) are the zero-cost regression once a live run has been recorded: the live responses are recorded as fixtures and the tests run on them afterwards.
- Model/thinking changes are configuration (`role_model_ids`, `role_thinking_levels`): they are part of the job-id fingerprint and must be stated in the evidence row of a run.

## 4. Sealing and ownership

Unseen sets are written by the owner or a second person, their hash committed before any live run, and never used for tuning. The runtime does not read fixtures (source scan of QRY-02a, to be extended). A result on the development set after tuning is a development result, never a held-out claim.

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
