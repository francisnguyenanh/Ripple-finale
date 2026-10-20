"""EUI-02 first question as ONE idempotent action (POST /v2/requests/{family}/{id}/assistant/ask) through the real route, admission, worker and store on the throwaway
PostgreSQL; synthetic recording stub model (no real model, not AI-quality evidence). Covers: opening / reading creates nothing, replay after a lost response, concurrent
clicks, a different question under the same key, invalid input, denied / suspended / switched-off callers write nothing, and a failed admission keeps the thread so the
same key retries once."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, fact_id, ra_env, read, settle, use  # noqa: F401  (ra_env is a fixture)


def first(q, client, cid, question, key):
    return client.post(f"/v2/requests/business_change/{cid}/assistant/ask", json={"workspace_id": q.env.ws, "question": question, "idempotency_key": key})


def rows(q, cid):
    """(threads, messages, jobs) of request.qa for this request: the paid / persistent things a click may create."""
    with q.env.store._tx() as cur:
        cur.execute("SELECT count(*) FROM v2_request_threads WHERE tenant_id=%s AND workspace_id=%s AND request_id=%s", (q.env.tenant, q.env.ws, cid))
        t = cur.fetchone()[0]
        cur.execute("""SELECT count(*), count(DISTINCT m.job_id) FROM v2_request_messages m JOIN v2_request_threads t ON t.tenant_id=m.tenant_id AND t.workspace_id=m.workspace_id
                       AND t.thread_id=m.thread_id WHERE t.tenant_id=%s AND t.workspace_id=%s AND t.request_id=%s""", (q.env.tenant, q.env.ws, cid))
        m, j = cur.fetchone()
    return t, m, j


def stage_answer(p):
    return answer(blocks=[("The stage is in the facts.", [fact_id(p, "state")], "fact")])


def test_opening_creates_nothing_and_the_first_ask_is_one_idempotent_action_across_a_lost_response_and_concurrent_clicks(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, stage_answer)
    listed = approver.get(f"/v2/requests/business_change/{cid}/assistant/threads", params={"workspace_id": q.env.ws})
    assert listed.status_code == 200 and listed.json()["threads"] == [] and rows(q, cid) == (0, 0, 0) and model.calls == 0     # opening the panel
    with ThreadPoolExecutor(max_workers=2) as pool:                                                    # a double click: both requests carry the same key
        rs = list(pool.map(lambda _: first(q, approver, cid, "Where does this stand?", "first-key-0001"), range(2)))
    assert sorted(r.status_code for r in rs) == [200, 200], [r.text for r in rs]
    assert sorted(bool(r.json()["replayed"]) for r in rs) == [False, True] and rs[0].json()["thread_id"] == rs[1].json()["thread_id"]
    assert rows(q, cid) == (1, 1, 1)
    again = first(q, approver, cid, "Where does this stand?", "first-key-0001")                      # the response was lost: the client sends the same call again
    assert again.status_code == 200 and again.json()["replayed"] is True and rows(q, cid) == (1, 1, 1)
    st = settle(q, approver, again.json()["thread_id"])
    assert st["messages"][0]["state"] == "answered" and model.calls == 1
    conflict = first(q, approver, cid, "Something else", "first-key-0001")
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict" and rows(q, cid) == (1, 1, 1)
    other = first(q, approver, cid, "A second conversation", "first-key-0002")                       # a new key is a new conversation, by design
    assert other.status_code == 200 and other.json()["thread_id"] != again.json()["thread_id"] and rows(q, cid) == (2, 2, 2)


def test_invalid_denied_suspended_and_switched_off_first_asks_write_nothing(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, stage_answer)
    for question, key in (("", "first-key-0003"), ("x" * 801, "first-key-0004"), ("Fine question", "short")):
        r = first(q, approver, cid, question, key)
        assert r.status_code == 400 and rows(q, cid) == (0, 0, 0), r.text
    viewer = login(q, f"viewer-f-{q.env.nonce}", ("viewer",))
    assert first(q, viewer, cid, "What next?", "first-key-0005").status_code == 404                    # may not read the request: generic not found
    suspended = login(q, f"appr-f-{q.env.nonce}", ("change_approver", "ai_suspended"))
    r = first(q, suspended, cid, "What next?", "first-key-0006")
    assert r.status_code == 403 and r.json()["code"] == "assistant_not_permitted"
    q.monkeypatch.setenv("RIPPLE_REQUEST_ASSISTANT", "off")
    r = first(q, approver, cid, "What next?", "first-key-0007")
    assert r.status_code == 409 and r.json()["code"] == "assistant_off"
    q.monkeypatch.delenv("RIPPLE_REQUEST_ASSISTANT")
    assert rows(q, cid) == (0, 0, 0) and model.calls == 0


def test_a_failed_admission_keeps_the_thread_and_the_same_key_retries_into_one_job(ra_env, monkeypatch):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, stage_answer)
    real, calls = q.env.store.create_job, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient admission failure")
        return real(*a, **k)
    monkeypatch.setattr(type(q.env.store), "create_job", lambda self, *a, **k: flaky(*a, **k))
    with pytest.raises(RuntimeError):                                                                  # the in-process client re-raises; a served route answers 500
        first(q, approver, cid, "What next?", "first-key-0008")
    assert rows(q, cid) == (1, 0, 0)                                                                   # no question, no job; the empty thread stays for the retry
    ok = first(q, approver, cid, "What next?", "first-key-0008")
    assert ok.status_code == 200 and ok.json()["replayed"] is False and rows(q, cid) == (1, 1, 1)
    listed = approver.get(f"/v2/requests/business_change/{cid}/assistant/threads", params={"workspace_id": q.env.ws}).json()["threads"]
    assert len(listed) == 1 and listed[0]["messages"] == 1
    st = settle(q, approver, ok.json()["thread_id"])
    assert st["messages"][0]["state"] == "answered" and model.calls == 1
