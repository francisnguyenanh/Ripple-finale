"""A new sign-in lasts one hour unless the operator sets RIPPLE_SESSION_LIFETIME_SECONDS (600..14400). The setting applies to sign-ins made after it; it cannot lengthen a session
that already exists and it does not skip authentication (a wrong credential still gets no session). Real route, real store."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from infra import judge, v2_auth
from tests.pitch_brief.chain import build


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _login(env, acct, cred):
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    return c.post("/v2/auth/login", json={"account_id": acct, "credential": cred})


def _seconds_left(env, acct) -> float:
    with env.store._tx() as cur:
        cur.execute("SELECT extract(epoch FROM (max(expires_at) - now())) FROM v2_auth_sessions WHERE account_id=%s", (acct,))
        return float(cur.fetchone()[0])


@pytest.mark.parametrize("value,expected", [(None, 3600), ("7200", 7200), ("14400", 14400), ("14401", 3600), ("599", 3600), ("abc", 3600), ("", 3600)])
def test_the_lifetime_is_bounded_and_defaults_to_one_hour(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("RIPPLE_SESSION_LIFETIME_SECONDS", raising=False)
    else:
        monkeypatch.setenv("RIPPLE_SESSION_LIFETIME_SECONDS", value)
    assert v2_auth.session_lifetime_seconds() == expected


def test_a_new_sign_in_follows_the_setting_and_a_wrong_credential_gets_nothing(env, ch, monkeypatch):
    monkeypatch.delenv("RIPPLE_SESSION_LIFETIME_SECONDS", raising=False)
    r = _login(env, env.req, f"cred-r-{env.nonce}")
    assert r.status_code == 200 and 3500 < _seconds_left(env, env.req) <= 3600
    monkeypatch.setenv("RIPPLE_SESSION_LIFETIME_SECONDS", "10800")
    r = _login(env, env.admin, f"cred-a-{env.nonce}")
    assert r.status_code == 200 and 10700 < _seconds_left(env, env.admin) <= 10800
    assert "Max-Age=10800" in r.headers.get("set-cookie", "")
    bad = _login(env, env.req, "wrong-credential")
    assert bad.status_code == 401 and "set-cookie" not in bad.headers
