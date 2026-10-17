# D0 definition (version d0.3.0): deterministic AI-off extractor

Written from the extraction output schema and rule-class spec and small synthetic fixtures only. Not tuned on the WP9a DEV labels, the private CHECK split, or the Pitch evaluation set (never seen).

## Interface
`pitch.d0.extract_from_observations(observations, artifact_id, source_revision_id, blob_hash, source_role)` and `extract_from_store(store, tenant, ws, revision, artifact_id, source_role)`. Input = the same persisted observations production extraction reads (`yaml.values.v1` key-path leaves, markdown chunks). Output = the same result dict as `ExtractRulesExecutor.execute` (`candidates` with identical per-candidate keys, `rejected`, `model_run_id`, `schema_version`, `executor_id`, ...), each candidate validated by `RuleCandidate`; extra keys `d0_version`, `provider_calls`=0, `live_call_provenance`="d0_deterministic". Evidence quote for a structured leaf is `<key.path>: <value>`, the same form the model is shown. Output is a pure function of the input (run id = sha256 of input).

## What it extracts (structured YAML leaves; last key segment tokenized on `_ - space camelCase`)
1. Numeric rule: token in {limit, threshold, max, maximum, min, minimum, cap, ceiling, floor, quota, deadline, sla, tolerance, retention, timeout, ttl, budget, retries, required} gives modality `must`; token in {amount, days, hours, minutes, rate, percent, pct, count, fee, interval, period, cooldown, duration, size, length} gives `observed`. Value must be a number or a numeric string ("500,000", "10 JPY"). A trailing currency/unit token in the key (jpy, usd, days, hours, minutes, pct, ...) becomes `unit` and is removed from the predicate.
2. Flag rule: boolean value with token in {require(s/d), mandatory, enforce(d), forbidden, prohibited, block(ed)} gives `must`; {allow(ed), optional, permit(ted)} gives `may`; {enable(d)} gives `observed`.
3. Outcome rule: string value with token in {approver(s), reviewer, escalate, escalation}; modality `must`, `outcome` = value.
- subject = parent key path (or `<root>`); predicate = remaining key tokens joined by `_`.
- scope = `{"section": parent path}` plus same-parent sibling scalars whose key is in {currency, unit, region, department, category, type, applies_to, scope, tier, level, grade, country, segment, channel}. Currency/unit siblings also fill `unit`.
- exceptions = same-parent sibling keys with token in {exception(s), exempt*, exclude(d), except} (list or string).
- Candidates with identical (subject, predicate, scope) merge, evidence combined (same rule as production).

## Prose chunks (markdown/text; roles normative/unknown/test only)
One sentence-level regex: `<subject> (must not exceed | must be at least/at most | must | shall | may) ... <number> [unit]`. Result is `applicability=UNKNOWN`, `applicability_reason=missing_scope`, scope `{"unresolved": true}`, like production's missing-scope handling.

## v0.2 reuse of product deterministic code
- Numeric/unit parsing uses `infra.worker.rule_identity._parse_value` (stdlib-only; checked provider-free by a fresh-process test and the AST test). Ambiguous text is not emitted as a number. Embedded units ("1,500 USD", "12 %", "30 minutes") become `unit`.
- Operator values (">= 3", "≥ 10") keep their original text as `value` (so downstream `value_comparison` parses them) and the operator is named in `rationale`; the production candidate dict has no operator key, so none is added. Modality is not derived from the operator.
- `_value_text` stays a local copy (importing `infra.worker.extraction` loads provider-adjacent modules); a parity test compares it with production.
- Alias index / `scope_key` not used (D0 merge fingerprint unchanged; not a trivial drop-in).
- xlsx/csv/workbook cells are skipped on purpose: Pitch scope is YAML + Markdown.
- No product deterministic rule extractor exists in the repo; D0 vocabulary and regex are new by necessity.

## Known limits
- Keyword vocabulary: a rule whose key lacks a registered token (e.g. `rule_x: 5`) is missed; a key with a token that is not a rule (e.g. `page_size`) is emitted.
- Does not read lists as rules, nested conditions, effective dates, cross-key relations, workbook cells (skipped), or non-English keys (non-ASCII key characters are dropped by the tokenizer, so distinct such keys can collapse and merge). Operator values are kept as text (see v0.2).
- Python constants ARE processed if present: any observation whose locator has a `symbol` is treated like a key-path leaf (e.g. `MAX_LIMIT = 500` gives subject `<root>`). Python is outside Pitch scope (YAML + Markdown); this is incidental behavior, not a designed rule class.
- Guard limits observed in testing: (a) the guard is process-wide, so a legitimate loopback connect in another thread is blocked while any D0 run is inside it; (b) the counter list is shared and cleared by the next outermost run, so a counter list held from an earlier run aliases the current one; (c) asyncio event-loop creation on Windows is blocked inside the guard (it needs a loopback socket).
- Mutation result (mutation test log, guard.py + extractor.py): 28 killed, 0 survived, 6 argued equivalent (N08, N11, N18, X02, X08, E14), 0 invalid.
- D-T9 (disclosed limitation): a same-subject, same-scope pair of keys inside one artifact, such as `max_amount` vs `maxAmount`, merges into one candidate with no CONFLICT. This is a shared product merge rule that affects both arms; D0 keeps the sorted-first path. The affected cases still count as errors, and extraction/normalization errors are reported separately from downstream conflict errors.
- Merge: candidates with the same (subject, predicate, scope) merge exactly as in production extraction (first value kept, all quotes kept as evidence). A differing second value is NOT surfaced as a conflict; production behaves the same, so both arms share this limit. Keys differing only by case/separator (`maxAmount` / `max_amount`) merge too.
- Scope siblings: scalar, bool and non-empty scalar-list values are kept in scope (v0.3, per the product `scope_key` contract); dict-valued siblings are left out.
- Provider-call claim is "0 provider calls" (guard + sentinel tests). The production job path still writes ledger rows (cost 0) for an `extraction` job; that does not contradict it.
- Guard (v0.3) is depth-counted and lock-protected: overlapping/non-LIFO/threaded entries are safe and restore originals when the last exit happens; an exit without an enter raises. It patches only the extraction/live_analysis client factories, genai.Client, AIRouter and socket connect/create_connection (not gap_answer/impact_judge, outside D0's path). It blocks loopback too.
- Observation without a `locator` key raises KeyError for the run; keys containing `!` are treated as workbook cells and skipped; `source_role` is case-sensitive; skipped leaves leave no trace.
- Scope is structural (path + sibling keys), not semantic; "applies to" statements in other keys are not linked.
- Prose: one number per sentence, English only, no scope, no negation handling beyond the listed phrases.
- Modality is a vocabulary decision, not a reading of intent.
- Zero provider/network calls: `pitch/d0/guard.py` raises on provider client factory, google.genai.Client, AIRouter and socket connects inside every run; sources import no provider modules (tested).
