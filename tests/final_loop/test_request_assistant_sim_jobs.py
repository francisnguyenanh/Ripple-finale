"""The judge's local simulator (tools/judge_local/sim_final.py) as the provider of the request assistant, through the real routes, admission, worker, checks and
read-back on the throwaway PostgreSQL. R23 finding: its decline was still the answer/5 shape, which the worker refuses as a schema violation since answer/6, so
a judge running the simulated mode saw the assistant unavailable. The simulated decline is now an answer/6 object: the person sees the server's authority
section and the simulated note, never an invented answer. Not AI-quality evidence."""
from __future__ import annotations

from infra.worker import explain
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import ask, ra_env, settle, thread  # noqa: F401  (ra_env is a fixture)
from tools.judge_local import sim_final as sf


def test_the_simulated_provider_declines_with_an_answer6_object_and_the_person_sees_the_server_section_and_the_note(ra_env):
    q = ra_env
    cid, approver = prepared(q)
    client = sf.SimFinal("gemini-3.8-flash", fallback=None)
    q.monkeypatch.setattr(explain, "_client_for_model", lambda m, location=None: client)
    t = thread(q, approver, cid)
    assert ask(q, approver, t["thread_id"], "Where does this request stand?", "key-r23-sim-0001", 0).status_code == 200
    m = settle(q, approver, t["thread_id"])["messages"][0]
    assert m["state"] == "answered", m                                                          # not "unavailable" (the schema violation of the answer/5 shape)
    a = m["answer"]
    assert a["status"] == "insufficient_context" and a["answer_schema"] == "request-assistant-answer/6"
    assert any(b.get("origin") == "task_record" for b in a["blocks"])                           # the server's authority section still reaches the person
    assert any(x.startswith("SIMULATED model") for x in a["limitations"]) and not [b for b in a["blocks"] if b.get("origin") != "task_record"]
