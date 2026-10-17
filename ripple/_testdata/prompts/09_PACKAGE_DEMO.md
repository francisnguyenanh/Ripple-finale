# Stage 09 — Package and judge/operator runbook

Operator inputs: frozen spec, accepted contracts, materialized artifact manifest, actual validation/review outputs and enabled Ripple capabilities. If actual outputs missing, generate a provisional runbook with NOT_RUN checkpoints, not a success story.

---

Package the generated system for development, test and judge demonstration. Output private tooling/runbooks under `operator/`, `authoring/`, `reports/`; ordinary standalone app README may be finalized in templates and rebuilt. Never place hidden scenario answers in the public input README.

Deliver:

1. `operator/START_HERE.vi.md`: exact local environment commands, isolation guidance, install/build/test/run CLI/optional web, expected output paths, reset by generating a fresh bundle, artifact inspection and common failure recovery. Commands must match delivered code, not aspirational filenames.
2. Private packaging script: export only `bundles/<bundle>/input/` through an allowlisted manifest. Include supported app/config/Markdown/CSV/JSONL/XLSX/test artifacts. HTML/PDF/DOCX forms may be kept as presentation extras; import only if Ripple adapter capability is verified. Never include `authoring/`, `evaluator_private/`, scenario manifests, model logs, `.venv`, credential files or future target snapshots.
3. `operator/IMPORT_MAPPING.md`: source roles and format eligibility, relative paths, presentation duplicates via rendered_from, exclusions and coverage. Do not create a semantic rule-equivalence map for the runtime. This mapping is private packaging/operator metadata.
4. `operator/DEMO_SCRIPT.vi.md`: 5–7 minute full walkthrough and a 3-minute cut; each step names user action, observable proof, expected failure/unknown and how to recover. No precomputed fake screen output.
5. `reports/DATASET_READINESS.md`: actual package version/hashes/seed/tool versions/test evidence, known planned conflicts/gaps, accidental failures, review status, synthetic-data disclaimer and precise limitations. NOT_RUN remains visible.

Demo sequence:

- Show one invoice, one workbook and code before import so the audience sees actual source artifacts.
- Import S0; show AI-discovered rule/relations with evidence and C01/C02/G01/G02. If Ripple's current implementation cannot do this yet, say capability pending AI3–AI5; do not substitute generator annotations as AI discoveries.
- Human authorized role answers emergency scope question and sets manager threshold to500k. Show knowledge changing but code still1m until approval/apply. Show freight500k as unrelated negative control.
- What-if runs real transactions; derive and display affected counts from actual results. Show config-only change leaves emergency path wrong, then AI-generated contextual patch resolves that dependency.
- Review actual source diffs and execution checks; authorized human approves; source changes; verifier reads materialized files; knowledge rebuilds. Existing finance-guide/SLA issues remain visible if outside change scope. Do not claim globally ALIGNED.
- Optional second change to750k proves current revision simulation; restore prior bytes creates new revision with decisions retained; manual Excel650k refresh opens conflict. Git auto-trigger clearly future.

Provide a mechanical preflight checklist (command/result/evidence) and a post-demo export of actual source revision/knowledge report IDs if supplied by Ripple. Dataset generator must not mint fake Ripple IDs, provider traces, approvals or screenshots.

Cost/readiness note: Vertex costs relate to generating and later analyzing data; offline fixture runtime has no model calls. Do not assert trial balance or pricing unless the operator supplies measured billing information. Do not publish/deploy anything as part of this prompt.
