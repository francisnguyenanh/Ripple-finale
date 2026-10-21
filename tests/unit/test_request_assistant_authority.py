"""Request assistant authority checks v2 (class of the A2 live P5 error): a block that says who holds a step, who is waited on, whether a step is required or whether the
reader may act must agree with the TYPED values of the facts it cites; a valid reference alone is not enough. Pure: built from task-view-shaped dicts, no DB, no model."""
from __future__ import annotations

from core.change_intelligence.v2 import request_assistant as ra

LIMITS = {"max_block_chars": 600, "max_limitations": 4}


def tv(obligations=(), waiting=(), actions=()):
    return {"request_ref": {"family": "business_change", "id": "chg-x"}, "stage": {"label": "Waiting for the knowledge decision"}, "title": "t",
            "obligations": list(obligations), "waiting_on": list(waiting), "blockers": [], "permitted_actions": list(actions), "stages": [], "completion_axes": {}}


def ob(code="decide_knowledge", label="Decide the knowledge change", holder="role_pool", holder_label="an approver (not the person who asked)", me=False, may=False,
       required=True, state="current"):
    return {"code": code, "label": label, "holder": holder, "holder_label": holder_label, "obligation_me": me, "eligible_me": may, "assigned_me": False, "required": required, "state": state}


def pack_of(view):
    return ra.pack({"family": "business_change", "id": "chg-x"}, "now", ra.task_facts(view), auth={})


def ids(p, kind):
    return [f["id"] for f in p["facts"] if f["kind"] == kind]


def run(p, text, refs, kind="fact", outcome="answered"):
    return ra.check_answer({"outcome": outcome, "answer_blocks": [{"text": text, "fact_refs": refs, "kind": kind}], "limitations": []}, p, LIMITS, legacy=True)


def only_typed(p, text, refs, kind="fact", **assertion):
    """Checks v4 (AIG-01): a RIGHT authority claim in free text is still refused; the same claim is kept only as a typed assertion, rendered by the server."""
    assert run(p, text, refs, kind)["rejected"][0]["reason"] == "authority_in_free_text", text
    a = {"subject": "self", "polarity": True, "scope": "current", "fact_ref": refs[0], **assertion}
    out = ra.check_answer({"outcome": "answered", "answer_blocks": [], "limitations": [], "authority_assertions": [a]}, p, LIMITS, legacy=True)
    assert out["status"] == "answered" and out["blocks"][0]["kind"] == "authority", (text, out)
    return out["blocks"][0]["text"]


def test_facts_carry_typed_values_the_model_does_not_see():
    p = pack_of(tv([ob(me=True, may=True)], [{"holder": "role_pool", "holder_label": "an approver"}], [{"action": "approve_knowledge", "label": "approve knowledge", "allowed": True}]))
    o = next(f for f in p["facts"] if f["kind"] == "obligation")
    assert o["data"] == {"holder": "role_pool", "holder_label": "an approver (not the person who asked)", "you": True, "may": True, "assigned": False, "required": True, "state": "current",
                         "label": "Decide the knowledge change"}
    prompt = ra.build_prompt({"prompt_template": "{facts}", "limits": {"max_prompt_chars": 9999}}, p, [], "q", perspective="x")
    assert "data" not in prompt and "'you'" not in prompt


def test_requester_approver_reviewer_confusion_is_refused():
    p = pack_of(tv([ob()]))
    o = ids(p, "obligation")
    assert run(p, "The requester must decide the knowledge change.", o)["rejected"][0]["reason"] == "holder_not_grounded"
    assert run(p, "The reviewer has to decide the knowledge change.", o)["rejected"][0]["reason"] == "holder_not_grounded"
    assert only_typed(p, "An approver has to decide the knowledge change.", o, subject="holder", relation="holds") ==         "An approver (not the person who asked) holds this step: Decide the knowledge change."


def test_you_versus_they_and_the_p5_sentence():
    viewer = pack_of(tv([ob(code="ask_draft", label="Ask for the AI draft", holder="requester", holder_label="the requester", me=False, may=False)]))
    o = ids(viewer, "obligation")
    p5 = run(viewer, "You may not approve this request yourself at this current stage as you hold the obligation for the current step.", o, "suggestion")
    assert p5["status"] == "refused" and p5["rejected"][0]["reason"] == "contradicts_cited_obligation"         # not an empty "success"
    assert only_typed(viewer, "You do not hold the current step; the requester holds it.", o, relation="holds", polarity=False) ==         "This step is not yours: Ask for the AI draft (held by the person who asked)."


def test_permission_is_not_assignment_admin_may_but_is_not_asked():
    admin = pack_of(tv([ob(me=False, may=True)], actions=[{"action": "approve_knowledge", "label": "approve knowledge", "allowed": True}]))
    o, a = ids(admin, "obligation"), ids(admin, "action")
    assert run(admin, "You are required to decide the knowledge change.", o)["rejected"][0]["reason"] == "contradicts_cited_obligation"
    assert only_typed(admin, "You can approve the knowledge change.", a, relation="may") == "You may do this: approve knowledge."   # permission: true
    assert run(admin, "You cannot approve the knowledge change.", a)["rejected"][0]["reason"] == "contradicts_cited_permission"


def test_a_negated_ability_that_contradicts_an_allowed_action_and_a_held_obligation_is_refused():
    appr = pack_of(tv([ob(me=True, may=True)], actions=[{"action": "approve_knowledge", "label": "approve knowledge", "allowed": True}]))
    refs = ids(appr, "obligation") + ids(appr, "action")
    out = run(appr, "You cannot approve because you hold the current step.", refs)
    assert out["rejected"][0]["reason"] == "contradicts_cited_permission"
    req = pack_of(tv([ob()], actions=[{"action": "approve_knowledge", "label": "approve knowledge", "allowed": False, "reason": "your role cannot decide this change"}]))
    assert only_typed(req, "Your role cannot decide this change.", ids(req, "action"), relation="may", polarity=False) == "You may not do this: approve knowledge."
    assert run(req, "You can approve the knowledge change now.", ids(req, "action"), "suggestion")["rejected"][0]["reason"] == "contradicts_cited_permission"


def test_an_optional_task_is_never_presented_as_required():
    rev = pack_of(tv([ob(code="review_optional", label="Review the proposal (optional)", holder="role_pool", holder_label="a reviewer", me=True, may=True, required=False)]))
    o = ids(rev, "obligation")
    assert only_typed(rev, "You may review the proposal before the approver decides.", o, "suggestion", relation="may") ==         "You may do this: Review the proposal (optional)."
    assert run(rev, "You must review the proposal.", o, "suggestion")["rejected"][0]["reason"] == "contradicts_cited_obligation"


def test_role_pool_and_system_holders_and_waiting_claims():
    sysv = pack_of(tv(waiting=[{"holder": "system", "holder_label": "the system"}]))
    w = ids(sysv, "waiting")
    assert only_typed(sysv, "The request is waiting on the system.", w, subject="holder", relation="waiting_on") == "The request is waiting on the system."
    assert run(sysv, "The request is waiting for the approver.", w)["rejected"][0]["reason"] == "holder_not_grounded"
    pool = pack_of(tv(waiting=[{"holder": "role_pool", "holder_label": "an approver"}]))
    assert only_typed(pool, "It is waiting for an approver to decide.", ids(pool, "waiting"), subject="holder", relation="waiting_on") == "The request is waiting on an approver."


def test_a_partly_refused_answer_is_partial_and_a_fully_refused_one_is_refused():
    p = pack_of(tv([ob()]))
    o = ids(p, "obligation")
    st = next(f["id"] for f in p["facts"] if f["kind"] == "state")
    two = ra.check_answer({"outcome": "answered", "answer_blocks": [{"text": "The current stage is in the facts.", "fact_refs": [st], "kind": "fact"},
                                                                    {"text": "The requester must decide it.", "fact_refs": o, "kind": "fact"}], "limitations": []}, p, LIMITS, legacy=True)
    assert two["status"] == "partial" and len(two["blocks"]) == 1 and two["rejected"][0]["reason"] == "holder_not_grounded"


def test_possessive_and_assigning_claims_need_a_step_that_is_the_readers():
    """Class of the next-step live N02a (2026-10-20): "Yes, the next step is yours." was shown to a judge who may do the step but is not asked. Possessive and assigning
    phrasings now need a cited obligation held by the reader or an action that is the reader's current step; negations and third-person holders are not claims about the reader."""
    may_not_asked = {"kind": "obligation", "text": "Ask for the AI draft of the change - required, held by the requester, state current (you may do it: yes).",
                     "data": {"holder": "requester", "holder_label": "the requester", "you": False, "may": True, "assigned": False, "required": True, "state": "current"}}
    waiting = {"kind": "waiting", "text": "Waiting on the requester.", "data": {"holder": "requester", "holder_label": "the requester"}}
    permitted = {"kind": "action", "text": "Action 'Ask for the AI draft of the change': allowed for you, but it is not a step you are asked to do now.", "data": {"action": "change.prepare", "allowed": True, "current": False}}
    asked = dict(may_not_asked, data=dict(may_not_asked["data"], you=True, assigned=True))
    current_action = dict(permitted, data=dict(permitted["data"], current=True))
    for text in ("Yes, the next step is yours.", "We are waiting on you.", "It's your turn to ask for the draft.", "You are the one who asks for the draft.", "You should ask for the AI draft."):
        assert ra.authority_problem(text, [may_not_asked, waiting, permitted], "interpretation") == "contradicts_cited_obligation", text
    assert ra.authority_problem("You should ask for the AI draft.", [asked], "suggestion") is None
    assert ra.authority_problem("You should ask for the AI draft.", [current_action], "suggestion") is None
    for text in ("No, the next step is not yours; it is waiting on the requester.", "The requester holds the next step.", "You may also do it, but you do not need to."):
        assert ra.authority_problem(text, [may_not_asked, waiting, permitted], "fact") is None, text
    assert ra.CHECKS_VERSION == "request-assistant-checks/13"
