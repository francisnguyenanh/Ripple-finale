"""Operator driver for the live-local review instance: the FIRST real AI runs, one gate at a time, through the product's own routes (in-process client, same database).

It never prints, stores or logs a password: the live-admin and live-requester passwords come from the environment (RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD,
RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD) or an interactive prompt, and live only in this process. It creates no job by itself except the ones listed below, and every job
needs the second person's approval exactly as in the UI (requester creates, admin approves). The server (armed, with its one worker) executes them.

  python -m tools.judge_local.live_run canary-extraction   # ONE extraction job (the smallest file); judged by the per-role canary gate; NEXT steps stay closed until PASS
  python -m tools.judge_local.live_run extract-rest        # the other artifacts, one at a time, only after the extraction canary PASSED; stops at the first finding or refusal
  python -m tools.judge_local.live_run report              # read-only: canary per role, ledger (settled + reserved) against the allocation, artifacts analysed / not analysed

Gate state: <live-local home>/canary_state.json. A FAIL or a budget refusal halts admission (state HALT); nothing is retried to get a nicer result and nothing is stubbed.
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import secrets
import sys
import time
from decimal import Decimal

from tools.judge_local import canary as K
from tools.judge_local import live_local as L

STATE = L.HOME / "canary_state.json"
FIRST = "harbor_demo/config/approval.yaml"            # the smallest file: the first (canary) job is also real data, never repeated
POLL_SECONDS, JOB_TIMEOUT = 5, 900


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"roles": {}, "halt": None}


def _save(st: dict) -> None:
    L.HOME.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")


def _secret(env_name: str, who: str) -> str:
    v = os.environ.get(env_name)
    return v if v else getpass.getpass(f"{who} password (not echoed, not stored): ")


def _client(account: str, password: str):
    from fastapi.testclient import TestClient
    from infra import judge
    origin = os.environ.get("RIPPLE_JUDGE_ORIGIN") or "https://judge.test"
    c = TestClient(judge.app, base_url=origin)
    c.headers.update({"Origin": origin, "X-Ripple-Judge": "1"})
    r = c.post("/v2/auth/login", json={"account_id": account, "credential": password})
    if r.status_code != 200:
        raise L.LiveLocalError(f"login of {account} failed (HTTP {r.status_code})")
    return c


def _env():
    store, _ids, blobs = L.services()
    os.environ["RIPPLE_BUDGET_CONFIG_PATH"] = str(L.HOME / "budget_config.yaml")
    L.configure_env(blobs)
    return store


def _artifacts(store) -> dict:
    head = store.head(L.TENANT, L.WS).source_revision_id
    view = store.source_revision_view(L.TENANT, L.WS, head)
    return {a["logical_path"]: a for a in view["artifacts"]}


def _config():
    from infra.worker.job_quota import load_worker_budget_config
    return load_worker_budget_config(str(L.HOME / "budget_config.yaml"))


def _spend() -> tuple[Decimal, Decimal]:
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT coalesce(sum(CASE WHEN entry_state='SETTLED' THEN cost_usd ELSE 0 END),0), "
                    "coalesce(sum(CASE WHEN entry_state<>'SETTLED' THEN reserved_usd ELSE 0 END),0) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s", (L.TENANT, L.WS))
        a, b = cur.fetchone()
    return Decimal(a), Decimal(b)


def _server_armed() -> bool:
    """What the SERVER declared at its last start (the audit row written by `serve`), not what this shell's environment says."""
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT payload->>'mode' FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='live_local.start' ORDER BY id DESC LIMIT 1", (L.TENANT, L.WS))
        row = cur.fetchone()
    return bool(row) and row[0] == "LIVE"


def _canary(role: str) -> dict:
    attempts, ledgers = K.read(L.dsn(), L.TENANT, L.WS)
    return K.report(attempts, ledgers, _config(), [role])[role]


def _run_job(req, adm, artifact: dict, source_role: str) -> dict:
    job_id = f"job-extraction-{secrets.token_hex(5)}"
    r = req.post("/v2/jobs", json={"workspace_id": L.WS, "kind": "extraction", "job_id": job_id, "payload": {"artifact_id": artifact["artifact_id"], "source_role": source_role}})
    if r.status_code != 200:
        return {"job_id": job_id, "refused": f"HTTP {r.status_code}: {r.text[:200]}"}
    a = adm.post(f"/v2/jobs/{job_id}/approve", json={"workspace_id": L.WS})
    if a.status_code != 200:
        return {"job_id": job_id, "refused": f"approve HTTP {a.status_code}: {a.text[:200]}"}
    t0 = time.time()
    while time.time() - t0 < JOB_TIMEOUT:
        body = adm.get(f"/v2/jobs/{job_id}", params={"workspace_id": L.WS}).json()
        if body.get("execution_status") in ("COMPLETED", "FAILED", "CANCELLED", "REJECTED"):
            return {"job_id": job_id, "status": body.get("execution_status"), "reason": body.get("execution_reason")}
        time.sleep(POLL_SECONDS)
    return {"job_id": job_id, "status": "TIMEOUT"}


def _gate(role: str, st: dict) -> dict:
    res = _canary(role)
    st["roles"][role] = {**res, "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if res["status"] == "FAIL":
        st["halt"] = f"canary {role} FAILED: " + "; ".join(res["findings"])
    _save(st)
    return res


def cmd_canary(_a) -> int:
    store = _env()
    st = _state()
    if st.get("halt"):
        print("HALT in force:", st["halt"])
        return 3
    if not _server_armed():
        print("the server's last start was not LIVE (armed): nothing is created")
        return 2
    arts = _artifacts(store)
    if FIRST not in arts:
        raise L.LiveLocalError("the workspace holds no approval.yaml: import the sources first")
    req, adm = _client("live-requester", _secret("RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD", "live-requester")), _client("live-admin", _secret("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", "live-admin"))
    out = _run_job(req, adm, arts[FIRST], "unknown")
    print("job:", out)
    res = _gate("extraction", st)
    print("canary extraction:", res["status"], res["findings"])
    st["first_extraction_job"] = out["job_id"]
    _save(st)
    return 0 if res["status"] == "PASS" else 1


def cmd_rest(_a) -> int:
    store = _env()
    st = _state()
    if st.get("halt"):
        print("HALT in force:", st["halt"])
        return 3
    if st["roles"].get("extraction", {}).get("status") != "PASS":
        print("the extraction canary has not PASSED: nothing is admitted")
        return 2
    if not _server_armed():
        print("the server's last start was not LIVE (armed): nothing is created")
        return 2
    arts = _artifacts(store)
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT payload->>'artifact_id' FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED'", (L.TENANT, L.WS))
        done = {r[0] for r in cur.fetchall()}
    req, adm = _client("live-requester", _secret("RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD", "live-requester")), _client("live-admin", _secret("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", "live-admin"))
    for path in sorted(arts):
        a = arts[path]
        if a["artifact_id"] in done:
            continue
        out = _run_job(req, adm, a, "implementation" if "/app/" in path else "unknown")
        print(path, out)
        if out.get("refused") or out.get("status") != "COMPLETED":
            st["halt"] = f"extraction of {path} did not complete: {out}"
            _save(st)
            return 1
        attempts, ledgers = K.read(L.dsn(), L.TENANT, L.WS)
        bad = [f for r in K.report([x for x in attempts if x["job_id"] == out["job_id"]], ledgers, _config(), ["extraction"])["extraction"]["findings"]]
        if bad:
            st["halt"] = f"extraction of {path} shows a finding: {bad}"
            _save(st)
            print("HALT:", st["halt"])
            return 1
    print("all artifacts of the head revision were attempted; run `report`")
    return 0


def cmd_report(_a) -> int:
    store = _env()
    arts = _artifacts(store)
    settled, reserved = _spend()
    st = _state()
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT payload->>'artifact_id', execution_status FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'", (L.TENANT, L.WS))
        by = {}
        for aid, status in cur.fetchall():
            by.setdefault(aid, []).append(status)
    print(f"ledger: settled {settled} USD + reserved {reserved} USD; cap {_config().total_cap_usd} USD; headroom {_config().total_cap_usd - settled - reserved} USD")
    print("halt:", st.get("halt"))
    print("canary per role:", {k: v.get("status") for k, v in st.get("roles", {}).items()} or "none yet")
    for path in sorted(arts):
        s = by.get(arts[path]["artifact_id"], [])
        print(f"  {path}: {'analysed' if 'COMPLETED' in s else ('failed' if s else 'NOT analysed')}")
    return 0


def cmd_session(_a) -> int:
    """ONE sitting: the three passwords are typed (not echoed) into THIS console, then the whole approved sequence runs and is logged WITHOUT any credential:
    extraction canary -> the other artifacts -> the journey (free text ... query, explanation) -> report. Every decision step is demo-account automation, SIMULATED."""
    from tools.judge_local import live_journey as J
    logf = L.HOME / f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log(msg):
        line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    store = _env()
    if not _server_armed():
        log("the server is not armed: nothing is created")
        return 2
    req = _client("live-requester", _secret("RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD", "live-requester"))
    adm = _client("live-admin", _secret("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", "live-admin"))
    apv = _client("live-approver", _secret("RIPPLE_LIVE_LOCAL_APPROVER_PASSWORD", "live-approver"))
    log("signed in (credentials are not logged); log file: " + str(logf))
    st = _state()
    if st.get("halt"):
        log("HALT in force: " + st["halt"])
        return 3
    arts = _artifacts(store)
    if st["roles"].get("extraction", {}).get("status") != "PASS":
        out = _run_job(req, adm, arts[FIRST], "unknown")
        log(f"canary extraction job: {out}")
        res = _gate("extraction", st)
        log(f"canary extraction: {res['status']} {res['findings']}")
        if res["status"] != "PASS":
            return 1
    import psycopg2
    done = set()
    for path in sorted(arts):
        with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
            cur.execute("SELECT 1 FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED' AND payload->>'artifact_id'=%s", (L.TENANT, L.WS, arts[path]["artifact_id"]))
            if cur.fetchone():
                continue
        out = _run_job(req, adm, arts[path], "implementation" if "/app/" in path else "unknown")
        log(f"extraction {path}: {out}")
        if out.get("status") != "COMPLETED":
            st["halt"] = f"extraction of {path} did not complete: {out}"
            _save(st)
            log("HALT: " + st["halt"])
            return 1
        attempts, ledgers = K.read(L.dsn(), L.TENANT, L.WS)
        bad = K.report([x for x in attempts if x["job_id"] == out["job_id"]], ledgers, _config(), ["extraction"])["extraction"]["findings"]
        if bad:
            st["halt"] = f"extraction of {path} shows a finding: {bad}"
            _save(st)
            log("HALT: " + st["halt"])
            return 1
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED' ORDER BY job_id", (L.TENANT, L.WS))
        ids = [r[0] for r in cur.fetchall()]
    ctx = J.new_ctx(ws=L.WS, req=req, adm=adm, apv=apv, approval_path=FIRST, approval_artifact_id=arts[FIRST]["artifact_id"], extraction_ids=ids,
                    scope_artifact_ids=sorted(a["artifact_id"] for a in arts.values()), poll=5, log=log)
    try:
        out = J.run_journey(ctx)
        log("journey ids: " + json.dumps({k: v for k, v in out.items() if isinstance(v, str)}))
    except J.JourneyStop as exc:
        st["journey_stop"] = {"step": exc.step, "finding": exc.finding, "ids": exc.ids}
        _save(st)
        log(f"JOURNEY STOPPED at {exc.step}: {exc.finding} {exc.ids}")
    for role in ("intent.interpret", "intent.explain", "change.draft", "gap.answer"):
        r = _gate(role, _state())
        log(f"canary {role}: {r['status']} {r['findings']}")
    cmd_report(None)
    return 0


CODE_FILES = ("harbor_demo/app/routing.py", "harbor_demo/app/emergency.py", "harbor_demo/app/config_loader.py")


def cmd_followup(_a) -> int:
    """Two bounded follow-ups on the EXISTING data (no reset, no apply, no wave): (1) a Harbor simulation over the 240 public sample cases from the CURRENT configuration back to the
    earlier value (no provider call); (2) a second extraction of ONLY the three code files with the current prompt (a new snapshot: the earlier results stay stored and are listed next to it)."""
    from tools.judge_local import live_journey as J
    logf = L.HOME / f"followup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log(msg):
        line = f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    store = _env()
    if not _server_armed():
        log("the server is not armed: nothing is created")
        return 2
    st = _state()
    if st.get("halt") or st["roles"].get("extraction", {}).get("status") != "PASS":
        log("a HALT is in force or the extraction canary has not PASSED: nothing is created")
        return 3
    req = _client("live-requester", _secret("RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD", "live-requester"))
    adm = _client("live-admin", _secret("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", "live-admin"))
    arts = _artifacts(store)
    ctx = J.new_ctx(ws=L.WS, req=req, adm=adm, apv=None, poll=4, log=log)
    cfg = store.read_artifact_blob(L.TENANT, L.WS, store.head(L.TENANT, L.WS).source_revision_id, arts[FIRST]["artifact_id"])["content"].decode("utf-8")
    import re
    cur = int(re.search(r"standard_manager_threshold_jpy:\s*(\d+)", cfg).group(1))
    back = 1000000 if cur != 1000000 else 500000
    try:
        jid, res = J.make_job(ctx, req, "simulation", {"adapter": "harbor_cli", "case": "route", "edits": [{"path": J.TARGET_Y, "value": back, "expected_current": cur}]}, "simulation")
        h = res.get("harbor") or {}
        log(f"simulation {jid}: status {res.get('status')}, cases {res.get('coverage', {}).get('submitted')}, changed {h.get('changed_count')}, unexplained {h.get('unexplained_changes')} (current limit {cur} -> {back}; no provider call)")
    except J.JourneyStop as exc:
        log(f"SIMULATION STOPPED: {exc.finding}")
    new_ids = {}
    for path in CODE_FILES:
        out = _run_job(req, adm, arts[path], "implementation")
        log(f"code re-extraction {path}: {out}")
        new_ids[path] = out.get("job_id")
        if out.get("status") != "COMPLETED":
            st["halt"] = f"code re-extraction of {path} did not complete: {out}"
            _save(st)
            log("HALT: " + st["halt"])
            return 1
        attempts, ledgers = K.read(L.dsn(), L.TENANT, L.WS)
        bad = K.report([x for x in attempts if x["job_id"] == out["job_id"]], ledgers, _config(), ["extraction"])["extraction"]["findings"]
        if bad:
            st["halt"] = f"code re-extraction of {path} shows a finding: {bad}"
            _save(st)
            log("HALT: " + st["halt"])
            return 1
    st["code_snapshot_2"] = {"jobs": new_ids, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "reason": "extraction instruction for CODE now asks for decision conditions (comparison, operator, config attribute, outcome)"}
    _save(st)
    cmd_report(None)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("canary-extraction").set_defaults(fn=cmd_canary)
    sub.add_parser("extract-rest").set_defaults(fn=cmd_rest)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("session").set_defaults(fn=cmd_session)
    sub.add_parser("followup").set_defaults(fn=cmd_followup)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except L.LiveLocalError as exc:
        print("refused:", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
