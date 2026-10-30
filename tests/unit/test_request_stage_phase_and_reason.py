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
    model = ra.validate_model_object(six(blocks, limitations), LIM)
    return p, ra.check_answer(model, p, LIM)


def six(blocks, limitations=(), part="status"):
    """R21: the live answer/6 shape (these tests were written for answer/5 and failed at the validator since answer/6; the checks they guard are unchanged)."""
    return {"outcome": "answered", "asks": [{"ask": "the question", "part": part, "blocks": list(blocks)}], "unrecorded": [], "limitations": list(limitations)}


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
    assert r and r[0]["text"] == ("What the workflow record shows for the current step: AI draft: current step, not started yet. "
                                  "What this reader can see of the record gives no reason why AI draft has not started.")   # context/14: not the holder: only its own view
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


def test_blocked_or_on_hold_is_a_status_the_record_must_carry():
    """checks/15 (A104 item H-06, 2026-10-27): "The request is blocked because the knowledge decision is pending" reached the reader while the record had no blocker
    and no blocked step, only a normal wait for a decision. "blocked" / "on hold" are refused unless the record carries a blocker or a blocked / failed phase;
    a negated use stays, and a real blocker keeps the word."""
    waiting = view({"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "current"},
                   [O("decide_knowledge", "Decide the knowledge change", holder="approver", holder_label="an approver", eligible_me=False)])
    p, _ = answer(waiting)
    sr = next(f["id"] for f in p["facts"] if f["kind"] == "stage_reason")
    _, bad = answer(waiting, [{"text": "The request is blocked because the knowledge decision is pending.", "fact_refs": [sr], "kind": "cause"},
                              {"text": "The request is on hold until an approver decides.", "fact_refs": [sr], "kind": "cause"}])
    assert [r["reason"] for r in bad["rejected"]] == ["status_contradicts_record"] * 2
    _, neg = answer(waiting, [{"text": "Nothing is blocked; the request waits for the knowledge decision.", "fact_refs": [sr], "kind": "cause"}])
    assert not neg["rejected"]
    blocker = view({"understand": "done", "draft": "done", "clarify": "blocked", "knowledge_decision": "blocked"}, blockers=[{"code": "knowledge_moved", "text": "The knowledge moved."}])
    p2, _ = answer(blocker)
    b = next(f["id"] for f in p2["facts"] if f["kind"] == "blocker")
    _, kept = answer(blocker, [{"text": "The request is blocked because the knowledge moved.", "fact_refs": [b], "kind": "cause"}])
    assert not kept["rejected"]
    assert ra.CHECKS_VERSION in ra.COMPATIBLE_CHECKS and "request-assistant-checks/14" in ra.COMPATIBLE_CHECKS


def test_a_decision_that_is_no_longer_current_is_not_given_as_the_reason_for_the_current_stage_unless_said_to_be_past():
    """checks/15 (A104 item H-13): "The request is in the knowledge decision stage because changes were requested" cited a request-for-changes decision the
    record marks as no longer current (the draft was revised after it). A cause citing such a decision is refused unless it says the decision is past."""
    waiting = view({"understand": "done", "draft": "done", "clarify": "done", "knowledge_decision": "current"},
                   [O("decide_knowledge", "Decide the knowledge change", holder="approver", holder_label="an approver", eligible_me=False)])
    old = ra.fact("decision", "knowledge decision: request_changes, not current (the draft was revised after this decision); reason given: state the scope.", "decision",
                  tier="history", key="decision:1")
    now = ra.fact("decision", "knowledge decision: approve, current; reason given: fine.", "decision", key="decision:2")
    p = ra.pack({"family": "business_change", "id": "x"}, "now", ra.task_facts(waiting) + [old, now], auth={})
    fid = {f["key"]: f["id"] for f in p["facts"]}

    def check(text, ref):
        model = ra.validate_model_object(six([{"text": text, "fact_refs": [ref], "kind": "cause"}]), LIM)
        return ra.check_answer(model, p, LIM)["rejected"]
    assert [r["reason"] for r in check("It is at this stage because changes were requested to state the scope.", fid["decision:1"])] == ["outdated_decision_as_cause"]
    assert check("Changes were requested earlier, but that decision is no longer current: the draft was revised after it.", fid["decision:1"]) == []
    assert check("It was approved: the knowledge decision is recorded.", fid["decision:2"]) == []                       # a current decision stays a cause


def test_saying_that_the_facts_do_not_state_who_approves_or_has_the_final_say_is_authority_prose():
    """checks/15 (A106 H2-09 / H2-10): limitations claiming authority is unstated were shown although the record states who holds the step. Refused like other
    authority prose; a limitation about a missing reason stays."""
    for t in ("The facts do not state whether the Chief Financial Officer has the final say on this change.",
              "The facts do not mention whether the Regional Manager can overrule the decision.",
              "The record does not say who approves this change."):
        assert ra.free_text_authority(t) == "authority_in_free_text", t
    for t in ("The facts do not record why the threshold was chosen.", "The facts name no Chief Financial Officer for this request."):
        assert ra.free_text_authority(t) is None, t


def test_a_negated_or_questioned_status_in_the_same_clause_is_not_a_claim_and_a_status_after_a_new_clause_still_is():
    """checks/17 (R10, live A112 H3-14): "The facts do not indicate whether ... external progress has stalled." is an honest limitation (the negation and the
    "whether" are more than 25 characters before the word) and was refused as a status claim. A negation or "whether" anywhere earlier in the same clause makes
    the word not a claim; a clause break (punctuation or a joining word such as "and", "but", "because") ends its reach, so "has not started and is stalled"
    is now a claim."""
    normal = view({"conversation": "done", "intent": "done", "draft": "current"}, [O("ask_draft", "Ask for the AI draft", holder="requester", holder_label="the requester",
                                                                                     eligible_me=False)], family="intent_flow", spec=T.INTENT_STEPS)
    p, _ = answer(normal)
    st = next(f["id"] for f in p["facts"] if f["kind"] == "step")
    _, ok = answer(normal, [{"text": "This reads as a pause before the next step: nothing in the record shows the request as blocked or failed.", "fact_refs": [st], "kind": "interpretation"},
                            {"text": "The request is not stalled: the AI draft step simply has not started yet.", "fact_refs": [st], "kind": "interpretation"}],
                   limitations=["The facts do not indicate whether the current duration at this step constitutes an abnormal delay or whether external progress has stalled."])
    assert not ok["rejected"] and len(ok["limitations"]) == 1
    _, bad = answer(normal, [{"text": "The AI draft has not started and the request is stalled.", "fact_refs": [st], "kind": "fact"},
                             {"text": "It is not moving because it is stuck at the draft.", "fact_refs": [st], "kind": "fact"},
                             {"text": "The request is stalled because the AI draft has not started yet.", "fact_refs": [st], "kind": "fact"},
                             {"text": "Nothing has changed for weeks, so the draft is currently underway.", "fact_refs": [st], "kind": "fact"}],
                    limitations=["The record is not clear, but progress has stopped."])
    assert [r["reason"] for r in bad["rejected"]] == ["status_contradicts_record"] * 5 and not [l for l in bad["limitations"] if not l.startswith("Not answered in this answer")]
    assert ra.CHECKS_VERSION in ra.COMPATIBLE_CHECKS and "request-assistant-checks/18" in ra.COMPATIBLE_CHECKS


def test_the_negation_must_govern_the_status_word_a_presupposed_status_or_an_adverbial_whether_is_still_a_claim():
    """checks/18 (R11 review of checks/17): a negation or "whether" earlier in the clause no longer excuses every status word after it. Still claims: a sentence-initial
    "whether ... or not" (adverbial, not an embedded question), a why / reason word that presupposes the status (checks/16 refused "the facts do not record why it
    is stalled", checks/17 kept it), a relative clause ("which leaves it stuck", "it is not the draft that is stalled"), and an honest part joined to a false one.
    Still kept: a negation that governs the status, an embedded "whether", "neither ... nor"."""
    normal = view({"conversation": "done", "intent": "done", "draft": "current"}, [O("ask_draft", "Ask for the AI draft", holder="requester", holder_label="the requester",
                                                                                     eligible_me=False)], family="intent_flow", spec=T.INTENT_STEPS)
    p, _ = answer(normal)
    claims = ["Whether or not anyone acts the request is stuck.", "Whether the requester acts or not, the request is stuck.",
              "The facts do not record why the request is stalled.", "Nothing records the reason the request is stuck.",
              "The requester has not acted which leaves it stuck.", "It is not the draft that is stalled.",
              "The facts hold no dates, and the request is stalled.", "The request is not stuck; it is stalled.",
              "The facts do not record who put the request on hold."]
    kept = ["It is unclear whether the request is stuck.", "Neither blocked nor stalled, the request waits at the draft step.",
            "Nothing shows that the request is blocked or failed.", "There is no blocker, so it is not stalled."]
    assert [t for t in claims if not ra.status_problem(t, p["facts"])] == []
    assert [t for t in kept if ra.status_problem(t, p["facts"])] == []
