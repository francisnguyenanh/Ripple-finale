"""AIX-K2 / KNO-07 interview integrity (owner decision DEC-0006): the model READS the words, the server checks COMPLETENESS, only the PERSON confirms.

Regressions for the failures exposed by the self-authored interview set (IV-06 a hedged "I am not sure which" ended the interview at once; IV-10 "the usual way" became
a confirmable READY reading): a hedge gets a bounded follow-up instead of ending; a vague reference is never confirmable; an explicit "do not know", a stop by the person,
the turn cap or a repeated unclear answer end UNRESOLVED with a reason; nothing is written to the knowledge until a complete reading is confirmed. STUB provider only:
these prove the pipeline and guards, nothing about real model quality.
"""
from __future__ import annotations

import json

import pytest

from core.change_intelligence.v2 import business_answer_interpret as bai
from core.change_intelligence.v2 import gap_interview as gi
from tests.final_loop.test_aix_k2_interview import answer, refresh, start
from tests.final_loop.test_kno01_business_store import counts, ws  # noqa: F401
from tests.final_loop.test_kno07_answer_interpret import ACTOR_GAP, actor_gap, mobj, resolve
from tests.final_loop.test_qry03_qa import qa_env, use  # noqa: F401

HEDGED = "I am not sure which, maybe the finance director."
VAGUE = "The usual people approve it the usual way."
PLAIN = "Only the finance director approves these."


# ---- completeness is a separate server step after the model reading (pure) ------------------------------------------------------------------------------

def test_a_hedged_or_vague_reading_is_kept_visible_but_never_ready_and_a_plain_one_is_complete():
    hedged = bai.completeness(resolve(mobj("actor", "finance director", quote=HEDGED), HEDGED), HEDGED)
    assert hedged["status"] == "NEEDS_CLARIFICATION" and hedged["answer"] == {"actor": "finance director"} and "hedged_answer" in hedged["reasons"]
    assert hedged["completeness"]["complete"] is False and hedged["completeness"]["problems"][0]["words"].lower().startswith("i am not sure")
    vague = bai.completeness(resolve(mobj("actor", "the usual people", quote=VAGUE), VAGUE), VAGUE)
    assert vague["status"] == "NEEDS_CLARIFICATION" and "vague_reference" in vague["reasons"] and "exactly" in vague["message"] and "do not know" in vague["message"]
    plain = bai.completeness(resolve(mobj("actor", "finance director", quote=PLAIN), PLAIN), PLAIN)
    assert plain["status"] == "READY" and plain["completeness"] == {"version": bai.COMPLETENESS_VERSION, "complete": True, "problems": []}
    refused = bai.completeness(resolve(mobj("actor", "chief financial officer", quote=PLAIN), PLAIN), PLAIN)           # a refused reading stays refused; nothing is added
    assert refused["status"] != "READY" and refused["completeness"]["complete"] is False and refused["completeness"]["problems"] == []


def test_only_an_explicit_do_not_know_stops_and_a_hedge_gets_a_targeted_follow_up():
    assert not gi.is_unknown(HEDGED) and not gi.is_unknown("not sure which one applies")
    assert gi.is_unknown("I don't know") and gi.is_unknown("We cannot say yet.") and gi.is_unknown("No idea.") and gi.is_unknown("unknown")
    h = bai.completeness(resolve(mobj("actor", "finance director", quote=HEDGED), HEDGED), HEDGED)
    t1 = {"turn": 0, "text": HEDGED, "reading": {k: h.get(k) for k in ("status", "uncovered", "reasons", "completeness")} | {"proposed_answer": h["answer"]}}
    nq = gi.next_question(ACTOR_GAP, [t1])
    assert nq["turn"] == 1 and nq["decision"] == "uncovered_part" and "not sure" in nq["question"].lower() and "do not know" in nq["question"] and nq["offers_unknown"]
    assert gi.session_status([t1], cap=5, pin_moved=False, confirmed=False) == "OPEN" and gi.unresolved_reason([t1], cap=5) is None
    v = bai.completeness(resolve(mobj("actor", "the usual people", quote=VAGUE), VAGUE), VAGUE)
    tv = {"turn": 0, "text": VAGUE, "reading": {k: v.get(k) for k in ("status", "uncovered", "reasons", "completeness")}}
    assert "concrete" in gi.next_question(ACTOR_GAP, [tv])["question"]


def test_every_unresolved_end_has_its_reason_and_an_incomplete_ready_reading_is_not_confirmable():
    unclear = {"status": "NEEDS_CLARIFICATION", "reasons": ["vague_reference"], "uncovered": ["the usual way"]}
    a, b = {"turn": 0, "text": "the usual way", "reading": unclear}, {"turn": 1, "text": "the usual way, as I said", "reading": dict(unclear)}
    assert gi.unresolved_reason([a], cap=5) is None and gi.unresolved_reason([a, b], cap=5) == "no_remaining_clarification"
    assert gi.unresolved_reason([a, {"turn": 1, "text": "I don't know", "unknown": True}], cap=5) == "unknown_declared"
    assert gi.unresolved_reason([a, {"turn": 1, "text": None, "stopped": True}], cap=5) == "stopped_by_person"
    assert gi.unresolved_reason([a, dict(b, reading={"status": "NEEDS_CLARIFICATION", "reasons": ["hedged_answer"], "uncovered": ["maybe"]})], cap=2) == "turn_cap_reached"
    assert set(gi.UNRESOLVED_REASONS) == {"unknown_declared", "stopped_by_person", "turn_cap_reached", "no_remaining_clarification"}
    forged = {"turn": 0, "text": PLAIN, "reading": {"status": "READY", "proposed_answer": {"actor": "x"}, "completeness": {"complete": False}}}
    assert gi.session_status([forged], cap=5, pin_moved=False, confirmed=False) == "OPEN"
    legacy = {"turn": 0, "text": PLAIN, "reading": {"status": "READY", "proposed_answer": {"actor": "x"}}}                 # a reading stored before the completeness step
    assert gi.session_status([legacy], cap=5, pin_moved=False, confirmed=False) == "READY_TO_CONFIRM"


# ---- real routes, worker and the answer path (STUB provider) --------------------------------------------------------------------------------------------

def _confirm(q, sid):
    return q.admin.post(f"/v2/business/interviews/{sid}/confirm", json={"workspace_id": q.env.ws})


def _stop(q, client, sid):
    return client.post(f"/v2/business/interviews/{sid}/stop", json={"workspace_id": q.env.ws})


def _reasons(q, sid):
    with q.env.store._tx() as cur:
        cur.execute("SELECT payload->>'reason' FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type='GAP_INTERVIEW_UNRESOLVED' AND payload->>'session_id'=%s",
                    (q.env.tenant, q.env.ws, sid))
        return [r[0] for r in cur.fetchall()]


def test_a_hedged_answer_gets_a_bounded_follow_up_writes_nothing_and_a_plain_answer_is_then_confirmed(qa_env):
    q = qa_env
    g = actor_gap(q)
    before = counts(q.env)
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    model = use(q, lambda p: mobj("actor", "finance director", quote=HEDGED))
    assert answer(q, q.admin, sid, HEDGED).status_code == 200
    st = refresh(q, q.admin, sid)
    r0 = st["turns"][0]["reading"]
    assert model.calls == 1 and st["status"] == "OPEN" and r0["status"] == "NEEDS_CLARIFICATION" and "hedged_answer" in r0["reasons"] and r0["completeness"]["complete"] is False
    assert r0["proposed_answer"] == {"actor": "finance director"}                                                           # the model's reading stays visible
    assert st["open_question"]["turn"] == 1 and "not sure" in st["open_question"]["question"].lower() and st["turns_left"] == 4
    c = _confirm(q, sid)
    assert c.status_code == 409 and c.json()["code"] == "not_ready" and counts(q.env) == before                         # zero writes while the facts are insufficient
    use(q, lambda p: mobj("actor", "finance director", quote=PLAIN))
    assert answer(q, q.admin, sid, PLAIN).status_code == 200
    st = refresh(q, q.admin, sid)
    assert st["status"] == "READY_TO_CONFIRM" and st["turns"][1]["reading"]["completeness"]["complete"] is True and counts(q.env) == before
    c = _confirm(q, sid)
    assert c.status_code == 200, c.text
    assert counts(q.env)["v2_business_decisions"] == before["v2_business_decisions"] + 1


def test_a_vague_usual_way_answer_is_never_confirmable_and_repeating_it_ends_unresolved_without_a_write(qa_env):
    q = qa_env
    g = actor_gap(q)
    before = counts(q.env)
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    use(q, lambda p: mobj("actor", "the usual people", quote=VAGUE))
    assert answer(q, q.admin, sid, VAGUE).status_code == 200
    st = refresh(q, q.admin, sid)
    assert st["status"] == "OPEN" and "vague_reference" in st["turns"][0]["reading"]["reasons"] and "concrete" in st["open_question"]["question"]
    assert _confirm(q, sid).status_code == 409
    use(q, lambda p: mobj("actor", "the usual people", quote=VAGUE + " As I said."))
    assert answer(q, q.admin, sid, VAGUE + " As I said.").status_code == 200
    st = refresh(q, q.admin, sid)
    assert st["status"] == "UNRESOLVED" and st["unresolved_reason"] == "no_remaining_clarification" and st["open_question"] is None
    assert _reasons(q, sid) == ["no_remaining_clarification"] and _confirm(q, sid).status_code == 409 and counts(q.env) == before


def test_the_person_can_stop_an_interview_and_an_explicit_do_not_know_ends_it_both_with_reasons_and_without_writes(qa_env):
    q = qa_env
    g = actor_gap(q)
    before = counts(q.env)
    model = use(q, lambda p: mobj("actor", "finance director", quote=HEDGED))
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    assert answer(q, q.admin, sid, HEDGED).status_code == 200
    refresh(q, q.admin, sid)
    assert _stop(q, q.req, sid).status_code == 403                                                                        # the same permission as answering
    s = _stop(q, q.admin, sid)
    assert s.status_code == 200, s.text
    st = s.json()
    assert st["status"] == "UNRESOLVED" and st["unresolved_reason"] == "stopped_by_person" and st["open_question"] is None and model.calls == 1
    assert _stop(q, q.admin, sid).status_code == 409 and answer(q, q.admin, sid, PLAIN).status_code == 409 and _confirm(q, sid).status_code == 409
    assert refresh(q, q.admin, sid)["status"] == "UNRESOLVED" and _reasons(q, sid) == ["stopped_by_person"] and counts(q.env) == before
    cond = next(x for x in __import__("tests.final_loop.test_kno07_answer_interpret", fromlist=["gaps"]).gaps(q, q.admin)["gaps"] if x["kind"] == "missing_condition")
    sid2 = start(q, q.admin, cond["gap_id"]).json()["session_id"]
    st = answer(q, q.admin, sid2, "I don't know.").json()
    assert st["status"] == "UNRESOLVED" and st["unresolved_reason"] == "unknown_declared" and model.calls == 1 and counts(q.env) == before


def test_follow_ups_are_bounded_by_the_turn_cap(qa_env):
    q = qa_env
    g = actor_gap(q)
    before = counts(q.env)
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    words = ["maybe the finance director", "perhaps the finance director", "probably the finance director", "I think the finance director", "I guess the finance director"]
    for i, w in enumerate(words):
        use(q, lambda p, w=w: mobj("actor", "finance director", quote=w))
        assert answer(q, q.admin, sid, w).status_code == 200, i
        st = refresh(q, q.admin, sid)
    assert st["status"] == "UNRESOLVED" and st["unresolved_reason"] == "turn_cap_reached" and st["turns_answered"] == gi.DEFAULT_CAP and st["open_question"] is None
    assert len(st["turns"]) == gi.DEFAULT_CAP and _reasons(q, sid) == ["turn_cap_reached"] and counts(q.env) == before


# ---- exposed-set regressions: the self-authored interview_v3 items that failed live (EV3-INTERVIEW-20261019-01); the recorded results stay as they were scored ------

IV06 = "Either the department head or HR, I am not sure which one."
IV10 = "That gets handled the usual way."


def test_exposed_hedged_two_actor_answer_is_followed_up():
    """Exposed item V3-IV-06."""
    assert not gi.is_unknown(IV06)                                                                                       # live: ended UNRESOLVED at once
    out = bai.completeness({"status": "READY", "answer": {"actor": "department head"}, "reasons": [], "uncovered": []}, IV06)
    assert out["status"] == "NEEDS_CLARIFICATION" and "hedged_answer" in out["reasons"]
    t = {"turn": 0, "text": IV06, "reading": {k: out.get(k) for k in ("status", "uncovered", "reasons", "completeness")}}
    assert _names_the_doubt(gi.next_question(ACTOR_GAP, [t])["question"]) and gi.session_status([t], cap=5, pin_moved=False, confirmed=False) == "OPEN"


def test_exposed_usual_way_reading_is_not_confirmable():
    """Exposed item V3-IV-10."""
    recorded = {"status": "READY", "answer": {"outcome": "That gets handled the usual way."}, "reasons": [], "uncovered": [], "quote": IV10}   # the live reading, verbatim
    out = bai.completeness(recorded, IV10)
    assert out["status"] == "NEEDS_CLARIFICATION" and out["reasons"] == ["vague_reference"] and out["answer"] == recorded["answer"]
    t = {"turn": 0, "text": IV10, "reading": {"status": out["status"], "proposed_answer": out["answer"], "reasons": out["reasons"], "uncovered": out["uncovered"],
                                              "completeness": out["completeness"]}}
    assert gi.session_status([t], cap=5, pin_moved=False, confirmed=False) == "OPEN"                                      # live: READY_TO_CONFIRM


# ---- completeness/2: uncertainty as classes from two sides, rechecked at status and confirm; clarification is not a contradiction (findings V4-IF-02 / V4-IF-03) --
# The hedge forms below are NOT the words of the v4 items (except the two verbatim replays, labelled); they are written to cover the classes, so a pass here is guard
# evidence for the pipeline, not model quality.

UNSEEN_HEDGES = [
    "Presumably the finance director.", "The finance director, I reckon.", "I suppose the finance director signs.", "If I remember correctly, the finance director.",
    "The finance director, as far as I know.", "It might be the finance director.", "It could well be the finance director.", "The finance director is likely the one.",
    "I'm fairly sure it is the finance director.", "Not entirely certain, but the finance director.", "The finance director or someone like that.",
    "Last I heard, the finance director.", "I was told the finance director handles it.", "The finance director?", "Apparently the finance director.",
    "We believe the finance director approves.", "I forget which, the finance director I think.",
]
PLAIN_CONTROLS = [
    "The finance director approves these.", "No doubt about it: the finance director approves these.", "It is not a guess, the finance director approves these.",
    "The form says \"maybe later\" but the finance director approves these.", "Earlier I said maybe, but the finance director approves these.",
    "In May the finance director approves these.", "Staff may not approve; the finance director approves these.",
]


def _names_the_doubt(q: str) -> bool:
    """A follow-up to a hedge says what is uncertain (the hedge words, or the uncertain reading) and keeps 'I do not know' open; the exact wording is not pinned."""
    return ("not certain" in q or "Which exactly" in q) and q.startswith("You said") and "do not know" in q


def _ready_actor(text, **extra):
    return bai.completeness(dict(resolve(dict(mobj("actor", "finance director", quote=text), **extra), text)), text)


@pytest.mark.parametrize("text", UNSEEN_HEDGES)
def test_uncertainty_of_any_class_is_never_ready_and_the_session_asks_about_it(text):
    out = _ready_actor(text)
    assert out["status"] == "NEEDS_CLARIFICATION" and "hedged_answer" in out["reasons"] and out["completeness"]["complete"] is False, out
    t = {"turn": 0, "text": text, "reading": {"status": out["status"], "proposed_answer": out["answer"], "reasons": out["reasons"], "uncovered": out["uncovered"],
                                              "completeness": out["completeness"], "quote": out["quote"]}}
    a = gi.assess(t)
    assert a["role"] == "unresolved_interpretation" and a["confirmable"] is False and a["problems"]
    assert gi.session_status([t], cap=5, pin_moved=False, confirmed=False) == "OPEN" and _names_the_doubt(gi.next_question(ACTOR_GAP, [t])["question"])


@pytest.mark.parametrize("text", PLAIN_CONTROLS)
def test_quoted_denied_or_retracted_doubt_and_other_senses_of_may_are_not_uncertainty(text):
    out = _ready_actor(text)
    assert out["status"] == "READY" and out["completeness"]["problems"] == [], out


def test_the_models_own_uncertainty_report_blocks_ready_even_without_a_known_word_and_quoted_or_empty_reports_do_not():
    text = "The finance director approves these, I would have to check."
    assert bai.uncertainty_problems(text, {"actor": "finance director"}, quote=text) == []                        # outside every server class
    out = _ready_actor(text, uncertainty=["I would have to check"])
    assert out["status"] == "NEEDS_CLARIFICATION" and out["completeness"]["problems"] == [{"code": "hedged_answer", "signal": "model_reported", "words": "I would have to check"}]
    assert "I would have to check" in out["uncovered"]
    paraphrased = _ready_actor(text, uncertainty=["the person seems unsure"])                                      # a report that quotes nothing still blocks (fail closed)
    assert paraphrased["status"] == "NEEDS_CLARIFICATION" and paraphrased["completeness"]["problems"][0]["words"] is None
    quoted = "The form says \"maybe later\" but the finance director approves these."
    assert _ready_actor(quoted, uncertainty=["maybe later"])["status"] == "READY"                                    # cited words are not the person's doubt
    assert _ready_actor(PLAIN, uncertainty=["none"])["status"] == "READY" and _ready_actor(PLAIN, uncertainty=[])["status"] == "READY"
    with pytest.raises(ValueError):
        bai.validate_model_object(dict(mobj("actor", "x", quote="x"), uncertainty="maybe"), {"max_uncovered": 6})
    assert bai.validate_model_object(mobj("actor", "x", quote="x"), {"max_uncovered": 6})                         # a recorded answer from before the key still validates


def test_replay_v4_if_03_recorded_reading_is_rechecked_and_cannot_be_confirmed():
    """Exposed item V4-IF-03 (EV4-FLOW-20261019-01): the reading as stored live, verbatim, under completeness/1 (complete: true)."""
    text = "Possibly the duty producer."
    recorded = {"quote": text, "status": "READY", "message": None, "reasons": [], "uncovered": [], "proposed_answer": {"actor": "the duty producer"},
                "completeness": {"version": "kno07-completeness/1", "complete": True, "problems": []}}
    t = {"turn": 0, "text": text, "reading": recorded, "reading_status": "READY"}
    a = gi.assess(t)
    assert a["confirmable"] is False and a["problems"][0]["words"] == "Possibly" and a["role"] == "unresolved_interpretation"
    assert gi.session_status([t], cap=5, pin_moved=False, confirmed=False) == "OPEN"                                 # live: READY_TO_CONFIRM
    assert "Possibly" in gi.next_question({"kind": "missing_actor", "affected": [{"title": "Noise complaints are logged"}]}, [t])["question"]


def test_replay_v4_if_02_vague_reading_then_clarification_is_not_a_contradiction_and_a_real_change_still_is():
    """Exposed item V4-IF-02 (EV4-FLOW-20261019-01): both readings verbatim as stored live."""
    vague = {"text": "Same thing we always do with those.", "turn": 0, "reading": {"quote": "Same thing we always do with those.", "status": "NEEDS_CLARIFICATION",
             "reasons": [], "uncovered": ["Same thing we always do with those."], "proposed_answer": {"outcome": "Same thing we always do with those."},
             "completeness": {"version": "kno07-completeness/1", "complete": False, "problems": []}}}
    clear = {"text": "A late cancellation loses its deposit.", "turn": 1, "reading": {"quote": "loses its deposit", "status": "READY", "reasons": [], "uncovered": [],
             "proposed_answer": {"outcome": "loses its deposit"}, "completeness": {"version": "kno07-completeness/1", "complete": True, "problems": []}}}
    assert gi.contradictions([vague, clear]) == []                                                                   # live: a contradiction blocked confirm
    assert gi.session_status([vague, clear], cap=5, pin_moved=False, confirmed=False) == "READY_TO_CONFIRM"
    assert gi.assess(vague)["role"] == "unresolved_interpretation" and gi.assess(vague)["problems"][0]["code"] == "vague_reference"
    assert gi.assess(clear)["role"] == "proposed_answer"
    other = {"text": "A late cancellation pays a fee.", "turn": 2, "reading": {"quote": "pays a fee", "status": "READY", "reasons": [], "uncovered": [],
             "proposed_answer": {"outcome": "pays a fee"}, "completeness": {"version": bai.COMPLETENESS_VERSION, "complete": True, "problems": []}}}
    c = gi.contradictions([vague, clear, other])
    assert c == [{"field": "outcome", "turns": [1, 2], "values": ["loses its deposit", "pays a fee"]}]                 # two plain answers that disagree: shown, never last-wins
    assert gi.session_status([vague, clear, other], cap=5, pin_moved=False, confirmed=False) == "OPEN"
    assert gi.next_question(ACTOR_GAP, [vague, clear, other])["decision"] == "contradiction"


def test_a_model_uncertainty_report_through_the_worker_gets_a_follow_up_and_confirm_rechecks_without_a_write(qa_env):
    q = qa_env
    g = actor_gap(q)
    before = counts(q.env)
    sid = start(q, q.admin, g["gap_id"]).json()["session_id"]
    text = "The finance director approves these, I would have to check."
    use(q, lambda p: dict(mobj("actor", "finance director", quote=text), uncertainty=["I would have to check"]))
    assert answer(q, q.admin, sid, text).status_code == 200
    st = refresh(q, q.admin, sid)
    t0 = st["turns"][0]
    assert st["status"] == "OPEN" and t0["reading"]["status"] == "NEEDS_CLARIFICATION" and t0["assessment"]["role"] == "unresolved_interpretation"
    assert "I would have to check" in st["open_question"]["question"] and _confirm(q, sid).status_code == 409 and counts(q.env) == before
    legacy = "Possibly the finance director."                                                                      # a recorded-shape answer without the new key: the server alone catches it
    use(q, lambda p: mobj("actor", "finance director", quote=legacy))
    assert answer(q, q.admin, sid, legacy).status_code == 200
    st = refresh(q, q.admin, sid)
    assert st["status"] == "OPEN" and st["turns"][1]["reading"]["completeness"]["problems"][0]["signal"] == "modal_adverb"
    with q.env.store._tx() as cur:                                                                               # a stored reading claiming completeness over hedged words
        cur.execute("SELECT turns FROM v2_gap_interviews WHERE tenant_id=%s AND workspace_id=%s AND session_id=%s", (q.env.tenant, q.env.ws, sid))
        turns = cur.fetchone()[0][:2]
        turns[1]["reading"] = dict(turns[1]["reading"], status="READY", reasons=[], uncovered=[], completeness={"version": "kno07-completeness/1", "complete": True, "problems": []})
        cur.execute("UPDATE v2_gap_interviews SET turns=%s::jsonb, status='READY_TO_CONFIRM' WHERE tenant_id=%s AND workspace_id=%s AND session_id=%s",
                    (json.dumps(turns), q.env.tenant, q.env.ws, sid))
    c = _confirm(q, sid)
    assert c.status_code == 409 and c.json()["code"] == "not_ready" and counts(q.env) == before                      # rechecked: never written
    st = refresh(q, q.admin, sid)
    assert st["status"] == "OPEN" and "Possibly" in st["open_question"]["question"]
    use(q, lambda p: dict(mobj("actor", "finance director", quote=PLAIN), uncertainty=[]))
    assert answer(q, q.admin, sid, PLAIN).status_code == 200
    st = refresh(q, q.admin, sid)
    assert st["status"] == "READY_TO_CONFIRM" and st["contradictions"] == []
    assert [t["assessment"]["role"] for t in st["turns"]] == ["unresolved_interpretation", "unresolved_interpretation", "proposed_answer"]
    assert _confirm(q, sid).status_code == 200 and counts(q.env)["v2_business_decisions"] == before["v2_business_decisions"] + 1
    st = q.admin.get(f"/v2/business/interviews/{sid}", params={"workspace_id": q.env.ws}).json()
    assert st["turns"][-1]["assessment"]["role"] == "confirmed_fact" and st["turns"][0]["assessment"]["superseded"] is True


def test_a_repeated_model_question_falls_back_and_the_hedge_follow_up_names_the_uncertain_reading():
    """Review finding (gallery V6, 2026-10-20): after a hedged answer the next question must say what is uncertain, not repeat the earlier question word for word."""
    gap = {"kind": "missing_actor", "affected": [{"title": "Emergency purchases"}]}
    hedged = {"turn": 0, "question": "Who approves emergency purchases? Or say that you do not know.", "text": "I am not sure which, maybe the finance director.",
              "reading": {"status": "NEEDS_CLARIFICATION", "proposed_answer": {"actor": "finance director"}, "quote": "maybe the finance director",
                          "uncovered": ["I am not sure which", "maybe"], "reasons": ["hedged_answer"],
                          "completeness": {"complete": False, "problems": [{"code": "hedged_answer", "words": "I am not sure which"}]}}}
    det = gi.next_question(gap, [hedged])
    assert det and "finance director" in det["question"] and "not certain" in det["question"] and det["offers_unknown"] is True
    assert det["question"] != hedged["question"]
    repeat = gi.ground_question({"status": "OK", "question": "Who approves emergency purchases?", "focus": "kind"}, gap, [{"title": "Emergency purchases"}],
                                [{"question": hedged["question"], "uncovered": hedged["reading"]["uncovered"], "contradictions": []}], det)
    assert repeat["questioner"] == "deterministic_fallback" and repeat["fallback_reason"] == "repeats_earlier_question" and "finance director" in repeat["question"]
    fresh = gi.ground_question({"status": "OK", "question": "Is the finance director the one who approves emergency purchases, or someone else?", "focus": "uncovered_part"},
                               gap, [{"title": "Emergency purchases"}], [{"question": hedged["question"], "uncovered": hedged["reading"]["uncovered"], "contradictions": []}], det)
    assert fresh["questioner"] == "model"
