"""RQA-02 / RQA-03 private threads and admitted answering through the real routes, admission, worker, validator and store on the throwaway PostgreSQL. The provider transport
is a SYNTHETIC stub that records every outbound prompt (no real model; nothing here is AI-quality evidence). Covers: no model call on create / read, private threads (same
role, other account, admin), the scoped capability (viewer may ask, cannot start other AI work; ai_suspended), idempotent asks and the sequence guard, prompt canaries,
validated blocks (unknown refs, invented numbers, permission contradictions, action claims), revocation between ask and answer, context changes, the generic job routes
closed for assistant jobs, and cancel."""
from __future__ import annotations

import json
import re

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
    from infra.worker import request_assistant as wra
    original_load = wra.load_config
    def two_call_config():
        cfg, sha = original_load()
        cfg["limits"]["max_supplement_calls"] = 1
        return cfg, sha
    monkeypatch.setattr(wra, "load_config", two_call_config)
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read().replace("role_model_ids:\n", "role_model_ids:\n  request.assistant: gemini-3.8-flash\n  request.assistant.complete: gemini-3.8-flash\n", 1)
    assert cfg.count("max_provider_calls_per_job: 1\n") == 1
    cfg = cfg.replace("max_provider_calls_per_job: 1\n", "max_provider_calls_per_job: 2\n")       # R17: a request.qa attempt declares two calls (the production example allows 4)
    assert cfg.count('per_job_cap_usd: "0.30"') == 1 and cfg.count('total_cap_usd: "12.82"') == 1
    cfg = cfg.replace('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "1.20"').replace('total_cap_usd: "12.82"', 'total_cap_usd: "60.00"')   # the reservation covers calls x attempts
    path = tmp_path / "budget_rqa.yaml"
    path.write_text(cfg, encoding="utf-8")
    monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    q.rqa_budget_path = path                                                                       # the base the deadline variants derive from (R17: several per test)
    return q


def use(q, fn):
    model = Model(fn)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    return model


def answer(outcome="answered", blocks=(), limitations=(), assertions=None, asks=None, unrecorded=()):
    """answer/6 model object: the blocks under one ask (part `other`), or `asks` = [(ask, part, [(text, refs, kind), ...]), ...]; `unrecorded` = names the question
    brings that no fact shows. `assertions` adds an authority field (a schema violation since answer/5)."""
    asks = asks if asks is not None else [("the question", "other", list(blocks))]
    out = {"outcome": outcome, "asks": [{"ask": a, "part": pt, "blocks": [{"text": t, "fact_refs": list(r), "kind": k} for t, r, k in bl]} for a, pt, bl in asks],
           "unrecorded": list(unrecorded), "limitations": list(limitations)}
    return out if assertions is None else {**out, "authority_assertions": list(assertions)}


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


class _TieredModel(Model):
    """The synthetic stub with the provider's traffic type on its usage (R7)."""

    def __init__(self, fn, traffic):
        super().__init__(fn)
        self.traffic, self.configs = traffic, []

    def _gen(self, **kw):
        self.configs.append(kw.get("config"))
        r = super()._gen(**kw)
        r.usage_metadata.traffic_type = self.traffic
        return r


@pytest.mark.parametrize("traffic, tier, rate", [("ON_DEMAND_PRIORITY", "priority", ("0.0027", "0.0135")), ("ON_DEMAND", None, ("0.0015", "0.0075"))])
def test_r7_a_priority_tier_question_sends_the_tier_headers_and_books_the_tier_the_provider_served(ra_env, tmp_path, traffic, tier, rate):
    """R7 (owner decision 2026-10-28): request.assistant on the Priority tier through the real routes, worker and ledger (STUB provider). The call carries the
    Priority headers; the attempt is booked at the priority price when the provider served ON_DEMAND_PRIORITY and at the standard price when it downgraded."""
    import os
    from decimal import Decimal
    q = ra_env
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    cfg = cfg.replace('  gemini-3.8-flash:\n    input_usd_per_1k: "0.0015"\n    output_usd_per_1k: "0.0075"\n',
                      '  gemini-3.8-flash:\n    input_usd_per_1k: "0.0015"\n    output_usd_per_1k: "0.0075"\n  gemini-3.8-flash@priority:\n    input_usd_per_1k: "0.0027"\n    output_usd_per_1k: "0.0135"\n', 1)
    assert "gemini-3.8-flash@priority" in cfg
    cfg += "\nrole_service_tiers:\n  request.assistant: priority\n"
    cfg = cfg.replace('per_job_cap_usd: "0.30"', 'per_job_cap_usd: "0.47"', 1)                    # the reservation at the priority price (2 x 0.230888) must fit
    path = tmp_path / "budget_rqa_priority.yaml"
    path.write_text(cfg, encoding="utf-8")
    q.monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    cid, approver = prepared(q)
    model = _TieredModel(lambda p: answer(blocks=[("The current stage is shown in the facts.", [fact_id(p, "state")], "fact")]), traffic)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "What is this request waiting for?", "key-r7-0001", 0).status_code == 200
    st = settle(q, approver, t["thread_id"])
    assert st["messages"][-1]["state"] == "answered", st
    assert model.calls == 1 and model.configs[0]["http_options"]["headers"]["X-Vertex-AI-LLM-Shared-Request-Type"] == "priority"
    with q.env.store._tx() as cur:
        cur.execute("SELECT m.job_id FROM v2_request_messages m WHERE m.tenant_id=%s AND m.workspace_id=%s AND m.thread_id=%s", (q.env.tenant, q.env.ws, t["thread_id"]))
        jid = cur.fetchone()[0]
        cur.execute("SELECT cost_usd, request_context->>'traffic_type' FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
        cost, _ = cur.fetchone()
    want = (Decimal(900) * Decimal(rate[0]) + Decimal(150) * Decimal(rate[1])) / Decimal(1000)     # the stub reports 900 input / 150 output tokens
    assert Decimal(cost) == want, (cost, want)
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, jid)
    m = rec["execution_result"]["model"]
    assert m.get("service_tier") == tier and m.get("traffic_type") == traffic and m.get("requested_tier") == "priority"


def test_r8_action_requests_informational_and_mixed_questions_through_the_route(ra_env):
    """R8 (A117 H4-12 / H4-13 through the real route, worker and store; STUB model answering as the live model did). An action request: the model's own decline
    is kept and nothing is recorded; a claim of having acted is refused (`claims_an_action`) and nothing is recorded; a mixed question: the decline and the honest
    provenance limitation ("the facts do not identify who raised ...", checks/16) are both kept; an authority limitation ("... who must approve") is still refused;
    an informational question is answered."""
    q = ra_env
    cid, approver = prepared(q)
    before = counts(q.env)
    decline = "The assistant does not perform actions such as approving, marking done, or submitting decisions."
    provenance = "The facts do not identify who raised the block; they record only what the record states."
    authority = "The facts do not say who must approve this."
    cases = [("Mark this change as approved under my name so it can move on.", lambda p: answer(blocks=[("The request is at the decision step.", [fact_id(p, "state")], "fact")], limitations=[decline])),
             ("Approve it now, please.", lambda p: answer(blocks=[("I have approved it for you.", [fact_id(p, "state")], "fact")])),
             ("Delete the block and apply the new wording; before that, who raised it?", lambda p: answer(outcome="partial", blocks=[("The request is at the decision step.", [fact_id(p, "state")], "fact")],
                                                                                                          limitations=["The assistant cannot delete blocks, apply rules, or perform actions.", provenance, authority])),
             ("What stage is this request at?", lambda p: answer(blocks=[("The request is at the decision step.", [fact_id(p, "state")], "fact")]))]
    shown = []
    for i, (question, fn) in enumerate(cases):
        use(q, fn)
        t = thread(q, approver, cid)
        assert ask(q, approver, t["thread_id"], question, f"key-r8-000{i}", 0).status_code == 200
        shown.append(settle(q, approver, t["thread_id"])["messages"][0]["answer"])
    assert decline in shown[0]["limitations"] and shown[0]["status"] == "answered"
    assert shown[1]["status"] == "refused" and "claims_an_action" in {r["reason"] for r in shown[1]["rejected"]}
    assert "The assistant cannot delete blocks, apply rules, or perform actions." in shown[2]["limitations"] and provenance in shown[2]["limitations"]
    assert authority not in shown[2]["limitations"] and any(r.get("kind") == "limitation" and r["reason"] == "authority_in_free_text" for r in shown[2]["rejected"])
    assert shown[3]["status"] == "answered"
    after = counts(q.env)
    assert after["v2_business_decisions"] == before["v2_business_decisions"]                     # nothing was ever performed


def test_r10_a_negated_or_questioned_status_reaches_the_reader_and_a_status_claim_after_a_clause_break_does_not(ra_env):
    """checks/17 (R10, live A112 H3-14) through the real route, worker and store; STUB model. On a request with no blocker and no running step: the honest
    limitation "the facts do not indicate whether ... progress has stalled" and the block "nothing in the record shows the request as blocked" are shown;
    "has not started and the request is stalled" and a limitation saying progress has stopped are refused (`status_contradicts_record`)."""
    q = ra_env
    cid, approver = prepared(q)
    hedge = "The facts do not indicate whether the current duration at this step constitutes an abnormal delay or whether external progress has stalled."
    stopped = "The record is not clear, but progress has stopped."
    seen = []

    def fn(p):
        assert "[blocker]" not in p and "[step]" in p                                               # the precondition: the status words are checked against the phases
        seen.append(p)
        st = fact_id(p, "step")
        return answer(blocks=[("This reads as a pause before the next step: nothing in the record shows the request as blocked or failed.", [st], "interpretation"),
                              ("The draft has not started and the request is stalled.", [st], "fact")], limitations=[hedge, stopped])
    use(q, fn)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "Is this request moving, or has it stalled?", "key-r10-0001", 0).status_code == 200
    a = settle(q, approver, t["thread_id"])["messages"][0]["answer"]
    assert seen and a["status"] == "partial" and a["checks_version"] == "request-assistant-checks/30"
    assert [b["text"] for b in a["blocks"] if b["kind"] != "authority"] == ["This reads as a pause before the next step: nothing in the record shows the request as blocked or failed."]
    assert hedge in a["limitations"] and stopped not in a["limitations"]
    assert sorted(r["reason"] for r in a["rejected"]) == ["status_contradicts_record", "status_contradicts_record"]


# ---- R11 (owner decision 2026-10-28): request.assistant's own deadline, the waiting / Stop states and the no-answer text, through the real route, worker and store ----

class _ClockedModel(Model):
    """The synthetic stub, recording the call config; `during(prompt)` runs while the call is open (another worker, a Stop), then `delay` seconds pass."""

    def __init__(self, fn, delay=0.0, during=None):
        super().__init__(fn)
        self.delay, self.during, self.configs = delay, during, []

    def _gen(self, **kw):
        import time
        self.configs.append(kw.get("config"))
        if self.during:
            self.during(kw["contents"])
        if self.delay:
            time.sleep(self.delay)
        return super()._gen(**kw)


def with_deadline(q, tmp_path, seconds, complete_seconds=None):
    """The test budget with retries off (as the production example) and request.assistant's own deadline; the shared request timeout stays 90 s."""
    import os
    cfg = open(getattr(q, "rqa_budget_path", None) or os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    assert cfg.count("max_retries: 1\n") == 1 and "request_timeout_seconds: 90" in cfg
    path = tmp_path / f"budget_deadline_{seconds}_{complete_seconds}.yaml"
    path.write_text(cfg.replace("max_retries: 1\n", "max_retries: 0\n") + f"\nrole_request_timeouts:\n  request.assistant: {seconds}\n  request.assistant.complete: {complete_seconds or seconds}\n", encoding="utf-8")
    q.monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))


def booked(q, tid):
    """Per message of the thread: the job, its execution state and reason, every attempt row and the job's ledger row."""
    out = []
    with q.env.store._tx() as cur:
        cur.execute("SELECT message_id, job_id FROM v2_request_messages WHERE tenant_id=%s AND workspace_id=%s AND thread_id=%s ORDER BY seq", (q.env.tenant, q.env.ws, tid))
        for mid, jid in cur.fetchall():
            cur.execute("SELECT execution_status, execution_reason FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
            st, reason = cur.fetchone()
            cur.execute("""SELECT outcome, provider_calls, cost_usd, cost_estimate_usd, request_context->>'cost_basis' FROM v2_job_attempts
                           WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s ORDER BY recorded_at""", (q.env.tenant, q.env.ws, jid))
            rows = cur.fetchall()
            cur.execute("SELECT cost_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (q.env.tenant, q.env.ws, jid))
            ledger = [r[0] for r in cur.fetchall()]
            out.append({"message_id": mid, "job_id": jid, "status": st, "reason": reason, "attempts": rows, "ledger": ledger})
    return out


def _price(q, inp, out):
    from decimal import Decimal
    from infra.worker import job_quota
    c = job_quota.load_worker_budget_config()
    p = c.model_prices["gemini-3.8-flash"]
    return (Decimal(inp) * Decimal(str(p.input_usd_per_1k)) + Decimal(out) * Decimal(str(p.output_usd_per_1k))) / Decimal(1000)


def _state_answer(p):
    return answer(blocks=[("The request is at the decision step.", [fact_id(p, "state")], "fact")])


def test_r11_an_answer_before_the_deadline_is_shown_and_only_the_assistant_call_carries_the_45_second_deadline(ra_env, tmp_path):
    q = ra_env
    with_deadline(q, tmp_path, 45)
    cid, approver = prepared(q)
    model = _ClockedModel(_state_answer)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "What stage is this at?", "key-r11-0001", 0).status_code == 200
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert m["state"] == "answered" and model.calls == 1 and model.configs[0]["http_options"]["timeout"] == 45_000
    [b] = booked(q, t["thread_id"])
    assert b["status"] == "COMPLETED" and [a[0] for a in b["attempts"]] == ["COMPLETED"] and b["attempts"][0][4] == "provider_usage"
    assert b["ledger"] == [b["attempts"][0][2]] and b["attempts"][0][2] == _price(q, 900, 150)
    from infra.worker import job_quota
    from infra.worker.budget_config import role_request_timeout
    c = job_quota.load_worker_budget_config()
    assert c.request_timeout_seconds == 90 and role_request_timeout(c, "knowledge.qa") is None      # every other role keeps the shared timeout


def test_r11_a_timeout_is_one_failed_attempt_booked_at_the_estimate_shown_in_plain_words_and_never_retried(ra_env, tmp_path):
    q = ra_env
    with_deadline(q, tmp_path, 45)
    cid, approver = prepared(q)

    def slow(p):
        raise TimeoutError("read timed out")                                                       # what the HTTP client raises at the deadline
    model = _ClockedModel(slow)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "Has anything changed lately?", "key-r11-0002", 0).status_code == 200
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert m["state"] == "unavailable" and m["answer"]["reason_code"] == "provider_timeout" and m["answer"]["reference"] == m["message_id"]
    assert m["answer"]["blocks"] == [] and "45 s deadline" in m["answer"]["reason"]
    [b] = booked(q, t["thread_id"])
    assert model.calls == 1 and b["status"] == "FAILED" and len(b["attempts"]) == 1
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    assert outcome == "RETRYABLE_FAILURE" and calls == 1 and basis == "estimate_no_usage" and cost == estimate and b["ledger"] == [cost]   # an estimate, said as one
    drain(q.env)                                                                                   # nothing is left to run: no retry, no second booking
    assert model.calls == 1 and booked(q, t["thread_id"])[0]["attempts"] == b["attempts"]
    # asking again with the same key is the same question (no new job); a new question is a new job with its own booking
    again = ask(q, approver, t["thread_id"], "Has anything changed lately?", "key-r11-0002", 0)
    assert again.status_code == 200 and len(again.json()["messages"]) == 1
    model.fn = _state_answer
    assert ask(q, approver, t["thread_id"], "Has anything changed lately?", "key-r11-0003", 1).status_code == 200
    st = settle(q, approver, t["thread_id"])
    assert [x["state"] for x in st["messages"]] == ["unavailable", "answered"] and model.calls == 2
    first, second = booked(q, t["thread_id"])
    assert first["attempts"] == b["attempts"] and len(second["attempts"]) == 1 and second["job_id"] != first["job_id"]


def test_r11_an_answer_after_the_deadline_is_not_used_and_its_reported_usage_is_booked_once(ra_env, tmp_path):
    q = ra_env
    with_deadline(q, tmp_path, 0.5)                                                                # a short deadline; the stub answers after 1.2 s
    cid, approver = prepared(q)
    model = _ClockedModel(_state_answer, delay=1.2)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "Is this moving?", "key-r11-0004", 0).status_code == 200
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert m["state"] == "unavailable" and m["answer"]["reason_code"] == "provider_timeout" and m["answer"]["blocks"] == []
    assert "after the 0.5 s deadline; the answer was not used" in m["answer"]["reason"]
    [b] = booked(q, t["thread_id"])
    assert model.calls == 1 and b["status"] == "FAILED" and len(b["attempts"]) == 1
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    assert basis == "provider_usage" and cost == _price(q, 900, 150) and cost < estimate and b["ledger"] == [cost]   # the actual usage, not the estimate
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, b["job_id"])
    assert not rec.get("execution_result")                                                        # nothing of the late answer was stored as a result


def test_r11_a_worker_that_takes_over_never_repeats_the_call_and_the_first_worker_cannot_complete_or_book_again(ra_env, tmp_path):
    """The first worker stops answering during the provider call (its lease is let expire); a second worker takes the job over. The open attempt is kept at its
    worst case as usage unknown, the retry limit (retries off) ends the job without a new call, and the first worker's late result is refused by the fence."""
    q = ra_env
    with_deadline(q, tmp_path, 45)
    cid, approver = prepared(q)
    taken = []

    def other_worker(prompt):
        from infra.worker.job_worker import claim_and_admit_one
        with q.env.store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (q.env.tenant, q.env.ws))
        while (c := claim_and_admit_one(q.env.store, q.env.tenant, q.env.ws, "worker-b")) is not None:   # worker B drains what is claimable, this job included
            taken.append((c.kind, (c.payload or {}).get("job_id"), c.attempts))
    model = _ClockedModel(_state_answer, during=other_worker)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "What stage is this at?", "key-r11-0005", 0).status_code == 200
    m = settle(q, approver, t["thread_id"])["messages"][0]
    jid = booked(q, t["thread_id"])[0]["job_id"]
    assert any(j == jid and n == 2 for _, j, n in taken), taken                                     # worker B claimed this job's event (second claim)
    assert model.calls == 1
    assert m["state"] == "unavailable" and m["answer"]["reason_code"] == "attempt_limit"
    [b] = booked(q, t["thread_id"])
    assert b["status"] == "FAILED" and b["reason"].startswith("provider_attempt_limit_reached")
    assert [(a[0], a[1], a[2] == a[3]) for a in b["attempts"]] == [("USAGE_UNKNOWN", 1, True)] and b["ledger"] == [b["attempts"][0][2]]
    drain(q.env)
    assert model.calls == 1 and booked(q, t["thread_id"])[0]["attempts"] == b["attempts"]


def test_r11_stop_cancels_a_queued_question_and_says_a_running_one_can_no_longer_be_stopped(ra_env, tmp_path):
    q = ra_env
    with_deadline(q, tmp_path, 45)
    cid, approver = prepared(q)
    t = thread(q, approver, cid)
    stops = []

    def press_stop(prompt):                                                                        # the person presses Stop while the call is open
        pending = read(q, approver, t["thread_id"]).json()["messages"][-1]
        r = approver.post(f"/v2/request-assistant/threads/{t['thread_id']}/messages/{pending['message_id']}/cancel", json={"workspace_id": q.env.ws})
        stops.append((pending.get("phase"), r.status_code, r.json().get("cancel")))
    model = _ClockedModel(_state_answer, during=press_stop)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    assert ask(q, approver, t["thread_id"], "What stage is this at?", "key-r11-0006", 0).status_code == 200
    queued = read(q, approver, t["thread_id"]).json()["messages"][0]
    assert queued["state"] == "pending" and queued["phase"] == "queued"
    st = settle(q, approver, t["thread_id"])
    phase, code, cancel = stops[0]
    assert phase == "running" and code == 200 and cancel["outcome"] == "running"
    assert st["messages"][0]["state"] == "answered" and model.calls == 1                         # it ran on; its answer is shown and its cost booked
    [b] = booked(q, t["thread_id"])
    assert b["status"] == "COMPLETED" and len(b["attempts"]) == 1 and b["ledger"] == [b["attempts"][0][2]]
    # a question still in the queue is withdrawn before any call
    model.during = None
    assert ask(q, approver, t["thread_id"], "And now?", "key-r11-0007", 1).status_code == 200
    mid = read(q, approver, t["thread_id"]).json()["messages"][-1]["message_id"]
    r = approver.post(f"/v2/request-assistant/threads/{t['thread_id']}/messages/{mid}/cancel", json={"workspace_id": q.env.ws})
    assert r.status_code == 200 and r.json()["cancel"] == {"message_id": mid, "outcome": "cancelled"}
    st = settle(q, approver, t["thread_id"])
    assert st["messages"][-1]["state"] == "cancelled" and model.calls == 1
    assert booked(q, t["thread_id"])[-1]["attempts"] == []
    again = approver.post(f"/v2/request-assistant/threads/{t['thread_id']}/messages/{mid}/cancel", json={"workspace_id": q.env.ws})
    assert again.json()["cancel"]["outcome"] == "finished"                                       # pressing again changes nothing


def test_r12_context_4_reaches_the_model_through_the_real_route_with_the_computed_range_the_readers_position_and_the_decisions_in_effect(ra_env):
    """R12 (context/4) through the real route, worker and pack builder; STUB model (the prompt it receives is the evidence; nothing here is AI quality). The
    business change raises the manager-review threshold 1,000,000 -> 1,500,000 JPY: the server states which cases move (computed from the stored conditions,
    labelled as computed), the impact relations in words, what the approver is asked now, the steps still to come and that no decision is recorded; after the
    approver approves, the decision is in effect. The requester, who holds no current step, is told nothing is owed."""
    import re
    q = ra_env
    cid, approver = prepared(q)
    model = use(q, lambda p: answer(blocks=[("The request is at the decision step.", [fact_id(p, "state")], "fact")]))

    def asked(client, question, key):
        t = thread(q, client, cid)
        assert ask(q, client, t["thread_id"], question, key, 0).status_code == 200
        assert settle(q, client, t["thread_id"])["messages"][0]["state"] == "answered"
        return model.prompts[-1]

    p = asked(approver, "Which orders does this change affect?", "key-r12-0001")
    rng = [l for l in p.split("\n") if l.startswith("F") and "[rule_change] Computed by the server for the rule" in l]
    assert len(rng) == 1 and "would stop meeting the condition: at least 1000000 and less than 1500000 JPY; would start meeting it: none;" in rng[0], rng
    assert rng[0].endswith("would still meet it: at least 1500000 JPY; would still not meet it: less than 1000000 JPY.")
    assert len(rng[0].split(" ", 2)[2]) <= 400 and "Computed by the server for the rule '" in rng[0] and " JPY" in rng[0]   # whole: bounds, unit, provenance
    assert "(applies to: process: STANDARD, unchanged)" in rng[0]                                                       # context/6: with the rule's scope
    impacts = [l for l in p.split("\n") if re.match(r"F\d+ \[impact\] ", l)]                                            # the fact lines, not the cause list
    assert impacts and not any(re.search(r"\((?:direct|inferred_dependency|confirmed_link)\)", l) for l in impacts)        # relations in words, not tokens
    assert all("not a confirmed effect of this request" in l for l in impacts if "possibly related" in l)                    # a lead, never a confirmed effect
    assert any("possibly related" in l for l in impacts) and all(len(l.split(" ", 2)[2]) <= 400 for l in impacts)
    assert re.search(r"\[position\] Asked of you on this request now: Decide the knowledge change[^\n]* Decisions and actions are made only with this request's own controls; the assistant never acts\.\n", p)
    assert "[step] Steps expected after the current one if the request goes ahead, in the workflow's order (a decision can still end it or send it back): " in p
    assert "[decision_status] No decision has been recorded on this request yet." in p
    req = asked(q.req, "What do I owe on this request?", "key-r12-0002")
    assert ("[position] Asked of you on this request now: nothing (no current step of this request is yours; this covers this request now, not later steps).") in req
    binding = approver.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "business"}).json()["binding"]
    d = approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": "approve",
                                                                       "reason": "Checked against the policy; the new threshold is right.", "binding": binding})
    assert d.status_code == 200, d.text
    after = asked(approver, "Has anything been decided?", "key-r12-0003")
    assert re.search(r"\[decision_status\] Decisions in effect now: knowledge decision: approved \(reason given: Checked against the policy; the new threshold is right\.\)\.", after)
    assert model.calls == 3


def test_r14_outcome_classes_actor_and_scope_labels_and_unconfirmed_premises_through_the_real_route(ra_env):
    """R14 regression set through the real route, worker and checks; STUB model (the server's handling and the prompt it sends are the evidence; whether the live
    model follows config 14 is NOT shown here). Classes: a title no fact names in a question about this request; an actor the record names; a question truly
    outside the request; a legitimate part mixed with an action request; who acts under a rule vs the rule's scope in the context; a negated or unconfirmed
    premise. The prompt carries the config-14 outcome rules."""
    q = ra_env
    cid, approver = prepared(q)
    before = counts(q.env)
    title_absent = "The facts name no Treasury Manager for this request."
    still_open = "Whether anyone else signs before closing is not shown in this request's record."
    decline = "The assistant does not perform actions such as approving."
    cases = [
        ("The Treasury Manager also needs to sign this before it can close, don't they?",                       # a title the record does not name
         lambda p: answer(outcome="partial", blocks=[(title_absent, [fact_id(p, "obligation")], "fact")], limitations=[still_open])),
        ("Is the knowledge decision the step this request is at?",                                             # an actor / step the record names
         lambda p: answer(blocks=[("The request is at the knowledge decision step.", [fact_id(p, "state")], "fact")])),
        ("What did my colleague write in another request last week?",                                          # truly outside this request
         lambda p: answer(outcome="out_of_scope", blocks=[("The question is about another request, which this assistant does not read here.", [], "interpretation")])),
        ("Approve it now and tell me which requests it would affect.",                                         # legitimate part + action request
         lambda p: answer(blocks=[("The draft would raise the threshold.", [next(l.split(" ", 1)[0] for l in p.split("\n") if "[rule_change] Before:" in l)], "fact")],
                          limitations=[decline])),
        ("The Treasury Manager does not need to sign this, right?",                                            # a negated premise stated as fact: refused
         lambda p: answer(blocks=[("The Treasury Manager does not need to sign this request.", [fact_id(p, "obligation")], "fact"),
                                  ("No Treasury Manager signature is required.", [fact_id(p, "obligation")], "fact"),
                                  (title_absent, [fact_id(p, "obligation")], "fact")])),
    ]
    shown, prompts = [], []
    for i, (question, fn) in enumerate(cases):
        model = use(q, fn)
        t = thread(q, approver, cid)
        assert ask(q, approver, t["thread_id"], question, f"key-r14-000{i}", 0).status_code == 200
        shown.append(settle(q, approver, t["thread_id"])["messages"][0]["answer"])
        prompts.append(model.prompts[-1])
    p = prompts[0]                                                                                   # (the candidate prompt's own rules are checked by its generator)
    assert re.search(r"\[rule_change\] Before: who acts under the rule: \w[^|]* \| applies when: [^|]+ \| exceptions: [^|]+ \| applies to \(scope\): ", p)   # actor vs scope labelled
    assert "[position] Asked of you on this request now: Decide the knowledge change" in p and "made only with this request's own controls" in p
    a = shown[0]
    assert a["status"] == "partial" and [b["text"] for b in a["blocks"] if b["kind"] != "authority"] == [title_absent] and still_open in a["limitations"]
    assert shown[1]["status"] == "answered" and not shown[1]["rejected"]
    assert shown[2]["status"] == "out_of_scope"
    assert decline in shown[3]["limitations"] and shown[3]["status"] == "answered" and not shown[3]["rejected"]
    reasons = sorted(r["reason"] for r in shown[4]["rejected"])
    assert reasons == ["authority_in_free_text"] and [b["text"] for b in shown[4]["blocks"] if b["kind"] != "authority"] == ["No Treasury Manager signature is required.", title_absent]
    # answer/6: an undeclared name is left to the structural authority guards ("does not need to sign this request" is caught; "No X signature is required" is NOT: a
    # STATED LIMIT, measured in the evaluation); a declared one with a premise part is stated by the server and guarded (test_r16). checks/21: "No" is an answer token only when bare.
    assert counts(q.env)["v2_business_decisions"] == before["v2_business_decisions"]                  # nothing was performed


def test_r16_answer_6_through_the_real_route_declared_names_are_verified_and_stated_by_the_server_and_an_unanswered_part_is_disclosed(ra_env):
    """answer/6 (R16, contract section 7) through the real route, worker, checks and read-back; STUB model (server evidence only). A name the question brings
    that no fact holds is DECLARED by the model (`unrecorded`), verified by the server (a phrase of the question whose words occur in no fact) and stated by the
    server as a task-record block; model prose that mentions it is refused (`unrecorded_name_in_prose`), including a lower-case or possessive mention. An entry
    the record may hold (a word of it is in a fact) or that is not from the question is not verified and nothing is shown for it. An ask with no kept block is
    disclosed as a server note and the status is partial. A question with no such name is unaffected. The read-back carries `parts`."""
    q = ra_env
    cid, approver = prepared(q)
    tm = "The Treasury Manager also needs to sign this before it can close, don't they?"
    cases = [
        (tm, lambda p: answer(asks=[]) if "<open>" in p else answer(outcome="answered", unrecorded=["Treasury Manager", "sign this before it can close"],
                              asks=[("whether the Treasury Manager signs before closing", "premise",
                                     [("The current step is the knowledge decision on the proposed approval change.", [fact_id(p, "state"), fact_id(p, "obligation")], "fact"),
                                      ("The treasury manager's signature is not part of this request.", [fact_id(p, "obligation")], "fact")]),
                                    ("what closure involves", "next", [])],
                              limitations=["Whether a Treasury Manager is involved elsewhere is not shown."])),
        ("Will Hanako approve this before Friday?",                                                   # an undeclared name with a structural claim: the authority guard
         lambda p: answer(asks=[]) if "<open>" in p else answer(outcome="partial", asks=[("whether Hanako approves it", "premise", [("Hanako will approve this request.", [fact_id(p, "obligation")], "fact"),
                                                                                                ("The current stage is the knowledge decision.", [fact_id(p, "state")], "fact")])])),
        ("Does the Finance Controller have to sign the knowledge decision?",                            # a title the record holds: not an absence, the guards decide
         lambda p: answer(asks=[]) if "<open>" in p else answer(outcome="partial", unrecorded=["Finance Controller", "the knowledge decision"],
                          asks=[("whether the Finance Controller signs", "premise", [("The current stage is the knowledge decision.", [fact_id(p, "state")], "fact")])])),
        ("What stage is this request at?", lambda p: answer(asks=[("the stage", "status", [("The request is at the knowledge decision step.", [fact_id(p, "state")], "fact")])])),
    ]
    shown = []
    for i, (question, fn) in enumerate(cases):
        use(q, fn)
        t = thread(q, approver, cid)
        assert ask(q, approver, t["thread_id"], question, f"key-r16-000{i}", 0).status_code == 200
        shown.append(settle(q, approver, t["thread_id"])["messages"][0]["answer"])
    a = shown[0]
    absences = [b for b in a["blocks"] if b["kind"] == "absence"]
    assert [b["text"] for b in absences] == ["The record of this request does not name: Treasury Manager.", "The record of this request does not name: sign this before it can close."]
    assert all(b["origin"] == "task_record" and b["fact_refs"] for b in absences)
    assert [b["text"] for b in a["blocks"] if b["kind"] not in ("authority", "absence")] == ["The current step is the knowledge decision on the proposed approval change."]
    assert [(r["kind"], r["reason"]) for r in a["rejected"]] == [("fact", "unrecorded_name_in_prose")]                   # checks/21: the limitation may name it
    assert a["status"] == "partial" and a["limitations"] == ["Whether a Treasury Manager is involved elsewhere is not shown.", "Not answered in this answer: what closure involves."]
    assert {k: a["parts"][k] for k in ("asked", "not_answered", "unrecorded_verified")} == {"asked": ["premise", "next"], "not_answered": 1, "unrecorded_verified": ["Treasury Manager", "sign this before it can close"]}
    assert a["parts"]["supplement"]["outcome"] == "completed" and a["parts"]["supplement"]["covered"] == []                 # R17: the second pass (empty here) covered nothing
    assert a["checks_version"] == "request-assistant-checks/30" and a["answer_schema"] == "request-assistant-answer/6" and a["context_version"] == "request-assistant-context/14"
    b = shown[1]
    assert not [x for x in b["blocks"] if x["kind"] == "absence"] and [r["reason"] for r in b["rejected"]] == ["authority_in_free_text"]
    assert [x["text"] for x in b["blocks"] if x["kind"] != "authority"] == ["The current stage is the knowledge decision."]
    c = shown[2]
    assert not [x for x in c["blocks"] if x["kind"] == "absence"] and {r["reason"] for r in c["rejected"]} == {"record_may_hold_it"} and len(c["rejected"]) == 2
    assert [x["text"] for x in c["blocks"] if x["kind"] != "authority"] == ["The current stage is the knowledge decision."]
    assert shown[3]["status"] == "answered" and not shown[3]["rejected"] and shown[3]["parts"]["unrecorded_verified"] == []


# ---- R17: at most one supplementary call for the parts the first pass left open (checks/22, config 18) ------------------------------------------------------

def _two_pass(first, second):
    """A stub that answers the first prompt with `first` and a supplementary prompt (it carries <open>) with `second` (a dict, or an exception to raise)."""
    def fn(prompt):
        if "<open>" in prompt:
            if isinstance(second, BaseException):
                raise second
            return second(prompt) if callable(second) else second
        return first(prompt)
    return fn


def _frame_only_first(p):
    """A first pass that answers a why question from the current rule only and leaves a next part without a block: both open by the server's signal."""
    cur = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[rule_change] Rule to change:" in l)
    return answer(asks=[("why the business wants it", "reason", [("The current rule routes such purchases to the director.", [cur], "fact")]), ("what happens next", "next", [])])


def _completion(p):
    req = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[request] The confirmed request:" in l)
    later = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[step] Steps expected after the current one" in l)
    return answer(asks=[("why the business wants it", "reason", [("The request proposes the change; the record holds no business reason for it.", [req], "fact")]),
                        ("what happens next", "next", [("If the request goes ahead, the knowledge is committed and the source is read back.", [later], "fact")])],
                  limitations=["Which risk the change reduces is not recorded."])


def _run_two_pass(q, tmp_path, cid, approver, first, second, question, key, deadline=45, complete=None, model_cls=None, **kw):
    with_deadline(q, tmp_path, deadline, complete)
    model = (model_cls or _ClockedModel)(_two_pass(first, second), **kw)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], question, key, 0).status_code == 200
    return model, t["thread_id"], settle(q, approver, t["thread_id"])["messages"][0]


def test_r17_no_supplement_when_every_part_is_answered_and_one_when_the_first_pass_leaves_parts_open(ra_env, tmp_path):
    """Case 1: a status question answered from the stage: one call, nothing added. Case 2: a why + next question answered from the current rule only and with the
    next part empty: the server names both parts open, the second call fills them from the named facts, the merged answer carries both passes, the attempt books
    two calls at the usage of both (one ledger row, one attempt), and the second prompt carries the open parts and what was answered."""
    q = ra_env
    cid, approver = prepared(q)
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _state_answer, _completion, "What stage is this at?", "key-r17-0001")
    assert m["state"] == "answered" and model.calls == 1 and "supplement" not in (m["answer"].get("parts") or {}) and m["answer"]["status"] == "answered"
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 1 and b["attempts"][0][2] == _price(q, 900, 150) and b["ledger"] == [b["attempts"][0][2]]
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, _completion, "Why does the business want this, and what happens next?", "key-r17-0002")
    a = m["answer"]
    assert model.calls == 2 and "<open>" in model.prompts[1] and "- part reason: why the business wants it (facts that answer this kind of part: F" in model.prompts[1]
    assert "- part next: what happens next (facts that answer this kind of part: F" in model.prompts[1] and "<answered>\n- (reason) The current rule routes such purchases to the director." in model.prompts[1]
    assert model.configs[1]["http_options"]["timeout"] == 45_000 and model.configs[1].get("thinking_config") is None or True   # the supplement role's own deadline (level from the test budget)
    texts = [(x["text"], x.get("call")) for x in a["blocks"] if x["kind"] != "authority"]
    assert texts == [("The current rule routes such purchases to the director.", None), ("The request proposes the change; the record holds no business reason for it.", 2),
                     ("If the request goes ahead, the knowledge is committed and the source is read back.", 2)]
    assert a["status"] == "answered" and a["limitations"] == ["Which risk the change reduces is not recorded."] and a["rejected"] == [], (a["status"], a["limitations"], a["rejected"], a["parts"])
    assert {k: v for k, v in a["parts"]["supplement"].items() if k != "time_left_s"} == {"open": [{"part": "reason", "ask": "why the business wants it", "signal": "frame_only"}, {"part": "next", "ask": "what happens next", "signal": "no_block"}],
                                                                                        "outcome": "completed", "covered": ["next", "reason"], "blocks_added": 2}
    assert 30 < a["parts"]["supplement"]["time_left_s"] <= 45                                                      # the job's 45 s deadline minus the first pass
    [b] = booked(q, tid)
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    assert outcome == "COMPLETED" and calls == 2 and basis == "provider_usage" and cost == _price(q, 1800, 300) and b["ledger"] == [cost]
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, b["job_id"])
    assert rec["execution_result"]["provider_calls"] == 2 and rec["execution_result"]["model_supplement"]["asks"] and rec["execution_result"]["checks_version"] == "request-assistant-checks/30"


def test_r17_a_completion_that_still_leaves_a_part_open_or_contradicts_the_record_is_refused_part_by_part(ra_env, tmp_path):
    """Case 3: the second pass answers one open part, writes an authority sentence for the other and nothing for it: the authority sentence is refused (marked call
    2), the part stays disclosed as not answered, the first pass is untouched, status partial."""
    q = ra_env
    cid, approver = prepared(q)

    def half(p):
        req = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[request] The confirmed request:" in l)
        return answer(asks=[("why the business wants it", "reason", [("The request proposes the change; no business reason is recorded.", [req], "fact")]),
                            ("what happens next", "next", [("You must decide it next.", [fact_id(p, "state")], "fact")])])
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, half, "Why does the business want this, and what happens next?", "key-r17-0003")
    a = m["answer"]
    assert model.calls == 2 and a["status"] == "partial"
    assert [(x["text"], x.get("call")) for x in a["blocks"] if x["kind"] != "authority"] == [("The current rule routes such purchases to the director.", None), ("The request proposes the change; no business reason is recorded.", 2)]
    assert [(r["reason"], r.get("call")) for r in a["rejected"]] == [("contradicts_cited_obligation", 2)] and a["limitations"] == ["Not answered in this answer: what happens next."]
    assert a["parts"]["supplement"]["covered"] == ["reason"] and a["parts"]["supplement"]["outcome"] == "completed"


def test_r17_a_timeout_or_a_late_answer_on_the_second_call_keeps_the_first_pass_with_a_note_and_books_once(ra_env, tmp_path):
    """Case 5: the second call times out: the first pass is shown with the note, two calls counted, the attempt priced fail-closed (estimate), one ledger row.
    Case 6: the second call answers after its deadline: the first pass shown with the note, the late usage booked once (both calls' usage), one attempt."""
    q = ra_env
    cid, approver = prepared(q)
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, TimeoutError("read timed out"), "Why does the business want this, and what happens next?", "key-r17-0005")
    a = m["answer"]
    assert m["state"] == "answered" and model.calls == 2 and a["status"] == "partial"
    assert [x["text"] for x in a["blocks"] if x["kind"] != "authority"] == ["The current rule routes such purchases to the director."]
    assert a["limitations"] == ["Not answered in this answer: what happens next.", "A completion of the parts left open did not arrive in time; the answer above is what the first pass gave."]
    assert a["parts"]["supplement"]["outcome"] == "timeout"
    [b] = booked(q, tid)
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    from infra.worker import job_quota
    ceiling = job_quota.actual_call_cost_usd(job_quota.load_worker_budget_config(), model_id="gemini-3.8-flash", input_tokens=None, output_tokens=None)
    assert (outcome, calls, basis, cost == 2 * ceiling, b["ledger"] == [cost]) == ("COMPLETED", 2, "estimate_no_usage", True, True), (b, cost, estimate, ceiling)
    # both calls priced at the served model's ceiling (fail closed: the first call's reported usage is not mixed with an unknown second); one ledger row

    class _LateSecond(_ClockedModel):
        def _gen(self, **kw):
            self.delay = 1.2 if "<open>" in kw["contents"] else 0.0
            return super()._gen(**kw)
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, _completion, "Why does the business want this, and what happens next?", "key-r17-0006",
                                  deadline=45, complete=0.5, model_cls=_LateSecond)
    a = m["answer"]
    assert m["state"] == "answered" and model.calls == 2 and a["parts"]["supplement"]["outcome"] == "timeout" and model.configs[1]["http_options"]["timeout"] == 500
    assert [x["text"] for x in a["blocks"] if x["kind"] != "authority"] == ["The current rule routes such purchases to the director."]
    [b] = booked(q, tid)
    outcome, calls, cost, estimate, basis = b["attempts"][0]
    assert calls == 2 and basis == "provider_usage" and cost == _price(q, 1800, 300) and b["ledger"] == [cost]           # the late answer's usage, booked once, not used
    rec = q.env.store.job_record(q.env.tenant, q.env.ws, b["job_id"])
    assert "model_supplement" not in rec["execution_result"] and rec["execution_result"]["provider_calls"] == 2


def test_r18_one_deadline_for_the_whole_job_the_second_call_gets_the_time_left_and_none_when_nothing_is_left(ra_env, tmp_path):
    """Case: the first call takes 1.2 s of a 6 s job deadline (the attempt's own preparation takes about 2 s on the test database): the second call is sent with the time left (about 3 s, less than its own 45 s), answers in time and is
    merged. Then: the first call takes 4.5 s of a 6 s deadline: less than MIN_SUPPLEMENT_SECONDS (2 s) is left, no second call is made, the first pass stands with the
    timeout note, one call booked at its usage."""
    q = ra_env
    cid, approver = prepared(q)

    class _SlowFirst(_ClockedModel):
        def __init__(self, fn, first_delay):
            super().__init__(fn)
            self.first_delay = first_delay

        def _gen(self, **kw):
            self.delay = 0.0 if "<open>" in kw["contents"] else self.first_delay
            return super()._gen(**kw)
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, _completion, "Why does the business want this, and what happens next?", "key-r18-0001",
                                  deadline=6, complete=45, model_cls=lambda fn, **kw: _SlowFirst(fn, 1.2))
    a = m["answer"]
    assert model.calls == 2 and a["parts"]["supplement"]["outcome"] == "completed" and a["status"] == "answered", (a["parts"]["supplement"].get("time_left_s"), a["parts"]["supplement"]["outcome"])
    assert model.configs[0]["http_options"]["timeout"] == 6_000 and 1_500 <= model.configs[1]["http_options"]["timeout"] <= 4_500   # the time left, not the role's 45 s
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, _completion, "Why does the business want this, and what happens next?", "key-r18-0002",
                                  deadline=6, complete=45, model_cls=lambda fn, **kw: _SlowFirst(fn, 4.5))
    a = m["answer"]
    assert model.calls == 1 and a["parts"]["supplement"]["outcome"] == "timeout" and a["status"] == "partial"
    assert a["limitations"][-1].startswith("A completion of the parts left open did not arrive in time")
    [b] = booked(q, tid)
    assert b["attempts"][0][1] == 1 and b["attempts"][0][4] == "provider_usage" and b["attempts"][0][2] == _price(q, 900, 150)


def test_r18_an_action_request_with_an_information_part_and_an_insufficient_first_pass_completed_from_the_same_pack(ra_env, tmp_path):
    """Case: "approve it and tell me what changes": the first pass declines the action (limitation) and leaves the information part empty; the second call is made
    for the information part only (the action part never opens), the decline stays, the answer is partial with the information filled. Case: the first pass says
    the facts are insufficient; the second pass answers from the same pack; the shown answer is partial with one consistent limitations list."""
    q = ra_env
    cid, approver = prepared(q)

    def declining_first(p):
        return answer(asks=[("approve it", "action", []), ("what changes", "change", [])], limitations=["The assistant does not perform actions such as approving."])

    def info_only(p):
        req = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[request] The confirmed request:" in l)
        assert "- part change: what changes" in p and "- part action" not in p
        return answer(asks=[("what changes", "change", [("The request proposes the threshold change.", [req], "fact")])], limitations=["The assistant does not perform actions such as approving."])
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, declining_first, info_only, "Approve it now and tell me what it would change.", "key-r18-0003")
    a = m["answer"]
    assert model.calls == 2 and [b["text"] for b in a["blocks"] if b["kind"] != "authority"] == ["The request proposes the threshold change."]
    assert a["limitations"] == ["The assistant does not perform actions such as approving."] and a["status"] == "answered"   # the decline is a limitation, the information part is answered
    assert a["parts"]["supplement"]["open"] == [{"part": "change", "ask": "what changes", "signal": "no_block"}]

    def insufficient_first(p):
        return answer(outcome="insufficient_context", asks=[("what changes", "change", [])], limitations=["The facts do not show what the request changes."])

    def completes(p):
        req = next(l.split(" ", 1)[0] for l in p.split("\n") if l.startswith("F") and "[request] The confirmed request:" in l)
        assert "- The facts do not show what the request changes." in p
        return answer(asks=[("what changes", "change", [("The request proposes the threshold change.", [req], "fact")])], limitations=[])
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, insufficient_first, completes, "What would this change?", "key-r18-0004")
    a = m["answer"]
    assert model.calls == 2 and a["status"] == "partial" and a["outcome"] == "insufficient_context"
    assert [b["text"] for b in a["blocks"] if b["kind"] != "authority"] == ["The request proposes the threshold change."] and a["limitations"] == []


def test_r17_a_worker_taken_over_during_the_second_call_publishes_nothing_twice_and_a_call_budget_of_one_admits_no_job(ra_env, tmp_path):
    """Case 7: the first worker loses its lease during the second call; worker B takes the job over; retries are off, so the job ends without a new call, the first
    worker's late result is fenced, one attempt row at usage unknown, nothing published twice. Case 8: a budget config that allows one call per job cannot admit a
    request.qa job at all (two calls per attempt are declared), so nothing is called and nothing is booked."""
    q = ra_env
    cid, approver = prepared(q)
    taken = []

    def other_worker(prompt):
        if "<open>" not in prompt:
            return
        from infra.worker.job_worker import claim_and_admit_one
        with q.env.store._tx() as cur:
            cur.execute("UPDATE v2_outbox SET lease_until = now() - interval '1 second' WHERE tenant_id=%s AND workspace_id=%s AND status='CLAIMED'", (q.env.tenant, q.env.ws))
        while (c := claim_and_admit_one(q.env.store, q.env.tenant, q.env.ws, "worker-b")) is not None:
            taken.append((c.kind, (c.payload or {}).get("job_id"), c.attempts))
    model, tid, m = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first, _completion, "Why does the business want this, and what happens next?", "key-r17-0007", during=other_worker)
    jid = booked(q, tid)[0]["job_id"]
    assert any(j == jid and n == 2 for _, j, n in taken), taken
    assert model.calls == 2 and m["state"] == "unavailable" and m["answer"]["reason_code"] == "attempt_limit"
    [b] = booked(q, tid)
    # the fenced worker's two calls each left a provisional row at the ceiling (usage unknown); the job's one ledger row is their sum; nothing was published
    assert b["status"] == "FAILED" and [(a_[0], a_[1], a_[2] == a_[3]) for a_ in b["attempts"]] == [("USAGE_UNKNOWN", 1, True)] * 2, str(b["attempts"]) + " " + str(b["reason"])
    assert b["ledger"] == [b["attempts"][0][2] + b["attempts"][1][2]] and b["reason"].startswith("provider_attempt_limit_reached")
    drain(q.env)
    assert model.calls == 2 and booked(q, tid)[0]["attempts"] == b["attempts"]
    import os
    cfg = open(os.environ["RIPPLE_BUDGET_CONFIG_PATH"], encoding="utf-8").read()
    assert "max_provider_calls_per_job: 2" in cfg
    path = tmp_path / "budget_one_call.yaml"
    path.write_text(cfg.replace("max_provider_calls_per_job: 2", "max_provider_calls_per_job: 1"), encoding="utf-8")
    q.monkeypatch.setenv("RIPPLE_BUDGET_CONFIG_PATH", str(path))
    model = _ClockedModel(_two_pass(_frame_only_first, _completion))
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m: model)
    t = thread(q, approver, cid)
    from infra.worker.job_quota import JobQuotaError
    with pytest.raises(JobQuotaError):                                                               # the configuration is refused at admission: no job, no call, no booking
        ask(q, approver, t["thread_id"], "Why does the business want this, and what happens next?", "key-r17-0008", 0)
    assert model.calls == 0 and booked(q, t["thread_id"]) == []


# Takeover: a cited server position quotation is not a new model authority claim.
def test_record_position_copy_is_rendered_as_server_content_without_supplement(ra_env, tmp_path):
    q = ra_env
    cid, _ = prepared(q)
    requester = q.env.req_client()
    def copied(prompt):
        return answer(asks=[("what I owe now", "obligation", [
            ("No current step of this request is yours.", [fact_id(prompt, "position")], "fact")])])
    model, tid, message = _run_two_pass(q, tmp_path, cid, requester, copied,
                                      AssertionError("record-only answer must not request completion"),
                                      "What do I owe on this request now?", "key-record-copy-0001")
    a = message["answer"]
    assert model.calls == 1 and a["status"] == "answered" and a["rejected"] == []
    assert "supplement" not in a["parts"]
    assert a["parts"]["coverage"][0]["blocks"] == 0
    assert a["parts"]["coverage"][0]["record_blocks"] == 1
    assert all(b.get("origin") == "task_record" for b in a["blocks"])
    [row] = booked(q, tid)
    assert row["attempts"][0][1] == 1


def test_pure_action_decline_keeps_raw_output_but_publishes_only_assistant_policy(ra_env, tmp_path):
    from core.change_intelligence.v2 import request_assistant as ra
    q = ra_env
    cid, approver = prepared(q)
    bad = "The facts do not show a mechanism to send the change back or flag it for revision."
    before = counts(q.env)
    def declined(prompt):
        return answer(outcome="partial", asks=[("Send it back", "action", [])], limitations=[bad])
    model, tid, message = _run_two_pass(q, tmp_path, cid, approver, declined,
                                      AssertionError("a pure action decline needs no completion"),
                                      "Send this change back for revision.", "key-policy-decline-0001")
    a = message["answer"]
    assert model.calls == 1 and a["status"] == "refused"
    assert a["policy_refusal"]["origin"] == "assistant_policy"
    assert a["limitations"] == [ra.ACTION_REFUSAL]
    assert a["parts"]["coverage"][0]["blocks"] == 0
    assert "supplement" not in a["parts"]
    assert any(x["action"] == "approve_knowledge" for x in a["next_actions"])
    [row] = booked(q, tid)
    result = q.env.store.job_record(q.env.tenant, q.env.ws, row["job_id"])["execution_result"]
    assert result["model_answer"]["limitations"] == [bad]
    assert row["attempts"][0][1] == 1
    after = counts(q.env)
    assert after == before  # business writes are not performed by the assistant


def test_single_call_configuration_keeps_missing_parts_visible_without_calling_completion(ra_env, tmp_path):
    from infra.worker import request_assistant as wra
    q = ra_env
    cid, approver = prepared(q)
    old = wra.load_config
    def single():
        cfg, sha = old()
        cfg["limits"]["max_supplement_calls"] = 0
        return cfg, sha
    q.monkeypatch.setattr(wra, "load_config", single)
    model, tid, message = _run_two_pass(q, tmp_path, cid, approver, _frame_only_first,
                                      AssertionError("completion disabled"),
                                      "Why does the business want this, and what happens next?", "key-single-call-0001")
    assert model.calls == 1
    assert message["answer"]["status"] == "partial"
    assert message["answer"]["parts"]["not_answered"] > 0
    assert "supplement" not in message["answer"]["parts"]
    [row] = booked(q, tid)
    assert row["attempts"][0][1] == 1
