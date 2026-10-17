"""U1: the per-file source lifecycle is read from the real store and keeps the stages apart:
imported, parsed, analysed (extraction job), claims pending/confirmed, knowledge revision. Stub provider only; no model is called."""
from __future__ import annotations

import base64
from uuid import uuid4

import pytest

from tests.pitch_brief.chain import build, cand_id
from tests.pitch_brief.conftest import harbor_files

APPROVAL = "harbor_demo/config/approval.yaml"


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _life(client, env):
    r = client.get("/v2/sources/lifecycle", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    d = r.json()
    return d, {a["logical_path"]: a for a in d["artifacts"]}


def _find(rows, name):
    return next(v for k, v in rows.items() if k.endswith(name))


def test_every_stage_is_its_own_fact_and_counts_come_from_the_store(env, ch, monkeypatch):
    monkeypatch.delenv("RIPPLE_PROVIDER_MODE", raising=False)
    d, rows = _life(env.req_client(), env)
    assert d["totals"]["imported"] == len(ch.arts) and len(rows) == len(ch.arts), "counts are the stored files, not a fixed number"
    analysed = [p for p, r in rows.items() if r["extraction"]["state"] == "analysed"]
    assert len(analysed) == len(ch.jobs) and 0 < len(analysed) < len(rows), "a file that was never analysed stays not_analysed"
    never = _find(rows, "calculations.py")
    assert never["extraction"]["state"] == "not_analysed" and never["claims"]["total"] == 0 and never["extraction"]["job_id"] is None
    ap = _find(rows, "approval.yaml")
    assert ap["parse"]["state"] == "parsed" and ap["parse"]["observations"] > 0
    assert ap["claims"]["total"] > 0 and ap["claims"]["confirmed"] == 0 and ap["claims"]["pending"] == ap["claims"]["total"]
    assert ap["extraction"]["on_current_revision"] and ap["extraction"]["analysed_source_revision_id"] == d["source_revision_id"]
    assert d["provider"]["mode"] == "NOT_DECLARED", "a model-shaped name is not proof of a live call"
    assert d["knowledge"]["state"] in ("no_knowledge_revision", "built_on_current_sources")
    assert "quote" not in str(d).lower(), "no evidence text in this read"


def test_provider_mode_is_what_the_operator_declared_and_never_inferred(env, ch, monkeypatch):
    for declared, want in (("SIMULATED", "SIMULATED"), ("live", "LIVE"), ("banana", "NOT_DECLARED")):
        monkeypatch.setenv("RIPPLE_PROVIDER_MODE", declared)
        assert _life(env.req_client(), env)[0]["provider"]["mode"] == want


def test_one_confirmed_claim_does_not_make_the_file_confirmed(env, ch):
    before = _find(_life(env.req_client(), env)[1], "approval.yaml")["claims"]
    cid = cand_id(ch, APPROVAL, next(c["predicate"] for c in ch.cands[APPROVAL]))
    r = ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": cid, "rationale": "checked against the policy"})
    assert r.status_code == 200, r.text
    after = _find(_life(env.req_client(), env)[1], "approval.yaml")
    assert after["claims"]["confirmed"] == 1 and after["claims"]["pending"] == before["total"] - 1 and after["claims"]["total"] == before["total"]
    assert after["extraction"]["state"] == "analysed", "analysis state is its own fact"


def test_a_new_revision_turns_an_old_analysis_into_stale_and_offers_no_current_claims(env, ch):
    d0, _ = _life(env.req_client(), env)
    files = harbor_files()
    changed = []
    for p, c in files:
        if p.endswith("approval.yaml"):
            c = c + b"\n# a reviewer note added outside Ripple\n"
        changed.append({"path": p, "content_b64": base64.b64encode(c).decode()})
    prep = ch.admin.post("/v2/sources/refresh/prepare", json={"workspace_id": env.ws, "idempotency_key": uuid4().hex, "files": changed})
    assert prep.status_code == 200, prep.text
    p = prep.json()
    r = ch.admin.post("/v2/sources/refresh/commit", json={"workspace_id": env.ws, "refresh_id": p["refresh_id"], "expected_version": p["base"]["version"],
                                                           "expected_refresh_digest": p["refresh_digest"], "acknowledged_deletions": []})
    assert r.status_code == 200, r.text
    d1, rows = _life(env.req_client(), env)
    assert d1["source_revision_id"] != d0["source_revision_id"]
    ap = _find(rows, "approval.yaml")
    assert ap["extraction"]["state"] == "stale" and ap["extraction"]["on_current_revision"] is False
    assert ap["extraction"]["analysed_source_revision_id"] == d0["source_revision_id"]
    assert ap["claims"]["total"] == 0, "claims of an older revision are not shown as the current claims"
    assert d1["totals"]["stale"] == 1, "only the file that changed is out of date"
    others = [r for k, r in rows.items() if r is not ap and r["extraction"]["state"] != "not_analysed"]
    assert others, "the fixture analysed other files"
    for r in others:      # their bytes are identical at the new revision: the earlier analysis is still valid and says so
        assert r["extraction"]["state"] == "analysed" and r["extraction"]["file_unchanged_since_analysis"] is True, r["logical_path"]
        assert r["extraction"]["on_current_revision"] is False and r["claims"]["total"] > 0
    assert d1["knowledge"]["state"] in ("built_on_older_sources", "no_knowledge_revision")


def test_a_person_without_a_session_or_in_another_workspace_gets_nothing(env, ch):
    import fastapi.testclient as tc
    from infra.judge import app
    anon = tc.TestClient(app, base_url="https://judge.test")
    assert anon.get("/v2/sources/lifecycle", params={"workspace_id": env.ws}).status_code in (401, 403)
    r = env.req_client().get("/v2/sources/lifecycle", params={"workspace_id": "no-such-workspace"})
    assert r.status_code in (403, 404)
