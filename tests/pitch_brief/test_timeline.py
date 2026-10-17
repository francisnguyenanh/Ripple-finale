"""J2: per-node timeline - decision/rationale/actor, knowledge vs source separation, lineage across re-extraction
(new candidate ids), no merging of different rules. Stub provider only."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from infra.worker import extraction
from tests.pitch_brief.chain import APPROVAL, POLICY, build, cand_id
from tests.pitch_brief.conftest import EXTRACT_MODEL, create_job, drain, job_body


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _tl(client, env, node):
    r = client.get("/v2/knowledge/timeline", params={"workspace_id": env.ws, "node_id": node})
    return r


class Reextract:
    """Second extraction of approval.yaml reading a different value (a model re-reading changed text)."""
    def __init__(self, value):
        self.calls, self.value = 0, value
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kw):
        self.calls += 1
        cands = [{"subject": "Manager approval", "predicate": "standard_manager_approval_threshold",
                  "value": self.value, "unit": "JPY", "modality": "must", "scope": {"order_type": "standard"},
                  "exceptions": [], "outcome": None,
                  "quotes": ["routing_rules.standard_manager_threshold_jpy: 1000000"], "rationale": "stub"}]
        return SimpleNamespace(text=json.dumps({"candidates": cands}), model_version=EXTRACT_MODEL,
                               candidates=[SimpleNamespace(finish_reason="STOP")],
                               usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=30,
                                                              thoughts_token_count=None, total_token_count=70))


def test_confirm_event_has_actor_rationale_and_is_knowledge_not_source(env, ch):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    r = ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": tid, "rationale": "matches policy"})
    assert r.status_code == 200, r.text
    t = _tl(ch.admin, env, tid).json()
    dec = [e for e in t["events"] if e["kind"] == "decision:confirm"]
    assert len(dec) == 1 and dec[0]["actor"] == env.admin and dec[0]["why"] == "matches policy"
    assert dec[0]["category"] == "knowledge" and "source file is not changed" in dec[0]["note"]
    assert dec[0]["state"] == "active" and isinstance(dec[0]["epoch"], int)
    assert not [e for e in t["events"] if e["category"] == "source"]       # no source change happened


def test_lineage_survives_reextraction_with_new_candidate_id_and_does_not_merge_other_rules(env, ch, monkeypatch):
    old = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    policy_rule = cand_id(ch, POLICY, "manager_approval_required_from")
    assert ch.admin.post("/v2/knowledge/confirm", json={"workspace_id": env.ws, "claim_id": old,
                                                        "rationale": "first"}).status_code == 200
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: Reextract(500000))
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    body = job_body(ch.admin, env, jid)
    assert body["execution_status"] == "COMPLETED", body
    new = body["execution_result"]["candidates"][0]["candidate_id"]
    assert new != old
    t = _tl(ch.admin, env, new).json()
    assert set(t["lineage_candidate_ids"]) == {old, new}
    kinds = [e["kind"] for e in t["events"]]
    assert kinds.count("extracted") == 2
    change = [e for e in t["events"] if e["kind"] == "value_changed_in_source"]
    assert change and change[0]["before"]["value"] == 1000000 and change[0]["after"]["value"] == 500000
    # the earlier human confirmation is still reachable from the NEW id through the lineage (and flagged as on the old id)
    dec = [e for e in t["events"] if e["kind"] == "decision:confirm"]
    assert dec and dec[0]["candidate_id"] == old
    # a different rule that merely shares text/number stays separate
    other = _tl(ch.admin, env, policy_rule).json()
    assert old not in other["lineage_candidate_ids"] and new not in other["lineage_candidate_ids"]


def test_timeline_denials_and_unknown_node(env, ch):
    tid = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    assert _tl(ch.admin, env, "cand-nope").status_code == 404
    from fastapi.testclient import TestClient
    from infra import judge
    anon = TestClient(judge.app, base_url="https://judge.test")
    assert anon.get("/v2/knowledge/timeline", params={"workspace_id": env.ws, "node_id": tid}).status_code in (401, 403)
    assert _tl(ch.admin, env, tid).status_code == 200
    r = ch.admin.get("/v2/knowledge/timeline", params={"workspace_id": "ws-other", "node_id": tid})
    assert r.status_code in (401, 403, 404) and "Manager" not in r.text


class ReextractScope(Reextract):
    """Same value as before, only the scope differs (the AI wrote the scope another way)."""
    def _gen(self, **kw):
        r = super()._gen(**kw)
        obj = json.loads(r.text)
        obj["candidates"][0]["scope"] = {"order_type": "standard", "currency": "JPY"}
        r.text = json.dumps(obj)
        return r


def test_a_scope_only_reread_is_not_called_a_change_made_by_an_apply(env, ch, monkeypatch):
    """Found 2026-10-08: six rules re-read with a different scope were labelled "Updated after a change was applied". Nothing was applied
    here: no apply job edited the file and the re-extraction was asked for by a person, so the stored provenance names no apply."""
    old = cand_id(ch, APPROVAL, "standard_manager_approval_threshold")
    monkeypatch.setattr(extraction, "_client_for_model", lambda m: ReextractScope(1000000))
    jid, r = create_job(env.req_client(), env, "extraction", {"artifact_id": ch.arts[APPROVAL], "source_role": "unknown"})
    assert r.status_code == 200, r.text
    assert ch.admin.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": env.ws}).status_code == 200
    drain(env)
    new = job_body(ch.admin, env, jid)["execution_result"]["candidates"][0]["candidate_id"]
    g = ch.admin.get("/v2/knowledge/graph", params={"workspace_id": env.ws}).json()
    n = next(x for x in g["nodes"] if x["id"] == new)
    assert n["state"] == "updated" and n["detail"]["previous"]["candidate_id"] == old
    h = n["detail"]["history"]
    assert h == {"update_kind": "scope_only", "source_edited_by_apply": None, "read_again_after_apply": None, "file_unchanged_since_read": False}
    ev = [e for e in _tl(ch.admin, env, new).json()["events"] if e["kind"] == "value_changed_in_source"]
    assert ev and ev[0]["change_kind"] == "scope_only" and ev[0]["source_edited_by_apply"] is None
    assert "value is unchanged" in ev[0]["why"]
