"""FB-A1 (local build): a verified external identity token becomes an ordinary Ripple session ONLY for a subject a person linked to an account. MOCK verifier (`local-mock` mode);
nothing here proves a real provider's behaviour. What this proves: no auto-join (unlinked subject refused), wrong project/tenant/expired/stale tokens refused with one generic
answer, authorization stays with the SQL membership (the exchanged session sees exactly the linked account's rights), link collisions refused, revocation stops future exchanges
without touching sessions or memberships, mode `off` answers 503, links need `membership.manage`, and no token appears in the audit.
"""
from __future__ import annotations

import json
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters import v2_external_identity as ext
from infra.adapters.v2_identity import PostgresIdentityStore

PROJECT = "ripple-local-demo"
ISS = ext.ISSUER_PREFIX + PROJECT


def sub(env, n=1):
    return f"ext-sub-{n}-{env.nonce}"                                                                                     # subjects are global per provider: unique per test environment


def token(sub="ext-sub-1", iss=ISS, aud=PROJECT, tenant=None, exp=None, auth_time=None):
    claims = {"iss": iss, "aud": aud, "sub": sub, "exp": exp or int(time.time()) + 600, "auth_time": auth_time or int(time.time())}
    if tenant:
        claims["tenant"] = tenant
    return "mock:" + json.dumps(claims)


def anon():
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    return c


def exchange(c, id_token):
    return c.post("/v2/auth/firebase/exchange", json={"id_token": id_token})


def link(client, env, sub, account_id, **extra):
    return client.post("/v2/auth/external/links", json={"workspace_id": env.ws, "issuer": ISS, "project_id": PROJECT, "subject": sub, "account_id": account_id, **extra})


@pytest.fixture
def fb_env(env, monkeypatch):
    monkeypatch.setenv("RIPPLE_FIREBASE_MODE", "local-mock")
    monkeypatch.setenv("RIPPLE_FIREBASE_PROJECT_ID", PROJECT)
    monkeypatch.delenv("RIPPLE_FIREBASE_TENANT", raising=False)
    return env


def test_only_a_linked_subject_signs_in_and_the_session_carries_the_sql_rights_of_that_account(fb_env):
    env = fb_env
    admin = env.admin_client()
    cfg = anon().get("/v2/auth/firebase/config").json()
    assert cfg["mode"] == "local-mock" and cfg["project_id"] == PROJECT and "credential" not in json.dumps(cfg).lower()
    assert exchange(anon(), token(sub(env))).status_code == 401                                                           # verified but unlinked: no auto-join
    assert env.req_client().post("/v2/auth/external/links", json={"workspace_id": env.ws, "issuer": ISS, "project_id": PROJECT, "subject": "s", "account_id": env.req}).status_code == 403
    assert link(admin, env, sub(env), "ghost-account").status_code == 409
    r = link(admin, env, sub(env), env.req)
    assert r.status_code == 200 and r.json()["linked"] is True
    assert link(admin, env, sub(env), env.req).json() == {"linked": False, "account_id": env.req, "reason": "already_linked"}
    assert link(admin, env, sub(env), env.admin).status_code == 409                                                       # collision: one subject, one account
    c = anon()
    r = exchange(c, token(sub(env)))
    assert r.status_code == 200 and r.json()["account_id"] == env.req and "ripple_v2" in r.cookies
    me = c.get("/v2/auth/me")
    assert me.status_code == 200 and me.json()["account_id"] == env.req
    assert c.get("/v2/business/gaps", params={"workspace_id": env.ws}).status_code in (200, 404)                          # a requester may read
    assert c.post("/v2/membership/grant", json={"workspace_id": env.ws, "account_id": "x", "roles": ["requester"]}).status_code == 403   # ...but holds no admin right from the token
    listed = admin.get("/v2/auth/external/links", params={"workspace_id": env.ws}).json()["links"]
    assert len(listed) == 1 and listed[0]["account_id"] == env.req and listed[0]["subject_hash"].startswith("sha256:") and sub(env) not in json.dumps(listed)
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT event_type, payload::text FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type LIKE 'external_identity.%%' ORDER BY id", (env.tenant, env.ws))
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["external_identity.linked", "external_identity.exchanged"] and all("mock:" not in r[1] and sub(env) not in r[1] for r in rows)


@pytest.mark.parametrize("make", [
    lambda s: token(s, iss=ext.ISSUER_PREFIX + "other-project", aud="other-project"),   # wrong project
    lambda s: token(s, aud="other-project"),                                               # audience mismatch
    lambda s: token(s, exp=int(time.time()) - 5),                                          # expired
    lambda s: token(s, auth_time=int(time.time()) - 3600),                                 # stale sign-in (fresh auth required for the exchange)
    lambda s: "not-a-mock-token",
    lambda s: "",
])
def test_wrong_project_expired_stale_or_malformed_tokens_are_refused_with_one_generic_answer(fb_env, make):
    env = fb_env
    assert link(env.admin_client(), env, sub(env), env.req).status_code == 200
    r = exchange(anon(), make(sub(env)))
    assert r.status_code == 401 and r.json() == {"error": "sign-in refused"}
    assert exchange(anon(), token(sub(env))).status_code == 200                                                            # the good token of the same subject still works


def test_revocation_disable_tenant_and_mode_off(fb_env, monkeypatch):
    env = fb_env
    admin = env.admin_client()
    assert link(admin, env, sub(env, 2), env.req).status_code == 200
    c = anon()
    assert exchange(c, token(sub(env, 2))).status_code == 200
    r = admin.post("/v2/auth/external/links/revoke", json={"workspace_id": env.ws, "issuer": ISS, "project_id": PROJECT, "subject": sub(env, 2)})
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert exchange(anon(), token(sub(env, 2))).status_code == 401                                                       # no future exchange
    assert c.get("/v2/auth/me").status_code == 200                                                                        # the existing session is not touched by the link revocation
    assert admin.post("/v2/auth/external/links/revoke", json={"workspace_id": env.ws, "issuer": ISS, "project_id": PROJECT, "subject": sub(env, 2)}).json()["revoked"] is False
    assert link(admin, env, sub(env, 3), env.req).status_code == 200
    PostgresIdentityStore(env.dsn).revoke_credential(env.req)                                                              # a disabled/expired account cannot be entered through the side door
    assert exchange(anon(), token(sub(env, 3))).status_code == 401
    monkeypatch.setenv("RIPPLE_FIREBASE_TENANT", "tenant-a")
    assert exchange(anon(), token(sub(env, 3))).status_code == 401                                                       # wrong identity tenant (none in the token)
    monkeypatch.setenv("RIPPLE_FIREBASE_MODE", "off")
    assert exchange(anon(), token(sub(env, 3))).status_code == 503 and anon().get("/v2/auth/firebase/config").json()["project_id"] is None
    monkeypatch.setenv("RIPPLE_FIREBASE_MODE", "admin-sdk")
    r = exchange(anon(), token(sub(env, 3)))
    assert r.status_code in (401, 503)                                                                                    # the real adapter never falls back to the mock: without the SDK 503, with it a real verification
