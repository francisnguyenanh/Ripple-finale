"""LIVE-LOCAL mode: a persistent local instance for a real-provider run. NOTHING here is simulated and nothing is seeded.

  python -m tools.judge_local.live_local status                  # what exists, is the server armed (reads only)
  python -m tools.judge_local.live_local serve                   # starts the web server + ONE bounded worker (disarmed unless an approval file validates)
  python -m tools.judge_local.live_local import-sources --tier T9C # imports the proposed Harbor files through the production routes (no provider call)
  python -m tools.judge_local.live_local set-password <account>  # issues a new password, printed once, to the console only
  python -m tools.judge_local.live_local reset --confirm <db name>   # EXPLICIT, destructive: drops the live-local database and blob folder

Differences from `run_local` (the rehearsal): the database and the blob folder PERSIST across restarts (nothing is dropped on start); no authored
candidates, stub AI output or simulated approval is created; extraction, interpretation, drafting, answers and explanations use the configured
provider client unchanged; the provider is REFUSED until the operator arms the server (below). Credentials, the approval file, the budget
config and the data live OUTSIDE the repository (`RIPPLE_LIVE_LOCAL_HOME`, default `~/<private-identifier>/live_local`); no password is stored in
the repository or a handover: a password is printed once when the account is created or re-issued.

ARMING (a provider call needs ALL of these; any missing one leaves the server disarmed, the project model policy at `deny`, and every
provider job refused before it is admitted - there is no fallback to a stub):
  1. `<home>/APPROVAL.json` written by the operator after the user approved a budget package (approval id, total cap, per-job cap, role -> model).
  2. `RIPPLE_BUDGET_CONFIG_PATH` naming a budget config whose total cap, per-job cap and role models EQUAL the approval (the ledger enforces it).
  3. `RIPPLE_LIVE_LOCAL_ARM=1`.
The 12.82 USD figure is not an approval and this module never reads it as one.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
from decimal import Decimal

REPO = pathlib.Path(__file__).resolve().parents[2]
HOME = pathlib.Path(os.environ.get("RIPPLE_LIVE_LOCAL_HOME") or (pathlib.Path.home() / "<private-identifier>" / "live_local"))
ADMIN_DSN = os.environ.get("RIPPLE_LIVE_LOCAL_ADMIN_DSN", "host=127.0.0.1 port=55831 dbname=postgres user=postgres password=localtest")
DB = os.environ.get("RIPPLE_LIVE_LOCAL_DB", "livelocal")
PROTECTED_PORTS = (8765, 8766, 8770)
TENANT, PROJECT, WS = "harbor-live", "harbor-live-p", "harbor-live-ws"
ACCOUNTS = (("live-admin", ("admin",)), ("live-requester", ("requester",)), ("live-approver", ("change_approver",)),
            ("live-reviewer", ("reviewer",)))
ROLES = ("extraction", "change.draft", "intent.interpret", "gap.answer", "impact.judge", "intent.explain", "simulation.analyze",
         # Final roles (one pinned model each; the approval file may name any subset)
         "knowledge.qa", "change.understand", "change.impact_deps", "change.draft_guidance", "knowledge.gap_proposals", "gap.answer_interpret", "retrieval.semantic")
_NAME = re.compile(r"livelocal[a-z0-9_]*")


class LiveLocalError(SystemExit):
    pass


def dsn() -> str:
    return ADMIN_DSN.replace("dbname=postgres", f"dbname={DB}")


def _loopback() -> None:
    host = [p.split("=", 1)[1] for p in ADMIN_DSN.split() if p.startswith("host=")]
    if not host or host[0] not in ("127.0.0.1", "localhost", "::1"):
        raise LiveLocalError("refusing: the live-local database must be on a loopback server")
    if not _NAME.fullmatch(DB):
        raise LiveLocalError("refusing: the live-local database name must match livelocal[a-z0-9_]*")


def blob_dir() -> pathlib.Path:
    return HOME / "blobs"


def ensure_database() -> bool:
    """Create the database if it is missing. NEVER drops or truncates. Returns True when it was created."""
    import psycopg2
    _loopback()
    conn = psycopg2.connect(ADMIN_DSN, connect_timeout=3)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB,))
        if cur.fetchone():
            return False
        cur.execute(f'CREATE DATABASE "{DB}"')
        return True
    finally:
        conn.close()


def services():
    from infra.adapters.v2_blobs_fs import FileBlobs
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    blobs = FileBlobs(blob_dir())
    store = PostgresV2Store(dsn(), blobs)
    store.migrate()
    ids = PostgresIdentityStore(dsn())
    ids.migrate()
    return store, ids, blobs


def provision(store, ids) -> dict:
    """Idempotent: the workspace and the three accounts are created only when missing. Returns {account: password} for NEW accounts only."""
    import psycopg2
    store.create_workspace(TENANT, PROJECT, WS)
    issued = {}
    with psycopg2.connect(dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT account_id FROM v2_accounts WHERE account_id = ANY(%s)", ([a for a, _ in ACCOUNTS],))
        have = {r[0] for r in cur.fetchall()}
    for account, roles in ACCOUNTS:
        if account in have:
            _upgrade_viewer_to_reviewer(ids, account, roles)
            continue
        issued[account] = secrets.token_urlsafe(12)
        ids.create_account(account, issued[account])
        ids.grant_membership(TENANT, PROJECT, WS, account, roles)
    return issued


def _upgrade_viewer_to_reviewer(ids, account: str, roles: tuple) -> None:
    """The role model now has a real reviewer (read, comment, ask for changes, AI help; no approve/apply). An account created earlier as a
    read-only viewer for that purpose is moved to it once; nothing else about an existing account (password, history) is touched."""
    if roles != ("reviewer",):
        return
    import psycopg2
    with psycopg2.connect(dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT roles FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s AND account_id=%s", (TENANT, WS, account))
        row = cur.fetchone()
    if row is not None and list(row[0]) == ["viewer"]:
        ids.grant_membership(TENANT, PROJECT, WS, account, roles)


def read_approval(env=None) -> tuple[bool, str, dict | None]:
    """(armed, reason, approval). Disarmed is the safe default; the reason says which condition is missing."""
    env = os.environ if env is None else env
    if env.get("RIPPLE_LIVE_LOCAL_ARM") != "1":
        return False, "RIPPLE_LIVE_LOCAL_ARM=1 is not set", None
    path = HOME / "APPROVAL.json"
    if not path.is_file():
        return False, f"no approval file at {path.name} in the live-local home", None
    try:
        ap = json.loads(path.read_text(encoding="utf-8"))
        total, per_job = Decimal(str(ap["total_cap_usd"])), Decimal(str(ap["per_job_cap_usd"]))
        roles = dict(ap["roles"])
        if not str(ap["approval_id"]).strip() or total <= 0 or per_job <= 0 or set(roles) - set(ROLES):
            raise ValueError("invalid values")
        until = ap.get("valid_until")
        if until and datetime.date.fromisoformat(until) < datetime.date.today():
            return False, "the approval has expired", ap
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return False, f"the approval file is invalid ({type(exc).__name__})", None
    cfg_path = env.get("RIPPLE_BUDGET_CONFIG_PATH")
    if not cfg_path or not pathlib.Path(cfg_path).is_file():
        return False, "RIPPLE_BUDGET_CONFIG_PATH does not name a budget config", ap
    try:
        from infra.worker.job_quota import load_worker_budget_config      # the loader the worker itself uses
        cfg = load_worker_budget_config(cfg_path)
    except Exception as exc:                      # a broken config is a refusal, never a default
        return False, f"the budget config is invalid ({type(exc).__name__})", ap
    from infra.worker.budget_invariants import problems
    bad = problems(cfg)
    if bad:
        return False, "the budget config is unsafe: " + "; ".join(bad), ap
    if cfg.total_cap_usd != total or cfg.per_job_cap_usd != per_job:
        return False, "the budget config caps differ from the approval", ap
    for role, model in roles.items():
        if cfg.role_model_ids.get(role, cfg.default_model_id) != model:
            return False, f"the budget config model for {role} differs from the approval", ap
    return True, "armed", ap


def refuse_provider(*_a, **_k):
    raise RuntimeError("provider refused: this live-local server is not armed (no stub is used instead)")


def disarm_clients() -> list[str]:
    """Replace every provider client factory by a refusal. Used only while disarmed; there is NO stub behind it."""
    import importlib
    done = []
    for name in ("extraction", "gap_answer", "change_draft", "intent_interpret", "explain", "impact_judge", "live_analysis"):
        mod = importlib.import_module(f"infra.worker.{name}")
        if hasattr(mod, "_client_for_model"):
            setattr(mod, "_client_for_model", refuse_provider)
            done.append(name)
    return done


def record_start(mode: str) -> None:
    """One audit row per start with the declared mode: the portable-package export refuses a database without it (or with any non-live mode)."""
    import psycopg2
    from psycopg2.extras import Json
    with psycopg2.connect(dsn()) as cx, cx.cursor() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'live_local.start','operator',%s)",
                    (TENANT, WS, Json({"mode": mode})))


def apply_mode(store, armed: bool, reason: str) -> None:
    """Persisted model-data policy follows the arming state at every start; the provider mode label is what the UI shows."""
    store.set_project_model_policy(TENANT, PROJECT, external_provider="allow" if armed else "deny")
    os.environ["RIPPLE_PROVIDER_MODE"] = "LIVE" if armed else "LIVE_DISARMED"
    record_start("LIVE" if armed else "LIVE_DISARMED")
    if not armed:
        disarm_clients()
    os.environ["RIPPLE_UI_NOTICE"] = ("LIVE-LOCAL - the AI provider is REAL and every run costs money (click for details)" if armed
                                      else "LIVE-LOCAL, NOT ARMED - data is kept, no AI is called until the operator arms this server (click for details)")
    os.environ["RIPPLE_UI_NOTICE_DETAILS"] = (f"Persistent local instance. {'Armed: provider calls are charged against the approved budget and recorded per job.' if armed else 'Provider calls are refused: ' + reason + '.'} "
                                              "Nothing is simulated and nothing is seeded. Restarting keeps the data; a reset is an explicit operator command.")


def auto_disarm_after(store, minutes: float, stop: threading.Event) -> threading.Thread:
    """A timed arming: after `minutes` the provider is disarmed in-process (the same refusals as a disarmed start), the project model policy goes
    back to deny, and one audit row says so. Data, accounts, server and tunnel stay. A restart without the ARM variable gives the same state."""
    def loop():
        if stop.wait(minutes * 60):
            return
        disarm_clients()
        os.environ["RIPPLE_PROVIDER_MODE"] = "LIVE_DISARMED"
        os.environ["RIPPLE_UI_NOTICE"] = "LIVE-LOCAL, NOT ARMED - the timed arming ended; data is kept, no AI is called until the operator arms this server"
        try:
            store.set_project_model_policy(TENANT, PROJECT, external_provider="deny")
            import psycopg2
            from psycopg2.extras import Json
            with psycopg2.connect(dsn()) as cx, cx.cursor() as cur:
                cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'live_local.auto_disarm','operator',%s)",
                            (TENANT, WS, Json({"after_minutes": minutes})))
        except Exception as exc:                  # the in-process refusal above already holds; report, never re-arm
            print(f"auto-disarm: could not write the audit row ({type(exc).__name__})", flush=True)
        print("auto-disarm: the timed arming ended; the provider is OFF", flush=True)
    t = threading.Thread(target=loop, name="live-local-auto-disarm", daemon=True)
    t.start()
    return t


def start_worker(store, stop: threading.Event) -> threading.Thread:
    """ONE bounded in-process worker for the live-local workspace. It only claims jobs already admitted (a provider job needs a different
    person's approval first); a failure keeps the data and is recorded on the job, never replaced by a stub."""
    from infra.worker.job_worker import claim_and_admit_one

    def loop():
        wid = f"live-local-{secrets.token_hex(4)}"
        while not stop.is_set():
            try:
                claimed = claim_and_admit_one(store, TENANT, WS, wid)
            except Exception as exc:                    # keep the loop alive; the job record carries the reason
                print(f"worker: {type(exc).__name__} (job kept, see its record)", file=sys.stderr, flush=True)
                claimed = None
            if claimed is None:
                stop.wait(1.0)
    t = threading.Thread(target=loop, name="live-local-worker", daemon=True)
    t.start()
    return t


def configure_env(blobs) -> None:
    os.environ["RIPPLE_V2_DSN"] = dsn()
    os.environ["RIPPLE_V2_BLOB_BUCKET"] = "local-live-fs"            # placeholder: routes only check that a blob store is configured
    origin = os.environ.get("RIPPLE_LIVE_LOCAL_ORIGIN", "").strip()
    if origin:
        # ONE exact HTTPS hostname of a Cloudflare Quick Tunnel: no wildcard, no http, no path. The origin/CSRF guard then accepts only this origin.
        if not re.fullmatch(r"https://[a-z0-9][a-z0-9-]{3,62}\.trycloudflare\.com", origin):
            raise LiveLocalError("RIPPLE_LIVE_LOCAL_ORIGIN must be exactly https://<name>.trycloudflare.com (no wildcard, no path)")
        os.environ["RIPPLE_JUDGE_ORIGIN"] = origin
    else:
        os.environ.pop("RIPPLE_JUDGE_ORIGIN", None)
    from infra import v2_source_routes
    v2_source_routes._blob_port = lambda: blobs


def cmd_status(_a) -> int:
    _loopback()
    import psycopg2
    exists = False
    try:
        conn = psycopg2.connect(ADMIN_DSN, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (DB,))
        exists = bool(cur.fetchone())
        conn.close()
    except Exception as exc:
        print(f"postgres unreachable ({type(exc).__name__})")
        return 2
    armed, reason, ap = read_approval()
    print(f"home: {HOME}\ndatabase {DB}: {'exists' if exists else 'does not exist yet'}\nblob folder: {blob_dir()} ({'exists' if blob_dir().exists() else 'not created'})")
    print(f"armed: {armed} ({reason})" + (f"; approval id {ap.get('approval_id')}, total cap {ap.get('total_cap_usd')} USD" if ap else ""))
    return 0


def cmd_costs(_a) -> int:
    """READ-ONLY. What the ledger holds, split by what each attempt's cost rests on: `provider_usage` = both token sides reported by the provider
    (actual); `estimate_*` = a side was missing and was priced at its ceiling (an ESTIMATE, never a confirmed provider cost); `legacy_*` = attempts
    written before the basis was recorded (classified by whether the provider reported any tokens). Nothing is changed."""
    _loopback()
    import psycopg2
    with psycopg2.connect(dsn(), connect_timeout=5) as cx, cx.cursor() as cur:
        cur.execute("""SELECT coalesce(request_context->>'cost_basis',
                          CASE WHEN input_tokens IS NULL AND output_tokens IS NULL AND provider_calls > 0 THEN 'legacy_estimate_no_usage' ELSE 'legacy_provider_usage' END) AS basis,
                          count(*), coalesce(sum(provider_calls),0), coalesce(sum(cost_usd),0)
                       FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND provider_calls > 0 GROUP BY 1 ORDER BY 1""", (TENANT, WS))
        rows = cur.fetchall()
        cur.execute("SELECT entry_state, count(*), coalesce(sum(cost_usd),0), coalesce(sum(reserved_usd),0) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s GROUP BY 1 ORDER BY 1", (TENANT, WS))
        ledger = cur.fetchall()
    print("attempts that made provider calls, by cost basis:")
    actual = estimated = 0
    for basis, n, calls, usd in rows:
        print(f"  {basis:28} attempts={n:4} calls={calls:4} cost_usd={usd}")
        if basis.endswith("provider_usage"):
            actual += usd
        else:
            estimated += usd
    print(f"actual (provider-reported usage): {actual} USD | estimated (priced at the ceiling, no usage reported): {estimated} USD | sum of attempts: {actual + estimated} USD")
    for state, n, usd, reserved in ledger:
        print(f"ledger {state}: rows={n} cost_usd={usd} reserved_usd={reserved}")
    return 0


def cmd_serve(a) -> int:
    port = a.port or int(os.environ.get("RIPPLE_LIVE_LOCAL_PORT", "0"))
    if not port or port in PROTECTED_PORTS:
        raise LiveLocalError("choose a free port with --port (never 8765, 8766 or 8770)")
    created = ensure_database()
    store, ids, blobs = services()
    issued = provision(store, ids)
    armed, reason, ap = read_approval()
    configure_env(blobs)
    apply_mode(store, armed, reason)
    import uvicorn
    from infra import judge
    stop = threading.Event()
    start_worker(store, stop)
    minutes = os.environ.get("RIPPLE_LIVE_LOCAL_ARM_MINUTES", "").strip()
    if armed and minutes:
        auto_disarm_after(store, float(minutes), stop)
    server = uvicorn.Server(uvicorn.Config(judge.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    print("=" * 78)
    print(f"RIPPLE LIVE-LOCAL - persistent; {'ARMED (real provider, real cost)' if armed else 'NOT ARMED (no provider call: ' + reason + ')'}")
    if armed and minutes:
        print(f"timed arming: the provider turns itself OFF after {minutes} minutes")
    print(f"database {DB} {'created now' if created else 'kept as it was'}; blobs in {blob_dir()}; nothing is dropped on start")
    print(f"Open http://127.0.0.1:{port}/app-next/   workspace {WS}")
    for acct, pw in issued.items():
        print(f"  NEW account {acct:16s} password {pw}   (printed once; use set-password to re-issue)")
    if not issued:
        print("  accounts already exist: passwords are unchanged")
    print("Ctrl+C stops the server; the data stays.")
    print("=" * 78, flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


def cmd_import(a) -> int:
    from fastapi.testclient import TestClient
    from tools.judge_local.live_sources import build_manifest
    from tools.judge_local import harbor_inventory as inv
    ensure_database()
    store, ids, blobs = services()
    provision(store, ids)                       # accounts missing here are created; the password is printed once
    password = os.environ.get("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD")
    if not password:
        raise LiveLocalError("set RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD to the live-admin password (it is not printed or stored)")
    configure_env(blobs)
    from infra import judge
    man = build_manifest(a.tier)
    root = inv.DEFAULT
    files = [{"path": f["path"], "content_b64": base64.b64encode((root / f["path"].split("/", 1)[1]).read_bytes()).decode(),
              **({"source_role": f["source_role"]} if f.get("source_role") not in (None, "unknown") else {})} for f in man["files"]]
    for f, spec in zip(man["files"], files):
        if "sha256:" + hashlib.sha256(base64.b64decode(spec["content_b64"])).hexdigest() != f["sha256"]:
            raise LiveLocalError("a Harbor file changed since the manifest was built")
    origin = os.environ.get("RIPPLE_JUDGE_ORIGIN") or "https://judge.test"       # the configured tunnel origin when there is one (the guard accepts only that)
    c = TestClient(judge.app, base_url=origin)
    c.headers.update({"Origin": origin, "X-Ripple-Judge": "1"})
    r = c.post("/v2/auth/login", json={"account_id": "live-admin", "credential": password})
    if r.status_code != 200:
        raise LiveLocalError(f"login failed (HTTP {r.status_code})")
    r = c.post("/v2/sources/imports", json={"workspace_id": WS, "idempotency_key": secrets.token_hex(8), "files": files})
    if r.status_code != 200:
        raise LiveLocalError(f"import refused (HTTP {r.status_code}): {r.text[:300]}")
    dto = r.json()
    version = 0
    r = c.post(f"/v2/sources/imports/{dto['import_id']}/commit", json={"workspace_id": WS, "expected_version": version, "acknowledged_skips": []})
    if r.status_code != 200:
        raise LiveLocalError(f"commit refused (HTTP {r.status_code}): {r.text[:300]}")
    from infra.worker.job_worker import claim_and_admit_one
    claim_and_admit_one(store, TENANT, WS, "live-local-import")        # the source_ingest job (parsing only; no provider)
    head = store.head(TENANT, WS)
    print(f"imported {len(files)} files ({a.tier}); source revision {head.source_revision_id}; no provider call was made; "
          f"{man['expected_initial_extraction_jobs']} extraction jobs are EXPECTED later (each needs a different person's approval and an armed server)")
    return 0


def cmd_set_password(a) -> int:
    ensure_database()
    store, ids, _ = services()
    if a.account not in dict(ACCOUNTS):
        raise LiveLocalError("unknown live-local account")
    import psycopg2
    from infra.adapters.v2_identity import _digest
    pw = secrets.token_urlsafe(12)
    with psycopg2.connect(dsn()) as cx, cx.cursor() as cur:
        cur.execute("UPDATE v2_accounts SET credential_digest=%s, disabled_at=NULL WHERE account_id=%s", (_digest(pw), a.account))
        cur.execute("UPDATE v2_auth_sessions SET revoked_at=now() WHERE account_id=%s AND revoked_at IS NULL", (a.account,))
    print(f"new password for {a.account}: {pw}   (printed once; earlier sessions are signed out)")
    return 0


TRIAL_LABEL = "live_trial_20261008"


def cmd_authorize_trial(a) -> int:
    """Durable, append-only job ceilings for the 2026-10-08 live trial (no provider call, no spend): at most 2 `sim.analysis` jobs in total and
    ONE more extraction job than exist now (every state counts, so a failed job still used its authorization; replays and restarts cannot
    reopen it). Rows cannot be updated or deleted; the smallest ceiling applies. Idempotent: an existing label is left as it is."""
    _loopback()
    import psycopg2
    ddl = (pathlib.Path(__file__).resolve().parents[2] / "infra" / "migrations" / "r3_job_ceilings.sql").read_text(encoding="utf-8")
    with psycopg2.connect(dsn(), connect_timeout=5) as cx, cx.cursor() as cur:
        cur.execute(ddl)                     # the same additive, idempotent file the server applies at start (CREATE ... IF NOT EXISTS)
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'", (TENANT, WS))
        extraction_now = cur.fetchone()[0]
        for kind, n in (("sim.analysis", 2), ("extraction", extraction_now + 1)):
            cur.execute("INSERT INTO v2_job_ceilings (tenant_id, workspace_id, kind, label, max_total) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (TENANT, WS, kind, TRIAL_LABEL, n))
        cur.execute("SELECT kind, label, max_total FROM v2_job_ceilings WHERE tenant_id=%s AND workspace_id=%s ORDER BY kind, label", (TENANT, WS))
        for row in cur.fetchall():
            print("ceiling:", row)
    return 0


def cmd_authorize_allocation(a) -> int:
    """Append a NEW person-approved allocation (job counts per kind) as window rows. It never edits or deletes an older row: for each kind named,
    the newest window row replaces the older ones and counts only the jobs queued from now on. Requires `--approval-ref` (where the person's
    approval can be found) and `--kind kind=N` (repeat). Runs no provider call and spends nothing."""
    _loopback()
    import psycopg2
    if not a.approval_ref.strip() or not a.kind:
        raise LiveLocalError("refusing: --approval-ref and at least one --kind kind=N are required")
    wanted = []
    for item in a.kind:
        kind, _, n = item.partition("=")
        if not kind or not n.isdigit():
            raise LiveLocalError(f"refusing: {item!r} is not kind=N")
        wanted.append((kind, int(n)))
    label = "alloc_" + a.label.strip()
    ddl = (pathlib.Path(__file__).resolve().parents[2] / "infra" / "migrations" / "r3_job_ceilings.sql").read_text(encoding="utf-8")
    with psycopg2.connect(dsn(), connect_timeout=5) as cx, cx.cursor() as cur:
        cur.execute(ddl)
        cur.execute("SELECT coalesce(max(id),0) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind='JOB_QUEUED'", (TENANT, WS))
        mark = cur.fetchone()[0]
        for kind, n in wanted:
            cur.execute("INSERT INTO v2_job_ceilings (tenant_id, workspace_id, kind, label, max_total, window_after) VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (TENANT, WS, kind, label, n, mark))
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'live_local.allocation','operator',%s)",
                    (TENANT, WS, json.dumps({"label": label, "approval_ref": a.approval_ref[:300], "window_after": mark, "jobs": dict(wanted)})))
        cur.execute("SELECT kind, label, max_total, window_after FROM v2_job_ceilings WHERE tenant_id=%s AND workspace_id=%s ORDER BY kind, created_at", (TENANT, WS))
        for row in cur.fetchall():
            print("ceiling:", row)
    return 0


def cmd_reset(a) -> int:
    """EXPLICIT and SCOPED: drops exactly the named live-local database and deletes exactly the blob folder under the live-local home."""
    _loopback()
    if a.confirm != DB:
        raise LiveLocalError(f"refusing: pass --confirm {DB} to drop database {DB} and the blob folder {blob_dir()}")
    import shutil
    import psycopg2
    conn = psycopg2.connect(ADMIN_DSN, connect_timeout=3)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (DB,))
    cur.execute(f'DROP DATABASE IF EXISTS "{DB}"')
    conn.close()
    if blob_dir().exists() and blob_dir().parent == HOME:
        shutil.rmtree(blob_dir())
    print(f"reset done: database {DB} dropped and {blob_dir()} removed. Nothing else was touched.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("costs").set_defaults(fn=cmd_costs)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=0)
    s.set_defaults(fn=cmd_serve)
    i = sub.add_parser("import-sources")
    i.add_argument("--tier", choices=("T6", "T9", "T14", "T9C", "T9C+"), required=True)
    i.set_defaults(fn=cmd_import)
    p = sub.add_parser("set-password")
    p.add_argument("account")
    p.set_defaults(fn=cmd_set_password)
    sub.add_parser("authorize-trial").set_defaults(fn=cmd_authorize_trial)
    al = sub.add_parser("authorize-allocation")
    al.add_argument("--label", required=True)
    al.add_argument("--approval-ref", default="")
    al.add_argument("--kind", action="append", default=[])
    al.set_defaults(fn=cmd_authorize_allocation)
    r = sub.add_parser("reset")
    r.add_argument("--confirm", default="")
    r.set_defaults(fn=cmd_reset)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
