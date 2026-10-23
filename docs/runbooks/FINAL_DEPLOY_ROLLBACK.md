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
  - `RIPPLE_JUDGE_ORIGIN` (the exact public origin), `RIPPLE_SESSION_LIFETIME_SECONDS`, `RIPPLE_DB_POOL_MAX` / `RIPPLE_DB_POOL_WAIT`;
  - AI: `RIPPLE_BUDGET_CONFIG_PATH` — a **production** budget configuration with its own cap, the roles to enable and their pinned model (the evaluation configuration and
    ledger of the development runs must not be reused); `RIPPLE_PROVIDER_MODE` and the provider switch decide whether real models are called at all (off = labelled simulated
    or deterministic fallback, never a silent success);
  - optional features stay off unless decided: `RIPPLE_SEMANTIC_RETRIEVAL`, `RIPPLE_REQUEST_ASSISTANT`, `RIPPLE_FIREBASE_MODE`, `RIPPLE_PROJECTION_SINK`, `RIPPLE_NOTIFICATION_TRANSPORT`.
- Prompt / schema pins: the pinned roles in `docs/evaluation/FINAL_AI_QUALITY_PLAN.md` are checked by `tests/contract/test_final_prompt_pins.py`; a changed pin invalidates its
  evidence.

## 3. Database
- **Back up first** (a full logical dump of the target database, checksum recorded) — the only rollback for data.
- Apply the schema as a **one-shot step before the new revision takes traffic** (the stores do not migrate at web start-up): `python -m tools.db_migrate` shows the target
  (host / port / database only), `python -m tools.db_migrate --apply` applies the v2 store and identity store migrations (exit 0 done, 2 DSN missing, 3 a statement failed).
  The SQL is forward-only, additive and idempotent; the upgrade of a populated database is verified by `tests/final_loop/test_rel02_upgrade_chain.py` (rows byte-identical,
  triggers intact). There is no down-migration.
- Never point the service at a database used for tests or at a development ledger.

## 4. Deploy and verify (staged)
1. Deploy the new revision **without traffic** (or to a separate instance).
2. Smoke on that revision, authenticated: health; sign-in of a test account; workspace create; one knowledge read; one queued job executed by the worker with the provider switch
   **off** (deterministic / simulated path); the cost ledger shows no unexpected entry; logs carry no secret or request body.
3. If live AI is enabled: one job per enabled role within the production cap, reserved before the call and settled after it.
4. Move traffic to the new revision; keep the previous revision deployable.

## 5. Roll back
- **Application:** route traffic back to the previous revision / previous image digest (workers likewise).
- **Data:** migrations are additive, so the previous image normally runs on the migrated schema; this has **not** been verified for the Final build. If the previous image
  refuses the schema, restore the pre-deploy dump (step 3) — history written since the deploy is lost and must be reconciled from the audit records.
- **Spend stop:** turn the provider switch off (no real model call; queued AI jobs stay queued by design), scale workers to zero.

## 6. Not verified for the Final build
No deployment on any cloud; no staging; no load, latency or availability measurement; no previous-image-on-new-schema check; no production budget configuration; no human
acceptance of AI quality. A deploy decision weighs these explicitly.
