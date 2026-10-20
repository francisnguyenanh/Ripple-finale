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

### Semantic drift ADVISORY — `ripple-semantic-drift/1` (SUR-04, added 2026-10-19)
- `POST /v2/business/ci/semantic-drift {workspace_id, repo_id, base, head, attempt 1-3}` (human actor, `ai.use` + `evidence.read`, a repository declared for the workspace and
  visible to the caller; `base`/`head` resolved to commit SHAs at request time; 409 `unknown_reference`, `no_accepted_knowledge`, `provider_disarmed`, `budget_exceeded`; 404 for an
  undeclared or hidden repository); `GET /v2/business/ci/semantic-drift/{job_id}` (`project.read` + `evidence.read`). One job (`ci.semantic_drift`, pinned role of the same
  name) per repository, SHAs, accepted snapshot, attempt and frozen config + pinned model; the same request again makes no new call.
- Exact part first and unchanged: with a protected `ci_mapping` declared on the repository the job runs the scalar check above on the same commits and stores its report;
  otherwise `exact.status: not_configured`.
- Advisory part: a model reads the bounded diff (paths in the repository's allowlist when one is declared; secret-like files never sent; withheld paths and truncation
  listed in `coverage`) and the accepted rules, and proposes findings (`operator_change`, `actor_change`, `exception_removed`, `condition_change`, `route_change`,
  `threshold_moved_elsewhere`). The server keeps a finding only if its rule was offered, its path is in the diff, its witness lines are verbatim changed lines of that path
  (a deleted file keeps its old path) and its numbers come from the witnesses or the rule; a comparator claim also gets `operator_witness: confirmed | not_shown`. Kept
  findings are `status: advisory` (`server_checked: witness_lines_exist`); nothing sendable -> no model call (`not_run_nothing_sendable`).
- Never a PASS: `never_a_pass: true` on every result; "no finding kept" is not compliance; nothing is written to the repository, the knowledge or a branch protection. A
  required-check integration remains a separate owner decision.

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

### Analyst memo over a SYNTHETIC replay — `ripple-roi-analyst/1` (SUR-07, added 2026-10-19)
- `POST /v2/business/roi/analyst {workspace_id, records (1-2000), baseline_revision, target_revision, assumptions, label, synthetic: true, attempt 1-3}` (human actor,
  `project.read` + `ai.use`); `GET /v2/business/roi/analyst/{job_id}` (`project.read`; the asker or a holder of `knowledge.confirm`; records never returned).
- **Real data is refused** before any job exists: without `synthetic: true` and a label starting with `SYNTHETIC` the answer is 409 `real_data_not_granted` (no data owner has
  granted real data). One job (`roi.analyst`, pinned role of the same name) per dataset hash, revisions, assumptions, label, attempt and frozen config + pinned model.
- The job recomputes the replay with the deterministic engine; the model reads only the metric table (numeric fields of the report) and the declared assumptions, and writes
  statements citing metric keys, questions about declared assumptions and up to three sensitivity values for a declared assumption. The server keeps a statement only if every
  number in it is the value of a metric it cites; realised-ROI / saving / forecast / certainty wording is refused; a scenario is kept only for a declared assumption with a
  positive number and is **computed by the replay engine** (`computed_by: roi_replay engine`, its own report hash), never by the model.
- Labelled on every result: a replay of a SYNTHETIC dataset; not a realised ROI, a saving or a forecast; nothing about real data.
