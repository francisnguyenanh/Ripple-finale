"""FB-R1 / FS-R1 (local build): committed rows -> minimal opaque hints in a sink through a durable checkpoint. Memory sink only; nothing here proves Firestore rules, listener
delivery or latency. What this proves: hints carry no business content (no job id, artifact id, rule text, amount); one feed per workspace; revisions are monotonic and an older
revision is ignored by the sink; a sink outage advances nothing and the next pass replays the same rows (idempotent); a repeated pass projects nothing new; the lag is reported;
projecting never creates jobs, documents or model calls.
"""
from __future__ import annotations

import json

import pytest

from infra.adapters import v2_projection as pj
from tests.final_loop.test_doc02_documents import ask, doc_jobs
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401
from tests.pitch_brief.conftest import drain


def test_hints_are_opaque_monotonic_idempotent_and_an_outage_replays(qa_env):
    q = qa_env
    sink = pj.MemorySink()
    before = pj.pending(q.env.store, q.env.tenant, q.env.ws)
    assert before["lag"]["outbox_rows"] > 0                                                                               # the fixture already committed knowledge (outbox rows exist)
    out = pj.project_once(q.env.store, sink, q.env.tenant, q.env.ws)
    assert out["written"] == out["hints"] > 0 and out["lag"]["outbox_rows"] == 0 and out["scope"].startswith("ws/")
    docs = sink.docs[out["scope"]]
    blob = json.dumps(docs)
    assert q.env.ws not in blob and q.env.tenant not in blob and "rule-" not in blob and "jpy" not in blob.lower()
    assert all(set(h) == {"schema", "key", "family", "revision", "changed_at", "expires_at"} and h["schema"] == "ripple-hint/1" for h in docs.values())
    assert sorted(h["revision"] for h in docs.values()) == list(range(1, len(docs) + 1))
    again = pj.project_once(q.env.store, sink, q.env.tenant, q.env.ws)
    assert again["hints"] == 0 and again["written"] == 0 and sink.puts == out["hints"]                                  # nothing new: nothing written
    jobs_before = doc_jobs(q)
    r = ask(q, q.admin, modes=["business"])
    assert r.status_code == 200
    aid = r.json()["documents"][0]["artifact_id"]
    drain(q.env)
    pend = pj.pending(q.env.store, q.env.tenant, q.env.ws)
    assert pend["lag"]["documents"] == 1 and pend["lag"]["outbox_rows"] >= 1
    sink.outage = True
    with pytest.raises(pj.SinkUnavailable):
        pj.project_once(q.env.store, sink, q.env.tenant, q.env.ws)
    assert pj.pending(q.env.store, q.env.tenant, q.env.ws)["lag"] == pend["lag"]                                           # the checkpoint did not move
    sink.outage = False
    out2 = pj.project_once(q.env.store, sink, q.env.tenant, q.env.ws)
    assert out2["written"] == out2["hints"] >= 2 and out2["lag"] == {"outbox_rows": 0, "documents": 0}
    doc_hints = [h for h in sink.docs[out["scope"]].values() if h["family"] == "document"]
    assert len(doc_hints) == 1 and aid not in json.dumps(doc_hints) and doc_hints[0]["key"] == pj.opaque_key(q.env.tenant, q.env.ws, "document", aid)
    stale = {**doc_hints[0], "revision": 0}
    assert sink.put(out["scope"], stale["key"], stale) is False and sink.ignored_older == 1                             # an older revision never overwrites
    assert doc_jobs(q) == (jobs_before[0] + 1, jobs_before[1] + 1)                                                        # projecting created no job and no document
    st = q.admin.get("/v2/projection/status", params={"workspace_id": q.env.ws}).json()
    assert st["lag"] == {"outbox_rows": 0, "documents": 0} and st["checkpoint"]["projected_count"] == out["written"] + out2["written"] and st["sink"] == "off"
    assert q.admin.post("/v2/projection/run", json={"workspace_id": q.env.ws}).status_code == 503                           # sink off: nothing runs
    assert q.req.post("/v2/projection/run", json={"workspace_id": q.env.ws}).status_code == 403


def test_the_memory_sink_route_runs_a_pass_and_two_workspaces_never_share_a_scope(qa_env, monkeypatch):
    q = qa_env
    monkeypatch.setenv("RIPPLE_PROJECTION_SINK", "memory")
    r = q.admin.post("/v2/projection/run", json={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    assert r.json()["written"] > 0 and r.json()["lag"]["outbox_rows"] == 0
    assert pj.scope_for(q.env.tenant, q.env.ws) != pj.scope_for(q.env.tenant, q.env.ws + "-other") and pj.scope_for("t1", "w") != pj.scope_for("t2", "w")


# ---- FS-C1 conversation read model and FS-O1 offline reference manifest (local, mock sink; offline mode off by default) ------------------------------------------------

def test_the_conversation_read_model_is_redacted_per_profile_and_the_offline_manifest_is_gated(qa_env, monkeypatch):
    q = qa_env
    from tests.final_loop.test_aix_d1_variants import base_document
    from tests.final_loop.test_aix_k2_interview import answer as ivw_answer, start as ivw_start
    from tests.final_loop.test_kno07_answer_interpret import actor_gap
    g = actor_gap(q)
    sid = ivw_start(q, q.admin, g["gap_id"]).json()["session_id"]
    assert ivw_answer(q, q.admin, sid, "We do not know yet.").status_code == 200                                           # an answered turn exists; its text must never be projected
    sink = pj.MemorySink()
    red = pj.project_conversations(q.env.store, sink, q.env.tenant, q.env.ws, profile="redacted")
    ana = pj.project_conversations(q.env.store, sink, q.env.tenant, q.env.ws, profile="analyst")
    assert red["documents"] >= 1 and ana["documents"] >= 1 and red["scope"] != ana["scope"]
    red_docs, ana_docs = json.dumps(sink.docs[red["scope"]]), json.dumps(sink.docs[ana["scope"]])
    assert "We do not know yet" not in red_docs and "We do not know yet" not in ana_docs and sid not in red_docs             # never the person's words, never the raw session id
    assert "question" not in red_docs and "Who acts" in ana_docs                                                           # the analyst profile carries the server's question only
    assert pj.project_conversations(q.env.store, sink, q.env.tenant, q.env.ws, profile="redacted")["documents"] == 0        # nothing new: nothing written
    with pytest.raises(ValueError):
        pj.project_conversations(q.env.store, sink, q.env.tenant, q.env.ws, profile="raw")
    assert q.admin.post("/v2/projection/conversations", json={"workspace_id": q.env.ws, "profile": "analyst"}).status_code == 503   # sink off
    aid = base_document(q, q.admin)
    r = q.admin.get(f"/v2/business/documents/{aid}/offline-manifest", params={"workspace_id": q.env.ws, "consent": "true"})
    assert r.status_code == 503 and r.json()["code"] == "offline_reference_off"                                             # disabled by default
    monkeypatch.setenv("RIPPLE_OFFLINE_REFERENCE", "trusted-device")
    r = q.admin.get(f"/v2/business/documents/{aid}/offline-manifest", params={"workspace_id": q.env.ws})
    assert r.status_code == 409 and r.json()["code"] == "consent_required"
    r = q.admin.get(f"/v2/business/documents/{aid}/offline-manifest", params={"workspace_id": q.env.ws, "consent": "true"})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["current_authority_checked"] is False and "current authority not checked" in m["label"] and m["content_hash"].startswith("sha256:") and m["expires_at"] > m["downloaded_at"]
    assert "no approval" in " ".join(m["rules"]) and m["purge_on"] == ["logout", "account_switch", "workspace_switch", "expiry"]
    assert q.req.get(f"/v2/business/documents/{aid}/offline-manifest", params={"workspace_id": q.env.ws, "consent": "true"}).status_code in (200, 403)   # the base document's permission decides
