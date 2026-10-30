"""context/11 and checks/27 (R23 mandate) through the real routes, admission, worker, checks and read-back on the throwaway PostgreSQL. The provider transport is the
SYNTHETIC recording stub (no real model; not AI-quality evidence). Covers: a next part is offered the current state; a judgement answered within the record is
shown and a certainty the record cannot give is refused; honest absences (a time; the holders of later steps when the pack says when they are recorded) are
shown and an unstated holder of the current step is refused; every refusal is counted in the stored answer."""
from __future__ import annotations

from core.change_intelligence.v2 import request_assistant as ra
from tests.final_loop.test_chg04_review import prepared
from tests.final_loop.test_kno01_business_store import ws  # noqa: F401  (fixture)
from tests.final_loop.test_qry03_qa import qa_env  # noqa: F401  (fixture)
from tests.final_loop.test_request_assistant_jobs import answer, ask, fact_id, ra_env, settle, thread, use  # noqa: F401  (ra_env is a fixture)

HOLDERS = "Who acts on each later step is recorded only when that step starts."


def _basis_line(prompt: str, part: str) -> str:
    return next(line for line in prompt.split("\n") if line.startswith(f"{part} ("))


def test_r23_a_judgement_within_the_record_and_honest_absences_are_shown_while_certainty_and_an_unstated_current_holder_are_refused(ra_env):
    q = ra_env
    cid, _approver = prepared(q)

    def model_fn(p):
        st = fact_id(p, "state")
        later = [line.split(" ", 1)[0] for line in p.split("\n") if line.startswith("F") and HOLDERS in line]
        lims = ["The facts do not show a timeline or a date for the decision.", "The facts do not show who approves the current step."]
        if later:
            lims.append("The facts do not show who signs off on later steps until each step starts.")
        return answer(asks=[("did something go wrong", "status", [("Nothing in the record shows a failure: no blocker is recorded and the current step has not started yet.", [st], "interpretation"),
                                                                    ("The request is on track.", [st], "interpretation")])], limitations=lims)
    model = use(q, model_fn)
    t = thread(q, q.req, cid)
    ask(q, q.req, t["thread_id"], "Did something go wrong with it, or has it simply not reached its turn?", "key-r23c27-0001", 0)
    m = settle(q, q.req, t["thread_id"])["messages"][0]
    a = m["answer"]
    prompt = model.prompts[0]
    assert model.calls == 1 and a["checks_version"] == "request-assistant-checks/30" and a["context_version"] == "request-assistant-context/14"
    assert fact_id(prompt, "state") in _basis_line(prompt, "next")                                    # context/11: the next part is offered where the request is now
    shown = [b["text"] for b in a["blocks"] if b["kind"] != "authority"]
    assert shown == ["Nothing in the record shows a failure: no blocker is recorded and the current step has not started yet."]
    reasons = sorted(r["reason"] for r in a["rejected"])
    assert "status_contradicts_record" in reasons and "authority_in_free_text" in reasons                # "on track" and the unstated current holder
    model_lims = [x for x in a["limitations"] if not x.startswith(ra.SERVER_NOTE_PREFIXES)]
    assert "The facts do not show a timeline or a date for the decision." in model_lims
    assert "The facts do not show who approves the current step." not in model_lims
    if HOLDERS in prompt:
        assert "The facts do not show who signs off on later steps until each step starts." in model_lims
    assert a["status"] == "partial" and a["authority_accounting"]["model_prose_authority_refused"] >= 1  # refusals are counted, never dropped
