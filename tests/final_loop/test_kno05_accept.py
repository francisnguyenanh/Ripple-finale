"""KNO-05: accepting business knowledge, its history, diff and revocation, through the real routes and store. No provider, no model call, no document code."""
from __future__ import annotations

import psycopg2
import pytest

from infra.adapters.v2_business import analyse_workspace, business_diff, business_view, gap_queue, knowledge_status
from infra.adapters.v2_postgres import PostgresV2Store
from tests.final_loop.test_kno01_business_store import counts, workspace_row, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno03_graph_gaps import seed
from tests.pitch_brief.conftest import drain


def answer(client, env, gap, ans, quote="decided"):
    pv = client.post("/v2/business/gaps/answer-preview", json={"workspace_id": env.ws, "gap_id": gap["gap_id"], "answer": ans}).json()
    r = client.post("/v2/business/gaps/answer", json={"workspace_id": env.ws, "gap_id": gap["gap_id"], "answer": ans, "quote": quote, "preview_hash": pv["preview_hash"]})
    assert r.status_code == 200, r.text
    return r.json()


def pending_id(env):
    return business_view(env.store, env.tenant, env.ws)["pending"]["snapshot"]["snapshot_id"]


def conflict_gap(env):
    return next(g for g in gap_queue(env.store, env.tenant, env.ws)["gaps"] if g["kind"] == "conflicting_facts")


@pytest.fixture
def analysed(env, ws):
    seed(env, ws)
    analyse_workspace(env.store, env.tenant, env.ws, actor_id=env.admin, idempotency_key="a1")


def accept(client, env, snapshot_id=None, **extra):
    return client.post("/v2/business/accept", json={"workspace_id": env.ws, "snapshot_id": snapshot_id or pending_id(env), **extra})


def test_critical_open_question_needs_an_acknowledgement_and_the_version_says_partial(env, analysed):
    admin = env.admin_client()
    before = (counts(env), workspace_row(env))
    r = accept(admin, env)
    assert r.status_code == 409 and r.json()["code"] == "open_critical_gaps" and (counts(env), workspace_row(env)) == before
    r = accept(admin, env, acknowledged_open_gaps=True)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["business_revision"] == 1 and out["partial"] is True and out["coverage"]["critical_open"] == 1 and out["coverage"]["acknowledged_open_gaps"] is True
    status = admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()
    assert status["accepted"]["business_revision"] == 1 and status["pending"] is None and status["accepted"]["questions"]["critical_open"] == 1


def test_accept_after_answers_survives_restart_with_history_and_a_semantic_diff(env, analysed):
    admin = env.admin_client()
    assert accept(admin, env, acknowledged_open_gaps=True).status_code == 200                          # revision 1 (partial)
    answer(admin, env, conflict_gap(env), {"choose_rule_id": "rule-capex-finance-config"}, "2 million is the rule.")
    out = accept(admin, env).json()                                                                    # the conflict is answered: nothing critical is left
    assert out["business_revision"] == 2 and out["partial"] is True and out["coverage"]["critical_open"] == 0
    fresh = PostgresV2Store(env.dsn, env.blobs)                                                        # restart
    view = business_view(fresh, env.tenant, env.ws)
    assert view["business_head"]["business_revision"] == 2 and view["accepted"]["snapshot"]["coverage"]["from_snapshot_id"]
    st = knowledge_status(fresh, env.tenant, env.ws)
    assert [h["business_revision"] for h in st["history"]] == [2, 1] and st["accepted"]["target_rules"] == 1
    diff = admin.get("/v2/business/diff", params={"workspace_id": env.ws, "from_revision": 1, "to_revision": 2}).json()
    assert [r["rule_id"] for r in diff["rules_added"]] == ["rule-capex-finance-config-target"] and diff["rules_removed"] == []
    assert any(q["after"] == "RESOLVED" for q in diff["questions_changed"]) and len(diff["decisions_added"]) == 1
    assert admin.get("/v2/business/diff", params={"workspace_id": env.ws, "from_revision": 1, "to_revision": 9}).status_code == 404
    assert business_diff(fresh, env.tenant, env.ws, 1, 1)["rules_changed"] == []


def test_replayed_accept_is_the_same_result_and_a_stale_proposal_is_refused(env, analysed):
    admin = env.admin_client()
    pid = pending_id(env)
    first = accept(admin, env, pid, acknowledged_open_gaps=True).json()
    n = counts(env)
    again = accept(admin, env, pid, acknowledged_open_gaps=True).json()
    assert again["replayed"] is True and again["business_revision"] == first["business_revision"] == 1 and counts(env) == n
    answer(admin, env, conflict_gap(env), {"choose_rule_id": "rule-capex-finance-policy"})
    old = accept(admin, env, pid)                                                                        # the previous proposal is no longer the candidate
    assert old.status_code == 200 and old.json()["replayed"] is True                                    # what was accepted is returned, not accepted twice
    other = accept(admin, env, "analysis-not-a-snapshot")
    assert other.status_code == 409 and other.json()["code"] == "not_latest_pending"
    assert business_view(env.store, env.tenant, env.ws)["business_head"]["business_revision"] == 1


def test_failure_inside_the_acceptance_changes_nothing(env, analysed, monkeypatch):
    admin = env.admin_client()
    pid = pending_id(env)
    before = (counts(env), workspace_row(env), business_view(env.store, env.tenant, env.ws)["business_head"])

    def boom(*a, **k):
        raise RuntimeError("injected commit/outbox failure")
    with monkeypatch.context() as m:                              # only this patch is undone; the fixture's environment stays
        m.setattr(PostgresV2Store, "_record_commit", boom)
        from infra.adapters.v2_business import accept_snapshot
        with pytest.raises(RuntimeError):
            accept_snapshot(env.store, env.tenant, env.ws, actor_id=env.admin, snapshot_id=pid, acknowledged_open_gaps=True)
    assert (counts(env), workspace_row(env), business_view(env.store, env.tenant, env.ws)["business_head"]) == before
    assert accept(admin, env, pid, acknowledged_open_gaps=True).status_code == 200                       # and the same request works afterwards


def test_source_moved_after_the_proposal_blocks_acceptance(env, analysed):
    head = env.store.head(env.tenant, env.ws)
    digest = env.store.stage_blob(b"moved")
    env.store.publish_source(env.tenant, env.ws, "src-moved", {"artifact-new": digest}, expected_version=head.version,
                             operation="source.import.commit", idempotency_key="moved")
    r = accept(env.admin_client(), env, acknowledged_open_gaps=True)
    assert r.status_code == 409 and r.json()["code"] == "stale_base"


def test_requester_cannot_accept_or_revoke(env, analysed):
    req = env.req_client()
    n = counts(env)
    assert accept(req, env).status_code == 403
    assert req.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": "decision-x", "reason": "no"}).status_code == 403
    assert req.get("/v2/business/knowledge", params={"workspace_id": env.ws}).status_code == 200
    assert counts(env) == n


def test_revoking_a_decision_supersedes_what_stood_on_it_and_keeps_the_history(env, analysed):
    admin = env.admin_client()
    gap = conflict_gap(env)
    out = answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-config"}, "2 million.")
    assert accept(admin, env).status_code == 200
    snapshots_before = counts(env)["v2_business_snapshots"]
    bad = admin.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": out["decision_id"], "reason": "  "})
    assert bad.status_code == 422
    r = admin.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": out["decision_id"], "reason": "Finance changed its mind."})
    assert r.status_code == 200, r.text
    assert r.json()["superseded_rules"] == ["rule-capex-finance-config-target"] and r.json()["business_revision"] == 2
    view = business_view(env.store, env.tenant, env.ws)
    rules = {x["rule_id"]: x for x in view["accepted"]["rules"]}
    assert rules["rule-capex-finance-config-target"]["status"] == "SUPERSEDED" and rules["rule-capex-finance-config"]["status"] == "VALIDATED"
    assert any(g["gap_id"] == gap["gap_id"] for g in gap_queue(env.store, env.tenant, env.ws)["gaps"])    # the question is asked again
    assert counts(env)["v2_business_snapshots"] == snapshots_before + 1                                 # nothing was deleted
    diff = admin.get("/v2/business/diff", params={"workspace_id": env.ws, "from_revision": 1, "to_revision": 2}).json()
    assert diff["rules_changed"][0]["after"]["status"] == "SUPERSEDED"
    again = admin.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": out["decision_id"], "reason": "again"})
    assert again.status_code == 409 and again.json()["code"] == "unknown_reference"                     # already revoked
    # the question can be answered again and accepted; the decision rows of both answers are still there
    answer(admin, env, gap, {"choose_rule_id": "rule-capex-finance-policy"}, "5 million after all.")
    final = accept(admin, env)
    assert final.status_code == 200, final.text
    assert final.json()["business_revision"] == 3


def test_accepting_knowledge_creates_no_document_work_and_marks_the_index_as_pending(env, analysed):
    admin = env.admin_client()
    answer(admin, env, conflict_gap(env), {"choose_rule_id": "rule-capex-finance-config"})
    assert accept(admin, env).status_code == 200
    st = admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()
    assert st["documents"] == {"freshness": "not_requested", "jobs": 0} and st["index"]["pending_events"] >= 1
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND kind LIKE 'document%%'", (env.tenant,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND kind='JOB_QUEUED' AND payload->>'kind' LIKE 'document%%'", (env.tenant,))
        assert cur.fetchone()[0] == 0
    drain(env)                                                                                           # the worker consumes the marker event
    assert admin.get("/v2/business/knowledge", params={"workspace_id": env.ws}).json()["index"]["pending_events"] == 0
