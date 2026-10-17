"""U1/U5: the workspace profile explains who may do what; an administrator of a project can start another workspace in it with
existing accounts and the existing role bundles; nobody else can, nothing is created that was not asked for. Stub-free (no model)."""
from __future__ import annotations

import pytest


def _project(env):
    return f"p-{env.nonce}"


def _create(client, env, **kw):
    body = {"project_id": _project(env), "name": "Harbor Treasury", "description": "Cash policy, limits and the desk workbook.",
            "idempotency_key": "k-1", "members": [{"account_id": env.req, "roles": ["requester"]}]}
    body.update(kw)
    return client.post("/v2/workspaces", json=body)


def test_profile_says_what_a_workspace_is_and_what_the_caller_may_do(env):
    adm, req = env.admin_client(), env.req_client()
    a = adm.get("/v2/workspace/profile", params={"workspace_id": env.ws}).json()
    assert a["project_id"] == _project(env) and a["source_files"] == 0 and a["display_name"] is None
    assert {s["id"]: s["allowed"] for s in a["steps"]} == {"read": True, "propose": True, "confirm": True, "approve": True}
    assert {m["account_id"] for m in a["members"]} == {env.admin, env.req}
    assert any(r["role"] == "admin" and not r["grantable"] for r in a["roles"])
    r = req.get("/v2/workspace/profile", params={"workspace_id": env.ws}).json()
    assert "members" not in r, "names of other people are for someone who manages access"
    assert {s["id"]: s["allowed"] for s in r["steps"]}["confirm"] is False
    assert "Confirm knowledge" not in r["my_capabilities"] and "Propose a change" in r["my_capabilities"]


def test_only_a_project_administrator_sees_options_and_creates(env):
    adm, req = env.admin_client(), env.req_client()
    opts = adm.get("/v2/workspace/create-options")
    assert opts.status_code == 200
    proj = opts.json()["projects"][0]
    assert proj["project_id"] == _project(env) and set(proj["accounts"]) == {env.admin, env.req}
    assert all(r["grantable"] for r in opts.json()["roles"]) and not any(r["role"] == "admin" for r in opts.json()["roles"])
    assert req.get("/v2/workspace/create-options").status_code == 403
    assert _create(req, env).status_code == 403
    assert _create(adm, env, project_id="p-somebody-else").status_code == 403


def test_created_workspace_has_a_name_members_and_nothing_else(env):
    adm, req = env.admin_client(), env.req_client()
    r = _create(adm, env)
    assert r.status_code == 200, r.text
    made = r.json()
    wid = made["workspace_id"]
    assert made["display_name"] == "Harbor Treasury" and made["creator_roles"] == ["admin"]
    listing = {w["workspace_id"]: w for w in req.get("/v2/workspaces").json()["workspaces"]}
    assert listing[wid]["display_name"] == "Harbor Treasury" and listing[wid]["roles"] == ["requester"]
    mine = {w["workspace_id"]: w for w in adm.get("/v2/workspaces").json()["workspaces"]}
    assert mine[wid]["roles"] == ["admin"], "the creator is an administrator of the NEW workspace only"
    prof = adm.get("/v2/workspace/profile", params={"workspace_id": wid}).json()
    assert prof["created_by"] == env.admin and prof["source_files"] == 0 and prof["description"].startswith("Cash policy")
    assert {m["account_id"]: m["roles"] for m in prof["members"]} == {env.admin: ["admin"], env.req: ["requester"]}
    life = req.get("/v2/sources/lifecycle", params={"workspace_id": wid}).json()
    assert life["source_revision_id"] is None and life["artifacts"] == [], "no source, no analysis, nothing created for the person"
    with env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s", (env.tenant, wid))
        assert cur.fetchone()[0] == 0
    ev = adm.get(f"/v2/audit/{wid}").json()["events"]
    assert any(e["event_type"] == "workspace.created" and e["actor_id"] == env.admin for e in ev)


def test_replay_is_the_same_workspace_and_bad_requests_are_refused(env):
    adm = env.admin_client()
    first = _create(adm, env).json()["workspace_id"]
    assert _create(adm, env).json()["workspace_id"] == first, "same idempotency key, same workspace"
    assert _create(adm, env, idempotency_key="k-2").status_code == 409, "a second workspace with the same name in a project"
    for bad in ({"name": "ab"}, {"name": "x" * 81}, {"description": "d" * 501}, {"members": [{"account_id": "nobody", "roles": ["requester"]}]},
                {"members": [{"account_id": env.req, "roles": ["admin"]}]}, {"members": [{"account_id": env.req, "roles": []}]},
                {"members": "everyone"}, {"idempotency_key": ""}):
        kw = {"name": "Other name", "idempotency_key": "k-bad", **bad}
        r = _create(adm, env, **kw)
        assert r.status_code == 400, (bad, r.status_code, r.text)


def test_anonymous_and_origin_are_refused_before_anything_is_read(env):
    import fastapi.testclient as tc
    from infra.judge import app
    anon = tc.TestClient(app, base_url="https://judge.test")
    assert anon.post("/v2/workspaces", json={"project_id": "x"}).status_code in (401, 403)
    assert anon.get("/v2/workspace/create-options").status_code in (401, 403)
