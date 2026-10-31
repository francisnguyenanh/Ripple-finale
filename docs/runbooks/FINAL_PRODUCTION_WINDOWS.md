# Final build — operating in windows: switch on, check, stop, back up, switch off, switch on again

Procedure for running the Final build only during planned operating windows (for example a test window and a presentation window of two to three days each) and
keeping cost low in between. Nothing here has been run against a production target; the procedure was rehearsed on a disposable staging project
(`docs/runbooks/FINAL_DEPLOY_ROLLBACK.md` for the deploy itself). Every command names its project and region explicitly; the gcloud default project is never used.

## 0. What persists, what stops, what keeps billing
| Item | Between windows | Billing when the service is idle |
|---|---|---|
| Database (Cloud SQL PostgreSQL) | **kept** (option A: instance stopped) **or** exported and deleted (option B) | A: storage, backups and possibly an idle IP address keep billing while stopped; B: only the exported dump in storage |
| Ledger, cap, job history, provenance, accepted knowledge | inside the database: kept as they are (option A) or restored from the dump (option B) | — |
| Exported dumps and a local copy | kept | storage per GiB-month |
| Container image (by digest) | kept in the registry | storage per GiB-month (small) |
| Secrets (database DSN, cookie signing key, budget configuration) | kept as references | within the free versions, otherwise per version-month |
| Cloud Run web service | kept, minimum instances 0, no traffic from outside (invoker removed or ingress internal) | none while no request arrives |
| Worker job | kept, not executed | none |
| Scheduler / backstop | paused or absent | a paused job still counts as a job |
| Budget alerts | kept | none (alerts only; they never stop spending) |
Switching off never means "no cost at all": storage, backups, images and (option A) a stopped database keep billing.

## 1. Switch on (or redeploy) for a window
1. Confirm the target: the production project id, region and image digest named in the change ticket (the digest of an exported, verified candidate).
2. Database: option A, start the instance (`gcloud sql instances patch <instance> --activation-policy=ALWAYS --project <prod>`); option B, create the instance and
   import the last dump into a new database, then compare row counts with the counts recorded at export (see section 5) before going on.
3. Schema: run the one-shot migrate job (`python -m tools.db_migrate --apply`) with the image of this window; it is idempotent and forward-only.
4. Configuration: the production budget configuration (`config/ai_closed_loop/final_budgets.production.example.yaml` as the shape) mounted from the secret store:
   explicit `enabled_roles` (never `all`), the approved total cap, prices re-read on the day (`price_snapshot_max_age_days`). Gap proposals run on
   `gemini-3.8-flash` (2026-10-28); the example prices it at the standard rate that applies from 2027-01-01, and the per-job cap (0.52) must stay at or above the
   reservation of the priciest configured model x 4 calls, or every job is refused (`budget_invariants.problems` reports it before arming).
5. Keep AI off while checking: `RIPPLE_PROVIDER_MODE=LIVE_DISARMED` on web and worker.

### 1a. Database connection envelope (decide before opening access)
Each web process holds at most `RIPPLE_DB_POOL_MAX` connections per distinct DSN (one pool per DSN; count each DSN the process uses); the worker
process does the same; operators' one-shot tools (`tools.ops_jobs`, `tools.db_migrate`) open one connection each. The sum must stay below the database's
`max_connections` minus its reserved connections:

`(old web instances x RIPPLE_DB_POOL_MAX) + (new web instances x RIPPLE_DB_POOL_MAX) + (worker executions x (RIPPLE_DB_POOL_MAX + 1)) + migration + operator tools + margin < max_connections - reserved`

Every term is counted during maintenance, the pre-traffic smoke and the rollout, not only in steady state (2026-10-30, REL-04):
- *old web instances*: the previous revision's instances that are still alive while the new one is deployed; **traffic at 0 % is not evidence that it holds no
  connections** — read `pg_stat_activity` per application and the platform's instance count, or scale the old revision to zero before counting it as 0;
- *worker executions*: the number of worker executions that can run at once. A Cloud Run job's `tasks` / `parallelism` bounds the tasks of one execution, not the
  executions a wake-up or the scheduler starts; the worker therefore holds an **execution lease** (`infra/worker/execution_lease.py`, a session advisory lock on
  one dedicated connection, hence the `+ 1`): a second execution exits at once with `another_execution_active`. With the lease on, count 1 running execution plus 1
  for the overlap of an exiting one; with the lease off (`RIPPLE_WORKER_EXECUTION_LEASE=0`) the bound is unverified and the envelope is not accepted;
- *migration*: the one-shot `tools.db_migrate` connection while it runs.
`python -m tools.db_envelope --max-connections 25 --reserved 3 --web-instances <N> --old-web-instances <M> --web-pool <P> --worker-executions <X> --worker-pool <Q>
--worker-executions-verified <evidence>` prints the sum and exits 0 only when it holds and the worker bound names its evidence; on the day add
`--observed-connections <count from pg_stat_activity>`. Example: 25 connections, 3 reserved, new web 3 x 4, worker 1 x (3 + 1), migration 1, operator 2, margin 2
= 21 < 22 holds **only after the old revision is at zero instances**; with the old revision still alive (3 x 4 = 12 more) it does not: scale the old revision to
zero (or use pool 2 on the web during the switch) before the new one is started. Set the platform's maximum instances so the formula holds; never rely on
autoscaling defaults.
- `RIPPLE_DB_WORK_SLOTS` (default: the pool size, or 16 without a pool) is how many request database units one web process runs at once off its event loop;
  keep it at or below `RIPPLE_DB_POOL_MAX` so a waiting unit waits for a slot and not inside a thread holding nothing.
- `RIPPLE_DB_POOL_WAIT` (default 15 s) bounds how long a request waits for a connection or slot; then it gets HTTP 503 `service_busy` with `Retry-After: 2`.
  Each refusal writes one log line `service_busy kind=pool|work pool={...} work={...}` (counters only: in use, peak, timeouts, waiting; no DSN, no user data).
  Repeated lines during a window mean the envelope is too small for the traffic: raise the pool within the formula or reduce instances; do not raise the
  wait to hide it.
- The worker uses the same pool setting. If it cannot get a connection, the exception ends the worker process (the platform starts a new one): before a
  provider call nothing was called and the job's lease is simply reclaimed later; after a provider call the in-flight attempt is resolved on reclaim and booked
  at its actual usage or conservatively at its reservation (never twice, never released early).
- Readiness: `GET /ready` answers 200 `{"status": "ok", "database": "ok"}` only when the Final database answers a trivial query; 503 `unavailable` when
  it does not (no address or driver text), 503 `service_busy` when the pool is saturated. Liveness stays `GET /health` (no database).
- This envelope is arithmetic, not a measured capacity: no number of concurrent users is claimed (the Final build has no load test against a production-like
  database; isolated tests prove bounded refusal and recovery only).

## 2. Checks before taking traffic
- The page shell answers; sign-in of a test account works; `/app-next/` shows the enabled AI kinds (meta `ripple-ai-kinds`) equal to the approved list.
- `python -m tools.ops_jobs list` shows `queued 0, running 0` (or only jobs this window intends to run); cancel left-over queued jobs (section 3, step 2).
- Ledger carried over: the recorded production spend equals the figure written at the end of the previous window (section 4); the remaining cap is the cap minus it.
- A deterministic smoke (import, worker run, knowledge read) passes with AI off; logs show no secret and no request body.
- Then switch AI on (`RIPPLE_PROVIDER_MODE=LIVE`), run one job of each kind the window will use if the owner wants a live smoke (charged to the production cap),
  and open access for the window's users.

## 3. Stop taking new requests and settle running jobs
1. Close new AI work first: the operator AI switch off (or `RIPPLE_PROVIDER_MODE=LIVE_DISARMED`); new AI requests are refused with a reason, nothing is created.
2. Jobs: `python -m tools.ops_jobs list`; wait until `running 0` (running jobs finish or fail under their lease); then
   `python -m tools.ops_jobs cancel-queued --reason "end of operating window N" --apply` so no old job runs by itself in the next window. Cancelled jobs keep their history.
3. Close access: remove the users' invoker binding (or set ingress to internal); the service keeps minimum instances 0.

## 4. Reconcile the ledger
- Read the production ledger: settled cost, open reservations (must be 0 once nothing runs), cost by kind. Record the figures in the window's record.
- Compare with the billed provider cost for the same period when the billing export is available (billing data arrives with a delay); a difference is reported, not
  adjusted. The cap is never reset between windows: the next window starts from the recorded spend. If the two windows fall in different budget periods, the
  owner decides whether a new allowance exists; the system never creates one.

## 5. Back up
- Only after section 3 (writes stopped, `running 0`, no open reservation): an on-demand Cloud SQL backup and a logical dump exported to the storage bucket
  (`gcloud sql export sql ... --database=<db> --project <prod>`), downloaded to a second location, checksum recorded, and the row counts per table recorded
  (they are the comparison for a restore).
- A restore returns exactly the dump: everything written after it is not in it. That is why writes stop first.

## 6. Switch off
- Option A (keep the instance): `gcloud sql instances patch <instance> --activation-policy=NEVER --project <prod>`; storage, backups and an idle address keep billing.
- Option B (lowest cost): after the dump is verified in two places, delete the instance (its automated backups go with it; the exported dump is the backup).
- Keep: the image digest, the secrets, the bucket with the dumps, the web service at minimum instances 0 without outside access, the worker job unexecuted.

## 7. Switch on again for the next window
Repeat section 1 (option B: create, import, compare counts), section 2 (ledger carried over, `queued 0`), then open access. Jobs cancelled at the end of the
previous window stay cancelled; nothing is retried automatically. A new image is used only if it was approved for this window; otherwise the same digest.

## 8. Not verified
No production project exists; option B's restore was rehearsed on staging (row counts equal to the dump, later writes absent); billing reconciliation against the
billing export has not been done for the Final build.
