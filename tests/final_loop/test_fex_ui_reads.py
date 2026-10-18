"""Extension ext/1 read hints for the interface (LOCAL ONLY, fixture repositories): the repository list a member may see, and per-action `can` hints on the
code candidate computed by the same checks the actions run. Neither is an authorisation: an action read as allowed is still refused when the state moved.
Real routes, worker, store; 0 provider calls apart from the STUB understanding of the change request."""
from __future__ import annotations

import json

from tests.final_loop.test_chg02_impact import confirmed
from tests.final_loop.test_fex_code_change import _committed, _config, _settle, fixture_repo  # noqa: F401  (fixture_repo is a fixture)
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_syn01_code import FREIGHT_REQ, OP_FREIGHT, code_env, login  # noqa: F401  (code_env is a fixture)


def _two(ws_id, repo, target, tmp):
    cfg = _config(ws_id, repo, target)
    fixture = cfg["workspaces"][ws_id]["repos"]["fixture"]
    fixture["display_name"] = "Harbor app (fixture)"
    cfg["workspaces"][ws_id]["repos"]["hidden"] = dict(fixture, display_name="Admin-only fixture", visible_to=["admin"])
    cfg["workspaces"][ws_id]["repos"]["remote"] = {"path": str(tmp / "mirror"), "allowlist": ["app/"],
                                                   "remote": {"provider": "github", "repository": "example-owner/secret-name", "branch": "main", "grants": ["pull_request"],
                                                              "token_env": "RIPPLE_TEST_TOKEN_VAR"}}
    return cfg


def test_repository_list_shows_only_what_the_member_may_see_and_never_a_location(code_env, fixture_repo, monkeypatch, tmp_path):
    q = code_env
    repo, target = fixture_repo
    empty = q.req.get("/v2/sources/repositories", params={"workspace_id": q.env.ws})
    assert empty.status_code == 200 and empty.json()["configured"] is False and empty.json()["repositories"] == []        # nothing declared: said, not an error
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_two(q.env.ws, repo, target, tmp_path)))
    mine = q.req.get("/v2/sources/repositories", params={"workspace_id": q.env.ws})
    assert mine.status_code == 200 and mine.headers.get("x-request-id")
    body = mine.json()
    ids = [r["repo_id"] for r in body["repositories"]]
    assert ids == ["fixture", "remote"], ids                                                                                  # the admin-only one is absent for the requester
    text = mine.text
    for leak in (str(repo), str(tmp_path), "example-owner", "secret-name", "RIPPLE_TEST_TOKEN_VAR", "mirror"):
        assert leak not in text, leak                                                                                         # no path, URL, remote name or token variable
    fx = body["repositories"][0]
    assert fx["name"] == "Harbor app (fixture)" and fx["source"] == "local" and fx["grants"] == {"read": True, "pull_request": False} and fx["ready"] is True
    rem = body["repositories"][1]
    assert rem["source"] == "remote" and rem["grants"] == {"read": False, "pull_request": True} and rem["sync"] == {"allowed": False, "reason_code": "no_read_grant"}
    admin_ids = [r["repo_id"] for r in q.admin.get("/v2/sources/repositories", params={"workspace_id": q.env.ws}).json()["repositories"]]
    assert admin_ids == ["fixture", "hidden", "remote"]
    viewer = login(q, f"viewer-rl-{q.env.nonce}", ("viewer",))
    v = viewer.get("/v2/sources/repositories", params={"workspace_id": q.env.ws}).json()
    assert v["repositories"][0]["sync"] == {"allowed": False, "reason_code": "permission_missing"}                           # a reader may see, not sync
    assert q.req.get("/v2/sources/repositories", params={"workspace_id": "ws-not-mine"}).status_code in (403, 404)           # another workspace: the usual refusal
    # a hidden repository is refused exactly like an undeclared one, by both actions that take a repo id
    s1 = q.req.post("/v2/sources/git/sync", json={"workspace_id": q.env.ws, "repo_id": "hidden"})
    s2 = q.req.post("/v2/sources/git/sync", json={"workspace_id": q.env.ws, "repo_id": "no-such-repo"})
    assert s1.status_code == s2.status_code == 409 and s1.json()["code"] == s2.json()["code"] == "repo_not_declared"
    assert s1.json()["error"].replace("hidden", "X") == s2.json()["error"].replace("no-such-repo", "X")


def test_can_hints_follow_the_action_checks_and_a_stale_hint_is_still_refused(code_env, fixture_repo, monkeypatch, tmp_path):
    q = code_env
    repo, target = fixture_repo
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps(_two(q.env.ws, repo, target, tmp_path)))
    cid0, _ = confirmed(q, FREIGHT_REQ, [OP_FREIGHT])
    h = q.req.get(f"/v2/business/changes/{cid0}/code/candidate", params={"workspace_id": q.env.ws, "repo_id": "fixture"}).json()["can"]
    assert h["actions"]["create_candidate"]["reason_code"] == "knowledge_not_committed" and h["actions"]["validate"]["reason_code"] == "no_candidate"
    hidden = q.req.get(f"/v2/business/changes/{cid0}/code/candidate", params={"workspace_id": q.env.ws, "repo_id": "hidden"}).json()["can"]
    assert hidden["actions"]["create_candidate"]["reason_code"] == "repo_not_declared"
    cid, approver, committed = _committed(q)
    h = q.req.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws, "repo_id": "fixture"}).json()["can"]
    assert h["actions"]["create_candidate"] == {"allowed": True, "reason_code": None, "reason": None}
    cand_id = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"}).json()["job_id"]
    _settle(q, q.admin, cid, "candidate")
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate/validate", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id}).status_code == 200
    _settle(q, q.admin, cid, "validation")
    appr = login(q, f"appr-h-{q.env.nonce}", ("change_approver",))
    other = login(q, f"appr-o-{q.env.nonce}", ("change_approver",))
    st = appr.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
    hint = st["can"]
    assert hint["for_candidate"] == cand_id and hint["binding"]["patch_hash"] == st["candidate"]["patch_hash"]
    assert hint["actions"]["approve"]["allowed"] is True and hint["actions"]["publish"]["reason_code"] == "not_approved"
    req_hint = q.req.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()["can"]["actions"]
    assert req_hint["approve"]["reason_code"] == "permission_missing"                                                          # same as the action's 403
    # the state moves after the hint was read: another approver decides; the first approver's action is refused although the hint said allowed
    body = {"workspace_id": q.env.ws, "candidate_job_id": cand_id, "action": "approve", "reason": "Reviewed: the literal is the decided value.",
            "patch_hash": hint["binding"]["patch_hash"], "validation_hash": hint["binding"]["validation_hash"]}
    assert other.post(f"/v2/business/changes/{cid}/code/candidate/decide", json=body).status_code == 200
    late = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json=body)
    assert late.status_code == 409 and late.json()["code"] == "already_decided"
    after = appr.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()["can"]["actions"]
    assert after["approve"]["reason_code"] == "already_decided" and after["publish"]["reason_code"] == "no_publish_grant"     # the fixture repo declares no PR grant
    pub = appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert pub.status_code == 409 and pub.json()["code"] == "no_publish_grant"                                                  # hint and action agree
