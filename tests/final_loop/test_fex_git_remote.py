"""FEX-01 (remote read) and FEX-05 (pull request) through the product-side Git connector, with a MOCKED GitHub transport and a local bare repository standing in for
github.com. Real routes, worker, store and ledger; no network. What this proves: grants are explicit (no read grant / no pull_request grant refuse; merge is never a grant and
never called); the fetch job brings only the declared branch into the managed mirror and hands it to git.sync; a deleted branch, a wrong repository, an occupied mirror and a
missing token are refused with their reason; the token never reaches argv; publishing re-checks the approval, validation, committed knowledge and the base on the branch, creates
ONE branch + commit + pull request, is idempotent on retry and records `pr_published` in the ledger; a revoked token is `remote_auth_refused`; a rewritten base is `stale_base`;
an existing branch with other content is `branch_conflict`.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time

import pytest

from infra.worker import code_change as cc
from infra.worker import git_remote as gr
from infra.worker.execution_types import NonRetryableExecutionError
from tests.final_loop.test_fex_code_change import CALC, TEST_FILE, _committed, _settle
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401
from tests.final_loop.test_syn01_code import code_env  # noqa: F401
from tests.pitch_brief.conftest import HARBOR_INPUT, drain

ID = ["-c", "user.name=fixture", "-c", "user.email=fixture@localhost"]


def _git(cwd, *args):
    return subprocess.run(["git", *ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _remote(tmp_path, files):
    """A work repository and its bare 'remote' (what github.com would hold)."""
    work = tmp_path / "work"
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "baseline")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    _git(work, "remote", "add", "origin", str(bare))
    return work, bare


def _repo_entry(bare, mirror, grants=("read", "pull_request"), **extra):
    remote = {"provider": "github", "repository": "acme/harbor", "branch": "main", "grants": list(grants), "token_env": "RIPPLE_TEST_GIT_TOKEN", "url": str(bare)}
    return {"path": str(mirror), "allowlist": ["app/"], "remote": remote, **extra}


class FakeGitHub:
    """The GitHub REST surface the connector uses, over the bare repository (existing commits) plus in-memory created objects."""

    def __init__(self, bare, status=None):
        self.bare, self.status, self.calls = bare, status, []
        self.blobs, self.trees, self.commits, self.refs, self.pulls = {}, {}, {}, {}, []

    def _g(self, *args):
        r = subprocess.run(["git", *args], cwd=self.bare, capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if self.status:
            return self.status, {"message": "Bad credentials"}
        parts = path.split("?")[0].split("/")[4:]                       # after /repos/acme/harbor
        if method == "GET" and parts[0] == "compare":
            base, branch = parts[1].split("...")
            if self._g("cat-file", "-e", base + "^{commit}")[0] != 0:
                return 404, {"message": "Not Found"}
            head = self._g("rev-parse", f"refs/heads/{branch}")[1]
            if base == head:
                return 200, {"status": "identical"}
            return 200, {"status": "ahead" if self._g("merge-base", "--is-ancestor", base, head)[0] == 0 else "diverged"}
        if parts[:2] == ["git", "commits"] and method == "GET":
            sha = parts[2]
            if sha in self.commits:
                return 200, self.commits[sha]
            code, tree = self._g("rev-parse", sha + "^{tree}")
            if code:
                return 404, {}
            parents = self._g("rev-list", "--parents", "-n", "1", sha)[1].split()[1:]
            return 200, {"sha": sha, "tree": {"sha": tree}, "parents": [{"sha": p} for p in parents]}
        if parts[:2] == ["git", "blobs"]:
            sha = hashlib.sha1(body["content"].encode()).hexdigest()
            self.blobs[sha] = body
            return 201, {"sha": sha}
        if parts[:2] == ["git", "trees"]:
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            self.trees[sha] = body
            return 201, {"sha": sha}
        if parts[:2] == ["git", "commits"] and method == "POST":
            sha = hashlib.sha1(json.dumps([body, len(self.commits)]).encode()).hexdigest()
            self.commits[sha] = {"sha": sha, "tree": {"sha": body["tree"]}, "parents": [{"sha": p} for p in body["parents"]], "message": body["message"]}
            return 201, self.commits[sha]
        if parts[:2] == ["git", "ref"]:
            name = "/".join(parts[3:])
            return (200, {"object": {"sha": self.refs[name]}}) if name in self.refs else (404, {"message": "Not Found"})
        if parts[:2] == ["git", "refs"]:
            name = body["ref"][len("refs/heads/"):]
            if name in self.refs:
                return 422, {"message": "Reference already exists"}
            self.refs[name] = body["sha"]
            return 201, {"ref": body["ref"]}
        if parts[0] == "pulls" and method == "GET":
            head = path.split("head=")[1].split("&")[0].split(":", 1)[1]
            return 200, [p for p in self.pulls if p["head"] == head]
        if parts[0] == "pulls" and method == "POST":
            pr = {"number": len(self.pulls) + 1, "html_url": f"https://github.invalid/acme/harbor/pull/{len(self.pulls) + 1}", "state": "open", "merged_at": None, "head": body["head"],
                  "base": body["base"], "title": body["title"]}
            self.pulls.append(pr)
            return 201, pr
        return 400, {"message": f"unexpected {method} {path}"}


# ---- pure ---------------------------------------------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [{"provider": "gitlab"}, {"repository": "not a repo"}, {"grants": ["read", "merge"]}, {"grants": ["admin"]}, {"branch": "../main"},
                                 {"url": "https://evil.example/acme/harbor.git"}, {"token_env": "lower"}])
def test_the_remote_declaration_is_strict_and_merge_is_never_a_grant(tmp_path, bad):
    _w, bare = _remote(tmp_path, {"app/x.py": "x = 1\n"})
    entry = _repo_entry(bare, tmp_path / "mirror")
    entry["remote"].update(bad)
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.remote_spec(entry)
    assert e.value.reason == "remote_config_invalid"


def test_without_a_url_override_the_remote_is_github_and_the_token_goes_to_the_environment_never_argv(tmp_path, monkeypatch):
    spec = gr.remote_spec({"path": str(tmp_path), "remote": {"provider": "github", "repository": "acme/harbor", "grants": ["read"], "token_env": "RIPPLE_TEST_GIT_TOKEN"}})
    assert spec["url"] == "https://github.com/acme/harbor.git" and spec["https"]
    monkeypatch.delenv("RIPPLE_TEST_GIT_TOKEN", raising=False)
    with pytest.raises(NonRetryableExecutionError) as e:
        gr._git_env(spec)
    assert e.value.reason == "remote_token_missing"
    monkeypatch.setenv("RIPPLE_TEST_GIT_TOKEN", "tok-not-real-123")
    env, secrets = gr._git_env(spec)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader" and "tok-not-real-123" in secrets and env["GIT_TERMINAL_PROMPT"] == "0"
    assert gr._scrub("fatal: tok-not-real-123 refused", secrets) == "fatal: *** refused"


def test_fetch_brings_only_the_declared_branch_into_the_managed_mirror_and_refuses_what_it_must(tmp_path):
    work, bare = _remote(tmp_path, {"app/x.py": "x = 1\n"})
    _git(work, "checkout", "-q", "-b", "other")
    (work / "app" / "x.py").write_text("x = 99\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "other branch")
    _git(work, "push", "-q", "origin", "other")
    _git(work, "checkout", "-q", "main")
    mirror = tmp_path / "mirror"
    entry = _repo_entry(bare, mirror)
    sha1 = gr.fetch(entry)
    assert sha1 == _git(work, "rev-parse", "main") and (mirror / "app" / "x.py").read_text(encoding="utf-8") == "x = 1\n"
    assert "refs/remotes/origin/other" not in _git(mirror, "for-each-ref", "--format=%(refname)")       # only the declared branch
    (work / "app" / "x.py").write_text("x = 2\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "second")
    _git(work, "push", "-q", "origin", "main")
    sha2 = gr.fetch(entry)
    assert sha2 != sha1 and cc._git(str(cc.clone(str(mirror), sha2)), "rev-parse", "HEAD").strip() == sha2                # a clone of the mirror carries the commit
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.fetch(_repo_entry(bare, tmp_path / "m2", grants=("pull_request",)))
    assert e.value.reason == "no_read_grant"
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "notes.txt").write_text("someone's files", encoding="utf-8")
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.fetch(_repo_entry(bare, occupied))
    assert e.value.reason == "mirror_not_managed"
    _w2, bare2 = _remote(tmp_path / "second", {"app/y.py": "y = 1\n"})
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.fetch(_repo_entry(bare2, mirror))                                                                              # the mirror belongs to another remote
    assert e.value.reason == "mirror_not_managed"
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.fetch(_repo_entry(empty, tmp_path / "m3"))
    assert e.value.reason == "remote_not_found"
    subprocess.run(["git", "branch", "-D", "main"], cwd=bare, check=True, capture_output=True)                      # the declared branch is deleted on the remote
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.fetch(entry)
    assert e.value.reason == "remote_branch_missing"


# ---- route + worker: fetch -> sync -------------------------------------------------------------------------------------------------------------------------------------------

def _settle_job(q, job_id, tries=80):
    rec = None
    for _ in range(tries):
        drain(q.env)
        rec = q.env.store.job_record(q.env.tenant, q.env.ws, job_id)
        if rec["execution_status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return rec
        time.sleep(0.3)
    return rec


def test_the_sync_route_on_a_remote_repository_fetches_by_job_and_hands_the_commit_to_git_sync(qa_env, tmp_path, monkeypatch):
    q = qa_env
    yaml = (HARBOR_INPUT / "config" / "approval.yaml").read_text(encoding="utf-8")
    work, bare = _remote(tmp_path, {"config/approval.yaml": yaml.replace("standard_manager_threshold_jpy: 1000000", "standard_manager_threshold_jpy: 1500000")})
    entry = _repo_entry(bare, tmp_path / "mirror", grants=("read",), allowlist=["config/"], mount="harbor_demo")
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps({"workspaces": {q.env.ws: {"repos": {"harbor": entry}}}}))
    r = q.admin.post("/v2/sources/git/sync", json={"workspace_id": q.env.ws, "repo_id": "harbor"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "git.fetch" and r.json()["created"] is True
    again = q.admin.post("/v2/sources/git/sync", json={"workspace_id": q.env.ws, "repo_id": "harbor"})
    assert again.json()["job_id"] == r.json()["job_id"] and again.json()["created"] is False                       # one fetch per minute and source revision
    fetched = _settle_job(q, r.json()["job_id"])
    assert fetched["execution_status"] == "COMPLETED", fetched.get("execution_reason")
    res = fetched["execution_result"]
    assert res["sha"] == _git(work, "rev-parse", "main") and res["repository"] == "acme/harbor"
    synced = _settle_job(q, res["sync_job_id"])
    assert synced["execution_status"] == "COMPLETED", synced.get("execution_reason")
    assert synced["execution_result"]["sha"] == res["sha"] and synced["execution_result"]["files"]["changed"] == ["harbor_demo/config/approval.yaml"]
    entry_no_read = _repo_entry(bare, tmp_path / "mirror", grants=("pull_request",))
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps({"workspaces": {q.env.ws: {"repos": {"harbor": entry_no_read}}}}))
    refused = q.admin.post("/v2/sources/git/sync", json={"workspace_id": q.env.ws, "repo_id": "harbor"})
    assert refused.status_code == 409 and refused.json()["code"] == "no_read_grant"


# ---- route + worker: approve -> publish ---------------------------------------------------------------------------------------------------------------------------------------

def _approved(q, entry, monkeypatch):
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps({"workspaces": {q.env.ws: {"repos": {"fixture": entry}}}}))
    gr.fetch(entry)                                                                                               # the managed mirror (fetch job tested above)
    cid, approver, committed = _committed(q)
    cand_id = q.req.post(f"/v2/business/changes/{cid}/code/candidate", json={"workspace_id": q.env.ws, "repo_id": "fixture"}).json()["job_id"]
    c = _settle(q, q.admin, cid, "candidate")["candidate"]
    assert c["status"] == "PROPOSED", c
    q.req.post(f"/v2/business/changes/{cid}/code/candidate/validate", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    val = _settle(q, q.admin, cid, "validation")["validation"]
    assert val["report_status"] == "PASSED", val
    from tests.final_loop.test_syn01_code import login
    appr = login(q, f"appr-pub-{q.env.nonce}", ("change_approver",))
    ok = appr.post(f"/v2/business/changes/{cid}/code/candidate/decide", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id, "action": "approve",
                                                                               "reason": "Reviewed the diff.", "patch_hash": c["patch_hash"], "validation_hash": val["validation_hash"]})
    assert ok.status_code == 200, ok.text
    return cid, cand_id, c, val, appr


def _fixture_files():
    return {"app/__init__.py": "", "app/calculations.py": CALC, "tests/test_freight.py": TEST_FILE, "requirements.txt": "# protected file: never patched\n"}


def test_an_approved_candidate_becomes_one_branch_commit_and_pull_request_idempotently_and_never_a_merge(code_env, tmp_path, monkeypatch):
    q = code_env
    work, bare = _remote(tmp_path, _fixture_files())
    entry = _repo_entry(bare, tmp_path / "mirror", validate=[["{python}", "-m", "pytest", "-q", "tests"]])
    fake = FakeGitHub(bare)
    monkeypatch.setattr(gr, "API_FACTORY", lambda spec: fake)
    cid, cand_id, c, val, appr = _approved(q, entry, monkeypatch)
    pub = appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert pub.status_code == 200, pub.text
    job = pub.json()
    assert job["created"] is True and job["branch"] == f"ripple/{cid}-{c['patch_hash'][:12]}"
    assert appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id}).json()["created"] is False
    rec = _settle_job(q, job["job_id"])
    assert rec["execution_status"] == "COMPLETED", rec.get("execution_reason")
    res = rec["execution_result"]
    assert res["pr_number"] == 1 and res["base_sha"] == c["base_sha"] and res["merged"] is False and len(fake.pulls) == 1 and fake.refs == {job["branch"]: res["commit_sha"]}
    tree = fake.trees[res["tree_sha"]]
    assert [e["path"] for e in tree["tree"]] == ["app/calculations.py"]                                         # only the patched file
    blob = fake.blobs[tree["tree"][0]["sha"]]
    import base64
    assert b"600_000" in base64.b64decode(blob["content"])
    assert not any("/merge" in p or m in ("PUT", "DELETE", "PATCH") for m, p in fake.calls)                    # never a merge, delete or protection change
    state = q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()
    assert state["pull_request"]["pr_number"] == 1 and "pr_published" in [d["kind"] for d in state["decisions"]]
    # a retry of the same publication reuses the branch and the pull request
    spec = gr.remote_spec(entry)
    files = gr._patched_files(entry["path"], c["base_sha"], q.admin.get(f"/v2/business/changes/{cid}/code/candidate", params={"workspace_id": q.env.ws}).json()["candidate"]["diff"])
    again = gr.publish_pr(fake, spec, base_sha=c["base_sha"], files=files, branch=job["branch"], title="t", body="b", message="m")
    assert again["reused_branch"] is True and again["reused_pr"] is True and again["pr_number"] == 1 and len(fake.pulls) == 1
    # the same branch name with other content is never overwritten
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.publish_pr(fake, spec, base_sha=c["base_sha"], files={"app/calculations.py": b"other\n"}, branch=job["branch"], title="t", body="b", message="m")
    assert e.value.reason == "branch_conflict"
    # a rewritten branch on the remote leaves the candidate's base behind: stale
    _git(work, "checkout", "-q", "--orphan", "rewritten")
    _git(work, "commit", "-q", "-m", "rewritten history")
    _git(work, "push", "-q", "-f", "origin", "rewritten:main")
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.publish_pr(fake, spec, base_sha=c["base_sha"], files=files, branch="ripple/other", title="t", body="b", message="m")
    assert e.value.reason == "stale_base"
    # a revoked token is refused with its reason
    with pytest.raises(NonRetryableExecutionError) as e:
        gr.publish_pr(FakeGitHub(bare, status=401), spec, base_sha=c["base_sha"], files=files, branch="ripple/x", title="t", body="b", message="m")
    assert e.value.reason == "remote_auth_refused"


def test_without_a_pull_request_grant_publishing_stays_refused_and_a_missing_token_fails_the_job(code_env, tmp_path, monkeypatch):
    q = code_env
    work, bare = _remote(tmp_path, _fixture_files())
    entry = _repo_entry(bare, tmp_path / "mirror", grants=("read",), validate=[["{python}", "-m", "pytest", "-q", "tests"]])
    cid, cand_id, c, val, appr = _approved(q, entry, monkeypatch)
    refused = appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert refused.status_code == 409 and refused.json()["code"] == "no_publish_grant"
    granted = dict(entry, remote=dict(entry["remote"], grants=["read", "pull_request"]))
    monkeypatch.setenv("RIPPLE_EXTENSION_CONFIG", json.dumps({"workspaces": {q.env.ws: {"repos": {"fixture": granted}}}}))
    monkeypatch.delenv("RIPPLE_TEST_GIT_TOKEN", raising=False)
    monkeypatch.setattr(gr, "API_FACTORY", None)                                                                  # the real transport: no token -> refused before any network call
    pub = appr.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id})
    assert pub.status_code == 200, pub.text
    rec = _settle_job(q, pub.json()["job_id"])
    assert rec["execution_status"] == "FAILED" and "remote_token_missing" in (rec.get("execution_reason") or "")
    assert q.req.post(f"/v2/business/changes/{cid}/code/candidate/publish", json={"workspace_id": q.env.ws, "candidate_job_id": cand_id}).status_code == 403
