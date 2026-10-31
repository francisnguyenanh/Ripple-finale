# Final build — deploy, verify and roll back (runbook; nothing here has been executed for the Final build)

This is the procedure an operator follows **after the owner decides to deploy**. No cloud resource, IAM binding, secret, image or deployment exists for the Final build; the
target platform, project and access model are the owner's decision. Recovery of a running system is in [`FINAL_RECOVERY.md`](FINAL_RECOVERY.md).

## 1. The candidate
- Build only from an **exported tree** (the submission export of a committed state), never from the development repository. The export's `SUBMISSION_MANIFEST.json` names the
  source commit, every file hash and the overall hash; record both in the change ticket.
- Before building: `export_submission.py verify` and `eol-check` on the export return 0; `python -m pytest tests/contract tests/unit -q` inside the export passes.

## 2. Image and configuration
- Image: the repository `Dockerfile` (Python 3.11 slim, pinned `requirements.txt`, one `uvicorn infra.judge:app` worker on port 8080). Deploy by **image digest**, not tag.
- The worker runs the same image with `python -m infra.worker.service` (`RIPPLE_V2_DSN`, `RIPPLE_WORKER_TENANT_ID` / `RIPPLE_WORKER_WORKSPACE_ID` or `RIPPLE_WORKER_WORKSPACES`, `RIPPLE_WORKER_POLL_SECONDS`; `GET /healthz` for the liveness probe); jobs are leased with fencing (see the recovery runbook).
- Configuration by environment variable only; secrets from the platform's secret store, never in env files or the image:
  - database DSN and session secret (secrets);
  - `RIPPLE_JUDGE_ORIGIN` (the exact public origin), `RIPPLE_SESSION_LIFETIME_SECONDS`, `RIPPLE_DB_POOL_MAX` / `RIPPLE_DB_POOL_WAIT` / `RIPPLE_DB_WORK_SLOTS` (sized by the connection envelope in `FINAL_PRODUCTION_WINDOWS.md` section 1a);
  - `RIPPLE_WORKER_EXECUTION_LEASE` (default on; the worker's one-execution lease, see `FINAL_PRODUCTION_WINDOWS.md` section 1a);
  - login protection (`infra/login_protection.py`): `RIPPLE_RATE_SHARED=1` (the counters are shared by every instance through the database), `RIPPLE_LOGIN_SCHEMA_AUTO=0` (both guard tables, `v2_login_failures` and `v2_rate_windows`, come from migration v2_042; no request path issues DDL), `RIPPLE_TRUSTED_PROXY_HOPS` (the ingress contract: 0 = no trusted proxy, the peer address counts and `X-Forwarded-For` is ignored; n = the n-th entry from the end of `X-Forwarded-For` was appended by a trusted hop; a value is **configuration verified on the deployment**, never assumed: the pre-traffic step D21 sends three requests, without the header, with one forged entry and with two, and the resolved source must be the sender's address in all three; a request with fewer entries than hops is `untrusted` and refused early), `RIPPLE_LOGIN_PRIVILEGED_ACCOUNTS` (comma-separated account ids whose global layer locks earlier and longer; privileged credentials are never published), the limits `RIPPLE_LOGIN_MAX_FAILURES` / `RIPPLE_LOGIN_LOCKOUT_SECONDS` (pair, 5 / 300), `RIPPLE_LOGIN_SOURCE_MAX_FAILURES` / `RIPPLE_LOGIN_SOURCE_LOCKOUT_SECONDS` (source, 100 / 300), `RIPPLE_LOGIN_ACCOUNT_GLOBAL_MAX_FAILURES` / `RIPPLE_LOGIN_ACCOUNT_GLOBAL_LOCKOUT_SECONDS` (account from every source, 60 / 60), `RIPPLE_LOGIN_MAX_ROWS` (system ceiling of counter rows, 200000; above it a new key is refused `429` and `login_rows_ceiling_reached` is logged once a minute; recovery is retention over time or `DELETE FROM v2_login_failures WHERE locked_until < extract(epoch from now())` by an operator);
  - AI: `RIPPLE_BUDGET_CONFIG_PATH` — a **production** budget configuration with its own cap, the roles to enable and their pinned model (the evaluation configuration and
    ledger of the development runs must not be reused); `RIPPLE_PROVIDER_MODE` and the provider switch decide whether real models are called at all (off = labelled simulated
    or deterministic fallback, never a silent success);
  - the production budget configuration lists `enabled_roles` explicitly (the AI features this deployment offers; `all` is for development and evaluation only;
    absent = no AI feature runs); see `docs/contracts/AI_CLOSED_LOOP_BUDGETS.md` section 6a;
  - the request assistant is ON by owner decision DEC-0009 (2026-10-27): leave `RIPPLE_REQUEST_ASSISTANT` unset (never `off`) and set the runtime switch `request_assistant` to `on` where operator switches are active; its pre-traffic smoke row is in the pre-traffic checklist;
  - optional features stay off unless decided: `RIPPLE_SEMANTIC_RETRIEVAL`, `RIPPLE_FIREBASE_MODE`, `RIPPLE_PROJECTION_SINK`, `RIPPLE_NOTIFICATION_TRANSPORT`.
- Prompt / schema pins: the pinned roles in `docs/evaluation/FINAL_AI_QUALITY_PLAN.md` are checked by `tests/contract/test_final_prompt_pins.py`; a changed pin invalidates its
  evidence.

## 3. Database
- **Stop writes, then back up** (a full logical dump of the target database, checksum recorded) — the only rollback for data. Writes stop before the dump (workers scaled
  to zero, provider switch off, a maintenance notice, traffic held on the old revision) and stay stopped until the migrate step and the smoke pass; otherwise rows written
  between the dump and a restore are lost. Order of the drain (2026-10-30): first close job creation (`ai_switch` off, `set_window_until(now)` so `window_creation_closed`
  refuses new jobs, the maintenance notice), then wait for `QUEUED = 0` and `RUNNING = 0` (`tools.ops_jobs list`) with a bound of 10 minutes; a job still QUEUED is
  cancelled by `tools.ops_jobs cancel-queued --apply` (audited); then `worker_claims` off and the wake kick budget frozen. **A lease that expired is not proof that the
  process stopped:** before the dump, the worker's execution lease must be absent (no session holds advisory lock `0x52495057`: `SELECT count(*) FROM pg_locks WHERE
  locktype='advisory' AND objid=1381256279`) and `pg_stat_activity` must show no worker application; only then is the backup taken and the migration run.
- Apply the schema as a **one-shot step before the new revision takes traffic** (the stores do not migrate at web start-up): `python -m tools.db_migrate` shows the target
  (host / port / database only), `python -m tools.db_migrate --apply` applies the v2 store and identity store migrations (exit 0 done, 2 DSN missing, 3 a statement failed).
  The SQL is forward-only, additive and idempotent; the upgrade of a populated database is verified by `tests/final_loop/test_rel02_upgrade_chain.py` (rows byte-identical,
  triggers intact). There is no down-migration.
- Never point the service at a database used for tests or at a development ledger.

## 4. Deploy and verify (staged)
1. Deploy the new revision **without traffic** (or to a separate instance).
2. Smoke on that revision, authenticated: health; readiness (`/ready` reports `"database": "ok"`); sign-in of a test account; workspace create; one knowledge read; one queued job executed by the worker with the provider switch
   **off** (deterministic / simulated path); the cost ledger shows no unexpected entry; logs carry no secret or request body.
3. If live AI is enabled: one job per enabled role within the production cap, reserved before the call and settled after it.
4. Move traffic to the new revision; keep the previous revision deployable.

## 5. Roll back
- **Application:** route traffic back to the previous revision / previous image digest (workers likewise).
- **Data:** migrations are additive, so the previous image runs on the migrated schema; this was rehearsed **locally** (an earlier Final commit's database suites passed on a
  database migrated and written by the candidate, then the candidate passed again on the same database). Prefer rolling back the application only. Restore the pre-deploy
  dump (section 3) only if the previous image cannot run on the schema or data are corrupted. **A restore is not a lossless rollback:** every row written after the dump is
  lost (the local rehearsal showed the restored database equal to the dump table by table and without the rows written afterwards); those writes must be re-entered or
  reconciled from the audit records, and the restore goes to a separate database first, is checked (row counts, triggers, one read-only smoke), then swapped in.
- **Spend stop:** turn the provider switch off (no real model call; queued AI jobs stay queued by design), scale workers to zero.

## 6. Not verified for the Final build
No deployment on any cloud; no staging; no load, latency or availability measurement; previous image on the new schema and restore checked only locally on one PostgreSQL 16
container (no point-in-time recovery); the production budget configuration exists only as the example `config/ai_closed_loop/final_budgets.production.example.yaml` (copied outside version control and mounted at deploy time; not deployed); no human acceptance of AI quality. A deploy decision weighs these explicitly.
