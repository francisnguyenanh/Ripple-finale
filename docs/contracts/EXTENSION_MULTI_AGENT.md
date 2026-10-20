# Extension contract `ma/1` — multi-agent runs (MA-00 coordinator, MA-R1 Review Board, MA-K1 Knowledge Arbiter, MA-C1 Coder + Tester, MA-S1 Simulation) — LOCAL BUILD

One coordinator (deterministic server code) decides the flow from a fixed manifest; specialists are provider jobs with typed output over the same authorized context; the join is
deterministic. Agreement among specialists is neither evidence nor approval: a run ends `REVIEW_READY` for a person to read in the review, or `INCOMPLETE` with the reason. No
specialist may spawn work, read another's round-1 answer, or hold a business capability; `change.approve`, `knowledge.confirm` and `change.apply` stay with people.

## 1. Identities and state
- `run_id = hash(workspace, workflow, binding)`, binding = `{change_id, semantic_diff_hash, draft_hash, pin}` of the CONFIRMED change: the same reviewed version is one run.
- `child_id = hash(run_id, role, round, attempt)` is also the child's job id (kind `ma.specialist`); `join job id = hash(run_id, after child)` (kind `ma.join`, deterministic).
- Run states `PLANNED -> RUNNING -> JOINING -> REVIEW_READY | INCOMPLETE | FAILED | CANCELLED` (compare-and-set on the status column); child states `PLANNED -> DISPATCHED -> COMPLETED | FAILED | CANCELLED | LATE_IGNORED`.
- Tables `v2_ma_runs`, `v2_ma_children` (state), `v2_ma_events` (immutable). Audit rows `MA_RUN_STARTED`, `MA_CHILD_DISPATCHED`, `MA_REBUTTAL_ROUND`, `MA_RUN_FINALIZED`, `MA_RUN_CANCELLED`.

## 2. Manifest `review_board` (version 1)
Roles `finance`, `operations`, `adversarial` (required), `compliance` (optional); 2 rounds; at most 1 rebuttal call per role; 0 synthesis calls; `max_concurrency` 2 (also bounded by the
workspace concurrency limit); deadline 30 minutes; `max_children = 8`. Every role has its own rubric (`config/ma_review_board.json`); all roles share ONE pinned model role
`ma.specialist` in the budget configuration.

## 3. Context and budget
- Context: built once from the review view of the person who starts the run (rule changes before/after, operations and flags, counterevidence, open questions, inventory refs; file
  paths only with `evidence.read`); `context_hash` stored; the authorized reference ids are the only ids a finding may cite.
- Root reservation: `max_children x per-job worst case` must fit the remaining project cap at admission; it is held as a `RESERVED` ledger row of the run and each child allocates
  its own reservation out of it (`create_job(budget_reservation)`), so `booked + allocated_unspent + unallocated_reserved <= root` (exposed as `budget` on the run; `holds`). At
  finalization or cancellation the remainder is released. Children are dispatched in slots; a planned child takes a slot when one frees.
- Admission by policy (`ai.use`) for the starter; `ma.run` is held by `change_approver`, `reviewer` and `admin`.

## 4. Specialist output and validation
Keys `{status OK|NONE_FOUND|UNSURE, findings[], disputes[], notes[]}`; finding `{category financial|operational|policy|fraud|legal|other, severity critical|high|medium|low,
statement, evidence_refs, assumptions, counterexample}`. Server: unknown category/severity, unsafe text, no reference or a reference outside the pack -> rejected (counted);
`legal` findings are kept with `legal_support: unsupported` and capped at `medium` (no versioned legal source); a dispute must name a claim of the rebuttal round. `claim_id =
hash(category, normalized statement, refs)`. The pinned output schema is also sent as the provider's response schema.

## 5. Rebuttal and join
- Round 2 runs only for claims of severity critical/high raised by exactly one role; every completed role receives the claims it did not raise (one call each) and may dispute or
  re-affirm (same statement). No third round; no new specialist is added while a dispute is open.
- Join: findings merged by claim id with `raised_by`, `rounds`, `disputes`, `resolution agreed | single_role | unresolved | upheld_by_raiser_disputed_by_others`; `agreements`
  (>= 2 roles), `unresolved_disagreements`, `missing_required_roles`, per-role coverage, `evidence_sources` (one source cited by several roles counts once), limitations,
  `human_action_needed: true`, `report_hash`. Order of children never changes the hash.
- `INCOMPLETE` when a required role did not complete, the deadline passed (pending children cancelled), or the business pin moved (`pin_moved`); never silently dropped.

## 5a. Manifest `arbiter` (MA-K1, version 1)
Roles `policy_analyst` and `code_auditor` (round 1, required, findings) and `arbiter` (round 2, required, synthesis: it receives the two VALIDATED analyses as data, never the
raw model text); no rebuttal; `max_children = 3`; requires `evidence.read` (the context adds the SYN-01 code evidence: per mapped code location its status, observed text excerpt,
literals, read-back binding). The arbiter's output is a table `rows[{rule_id, comparison aligned|divergent|unverifiable, policy_says, code_shows, policy_refs, code_refs,
assumptions, options, actor_to_decide}]`; the server rejects rows for rules outside the pack, drops refs outside it and **downgrades `aligned`/`divergent` rows without evidence on
both sides to `unverifiable`** (`downgraded_from`). The table compares the decided policy with observed repository text, never with production behaviour; it changes no accepted
state and names the actor who decides (the existing decision and SYN-02 paths). Readers without `evidence.read` see the policy side only.

## 5b. Manifest `coder_tester` (MA-C1 / AIX-C1 pilot, version 1)
The CODER is the deterministic FEX-03 candidate (no model writes code). Role `tester` (round 1, required, kind `tests`) proposes TYPED cases from the decided rule before seeing the
patch: `{name, module, function, args (JSON scalars), expect_op eq|ne|gt|gte|lt|lte, expect_value, rationale}`; the server keeps only identifier-shaped names/functions, modules
and functions that appear in the code evidence of the candidate's files, scalar arguments and a known operator (everything else rejected with a reason). Role `sandbox` (round 2,
required, deterministic job `ma.sandbox`, no provider call, no reservation) renders the cases into one pytest file through a fixed template (json-encoded scalars, regex-checked
identifiers), clones the declared repository at the candidate's base twice (without and with the patch), runs the repository's protected declared commands and the generated file on
both, and reports per case `base`/`patched` outcomes with a reading `distinguishes | passes_both | fails_both | regression`, the protected results and a verdict
`review_ready | protected_tests_not_passed | no_protected_tests`. Review-ready needs the protected tests to pass; failing generated cases are findings for the developer, never a
reason to loosen a check. Repairs: 0 in this build (the coder is deterministic; a bounded repair loop is future work). Needs `evidence.read` and a completed candidate
(`candidate_job_id` in the start body; 409 `candidate_required` / `candidate_not_completed`). Staleness uses the code-change rule (the change's own commit is expected). Isolation
is advisory exactly as FEX-04.

### 5b.1 Bounded repair loop (AIX-C1, added 2026-10-17)

Role `repairer` (kind `repair`, optional, repeats; `repair_max` 2, so at most three candidates per run; attempts never reset). After a sandbox round whose verdict is not `review_ready`, the coordinator plans one repairer child with the structured failure feedback (verdict, protected command tails, generated regressions) and the literal SITES the server scanned in the base checkout for the decided rule's current value (allowlisted `.py` files only, never tests, bounded by `max_sites`). The repairer may only choose offered sites (`validate_repair_object`: exact match, no duplicates); it never writes code, names a number, touches a test or weakens a check. An accepted choice becomes a new deterministic `code.candidate` job (`<parent>-r<n>`: the parent's sites plus the chosen ones -> new patch hash) that wakes the coordinator through a join job; one more sandbox round runs on it. Termination: `review_ready` -> `REVIEW_READY`; otherwise `INCOMPLETE` with `needs_human:repairs_exhausted | repairer_proposed_nothing | repair_candidate_failed | no_progress` (a patch hash tried before) and the whole history (`tests.attempts`, `tests.history` per sandbox round, `tests.repairs` per repairer with accepted/rejected sites). The root reservation covers the repair calls (`max_children` = tester + `repair_max`). Events `MA_REPAIR_REQUESTED`, `MA_REPAIR_ROUND`. Not in this build: model-written patch bodies, OS-level sandbox, PR grants.

## 5c. Manifest `simulation` (MA-S1 / AIX-S1 pilot, version 1)
Roles `scenario_designer` (round 1, required, kind `scenarios`) and `assumption_critic` (round 1, optional, findings); role `engine` (round 2, required, deterministic job
`ma.simulate`, no provider call). Scenarios are TYPED (`policy baseline|target, arrival_rate_per_day, horizon_days, replications, capacity {role: per working day},
request_mix | null, sla_days`) and validated by the engine's own bounds (horizon 1-90, replications 1-20, rate <= 200/day, capacity 0.01-500/day, SLA 0.1-60 days, at most
20,000 requests, known approver roles only); invalid ones are rejected with the reason. The engine (`core/change_intelligence/v2/process_simulation.py`) is the product's own
seeded discrete simulation of one approval process: computational actors are queues with declared capacities on working days; requests arrive at the declared rate with the
declared mix and are routed by the deterministic policy evaluator over the rule set of the scenario (baseline = the accepted knowledge the change was confirmed against; target =
the same with the change's `after` rules); only rules whose fields the engine's facts can decide route anything (the rest is listed as coverage); INDETERMINATE requests are
counted `unsupported`, never cleared. KPIs per scenario across replications (mean/min/max): cycle time mean/median/p95, SLA breach share, arrivals/completed, unsupported share,
served and utilisation per role, routes; checks: conservation (`arrivals = completed + denied + unsupported + in_system`), identical route sets across replications; same inputs
and seed -> same `result_hash`. The report compares every scenario with a default baseline scenario and carries the limitations (declared assumptions, no calibration, no
behavioural response, min/max over replications, exploratory, not a forecast). No model-generated simulation code is executed.

### 5c.1 Convergence and calibration harness (added 2026-10-17)

The engine child runs the baseline over `seeds` independent seeds (default 3) and reports `convergence` (per headline KPI the spread of per-seed means relative to their mean against `convergence_tolerance_rel`; statistical convergence of the engine, never agents' agreement). The run may carry a validated `calibration` block (`observed {source, n, cycle_time_mean_days?, cycle_time_p95_days?, sla_breach_share?}`, `tolerance_rel` per KPI or `default`): the engine backtests the baseline against it -> `calibration.status uncalibrated | within_tolerance | outside_tolerance` with per-KPI relative errors. `within_tolerance` on synthetic or unaccepted data never turns the result into a forecast: the comparison keeps its exploratory limitations; the data owner's acceptance of the holdout and of the tolerance is the gate.

## 6. Routes
`POST /v2/ma/runs {workspace_id, change_id, workflow review_board|arbiter|coder_tester|simulation, candidate_job_id?}` (`ma.run` + `ai.use`; 409 `not_confirmed`, `knowledge_moved`, `budget_exceeded`, `role_model_not_pinned`, `evidence_rights_required`, `provider_disarmed`);
`GET /v2/ma/runs/{run_id}` (`project.read` + change visibility; refs only with `evidence.read`); `POST /v2/ma/runs/{run_id}/advance` (idempotent recovery step, `ma.run`);
`POST /v2/ma/runs/{run_id}/cancel {reason}` (`ma.run`); `GET /v2/business/changes/{change_id}/ma`.

## 6a. Advisory memos (SUR-06 risk memo, SUR-08 synthesis memo; added 2026-10-19)
Two pinned roles write advisory memos over an immutable pack of server facts (`core/change_intelligence/v2/advisory_memo.py`, worker `infra/worker/advisory_memo.py`,
configs `config/change_risk_memo.json`, `config/ma_synthesis_memo.json`; one provider call per job; the result is stored on the job only).
- **Risk memo** (`change.risk_memo`): over the impact report of a CONFIRMED change (operations, affected rules with the algorithm's severity, open questions) and, when
  `board_run_id` names a finished review-board run of that change, its findings. `POST /v2/business/changes/{change_id}/impact/risk-memo {workspace_id, attempt 1-3, board_run_id?}`
  (human actor, `ai.use`, change visibility; 409 `not_confirmed`, `board_not_finished`, `provider_disarmed`, `budget_exceeded`); `GET .../impact/risk-memo?board_run_id=`
  (`project.read` + change visibility; reads only).
- **Synthesis memo** (`ma.synthesis_memo`): over a finished review-board report. `POST /v2/ma/runs/{run_id}/synthesis-memo {workspace_id, attempt 1-3}` (human actor, `ai.use`,
  change visibility; 409 `board_not_finished`, `not_a_review_board`); `GET /v2/ma/runs/{run_id}/synthesis-memo`.
- **Identity:** one job per (change or run, attempt, board report hash, frozen config + pinned model); the same request again returns the same job and makes no new call; a
  board report that moves after the request fails the job `board_report_moved` (never re-read silently).
- **Server checks:** each statement cites offered refs only (a written ref is matched exactly, or by two deterministic repairs onto exactly one offered ref: the prompt line's `ref=` label copied with the value, or a non-numeric bare id such as `rule-x` for `rule:rule-x`; an unknown, ambiguous, numeric or wrongly prefixed ref refuses the statement `ref_not_offered`; a kept statement keeps the written form in `refs_written`, counted in `counts.refs_canonicalized`); every number is written in a cited fact (no invented money, probability, percentage or count); forecast /
  certainty wording and status claims (approved, compliant, implemented, verified ...) the facts do not hold are refused; materiality is never below the cited facts'
  algorithm or board severity (`floor_applied`); a refused statement is dropped whole with its reason. Every `must_address` fact (HIGH severity, open question, minority or
  unresolved finding, missing required role, human action) not cited by a kept statement is listed by the server in `not_addressed`; `complete` is false then.
- **Authority:** a memo changes no severity, verdict, required review, relation, knowledge or change; no source evidence is put in the prompt (titles, severities, reasons,
  finding statements only). Failed / pending memos say so and conclude nothing about risk. Real-model memo quality and usefulness are not established by the local build.

## 7. Recovery
A child writes its validated result into its own row BEFORE creating its join job, so the join never races the worker. A worker dying between the child's completion and the join
creation leaves the run RUNNING with a terminal child: `POST .../advance` (or the next child's join) finishes it. Late results of a cancelled run are recorded `LATE_IGNORED`.

### 7.1 Recovery evidence (added 2026-10-17)

`tests/final_loop/test_ma_recovery.py`: two worker connections draining one run (every child once; sequential and parallel scheduling give the same report hash), a dead worker's expired lease reclaimed and its stale token fenced, a knowledge version moving under a run (`INCOMPLETE pin_moved`, report bound to the original pin, nothing written into the new version), an optional role failing without becoming consensus. A specialist whose `prepare` fails records its child FAILED and wakes the coordinator (no operator step needed). `tests/final_loop/test_ma_process_kill.py`: a real worker process killed while a specialist call is in flight leaves the durable attempt as `USAGE_UNKNOWN` booked at its ceiling (the run's budget invariant counts it); the reclaim respects the approved retry limit (with retries off the child fails `provider_attempt_limit_reached` without a second call); when the worker ends a child's job before any executor code runs (retry limit, per-job cap, cancelled before execution, refused at admission) it calls the executor hook `on_terminal_without_execution`, which marks the child FAILED and enqueues its join job, so a dead LAST child never leaves the run waiting for an operator.

## 8. Not covered (OPEN)
Real-model quality of any specialist (synthetic stubs only in this build); the value gate against a single reviewer (plan 10.4); calibration of the simulation
against observed data, behavioural response models and LLM-driven actor cohorts (MA-S1 beyond the pilot); a bounded repair loop and model-proposed patch bodies for the coder (AIX-C1 full loop); a UI surface for the report (API only here); service identities for agents; adoption of an arbiter row into the
SYN-02 reconciliation status is a person's decision through the existing paths.
