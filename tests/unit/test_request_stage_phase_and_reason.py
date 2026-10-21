"""D1 / D4 (context/3, checks/12): the step phase comes from what the record holds (a system wait, a running job, the current obligation's kind, the step state), the
request assistant's context states it and the recorded stage reason, and a status the model states must agree with it. Pure: the real projection `_dto`, the real
`task_facts` / `pack` / `check_answer`; no DB, no model."""
from __future__ import annotations

from core.change_intelligence.v2 import request_assistant as ra
from infra.adapters import v2_request_tasks as T

O = T._obligation
LIM = {"max_block_chars": 600, "max_limitations": 4, "max_blocks": 6, "max_assertions": 4}


def view(states, obligations=(), waiting=(), blockers=(), family="business_change", spec=T.BUSINESS_STEPS, phases=None, code="C"):
    return T._dto(family, "x", None, "t", {"code": code, "label": "L"}, list(obligations), list(waiting), list(blockers), [], T._steps(spec, states, phases), {}, [], [])


def phases(tv):
    return {s["code"]: s["phase"] for s in tv["stages"] if s["state"] != "future"}


def facts_of(tv, kind):
    return [f for f in ra.task_facts(tv) if f["kind"] == kind]


def answer(tv, blocks=(), limitations=()):
    p = ra.pack({"family": "business_change", "id": "x"}, "now", ra.task_facts(tv), auth={})
    model = ra.validate_model_object({"outcome": "answered", "answer_blocks": list(blocks), "limitations": list(limitations)}, LIM)
    return p, ra.check_answer(model, p, LIM)


DONE3 = {"understand": "done", "draft": "done", "clarify": "done"}
DECIDE = O("decide_knowledge", "Decide the knowledge change", holder="role_pool", holder_label="an approver", eligible_me=False)


def test_phases_come_from_the_record_not_from_the_word_current():
    assert phases(view({"conversation": "done", "intent": "done", "draft": "current"}, [O("ask_draft", "Ask for the AI draft", holder="requester", holder_label="the requester",
                                                                                          eligible_me=False)], family="intent_flow", spec=T.INTENT_STEPS))["draft"] == "not_started"
    assert phases(view({"conversation": "done", "intent": "done", "draft": "current"}, waiting=[{"holder": "system", "holder_label": "the system", "code": "draft_running"}],
                       family="intent_flow", spec=T.INTENT_STEPS))["draft"] == "running"
    assert phases(view({**DONE3, "knowledge_decision": "current"}, [DECIDE]))["knowledge_decision"] == "awaiting_decision"
    sent_back = view({**DONE3, "knowledge_decision": "waiting"}, [O("respond_to_changes", "Respond", holder="requester", holder_label="the requester", eligible_me=False),
                                                                  O("decide_knowledge", "Decide", holder="role_pool", holder_label="an approver", eligible_me=False, state="waiting")])
    assert phases(sent_back)["knowledge_decision"] == "awaiting_action"
    assert phases(view({**DONE3, "knowledge_decision": "waiting"}))["knowledge_decision"] == "awaiting_other_step"
    assert phases(view({**DONE3, "knowledge_decision": "done", "commit": "done", "readback": "current"}))["readback"] == "unknown"
    assert phases(view({**DONE3, "knowledge_decision": "done", "commit": "done", "file_decision": "done", "source_apply": "current"}, phases={"source_apply": "running"}))["source_apply"] == "running"
    blocked = view({"understand": "done", "draft": "done", "clarify": "blocked", "knowledge_decision": "blocked"}, blockers=[{"code": "knowledge_moved", "text": "moved"}])
    assert phases(blocked)["clarify"] == "blocked" and all(s["phase_label"] == T.PHASE_LABEL[s["phase"]] for s in blocked["stages"])


def test_parallel_steps_each_keep_their_own_phase_and_both_are_the_recorded_reason():
    tv = view({**DONE3, "knowledge_decision": "done", "commit": "current", "file_decision": "current"},
              [O("commit_knowledge", "Commit", holder="role_pool", holder_label="a knowledge owner", eligible_me=False),
               O("decide_file_writes", "Decide files", holder="role_pool", holder_label="an approver", eligible_me=False)])
    assert {k: phases(tv)[k] for k in ("commit", "file_decision")} == {"commit": "awaiting_action", "file_decision": "awaiting_decision"}
    reason = facts_of(tv, "stage_reason")
    assert len(reason) == 1 and "Knowledge committed: current step, waiting for an action" in reason[0]["text"] and "File edits decided: current step, waiting for a decision" in reason[0]["text"]
    assert not facts_of(tv, "reason_gap")


def test_a_normal_wait_gives_a_recorded_reason_a_real_blocker_gives_none_beside_it_and_missing_data_is_a_gap():
    normal = view({"conversation": "done", "intent": "done", "draft": "current"}, [O("ask_draft", "Ask for the AI draft", holder="requester", holder_label="the requester",
                                                                                     eligible_me=False)], family="intent_flow", spec=T.INTENT_STEPS)
    r = facts_of(normal, "stage_reason")
    assert r and r[0]["text"] == "What the workflow record shows for the current step: AI draft: current step, not started yet."
    assert "nothing blocks" not in r[0]["text"].lower() and "no blocker" not in r[0]["text"].lower()
    step = facts_of(normal, "step")[0]
    assert "AI draft: current step, not started yet" in step["text"] and ": current." not in step["text"] and step["data"]["phases"]["draft"] == "not_started"
    blocker = view({"understand": "done", "draft": "done", "clarify": "blocked", "knowledge_decision": "blocked"}, blockers=[{"code": "knowledge_moved", "text": "The knowledge moved."}])
    assert facts_of(blocker, "stage_reason") == [] and facts_of(blocker, "reason_gap") == [] and facts_of(blocker, "blocker")
    gap = view({**DONE3, "knowledge_decision": "done", "commit": "done", "readback": "current"})
    assert facts_of(gap, "stage_reason") == [] and "Source read back" in facts_of(gap, "reason_gap")[0]["text"]
    none_current = view({**DONE3, "knowledge_decision": "done"}, code="SOMETHING")
    assert "the current stage" in facts_of(none_current, "reason_gap")[0]["text"]
    finished = view({**DONE3, "knowledge_decision": "done", "commit": "done"}, code="COMMITTED")
    assert facts_of(finished, "reason_gap") == [] and facts_of(finished, "stage_reason") == []
    p = ra.pack({"family": "business_change", "id": "x"}, "now", ra.task_facts(normal), auth={})
    assert [f["kind"] for f in ra.cause_basis(p["facts"])] == ["stage_reason"]                     # the recorded reason may ground a cause; the gap may not
    q = ra.pack({"family": "business_change", "id": "x"}, "now", ra.task_facts(gap), auth={})
    assert ra.cause_basis(q["facts"]) == []


def test_a_cause_from_the_recorded_reason_is_kept_and_a_status_against_the_record_is_refused_in_blocks_and_limitations():
    normal = view({"conversation": "done", "intent": "done", "draft": "current"}, [O("ask_draft", "Ask for the AI draft", holder="requester", holder_label="the requester",
                                                                                     eligible_me=False)], family="intent_flow", spec=T.INTENT_STEPS)
    p, _ = answer(normal)
    sr, st = next(f["id"] for f in p["facts"] if f["kind"] == "stage_reason"), next(f["id"] for f in p["facts"] if f["kind"] == "step")
    _, ok = answer(normal, [{"text": "The request is at this stage because the AI draft has not been started yet.", "fact_refs": [sr], "kind": "cause"},
                            {"text": "The AI draft has not started; nothing is in progress yet.", "fact_refs": [st], "kind": "fact"}])
    assert ok["status"] == "answered" and not ok["rejected"]
    _, bad = answer(normal, [{"text": "The AI draft step is currently underway.", "fact_refs": [st], "kind": "fact"},                            # Flash C1
                             {"text": "The request has stopped.", "fact_refs": [st], "kind": "fact"}],
                    limitations=["Progress has stopped beyond the current stage.", "The facts do not record why beyond the current stage."])
    assert [r["reason"] for r in bad["rejected"]] == ["status_contradicts_record"] * 3 and bad["limitations"][0] == "The facts do not record why beyond the current stage."
    running = view({"conversation": "done", "intent": "done", "draft": "current"}, waiting=[{"holder": "system", "holder_label": "the system", "code": "draft_running"}],
                   family="intent_flow", spec=T.INTENT_STEPS)
    p2, _ = answer(running)
    st2 = next(f["id"] for f in p2["facts"] if f["kind"] == "step")
    _, kept = answer(running, [{"text": "The AI draft is being written.", "fact_refs": [st2], "kind": "fact"}])
    assert not kept["rejected"]
    blocked = view({"understand": "done", "draft": "done", "clarify": "blocked", "knowledge_decision": "blocked"}, blockers=[{"code": "knowledge_moved", "text": "The knowledge moved."}])
    p3, _ = answer(blocked)
    b = next(f["id"] for f in p3["facts"] if f["kind"] == "blocker")
    _, stopped = answer(blocked, [{"text": "The request is stopped because the knowledge moved.", "fact_refs": [b], "kind": "cause"}])
    assert not stopped["rejected"]
