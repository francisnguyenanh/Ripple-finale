# Stage 06 — Independent expected evaluator

**Operator: start a fresh Vertex conversation.** Set the same system instruction. Supply only frozen spec, accepted data/function interface contracts, approved case allocation specification and any human amendments. Do not supply application source, app test results or model-produced actual outcome files when writing this evaluator.

---

Act as the independent fixture test author. Generate deterministic expected evaluators and tests from the supplied policy/observed-state specification, not from the application under test. You have not executed any code unless a real execution tool is available.

Outputs belong only to `evaluator_private/` and private reports tooling. Do not copy these files into Ripple input.

Required batches:

1. `truth_contract.json` / human-readable decision table: normative facts, observed S0 implementation, human-unknown issues, approved S1/S2 overrides, private C01/C02/G01/G02 metadata. Explicitly distinguish future emergency normative UNKNOWN from its observable S0 hardcoded behavior. Validate hand-calculated anchors with reasoning concise enough for human review.
2. `oracle.py`: standalone evaluator using standard library, integer arithmetic and declarative decision tables/ordered policy predicates. It must not import/call app routing/matching, Ripple modules, existing single-threshold factory oracle or semantic parser to discover expected policy. No model calls. Avoid copying the implementation algorithm line-for-line; expected behavior comes from independent tables.
3. `tests/test_oracle_anchors.py`: fixed manually specified anchors from FROZEN_SPEC, boundary values and priority controls, symmetric tolerance differences, date arithmetic, maker/checker, supplier invoice normalization, missing scope. These expectations must not be computed by the oracle being tested.
4. `validate.py` and harness: execute the reference app as an isolated subprocess against data to obtain ACTUAL outputs; compute EXPECTED separately from fixture spec; compare, never rewrite expectations to match results. Sandbox/no-network execution is operator infrastructure, not fake protection by this script. Use explicit source/scenario version.
5. Dataset integrity, file/schema/hash/foreign-key/money checks; workbook reopen/roundtrip; public/private leakage scan; no-source-outside-root import/read checks; exact conformance report of planned inconsistencies vs unexpected failures.

Separate reports:

- `application_health`: syntax/import/characterization/data integrity, expected PASS.
- `baseline_policy_findings`: expected conflicts/GAPs C01/C02/G01/G02; their presence is not a broken build. Normative questions remain unresolved where specified.
- `scenario_behavior`: actual vs desired executed outcomes, source/corpus/config hashes, changed effective requests counted once, before/after routes; no fixed "47" count.
- `knowledge_gold`: private labelled rule representations/relations/conflict types/minimal justified impacts. Human review required for semantic label correctness; cite materialized evidence spans only after files exist.

Anti-circularity constraints:

- Monkeypatch application routing to return AUTO for every valid request: at least one independent comparison must fail.
- Alter emergency source to ignore changed config in S1: independent expected S1 check must fail.
- Change workbook C01 manager cell: expected conflict reporting updates from reading actual artifact values, but canonical policy values come from frozen spec, not that reader.
- Change oracle expected target merely to match application: anchor tests must fail. Do not generate tests that call the oracle to compute their own assertions.
- Evidence locator resolution may read actual source/workbook to verify position/hash; this is distinct from deriving policy truth by parsing the code under test.

Produce a human review checklist and mark oracle provisional until reviewed. A fresh model conversation reduces shared context but does not guarantee independent correctness. All hashes/PASS/affected totals remain NOT_RUN until real scripts run. Never "repair" intentional baseline conflicts as part of oracle development.
