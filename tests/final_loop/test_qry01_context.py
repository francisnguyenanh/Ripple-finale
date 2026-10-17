"""QRY-01: versioned business index and the snapshot-pinned context pack, through the real routes, worker and store. No provider, no model call."""
from __future__ import annotations

import psycopg2
import pytest

from core.change_intelligence.v2 import business_index as bi
from infra.adapters import v2_business_index as vbi
from infra.adapters.v2_business import business_view
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_kno05_accept import accept, analysed, answer, conflict_gap  # noqa: F401  (analysed is a fixture)
from tests.pitch_brief.conftest import drain

QUERY = "Who reviews CAPEX requests of 2,500,000 yen?"


def ask(client, env, query=QUERY, **extra):
    r = client.post("/v2/business/context", json={"workspace_id": env.ws, "query": query, **extra})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def accepted(env, analysed):                                           # noqa: F811
    admin = env.admin_client()
    answer(admin, env, conflict_gap(env), {"choose_rule_id": "rule-capex-finance-config"}, "Finance reviews CAPEX from 2,000,000 JPY.")
    assert accept(admin, env).status_code == 200
    return admin


def index_rows(env, snapshot_id):
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, item_count FROM v2_business_index_builds WHERE tenant_id=%s AND snapshot_id=%s", (env.tenant, snapshot_id))
        return cur.fetchall()


def test_confirmed_target_is_retrievable_without_any_source_edit_and_the_pack_is_pinned(env, accepted):
    snap = business_view(env.store, env.tenant, env.ws)["accepted"]["snapshot"]
    before = ask(accepted, env)
    assert before["served_from"] == "snapshot_direct" and before["index"]["status"] == "BUILDING" and before["notes"]       # accepted, index not built yet: labelled fallback
    drain(env)                                                                                                            # the worker consumes the commit event and builds the index
    pack = ask(accepted, env)
    assert pack["served_from"] == "index" and pack["index"]["status"] == "READY" and index_rows(env, snap["snapshot_id"]) == [("READY", pack["index"]["item_count"])]
    ids = [i["item_id"] for i in pack["items"]]
    assert "rule-capex-finance-config-target" in ids[:3] and any(i["kind"] == "decision" for i in pack["items"])        # the decision and the rule it created rank at the top
    target = next(i for i in pack["items"] if i["item_id"] == "rule-capex-finance-config-target")
    assert target["authority"] == "TARGET" and target["decision_ids"] and target["actor_ref"] == "role-finance"          # provenance: the human decision
    assert pack["pin"]["snapshot_id"] == snap["snapshot_id"] and pack["pin"]["business_revision"] == 1 and pack["pin"]["index_version"] == bi.INDEX_VERSION
    assert pack["pin"]["source_revision_id"] == env.store.head(env.tenant, env.ws).source_revision_id                      # the source revision is unchanged
    assert {i["item_id"] for i in before["items"]} == {i["item_id"] for i in pack["items"]}                              # direct and indexed reads agree: one version
    assert any(c["kind"] == "conflicting_rule" for c in pack["counterevidence"])                                         # the disagreeing source statement is not hidden
    assert pack["graph"] and pack["coverage"]["returned"] == len(pack["items"]) and pack["coverage"]["max_items"] == 40


def test_observed_view_is_source_only_and_observation_text_needs_evidence_rights(env, accepted):
    drain(env)
    obs = ask(accepted, env, "Manager approval threshold", view="observed")
    assert all(i["kind"] == "rule" and i["authority"] == "OBSERVED" for i in obs["items"]) and obs["pin"]["view"] == "observed"
    assert obs["observations"]["status"] == "READY" and obs["observations"]["results"] and obs["observations"]["source_revision_id"] == obs["pin"]["source_revision_id"]
    req = env.req_client()                                                                                                 # a requester: project.read, no evidence.read
    low = ask(req, env, "Manager approval threshold", view="observed")
    assert low["observations"]["results"] == [] and low["observations"]["withheld"] > 0 and "raw_value" not in str(low)
    assert [i["item_id"] for i in low["items"]] == [i["item_id"] for i in obs["items"]]                                    # business meaning is the same for both


def test_source_only_workspace_answers_from_the_sources_and_says_there_is_no_knowledge(env, ws):
    pack = ask(env.admin_client(), env, "Manager approval threshold")
    assert pack["items"] == [] and pack["pin"]["snapshot_id"] is None and pack["observations"]["results"] and any("no business knowledge" in n.lower() for n in pack["notes"])


def test_a_non_ready_or_old_index_is_never_served_as_current(env, accepted):
    snap = business_view(env.store, env.tenant, env.ws)["accepted"]["snapshot"]["snapshot_id"]
    drain(env)
    good = ask(accepted, env)
    ids = [i["item_id"] for i in good["items"]]
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("UPDATE v2_business_index_builds SET index_version='business-index-v0' WHERE tenant_id=%s AND snapshot_id=%s", (env.tenant, snap))
        cur.execute("UPDATE v2_business_index_items SET index_version='business-index-v0' WHERE tenant_id=%s AND snapshot_id=%s", (env.tenant, snap))
    stale = ask(accepted, env)
    assert stale["index"]["status"] == "STALE" and stale["served_from"] == "snapshot_direct" and [i["item_id"] for i in stale["items"]] == ids
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:                   # rows of the current version WITHOUT a READY marker (a half-built index)
        cur.execute("DELETE FROM v2_business_index_builds WHERE tenant_id=%s AND snapshot_id=%s", (env.tenant, snap))
        cur.execute("""INSERT INTO v2_business_index_items (tenant_id, workspace_id, snapshot_id, index_version, item_id, kind, term_vector, body)
            VALUES (%s,%s,%s,%s,'rule-ghost','rule','{"capex": 9}'::jsonb, '{"item_id":"rule-ghost","kind":"rule","text":"capex ghost","authority":"TARGET"}'::jsonb)""",
                    (env.tenant, env.ws, snap, bi.INDEX_VERSION))
    half = ask(accepted, env)
    assert "rule-ghost" not in str(half) and half["index"]["status"] == "BUILDING" and half["served_from"] == "snapshot_direct"


def test_failed_build_is_marked_retried_and_not_acknowledged(env, accepted, monkeypatch):
    snap = business_view(env.store, env.tenant, env.ws)["accepted"]["snapshot"]["snapshot_id"]
    with monkeypatch.context() as m:
        def boom(*a, **k):
            raise RuntimeError("injected index failure")
        m.setattr(bi, "build_items", boom)
        drain(env)
    assert index_rows(env, snap) == [("FAILED", 0)]
    pack = ask(accepted, env)
    assert pack["index"]["status"] == "FAILED" and pack["served_from"] == "snapshot_direct" and pack["items"]
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:                    # the event is still waiting, so a later claim can build it
        cur.execute("SELECT status FROM v2_outbox WHERE tenant_id=%s AND kind='BUSINESS_KNOWLEDGE_COMMITTED' AND payload->>'snapshot_id'=%s", (env.tenant, snap))
        assert cur.fetchone()[0] == "CLAIMED"
        cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND kind='BUSINESS_KNOWLEDGE_COMMITTED' AND payload->>'snapshot_id'=%s", (env.tenant, snap))
    drain(env)
    assert [r[0] for r in index_rows(env, snap)] == ["READY"] and ask(accepted, env)["served_from"] == "index"


def test_rebuild_is_idempotent_and_revocation_removes_withdrawn_items(env, accepted):
    drain(env)
    first = business_view(env.store, env.tenant, env.ws)["accepted"]["snapshot"]["snapshot_id"]
    again = vbi.build_index(env.store, env.tenant, env.ws, first)
    assert again["status"] == "READY" and len(index_rows(env, first)) == 1
    pack = ask(accepted, env)
    decision = next(i["item_id"] for i in pack["items"] if i["kind"] == "decision")
    r = accepted.post("/v2/business/decisions/revoke", json={"workspace_id": env.ws, "decision_id": decision, "reason": "withdrawn"})
    assert r.status_code == 200, r.text
    drain(env)
    after = ask(accepted, env)
    assert after["pin"]["business_revision"] == 2 and after["served_from"] == "index"
    assert "rule-capex-finance-config-target" not in [i["item_id"] for i in after["items"]] and decision not in [i["item_id"] for i in after["items"]]
    assert any(i["kind"] == "gap" and i["status"] == "REOPENED" for i in after["items"])                                # the question is open again, so an answer can say so


def test_scope_and_validation(env, accepted):
    drain(env)
    env.store.create_workspace("t-other-" + env.nonce, f"p-{env.nonce}", env.ws)
    other = vbi.context_pack(env.store, "t-other-" + env.nonce, env.ws, QUERY)
    assert other["items"] == [] and other["pin"]["snapshot_id"] is None                                                    # same workspace id, another tenant: nothing
    assert accepted.post("/v2/business/context", json={"workspace_id": env.ws, "query": QUERY, "top_k": 41}).status_code == 400
    assert accepted.post("/v2/business/context", json={"workspace_id": env.ws, "query": QUERY, "view": "everything"}).status_code == 422
    assert accepted.post("/v2/business/context", json={"workspace_id": env.ws, "query": "  "}).status_code == 400
    assert accepted.post("/v2/business/context", json={"workspace_id": "ws-nope", "query": QUERY}).status_code in (403, 404)
    anonymous = env.admin_client()
    anonymous.cookies.clear()
    assert anonymous.post("/v2/business/context", json={"workspace_id": env.ws, "query": QUERY}).status_code in (401, 403)
    unrelated = ask(accepted, env, "zzzz qqqq nothing matches this")
    assert unrelated["items"] == [] and unrelated["coverage"]["returned"] == 0                                           # nothing is invented to fill the list


def test_source_moved_after_acceptance_is_flagged_but_stays_on_its_own_revision(env, accepted):
    drain(env)
    pin = ask(accepted, env)["pin"]
    head = env.store.head(env.tenant, env.ws)
    digest = env.store.stage_blob(b"newer")
    env.store.publish_source(env.tenant, env.ws, "src-newer", {"artifact-new": digest}, expected_version=head.version, operation="source.import.commit", idempotency_key="newer")
    pack = ask(accepted, env)
    assert pack["observed_stale"] is True and pack["pin"] == pin and pack["observations"]["source_revision_id"] == pin["source_revision_id"]


def test_observations_without_a_ready_source_index_are_reported_not_invented(env, accepted):
    drain(env)
    rev = ask(accepted, env)["pin"]["source_revision_id"]
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:                    # the source index of the pinned revision is gone
        cur.execute("DELETE FROM v2_index_builds WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s", (env.tenant, env.ws, rev))
    pack = ask(accepted, env, "Manager approval threshold", view="observed")
    assert pack["observations"]["status"] == "NOT_READY" and pack["observations"]["results"] == [] and pack["items"]       # the business items still answer; no source text is made up
