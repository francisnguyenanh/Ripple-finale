"""Answer integrity v2 (AIG-01 typed authority, AIG-02 bounded priority context; contract docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md section 6). Pure: task-view-shaped
dicts, no DB, no model. Authority is decided from typed fact values and rendered by the server; the pack keeps every mandatory fact or refuses."""
from __future__ import annotations

import random

import pytest

from core.change_intelligence.v2 import request_assistant as ra

LIMITS = {"max_block_chars": 600, "max_limitations": 4, "max_assertions": 4}
REF = {"family": "business_change", "id": "chg-x"}


def tv(obligations=(), waiting=(), actions=(), blockers=(), stage="Waiting for the knowledge decision"):
    return {"request_ref": REF, "stage": {"label": stage}, "title": "t", "obligations": list(obligations), "waiting_on": list(waiting), "blockers": list(blockers),
            "permitted_actions": list(actions), "stages": [], "completion_axes": {}}


def ob(code="decide_knowledge", label="Decide the knowledge change", holder="role_pool", holder_label="an approver", me=False, may=False, required=True, state="current"):
    return {"code": code, "label": label, "holder": holder, "holder_label": holder_label, "obligation_me": me, "eligible_me": may, "assigned_me": me, "required": required,
            "state": state}


def pack_of(view, extra=()):
    return ra.pack(REF, "now", ra.task_facts(view) + list(extra), auth={"actor": "u1"})


def fid(p, kind):
    return next(f["id"] for f in p["facts"] if f["kind"] == kind)


def answer(p, assertions=(), blocks=(), outcome="answered"):                          # answer/2-4 objects (model-chosen assertions): the legacy path kept for replays
    return ra.check_answer({"outcome": outcome, "answer_blocks": list(blocks), "limitations": [], "authority_assertions": list(assertions)}, p, LIMITS, legacy=True)


def A(ref, relation, subject="self", polarity=True, scope="current"):
    return {"subject": subject, "fact_ref": ref, "relation": relation, "polarity": polarity, "scope": scope}


def reasons(out):
    return [r["reason"] for r in out["rejected"]]


# ---------------- AIG-01: typed assertions ----------------

def test_a_judge_who_may_act_but_is_not_asked_is_never_told_the_step_is_theirs():
    p = pack_of(tv([ob(me=False, may=True)]))
    o = fid(p, "obligation")
    assert reasons(answer(p, [A(o, "holds")])) == ["assertion_contradicts_fact"]
    assert reasons(answer(p, [A(o, "must")])) == ["assertion_contradicts_fact"]
    kept = answer(p, [A(o, "may"), A(o, "holds", polarity=False)])
    assert [b["text"] for b in kept["blocks"]] == ["You may do this, but you are not asked to: Decide the knowledge change.",
                                                    "This step is not assigned to you: Decide the knowledge change. It is open to an approver; you may act on it, but you are not asked to."]
    assert all(b["kind"] == "authority" and b["fact_refs"] == [o] for b in kept["blocks"])


def test_optional_versus_required_and_future_versus_current():
    p = pack_of(tv([ob(code="review", label="Review the proposal", me=True, may=True, required=False)]))
    o = fid(p, "obligation")
    assert reasons(answer(p, [A(o, "must")])) == ["assertion_contradicts_fact"]                 # optional is never "required"
    assert answer(p, [A(o, "must", polarity=False)])["blocks"][0]["text"] == "You are not asked to do this: Review the proposal."
    fut = pack_of(tv([ob(code="publish", label="Publish the change", me=True, may=True, state="future")]))
    f = fid(fut, "obligation")
    assert reasons(answer(fut, [A(f, "holds")])) == ["assertion_contradicts_fact"]              # not the current step
    assert answer(fut, [A(f, "holds", scope="later")])["blocks"][0]["text"] == "This later step will be yours: Publish the change."


def test_waiting_holder_blocked_and_unsupported_combinations():
    p = pack_of(tv([ob(state="blocked")], waiting=[{"code": "decide", "holder": "role_pool", "holder_label": "an approver"}],
                   blockers=[{"code": "impact_unresolved", "text": "Two impacted rules are not resolved."}]))
    o, w, b = fid(p, "obligation"), fid(p, "waiting"), fid(p, "blocker")
    assert reasons(answer(p, [A(w, "waiting_on")])) == ["assertion_not_supported"]              # the reader is never "waited on" through a holder fact
    assert reasons(answer(p, [A(o, "holds", subject="holder", polarity=False)])) == ["assertion_not_supported"]
    assert reasons(answer(p, [A(fid(p, "state"), "holds")])) == ["assertion_unknown_fact"]       # a stage fact is not authority
    assert reasons(answer(p, [A("F99", "holds")])) == ["assertion_unknown_fact"]
    out = answer(p, [A(w, "waiting_on", subject="holder"), A(b, "blocked"), A(o, "blocked")])
    assert [x["text"] for x in out["blocks"]] == ["The request is waiting on an approver.", "The request is blocked: Two impacted rules are not resolved.",
                                                  "This step is blocked: Decide the knowledge change."]


def test_a_correct_assertion_with_wrong_prose_is_partial_and_the_prose_is_refused():
    p = pack_of(tv([ob(code="ask_draft", label="Ask for the AI draft", holder="requester", holder_label="the requester")]))
    o = fid(p, "obligation")
    out = answer(p, [A(o, "holds", polarity=False)], [{"text": "You hold the current step, so ask for the draft.", "fact_refs": [o], "kind": "suggestion"}])
    assert out["status"] == "partial" and [b["kind"] for b in out["blocks"]] == ["authority"]
    assert reasons(out) == ["contradicts_cited_obligation"]


def test_a_bare_yes_or_hidden_authority_in_an_explanation_is_refused():
    p = pack_of(tv([ob(me=True, may=True)]))
    st, o = fid(p, "state"), fid(p, "obligation")
    assert reasons(answer(p, blocks=[{"text": "Yes, it is waiting for the knowledge decision.", "fact_refs": [st], "kind": "fact"}])) == ["unbound_affirmation"]
    assert reasons(answer(p, blocks=[{"text": "Because you are responsible for this decision, the change waits for it.", "fact_refs": [o], "kind": "interpretation"}])) \
        == ["authority_in_free_text"]


def test_an_all_refused_answer_is_refused_not_useful():
    p = pack_of(tv([ob()]))
    o = fid(p, "obligation")
    out = answer(p, [A(o, "waiting_on")], [{"text": "You must decide it.", "fact_refs": [o], "kind": "fact"}])     # unsupported: nothing for the server to correct
    assert out["status"] == "refused" and out["blocks"] == []
    corrected = answer(p, [A(o, "holds")], [{"text": "You must decide it.", "fact_refs": [o], "kind": "fact"}])
    assert corrected["status"] == "partial" and [b.get("corrected") for b in corrected["blocks"]] == [True] and not ra.semantic_success(corrected)


def test_two_requests_with_the_same_holder_and_stage_keep_their_own_blocker_explanations():
    """Same holder/stage, different blockers: the server-rendered authority is identical, the LLM's business explanation must be grounded in each request's own blocker."""
    a = pack_of(tv([ob(state="blocked")], blockers=[{"code": "budget", "text": "The budget impact of 5000 EUR is not approved."}]))
    b = pack_of(tv([ob(state="blocked")], blockers=[{"code": "approvals", "text": "Missing 2 approvals from the safety reviewers."}]))
    ba, bb = fid(a, "blocker"), fid(b, "blocker")
    ea = answer(a, [A(ba, "blocked")], [{"text": "The decision waits until finance confirms the 5000 EUR budget impact; that trade-off is the open point.",
                                         "fact_refs": [ba], "kind": "interpretation"}])
    eb = answer(b, [A(bb, "blocked")], [{"text": "Safety review is the open point: 2 approvals are still missing before the decision can go ahead.",
                                         "fact_refs": [bb], "kind": "interpretation"}])
    assert ea["status"] == eb["status"] == "answered"
    assert ea["blocks"][0]["text"] != eb["blocks"][0]["text"] and "5000" in ea["blocks"][1]["text"] and "2 approvals" in eb["blocks"][1]["text"]
    swapped = answer(b, [A(bb, "blocked")], [{"text": "The decision waits until finance confirms the 5000 EUR budget impact.", "fact_refs": [bb], "kind": "interpretation"}])
    assert swapped["status"] == "partial" and reasons(swapped) == ["number_not_in_cited_facts"]     # A's explanation is not accepted for B


def test_assertion_shape_is_validated_and_old_model_objects_stay_valid():
    lim = {"max_assertions": 1, "max_blocks": 6, "max_block_chars": 600, "max_limitations": 4}
    base = {"outcome": "answered", "answer_blocks": [], "limitations": []}
    assert ra.validate_model_object(base, lim)["authority_assertions"] == []
    for bad in ([A("F1", "owns")], [A("F1", "holds", subject="admin")], [dict(A("F1", "holds"), polarity="yes")], [A("F1", "holds"), A("F1", "may")],
                [dict(A("F1", "holds"), extra=1)]):
        with pytest.raises(ValueError):
            ra.validate_model_object({**base, "authority_assertions": bad}, lim)
    assert ra.ANSWER_SCHEMA == "request-assistant-answer/5" and ra.LOCALE == "en"


# ---------------- AIG-02: bounded priority context ----------------

def _support(n):
    return [ra.fact("impact", f"Impact {i:03d}: rule R{i} changes.", "impact", key=f"impact:R{i:03d}") for i in range(n)]


def test_a_blocker_offered_last_after_many_support_facts_is_kept():
    view = tv([ob()], blockers=[{"code": "late", "text": "A late blocker that decides the answer."}])
    facts = _support(120) + ra.task_facts(view)                                                 # mandatory facts offered after 120 support facts
    p = ra.pack(REF, "now", facts, auth={})
    assert any(f["kind"] == "blocker" for f in p["facts"]) and len(p["facts"]) == ra.MAX_FACTS
    t = p["coverage"]["tiers"]
    assert t["mandatory"]["omitted"] == 0 and t["support"]["omitted"] == 120 - (ra.MAX_FACTS - t["mandatory"]["selected"]) and p["coverage"]["truncated"]


def test_mandatory_overflow_or_a_cut_mandatory_fact_is_refused_not_trimmed():
    many = [ra.fact("blocker", f"Blocker {i}.", "task_view", tier="mandatory", key=f"blocker:{i}") for i in range(ra.MAX_FACTS + 1)]
    with pytest.raises(ra.ContextOverflow) as e:
        ra.pack(REF, "now", many, auth={})
    assert e.value.reason == "mandatory_facts_exceed_bounds"
    long = ra.fact("blocker", "Blocker: not " + "x" * 500, "task_view", tier="mandatory", key="blocker:long")
    with pytest.raises(ra.ContextOverflow) as e:
        ra.pack(REF, "now", [long], auth={})
    assert e.value.reason == "mandatory_fact_too_long"
    assert ra.pack(REF, "now", [dict(long, tier="support")], auth={})["coverage"]["truncated"]   # a support fact may be shortened and is flagged


def test_selection_ids_and_hash_are_stable_under_enumeration_order_and_inputs_are_not_mutated():
    view = tv([ob()], waiting=[{"code": "decide", "holder": "role_pool", "holder_label": "an approver"}], blockers=[{"code": "b", "text": "One blocker."}])
    facts = ra.task_facts(view) + _support(30)
    history = [ra.fact("decision", f"Decision {i}.", "decision", tier="history", key=f"decision:{i}") for i in range(5)]
    p1 = ra.pack(REF, "t1", facts + history, auth={"actor": "u1"})
    shuffled = facts[:]
    random.Random(7).shuffle(shuffled)
    p2 = ra.pack(REF, "t2", shuffled + history, auth={"actor": "u1"})                           # different read time, different enumeration
    assert [(f["id"], f["key"]) for f in p1["facts"]] == [(f["id"], f["key"]) for f in p2["facts"]] and p1["manifest_hash"] == p2["manifest_hash"]
    assert all("id" not in f for f in facts + history)
    assert [f["key"] for f in p1["facts"] if f["tier"] == "history"] == [f"decision:{i}" for i in range(5)]   # newest-first order from the caller kept
    assert ra.pack(REF, "t1", facts + history, auth={"actor": "u2"})["manifest_hash"] != p1["manifest_hash"]
    changed = ra.task_facts(tv([ob()], waiting=[{"code": "decide", "holder": "role_pool", "holder_label": "an approver"}], blockers=[{"code": "b", "text": "Another blocker."}]))
    assert ra.pack(REF, "t1", changed + _support(30) + history, auth={"actor": "u1"})["manifest_hash"] != p1["manifest_hash"]


# ---------------- AIG-03 local positive checks (scoring rule, rendering, explanation) ----------------

def test_every_supported_assertion_renders_the_canonical_sentence():
    p = pack_of(tv([ob(me=True, may=True), ob(code="later", label="Publish the change", me=True, may=True, state="future")],
                   waiting=[{"code": "w", "holder": "role_pool", "holder_label": "an approver"}],
                   blockers=[{"code": "b", "text": "Two impacted rules are not resolved."}],
                   actions=[{"action": "approve_knowledge", "label": "approve knowledge", "allowed": True, "current": True}]))
    ob_now = next(f["id"] for f in p["facts"] if f["kind"] == "obligation" and f["data"]["state"] == "current")
    ob_later = next(f["id"] for f in p["facts"] if f["kind"] == "obligation" and f["data"]["state"] == "future")
    expected = [(A(ob_now, "holds"), "You hold the current step: Decide the knowledge change."),
                (A(ob_now, "must"), "You are asked to do this now: Decide the knowledge change."),
                (A(ob_now, "may"), "You may do this: Decide the knowledge change."),
                (A(ob_later, "holds", scope="later"), "This later step will be yours: Publish the change."),
                (A(fid(p, "action"), "may"), "You may do this: approve knowledge."),
                (A(fid(p, "action"), "must"), "This is the step you are asked to do now: approve knowledge."),
                (A(fid(p, "waiting"), "waiting_on", subject="holder"), "The request is waiting on an approver."),
                (A(fid(p, "blocker"), "blocked"), "The request is blocked: Two impacted rules are not resolved.")]
    for a, text in expected:
        out = answer(p, [a])
        assert out["status"] == "answered" and out["blocks"][0]["text"] == text, (a, out)


def test_the_model_still_explains_the_business_cause_and_template_only_or_all_refused_is_not_semantic_success():
    p = pack_of(tv([ob(state="blocked")], blockers=[{"code": "b", "text": "Finance has not confirmed the 5000 EUR budget impact."}]))
    b = fid(p, "blocker")
    explained = answer(p, [A(b, "blocked")], [{"text": "The decision waits because finance has not confirmed the 5000 EUR budget impact; that is the open trade-off.",
                                                 "fact_refs": [b], "kind": "interpretation"}])
    assert explained["status"] == "answered" and [x["kind"] for x in explained["blocks"]] == ["authority", "interpretation"]
    assert explained["explained_fact_kinds"] == ["blocker"] and ra.semantic_success(explained)
    template_only = answer(p, [A(b, "blocked")])
    assert template_only["status"] == "answered" and not ra.semantic_success(template_only)            # server sentence alone: not a semantic answer
    refused = answer(p, [A(b, "holds")], [{"text": "You must fix it.", "fact_refs": [b], "kind": "fact"}])
    assert refused["status"] == "refused" and not ra.semantic_success(refused)
    stage_only = answer(p, blocks=[{"text": "The request is in the knowledge decision stage.", "fact_refs": [fid(p, "state")], "kind": "fact"}])
    assert stage_only["status"] == "answered" and not ra.semantic_success(stage_only)                  # restating the stage explains nothing
    assert not ra.semantic_success({"status": "insufficient_context", "blocks": [], "explained_fact_kinds": []})


def test_two_requests_same_holder_and_stage_score_on_their_own_causes():
    a = pack_of(tv([ob(state="blocked")], blockers=[{"code": "budget", "text": "The budget impact of 5000 EUR is not approved."}]))
    b = pack_of(tv([ob(state="blocked")], blockers=[{"code": "approvals", "text": "Missing 2 approvals from the safety reviewers."}]))
    ra_ = answer(a, [A(fid(a, "blocker"), "blocked")], [{"text": "Finance must still approve the 5000 EUR budget impact.", "fact_refs": [fid(a, "blocker")], "kind": "interpretation"}])
    rb = answer(b, [A(fid(b, "blocker"), "blocked")], [{"text": "Safety review is open: 2 approvals are missing.", "fact_refs": [fid(b, "blocker")], "kind": "interpretation"}])
    assert ra.semantic_success(ra_) and ra.semantic_success(rb)
    assert ra_["blocks"][1]["text"] != rb["blocks"][1]["text"] and ra_["blocks"][0]["text"] != rb["blocks"][0]["text"]
    wrong = answer(b, [A(fid(b, "blocker"), "blocked")], [{"text": "Finance must still approve the 5000 EUR budget impact.", "fact_refs": [fid(b, "blocker")], "kind": "interpretation"}])
    assert not ra.semantic_success(wrong)                                                              # A's cause on B is refused, leaving only the template


# ---------------- AIG-03 v5 (checks/5, answer/3): cause blocks and server-computed blocker coverage ----------------

def two_blockers(codes=("knowledge_moved", "unmapped_changed_rule"), texts=("The business knowledge moved after confirmation.", "A changed rule has no mapped file.")):
    return pack_of(tv([ob(state="blocked")], blockers=[{"code": c, "text": t} for c, t in zip(codes, texts)]))


def blocker_ids(p):
    return [f["id"] for f in p["facts"] if f["kind"] == "blocker"]


def test_every_offered_blocker_is_explained_or_disclosed_never_dropped_silently():
    p = two_blockers()
    b1, b2 = blocker_ids(p)
    one = answer(p, blocks=[{"text": "The request is stopped because the business knowledge moved after confirmation.", "fact_refs": [b1], "kind": "cause"}])
    assert one["blocker_coverage"] == {"offered": 2, "explained": [b1], "unexplained": [b2]}
    assert any(x.startswith("Not explained in this answer: ") and "mapped file" in x for x in one["limitations"])       # disclosed, nothing invented
    both = answer(p, blocks=[{"text": "The request is stopped because the knowledge moved and a changed rule has no mapped file.", "fact_refs": [b1, b2], "kind": "cause"}])
    assert both["blocker_coverage"]["unexplained"] == [] and not any(x.startswith("Not explained") for x in both["limitations"])
    authority_only = answer(p, [A(b1, "blocked"), A(b2, "blocked")])
    assert authority_only["blocker_coverage"]["unexplained"] == [b1, b2]                                     # the server sentence is not an explanation


def test_same_holder_and_stage_with_different_causes_and_an_unknown_blocker():
    a = two_blockers(codes=("knowledge_moved",), texts=("The business knowledge moved after confirmation.",))
    b = two_blockers(codes=("knowledge_moved", "unmapped_changed_rule"))
    ra_ = answer(a, blocks=[{"text": "The request is stopped because the knowledge moved.", "fact_refs": blocker_ids(a), "kind": "cause"}])
    rb = answer(b, blocks=[{"text": "The request is stopped because the knowledge moved.", "fact_refs": blocker_ids(b)[:1], "kind": "cause"}])
    assert ra_["blocker_coverage"]["unexplained"] == [] and len(rb["blocker_coverage"]["unexplained"]) == 1      # the same words do not cover B's second cause
    unknown = pack_of(tv([ob(state="blocked")], blockers=[{"code": None, "text": "Something the analysis could not classify."}]))
    out = answer(unknown, blocks=[{"text": "The stage is waiting for the knowledge decision.", "fact_refs": [fid(unknown, "state")], "kind": "fact"}])
    assert out["blocker_coverage"]["offered"] == 1 and any("could not classify" in x for x in out["limitations"])


def test_no_blocker_adds_nothing_and_a_cause_needs_a_cause_fact_and_no_authority():
    p = pack_of(tv([ob(me=True, may=True)]))
    plain = answer(p, blocks=[{"text": "The decision waits for the rule change to be reviewed.", "fact_refs": [fid(p, "state")], "kind": "fact"}])
    assert plain["blocker_coverage"] == {"offered": 0, "explained": [], "unexplained": []} and plain["limitations"] == []
    assert reasons(answer(p, blocks=[{"text": "The request is at this stage.", "fact_refs": [fid(p, "state")], "kind": "cause"}])) == ["cause_without_cause_fact"]
    q = two_blockers()
    refused = answer(q, blocks=[{"text": "You cannot approve because the knowledge moved.", "fact_refs": blocker_ids(q)[:1], "kind": "cause"}])
    assert reasons(refused) == ["authority_in_free_text"] and len(refused["blocker_coverage"]["unexplained"]) == 2   # refused whole, never trimmed
    assert reasons(answer(q, [A(fid(q, "obligation"), "holds")])) == ["assertion_contradicts_fact"]


def test_a_kept_cause_block_counts_as_semantic_success_and_the_model_may_still_disclose_itself():
    p = two_blockers(codes=("knowledge_moved",), texts=("The business knowledge moved after confirmation.",))
    out = ra.check_answer({"outcome": "partial", "answer_blocks": [{"text": "The request is stopped because the knowledge moved.", "fact_refs": blocker_ids(p), "kind": "cause"}],
                           "limitations": ["The facts do not say when the knowledge moved."], "authority_assertions": []}, p, LIMITS, legacy=True)
    assert ra.semantic_success(out) and out["limitations"] == ["The facts do not say when the knowledge moved."]


# ---------------- checks/6: a contradicted assertion is answered by the server's corrected sentence ----------------

def test_a_contradicted_claim_about_someone_elses_step_shows_that_it_is_not_the_readers():
    for state in ("current", "blocked", "future"):
        p = pack_of(tv([ob(state=state)]))
        o = fid(p, "obligation")
        out = answer(p, [A(o, "holds"), A(o, "must")])
        assert reasons(out) == ["assertion_contradicts_fact"] * 2, state                    # the refusals stay recorded
        assert [(b["text"], b["corrected"]) for b in out["blocks"]] == [("This step is not assigned to you: Decide the knowledge change. It is open to an approver; you cannot act on it now.", True)], state


def test_a_current_claim_about_a_blocked_or_later_step_is_corrected_by_its_stage():
    p = pack_of(tv([ob(state="blocked", me=True, may=True)]))
    o = fid(p, "obligation")
    assert [b["text"] for b in answer(p, [A(o, "holds", subject="holder")])["blocks"]] == ["This step is blocked: Decide the knowledge change."]
    assert [b["text"] for b in answer(p, [A(o, "may")])["blocks"]] == ["This step is blocked: Decide the knowledge change."]   # never "you may not": the stage decides
    later = pack_of(tv([ob(state="future", me=True)]))
    lo = fid(later, "obligation")
    assert [b["text"] for b in answer(later, [A(lo, "must")])["blocks"]] == ["You will be asked to do this later: Decide the knowledge change."]


def test_an_opposite_is_shown_only_when_the_fact_decides_it_and_never_twice():
    p = pack_of(tv([ob(me=False, may=True)], actions=[{"action": "approve", "label": "Approve", "allowed": False, "current": False}]))
    o, act = fid(p, "obligation"), fid(p, "action")
    out = answer(p, [A(o, "may", polarity=False), A(act, "may")])
    assert reasons(out) == ["assertion_contradicts_fact", "assertion_contradicts_fact"]
    assert [b["text"] for b in out["blocks"]] == ["You may do this, but you are not asked to: Decide the knowledge change.", "You may not do this: Approve."]
    dup = answer(p, [A(o, "may"), A(o, "may", polarity=False)])                             # the kept claim and the correction of its opposite are the same sentence
    assert [b["text"] for b in dup["blocks"]] == ["You may do this, but you are not asked to: Decide the knowledge change."]
    done = pack_of(tv([ob(state="done", me=True)]))
    assert answer(done, [A(fid(done, "obligation"), "must")])["blocks"] == []                # a finished step decides nothing about "now"



def test_a_correction_names_the_refused_assertion_and_the_accounting_keeps_model_errors_apart():
    p = pack_of(tv([ob()]))
    o = fid(p, "obligation")
    out = answer(p, [A(o, "holds"), A(o, "may", polarity=False), A(o, "waiting_on")])
    corr = [b for b in out["blocks"] if b.get("corrected")]
    assert [(b["origin"], b["corrects"]) for b in corr] == [("server_correction", 0)]
    assert [r.get("corrected") for r in out["rejected"]] == [True, None]                     # the contradiction is corrected; the unsupported one is not
    assert out["authority_accounting"] == {"model_assertions": 3, "model_kept": 1, "model_contradicted": 1, "model_unsupported": 1, "model_unknown_fact": 0, "authority_missing": False,
                                           "server_corrections": 1, "shown_authority": 2, "shown_from_model": 1}


def test_a_correction_never_carries_a_wrong_or_unsupported_business_cause():
    p = pack_of(tv([ob(state="blocked")], blockers=[{"code": "budget", "text": "The budget impact of 5000 EUR is not approved."}]))
    o, b = fid(p, "obligation"), fid(p, "blocker")
    out = answer(p, [A(o, "holds")], [{"text": "It is blocked because 7000 EUR were never approved.", "fact_refs": [b], "kind": "cause"},
                                      {"text": "The request waits for the knowledge decision.", "fact_refs": [o], "kind": "cause"},
                                      {"text": "Stopped because of the budget; you cannot approve it.", "fact_refs": [b], "kind": "cause"}])
    assert reasons(out) == ["number_not_in_cited_facts", "cause_without_cause_fact", "authority_in_free_text", "assertion_contradicts_fact"]
    assert [b_["kind"] for b_ in out["blocks"]] == ["authority"] and out["blocks"][0]["corrected"]   # only the server's sentence is shown
    assert out["status"] == "partial" and not ra.semantic_success(out)                       # a correction is not a business explanation
    assert out["blocker_coverage"]["unexplained"] == [b] and any(x.startswith("Not explained in this answer") for x in out["limitations"])


def test_a_correction_comes_only_from_the_readers_own_pack_and_differs_by_reader():
    mine, other = pack_of(tv([ob(me=True, may=True)])), pack_of(tv([ob(me=False, may=True)]))
    om, oo = fid(mine, "obligation"), fid(other, "obligation")
    assert [b["text"] for b in answer(mine, [A(om, "holds", polarity=False)])["blocks"]] == ["You hold the current step: Decide the knowledge change."]
    assert [b["text"] for b in answer(other, [A(oo, "holds")])["blocks"]] == ["This step is not assigned to you: Decide the knowledge change. It is open to an approver; you may act on it, but you are not asked to."]
    unknown = answer(mine, [A("F999", "holds")])
    assert unknown["blocks"] == [] and unknown["rejected"][0]["reason"] == "assertion_unknown_fact" and "corrected" not in unknown["rejected"][0]   # no fact, no correction


def test_an_out_of_scope_answer_carries_no_blocker_notes_but_insufficient_context_still_does():
    """Finding of Q3 (AIG03-Q3-20261021-01): "Not explained in this answer: <blocker>" was appended to answers about something else entirely."""
    p = pack_of(tv([ob(state="blocked")], blockers=[{"code": "budget", "text": "The budget impact is not approved."}]))
    oos = answer(p, outcome="out_of_scope", blocks=[{"text": "That is not about this request.", "fact_refs": [], "kind": "interpretation"}])
    assert oos["status"] == "out_of_scope" and not any(x.startswith("Not explained") for x in oos["limitations"])
    assert oos["blocker_coverage"]["unexplained"]                                            # the coverage is still computed and stored
    ins = answer(p, outcome="insufficient_context", blocks=[{"text": "The facts do not say.", "fact_refs": [], "kind": "interpretation"}])
    assert any(x.startswith("Not explained") for x in ins["limitations"])


def test_pool_steps_name_who_they_are_open_to_and_what_this_reader_may_do_never_an_assignment_from_a_role():
    """Owner 2026-10-21: holder vs who may act vs who reads, from the task record and the reader's identity (no assignment inferred from a role or eligibility)."""
    holder = pack_of(tv([ob(me=True, may=True, holder="role_pool")]))                      # the reader is asked (obligation_me)
    assert [b["text"] for b in answer(holder, [A(fid(holder, "obligation"), "holds")])["blocks"]] == ["You hold the current step: Decide the knowledge change."]
    other_approver = pack_of(tv([ob(me=False, may=True)]))                                 # eligible, not asked
    assert [b["text"] for b in answer(other_approver, [A(fid(other_approver, "obligation"), "holds", polarity=False)])["blocks"]] == ["This step is not assigned to you: Decide the knowledge change. It is open to an approver; you may act on it, but you are not asked to."]
    multi_role = pack_of(tv([ob(me=False, may=False)]))                                    # e.g. requester + approver on their own request (distinct approver): may not act
    assert [b["text"] for b in answer(multi_role, [A(fid(multi_role, "obligation"), "holds", polarity=False)])["blocks"]] == ["This step is not assigned to you: Decide the knowledge change. It is open to an approver; you cannot act on it now."]
    named = pack_of(tv([ob(code="answer", label="Answer the open questions", holder="requester", holder_label="the requester", me=False, may=False)]))
    assert [b["text"] for b in answer(named, [A(fid(named, "obligation"), "holds", polarity=False)])["blocks"]] == ["This step is not yours: Answer the open questions (held by the person who asked)."]
    assert any("open to an approver (not assigned to anyone yet)" in str(f.get("display") or "") for f in other_approver["facts"])


# ---------------- answer/5 + checks/8: the server states authority from the task record; the model explains (design note RQA_AUTHORITY_DESIGN_20261021) ----------------

LIM5 = {"max_assertions": 4, "max_blocks": 6, "max_block_chars": 600, "max_limitations": 4}


def answer5(p, blocks=(), outcome="answered", limitations=()):
    model = ra.validate_model_object({"outcome": outcome, "answer_blocks": list(blocks), "limitations": list(limitations)}, LIM5)
    return ra.check_answer(model, p, LIMITS)


def auth_texts(out):
    return [b["text"] for b in out["blocks"] if b["kind"] == "authority"]


def test_a1_a4_the_authority_section_comes_from_the_record_not_from_roles():
    mine = pack_of(tv([ob(me=True, may=True)]))                                                       # A-1 the reader's own current step
    assert auth_texts(answer5(mine)) == ["You hold the current step: Decide the knowledge change."]
    optional = pack_of(tv([ob(me=True, may=True, required=False)]))
    assert auth_texts(answer5(optional)) == ["You hold the current step: Decide the knowledge change (optional)."]
    blocked = pack_of(tv([ob(state="blocked", me=True, may=True)]))                                   # A-2 blocked is never "held now"
    assert auth_texts(answer5(blocked)) == ["This step is blocked: Decide the knowledge change."]
    eligible = pack_of(tv([ob(me=False, may=True)]))                                                  # A-3 / A-4 eligible, not asked: never assigned from eligibility
    assert auth_texts(answer5(eligible)) == ["This step is not assigned to you: Decide the knowledge change. It is open to an approver; you may act on it, but you are not asked to."]
    cannot = pack_of(tv([ob(me=False, may=False)]))
    assert auth_texts(answer5(cannot))[0].endswith("you cannot act on it now.")
    named = pack_of(tv([ob(code="answer", label="Answer the open questions", holder="requester", holder_label="the requester")]))
    assert auth_texts(answer5(named)) == ["This step is not yours: Answer the open questions (held by the person who asked)."]
    assert all(b["origin"] == "task_record" for b in answer5(named)["blocks"] if b["kind"] == "authority")


def test_a5_a9_waits_actions_parallel_steps_and_an_empty_record():
    p = pack_of(tv([ob(), ob(code="review", label="Review the proposal", holder="role_pool", holder_label="a reviewer", me=True, may=True, required=False)],
                   waiting=[{"holder": "approver", "holder_label": "an approver", "code": "decide"}],
                   actions=[{"action": "comment", "label": "Add a comment", "allowed": True, "current": False}, {"action": "approve", "label": "Approve", "allowed": False, "current": False}]))
    t = auth_texts(answer5(p))
    assert "You hold the current step: Review the proposal (optional)." in t and any(x.startswith("This step is not assigned to you: Decide the knowledge change") for x in t)   # A-8 both
    assert "The request is waiting on an approver." in t                                               # A-5
    assert "You may do this: Add a comment." in t and not any("Approve" in x for x in t)               # A-6 a not-allowed action is never offered
    empty = pack_of(tv([]))
    assert auth_texts(answer5(empty)) == ["No step on this request is open right now."]               # A-9


def test_a7_b6_the_section_is_always_there_but_never_stands_in_for_the_model():
    p = pack_of(tv([ob(state="blocked")], blockers=[{"code": "budget", "text": "The budget impact of 5000 EUR is not approved."}]))
    b = fid(p, "blocker")
    good = answer5(p, [{"text": "It stops because finance has not approved the 5000 EUR budget impact.", "fact_refs": [b], "kind": "cause"}])
    assert good["status"] == "answered" and auth_texts(good) and ra.semantic_success(good)             # B-6 useful: authority + the model's explanation
    nothing = answer5(p, [{"text": "You must approve it now.", "fact_refs": [b], "kind": "fact"}])
    assert nothing["status"] == "refused" and auth_texts(nothing) and not ra.semantic_success(nothing)  # A-7 shown; B-1 refused prose never counts as an answer
    assert nothing["authority_accounting"] == {"source": "task_record", "shown_authority": 1, "model_prose_authority_refused": 1}


def test_b2_b4_causes_keep_their_own_checks_and_coverage_counts_only_explanations():
    p = pack_of(tv([ob(state="blocked")], blockers=[{"code": "budget", "text": "The budget impact of 5000 EUR is not approved."},
                                                    {"code": "approvals", "text": "Missing 2 approvals from the safety reviewers."}]))
    b1 = next(f["id"] for f in p["facts"] if f["kind"] == "blocker" and "5000" in f["text"])
    out = answer5(p, [{"text": "Finance has not approved the 7000 EUR impact.", "fact_refs": [b1], "kind": "cause"},
                      {"text": "Finance has not approved the 5000 EUR budget impact.", "fact_refs": [b1], "kind": "cause"}])
    assert reasons(out) == ["number_not_in_cited_facts"]                                               # B-2 a wrong cause is refused on its own
    assert out["blocker_coverage"]["offered"] == 2 and len(out["blocker_coverage"]["unexplained"]) == 1  # B-4
    assert sum(1 for x in out["limitations"] if x.startswith("Not explained in this answer")) == 1


def test_a10_a_truncated_context_is_told_to_the_reader_and_out_of_scope_gets_no_notes():
    many = [ra.fact("rule_change", f"Rule change number {i} with a long explanation " + "x" * 300, "test", key=f"rc:{i}") for i in range(60)]
    p = pack_of(tv([ob()]), extra=many)
    assert p["coverage"]["truncated"]
    out = answer5(p, [{"text": "It waits for the knowledge decision.", "fact_refs": [fid(p, "state")], "kind": "fact"}])
    assert any(x.startswith("Some facts about this request were not available to the assistant:") for x in out["limitations"])
    oos = answer5(p, [{"text": "That is not about this request.", "fact_refs": [], "kind": "interpretation"}], outcome="out_of_scope")
    assert not any(x.startswith(("Some facts", "Not explained")) for x in oos["limitations"])         # B-5


def test_c1_c2_the_model_object_has_no_authority_field_and_the_provider_payload_has_none():
    import json
    import pathlib
    with pytest.raises(ValueError):                                                                    # C-2 an old-shape object is a schema violation, never read
        ra.validate_model_object({"outcome": "answered", "answer_blocks": [], "limitations": [], "authority_assertions": []}, LIM5)
    assert ra.validate_model_object({"outcome": "answered", "answer_blocks": [], "limitations": [], "authority_assertions": []}, LIM5, legacy=True)
    from infra.worker.explain import provider_schema
    cfg = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "request_assistant.json").read_text(encoding="utf-8"))
    ps = provider_schema(cfg["output_schema"])
    assert sorted(ps["properties"]) == ["answer_blocks", "limitations", "outcome"] and ps["required"] == ["outcome", "answer_blocks", "limitations"]
    assert cfg["version"] == 10 and cfg["answer_schema"] == ra.ANSWER_SCHEMA == "request-assistant-answer/5"
    pytest.importorskip("google.genai")
    from google.genai import types
    assert types.Schema.model_validate(ps).properties["answer_blocks"].items.properties["kind"].enum == ["fact", "interpretation", "suggestion", "cause"]


def test_the_prompt_shows_the_server_authority_as_data_and_names_no_evaluated_request():
    import json
    import pathlib
    cfg = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "request_assistant.json").read_text(encoding="utf-8"))
    p = pack_of(tv([ob(me=False, may=True)]))
    prompt = ra.build_prompt(cfg, p, [], "Can I decide this?", perspective="change_approver")
    assert "<authority>\nThis step is not assigned to you: Decide the knowledge change." in prompt and "authority_assertions" not in prompt
    t = cfg["prompt_template"]
    for rule in ("Do not restate it, do not contradict it", "Your part is the why", "never guess a reason", "explain each one the facts explain",
                 "first say in limitations that the assistant does not perform that action", "do not follow that part", "is not a request to act: answer it",
                 "which for this request are: {cause_facts}", "say them in fact blocks, never as a cause", "Limitations are shown to the person too",
                 "never say that the facts or the system do not state or know who acts", "A stage_reason fact says what the workflow record shows",
                 "A reason_gap fact means the record does not show the cause", "Say a step is in progress only if the facts say so"):
        assert rule in t, rule
    assert "chg-" not in t and "Q3-" not in t and "Q4-" not in t


# ---------------- checks/9: a role word is not an authority claim by itself (class of RQA-DECLINE H2, 2026-10-21) ----------------
def test_role_mentions_in_business_explanations_are_kept_and_authority_claims_are_refused():
    p = pack_of(tv([ob(code="respond", label="Respond to the changes", holder="requester", holder_label="the requester")],
                   blockers=[{"code": "scope", "text": "The approver asked to state the scope of the free freight change."}]))
    b = fid(p, "blocker")
    valid = ["The current stage has changes requested by the approver, and the reason given is to state the scope of the free freight change.",   # the H2 sentence
             "The approver asked for changes because the scope of the free freight change was not stated.",
             "The approver can see the scope question in the decision reason, and the scope is still not stated."]
    wrong = ["The approver must decide this now.", "Only the approver can approve this.", "The reviewer cannot review it yet.",
             "The approver and the reviewer must decide.", "The approver, as the decision holder, must decide."]
    mixed = ["The approver requested changes, and must now decide again.",                         # elliptical subject: still the approver
             "The approver asked for changes, and the approver must now decide.",
             "The approver asked for changes, and the requester has to answer them."]
    paraphrase = ["It is up to you to decide.", "The next step is yours.", "We are waiting on you.", "You must answer the approver now."]   # no third-person role subject
    for text in valid:
        out = answer5(p, [{"text": text, "fact_refs": [b], "kind": "fact"}])
        assert out["status"] == "answered" and not out["rejected"], (text, out["rejected"])
    for text in wrong + mixed + paraphrase:
        out = answer5(p, [{"text": text, "fact_refs": [b], "kind": "fact"}])
        assert out["rejected"] and out["rejected"][0]["reason"] in ra.AUTHORITY_TEXT_REASONS, (text, out["rejected"])
        assert out["authority_accounting"]["model_prose_authority_refused"] == 1 and out["status"] == "refused", text
    assert ra.CHECKS_VERSION == "request-assistant-checks/14" and "request-assistant-checks/13" in ra.COMPATIBLE_CHECKS and {"request-assistant-checks/8", "request-assistant-checks/9", "request-assistant-checks/10", "request-assistant-checks/11", "request-assistant-checks/12"} <= ra.COMPATIBLE_CHECKS


# ---------------- checks/10: one cause rule for the model and the validator; authority claims about this request by anyone; guarded limitations ----------------
def cause_prompt(p):
    import json
    import pathlib
    cfg = json.loads((pathlib.Path(__file__).resolve().parents[2] / "config" / "request_assistant.json").read_text(encoding="utf-8"))
    return ra.build_prompt(cfg, p, [], "Why is it here?", perspective="requester")


def test_no_reason_in_the_pack_a_stage_restated_as_a_cause_is_refused_the_stage_as_a_fact_is_kept_and_the_withheld_why_is_said():
    p = pack_of(tv([ob(me=True, may=True)], waiting=[{"holder": "role_pool", "holder_label": "an approver"}]))
    assert ra.cause_basis(p["facts"]) == [] and "which for this request are: (none)." in cause_prompt(p)
    st, wt = fid(p, "state"), fid(p, "waiting")
    out = answer5(p, [{"text": "The request is waiting for the knowledge decision.", "fact_refs": [st], "kind": "fact"},
                      {"text": "The request is stopped because the knowledge decision has not been taken yet.", "fact_refs": [st, wt], "kind": "cause"}])
    assert [r["reason"] for r in out["rejected"]] == ["cause_without_cause_fact"] and out["status"] == "partial"
    assert [b["text"] for b in out["blocks"] if b["kind"] != "authority"] == ["The request is waiting for the knowledge decision."]
    assert out["limitations"][-1].startswith("An explanation of why was not shown: the facts given to the assistant record no reason")
    alone = answer5(p, [{"text": "It waits because of the stage.", "fact_refs": [st], "kind": "cause"}])     # nothing else: refused, not passed off as an answer
    assert alone["status"] == "refused" and alone["limitations"][-1].startswith("An explanation of why was not shown")
    quiet = answer5(p, [{"text": "The request is waiting for the knowledge decision.", "fact_refs": [st], "kind": "fact"}],
                    limitations=["The facts do not record why the request is at this stage."])
    assert quiet["status"] == "answered" and quiet["limitations"] == ["The facts do not record why the request is at this stage."]


def test_one_or_several_reasons_are_listed_to_the_model_a_cause_citing_one_is_kept_and_one_citing_only_the_wait_is_refused():
    one = two_blockers(codes=("knowledge_moved",), texts=("The business knowledge moved after confirmation.",))
    b = blocker_ids(one)[0]
    assert [f["id"] for f in ra.cause_basis(one["facts"])] == [b] and f"which for this request are: {b} [blocker]." in cause_prompt(one)
    kept = answer5(one, [{"text": "It is blocked because the business knowledge moved after confirmation.", "fact_refs": [fid(one, "state"), b], "kind": "cause"}])
    assert kept["status"] == "answered" and not kept["rejected"] and kept["blocker_coverage"]["unexplained"] == []
    wait = answer5(one, [{"text": "It is blocked because the request asks for a change.", "fact_refs": [fid(one, "request")], "kind": "cause"}])
    assert [r["reason"] for r in wait["rejected"]] == ["cause_without_cause_fact"]
    assert "An explanation of why was not shown: it did not cite the fact that records the reason." in wait["limitations"]
    assert any(x.startswith("Not explained in this answer:") for x in wait["limitations"])           # the blocker itself is still disclosed
    many = two_blockers()
    ids_ = blocker_ids(many)
    assert [f["id"] for f in ra.cause_basis(many["facts"])] == ids_ and all(i in cause_prompt(many) for i in ids_)
    both = answer5(many, [{"text": "It is blocked by the two recorded blockers.", "fact_refs": ids_, "kind": "cause"}])
    assert both["status"] == "answered" and both["blocker_coverage"]["unexplained"] == []


def test_authority_claims_about_this_request_by_a_title_or_a_person_are_refused_and_rule_descriptions_are_kept():
    p = two_blockers(codes=("knowledge_moved",), texts=("The business knowledge moved after confirmation.",))
    b = blocker_ids(p)[0]
    claims = ["The Finance Controller has to approve it.", "Someone in finance has to sign this off.", "It must be decided by Hanako Sato.", "The decision lies with the CFO.",
              "The request is waiting on Hanako.", "Hanako will approve this request.", "This change has to be approved by the Finance Controller.",
              "The Finance Controller is the approver for this request.", "It is the Finance Controller's call.",
              "The rule sends these purchases to the Finance Controller, who has to approve it now."]                      # mixed: rule + claim about this request
    rules = ["Purchases from high-risk suppliers go to the Finance Controller regardless of amount.", "Under the proposed rule, the Finance Controller approves these purchases.",
             "The change moves approval of high-risk purchases from the Operations Director to the Finance Controller.",
             "The Finance Controller is the approver for purchases from high-risk suppliers.",
             "The new rule will apply to this request.", "The scope answer should clarify it.", "The draft will complete this step once it is asked for."]   # effects, not who acts
    for text in claims:
        out = answer5(p, [{"text": text, "fact_refs": [b], "kind": "fact"}])
        assert [r["reason"] for r in out["rejected"]] == ["authority_in_free_text"] and out["authority_accounting"]["model_prose_authority_refused"] == 1, text
    for text in rules:
        out = answer5(p, [{"text": text, "fact_refs": [b], "kind": "fact"}])
        assert out["status"] == "answered" and not out["rejected"], (text, out["rejected"])


def test_limitations_are_guarded_like_blocks_a_decline_by_the_assistant_is_kept_and_server_notes_are_never_filtered():
    p = two_blockers(codes=("knowledge_moved",), texts=("The business knowledge moved after confirmation.",))
    b = blocker_ids(p)[0]
    out = answer5(p, [{"text": "It is blocked because the business knowledge moved after confirmation.", "fact_refs": [b], "kind": "cause"}],
                  limitations=["The assistant cannot approve this request; it only reads and explains.", "Only the Finance Controller can approve it.", "You must decide it now."])
    assert out["limitations"][0] == "The assistant cannot approve this request; it only reads and explains."
    assert [(r["kind"], r["reason"], r.get("limitation")) for r in out["rejected"]] == [("limitation", "authority_in_free_text", 1), ("limitation", "authority_in_free_text", 2)]
    assert out["authority_accounting"]["model_prose_authority_refused"] == 2 and out["status"] == "partial"
    unknown = answer5(p, [{"text": "It is blocked because the business knowledge moved after confirmation.", "fact_refs": [b], "kind": "cause"}],     # checks/11 (RQA-V8 P3)
                      limitations=["The facts do not state who holds the responsibility for the next response.", "It is unclear who decides next.",
                                   "The facts do not record why the knowledge moved."])
    assert [r.get("limitation") for r in unknown["rejected"]] == [0, 1] and unknown["limitations"][0] == "The facts do not record why the knowledge moved."
    names = answer5(p, [{"text": "It is blocked because the business knowledge moved after confirmation.", "fact_refs": [b], "kind": "cause"}],     # checks/13 (RQA-V9 P3)
                    limitations=["The facts do not explicitly name the individual person who holds the next action beyond indicating the workflow state.",
                                 "The person who decides next is not named in the facts.", "The facts do not name the people who asked for this change.",
                                 "The facts do not specify which employees asked for this change."])
    assert [r.get("limitation") for r in names["rejected"]] == [0, 1]
    assert names["limitations"][:2] == ["The facts do not name the people who asked for this change.", "The facts do not specify which employees asked for this change."]
    oos = answer5(p, [], outcome="out_of_scope", limitations=["The question is not about this request."])
    assert oos["status"] == "out_of_scope" and oos["limitations"] == ["The question is not about this request."]
