"""Multi-process / multi-instance safety of the sandbox: every guarantee rests on SHARED database state (advisory locks,
audit-event lease chain, outbox fencing, atomic budget reservation), not on a process-local lock. Plus the budget arithmetic
and the limits that will be deployed. Stub provider only."""
from __future__ import annotations

import json
import threading

import psycopg2
import pytest

from tests.pitch_brief.chain import APPROVAL, build
from tests.pitch_brief.conftest import (DSN, REAL_BUDGET_PATH, SANDBOX_LIMITS_PATH, REPO, create_job, sandbox_config)


@pytest.fixture
def sb(env, monkeypatch):
    ch = build(env, monkeypatch)

    def configure(**over):
        monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev, **over))
    configure()
    ch.configure = configure
    return ch


def _client_for(acct, cred):
    from fastapi.testclient import TestClient
    from infra import judge
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": cred}).status_code == 200
    return c


def test_concurrent_lease_acquire_has_exactly_one_winner(env, sb):
    from tests.pitch_brief.test_ui_flow import _account
    acct, cred = _account(env, "requester")
    clients = [sb.admin, _client_for(acct, cred)]
    results = []

    def go(c):
        results.append(c.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire"}).status_code)
    threads = [threading.Thread(target=go, args=(c,)) for c in clients for _ in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    # four racing requests from two accounts: the holder may re-acquire its own lease, the other account never wins
    assert results.count(409) >= 1 and results.count(200) >= 1
    st = sb.admin.get("/v2/sandbox/status", params={"workspace_id": env.ws}).json()
    holder = st["lease"]["holder"]
    assert holder in (env.admin, acct)
    other = clients[1] if holder == env.admin else clients[0]
    assert other.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire"}).status_code == 409


def test_pump_is_exclusive_across_instances_through_the_database(env, sb):
    assert sb.admin.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire"}).status_code == 200
    other_instance = psycopg2.connect(DSN)           # stands in for a pump running in ANOTHER process/instance
    other_instance.autocommit = True
    cur = other_instance.cursor()
    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"sandbox-pump:{env.tenant}:{env.ws}",))
    assert cur.fetchone()[0] is True
    try:
        r = sb.admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws})
        assert r.status_code == 202 and r.json()["busy"] is True and "another instance" in r.json()["message"]
    finally:
        other_instance.close()                       # releases the session lock
    r = sb.admin.post("/v2/sandbox/pump", json={"workspace_id": env.ws})
    assert r.status_code == 200 and r.json()["busy"] is False


def test_provider_job_limit_per_lease_counts_model_jobs_only(env, sb):
    sb.configure(max_provider_jobs_per_lease=2)
    assert sb.admin.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": "acquire",
                                                    "participants": [env.req]}).status_code == 200
    req = env.req_client()
    for _ in range(2):
        jid, r = create_job(req, env, "extraction", {"artifact_id": sb.arts[APPROVAL], "source_role": "unknown"})
        assert r.status_code == 200, r.text
        assert sb.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
        from tests.pitch_brief.conftest import drain
        drain(env)
    jid, r = create_job(req, env, "extraction", {"artifact_id": sb.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 429 and r.json()["code"] == "sandbox_provider_job_limit"
    ids = ["x"]
    assert create_job(req, env, "relation", {"candidate_ids": ids})[1].status_code == 200   # deterministic jobs are not model jobs
    st = sb.admin.get("/v2/sandbox/status", params={"workspace_id": env.ws}).json()
    assert st["jobs"]["provider_used_in_lease"] == 2 and st["jobs"]["provider_limit_per_lease"] == 2


def test_budget_admission_is_the_worst_case_reservation_and_atomic_in_the_database(env, monkeypatch, tmp_path):
    """The exact, instance-independent allowance: the candidate's total cap = settled-at-baseline + approved allowance.
    Every job reserves the worst case (2 attempts x worst call = 0.25632 USD) INSIDE the creating transaction under the
    project lock, so two instances cannot both pass the last slot. Here: cap = baseline + 1.5 reservations."""
    from decimal import Decimal
    from infra.worker import job_quota
    ch = build(env, monkeypatch)
    cfg_real = job_quota.load_worker_budget_config()
    estimate, total_cap = job_quota.compute_budget_reservation(cfg_real)     # (estimate, cap) the store reserves against
    reservation = Decimal(str(estimate))
    assert Decimal(str(total_cap)) == Decimal("12.82")
    assert reservation == Decimal("0.25632")          # 2 attempts x (24000 x 0.0015/1k + 12288 x 0.0075/1k) = 2 x 0.12816
    with env.store._tx() as cur:
        from infra.adapters.v2_postgres import PROJECT_SPEND_SQL, project_spend_params
        cur.execute(PROJECT_SPEND_SQL, project_spend_params(env.tenant, env.ws))
        baseline = Decimal(str(cur.fetchone()[0]))
    cap = (baseline + reservation * Decimal("1.5")).quantize(Decimal("0.00001"))
    text = REAL_BUDGET_PATH.read_text(encoding="utf-8").replace('total_cap_usd: "12.82"', f'total_cap_usd: "{cap}"')
    assert str(cap) in text
    small = tmp_path / "cap.yaml"
    small.write_text(text, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(small))
    req = env.req_client()
    j1, r1 = create_job(req, env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r1.status_code == 200, r1.text                                    # baseline + R <= cap
    j2, r2 = create_job(req, env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r2.status_code != 200 and "budget" in r2.text.lower(), (r2.status_code, r2.text)   # baseline + 2R > cap: refused
    # the refusal is about the RESERVATION, not about what was really spent: once the first job settles at its real
    # (tiny, stubbed) cost, the next job fits again
    assert env.admin_client().post(f"/v2/jobs/{j1}/approve", json={"workspace_id": env.ws}).status_code == 200
    from tests.pitch_brief.conftest import drain
    drain(env)
    j3, r3 = create_job(req, env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r3.status_code == 200, r3.text


def test_limits_are_one_file_used_by_tests_launcher_and_the_report(env):
    limits = {k: v for k, v in json.loads(SANDBOX_LIMITS_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    cfg = json.loads(sandbox_config("w", "seed"))["workspaces"]["w"]
    assert {k: cfg[k] for k in limits} == limits and cfg["seed_revision_id"] == "seed"
    from infra.worker import sandbox_hold
    assert all(sandbox_hold.DEFAULTS[k] == v for k, v in limits.items())      # code defaults == the file: no silent drift
    launcher = (REPO / "tools" / "judge_local" / "run_local.py").read_text(encoding="utf-8")
    assert "C.sandbox_config(" in launcher and "max_jobs_per_lease" not in launcher and "REAL_BUDGET_PATH" in launcher
    proposal = REPO / "plan" / "ai_closed_loop" / "product_focus" / "JUDGE_BUDGET_AMENDMENT_PROPOSAL.md"
    if not proposal.exists():
        pytest.skip("the internal budget-amendment proposal (plan/) is not part of the release package; the limits-file checks above ran")
    report = proposal.read_text(encoding="utf-8")
    for k in ("max_jobs_per_lease", "max_provider_jobs_per_lease", "pump_max_jobs", "pump_seconds", "lease_minutes"):
        assert f"{k} = {limits[k]}" in report, k
    assert f"min_remaining_usd = {limits['min_remaining_usd']}" in report
