# Claims → evidence (first live run). A claim without a row here is not made.

| # | Claim (as worded in the README) | Evidence | Where to look | Limit |
|---|---|---|---|---|
| 1 | A model drafts the impact and proposed edits from the request and stored evidence, with citations | live draft job `ui-draft-78959fd19cce` (gemini-3.8-flash, Vertex global, 3078/703 tokens, cost 0.033 USD) | `LIVE_EVIDENCE.md`, screen 02 | one scenario, one run |
| 2 | The server verifies citations, reads the current value from the stored file and the proposed value from the confirmed request, and builds the typed edit | `tests/pitch_brief/test_change_draft.py` (model cannot change value/path, fabricated quote, foreign ref, no-op/ambiguity overrides) | test names | stub provider offline; live run consistent |
| 3 | Unclassified, manual or ambiguous items are never hidden and block "complete" | live draft PARTIAL (workbook ambiguity, policy manual, workflow unaddressed, emergency not covered); loop status PARTIAL | screens 02, 04 | |
| 4 | Proposal is generated from the draft without retyping | proposal `ui-proposal-0aa43ec3216d`; `test_draft_to_proposal.py` | screen 03 | YAML values only |
| 5 | Verification expectation is stated by a different person from the request, not from the proposal/model | contract by `change_approver`; review acceptance PASS; test "requester cannot author its own contract" | `LIVE_EVIDENCE.md` | accounts are SIMULATED; human acceptance NOT_RUN |
| 6 | Apply is guarded (digest, stale, role, duplicate) and verified by readback | apply `ui-apply-e05c66830778` COMPLETED, verification PASSED; offline negatives in `test_draft_to_proposal.py` | | |
| 7 | After apply the changed sources are re-extracted and a query on the new revision cites the new text | reanalysis 8/8; query `ui-query-0819b3623a9c` → 500000 @ the new revision | screen 05 | query model is flash-lite |
| 8 | Spend is bounded and measured | ledger read through the Auth Proxy: ~0.34 USD settled (35 rows) of a 12.82 USD cap; conservative input guard; pinned role model; global endpoint guard | `LIVE_EVIDENCE.md` | |
| 9 | Under the production-like concurrency limit (2) the re-extraction jobs all complete exactly once | `test_reanalysis_progresses_under_concurrency_limit_2...`; live 8/8 | | |

## Not claimed
Harbor complete; AI better than non-AI; accuracy figures; C02 scope_mismatch visible **live** (fixture only); emergency / Markdown / code / workbook auto-apply; knowledge acceptance finished; any pass of the held-out AI-quality criteria (they were not met, see `../evaluation/HELD_OUT_EVALUATION.md`); human acceptance.
