"""DOC-02: Business/Technical documents generated on explicit request, through the real routes, the deterministic worker, the blob store and the metadata row. No model, no
provider. What this proves: pinning to one snapshot for both modes, immutable bytes with a verifiable hash, no generation on knowledge import/accept, permission and redaction
scope on every read, failure/limit refusals writing nothing, staleness after the knowledge moves. Not human acceptance of the wording (DOC-01 goldens pin it)."""
from __future__ import annotations

import hashlib

import psycopg2
import pytest

from core.change_intelligence.v2 import document_ast as da
from infra.adapters import v2_business_documents as docs
from infra.adapters.v2_identity import PostgresIdentityStore
from tests.final_loop.test_chg04_review import login
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (qa_env is a fixture)
from tests.pitch_brief.conftest import drain, job_body


def ask(q, client, **body):
    return client.post("/v2/business/documents", json={"workspace_id": q.env.ws, **body})


def state(q, client, aid):
    return client.get(f"/v2/business/documents/{aid}", params={"workspace_id": q.env.ws})


def content(q, client, aid):
    return client.get(f"/v2/business/documents/{aid}/content", params={"workspace_id": q.env.ws})


def doc_jobs(q):
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='document.generate'", (q.env.tenant, q.env.ws))
        n = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s", (q.env.tenant, q.env.ws))
        return n, cur.fetchone()[0]


def test_accepting_knowledge_made_no_document_and_both_modes_pin_the_same_snapshot_with_immutable_verifiable_bytes(qa_env):
    q = qa_env
    assert doc_jobs(q) == (0, 0)                                                                         # the fixture imported, analysed, answered and accepted: zero document work
    r = ask(q, q.admin, modes=["business", "technical"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert [d["mode"] for d in out["documents"]] == ["business", "technical"] and {d["snapshot_id"] for d in out["documents"]} == {out["snapshot_id"]}
    assert all(d["freshness"] == "generating" for d in out["documents"]) and doc_jobs(q) == (2, 0)
    ids = [d["artifact_id"] for d in out["documents"]]
    assert content(q, q.admin, ids[0]).status_code == 409                                              # not available yet: nothing is served
    drain(q.env)
    for aid, mode in zip(ids, ("business", "technical")):
        st = state(q, q.admin, aid).json()
        assert st["freshness"] == "available" and st["mode"] == mode and st["content_hash"].startswith("sha256:") and st["rule_count"] > 0
        c = content(q, q.admin, aid)
        assert c.status_code == 200 and c.headers["content-type"].startswith("text/markdown") and c.headers["x-content-hash"] == st["content_hash"]
        assert "sha256:" + hashlib.sha256(c.content).hexdigest() == st["content_hash"] and da.content_hash(c.text) == st["content_hash"]
        assert c.text.endswith("\n") and "\r" not in c.text
        rec = job_body(q.admin, q.env, aid)
        assert rec["execution_status"] == "COMPLETED" and rec["execution_provider_calls"] == 0
    business, technical = content(q, q.admin, ids[0]).text, content(q, q.admin, ids[1]).text
    assert "rule-standard-manager-review" in business and "rule-standard-manager-review" in technical                # rule ids link the two documents (DOC-01)
    assert out["snapshot_id"] in technical and out["snapshot_id"] not in business and "sha256:" not in business       # pin and hashes are Technical only
    assert doc_jobs(q) == (2, 2)
    again = ask(q, q.admin, modes=["business", "technical"]).json()
    assert [d["artifact_id"] for d in again["documents"]] == ids and all(d["freshness"] == "available" for d in again["documents"]) and doc_jobs(q) == (2, 2)   # same key: nothing new
    listed = q.admin.get("/v2/business/documents", params={"workspace_id": q.env.ws}).json()["documents"]
    assert {d["artifact_id"] for d in listed} == set(ids)


def test_redaction_scope_and_permissions_are_rechecked_on_every_read(qa_env):
    q = qa_env
    r = ask(q, q.req, modes=["business"])
    assert r.status_code == 200, r.text
    assert ask(q, q.req, modes=["technical"]).status_code == 403                                          # Technical needs evidence.read
    tech_admin = ask(q, q.admin, modes=["technical"]).json()["documents"][0]["artifact_id"]
    reviewer = login(q, f"rev-doc-{q.env.nonce}", ("reviewer",))                                            # evidence.read, no knowledge rights
    drain(q.env)
    biz = r.json()["documents"][0]["artifact_id"]
    assert content(q, q.req, biz).status_code == 200 and content(q, q.req, tech_admin).status_code == 403 and state(q, q.req, tech_admin).status_code == 403
    assert content(q, reviewer, tech_admin).status_code == 200
    listed = q.req.get("/v2/business/documents", params={"workspace_id": q.env.ws}).json()["documents"]
    assert {d["artifact_id"] for d in listed} == {biz}                                                     # the requester sees only what it may read
    viewer = login(q, f"viewer-doc-{q.env.nonce}", ("viewer",))
    assert ask(q, viewer, modes=["business"]).status_code == 200 and content(q, viewer, biz).status_code == 200      # project.read may view Business
    anonymous = q.env.admin_client()
    anonymous.cookies.clear()
    assert anonymous.get(f"/v2/business/documents/{biz}/content", params={"workspace_id": q.env.ws}).status_code in (401, 403)
    PostgresIdentityStore(q.env.dsn).revoke_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, q.env.req)
    assert content(q, q.req, biz).status_code in (401, 403)                                                # revoked after generation: the cached document is not served
    assert state(q, q.admin, "doc-nothing").status_code == 404 and content(q, q.admin, "doc-nothing").status_code == 404


def test_invalid_requests_write_nothing_and_a_limit_fails_the_job_without_bytes(qa_env, monkeypatch):
    q = qa_env
    before = doc_jobs(q)
    assert ask(q, q.admin, modes=["poem"]).status_code == 400 and ask(q, q.admin, modes=[]).status_code == 400
    assert ask(q, q.admin, modes=["business"], scope={"paths": ["x"]}).status_code == 422
    assert ask(q, q.admin, modes=["business"], snapshot_id="snap-nope").status_code == 404
    assert ask(q, q.admin, modes=["business"], template_version="99").status_code == 422
    pending = q.admin.get("/v2/business/knowledge", params={"workspace_id": q.env.ws}).json()
    assert doc_jobs(q) == before
    monkeypatch.setattr(docs.da, "MAX_RULES_PER_ARTIFACT", 1)                                               # the fixture holds more than one rule
    r = ask(q, q.admin, modes=["business"], scope={"concept_ids": ["concept-purchase-approval"]})
    assert r.status_code == 200
    aid = r.json()["documents"][0]["artifact_id"]
    drain(q.env)
    st = state(q, q.admin, aid).json()
    assert st["freshness"] == "failed" and st["failure_reason"].startswith("limit_exceeded") and doc_jobs(q)[1] == before[1]
    assert content(q, q.admin, aid).status_code == 409
    assert pending["documents"]["freshness"] == "not_requested"


def test_a_document_of_an_older_knowledge_version_is_served_as_stale_and_a_scoped_document_is_its_own_artifact(qa_env):
    q = qa_env
    full = ask(q, q.admin, modes=["business"]).json()["documents"][0]["artifact_id"]
    scoped = ask(q, q.admin, modes=["business"], scope={"rule_ids": ["rule-standard-manager-review"]}).json()["documents"][0]["artifact_id"]
    assert scoped != full
    drain(q.env)
    assert state(q, q.admin, scoped).json()["rule_count"] == 1 and state(q, q.admin, full).json()["rule_count"] > 1
    with q.env.store._tx() as cur:
        cur.execute("SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND kind='answer_gap'", (q.env.tenant,))
        decision = cur.fetchone()[0]
    assert q.admin.post("/v2/business/decisions/revoke", json={"workspace_id": q.env.ws, "decision_id": decision, "reason": "changed"}).status_code == 200
    drain(q.env)
    st = state(q, q.admin, full).json()
    assert st["freshness"] == "stale" and doc_jobs(q) == (2, 2)                                            # the knowledge moved: no regeneration by itself
    c = content(q, q.admin, full)
    assert c.status_code == 200 and c.headers["x-freshness"] == "stale" and c.headers["x-content-hash"] == st["content_hash"]
    fresh = ask(q, q.admin, modes=["business"]).json()["documents"][0]
    assert fresh["artifact_id"] != full and fresh["snapshot_id"] != st["snapshot_id"] and fresh["freshness"] == "generating"      # an explicit request on the new head is a new artifact


def test_the_document_of_accepted_knowledge_lists_the_persons_decision_and_says_the_disagreement_was_decided(qa_env):
    """Found 2026-10-21 by the journey harness: accepting copies the rules into a new snapshot without the decisions, and documents exist only for accepted knowledge,
    so "Decisions people made" was always empty and a settled disagreement still said a person had to decide."""
    from tests.final_loop.test_qry03_qa import DECISION_QUOTE
    q = qa_env
    out = ask(q, q.admin, modes=["business", "technical"]).json()
    drain(q.env)
    business, technical = (content(q, q.admin, d["artifact_id"]).text for d in out["documents"])
    assert f"“{DECISION_QUOTE}”" in business and "No decision has been recorded." not in business
    assert "disagree. A person decided the business rule:" in business and "A person has to decide which one is the business rule." not in business
    assert "; decided by `rule-capex-finance-config-target`" in technical and ", gap `" in technical
