# Extension contract `gov/1` — governance over the accepted knowledge (LOCAL ONLY in the Final build)

Four pieces that read the accepted business knowledge and decide, package, check or replay deterministically. None of them calls a model, pushes, deploys or reaches an
external system; each says what it does not prove.

| Piece | Entry point | What it does | Not covered (OPEN) |
|---|---|---|---|
| Typed policy decision (MON-POL) | `POST /v2/business/policy/decide` (`core/change_intelligence/v2/policy_decision.py`) | `ALLOW / DENY / REQUIRES_APPROVAL / INDETERMINATE` for typed facts against one accepted snapshot; conservative (a missing fact, an unevaluable exception, an open question make it INDETERMINATE); audited; bound to a knowledge version and a facts digest | a bearer authorization; service identity; enforcement at the tool boundary of an external agent |
| Audit package (MON-AUD) | `POST /v2/business/audit/export` | a scoped zip of records with a manifest, redaction by evidence rights and a standalone verifier that detects tampering and unlisted files | signatures / KMS; mapping to a regulation |
| Business CI drift check (MON-CI, advisory) | `python -m tools.business_ci.check` (`core/change_intelligence/v2/business_ci.py`) | for one repository state (`base` -> `head`) and one pinned policy + protected mapping, a versioned report per mapped rule: `aligned`, `drift`, `no_impact`, `unsupported`; conclusion `aligned` / `drift` / `incomplete` with `would_block`; a stored report is verified by hash and against the current head | the required-check integration on a real repository (the completion gate of the package); any profile other than YAML existing scalars; any execution of repository code |
| Historical replay (MON-ROI, engine + one scoped compare request) | `POST /v2/business/roi/replay` (`core/change_intelligence/v2/roi_replay.py::replay`) | the same frozen cohort decided under a baseline and a target policy with the MON-POL evaluator; counts and values by band, director involvement, declared-assumption effort hours with a range, a cost range only when an hourly cost is declared; `not_estimated` where it cannot compute; INDETERMINATE rows listed as unsupported and excluded from every saving | an authorised real sample, data-quality/cohort review and the data owner's acceptance (the completion gate); any behavioural, risk or net-ROI claim; currencies other than JPY |

## Business CI drift check — report `ripple-business-ci/1`
- Inputs: the policy (`{snapshot_id, business_revision, rules}` as the product stores accepted rules), the mapping (`{profile: "yaml-scalar", repo_id, policy_hash, files: {"approval.yaml": "config/approval.yaml"}}`), the repository path, `base` and `head`.
  The mapping is pinned to the policy hash and to a repository id: a moved policy or another repository yields `incomplete` with nothing evaluated. The mapping and the checker
  are outside the candidate change's reach by construction of the integration (protected review); the CLI itself only reads.
- Checkable rule: effective (a TARGET rule replaces its OBSERVED root), one numeric comparison in its condition, a `file.yaml#dotted.key` source binding whose file the mapping maps.
  Rules without a YAML binding are listed as `unmapped` (coverage, not checked, never "passed"); YAML-bound rules the profile cannot check (file not in the mapping, other condition
  shapes) are `unsupported` and block.
- Per rule: `aligned` (touched by the diff, equals the policy), `no_impact` (not touched, equals the policy), `drift` (differs; `introduced_by_diff` says whether the diff caused it; pre-existing drift
  is still drift), `unsupported` (location unreadable or not numeric at head).
- Conclusion: `drift` or `incomplete` -> `would_block: true`; `aligned` only when every mapped rule reads the policy value and coverage is complete. Exit codes 0 / 2 / 3 / 4 (usage).
- `verify`: hash of the stored report (a forged body is detected) and the current head (a moved head makes the report stale).
- Limits: bounded drift on mapped scalars, never proof of a program's business semantics; GitHub `skipped`/`neutral` must be mapped to blocking by the integration, not here.

## Historical replay — report `ripple-roi-replay/1`
- Records: `{id, time (ISO 8601), amount_jpy, currency, category, process, supplier_risk}`; quality check excludes duplicates, missing fields, bad dates/amounts, unsupported currencies and
  rows outside the effective window, each with a reason; the dataset hash is over the valid rows.
- Facts per record: `process`, `request.net_amount_jpy`, `request.category`, `supplier.risk`; both policies are evaluated on exactly the same rows.
- Metrics: approvals, director-required, requires/deny/allow per side; changed rows with value by band (`lt_1m`, `1m_to_2m`, `2m_to_5m`, `gte_5m`); `approvals_delta`, `director_delta`;
  effort = approvals x declared minutes (with the declared range); cost = delta hours x declared hourly cost (gross, no implementation cost, no horizon: never a net ROI) or `not_estimated`;
  risk fields are always `not_estimated`.
- Determinism: same inputs give the same `report_hash`; denominators reconcile (`cohort = replayed + unsupported`).
- Coverage: only rules whose condition fields, scope keys and exception fields are columns of the dataset (`process`, `request.net_amount_jpy`, `request.category`, `supplier.risk`) are replayed; every other rule is listed under `coverage.<side>.rules_not_evaluated` with its reason (`fields_not_in_dataset`, `rule_condition_unknown`) and decides nothing. An INDETERMINATE decision on an evaluable rule (an open question, an unevaluable exception, an unknown unit) makes the row `unsupported`.
- Route `POST /v2/business/roi/replay` `{workspace_id, records (<= 10,000), baseline_revision, target_revision, assumptions, window_from?, window_to?, label?}` (`project.read`): both revisions must be ACCEPTED versions of the workspace (`409 unknown_reference` otherwise); the caller's records are replayed and never stored or echoed; one audit row `ROI_REPLAY_RUN` carries the hashes and denominators. The response adds `pin {baseline, target}` with snapshot ids.
