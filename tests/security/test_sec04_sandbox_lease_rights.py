"""SEC-04 (2026-10-31): a sandbox lease is the right to write in the sandbox for a while, so it is not handed to any signed-in member: a viewer
(read-only roles) is refused the slot with 403 and still reads; a requester (a writing role) acquires and releases it; with the operator allowlist
`RIPPLE_SANDBOX_LEASE_ACCOUNTS` set, only the listed accounts acquire, even a requester outside the list is refused. Real routes, real PostgreSQL."""
from __future__ import annotations

from fastapi.testclient import TestClient

from infra import judge
from infra.adapters.v2_identity import PostgresIdentityStore
from tests.pitch_brief.chain import build
from tests.pitch_brief.conftest import env, sandbox_config  # noqa: F401  (env is a fixture)


def _member(env, name, roles, cred="cv"):
    ids = PostgresIdentityStore(env.dsn)
    ids.create_account(name, cred)
    ids.grant_membership(env.tenant, f"p-{env.nonce}", env.ws, name, tuple(roles))
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": name, "credential": cred}).status_code == 200
    return c


def _lease(client, env, action):
    return client.post("/v2/sandbox/lease", json={"workspace_id": env.ws, "action": action, "participants": []})


def test_a_viewer_cannot_take_the_sandbox_slot_a_requester_can_and_the_allowlist_narrows_it(env, monkeypatch):
    ch = build(env, monkeypatch)
    monkeypatch.setenv("RIPPLE_SANDBOX_CONFIG", sandbox_config(env.ws, ch.rev))
    monkeypatch.delenv("RIPPLE_SANDBOX_LEASE_ACCOUNTS", raising=False)
    viewer = _member(env, f"viewer-sec04-{env.nonce}", ("viewer",))
    requester = _member(env, f"req-sec04-{env.nonce}", ("requester",))
    r = _lease(viewer, env, "acquire")
    assert r.status_code == 403 and "writing role" in r.text, r.text
    assert _lease(viewer, env, "renew").status_code == 403
    assert viewer.get("/v2/sandbox/status", params={"workspace_id": env.ws}).status_code == 200      # reading never needs a lease
    r = _lease(requester, env, "acquire")
    assert r.status_code == 200, r.text
    assert _lease(viewer, env, "acquire").status_code == 403                                           # still refused, not 409: the role answers first
    assert _lease(requester, env, "release").status_code == 200
    # the operator allowlist narrows the holders further: a requester outside it is refused, an account inside it acquires
    monkeypatch.setenv("RIPPLE_SANDBOX_LEASE_ACCOUNTS", f"someone-else-{env.nonce}")
    assert _lease(requester, env, "acquire").status_code == 403
    listed = _member(env, f"listed-sec04-{env.nonce}", ("viewer",))
    monkeypatch.setenv("RIPPLE_SANDBOX_LEASE_ACCOUNTS", f"listed-sec04-{env.nonce}, someone-else")
    assert _lease(listed, env, "acquire").status_code == 200                                          # the allowlist is the operator's explicit choice
    assert _lease(listed, env, "release").status_code == 200
