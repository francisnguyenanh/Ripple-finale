"""AH-05 durable correlation on the throwaway PostgreSQL: HTTP request -> job -> attempt -> result survives a duplicate request (one job, two linked request IDs),
the worker's own execution (attempts and terminal reason reachable from the request ID), and leaves job identity / dedupe unchanged. Synthetic provider only."""
from __future__ import annotations

from infra.worker import explain
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import Model, qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, fact_id, ra_env, read, settle  # noqa: F401  (ra_env is a fixture)
from tools import ops_jobs


def test_a_duplicate_first_ask_links_two_request_ids_to_one_job_and_the_trace_reaches_its_attempts(ra_env, capsys):
    q = ra_env
    cid, approver = prepared(q)
    model = Model(lambda p: answer(blocks=[("The current stage is shown in the facts.", [fact_id(p, "state")], "fact")]))
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    body = {"workspace_id": q.env.ws, "question": "Why is this waiting?", "idempotency_key": "k-ah05-1"}
    r1 = approver.post(f"/v2/requests/business_change/{cid}/assistant/ask", json=body)
    r2 = approver.post(f"/v2/requests/business_change/{cid}/assistant/ask", json=body)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    rid1, rid2 = r1.headers["x-request-id"], r2.headers["x-request-id"]
    assert rid1 != rid2
    tid = r1.json()["thread_id"]
    assert r2.json()["thread_id"] == tid
    with q.env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s", (q.env.tenant, q.env.ws, r1.json()["message_id"]))
        job_id = cur.fetchone()[0]
    links = q.env.store.job_correlation(q.env.tenant, q.env.ws, job_id)
    assert [(l["request_id"], l["created"]) for l in links] == [(rid1, True), (rid2, False)]      # one logical job, both requests recorded
    assert q.env.store.jobs_for_request(rid2)[0]["job_id"] == job_id
    st = settle(q, approver, tid)
    assert st["messages"][-1]["state"] == "answered" and model.calls == 1                              # dedupe unchanged: one job, one provider call
    capsys.readouterr()
    q.monkeypatch.setenv("RIPPLE_V2_DSN", q.env.dsn)
    assert ops_jobs.main(["trace", "--request-id", rid1]) == 0
    out = capsys.readouterr().out
    assert job_id in out and "status=COMPLETED" in out and "attempt " in out
    assert "Why is this waiting?" not in out and "The current stage" not in out                       # identifiers and metadata only


def test_jobs_without_correlation_stay_readable_and_honestly_uncorrelated(ra_env):
    q = ra_env
    assert q.env.store.job_correlation(q.env.tenant, q.env.ws, "no-such-job") == []
    assert q.env.store.jobs_for_request("0" * 32) == []
