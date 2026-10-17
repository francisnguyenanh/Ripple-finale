"""Centralized permission enforcement on top of the identity layer.

All routes go through the real, deployed `infra.judge.app`, with authority
always resolved server-side from PostgreSQL membership rows, never trusted
from the request body/headers. Uses a disposable PostgreSQL 16 server, same
as the identity tests.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg2
import pytest
from fastapi.testclient import TestClient

from infra import judge
from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store
from infra.worker.job_worker import claim_and_admit_one


DSN = os.environ.get(
    "AI2_T02_TEST_DSN",
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
        pytest.skip("AI2-T02 needs disposable PostgreSQL; set AI2_T02_TEST_DSN")

    nonce = uuid4().hex
    tenant, project, workspace = f"tenant-{nonce}", f"project-{nonce}", f"workspace-{nonce}"
    admin = f"admin-{nonce}"
    requester, owner, approver, restorer = (
        f"{name}-{nonce}" for name in ("requester", "owner", "approver", "restorer")
    )
    credentials = {name: f"cred-{name}-{nonce}-distinct"
                  for name in (admin, requester, owner, approver, restorer)}

    storage = PostgresV2Store(DSN, MemoryBlobs())
    storage.migrate()
    storage.create_workspace(tenant, project, workspace)
    digest = storage.stage_blob(b"seed artifact")
    storage.publish_source(tenant, workspace, "rev-0", {"artifact": digest},
                           expected_version=0, operation="source.import.commit",
                           idempotency_key=uuid4().hex)

    identities = PostgresIdentityStore(DSN)
    identities.migrate()
    identities.bootstrap_admin(tenant, project, workspace, admin, credentials[admin])
    for account in (requester, owner, approver, restorer):
        identities.create_account(account, credentials[account])
    identities.grant_membership(tenant, project, workspace, requester, ("requester",))
    identities.grant_membership(tenant, project, workspace, owner, ("knowledge_owner",))
    identities.grant_membership(tenant, project, workspace, approver, ("change_approver",))
    identities.grant_membership(tenant, project, workspace, restorer, ("restore_operator",))

    monkeypatch.setenv("RIPPLE_V2_DSN", DSN)

    def browser():
        client = TestClient(judge.app, base_url="https://judge.test")
        client.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
        return client

    return locals()


def login(client, account, credential):
    response = client.post("/v2/auth/login", json={"account_id": account, "credential": credential})
    assert response.status_code == 200, response.text
    return response


def create_job(client, workspace, kind, job_id, **extra):
    return client.post("/v2/jobs", json={"workspace_id": workspace, "kind": kind,
                                         "job_id": job_id, **extra})


def approve_job(client, workspace, job_id):
    return client.post(f"/v2/jobs/{job_id}/approve", json={"workspace_id": workspace})


def _run_job_to_completion(store, tenant, workspace, job_id, *, max_claims=10):
    """Drive job_id through the real worker claim path until its
    execution_status is COMPLETED -- production now requires a completed
    execution result before recheck_and_publish_job will publish."""
    for _ in range(max_claims):
        record = store.job_record(tenant, workspace, job_id)
        if record["execution_status"] == "COMPLETED":
            return
        claim = claim_and_admit_one(store, tenant, workspace, "worker-" + uuid4().hex)
        if claim is None:
            break
    record = store.job_record(tenant, workspace, job_id)
    assert record["execution_status"] == "COMPLETED", (
        f"test setup failed to drive job {job_id} to COMPLETED execution "
        f"(stuck at execution_status={record['execution_status']!r})"
    )


def publish_job(client, workspace, job_id):
    return client.post(f"/v2/jobs/{job_id}/publish", json={"workspace_id": workspace})


# ---------------------------------------------------------------------------
# Deny tests, one per permission
# ---------------------------------------------------------------------------

def test_requester_without_change_approve_cannot_approve(setup):
    requester, approver, credentials, browser, workspace, credentials = (
        setup[key] for key in ("requester", "approver", "credentials", "browser", "workspace", "credentials")
    )
    client = browser()
    login(client, requester, credentials[requester])
    created = create_job(client, workspace, "change", f"job-{uuid4().hex}")
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    denied = approve_job(client, workspace, job_id)
    assert denied.status_code == 403, denied.text


def test_answerer_without_knowledge_confirm_cannot_confirm(setup):
    requester, credentials, browser, workspace = (
        setup[key] for key in ("requester", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, requester, credentials[requester])
    response = client.post("/v2/knowledge/confirm",
                           json={"workspace_id": workspace, "claim_id": "claim-1"})
    assert response.status_code == 403, response.text


def test_approver_without_source_restore_cannot_restore(setup):
    approver, credentials, browser, workspace = (
        setup[key] for key in ("approver", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, approver, credentials[approver])
    response = create_job(client, workspace, "restore", f"restore-{uuid4().hex}")
    assert response.status_code == 403, response.text


def test_knowledge_owner_cannot_create_change_job(setup):
    owner, credentials, browser, workspace = (
        setup[key] for key in ("owner", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, owner, credentials[owner])
    response = create_job(client, workspace, "change", f"job-{uuid4().hex}")
    assert response.status_code == 403, response.text


def test_no_session_denied_on_every_route(setup):
    workspace = setup["workspace"]
    client = setup["browser"]()
    assert create_job(client, workspace, "change", "x").status_code == 401
    assert approve_job(client, workspace, "x").status_code == 401
    assert publish_job(client, workspace, "x").status_code == 401
    assert client.post("/v2/knowledge/confirm",
                       json={"workspace_id": workspace, "claim_id": "c"}).status_code == 401
    assert client.get(f"/v2/audit/{workspace}").status_code == 401


# ---------------------------------------------------------------------------
# Body/header forgery cannot substitute for real authority
# ---------------------------------------------------------------------------

def test_forged_actor_or_role_in_body_or_headers_cannot_bypass_permission(setup):
    requester, credentials, browser, workspace = (
        setup[key] for key in ("requester", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, requester, credentials[requester])
    forged_body = {"workspace_id": workspace, "kind": "change", "job_id": f"job-{uuid4().hex}",
                  "actor_id": "someone-else", "role": "admin", "roles": ["admin"],
                  "tenant_id": "other-tenant"}
    response = client.post("/v2/jobs", json=forged_body,
                           headers={"X-Actor": "admin", "X-Role": "admin", "X-Tenant-ID": "other-tenant"})
    # A change job is allowed to be created (requester holds intent.create);
    # what matters is that it is attributed to the REAL account, not the
    # forged one, and that forging a role does not grant approve permission.
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    status = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.json()["requested_by"] == requester
    denied = client.post(f"/v2/jobs/{job_id}/approve",
                         json={"workspace_id": workspace, "actor_id": "admin", "role": "admin"},
                         headers={"X-Role": "admin"})
    assert denied.status_code == 403, denied.text


def test_guessed_workspace_id_in_other_tenant_does_not_grant_authority(setup):
    requester, credentials, browser = (
        setup[key] for key in ("requester", "credentials", "browser")
    )
    client = browser()
    login(client, requester, credentials[requester])
    response = create_job(client, "some-foreign-workspace-id", "change", f"job-{uuid4().hex}")
    assert response.status_code in (401, 403, 404)


# ---------------------------------------------------------------------------
# Self-approval policy, both modes
# ---------------------------------------------------------------------------

def test_self_approval_denied_when_distinct_approver_required(setup):
    identities, admin, credentials, browser, tenant, project, workspace = (
        setup[key] for key in ("identities", "admin", "credentials", "browser", "tenant", "project", "workspace")
    )
    both = f"both-{uuid4().hex}"
    identities.create_account(both, "both-secret-distinct")
    identities.grant_membership(tenant, project, workspace, both, ("requester", "change_approver"))
    client = browser()
    login(client, both, "both-secret-distinct")
    created = create_job(client, workspace, "change", f"job-{uuid4().hex}")
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    denied = approve_job(client, workspace, job_id)
    assert denied.status_code == 403, denied.text


def test_self_approval_allowed_when_policy_relaxed_but_is_audited(setup):
    identities, storage, admin, tenant, project, workspace = (
        setup[key] for key in ("identities", "storage", "admin", "tenant", "project", "workspace")
    )
    storage.set_workspace_policy(tenant, workspace, require_distinct_approver=False)
    both = f"both-{uuid4().hex}"
    identities.create_account(both, "both-secret-distinct-2")
    identities.grant_membership(tenant, project, workspace, both, ("requester", "change_approver"))
    client = setup["browser"]()
    login(client, both, "both-secret-distinct-2")
    created = create_job(client, workspace, "change", f"job-{uuid4().hex}")
    job_id = created.json()["job_id"]
    approved = approve_job(client, workspace, job_id)
    assert approved.status_code == 200, approved.text
    audit = client.get(f"/v2/audit/{workspace}")
    assert audit.status_code == 200, audit.text
    events = audit.json()["events"]
    assert any(event.get("event_type") == "job.self_approval" for event in events)


# ---------------------------------------------------------------------------
# Revoke while a job is pending/approved blocks publish
# ---------------------------------------------------------------------------

def test_revoking_approver_after_approval_blocks_publish(setup):
    identities, requester, approver, credentials, tenant, project, workspace = (
        setup[key] for key in ("identities", "requester", "approver", "credentials", "tenant", "project", "workspace")
    )
    requester_client = setup["browser"]()
    login(requester_client, requester, credentials[requester])
    created = create_job(requester_client, workspace, "change", f"job-{uuid4().hex}")
    job_id = created.json()["job_id"]

    approver_client = setup["browser"]()
    login(approver_client, approver, credentials[approver])
    approved = approve_job(approver_client, workspace, job_id)
    assert approved.status_code == 200, approved.text

    identities.revoke_membership(tenant, project, workspace, approver)

    denied = publish_job(requester_client, workspace, job_id)
    assert denied.status_code == 409, denied.text
    status = requester_client.get(f"/v2/jobs/{job_id}", params={"workspace_id": workspace})
    assert status.json()["status"] == "APPROVED"  # not silently PUBLISHED


def test_approved_job_publishes_when_approver_authority_still_current(setup):
    requester, approver, credentials, workspace = (
        setup[key] for key in ("requester", "approver", "credentials", "workspace")
    )
    requester_client = setup["browser"]()
    login(requester_client, requester, credentials[requester])
    created = create_job(requester_client, workspace, "change", f"job-{uuid4().hex}")
    job_id = created.json()["job_id"]

    approver_client = setup["browser"]()
    login(approver_client, approver, credentials[approver])
    assert approve_job(approver_client, workspace, job_id).status_code == 200

    _run_job_to_completion(setup["storage"], setup["tenant"], workspace, job_id)

    published = publish_job(requester_client, workspace, job_id)
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED"


# ---------------------------------------------------------------------------
# Human-only decision check
# ---------------------------------------------------------------------------

def test_knowledge_confirm_requires_human_actor(setup, monkeypatch):
    owner, credentials, browser, workspace = (
        setup[key] for key in ("owner", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, owner, credentials[owner])

    from infra.adapters import v2_identity

    original = v2_identity.PostgresIdentityStore.resolve_context

    def as_model(self, token, workspace_id=None):
        result = original(self, token, workspace_id)
        result["actor_kind"] = "model"
        return result

    monkeypatch.setattr(v2_identity.PostgresIdentityStore, "resolve_context", as_model)
    response = client.post("/v2/knowledge/confirm",
                           json={"workspace_id": workspace, "claim_id": "claim-2"})
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# membership.manage enforcement
# ---------------------------------------------------------------------------

def test_membership_manage_required_to_grant_or_revoke(setup):
    requester, credentials, browser, workspace = (
        setup[key] for key in ("requester", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, requester, credentials[requester])
    granted = client.post("/v2/membership/grant",
                          json={"workspace_id": workspace, "account_id": "new-account",
                               "roles": ["requester"]})
    assert granted.status_code == 403, granted.text
    revoked = client.post("/v2/membership/revoke",
                          json={"workspace_id": workspace, "account_id": requester})
    assert revoked.status_code == 403, revoked.text


def test_admin_can_grant_membership_but_not_admin_role_over_http(setup):
    admin, credentials, browser, workspace = (
        setup[key] for key in ("admin", "credentials", "browser", "workspace")
    )
    client = browser()
    login(client, admin, credentials[admin])
    new_account = f"new-{uuid4().hex}"
    setup["identities"].create_account(new_account, "new-secret-distinct")
    granted = client.post("/v2/membership/grant",
                          json={"workspace_id": workspace, "account_id": new_account,
                               "roles": ["requester"]})
    assert granted.status_code == 200, granted.text
    escalate = client.post("/v2/membership/grant",
                           json={"workspace_id": workspace, "account_id": new_account,
                                "roles": ["admin"]})
    assert escalate.status_code == 400, escalate.text


# ---------------------------------------------------------------------------
# UI capability map is not enforcement: a route with no server-side
# permission behind it must never be trusted for a mutating/restricted op.
# ---------------------------------------------------------------------------

def test_route_registry_matches_the_deployed_app(setup):
    from infra.adapters.v2_policy import ROUTE_PERMISSIONS

    def walk(routes):
        for route in routes:
            # Newer FastAPI/Starlette wraps an included APIRouter's routes
            # behind an opaque `_IncludedRouter`; unwrap via its
            # `original_router` so a mounted sub-router's routes are still
            # found by this audit instead of silently reporting none.
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from walk(nested.routes)
                continue
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", None)
            if path:
                for method in methods:
                    yield (method, path)

    registered = set(walk(judge.app.routes))
    for method, path in ROUTE_PERMISSIONS:
        assert (method, path) in registered, f"{method} {path} not registered on the deployed app"
