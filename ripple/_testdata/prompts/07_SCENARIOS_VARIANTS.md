# Stage 07 — Controlled scenarios and unseen-source variants

Operator inputs: system instruction, frozen spec, accepted contracts, generated builders, actual source files, private reviewed oracle and executed integrity reports if available. DEFAULT_MODE=demo. Request MODE=eval_pack explicitly for the larger corpus.

---

Create private deterministic scenario/variant builders around the delivered reference system. Do not implement Ripple's versioning, knowledge, approval or rollback engine; fixtures only materialize source states and expected boundaries for those product features to handle.

Demo mode outputs:

1. `evaluator_private/scenarios.json`: S0–S5 exact starting source state, human request text, known/unknown authority answers, eligible scope, required/recommended/no-op/unsupported actions, unaffected controls and computed-output fields left for execution. Keep human answers private until an authorized operator supplies them in Ripple.
2. Private mutation/materialization script: canonical build→S0 applies C01 manager XLSX cell and C02 finance guide wording with precondition assertions. Assert exactly the two declared changes; G01/G02 are inherent gaps, not silent additional mutations. No C/G labels in exported source.
3. S1 reference target: 500k for standard/emergency with shared config reading; Excel already500k no-op; preserve freight500k, finance priority and separate unresolved finance-guide conflict unless S5 approved. S2 from S1 at750k; S4 from S1 at Excel650k. Private reference target snapshots are never the input to Ripple's proposal generator while it is being evaluated.
4. A **config-only partial S1** fixture: standard changes but emergency remains literal1m, exposing new inconsistency. Label outside input only. Counts computed by running actual handlers and independent expected tables.
5. Restore target bytes S0 plus expected Ripple behavior description: restoring source does not reset human authority. Generator only supplies target manifest/history comparison; Ripple must create the forward revision and retain its own decisions.
6. Standalone adversarial copies: prompt injection in SOP/cell/comment, missing policy section, deleted workbook sheet, wrong operator, stale date, same-number unrelated source. Never put these accidental defects into the main judge bundle.

Eval-pack mode (after demo passes):

- Generate at least 6 development and 6 held-out bundles per plan AI0. Maintain same supported domain and execution interfaces; vary narrative organization, identifiers/header names, document-only rules, exception scopes, relationships and negative controls. Some bundles may have fewer files and static-only simulation, explicitly declared privately.
- Use distinct authoring templates/scenario structures between dev and heldout; numeric substitution alone is insufficient. Rotate rule vocabulary and mixed evidence density without adding shared canonical IDs. Preserve app function interface or ship a privately registered evaluator adapter, not an undisclosed hint to Ripple.
- Meet plan minimum label counts: >=60 heldout rule representations, >=80 labelled relation pairs including negatives, >=24 conflict and >=24 non-conflict cases, >=24 GAP cases with >=12 insufficient-evidence cases, >=12 change scenarios. These are **computed checks**, not claims to put in a generated report before counting.
- Private oracle spec for each variant is frozen before emitting application artifacts. Expected code/data fields must be generated from that variant spec using the independent oracle path. No importing actual app to create expected answers.
- Export held-out inputs to a separate access-controlled location for the evaluator; do not add holdout answers to the coding agent's working context. If already reviewed/tuned on, mark as development and replace holdout.

Private manifests must record: variant/split/seed/version, actual source hashes, semantic digests, mutation ID+exact pre/post evidence, relationship gold labels+reason, expected decision source, generator/model provenance and review status. ID labels/manifest files never copied to input.

Deliver scripts with explicit output destinations/no destructive parent writes, smoke checks for all scenarios, split/leakage verification and commands to compute result counts. Do not add unsupported language adapters or claim six renamed clones prove enterprise generalization.
