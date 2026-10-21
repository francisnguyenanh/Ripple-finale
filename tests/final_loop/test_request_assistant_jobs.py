"""RQA-02 / RQA-03 private threads and admitted answering through the real routes, admission, worker, validator and store on the throwaway PostgreSQL. The provider transport
is a SYNTHETIC stub that records every outbound prompt (no real model; nothing here is AI-quality evidence). Covers: no model call on create / read, private threads (same
role, other account, admin), the scoped capability (viewer may ask, cannot start other AI work; ai_suspended), idempotent asks and the sequence guard, prompt canaries,
validated blocks (unknown refs, invented numbers, permission contradictions, action claims), revocation between ask and answer, context changes, the generic job routes
closed for assistant jobs, and cancel."""
from __future__ import annotations

import json

import pytest

from infra.worker import explain
from tests.final_loop.test_chg04_review import login, prepared
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401  (ws is a fixture)
from tests.final_loop.test_qry03_qa import Model, qa_env  # noqa: F401  (fixture)
from tests.pitch_brief.conftest import drain

CANARY = "CANARY-HIDDEN-CHAT-3F8A"


@pytest.fixture
def ra_env(qa_env, monkeypatch, tmp_path):                  # noqa: F811
    import os
    q = qa_env
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  request.assistant: gemini-3.8-flash\n", 1)
    path = tmp_path / "budget_rqa.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    return q


def use(q, fn):
    model = Model(fn)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    return model


def answer(outcome="answered", blocks=(), limitations=(), assertions=None):
    out = {"outcome": outcome, "answer_blocks": [{"text": t, "fact_refs": list(r), "kind": k} for t, r, k in blocks], "limitations": list(limitations)}
    return out if assertions is None else {**out, "authority_assertions": list(assertions)}         # absent: the answer-schema-1 object shape stays valid


def fact_id(prompt: str, kind: str) -> str:
    return next(l.split(" ", 1)[0] for l in prompt.split("\n") if l.startswith("F") and f"[{kind}]" in l)


def own_obligation(prompt: str) -> str:
    return next(l.split(" ", 1)[0] for l in prompt.split("\n") if l.startswith("F") and "[obligation]" in l and "held by you" in l)


def thread(q, client, cid):
    r = client.post(f"/v2/requests/business_change/{cid}/assistant/threads", json={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    return r.json()


def ask(q, client, tid, text, key, after):
    return client.post(f"/v2/request-assistant/threads/{tid}/messages", json={"workspace_id": q.env.ws, "question": text, "idempotency_key": key, "after_seq": after})


def read(q, client, tid):
    return client.get(f"/v2/request-assistant/threads/{tid}", params={"workspace_id": q.env.ws})


def settle(q, client, tid, tries=40):
    for _ in range(tries):
        drain(q.env)
        st = read(q, client, tid).json()
        if st["messages"] and st["messages"][-1]["state"] != "pending":
            return st
    return st


def test_private_thread_explicit_ask_checked_answer_and_no_call_on_create_or_read(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    from tests.final_loop.test_request_assistant_access import private_chat
    private_chat(q.env, q.env.req, f"secret plan {CANARY}")
    model = use(q, lambda p: answer(blocks=[("The current stage is shown in the facts.", [fact_id(p, "state")], "fact")]))   # answer/5: no authority from the model
    before = counts(q.env)
    t = thread(q, approver, cid)
    assert t["private"] is True and t["messages"] == [] and model.calls == 0
    assert read(q, approver, t["thread_id"]).status_code == 200 and model.calls == 0                     # opening / reading never calls a model
    r = ask(q, approver, t["thread_id"], "What do I need to do next?", "key-0001-aaaa", 0)
    assert r.status_code == 200, r.text
    st = settle(q, approver, t["thread_id"])
    m = st["messages"][0]
    assert model.calls == 1 and m["state"] == "answered" and m["answer"]["status"] == "answered" and m["current"] is True
    assert m["answer"]["blocks"][0]["kind"] == "authority" and m["answer"]["blocks"][0]["text"].startswith("You hold the current step: ")   # from the task record
    assert m["answer"]["blocks"][0]["origin"] == "task_record" and [b["kind"] for b in m["answer"]["blocks"]][-1] == "fact"            # then the model's own block
    assert m["citations"] and all(ref.startswith("F") for ref in m["citations"])
    assert any(a["action"] == "approve_knowledge" for a in m["answer"]["next_actions"])                     # next steps from the server, not the model
    prompt = model.prompts[0]
    assert CANARY not in prompt and q.env.req not in prompt and "What do I need to do next?" in prompt and "<facts>" in prompt
    replay = ask(q, approver, t["thread_id"], "What do I need to do next?", "key-0001-aaaa", 0)
    assert replay.status_code == 200 and replay.json()["replayed"] is True and model.calls == 1               # same key, same question: no second job
    assert ask(q, approver, t["thread_id"], "Something else", "key-0001-aaaa", 1).json()["code"] == "idempotency_conflict"
    assert ask(q, approver, t["thread_id"], "Stale tab question", "key-0002-bbbb", 0).json()["code"] == "sequence_conflict"
    after = counts(q.env)
    assert after["v2_business_decisions"] == before["v2_business_decisions"]                                  # the assistant never records a decision


def test_threads_are_private_and_the_generic_job_routes_are_closed_for_assistant_jobs(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    use(q, lambda p: answer(blocks=[("The stage is in the facts.", [fact_id(p, "state")], "fact")]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "Where does this stand?", "key-0003-cccc", 0)
    other = login(q, f"appr2-{q.env.nonce}", ("change_approver", "reviewer"))                                # same roles, another account
    for client in (other, q.admin, q.req):
        assert read(q, client, t["thread_id"]).status_code == 404
        assert ask(q, client, t["thread_id"], "x", "key-0004-dddd", 1).status_code == 404
        listed = client.get(f"/v2/requests/business_change/{cid}/assistant/threads", params={"workspace_id": q.env.ws})
        assert listed.status_code == 200 and listed.json()["threads"] == []
    jid = read(q, approver, t["thread_id"]).json()["messages"][0]["message_id"]
    with q.env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s", (q.env.tenant, q.env.ws, jid))
        job = cur.fetchone()[0]
    for client in (approver, q.admin):
        assert client.get(f"/v2/jobs/{job}", params={"workspace_id": q.env.ws}).status_code == 404            # the thread route is the only read path
        for path in ("approve", "retry", "cancel"):
            r = client.post(f"/v2/jobs/{job}/{path}", json={"workspace_id": q.env.ws})
            missing = client.post(f"/v2/jobs/rqa-does-not-exist/{path}", json={"workspace_id": q.env.ws})
            assert (r.status_code, r.json()) == (missing.status_code, missing.json()), path                 # the same answer as for a job that does not exist
    assert q.admin.post("/v2/jobs", json={"workspace_id": q.env.ws, "kind": "request.qa", "job_id": "rqa-forged", "payload": {}}).status_code in (400, 403)


def test_the_scoped_capability_lets_a_viewer_ask_about_a_readable_request_only_and_suspension_removes_it(ra_env, monkeypatch):
    from tests.final_loop.test_kno07_answer_interpret import actor_gap, interpret
    q = ra_env
    cid, approver = prepared(q)
    viewer = login(q, f"viewer-a-{q.env.nonce}", ("viewer",))
    assert viewer.post(f"/v2/requests/business_change/{cid}/assistant/threads", json={"workspace_id": q.env.ws}).status_code == 404       # not readable for a viewer
    r = interpret(q, viewer, actor_gap(q)["gap_id"], "The duty manager.")
    assert r.status_code == 403                                                                               # request.assistant.ask is not ai.use
    suspended = login(q, f"appr-s-{q.env.nonce}", ("change_approver", "ai_suspended"))
    r = suspended.post(f"/v2/requests/business_change/{cid}/assistant/threads", json={"workspace_id": q.env.ws})
    assert r.status_code == 403 and r.json()["code"] == "assistant_not_permitted"


def test_the_validator_refuses_unknown_refs_invented_numbers_permission_contradictions_and_action_claims(ra_env):
    q = ra_env
    cid, _approver = prepared(q)

    def bad(p):
        return answer(blocks=[("This rests on a fact that does not exist.", ["F999"], "fact"),
                              ("The threshold becomes 7,777,777 JPY.", [fact_id(p, "state")], "fact"),
                              ("You can approve the knowledge change now.", [fact_id(p, "action")], "suggestion"),
                              ("I have approved it for you.", [fact_id(p, "state")], "fact")])
    use(q, bad)
    t = thread(q, q.req, cid)                                                                                 # the requester may not decide: the action fact says so
    ask(q, q.req, t["thread_id"], "Can I approve it? I agree.", "key-0005-eeee", 0)
    m = settle(q, q.req, t["thread_id"])["messages"][0]
    reasons = sorted(r["reason"] for r in m["answer"]["rejected"])
    assert m["answer"]["status"] == "refused" and [b for b in m["answer"]["blocks"] if b["kind"] != "authority"] == []                         # the record's section is shown but is not an answer
    assert {"unknown_fact_ref", "number_not_in_cited_facts", "claims_an_action"} <= set(reasons)
    assert "contradicts_cited_permission" in reasons or "suggestion_without_action_fact" in reasons


def test_revocation_and_a_moved_context_between_ask_and_answer_withhold_and_fail_closed(ra_env):
    from infra.adapters.v2_identity import PostgresIdentityStore
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[("The stage is in the facts.", [fact_id(p, "state")], "fact")]))
    acct = f"appr-r-{q.env.nonce}"
    revokee = login(q, acct, ("change_approver",))
    t = thread(q, revokee, cid)
    ask(q, revokee, t["thread_id"], "What next?", "key-0006-ffff", 0)
    ids = PostgresIdentityStore(q.env.dsn)
    ids.grant_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, acct, ("viewer",))                      # roles replaced before the worker runs
    drain(q.env)
    assert model.calls == 0                                                                                    # prepare refused: no provider call
    assert read(q, revokee, t["thread_id"]).status_code == 404                                                 # cannot read the request any more: thread withheld
    t2 = thread(q, approver, cid)
    ask(q, approver, t2["thread_id"], "What next?", "key-0007-gggg", 0)
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")   # the request's state moves before the answer
    st = settle(q, approver, t2["thread_id"])
    m = st["messages"][0]
    assert model.calls == 0 and m["state"] == "unavailable" and "context_changed" in (m["answer"].get("reason") or "")


def test_a_pending_answer_can_be_cancelled_by_its_asker(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[("x", [fact_id(p, "state")], "fact")]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "What next?", "key-0008-hhhh", 0)
    mid = read(q, approver, t["thread_id"]).json()["messages"][0]["message_id"]
    r = approver.post(f"/v2/request-assistant/threads/{t['thread_id']}/messages/{mid}/cancel", json={"workspace_id": q.env.ws})
    assert r.status_code == 200, r.text
    st = settle(q, approver, t["thread_id"])
    assert st["messages"][0]["state"] == "cancelled" and model.calls == 0


def test_access_change_after_the_answer_withholds_it_books_the_usage_and_history_stays_in_the_own_thread(ra_env):
    from infra.adapters.v2_identity import PostgresIdentityStore
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[("The stage is in the facts.", [fact_id(p, "state")], "fact")]))
    acct = f"appr-w-{q.env.nonce}"
    a = login(q, acct, ("change_approver",))
    other = login(q, f"appr-o-{q.env.nonce}", ("change_approver",))
    t_other = thread(q, other, cid)
    ask(q, other, t_other["thread_id"], "OTHER-PERSON-QUESTION-71", "key-0010-jjjj", 0)
    settle(q, other, t_other["thread_id"])
    t = thread(q, a, cid)
    ask(q, a, t["thread_id"], "FIRST-QUESTION-42", "key-0011-kkkk", 0)
    first = settle(q, a, t["thread_id"])["messages"][0]
    assert first["state"] == "answered"
    ask(q, a, t["thread_id"], "And after that?", "key-0012-llll", 1)
    drain(q.env)
    second_prompt = model.prompts[-1]
    assert "FIRST-QUESTION-42" in second_prompt and "OTHER-PERSON-QUESTION-71" not in second_prompt      # history: this person's own thread only
    with q.env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s AND seq=2", (q.env.tenant, q.env.ws, t["thread_id"]))
        jid = cur.fetchone()[0]
        cur.execute("UPDATE v2_request_messages SET answer=NULL, answered_at=NULL WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
    PostgresIdentityStore(q.env.dsn).grant_membership(q.env.tenant, f"p-{q.env.nonce}", q.env.ws, acct, ("change_approver", "reviewer"))   # still reads the request, other access
    st = read(q, a, t["thread_id"]).json()
    assert [m["state"] for m in st["messages"]] == ["withheld", "withheld"] and all("answer" not in m and "citations" not in m for m in st["messages"])
    with q.env.store._tx() as cur:
        cur.execute("SELECT entry_state FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
        assert cur.fetchone()[0] == "SETTLED"                                                                # the call happened: its usage is booked although the answer is withheld
        cur.execute("SELECT answer FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
        assert cur.fetchone()[0] is None                                                                     # not published under the changed access
    listed = a.get(f"/v2/requests/intent_flow/intent-elsewhere/assistant/threads", params={"workspace_id": q.env.ws})
    assert listed.status_code == 404                                                                         # another / unknown request: nothing


def test_raw_and_checked_answers_are_kept_apart_identity_follows_the_config_and_another_workspace_reaches_nothing(ra_env, monkeypatch):
    from infra.worker import request_assistant as wra
    q = ra_env
    cid, approver = prepared(q)
    use(q, lambda p: answer(blocks=[("The stage is in the facts.", [fact_id(p, "state")], "fact"), ("Refused: invented 9,999 JPY.", [fact_id(p, "state")], "fact")]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "Where does it stand?", "key-0020-mmmm", 0)
    st = settle(q, approver, t["thread_id"])
    m = st["messages"][0]
    assert m["answer"]["status"] == "partial" and len([b for b in m["answer"]["blocks"] if b["kind"] != "authority"]) == 1 and "model_answer" not in m["answer"]          # the thread shows only the checked answer
    with q.env.store._tx() as cur:
        cur.execute("SELECT job_id FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND message_id=%s", (q.env.tenant, q.env.ws, m["message_id"]))
        job = q.env.store.job_record(q.env.tenant, q.env.ws, cur.fetchone()[0])
    raw = job["execution_result"]["model_answer"]
    assert len(raw["answer_blocks"]) == 2 and job["execution_result"]["rejected"][0]["reason"] == "number_not_in_cited_facts"      # raw kept apart for evaluation
    j1 = wra.job_id_for("rqt-x", "rqm-y")
    real = wra.load_config
    monkeypatch.setattr(wra, "load_config", lambda: (real()[0], "b" * 64))
    assert wra.job_id_for("rqt-x", "rqm-y") != j1                                                                             # a changed prompt / config is a new job, never a replay
    for ws_other in ("ws-of-someone-else", q.env.ws + "-x"):
        r = approver.get(f"/v2/request-assistant/threads/{t['thread_id']}", params={"workspace_id": ws_other})
        assert r.status_code in (401, 403, 404) and t["thread_id"] not in r.text and "Where does it stand" not in r.text


def test_a_claim_that_the_reader_holds_an_obligation_needs_an_obligation_fact_held_by_them():
    """A2 live P5 (RQA-LIVE-A2-20261020-01, verbatim): the viewer was told "as you hold the obligation" while the cited fact named the requester. Checks 4 (AIG-01): the
    true sentences P2 / P4 (verbatim) are no longer kept as free text either; such a statement is kept only as a typed assertion the server renders."""
    from core.change_intelligence.v2 import request_assistant as ra
    limits = {"max_block_chars": 600, "max_limitations": 4}
    facts = [{"id": "F1", "kind": "state", "text": "Current stage: Waiting for the knowledge decision."},
             {"id": "F2", "kind": "obligation", "text": "Ask for the AI draft - required, held by the requester, state current (you may do it: no; only the requester does this step)."},
             {"id": "F3", "kind": "obligation", "text": "Decide the knowledge change (approve, reject or request changes) - required, held by you, state current (you may do it: yes)."},
             {"id": "F6", "kind": "action", "text": "Action 'approve knowledge': not allowed for you - your role cannot decide this change."}]
    pack = {"facts": facts}

    def check(text, refs, kind):
        return ra.check_answer({"outcome": "answered", "answer_blocks": [{"text": text, "fact_refs": refs, "kind": kind}], "limitations": []}, pack, limits)
    p5 = check("You may not approve this request yourself at this current stage as you hold the obligation for the current step.", ["F2"], "suggestion")
    assert [b for b in p5["blocks"] if b["kind"] != "authority"] == [] and p5["rejected"][0]["reason"] == "contradicts_cited_obligation"
    p2 = check("You are required to decide the knowledge change and proposed file edits.", ["F3"], "fact")
    p4 = check("Your role cannot decide this change and is not allowed to approve the knowledge.", ["F6"], "fact")
    neg = check("You do not hold the current step; the requester does.", ["F2"], "fact")
    for out in (p2, p4, neg):
        assert [b for b in out["blocks"] if b["kind"] != "authority"] == [] and out["rejected"][0]["reason"] == "authority_in_free_text"


def test_an_obligation_that_changes_while_the_answer_waits_fails_closed_as_context_changed(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[("You are required to decide the knowledge change.", [fact_id(p, "obligation")], "fact")]))
    t = thread(q, approver, cid)
    ask(q, approver, t["thread_id"], "What must I do?", "key-0030-nnnn", 0)
    rv = approver.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "technical"}).json()
    d = approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": "request_changes",
                                                                         "reason": "Please name the scope.", "binding": rv["binding"]})
    assert d.status_code == 200, d.text                                                                     # the person's own control, not the chat
    st = settle(q, approver, t["thread_id"])
    m = st["messages"][0]
    assert model.calls == 0 and m["state"] == "unavailable" and "context_changed" in (m["answer"].get("reason") or "")   # no answer from the old obligation


def test_switching_the_assistant_off_refuses_new_asks_keeps_history_stops_a_queued_answer_and_leaves_the_request_working(ra_env):
    """RUX-06 feature off (`RIPPLE_REQUEST_ASSISTANT=off`, the same check as the runtime switch `request_assistant`): the server refuses new threads and questions with an
    explained 409 (not only a hidden button), earlier answers stay readable to their asker, an answer queued before the switch is stopped before any provider call and
    settled by the normal path, and the request's task view and business actions keep working."""
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[("The current stage is in the facts.", [fact_id(p, "state")], "fact")]))
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "Where does it stand?", "key-off-0001-aaaa", 0).status_code == 200
    first = settle(q, approver, t["thread_id"])
    assert first["messages"][0]["state"] == "answered" and model.calls == 1
    assert ask(q, approver, t["thread_id"], "And next?", "key-off-0002-bbbb", 1).status_code == 200          # queued while still on
    q.monkeypatch.setenv("RIPPLE_REQUEST_ASSISTANT", "off")
    r = approver.post(f"/v2/requests/business_change/{cid}/assistant/threads", json={"workspace_id": q.env.ws})
    assert r.status_code == 409 and r.json()["code"] == "assistant_off"
    r = ask(q, approver, t["thread_id"], "Once more?", "key-off-0003-cccc", 2)
    assert r.status_code == 409 and r.json()["code"] == "assistant_off"
    st = settle(q, approver, t["thread_id"])
    assert st["messages"][0]["state"] == "answered" and st["messages"][1]["state"] != "answered" and model.calls == 1   # stopped before the provider call
    assert read(q, q.req, t["thread_id"]).status_code == 404                                                     # history keeps its ACL: still private
    import psycopg2
    with psycopg2.connect(q.env.dsn) as conn, conn.cursor() as cur:                                              # nothing left reserved: the stop went through the normal accounting
        cur.execute("SELECT count(*) FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND entry_state='RESERVED'", (q.env.tenant, q.env.ws))
        assert cur.fetchone()[0] == 0
    tv = approver.get(f"/v2/requests/business_change/{cid}/task-view", params={"workspace_id": q.env.ws}).json()
    assert tv["features"]["request_assistant"] == "off" and any(o["code"] == "decide_knowledge" and o["obligation_me"] for o in tv["obligations"])
    binding = approver.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "business"}).json()["binding"]
    d = approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": "approve",
                                                                       "reason": "Checked against the policy; the new threshold is right.", "binding": binding})
    assert d.status_code == 200, d.text                                                                          # business actions are not affected
    q.monkeypatch.delenv("RIPPLE_REQUEST_ASSISTANT")
    assert approver.get(f"/v2/requests/business_change/{cid}/task-view", params={"workspace_id": q.env.ws}).json()["features"]["request_assistant"] == "on"
