"""SEC-02 through the real route and the shared PostgreSQL counters (`RIPPLE_RATE_SHARED=1`): the three layers over HTTP, the ingress contract,
no account-existence leak, bounded storage under 20 concurrent writers at the ceiling, a throttled request that never reaches the credential
check, database failure answered 429, two independent app instances (two module states) sharing the lock through the database."""
from __future__ import annotations

import importlib
import os
import threading
import time
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import login_protection as lp

DSN = os.environ.get("AI2_T02_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs a disposable PostgreSQL (AI2_T02_TEST_DSN)")


@pytest.fixture
def env(monkeypatch):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("test database not reachable")
    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    monkeypatch.setenv("RIPPLE_RATE_SHARED", "1")
    monkeypatch.setenv("RIPPLE_TRUSTED_PROXY_HOPS", "1")          # the test client sets the forwarded header itself (one trusted hop)
    monkeypatch.delenv("RIPPLE_LOGIN_MAX_ROWS", raising=False)
    from infra import judge, v2_auth, v2_http_guard
    from infra.adapters.v2_identity import PostgresIdentityStore
    from infra.adapters.v2_postgres import PostgresV2Store
    PostgresV2Store(DSN, None).migrate()
    ids = PostgresIdentityStore(DSN)
    ids.migrate()
    nonce = uuid4().hex[:10]
    acct, cred = f"u-{nonce}", f"cred-{nonce}-good"
    ids.create_account(acct, cred)
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("DELETE FROM v2_login_failures")
    v2_auth._THROTTLE.reset()
    v2_http_guard._login_ready = False
    client = TestClient(judge.app, base_url="https://judge.test")
    client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    return {"client": client, "acct": acct, "cred": cred, "ids": ids, "v2_auth": v2_auth}


def _login(client, acct, cred, source):
    return client.post("/v2/auth/login", json={"account_id": acct, "credential": cred}, headers={"X-Forwarded-For": source})


def _rows(where="", params=()):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT key, window_start, failures, locked_until FROM v2_login_failures " + where, params)
        return cur.fetchall()


def test_l1_one_source_locks_only_its_pair_the_right_credential_from_another_source_passes(env):
    c, acct, cred = env["client"], env["acct"], env["cred"]
    for _ in range(5):
        assert _login(c, acct, "wrong", "203.0.113.9").status_code == 401
    r = _login(c, acct, cred, "203.0.113.9")
    assert r.status_code == 429 and r.json() == {"error": "too many attempts"} and r.headers.get("Retry-After")
    assert _login(c, acct, cred, "198.51.100.10").status_code == 200, "a judge on another address signs in while the attacker's pair is locked"
    assert _login(c, "someone-else", "wrong", "203.0.113.9").status_code == 401, "the attacker's source is not locked by five failures"


def test_l2_source_spray_locks_the_source_only(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_LOGIN_SOURCE_MAX_FAILURES", "10")
    c, acct, cred = env["client"], env["acct"], env["cred"]
    for i in range(10):
        assert _login(c, f"guess-{i}", "wrong", "203.0.113.20").status_code == 401
    assert _login(c, acct, cred, "203.0.113.20").status_code == 429
    assert _login(c, acct, cred, "203.0.113.21").status_code == 200


def test_l3_distributed_guessing_locks_the_account_briefly_then_the_owner_signs_in(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_LOGIN_ACCOUNT_GLOBAL_MAX_FAILURES", "12")
    monkeypatch.setenv("RIPPLE_LOGIN_ACCOUNT_GLOBAL_LOCKOUT_SECONDS", "2")
    c, acct, cred = env["client"], env["acct"], env["cred"]
    for i in range(12):
        assert _login(c, acct, "wrong", f"10.9.{i}.1").status_code == 401          # 4 pairs x 3 failures: below L1, above L3 together
    r = _login(c, acct, cred, "10.9.99.1")
    assert r.status_code == 429, "the account is locked from every source"
    time.sleep(2.2)
    assert _login(c, acct, cred, "10.9.99.1").status_code == 200, "after the short lock the owner signs in"
    assert _login(c, acct, cred, "10.9.99.1").status_code == 200, "no re-lock without new failures inside the same window"


def test_no_existence_leak_and_the_same_answer_for_a_missing_proposal_of_the_user(env):
    c = env["client"]
    a = _login(c, env["acct"], "wrong", "203.0.113.30")
    b = _login(c, "no-such-account", "wrong", "203.0.113.30")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, {"error": "invalid credentials"})
    for _ in range(5):
        _login(c, "no-such-account", "wrong", "203.0.113.31")
    for _ in range(5):
        _login(c, env["acct"], "wrong", "203.0.113.32")
    x = _login(c, "no-such-account", "wrong", "203.0.113.31")
    y = _login(c, env["acct"], "wrong", "203.0.113.32")
    assert (x.status_code, x.json()) == (y.status_code, y.json()) == (429, {"error": "too many attempts"})


def test_ingress_contract_over_http(env, monkeypatch):
    c, acct = env["client"], env["acct"]
    monkeypatch.setenv("RIPPLE_TRUSTED_PROXY_HOPS", "0")
    for i in range(5):
        _login(c, acct, "wrong", f"1.1.1.{i}")                                     # five spoofed addresses: without a trusted proxy they are one peer
    assert _login(c, acct, env["cred"], "1.1.1.99").status_code == 429
    env["v2_auth"]._THROTTLE.reset()
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("DELETE FROM v2_login_failures")
    monkeypatch.setenv("RIPPLE_TRUSTED_PROXY_HOPS", "1")
    assert c.post("/v2/auth/login", json={"account_id": acct, "credential": "wrong"}).status_code == 401
    assert any(k.startswith("s:untrusted") for k, *_ in _rows()), "a request without the forwarded header behind a proxy counts as untrusted"
    _login(c, acct, "wrong", "not an address")
    assert any(k == "s:invalid" for k, *_ in _rows())
    _login(c, acct, "wrong", "2001:db8::7")
    assert any(k == "as:" + acct + "|2001:db8::7" for k, *_ in _rows()), "an IPv6 source keys normally"


def test_throttled_requests_never_reach_the_credential_check_or_grow_the_counters(env, monkeypatch):
    c, acct = env["client"], env["acct"]
    from infra import v2_auth
    checks = []
    real_store = v2_auth._store
    store = real_store()
    real_login = store.login

    def counting_login(*a, **k):
        checks.append(1)
        return real_login(*a, **k)

    monkeypatch.setattr(store, "login", counting_login)
    monkeypatch.setattr(v2_auth, "_store", lambda: store)
    for _ in range(5):
        _login(c, acct, "wrong", "203.0.113.40")
    before = _rows("WHERE key LIKE %s", ("as:" + acct + "|203.0.113.40",))
    results = []

    def attempt():
        results.append(_login(c, acct, "wrong", "203.0.113.40").status_code)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results.count(429) == 40 and len(checks) == 5, "after the lock no request reached the credential check"
    assert _rows("WHERE key LIKE %s", ("as:" + acct + "|203.0.113.40",)) == before, "a refused attempt changes no counter"


def test_storage_bounds_per_source_and_the_system_ceiling_under_concurrent_writers(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_LOGIN_SOURCE_MAX_FAILURES", "20")
    c = env["client"]
    for i in range(60):
        _login(c, f"acct-{i}", "wrong", "203.0.113.50")
    rows = _rows("WHERE key LIKE %s OR key = %s OR key LIKE %s", ("as:%|203.0.113.50", "s:203.0.113.50", "a:acct-%"))
    assert len(rows) <= 2 * 20 + 1, f"one source writes at most 2 x source_max + 1 rows per window, got {len(rows)}"
    monkeypatch.setenv("RIPPLE_LOGIN_MAX_ROWS", "1000")
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("DELETE FROM v2_login_failures")
    results = []

    def writer(n):
        for j in range(50):
            results.append(_login(c, f"w{n}-{j}", "wrong", f"10.20.{n}.1").status_code)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    total = len(_rows())
    assert total <= 1000, f"the system ceiling holds under 20 concurrent writers over 1000 distinct keys: {total} rows"
    assert 429 in results, "above the ceiling a new key is refused rather than counted"
    assert len(results) == 1000


def test_database_failure_answers_429_not_500(env, monkeypatch):
    c, acct = env["client"], env["acct"]
    from infra import v2_http_guard
    monkeypatch.setattr(v2_http_guard, "login_wait", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    r = _login(c, acct, env["cred"], "203.0.113.60")
    assert r.status_code == 429 and r.headers.get("Retry-After") == "15"


def test_two_independent_app_instances_share_the_lock_through_the_database(env, monkeypatch):
    """Two module states (the in-process throttle of each instance is separate) agree through `v2_login_failures`."""
    c1, acct, cred = env["client"], env["acct"], env["cred"]
    from infra import v2_auth
    other_throttle = v2_auth.LoginThrottle()
    for _ in range(3):
        _login(c1, acct, "wrong", "203.0.113.70")
    monkeypatch.setattr(v2_auth, "_THROTTLE", other_throttle)                       # instance 2: an empty in-process state
    from infra import judge
    c2 = TestClient(judge.app, base_url="https://judge.test")
    c2.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    for _ in range(2):
        _login(c2, acct, "wrong", "203.0.113.70")
    assert _login(c2, acct, cred, "203.0.113.70").status_code == 429, "3 + 2 failures across two instances lock the pair"
    assert _login(c2, acct, cred, "203.0.113.71").status_code == 200
