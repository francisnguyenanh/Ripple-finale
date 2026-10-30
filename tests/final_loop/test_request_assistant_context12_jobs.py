"""context/12 (R23, H7-43 / H7-29 classes) through the real task view, context builder and worker on the throwaway PostgreSQL: the later-steps fact follows
whether THIS request continues - waiting for the decision, the knowledge moved (a new, separate request), changes requested, rejected, committed - and the
decision choices are a fact only while the decision is open. The provider is the SYNTHETIC recording stub (no real model; not AI-quality evidence)."""
from __future__ import annotations

from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, ask, fact_id, ra_env, settle, thread, use  # noqa: F401  (ra_env is a fixture)


def _facts(q, cid, account, roles) -> dict:
    from infra.adapters import v2_request_assistant as vra
    pk = vra.build_context(q.env.store, q.env.tenant, q.env.ws, "business_change", cid, {"tenant_id": q.env.tenant, "workspace_id": q.env.ws, "account_id": account,
                                                                                          "roles": roles})
    return {f["key"]: f for f in pk["facts"]}


def _decide(q, approver, cid, action):
    binding = q.req.get(f"/v2/business/changes/{cid}/review", params={"workspace_id": q.env.ws, "mode": "business"}).json()["binding"]
    d = approver.post(f"/v2/business/changes/{cid}/review/decide", json={"workspace_id": q.env.ws, "kind": "knowledge", "action": action, "reason": "State the scope.",
                                                                       "binding": binding})
    assert d.status_code == 200, d.text


def test_r23_waiting_for_the_decision_the_request_continues_and_the_choices_are_offered(qa_env):
    q = qa_env
    cid, _approver = prepared(q)
    f = _facts(q, cid, q.env.req, ("requester",))
    assert f["steps:later"]["text"].startswith("Steps expected after the current one if the request goes ahead") and "steps:later:holders" in f
    assert "request changes returns it to the person who asked" in f["decision:options"]["text"]


def test_r23_after_the_knowledge_moved_this_request_cannot_continue_and_no_path_is_offered_for_it(qa_env):
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    q = qa_env
    cid, _approver = prepared(q)
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")
    f = _facts(q, cid, q.env.req, ("requester",))
    later = f["steps:later"]["text"]
    assert later.startswith("This request cannot continue: it has to be stated again as a new change request, which is a separate request.")
    assert "will not take place on this request" in later and "The steps and holders of a new request are not part of this record." in later
    assert "if the request goes ahead" not in later and "steps:later:holders" not in f and "decision:options" not in f   # the decision is blocked: no choices offered


def test_r23_after_changes_were_requested_later_steps_follow_a_revised_version(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    _decide(q, approver, cid, "request_changes")
    f = _facts(q, cid, q.env.req, ("requester",))
    assert f["steps:later"]["text"].startswith("Steps expected after the current one once a revised version answers the requested changes")
    assert "decision:options" in f                                                                        # the decision waits for the revised version


def test_r23_a_rejected_change_has_ended_and_offers_no_later_steps(qa_env):
    q = qa_env
    cid, approver = prepared(q)
    _decide(q, approver, cid, "reject")
    f = _facts(q, cid, q.env.req, ("requester",))
    assert f["steps:later"]["text"].startswith("This request has ended (") and "no further step of this request is expected" in f["steps:later"]["text"]
    assert "steps:later:holders" not in f and "decision:options" not in f


def test_r23_a_committed_change_is_neither_ended_nor_a_new_request_and_offers_no_decision_choices(qa_env):
    from tests.final_loop.test_syn02_reconcile import committed_change
    q = qa_env
    cid = committed_change(q)
    f = _facts(q, cid, q.env.admin, ("admin",))
    later = (f.get("steps:later") or {}).get("text", "")
    assert "cannot continue" not in later and "has ended" not in later and "decision:options" not in f


def test_r23_the_worker_sends_the_cannot_continue_fact_in_the_prompt_and_the_next_basis(ra_env):
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    q = ra_env
    cid, _approver = prepared(q)
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")
    model = use(q, lambda p: answer(asks=[("what I am waiting on", "next", [("The request is blocked because the business knowledge changed.", [fact_id(p, "blocker")], "fact")])]))
    t = thread(q, q.req, cid)
    ask(q, q.req, t["thread_id"], "What am I waiting on before I can see this move?", "key-r23c12-0001", 0)
    m = settle(q, q.req, t["thread_id"])["messages"][0]
    prompt = model.prompts[0]
    line = next(x for x in prompt.split("\n") if "This request cannot continue" in x)
    fid = line.split(" ", 1)[0]
    assert "if the request goes ahead" not in line and fid in next(x for x in prompt.split("\n") if x.startswith("next ("))
    assert m["answer"]["context_version"] == "request-assistant-context/14" and m["state"] == "answered"
