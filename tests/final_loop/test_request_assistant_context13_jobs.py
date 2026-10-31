"""context/13 and checks/29 (R23, after the config 30 development run) through the real task view, context builder, routes, worker and checks on the throwaway
PostgreSQL: a current step not started says that the record gives no reason and is offered for "why"; a premise part is offered the recorded steps, an answer
that describes them is shown while the server states the name the record lacks; "waiting rather than stuck" is kept and "stuck rather than waiting" refused.
The provider is the SYNTHETIC recording stub (no real model; not AI-quality evidence)."""
from __future__ import annotations

import pytest

from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, ask, fact_id, ra_env, settle, thread, use  # noqa: F401  (ra_env is a fixture)
from tests.pitch_brief.chain import S1, build, intent


@pytest.fixture
def ch(env, monkeypatch):
    return build(env, monkeypatch)


def _basis_line(prompt: str, part: str) -> str:
    return next(line for line in prompt.split("\n") if line.startswith(f"{part} ("))


def test_r23_a_step_not_started_says_no_reason_is_recorded_and_is_offered_for_why(env, ch):
    from core.change_intelligence.v2 import request_assistant as ra
    from infra.adapters import v2_request_assistant as vra
    req = env.req_client()
    iid = intent(env, req, S1)["intent_id"]                                                              # stated and confirmed, no draft yet
    def reason_of(account, roles):
        pk = vra.build_context(env.store, env.tenant, env.ws, "intent_flow", iid, {"tenant_id": env.tenant, "workspace_id": env.ws, "account_id": account,
                                                                                  "roles": roles})
        return pk, next(f for f in pk["facts"] if f["kind"] == "stage_reason")
    pk, sr = reason_of(env.req, ("requester",))                                                          # the holder's complete view: none recorded
    assert sr["text"].endswith("The record gives no reason why AI draft has not started.") and sr["data"]["reason_recorded"] is False
    assert sr["id"] in ra.basis_map(pk["facts"])["reason"] and pk["version"] == "request-assistant-context/14"
    _, sr = reason_of(env.req, ("requester", "ai_suspended"))                                            # context/14: the holder's own recorded reason
    assert sr["text"].endswith("The record notes for AI draft: AI use is switched off for you.") and sr["data"]["reason_recorded"] is True
    _, sr = reason_of("someone-else", ("viewer",))                                                       # another reader sees only its own rights
    assert sr["text"].endswith("What this reader can see of the record gives no reason why AI draft has not started.") and sr["data"]["reason_recorded"] is None


def _moved(q):
    from tests.final_loop.test_kno05_accept import answer as answer_gap
    cid, approver = prepared(q)
    gap = next(g for g in q.admin.get("/v2/business/gaps", params={"workspace_id": q.env.ws}).json()["gaps"] if g["kind"] == "missing_actor")
    answer_gap(q.admin, q.env, gap, {"actor": "finance director"}, "Only the finance director approves these.")
    return cid, approver


def test_r23_a_named_step_the_record_lacks_is_answered_with_the_recorded_steps_and_the_server_states_the_name(ra_env):
    q = ra_env
    cid, _approver = _moved(q)
    question = "My manager says the Regional Compliance Lead must bless it first. What does that step involve?"
    text = ("The recorded steps are the knowledge decision, blocked because the business knowledge changed after confirmation, and stating the request again "
            "as a new change request.")

    def model_fn(p):
        obls = [line.split(" ", 1)[0] for line in p.split("\n") if line.startswith("F") and "[obligation]" in line]
        return answer(asks=[("what that step involves", "premise", [(text, obls + [fact_id(p, "blocker")], "fact")])], unrecorded=["Regional Compliance Lead"])
    model = use(q, model_fn)
    t = thread(q, q.req, cid)
    ask(q, q.req, t["thread_id"], question, "key-r23c13-0001", 0)
    a = settle(q, q.req, t["thread_id"])["messages"][0]["answer"]
    prompt = model.prompts[0]
    obls = [line.split(" ", 1)[0] for line in prompt.split("\n") if line.startswith("F") and "[obligation]" in line]
    assert obls and all(o in _basis_line(prompt, "premise") for o in obls)                         # context/13: the premise part is offered the recorded steps
    assert a["context_version"] == "request-assistant-context/14" and a["checks_version"] == "request-assistant-checks/31"
    assert any(b["text"] == text for b in a["blocks"]) and not a["rejected"]
    assert any(b.get("kind") == "absence" and "Regional Compliance Lead" in b["text"] for b in a["blocks"])   # the absence is the server's


@pytest.mark.parametrize("text, kept", [("It is waiting for the knowledge decision rather than stuck or stopped.", True),
                                        ("It is stuck rather than waiting for the knowledge decision.", False)])
def test_r23_a_contrastive_negation_is_read_by_clause_through_the_worker(ra_env, text, kept):
    q = ra_env
    cid, _approver = prepared(q)                                                                          # waiting for the decision, no blocker
    use(q, lambda p: answer(asks=[("is it stuck", "status", [(text, [fact_id(p, "state")], "interpretation")])]))
    t = thread(q, q.req, cid)
    ask(q, q.req, t["thread_id"], "Is it stuck, or just waiting?", "key-r23c13-%d" % kept, 0)
    a = settle(q, q.req, t["thread_id"])["messages"][0]["answer"]
    assert any(b["text"] == text for b in a["blocks"]) is kept
    assert any(r["reason"] == "status_contradicts_record" for r in a["rejected"]) is (not kept)


def test_r23_checks30_a_kept_limitation_reaches_the_reader_with_the_fact_it_rests_on(ra_env):
    q = ra_env
    cid, _approver = prepared(q)                                                                          # waiting for the decision: later steps apply
    later = "The facts do not record who acts on later steps until each step starts."
    use(q, lambda p: answer(asks=[("what happens next", "next", [("The request waits for the knowledge decision.", [fact_id(p, "state")], "fact")])],
                            limitations=[later]))
    t = thread(q, q.req, cid)
    ask(q, q.req, t["thread_id"], "Who signs off after this step?", "key-r23c30-0001", 0)
    m = settle(q, q.req, t["thread_id"])["messages"][0]
    a = m["answer"]
    assert a["checks_version"] == "request-assistant-checks/31" and later in a["limitations"]               # the model's words, unchanged
    sup = [s for s in a["limitation_support"] if s["text"] == later]
    assert len(sup) == 1 and sup[0]["basis"] == "later_step_holders"
    ref = sup[0]["fact_refs"][0]
    assert m["citations"][ref]["text"] == "Who acts on each later step is recorded only when that step starts."   # shown to the reader from its own pack
