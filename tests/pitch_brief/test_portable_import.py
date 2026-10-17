"""Portable importer (offline, isolated store): dry run writes nothing; apply imports the sources through the production route and reuses only compatible, re-validated
outputs; ids are remapped; a second run writes nothing; a modified package, a wrong actor, a different target revision or a different model/prompt is refused or not reused;
nothing carries memberships, credentials or approval authority, and no provider is called.

FIXTURE HONESTY: the package here is exported from a throwaway workspace whose extraction outputs come from the suite's stub provider; a LIVE start record is
inserted in THAT workspace only so the exporter's live-history guard (tested separately) lets the plumbing run. These outputs are plumbing fixtures, never live evidence.
"""
from __future__ import annotations

import base64
import json
import shutil
from uuid import uuid4

import psycopg2
import pytest

from infra.adapters.v2_identity import PostgresIdentityStore
from infra.adapters.v2_postgres import PostgresV2Store
from infra.worker import extraction, job_quota
from tests.pitch_brief import conftest as C
from tests.pitch_brief.chain import build
from tools.judge_local import portable_import as I
from tools.judge_local import portable_package as P

DSN = C.DSN


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


@pytest.fixture
def pkg(env, ch, tmp_path):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'live_local.start','operator','{\"mode\":\"LIVE\"}')", (env.tenant, env.ws))
    out = tmp_path / "pkg"
    P.export(DSN, env.blobs, env.tenant, env.ws, out)
    return out


def _target(env, monkeypatch, *, admin_roles=("admin",)):
    """A SECOND tenant/workspace with its own admin, sharing the content-addressed blob store; its sources come only from the importer."""
    nonce = uuid4().hex[:10]
    tenant, project, ws = f"tt-{nonce}", f"tp-{nonce}", f"tw-{nonce}"
    store = PostgresV2Store(DSN, env.blobs)
    store.create_workspace(tenant, project, ws)
    store.set_project_model_policy(tenant, project, external_provider="allow")
    ids = PostgresIdentityStore(DSN)
    admin, other = f"ta-{nonce}", f"tr-{nonce}"
    ids.bootstrap_admin(tenant, project, ws, admin, f"cred-{nonce}")
    ids.create_account(other, f"cred-o-{nonce}")
    ids.grant_membership(tenant, project, ws, other, ("requester",))
    from fastapi.testclient import TestClient
    from infra import judge
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": admin, "credential": f"cred-{nonce}"}).status_code == 200

    def import_sources(files):
        body = {"workspace_id": ws, "idempotency_key": uuid4().hex, "files": [{"path": p, "content_b64": base64.b64encode(b).decode()} for p, b in files]}
        r = c.post("/v2/sources/imports", json=body)
        assert r.status_code == 200, r.text
        r2 = c.post(f"/v2/sources/imports/{r.json()['import_id']}/commit", json={"workspace_id": ws, "expected_version": 0, "acknowledged_skips": []})
        assert r2.status_code == 200, r2.text
        from infra.worker.job_worker import claim_and_admit_one
        assert claim_and_admit_one(store, tenant, ws, "ingest") is not None
        return store.head(tenant, ws).source_revision_id
    return type("T", (), dict(tenant=tenant, ws=ws, store=store, admin=admin, other=other, client=c, import_sources=staticmethod(import_sources)))


def _cfg():
    return job_quota.load_worker_budget_config()


def _jobs(t, kind="extraction"):
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s", (t.tenant, t.ws, kind))
        return cur.fetchone()[0]


def _rehash(pkg_dir):
    m = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
    m["integrity"] = P._integrity(pkg_dir)
    (pkg_dir / "manifest.json").write_bytes(P._canon(m))


class NoProvider:
    def __getattr__(self, name):
        raise AssertionError("the importer must never reach a provider")


def test_01_dry_run_on_an_empty_target_writes_nothing(env, ch, pkg, monkeypatch):
    t = _target(env, monkeypatch)
    rep = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), apply=False)
    assert rep["ok"], rep
    assert rep["dry_run"] and len(rep["reuse"]) == 5 and not rep["regenerate"] and rep["written"] == []
    assert any("would import the source files" in w for w in rep["warnings"])
    assert t.store.head(t.tenant, t.ws).source_revision_id is None and _jobs(t) == 0           # no revision, no job
    assert rep["authority_transferred"] is False and rep["memberships_or_credentials_imported"] is False


def test_02_apply_imports_sources_then_reuses_revalidated_outputs_without_a_provider(env, ch, pkg, monkeypatch):
    t = _target(env, monkeypatch)
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: NoProvider())
    rep = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert rep["ok"], rep
    assert len(rep["written"]) == 5 and not rep["regenerate"] and rep["errors"] == []
    rev = t.store.head(t.tenant, t.ws).source_revision_id
    assert rev and rev != ch.rev                                                                # a NEW revision id made by the production import route
    for jid in rep["written"]:
        rec = t.store.job_record(t.tenant, t.ws, jid)
        assert rec["execution_status"] == "COMPLETED" and rec["execution_executor_id"] == I.IMPORT_EXECUTOR
        assert rec["execution_provider_calls"] == 0 and str(rec["execution_cost_usd"]) in ("0", "0.0", "0.00")
        assert rec["execution_result"]["imported_from"]["package_root"].startswith("sha256:")
        assert rec["approved_by"] == t.admin and rec["requested_by"] == "portable-import"      # the importing operator, not the package's approver
        assert all(c["source_revision_id"] == rev for c in rec["execution_result"]["candidates"])
        text = json.dumps(rec["execution_result"])
        assert ch.rev not in text and not any(a in text for a in ch.arts.values())             # every old id was remapped
    # the product reads the imported output like any other extraction: the review queue lists candidates
    q = t.client.get("/v2/hitl/queue", params={"workspace_id": t.ws})
    assert q.status_code == 200 and q.json().get("items")
    # nothing was queued for a worker: no outbox event exists for an imported job, and no spend was booked for them
    with psycopg2.connect(DSN) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND payload->>'job_id' = ANY(%s)", (t.tenant, t.ws, rep["written"]))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT coalesce(sum(cost_usd),0) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)", (t.tenant, t.ws, rep["written"]))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s", (t.tenant, t.ws))
        assert cur.fetchone()[0] == 0                                                           # no decision, no approval came across
        cur.execute("SELECT count(*) FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s", (t.tenant, t.ws))
        assert cur.fetchone()[0] == 2                                                           # only the two accounts this test created


def test_03_a_second_run_writes_nothing(env, ch, pkg, monkeypatch):
    t = _target(env, monkeypatch)
    first = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    n = _jobs(t)
    second = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert second["ok"] and second["written"] == [] and sorted(second["skipped_existing"]) == sorted(first["written"]) and _jobs(t) == n == 5


def test_04_only_an_admin_of_the_target_may_import(env, ch, pkg, monkeypatch):
    t = _target(env, monkeypatch)
    rep = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.other, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert not rep["ok"] and any("admin role" in e for e in rep["errors"])
    rep = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor="someone-unknown", config=_cfg(), import_sources=t.import_sources, apply=True)
    assert not rep["ok"] and t.store.head(t.tenant, t.ws).source_revision_id is None and _jobs(t) == 0


def test_05_a_modified_package_is_rejected_before_anything_is_written(env, ch, pkg, monkeypatch, tmp_path):
    t = _target(env, monkeypatch)
    # (a) a source blob is changed
    a = tmp_path / "a"
    shutil.copytree(pkg, a)
    blob = next((a / "blobs").iterdir())
    blob.write_bytes(blob.read_bytes() + b"x")
    r = I.run(a, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert not r["ok"] and any("integrity" in e or "digest" in e for e in r["errors"])
    # (b) a quote is edited and the manifest is re-hashed by the forger: the evidence re-check still catches it
    b = tmp_path / "b"
    shutil.copytree(pkg, b)
    rows = [json.loads(x) for x in (b / "extractions.jsonl").read_text(encoding="utf-8").splitlines()]
    rows[0]["execution_result"]["candidates"][0]["evidence"][0]["quote"] = "a quote that the source never contained"
    (b / "extractions.jsonl").write_bytes(b"".join(P._canon(x) for x in rows))
    _rehash(b)
    r = I.run(b, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert not r["ok"] and any("quote is not found" in e for e in r["errors"])
    # (c) memberships smuggled in, manifest re-hashed
    c = tmp_path / "c"
    shutil.copytree(pkg, c)
    rows = [json.loads(x) for x in (c / "decisions.jsonl").read_text(encoding="utf-8").splitlines()] or [{"decision_id": "d"}]
    rows[0]["memberships"] = [{"account_id": "evil", "roles": ["admin"]}]
    (c / "decisions.jsonl").write_bytes(b"".join(P._canon(x) for x in rows))
    _rehash(c)
    r = I.run(c, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert not r["ok"] and any("forbidden key" in e for e in r["errors"])
    assert t.store.head(t.tenant, t.ws).source_revision_id is None and _jobs(t) == 0           # none of the three wrote anything, not even the sources


def test_06_an_output_that_does_not_match_the_target_is_not_reused(env, ch, pkg, monkeypatch, tmp_path):
    t = _target(env, monkeypatch)
    # a changed prompt hash with a re-hashed manifest: the target's own executor recomputes it and disagrees
    d = tmp_path / "d"
    shutil.copytree(pkg, d)
    rows = [json.loads(x) for x in (d / "extractions.jsonl").read_text(encoding="utf-8").splitlines()]
    rows[0]["execution_prompt_hash"] = "sha256:" + "0" * 64
    (d / "extractions.jsonl").write_bytes(b"".join(P._canon(x) for x in rows))
    _rehash(d)
    r = I.run(d, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert r["ok"] and len(r["written"]) == 4 and len(r["regenerate"]) == 1
    assert any("prompt hash differs" in x for x in r["regenerate"][0]["reasons"])
    # a different configured model: nothing is reusable, no provider is called, the sources are still imported
    t2 = _target(env, monkeypatch)
    text = C.REAL_BUDGET_PATH.read_text(encoding="utf-8").replace("  extraction: gemini-3.8-flash", "  extraction: gemini-3.5-flash-lite")
    other_cfg = tmp_path / "other.yaml"
    other_cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(other_cfg))
    r = I.run(pkg, store=t2.store, tenant=t2.tenant, ws=t2.ws, actor=t2.admin, config=_cfg(), import_sources=t2.import_sources, apply=True)
    assert r["ok"] and r["written"] == [] and len(r["regenerate"]) == 5 and all("model differs" in x["reasons"][0] for x in r["regenerate"])


def test_07_a_target_with_a_different_revision_is_refused_and_nothing_is_overwritten(env, ch, pkg, monkeypatch):
    t = _target(env, monkeypatch)
    from tests.pitch_brief.conftest import harbor_files
    rev = t.import_sources(harbor_files()[:5])                                                  # five of the six files: a different revision
    r = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), import_sources=t.import_sources, apply=True)
    assert not r["ok"] and any("different source revision" in e for e in r["errors"])
    assert t.store.head(t.tenant, t.ws).source_revision_id == rev and _jobs(t) == 0


def test_08_a_package_without_live_history_cannot_even_be_exported(env, ch, tmp_path):
    """The stub-built workspace has no live start record: the exporter refuses it, so stub outputs never become a package by accident."""
    with pytest.raises(SystemExit, match="no live-local start"):
        P.export(DSN, env.blobs, env.tenant, env.ws, tmp_path / "x")


def test_09_the_cli_dry_runs_then_applies_to_the_live_local_instance_only(env, ch, pkg, monkeypatch, capsys):
    """Same library, driven the way an operator would: dry run first (writes nothing), then --apply, then a second --apply that writes nothing."""
    import re
    import secrets
    from tools.judge_local import live_local as L
    mp = monkeypatch
    home = pkg.parent / "home"
    mp.setattr(L, "HOME", home)
    mp.setattr(L, "ADMIN_DSN", DSN.replace("dbname=pbrief", "dbname=postgres"))
    mp.setattr(L, "DB", "livelocal_p" + secrets.token_hex(3))
    for k in ("RIPPLE_V2_DSN", "RIPPLE_V2_BLOB_BUCKET"):
        mp.setenv(k, "x")
    from infra import v2_source_routes
    mp.setattr(v2_source_routes, "_blob_port", v2_source_routes._blob_port)
    try:
        L.ensure_database()
        store, ids, _ = L.services()
        pw = secrets.token_urlsafe(8)
        issued = L.provision(store, ids)
        mp.setenv("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", issued["live-admin"])
        assert I.main([str(pkg)]) == 0
        out = capsys.readouterr().out
        rep = json.loads(out[out.index("{"):])
        assert rep["dry_run"] and len(rep["reuse"]) == 5 and rep["written"] == []
        assert store.head(L.TENANT, L.WS).source_revision_id is None                              # a dry run imported nothing
        assert I.main([str(pkg), "--apply"]) == 0
        out = capsys.readouterr().out
        rep = json.loads(out[out.index("{"):])
        assert len(rep["written"]) == 5 and not rep["regenerate"]
        with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
            cur.execute("SELECT count(*), coalesce(sum(execution_provider_calls),0), coalesce(sum(execution_cost_usd),0) FROM v2_jobs WHERE tenant_id=%s AND kind='extraction'", (L.TENANT,))
            assert cur.fetchone() == (5, 0, 0)
        assert I.main([str(pkg), "--apply"]) == 0
        out = capsys.readouterr().out
        rep = json.loads(out[out.index("{"):])
        assert rep["written"] == [] and len(rep["skipped_existing"]) == 5
        os_pw = issued["live-admin"]
        assert os_pw not in out                                                                  # the password is never printed
    finally:
        try:
            L.cmd_reset(type("A", (), {"confirm": L.DB})())
        except SystemExit:
            pass


def test_dry_run_says_what_is_kept_recorded_and_not_carried(env, ch, pkg, monkeypatch):
    t = _target(env, monkeypatch)
    rep = I.run(pkg, store=t.store, tenant=t.tenant, ws=t.ws, actor=t.admin, config=_cfg(), apply=False)
    sc = rep["scope"]
    assert sc["kind"].startswith("reuse of AI extraction outputs") and "not a restore" in sc["kind"]
    assert any("re-validated" in x for x in sc["kept_when_applied"]) and any("decisions" in x for x in sc["not_imported_but_kept_in_the_package"])
    assert any("approval" in x for x in sc["not_carried_unsupported"]) and any("change history" in x for x in sc["not_carried_unsupported"])
    assert "does not move the knowledge state" in sc["claim_limit"]
