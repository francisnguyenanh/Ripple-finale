# Stage 03 — Reproducible business data and materialization

Operator inputs: system instruction, frozen spec, accepted contracts/build contract and public app interfaces. Include implementation only for computing **observed baseline** events, never use its output as normative oracle.

---

Generate deterministic Python data/build tools, not hundreds of literal JSON rows. Outputs stay in `authoring/` except materialized public files produced when the operator runs them. Return bounded file batches.

Create:

1. Private structured generation specification and case allocation catalog: exact mandatory cases, random-fill algorithm, IDs/foreign keys, seed/reference date, PO/receipt/invoice mapping, baseline scripted approval actors and reason each edge case exists. Keep catalog outside public input.
2. Seeded data builder for 20 suppliers, 8 employees, 240 requests, 120 POs, 140 receipt events, 120 business invoices; separate 24 private negative/edge cases. Ensure the 12 STANDARD and 12 EMERGENCY 600k witness groups and threshold boundaries in FROZEN_SPEC.
3. `authoring/build.py` orchestrator with explicit output root, bundle ID, seed, reference date, stage selection and resume manifest. Copy only accepted public templates and generated public artifacts. Require explicit overwrite for an existing generated bundle; do not recursively delete arbitrary paths or write parent repository files.
4. Referential-integrity/arithmetic validation utility and small safe inspect/sample CLI. Actual observed decision/history outputs go into public `data/history/` or agreed files; expected labels/oracle responses never do.

Data constraints:

- Two BLOCKED suppliers, three HIGH-risk active suppliers, remaining normal active; allocate witnesses to normal active suppliers. IDs stable and business names fictional.
- Receipt count 140 comes from 100 one-event POs and 20 two-event POs with quantity >=2 for those splits. Freeze the subset with partial/damaged receipts. Do not fake 140 by adding zero/negative quantities or orphan records.
- Price deltas intentionally include 2% and 10k caps, above/below/equal cases. All currency is JPY. Keep goods net, freight, tax and gross separate; record every intentional hold in private case catalog.
- Baseline approval event generation may invoke the app to record actual observed decisions; mark this provenance in private build metadata. This does not establish policy truth. Any private expected evaluator must not reuse that app call or decision helper.
- Normal business invoices are one per PO with unique normalized supplier+invoice key. Duplicate/malformed cases live in private test suite and can be separately replayed. Same invoice number at two suppliers is a valid negative control.
- Produce CSV and JSONL where specified, no type drift between formats. Explicitly sort IDs and replay history. Use local random.Random(seed), not global random state/hash()/now().

Builder initially may mark workbook/form/doc stages pending with explicit stage status, **not** report a complete bundle until stage 04/05 artifacts exist. Runtime data not dependent on generator files after packaging.

Provide commands to build twice into two fresh controlled output directories, validate foreign keys/money/counts, and compare logical digests. Checks are NOT_RUN without actual execution. Do not invent file hashes, affected counts or claim realistic business accuracy from synthetic randomness.
