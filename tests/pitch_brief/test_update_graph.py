"""J4: after a real apply (scalar YAML) the graph/timeline show the new revision: changed rule labelled with reason,
source change separated from knowledge decisions, residual sources still visible, duplicate apply does nothing.
Stub providers only (wiring evidence, not live)."""
from __future__ import annotations

import pytest

from tests.pitch_brief.chain import APPROVAL, FINGUIDE, POLICY, build, cand_id
from tests.pitch_brief.conftest import create_job, drain
from tests.pitch_brief.test_loop_feedback import _apply_chain
from tests.pitch_brief.chain import build as _build


@pytest.fixture
def ch(env, monkeypatch):
    return _build(env, monkeypatch, finguide_scope={"category": "all"})


def _graph(env, client):
    r = client.get("/v2/knowledge/graph", params={"workspace_id": env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def test_graph_after_apply_shows_updated_rule_with_reason_and_residual_sources(env, ch, monkeypatch):
    old_id = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    before = _graph(env, ch.admin)
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    after = _graph(env, ch.admin)
    assert after["head"]["source_revision_id"] != before["head"]["source_revision_id"]
    nodes = {n["id"]: n for n in after["nodes"]}
    changed = [n for n in after["nodes"] if n["type"] == "candidate" and n["state"] == "updated"]
    assert len(changed) == 1
    n = changed[0]
    assert n["id"] != old_id and n["detail"]["previous"]["candidate_id"] == old_id
    assert n["detail"]["previous"]["value"] == 1000000 and n["detail"]["value"] == 500000
    assert "NOT carried over" in n["detail"]["update_reason"]
    assert n["detail"]["logical_path"] == APPROVAL
    # provenance is the STORED one: the apply job's own result and the re-extraction job's own payload
    h = n["detail"]["history"]
    assert h["update_kind"] == "value_changed" and h["source_edited_by_apply"] == apj and h["read_again_after_apply"] == apj
    # the old candidate id is not drawn as current knowledge, and nothing was silently merged: other files' rules remain
    assert old_id not in nodes
    # the stub re-extraction found nothing in the other files: they stay listed, with 0 rules disclosed (not hidden)
    cov = {a["logical_path"]: a for a in after["coverage"]["artifacts"]}
    assert cov[POLICY]["candidates"] == 0 and cov[POLICY]["extraction"] == "PENDING_HUMAN"
    assert any(x["type"] == "artifact" and x["label"] == POLICY for x in after["nodes"])
    assert after["coverage"]["updated_candidates"] == 1
    # source change and knowledge decision are separate event categories on the timeline
    tl = ch.admin.get("/v2/knowledge/timeline", params={"workspace_id": env.ws, "node_id": n["id"]}).json()
    cats = [e["category"] for e in tl["events"]]
    assert "source" in cats and "analysis" in cats
    src = [e for e in tl["events"] if e["kind"] == "source_changed"]
    assert src and src[0]["before"]["blob_hash"] != src[0]["after"]["blob_hash"]
    assert src[0]["edited_by_apply"] == apj and "edited by an applied change" in src[0]["why"]
    vc = [e for e in tl["events"] if e["kind"] == "value_changed_in_source"]
    assert vc and vc[0]["change_kind"] == "value_changed" and vc[0]["source_edited_by_apply"] == apj
    assert any(e["kind"] == "extracted" and e["read_again_after_apply"] == apj for e in tl["events"])
    assert not [e for e in tl["events"] if e["category"] == "knowledge" and e["kind"].startswith("decision")]


def test_duplicate_apply_does_not_repeat_provider_calls_or_change_source(env, ch, monkeypatch):
    c, apj, post = _apply_chain(env, ch, monkeypatch)
    head = env.store.head(env.tenant, env.ws).source_revision_id
    calls = post.calls
    appr = ch.admin.get(f"/v2/jobs/{apj}", params={"workspace_id": env.ws}).json()
    payload = appr["payload"]
    jid, r = create_job(c.req, env, "apply", dict(payload))
    drain(env)
    drain(env)
    assert post.calls == calls                                               # no second re-extraction wave
    assert env.store.head(env.tenant, env.ws).source_revision_id == head     # no second source revision
