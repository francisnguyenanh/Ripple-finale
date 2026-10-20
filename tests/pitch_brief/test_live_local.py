"""Live-local mode (offline): the data survives a restart, nothing is seeded, a disarmed server refuses the provider (no stub behind it), arming needs
the approval + budget config + switch to agree, reset is explicit and scoped, and the portable package keeps stub and live apart, verifies, plans and remaps.
No provider is called anywhere: the only AI client in this file is the refusal `disarm_clients` installs. A throwaway database `livelocal_t*` and a temp
home folder are created and removed here; the live-local database a person uses is never touched.
"""
from __future__ import annotations

import importlib
import json
import re
import pathlib
import secrets
import shutil

import psycopg2
import pytest

from tests.pitch_brief import conftest as C
from tools.judge_local import live_local as L
from tools.judge_local import portable_package as P
from tools.judge_local.live_sources import TIERS, build_manifest

_MODS = ("extraction", "gap_answer", "change_draft", "intent_interpret", "explain", "impact_judge", "live_analysis")


def _admin_dsn():
    return C.DSN.replace("dbname=pbrief", "dbname=postgres") if "dbname=pbrief" in C.DSN else C.DSN.split("dbname=")[0] + "dbname=postgres " + " ".join(
        p for p in C.DSN.split() if p.split("=")[0] in ("user", "password", "host", "port"))


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if "pbrief" not in C.DSN and "dbname=" not in C.DSN:
        pytest.skip("needs a disposable PostgreSQL")
    mp = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("livehome")
    mp.setattr(L, "HOME", home)
    mp.setattr(L, "ADMIN_DSN", _admin_dsn())
    mp.setattr(L, "DB", "livelocal_t" + secrets.token_hex(3))
    for k in ("RIPPLE_V2_DSN", "RIPPLE_V2_BLOB_BUCKET", "RIPPLE_PROVIDER_MODE", "RIPPLE_UI_NOTICE", "RIPPLE_UI_NOTICE_DETAILS", "RIPPLE_LIVE_LOCAL_ARM"):
        mp.setenv(k, "x")
    mp.delenv("RIPPLE_LIVE_LOCAL_ARM")
    mp.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(C.REAL_BUDGET_PATH))
    from infra import v2_source_routes
    mp.setattr(v2_source_routes, "_blob_port", v2_source_routes._blob_port)
    for name in _MODS:
        mod = importlib.import_module(f"infra.worker.{name}")
        if hasattr(mod, "_client_for_model"):
            mp.setattr(mod, "_client_for_model", mod._client_for_model)
    yield L
    try:
        L.cmd_reset(type("A", (), {"confirm": L.DB})())
    finally:
        mp.undo()


def _client(password, account="live-admin"):
    from fastapi.testclient import TestClient
    from infra import judge
    c = TestClient(judge.app, base_url="https://judge.test")
    c.headers.update({"Origin": "https://judge.test", "X-Ripple-Judge": "1"})
    assert c.post("/v2/auth/login", json={"account_id": account, "credential": password}).status_code == 200
    return c


def _count(sql, args=()):
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def test_01_create_once_then_persist_across_restart(live, monkeypatch):
    assert live.ensure_database() is True
    store, ids, blobs = live.services()
    issued = live.provision(store, ids)
    assert set(issued) == {"live-admin", "live-requester", "live-approver", "live-reviewer"}
    monkeypatch.setenv("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", issued["live-admin"])
    assert live.main(["import-sources", "--tier", "T6"]) == 0
    head = store.head(live.TENANT, live.WS).source_revision_id
    assert head
    n_art = _count("SELECT count(*) FROM v2_artifact_revisions WHERE tenant_id=%s AND source_revision_id=%s", (live.TENANT, head))
    assert n_art == 6
    # a restart: services are rebuilt from nothing but the database and the blob folder
    assert live.ensure_database() is False                                   # not re-created, not dropped
    store2, ids2, blobs2 = live.services()
    assert live.provision(store2, ids2) == {}                                # no new account, no new password
    assert store2.head(live.TENANT, live.WS).source_revision_id == head
    digest = _count("SELECT count(*) FROM v2_blob_refs WHERE tenant_id=%s", (live.TENANT,))
    assert digest >= 6
    rows = store2.source_revision_view(live.TENANT, live.WS, head)["artifacts"]
    assert len(rows) == 6
    with psycopg2.connect(live.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND source_revision_id=%s", (live.TENANT, head))
        for (h,) in cur.fetchall():
            assert blobs2.read(h)                                            # the bytes are on disk, not in memory
    assert (live.HOME / "blobs").is_dir() and any((live.HOME / "blobs").rglob("*"))
    # the old password still works after the restart; the second import path reused the same account
    assert _client(issued["live-admin"]).get("/v2/auth/me").status_code == 200


def test_02_nothing_is_seeded_and_nothing_simulated(live):
    assert _count("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND kind NOT IN ('source_ingest')", (live.TENANT,)) == 0
    assert _count("SELECT count(*) FROM v2_decisions WHERE tenant_id=%s", (live.TENANT,)) == 0
    assert _count("SELECT coalesce(sum(cost_usd),0) FROM v2_job_cost_ledger WHERE tenant_id=%s", (live.TENANT,)) == 0     # the ingest job costs nothing
    assert _count("SELECT count(*) FROM v2_job_cost_ledger l JOIN v2_jobs j USING (tenant_id, workspace_id, job_id) WHERE l.tenant_id=%s AND j.kind<>'source_ingest'", (live.TENANT,)) == 0
    assert _count("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND execution_model_id IS NOT NULL", (live.TENANT,)) == 0   # no model recorded: nothing ran
    src = pathlib.Path(L.__file__).read_text(encoding="utf-8")
    for forbidden in ("SimInterpreter", "SimWording", "StubClient", "sim_purposes", "chain.build"):
        assert forbidden not in src


def test_03_disarmed_is_the_default_and_refuses_the_provider(live, monkeypatch):
    armed, reason, _ = live.read_approval()
    assert not armed and "ARM" in reason
    store, _, _ = live.services()
    live.apply_mode(store, armed, reason)
    import os
    assert os.environ["RIPPLE_PROVIDER_MODE"] == "LIVE_DISARMED"
    assert _count("SELECT count(*) FROM v2_project_model_policy WHERE project_id=%s AND external_provider='deny'", (live.PROJECT,)) == 1
    for name in _MODS:
        mod = importlib.import_module(f"infra.worker.{name}")
        if hasattr(mod, "_client_for_model"):
            with pytest.raises(RuntimeError, match="not armed"):
                mod._client_for_model("gemini-3.8-flash")
    # the start is recorded for the package export
    assert _count("SELECT count(*) FROM v2_audit WHERE event_type='live_local.start' AND payload->>'mode'='LIVE_DISARMED'") >= 1


def test_04_disarmed_server_shows_the_mode_and_creates_no_provider_job(live, capsys):
    store, ids, _ = live.services()
    live.cmd_set_password(type("A", (), {"account": "live-admin"})())    # the new password is printed to the console only
    pw = re.search(r"new password for live-admin: (\S+)", capsys.readouterr().out).group(1)
    c = _client(pw)
    life = c.get("/v2/sources/lifecycle", params={"workspace_id": live.WS}).json()
    assert life["provider"]["mode"] == "LIVE_DISARMED"
    head = store.head(live.TENANT, live.WS).source_revision_id
    art = store.source_revision_view(live.TENANT, live.WS, head)["artifacts"][0]["artifact_id"]
    from infra.adapters.v2_identity import _digest
    with psycopg2.connect(live.dsn()) as cx, cx.cursor() as cur:
        cur.execute("UPDATE v2_accounts SET credential_digest=%s WHERE account_id='live-requester'", (_digest("req-pw-for-test"),))
    req = _client("req-pw-for-test", "live-requester")
    jid = "job-ll-" + secrets.token_hex(4)
    r = req.post("/v2/jobs", json={"workspace_id": live.WS, "kind": "extraction", "job_id": jid, "payload": {"artifact_id": art, "source_role": "unknown"}})
    # the provider gate (ed0893a) refuses the request itself while disarmed: an explained 409, nothing created, nothing charged
    assert r.status_code == 409 and r.json()["code"] == "provider_disarmed", r.text
    assert _count("SELECT count(*) FROM v2_jobs WHERE job_id=%s", (jid,)) == 0
    assert _count("SELECT coalesce(sum(cost_usd),0) FROM v2_job_cost_ledger WHERE tenant_id=%s", (live.TENANT,)) == 0


def test_05_arming_needs_approval_budget_config_and_switch(live, monkeypatch, tmp_path):
    env = {"RIPPLE_LIVE_LOCAL_ARM": "1", "RIPPLE_BUDGET_CONFIG_PATH": str(C.REAL_BUDGET_PATH)}
    assert live.read_approval(env)[1].startswith("no approval file")
    approval = {"approval_id": "t-1", "total_cap_usd": "9.50", "per_job_cap_usd": "0.30", "roles": {"extraction": "gemini-3.8-flash"}}
    (live.HOME / "APPROVAL.json").write_text(json.dumps(approval), encoding="utf-8")
    armed, why, _ = live.read_approval(env)
    assert not armed and "differ" in why                    # the real config's cap is 12.82, the approval says 9.50: never silently accepted
    cfg = tmp_path / "budget.yaml"
    cfg.write_text(C.REAL_BUDGET_PATH.read_text(encoding="utf-8").replace('total_cap_usd: "12.82"', 'total_cap_usd: "9.50"'), encoding="utf-8")
    env["RIPPLE_BUDGET_CONFIG_PATH"] = str(cfg)
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(cfg))      # the worker's loader reads the process environment, which wins over a path argument
    assert live.read_approval(env)[0] is True, live.read_approval(env)[1]
    approval["roles"] = {"extraction": "gemini-3.5-flash-lite"}
    (live.HOME / "APPROVAL.json").write_text(json.dumps(approval), encoding="utf-8")
    assert "model for extraction differs" in live.read_approval(env)[1]
    approval["roles"], approval["valid_until"] = {"extraction": "gemini-3.8-flash"}, "2020-01-01"
    (live.HOME / "APPROVAL.json").write_text(json.dumps(approval), encoding="utf-8")
    assert "expired" in live.read_approval(env)[1]
    env.pop("RIPPLE_LIVE_LOCAL_ARM")
    approval.pop("valid_until")
    (live.HOME / "APPROVAL.json").write_text(json.dumps(approval), encoding="utf-8")
    assert not live.read_approval(env)[0]                    # without the explicit switch it stays disarmed
    (live.HOME / "APPROVAL.json").write_text("{not json", encoding="utf-8")
    env["RIPPLE_LIVE_LOCAL_ARM"] = "1"
    assert "invalid" in live.read_approval(env)[1]
    (live.HOME / "APPROVAL.json").unlink()


def test_06_serve_refuses_the_protected_ports(live):
    for port in (8765, 8766, 8770, 0):
        with pytest.raises(SystemExit):
            live.cmd_serve(type("A", (), {"port": port})())


def test_07_reset_is_explicit_and_scoped(live, monkeypatch):
    with pytest.raises(SystemExit, match="--confirm"):
        live.cmd_reset(type("A", (), {"confirm": ""})())
    with pytest.raises(SystemExit):
        live.cmd_reset(type("A", (), {"confirm": "something-else"})())
    monkeypatch.setattr(L, "DB", "rehearsal")
    with pytest.raises(SystemExit, match="livelocal"):
        live.ensure_database()
    with pytest.raises(SystemExit, match="livelocal"):
        live.cmd_reset(type("A", (), {"confirm": "rehearsal"})())
    monkeypatch.setattr(L, "ADMIN_DSN", L.ADMIN_DSN.replace("host=127.0.0.1", "host=10.1.2.3"))
    with pytest.raises(SystemExit, match="loopback"):
        live.ensure_database()


# ------------------------------------------------------------------------------------------------------------------ portable package
def _fixture_package(live, tmp_path, with_quote=True):
    """Export of the REAL import (sources + observations); one extraction row is authored here only to exercise the plumbing of verify/plan/remap."""
    store, _, blobs = live.services()
    out = tmp_path / "pkg"
    man = P.export(live.dsn(), blobs, live.TENANT, live.WS, out)
    assert man["counts"]["artifacts"] == 6 and man["counts"]["observations"] > 0
    assert man["authority_transferable"] is False
    rows = [json.loads(x) for x in (out / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    # deterministic choice (artifact and observation ids are random per import, so "the first observation" was not): the first line of the lexically smallest prose chunk
    ob = min((o for o in rows if o["kind"] == "text_chunk" and isinstance(o["raw_value"], str) and o["raw_value"].strip()), key=lambda o: (o["raw_value"], o["artifact_id"]))
    first_line = next(x.strip() for x in ob["raw_value"].splitlines() if x.strip())
    quote = first_line[:40] if with_quote else "this quote does not exist anywhere"
    row = {"job_id": "job-x", "kind": "extraction", "pinned_source_revision_id": ob["source_revision_id"], "payload": {"artifact_id": ob["artifact_id"]},
           "execution_status": "COMPLETED", "execution_result": {"candidates": [{"evidence": [{"quote": quote}]}]}, "execution_model_id": "gemini-3.8-flash",
           "execution_prompt_hash": "ph1", "execution_schema_hash": "sh1", "execution_usage": {}, "execution_cost_usd": "0.01", "execution_provider_calls": 1}
    (out / "extractions.jsonl").write_bytes(P._canon(row))
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    m["integrity"] = P._integrity(out)
    (out / "manifest.json").write_bytes(P._canon(m))
    return out


def test_08_export_refuses_a_database_that_was_not_live(live, tmp_path):
    store, _, blobs = live.services()
    store.create_workspace("other-t", "other-p", "other-ws")          # a workspace nobody started through live_local (the audit log is append-only)
    with pytest.raises(SystemExit, match="no live-local start"):
        P.export(live.dsn(), blobs, "other-t", "other-ws", tmp_path / "a")
    with psycopg2.connect(live.dsn()) as cx, cx.cursor() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES ('other-t','other-ws','live_local.start','operator','{\"mode\":\"SIMULATED\"}')")
    with pytest.raises(SystemExit, match="not live"):
        P.export(live.dsn(), blobs, "other-t", "other-ws", tmp_path / "b")


def test_09_package_verifies_detects_tampering_and_carries_no_secrets(live, tmp_path):
    pkg = _fixture_package(live, tmp_path)
    r = P.verify(pkg)
    assert r["ok"], r
    keys = {k.lower() for n in P.FILES for k in P.package_keys(pkg / n)}
    assert not keys & P.FORBIDDEN_KEYS and not keys & {"account_id", "roles", "credential_digest"}
    blob = next((pkg / "blobs").iterdir())
    blob.write_bytes(blob.read_bytes() + b"x")
    assert not P.verify(pkg)["ok"]
    bad = _fixture_package(live, tmp_path / "second", with_quote=False)
    errs = P.verify(bad)["errors"]
    assert any("quote is not found" in e for e in errs)


def test_10_plan_reuses_only_compatible_outputs_and_transfers_no_authority(live, tmp_path):
    pkg = _fixture_package(live, tmp_path)
    ok = P.plan(pkg, {"extraction": "gemini-3.8-flash"}, {"ph1"}, {"sh1"})
    assert [j["job_id"] for j in ok["reuse"]] == ["job-x"] and not ok["regenerate"]
    assert ok["authority_transferred"] is False and any("canary" in t for t in ok["target_still_needs"])
    for models, ph, sh, word in (({"extraction": "gemini-3.5-flash-lite"}, {"ph1"}, {"sh1"}, "model"), ({"extraction": "gemini-3.8-flash"}, {"zz"}, {"sh1"}, "prompt"),
                                 ({"extraction": "gemini-3.8-flash"}, {"ph1"}, {"zz"}, "schema"), ({}, None, None, "no extraction model")):
        d = P.plan(pkg, models, ph, sh)
        assert d["regenerate"] and not d["reuse"] and word in " ".join(d["regenerate"][0]["reasons"])


def test_11_remap_rewrites_every_reference_and_still_verifies(live, tmp_path):
    pkg = _fixture_package(live, tmp_path)
    src = json.loads((pkg / "sources.json").read_text(encoding="utf-8"))
    rev = src["revisions"][-1]["revision_id"]
    art = src["artifacts"][0]["artifact_id"]
    res = P.remap(pkg, tmp_path / "mapped", {rev: "tgt-rev-1", art: "tgt-art-1", "job-x": "tgt-job-1"})
    assert res["ok"], res
    text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "mapped").glob("*.json*"))
    assert rev not in text and "tgt-rev-1" in text and "tgt-art-1" in text and "tgt-job-1" in text
    with pytest.raises(SystemExit, match="already exists"):          # never overwrite an earlier output
        P.remap(pkg, tmp_path / "mapped", {})
    # a reference to something that is not in the package is reported, not hidden
    broken = tmp_path / "broken"
    shutil.copytree(pkg, broken)
    row = json.loads((broken / "extractions.jsonl").read_text(encoding="utf-8"))
    row["pinned_source_revision_id"] = "rev-not-in-package"
    (broken / "extractions.jsonl").write_bytes(P._canon(row))
    m = json.loads((broken / "manifest.json").read_text(encoding="utf-8"))
    m["integrity"] = P._integrity(broken)
    (broken / "manifest.json").write_bytes(P._canon(m))
    assert any("unknown revision" in e for e in P.verify(broken)["errors"])


def test_12_source_tiers_are_eligible_and_hashed():
    for tier, n in (("T6", 6), ("T9", 9), ("T14", 14), ("T9C", 12), ("T9C+", 13)):
        m = build_manifest(tier)
        assert len(m["files"]) == n == m["expected_initial_extraction_jobs"] and m["stub_facts_copied"] is False
        assert all(f["sha256"].startswith("sha256:") and f["parser"] == "supported" for f in m["files"])
        assert not any(f["path"].split("/")[1] in ("evaluator_private", "authoring", "reports") for f in m["files"])
    assert set(TIERS["T6"]) <= set(TIERS["T9"])


def test_13_wave_causality_comparison_is_pure_and_names_the_explanation():
    if not (pathlib.Path(__file__).resolve().parents[2] / "tools" / "pitch_cloud" / "08_wave_causality.py").exists():
        pytest.skip("operator cloud tooling (tools/pitch_cloud) is not part of the release package")
    W = importlib.import_module("tools.pitch_cloud.08_wave_causality")
    assert W.split_wave_id("reanalysis-apply-job-apply-1-art-7", ["job-apply-1"]) == ("job-apply-1", "art-7")
    assert W.split_wave_id("reanalysis-apply-other-art-7", ["job-apply-1"]) == (None, None)
    arts = {f"a{i}" for i in range(6)}
    assert W.compare_wave(arts, sorted(arts))["verdict"] == "WAVE_EQUALS_PUBLISHED_ARTIFACTS"
    assert W.compare_wave(arts, sorted(arts) + ["a6", "a7"])["verdict"] == "EXTRA_JOBS_NOT_IN_REVISION"
    assert W.compare_wave(arts | {"a6", "a7"}, sorted(arts))["missing"] == ["a6", "a7"]       # 8 artifacts published, 6 jobs: a partial wave
    assert W.compare_wave(arts, sorted(arts) + ["a0"])["duplicates"] == ["a0"]
    src = pathlib.Path(W.__file__).read_text(encoding="utf-8")
    assert "INSERT" not in src and "UPDATE" not in src and "DELETE" not in src and "readonly=True" in src


def test_14_a_file_with_no_observations_is_never_proposed(monkeypatch, tmp_path):
    """A file that parses to zero observations cannot yield an accepted quote, so a job on it costs money for nothing and every later wave would pay for it too."""
    from tools.judge_local import live_sources as S
    root = tmp_path / "harbor_demo" / "input"
    (root / "app").mkdir(parents=True)
    (root / "app" / "empty.py").write_bytes(b"import os" + bytes([10]))   # only an import: nothing to observe
    monkeypatch.setitem(S.TIERS, "TX", ("app/empty.py",))
    with pytest.raises(SystemExit, match="no observations"):
        S.build_manifest("TX", root)
    for tier in ("T6", "T9", "T14", "T9C", "T9C+"):
        assert all(f["observations"] > 0 for f in S.build_manifest(tier)["files"])


def test_15_code_is_in_only_the_code_tiers_with_the_implementation_role():
    from tools.judge_local import live_sources as S
    for tier in ("T6", "T9", "T14"):
        assert not any(f["path"].startswith("harbor_demo/app/") for f in S.build_manifest(tier)["files"])
    m = S.build_manifest("T9C")
    code = {f["path"].split("/", 1)[1]: f for f in m["files"] if f["path"].startswith("harbor_demo/app/")}
    assert sorted(code) == ["app/config_loader.py", "app/emergency.py", "app/routing.py"]                   # the smallest set that proves the story
    assert all(f["source_role"] == "implementation" and f["observations"] >= 2 and f["format"] == ".py" for f in code.values())
    assert all(f["source_role"] == "unknown" for f in m["files"] if f["path"] not in {"harbor_demo/" + k for k in code})
    assert [f["path"] for f in S.build_manifest("T9C+")["files"]][-1] == "harbor_demo/app/calculations.py"
    assert set(S.TIERS["T9"]) < set(S.TIERS["T9C"]) < set(S.TIERS["T9C+"])


def test_16_t9c_plus_manifest_names_path_hash_role_parser_version_and_observations_and_reads_no_private_tree():
    m = build_manifest("T9C+")
    assert len(m["files"]) == 13 and m["expected_initial_extraction_jobs"] == 13
    for f in m["files"]:
        assert f["path"].startswith("harbor_demo/") and f["sha256"].startswith("sha256:") and len(f["sha256"]) == 71
        assert f["parser_version"] and f["parse_status"] == "parsed" and f["diagnostics"] == [] and f["observations"] > 0 and f["source_role"]
        assert not any(w in f["path"] for w in ("evaluator_private", "authoring", "gold", "reports"))
    assert {f["parser_version"] for f in m["files"] if f["path"].startswith("harbor_demo/app/")} == {"1.1.0"}
    assert all(f["source_role"] == "implementation" for f in m["files"] if f["path"].startswith("harbor_demo/app/"))
    assert m["coverage"]["covers"]


@pytest.mark.parametrize("origin", ["http://a-b-c-d.trycloudflare.com", "https://*.trycloudflare.com", "https://abcd.trycloudflare.com/x", "https://abcd.example.com",
                                    "https://abcd.trycloudflare.com.evil.io", "https://a.trycloudflare.com"])
def test_17_the_tunnel_origin_is_one_exact_trycloudflare_hostname(live, monkeypatch, origin):
    monkeypatch.setenv("RIPPLE_LIVE_LOCAL_ORIGIN", origin)
    monkeypatch.delenv("RIPPLE_JUDGE_ORIGIN", raising=False)
    with pytest.raises(live.LiveLocalError):
        live.configure_env(None)


def test_18_a_valid_tunnel_origin_becomes_the_only_accepted_origin_and_unset_means_none(live, monkeypatch):
    monkeypatch.setenv("RIPPLE_LIVE_LOCAL_ORIGIN", "https://inkjet-math-bradley-unique.trycloudflare.com")
    live.configure_env(None)
    import os
    assert os.environ["RIPPLE_JUDGE_ORIGIN"] == "https://inkjet-math-bradley-unique.trycloudflare.com"
    monkeypatch.delenv("RIPPLE_LIVE_LOCAL_ORIGIN")
    live.configure_env(None)
    assert "RIPPLE_JUDGE_ORIGIN" not in os.environ


def test_19_the_reviewer_account_is_a_reviewer_and_a_viewer_stays_read_only():
    from infra.adapters.v2_policy import ROLE_PERMISSIONS
    from tools.judge_local import live_local as L
    assert dict(L.ACCOUNTS)["live-reviewer"] == ("reviewer",) and ROLE_PERMISSIONS["viewer"] == frozenset({"project.read", "request.assistant.ask"})
    for p in ("source.import", "intent.create", "change.approve", "change.apply", "knowledge.confirm", "source.restore", "membership.manage",
              "verification.contract.set"):
        assert p not in ROLE_PERMISSIONS["reviewer"] and p not in ROLE_PERMISSIONS["viewer"]
    assert {"change.review", "ai.use", "evidence.read"} <= ROLE_PERMISSIONS["reviewer"]


def test_19b_an_existing_viewer_reviewer_account_is_moved_to_the_reviewer_role_once_and_nothing_else_changes(live):
    from tools.judge_local import live_local as L
    live.ensure_database()
    store, ids, _ = live.services()
    live.provision(store, ids)
    ids.grant_membership(L.TENANT, L.PROJECT, L.WS, "live-reviewer", ("viewer",))        # the account as it was created before the role existed
    again = live.provision(store, ids)
    assert again == {}                                                                    # no new account, no new password
    with store._tx() as cur:
        cur.execute("SELECT roles FROM v2_memberships WHERE tenant_id=%s AND workspace_id=%s AND account_id='live-reviewer'", (L.TENANT, L.WS))
        assert list(cur.fetchone()[0]) == ["reviewer"]
        cur.execute("SELECT count(*) FROM v2_accounts WHERE account_id='live-reviewer'")
        assert cur.fetchone()[0] == 1


def test_20_the_driver_reads_the_arming_state_the_server_declared_and_refuses_to_run_without_it(live, monkeypatch, capsys):
    from tools.judge_local import live_run as R
    live.ensure_database()
    live.record_start("LIVE_DISARMED")
    assert R._server_armed() is False
    live.record_start("LIVE")
    assert R._server_armed() is True
    live.record_start("LIVE_DISARMED")
    monkeypatch.setattr(R, "_env", lambda: live.services()[0])
    monkeypatch.setattr(R, "_state", lambda: {"roles": {}, "halt": None})
    assert R.cmd_canary(None) == 2 and "not LIVE" in capsys.readouterr().out
    monkeypatch.setattr(R, "_state", lambda: {"roles": {}, "halt": "canary extraction FAILED: x"})
    assert R.cmd_rest(None) == 3                                                    # a HALT in force admits nothing
    monkeypatch.setattr(R, "_state", lambda: {"roles": {"extraction": {"status": "NOT_OBSERVED"}}, "halt": None})
    assert R.cmd_rest(None) == 2                                                    # no extraction canary PASS: nothing is admitted
