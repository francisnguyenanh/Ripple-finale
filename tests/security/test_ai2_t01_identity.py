"""Identity acceptance through the public v2 route and real PostgreSQL.

The private provisioning calls below are deliberately outside HTTP.  They
establish identities and memberships before a browser can authenticate.
"""

from __future__ import annotations

import os
import hashlib
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters.v2_postgres import PostgresV2Store
from infra.adapters.v2_identity import IdentityDenied


DSN = os.environ.get(
    "AI2_T01_TEST_DSN",
    "host=127.0.0.1 port=55434 dbname=ai1t04 user=postgres password=localtest",
)


class MemoryBlobs:
    def __init__(self):
        self.data = {}

    def put_immutable(self, digest, content):
        self.data.setdefault(digest, content)

    def read(self, digest):
        return self.data[digest]


@pytest.fixture
def setup(monkeypatch):
    try:
        with psycopg2.connect(DSN + " connect_timeout=2"):
            pass
    except psycopg2.OperationalError:
        pytest.skip("AI2-T01 needs disposable PostgreSQL; set AI2_T01_TEST_DSN")

    from infra.adapters.v2_identity import PostgresIdentityStore

    nonce = uuid4().hex
    tenant, project, other_project = f"tenant-{nonce}", f"project-{nonce}", f"other-{nonce}"
    workspace, foreign_workspace = f"workspace-{nonce}", f"foreign-{nonce}"
    admin, a, b, c = (f"{name}-{nonce}" for name in ("admin", "requester", "owner", "outsider"))
    credentials = {name: f"cred-{name}-{nonce}-distinct" for name in (admin, a, b, c)}

    storage = PostgresV2Store(DSN, MemoryBlobs())
    storage.migrate()
    storage.create_workspace(tenant, project, workspace)
    storage.create_workspace(tenant, other_project, foreign_workspace)
    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, project, workspace, admin, credentials[admin])
    for account in (a, b, c):
        identities.create_account(account, credentials[account])
    identities.grant_membership(tenant, project, workspace, a, ("requester",))
    identities.grant_membership(tenant, project, workspace, b, ("knowledge_owner", "change_approver"))
    identities.grant_membership(tenant, other_project, foreign_workspace, c, ("requester",))

    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)
    def browser():
        client = TestClient(judge.app, base_url="https://judge.test")
        client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        return client
    return locals()


def login(client, account, credential):
    response = client.post("/v2/auth/login", json={"account_id": account, "credential": credential})
    assert response.status_code == 200, response.text
    cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie and "Secure" in cookie
    return response


def test_v2_auth_requires_real_individual_credentials_and_resolves_principal(setup):
    a, b, credentials, browser = (setup[key] for key in ("a", "b", "credentials", "browser"))
    first, second = browser(), browser()
    assert first.get("/v2/auth/me").status_code == 401
    assert first.post("/v2/auth/login", json={"account_id": a, "credential": credentials[b]}).status_code == 401
    login(first, a, credentials[a])
    login(second, b, credentials[b])
    assert first.get("/v2/auth/me").json()["account_id"] == a
    assert second.get("/v2/auth/me").json()["account_id"] == b


def test_two_independent_sessions_share_the_same_durable_workspace_head(setup):
    a, b, tenant, workspace, credentials, browser, storage = (
        setup[key] for key in ("a", "b", "tenant", "workspace", "credentials", "browser", "storage")
    )
    first, second = browser(), browser()
    login(first, a, credentials[a])
    login(second, b, credentials[b])
    for client in (first, second):
        selected = client.post("/v2/workspaces/select", json={"workspace_id": workspace})
        assert selected.status_code == 200, selected.text
    digest = storage.stage_blob(b"shared artifact")
    storage.publish_source(tenant, workspace, "shared-revision", {"artifact": digest},
                           expected_version=0, operation="source.import.commit", idempotency_key=uuid4().hex)
    for client in (first, second):
        response = client.get(f"/v2/workspaces/{workspace}/head")
        assert response.status_code == 200, response.text
        assert response.json()["source_revision_id"] == "shared-revision"


def test_other_project_cannot_select_or_read_even_with_guessed_ids(setup):
    c, credentials, browser, workspace, foreign_workspace = (
        setup[key] for key in ("c", "credentials", "browser", "workspace", "foreign_workspace")
    )
    client = browser()
    login(client, c, credentials[c])
    listing = client.get("/v2/workspaces")
    assert listing.status_code == 200, listing.text
    assert foreign_workspace in str(listing.json())
    assert workspace not in str(listing.json())
    assert client.post("/v2/workspaces/select", json={"workspace_id": workspace}).status_code in (403, 404)
    assert client.get(f"/v2/workspaces/{workspace}/head").status_code in (403, 404)


def test_client_supplied_actor_role_and_tenant_cannot_change_authority(setup):
    a, b, credentials, browser, tenant, workspace = (
        setup[key] for key in ("a", "b", "credentials", "browser", "tenant", "workspace")
    )
    client = browser()
    login(client, a, credentials[a])
    forged = {"workspace_id": workspace, "actor": b, "role": "admin", "tenant_id": tenant}
    response = client.post("/v2/workspaces/select", json=forged,
                           headers={"X-Actor": b, "X-Role": "admin", "X-Tenant-ID": tenant})
    assert response.status_code in (200, 400, 403), response.text
    me = client.get("/v2/auth/me").json()
    assert me["account_id"] == a
    assert "admin" not in str(me.get("roles", []))


def test_logout_revokes_browser_session_and_private_bootstrap_is_not_public(setup):
    a, credentials, browser = (setup[key] for key in ("a", "credentials", "browser"))
    client = browser()
    login(client, a, credentials[a])
    assert client.post("/v2/auth/logout").status_code == 200
    assert client.get("/v2/auth/me").status_code == 401
    for path in ("/v2/auth/bootstrap", "/v2/bootstrap-admin"):
        assert client.post(path, json={"account_id": "attacker", "role": "admin"}).status_code in (404, 405)


def test_expired_credential_cannot_log_in(setup):
    identities, browser = (setup[key] for key in ("identities", "browser"))
    expired = "expired-" + uuid4().hex
    identities.create_account(expired, "expired-secret", credential_expires_at="2000-01-01T00:00:00Z")
    response = browser().post("/v2/auth/login", json={"account_id": expired, "credential": "expired-secret"})
    assert response.status_code == 401


def test_membership_revoke_blocks_existing_session_and_is_audited(setup):
    a, credentials, browser, identities, tenant, project, workspace = (
        setup[key] for key in ("a", "credentials", "browser", "identities", "tenant", "project", "workspace")
    )
    client = browser()
    login(client, a, credentials[a])
    assert client.get(f"/v2/workspaces/{workspace}/head").status_code == 200
    identities.revoke_membership(tenant, project, workspace, a)
    assert client.get(f"/v2/workspaces/{workspace}/head").status_code in (403, 404)
    events = identities.audit_events(tenant, workspace)
    assert any(event.get("event_type") == "membership.grant" and event.get("account_id") == a for event in events)
    assert any(event.get("event_type") == "membership.revoke" and event.get("account_id") == a for event in events)


def test_membership_role_change_refreshes_existing_session_and_policy_version(setup):
    a, credentials, browser, identities, tenant, project, workspace = (
        setup[key] for key in ("a", "credentials", "browser", "identities", "tenant", "project", "workspace")
    )
    client = browser()
    login(client, a, credentials[a])
    original = client.get("/v2/workspaces").json()["workspaces"][0]
    assert original["roles"] == ["requester"]
    identities.grant_membership(tenant, project, workspace, a, ("knowledge_owner",))
    refreshed = client.get("/v2/workspaces").json()["workspaces"][0]
    assert refreshed["roles"] == ["knowledge_owner"]
    assert refreshed["policy_version"] != original["policy_version"]
    current = identities.current_authority(a, tenant, workspace)
    assert current["roles"] == ["knowledge_owner"]
    assert current["policy_version"] == refreshed["policy_version"]


def test_expired_server_session_cannot_read_or_select_even_with_cookie(setup):
    a, credentials, browser, workspace = (
        setup[key] for key in ("a", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, a, credentials[a])
    token = client.cookies.get("ripple_v2")
    assert token and client.get(f"/v2/workspaces/{workspace}/head").status_code == 200
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE v2_auth_sessions SET expires_at=now()-interval '1 second' WHERE token_digest=%s",
                    (hashlib.sha256(token.encode()).hexdigest(),))
        assert cur.rowcount == 1
    assert client.get("/v2/auth/me").status_code == 401
    assert client.get(f"/v2/workspaces/{workspace}/head").status_code in (401, 403, 404)
    assert client.post("/v2/workspaces/select", json={"workspace_id": workspace}).status_code in (401, 403, 404)


def test_credential_revocation_invalidates_live_sessions_and_relogin(setup):
    a, credentials, browser, identities, workspace = (
        setup[key] for key in ("a", "credentials", "browser", "identities", "workspace")
    )
    client = browser()
    login(client, a, credentials[a])
    identities.revoke_credential(a)
    assert client.get("/v2/auth/me").status_code == 401
    assert client.get(f"/v2/workspaces/{workspace}/head").status_code in (401, 403, 404)
    assert browser().post("/v2/auth/login", json={"account_id": a,
                                                  "credential": credentials[a]}).status_code == 401


def test_current_authority_recheck_denies_disabled_account(setup):
    a, identities, tenant, workspace = (
        setup[key] for key in ("a", "identities", "tenant", "workspace")
    )
    assert "requester" in identities.current_authority(a, tenant, workspace)["roles"]
    identities.revoke_credential(a)
    with pytest.raises(IdentityDenied):
        identities.current_authority(a, tenant, workspace)


def test_current_authority_recheck_denies_expired_credential(setup):
    a, identities, tenant, workspace = (
        setup[key] for key in ("a", "identities", "tenant", "workspace")
    )
    assert "requester" in identities.current_authority(a, tenant, workspace)["roles"]
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE v2_accounts SET credential_expires_at=now()-interval '1 second' WHERE account_id=%s",
                    (a,))
        assert cur.rowcount == 1
    with pytest.raises(IdentityDenied):
        identities.current_authority(a, tenant, workspace)


def test_same_workspace_id_in_two_tenants_is_ambiguous_not_first_match(setup):
    a, credentials, browser, identities, storage, workspace = (
        setup[key] for key in ("a", "credentials", "browser", "identities", "storage", "workspace")
    )
    other_tenant = "another-" + uuid4().hex
    storage.create_workspace(other_tenant, "project", workspace)
    identities.grant_membership(other_tenant, "project", workspace, a, ("requester",))
    client = browser()
    login(client, a, credentials[a])
    listing = client.get("/v2/workspaces")
    assert listing.status_code == 200
    assert sum(item["workspace_id"] == workspace for item in listing.json()["workspaces"]) == 2
    assert client.post("/v2/workspaces/select", json={"workspace_id": workspace}).status_code in (403, 404)
    assert client.get(f"/v2/workspaces/{workspace}/head").status_code in (403, 404)


def test_demo_accounts_have_four_distinct_server_issued_credentials(setup):
    identities, tenant, project, workspace, browser = (
        setup[key] for key in ("identities", "tenant", "project", "workspace", "browser")
    )
    issued = identities.provision_demo_accounts(tenant, project, workspace,
                                                prefix="demo-" + uuid4().hex)
    roles = {"requester", "knowledge_owner", "change_approver", "restore_operator"}
    assert set(issued) == roles
    assert len({issued[role]["account_id"] for role in roles}) == 4
    assert len({issued[role]["credential"] for role in roles}) == 4
    for role in roles:
        client = browser()
        login(client, issued[role]["account_id"], issued[role]["credential"])
        listed = client.get("/v2/workspaces").json()["workspaces"]
        assert len(listed) == 1 and listed[0]["roles"] == [role]
    requester, approver = issued["requester"], issued["change_approver"]
    assert browser().post("/v2/auth/login", json={"account_id": requester["account_id"],
        "credential": approver["credential"]}).status_code == 401


def test_private_bootstrap_is_one_time_and_failed_rebootstrap_is_atomic(setup):
    identities, tenant, project, workspace = (
        setup[key] for key in ("identities", "tenant", "project", "workspace")
    )
    impostor = "impostor-" + uuid4().hex
    with pytest.raises(IdentityDenied):
        identities.bootstrap_admin(tenant, project, workspace, impostor, "impostor-secret")
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_accounts WHERE account_id=%s", (impostor,))
        assert cur.fetchone()[0] == 0
