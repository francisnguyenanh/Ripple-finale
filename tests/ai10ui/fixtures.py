"""pytest fixtures shared by tests/ai10t01 and tests/ai10t02 (import with `*` in each conftest).

`world` = the real hermetic product: isolated Postgres, in-memory blob seam, real
`infra.judge.app` served on 127.0.0.1 by uvicorn (thread), one workspace with distinct human
accounts (analyst/requester, reviewer/knowledge_owner, approver/change_approver,
operator/restore_operator, admin), a pausable worker thread over the production dispatcher (NOT
started until a test asks), and a deterministic provider seam. Playwright pages are created per
test with a request log, dialog log, console-error log and a non-loopback request block.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.ai10ui import harness  # noqa: F401  (sets the isolated DSN env BEFORE upstream imports)
from tests.ai10ui.harness import ServerThread, UiWorker, install_text_provider  # noqa: F401

from tests.ai4t01.conftest import setup, login, drain_outbox, DSN  # noqa: F401
from tests.ai5t02.conftest import owner  # noqa: F401
from tests.ai9t01.conftest import ai9_hermetic  # noqa: F401  (autouse: blob seam + network guard)


_BROWSER = {}


def _close_browser():
    b = _BROWSER.pop("browser", None)
    pw = _BROWSER.pop("pw", None)
    try:
        if b:
            b.close()
    finally:
        if pw:
            pw.stop()


@pytest.fixture(scope="session")
def pw_browser():
    """ONE Chromium per process, even when tests/ai10t01 and tests/ai10t02 register this fixture separately
    (two session instances would start Playwright twice and every later test would error at setup)."""
    if "browser" not in _BROWSER:
        pw_mod = pytest.importorskip("playwright.sync_api")
        pw = pw_mod.sync_playwright().start()
        try:
            _BROWSER["browser"] = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment problem, not a product result
            pw.stop()
            pytest.fail(f"AI10 harness: chromium could not launch: {exc}")
        _BROWSER["pw"] = pw
        import atexit
        atexit.register(_close_browser)
    return _BROWSER["browser"]


class World:
    def __init__(self, setup_ctx, owner_ctx, monkeypatch, tmp_path, pw_browser):
        from infra.adapters.v2_identity import PostgresIdentityStore
        self.setup = setup_ctx
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self._browser = pw_browser
        self.store = setup_ctx["store"]
        self.tenant = setup_ctx["tenant"]
        self.project = setup_ctx["project"]
        self.workspace = setup_ctx["workspace"]
        self.identities = PostgresIdentityStore(DSN)
        self.identities.migrate()
        # operator prerequisite of every analysis job (fail-closed without it)
        self.store.set_project_model_policy(self.tenant, self.project, external_provider="allow")
        self._accounts = {
            "admin": {"account": setup_ctx["admin"], "credential": setup_ctx["credential"][:-4]},
            "analyst": {"account": setup_ctx["requester"], "credential": setup_ctx["credential"]},
            "reviewer": {"account": owner_ctx["account"], "credential": owner_ctx["credential"]},
        }
        for name, role in (("approver", "change_approver"), ("operator", "restore_operator")):
            self.add_account(name, (role,))
        monkeypatch.delenv("RIPPLE_JUDGE_ORIGIN", raising=False)
        self.server = ServerThread().start()
        self.worker = UiWorker(setup_ctx)
        self.provider = None
        self._contexts = []
        self._clients = []

    # ---- accounts
    def add_account(self, name, roles, workspace=None):
        nonce = uuid4().hex[:10]
        account, credential = f"{name}-{nonce}", f"cred-{name}-{nonce}"
        self.identities.create_account(account, credential)
        self.identities.grant_membership(self.tenant, self.project, workspace or self.workspace,
                                         account, tuple(roles))
        self._accounts[name] = {"account": account, "credential": credential}
        return self._accounts[name]

    def acct(self, name):
        return self._accounts[name]

    def grant(self, name, roles, workspace):
        self.identities.grant_membership(self.tenant, self.project, workspace,
                                         self._accounts[name]["account"], tuple(roles))

    def revoke(self, name, workspace=None):
        self.identities.revoke_membership(self.tenant, self.project, workspace or self.workspace,
                                          self._accounts[name]["account"])

    def new_workspace(self, prefix="ws2"):
        workspace = f"{prefix}-{uuid4().hex[:10]}"
        self.store.create_workspace(self.tenant, self.project, workspace)
        return workspace

    # ---- out-of-band API client (in-process TestClient over the SAME app + DB)
    def api(self, name):
        client = self.setup["browser"]()
        a = self.acct(name)
        login(client, a["account"], a["credential"])
        self._clients.append(client)
        return client

    # ---- browser
    def new_page(self, *, timeout_ms=8000):
        context = self._browser.new_context(base_url=self.server.url, accept_downloads=True,
                                            viewport={"width": 1280, "height": 900})
        self._contexts.append(context)
        blocked = []

        def guard(route):
            host = route.request.url.split("/")[2] if "//" in route.request.url else ""
            if host.split(":")[0] in ("127.0.0.1", "localhost") or route.request.url.startswith(
                    ("data:", "about:", "blob:")):
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()

        context.route("**/*", guard)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page._ai10 = SimpleNamespace(dialogs=[], errors=[], requests=[], responses=[],
                                     blocked=blocked, context=context)

        def on_dialog(dialog):
            page._ai10.dialogs.append(dialog.message)
            dialog.dismiss()

        def on_request(req):
            if req.url.startswith(self.server.url):
                page._ai10.requests.append({"url": req.url, "method": req.method,
                                            "body": req.post_data, "headers": req.headers})

        def on_response(resp):
            if resp.url.startswith(self.server.url):
                page._ai10.responses.append({"url": resp.url, "status": resp.status})

        page.on("dialog", on_dialog)
        page.on("pageerror", lambda exc: page._ai10.errors.append(str(exc)))
        page.on("request", on_request)
        page.on("response", on_response)
        return page

    # ---- worker / provider
    def start_worker(self):
        self.worker.start()
        return self.worker

    def close(self):
        self.worker.stop()
        for context in self._contexts:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
        self.server.stop()


@pytest.fixture
def world(setup, owner, monkeypatch, tmp_path, pw_browser):
    w = World(setup, owner, monkeypatch, tmp_path, pw_browser)
    yield w
    w.close()


# ---------------------------------------------------------------------- data builders

YAML_TEXT = "refund_window_days: 14\nmanager_threshold_jpy: 500000\n"
MD_TEXT = "# Refund policy\n\nRefunds are accepted within 14 days of purchase.\n"


def import_via_api(world, files, *, account="analyst", workspace=None):
    """Public API import + commit (prepare -> commit) then drive the ingest worker to the end
    (hermetic). Returns {revision_id, artifacts{logical_path: artifact_id}}. Used ONLY to
    build background state; the UI discovers ids by itself."""
    import base64
    client = world.api(account)
    ws = workspace or world.workspace
    body = {"workspace_id": ws, "idempotency_key": uuid4().hex,
            "files": [{"path": p, "content_b64": base64.b64encode(c).decode()} for p, c in files]}
    prep = client.post("/v2/sources/imports", json=body)
    assert prep.status_code == 200, prep.text
    imp = prep.json()
    head = world.store.head(world.tenant, ws)
    commit = client.post(f"/v2/sources/imports/{imp['import_id']}/commit",
                         json={"workspace_id": ws, "expected_version": head.version})
    assert commit.status_code == 200, commit.text
    revision_id = world.store.head(world.tenant, ws).source_revision_id
    return {"import": imp, "commit": commit.json(), "revision_id": revision_id}


def pump_all(world, *, max_steps=60, workspace=None):
    """Synchronously drain the dispatcher (only while the worker thread is NOT running)."""
    from tests.ai8t03.conftest import pump
    setup_ctx = {**world.setup, "workspace": workspace} if workspace else world.setup
    log = pump(setup_ctx, max_steps=max_steps)
    assert not [e for e in log if e[0] == "EXC"], log
    return log


def artifact_ids(world, revision_id, workspace=None):
    view = world.store.source_revision_view(world.tenant, workspace or world.workspace, revision_id)
    return {a["logical_path"]: a["artifact_id"] for a in view["artifacts"]}


def seed_candidates(world, monkeypatch, *, path, text, candidates, needle=None, extra_files=()):
    """Background state through the PUBLIC API + production dispatcher (worker thread must NOT be
    running): import `text` at `path`, ingest, run a real `extraction` job as the analyst with the
    deterministic provider answering `candidates`. Returns ids for the test to COMPARE the UI
    against afterwards (the UI never receives them)."""
    from tests.ai10ui.harness import install_text_provider
    imp = import_via_api(world, [(path, text.encode())] + list(extra_files))
    pump_all(world)
    ids = artifact_ids(world, imp["revision_id"])
    needle = needle or candidates[0]["quotes"][0][:24]
    holder = install_text_provider(monkeypatch, [(needle, candidates)])
    client = world.api("analyst")
    job_id = f"job-extract-{uuid4().hex}"
    resp = client.post("/v2/jobs", json={"workspace_id": world.workspace, "kind": "extraction",
                                         "job_id": job_id, "payload": {"artifact_id": ids[path],
                                                                       "source_role": "normative"}})
    assert resp.status_code == 200, resp.text
    pump_all(world)
    body = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": world.workspace}).json()
    assert body["execution_status"] == "COMPLETED", body
    cands = body["execution_result"]["candidates"]
    queue = client.get("/v2/hitl/queue", params={"workspace_id": world.workspace}).json()["items"]
    return {"revision_id": imp["revision_id"], "artifact_id": ids[path], "job_id": job_id,
            "candidates": cands, "queue": queue, "provider": holder,
            "question_ids": [i["question_id"] for i in queue]}


REFUND_CAND = {"subject": "refund", "predicate": "refund_window_days", "value": 14, "unit": "days",
               "modality": "must", "scope": {"category": "refund"}, "outcome": "Refund accepted",
               "quotes": ["Refunds are accepted within 14 days of purchase."]}


def seed_one_question(world, monkeypatch):
    return seed_candidates(world, monkeypatch, path="policy/refund.md", text=MD_TEXT,
                           candidates=[REFUND_CAND])


def decisions(world, target=None):
    from tests.ai10ui.harness import q
    sql = ("SELECT decision_id, actor_id, action, target, provenance, rationale, current_permission "
           "FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s")
    args = [world.tenant, world.workspace]
    if target:
        sql += " AND target=%s"
        args.append(target)
    return [tuple(r) for r in q(world.setup, sql + " ORDER BY epoch", tuple(args))]


def hitl_answers(world, question_id=None):
    from tests.ai10ui.harness import q
    sql = ("SELECT question_id, actor_id, answer_text, version_after FROM v2_hitl_answers "
           "WHERE tenant_id=%s AND workspace_id=%s")
    args = [world.tenant, world.workspace]
    if question_id:
        sql += " AND question_id=%s"
        args.append(question_id)
    return [tuple(r) for r in q(world.setup, sql + " ORDER BY id", tuple(args))]


YAML_PATH = "config/refund.yaml"
ORACLE_EXPECTED = 30


def change_base(world, monkeypatch, tmp_path, *, yaml_text=YAML_TEXT, extra_files=(), oracle=True):
    """Background state for the change journeys, built ONLY through public API + dispatcher:
    one revision holding the policy doc and the YAML config; a real extraction candidate over the
    doc; the knowledge owner confirms it and accepts a knowledge revision (the 'current accepted
    KM' every intent needs); the independent verification oracle (operator file, outside the
    model path) expects the threshold to become ORACLE_EXPECTED. The UI still has to discover and
    drive everything from here (intent -> ... -> export); ids below are only for COMPARISON."""
    seed = seed_candidates(world, monkeypatch, path="policy/refund.md", text=MD_TEXT,
                           candidates=[REFUND_CAND],
                           extra_files=[(YAML_PATH, yaml_text.encode())] + list(extra_files))
    cid = seed["candidates"][0]["candidate_id"]
    reviewer = world.api("reviewer")
    resp = reviewer.post("/v2/knowledge/confirm", json={"workspace_id": world.workspace,
                                                        "claim_id": cid, "rationale": "base"})
    assert resp.status_code == 200, resp.text
    head = world.store.head(world.tenant, world.workspace)
    resp = reviewer.post("/v2/knowledge/accept", json={
        "workspace_id": world.workspace, "knowledge_revision_id": "km-base",
        "source_revision_id": head.source_revision_id, "decision_epoch": head.decision_epoch,
        "expected_version": head.version, "idempotency_key": "km-base-key"})
    assert resp.status_code == 200, resp.text
    ids = artifact_ids(world, seed["revision_id"])
    if oracle:
        from tests.ai8t03.conftest import write_oracle
        write_oracle(tmp_path, world.monkeypatch, artifact_id=ids[YAML_PATH], expected=ORACLE_EXPECTED)
    seed.update(yaml_artifact_id=ids[YAML_PATH], candidate_id=cid, artifact_ids=ids)
    return seed


def sql_one(world, sql, *args):
    from tests.ai10ui.harness import q
    rows = q(world.setup, sql, args)
    return rows[0][0] if rows else None


def change_decision_rows(world):
    from tests.ai10ui.harness import q
    return [tuple(r) for r in q(world.setup, "SELECT job_id, action, actor_id, approval_digest FROM "
                                "v2_change_decisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY id",
                                (world.tenant, world.workspace))]


def result_decision_rows(world):
    from tests.ai10ui.harness import q
    return [tuple(r) for r in q(world.setup, "SELECT apply_job_id, action, actor_id, report_digest FROM "
                                "v2_change_result_decisions WHERE tenant_id=%s AND workspace_id=%s "
                                "ORDER BY id", (world.tenant, world.workspace))]


def jobs_of_kind(world, kind):
    from tests.ai10ui.harness import q
    return [tuple(r) for r in q(world.setup, "SELECT job_id, execution_status FROM v2_jobs WHERE "
                                "tenant_id=%s AND workspace_id=%s AND kind=%s ORDER BY job_id",
                                (world.tenant, world.workspace, kind))]


TERMINAL_JOB = ("COMPLETED", "FAILED", "CANCELLED")


def api_job(client, ws, kind, payload, *, timeout=60):
    """Submit a job through the public route and wait for its terminal execution status
    (worker thread must be running)."""
    import time
    job_id = f"job-{kind}-{uuid4().hex}"
    resp = client.post("/v2/jobs", json={"workspace_id": ws, "kind": kind, "job_id": job_id,
                                         "payload": payload})
    assert resp.status_code == 200, (kind, resp.text)
    end = time.time() + timeout
    body = None
    while time.time() < end:
        body = client.get(f"/v2/jobs/{job_id}", params={"workspace_id": ws}).json()
        if body["execution_status"] in TERMINAL_JOB:
            return job_id, body
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish: {body}")


def api_chain(world, base, *, upto="approval", proposed=30, expected=14, approve=False):
    """The public API chain (no browser) up to `upto` in {approval, applied}. Used only where the
    property under test is the SERVER's answer to a raw client (forged digest, wrong role, revoked
    account); the UI journeys never use it. Returns a dict of ids/digests."""
    ws = world.workspace
    req, own, apr = world.api("analyst"), world.api("reviewer"), world.api("approver")
    parse = req.post("/v2/intent/parse", json={"workspace_id": ws, "raw_request": f"Set the refund window to {proposed} days.",
                                               "target_hint": base["candidate_id"]})
    assert parse.status_code == 200, parse.text
    intent_id = parse.json()["intent_id"]
    assert own.post("/v2/intent/confirm", json={"workspace_id": ws, "intent_id": intent_id}).status_code == 200
    impact, _ = api_job(req, ws, "impact", {"candidate_ids": [], "existing_finding_ids": []})
    sim, _ = api_job(req, ws, "simulation", {})
    prop, b = api_job(req, ws, "proposal", {
        "intent_id": intent_id, "impact_job_id": impact, "simulation_job_id": sim,
        "target_artifact_id": base["yaml_artifact_id"], "edit_kind": "yaml_typed_edit",
        "edits": [{"path": "refund_window_days", "expected_current": expected, "proposed": proposed}]})
    assert b["execution_status"] == "COMPLETED", b
    review, b = api_job(req, ws, "review", {"proposal_job_id": prop})
    approval, b = api_job(req, ws, "approval", {"proposal_job_id": prop, "review_job_id": review})
    assert b["execution_status"] == "COMPLETED", b
    out = {"intent_id": intent_id, "proposal": prop, "review": review, "approval": approval,
           "digest": b["execution_result"]["approval_digest"], "req": req, "own": own, "apr": apr}
    if upto == "approval" and not approve:
        return out
    dec = apr.post(f"/v2/jobs/{approval}/decision", json={
        "workspace_id": ws, "action": "approve", "reason": "api chain",
        "expected_approval_digest": out["digest"]})
    assert dec.status_code == 200, dec.text
    if upto == "approval":
        return out
    apply_id, b = api_job(req, ws, "apply", {"approval_job_id": approval,
                                             "expected_approval_digest": out["digest"]})
    assert b["execution_status"] == "COMPLETED", b
    out["apply"] = apply_id
    return out


def wait_change_api(client, ws, apply_id, state, *, timeout=60):
    import time
    end = time.time() + timeout
    dto = None
    while time.time() < end:
        dto = client.get(f"/v2/changes/{apply_id}", params={"workspace_id": ws}).json()
        if dto["state"] == state:
            return dto
        time.sleep(0.3)
    raise AssertionError(f"change {apply_id} never reached {state}: {dto}")


def complete_change_via_api(world, base, *, proposed=30, expected=14):
    """Public API: apply -> verify -> KM accept -> result accept. Background history for the
    history/restore/refresh journeys (S0 -> S1); the UI discovers the revisions itself."""
    chain = api_chain(world, base, upto="applied", proposed=proposed, expected=expected)
    ws, apply_id = world.workspace, chain["apply"]
    dto = wait_change_api(chain["req"], ws, apply_id, "MACHINE_VERIFIED")
    head = world.store.head(world.tenant, ws)
    acc = chain["own"].post("/v2/knowledge/accept", json={
        "workspace_id": ws, "knowledge_revision_id": dto["knowledge"]["candidate_revision_id"],
        "source_revision_id": head.source_revision_id, "decision_epoch": head.decision_epoch,
        "expected_version": head.version, "idempotency_key": uuid4().hex})
    assert acc.status_code == 200, acc.text
    report = world.store.job_record(world.tenant, ws, dto["verification"]["job_id"])["execution_result"]
    done = chain["apr"].post(f"/v2/changes/{apply_id}/result-decision", json={
        "workspace_id": ws, "action": "accept", "reason": "background",
        "expected_report_digest": report["report_digest"]})
    assert done.status_code == 200, done.text
    chain["dto"] = wait_change_api(chain["req"], ws, apply_id, "COMPLETED")
    chain["s1"] = world.store.head(world.tenant, ws).source_revision_id
    return chain
