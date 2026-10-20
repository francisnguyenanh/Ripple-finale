"""Interview sessions over one open question (AIX-K2) on top of KNO-07 and the existing answer path.

start -> one session per (workspace, gap, knowledge version) with the first deterministic question; answer(text) -> the KNO-07 reading job of that text (same job id as the
single-shot route: version, gap, text), recorded as a turn; refresh -> readings pulled from the jobs, contradictions and the next question computed, status derived; confirm ->
the latest READY reading's structured answer with the person's own words as the quote through `preview_answer` + `confirm_answer` (the interview never writes knowledge itself).
Three separate steps (owner decision DEC-0006): the model READS the words (KNO-07 job), the server checks COMPLETENESS (hedged or vague words keep the reading at
NEEDS_CLARIFICATION and get a bounded follow-up; rechecked when the status is derived and at confirm), and only the PERSON confirms. A reading over unclear words is an
unresolved interpretation, not a fact: it stays in the history and never contradicts the clarification that replaces it. stop -> the person ends the interview (UNRESOLVED, nothing written).
UNRESOLVED always carries a reason: unknown_declared, stopped_by_person, turn_cap_reached (`DEFAULT_CAP` answered turns) or no_remaining_clarification (the controls continue). A moved knowledge version makes the session STALE: nothing of it is saved to the graph.
"""
from __future__ import annotations

import re

import datetime

from psycopg2.extras import Json

from core.change_intelligence.v2 import gap_interview as gi
from infra.adapters import v2_business
from infra.adapters.v2_postgres import TenantScopeError, _as_object

AUDIT = {"start": "GAP_INTERVIEW_STARTED", "turn": "GAP_INTERVIEW_TURN", "confirm": "GAP_INTERVIEW_CONFIRMED", "unresolved": "GAP_INTERVIEW_UNRESOLVED", "question": "GAP_INTERVIEW_QUESTION"}
READING_KEYS = ("status", "proposed_answer", "quote", "uncovered", "message", "preview", "reasons", "completeness", "context")


def questioner_mode() -> str:
    """`deterministic` (default) or `model`: the follow-up questions come from the pinned role gap.interview, grounded by the server, with the deterministic question as fallback."""
    import os
    mode = os.environ.get("RIPPLE_INTERVIEW_QUESTIONER", "deterministic")
    return mode if mode in ("deterministic", "model") else "deterministic"


class InterviewRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _audit(cur, tenant_id, workspace_id, event, actor_id, payload):
    cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)", (tenant_id, workspace_id, event, actor_id, Json(payload)))


def _row(store, tenant_id, workspace_id, session_id) -> dict | None:
    with store._tx() as cur:
        cur.execute("SELECT gap_id, snapshot_id, status, turns, cap, requested_by, decision_id, created_at, updated_at FROM v2_gap_interviews WHERE tenant_id=%s AND workspace_id=%s AND session_id=%s",
                    (tenant_id, workspace_id, session_id))
        r = cur.fetchone()
    if not r:
        return None
    return {"session_id": session_id, "gap_id": r[0], "snapshot_id": r[1], "status": r[2], "turns": _as_object(r[3]) or [], "cap": r[4], "requested_by": r[5], "decision_id": r[6],
            "created_at": r[7].isoformat(), "updated_at": r[8].isoformat()}


def _open_gap(store, tenant_id, workspace_id, gap_id) -> dict | None:
    return next((g for g in v2_business.gap_queue(store, tenant_id, workspace_id)["gaps"] if g["gap_id"] == gap_id), None)


def _save(store, tenant_id, workspace_id, session_id, *, status, turns, decision_id=None) -> None:
    with store._tx() as cur:
        cur.execute("UPDATE v2_gap_interviews SET status=%s, turns=%s, decision_id=COALESCE(%s, decision_id), updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND session_id=%s",
                    (status, Json(turns), decision_id, tenant_id, workspace_id, session_id))


def start(store, tenant_id, workspace_id, gap_id, *, actor_id: str) -> dict:
    _, snap = v2_business._latest_view(store, tenant_id, workspace_id)
    if snap is None:
        raise InterviewRefused("no_snapshot", "there is no business knowledge to answer against")
    gap = _open_gap(store, tenant_id, workspace_id, gap_id)
    if gap is None:
        raise TenantScopeError("open question not found")
    session_id = gi.session_id_for(workspace_id, gap_id, snap["snapshot"]["snapshot_id"])
    existing = _row(store, tenant_id, workspace_id, session_id)
    if existing is not None:
        return {"session_id": session_id, "created": False, **state(store, tenant_id, workspace_id, session_id)}
    turns = [dict(gi.first_question(gap), text=None, job_id=None, reading=None, reading_status=None, unknown=False, asked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())]
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_gap_interviews (tenant_id, workspace_id, session_id, gap_id, snapshot_id, status, turns, cap, requested_by) VALUES (%s,%s,%s,%s,%s,'OPEN',%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (tenant_id, workspace_id, session_id, gap_id, snap["snapshot"]["snapshot_id"], Json(turns), gi.DEFAULT_CAP, actor_id))
        _audit(cur, tenant_id, workspace_id, AUDIT["start"], actor_id, {"session_id": session_id, "gap_id": gap_id, "snapshot_id": snap["snapshot"]["snapshot_id"], "cap": gi.DEFAULT_CAP})
    if questioner_mode() == "model":
        # LLM-K2 (roadmap 15.4): the FIRST question is chosen by the model too (same role, same grounding and fallback as the follow-ups); the deterministic question
        # is kept as the labelled fallback. Without a provider or budget the deterministic question is asked and says so.
        s = {"session_id": session_id, "gap_id": gap_id, "snapshot_id": snap["snapshot"]["snapshot_id"], "requested_by": actor_id}
        first = turns[0]
        qjid = _question_job(store, tenant_id, workspace_id, s, {k: first[k] for k in first if k in ("turn", "question", "decision", "offers_unknown")}, [])
        det = {k: first[k] for k in ("turn", "question", "decision", "offers_unknown") if k in first}
        turns[0] = dict(first, question=None if qjid else first["question"], deterministic=det, question_job_id=qjid,
                        questioner="model_pending" if qjid else "deterministic_fallback", fallback_reason=None if qjid else "provider_unavailable")
        _save(store, tenant_id, workspace_id, session_id, status="OPEN", turns=turns)
    return {"session_id": session_id, "created": True, **state(store, tenant_id, workspace_id, session_id)}


def answer(store, tenant_id, workspace_id, session_id, *, text: str, authority: dict) -> dict:
    """Record the person's words for the current question and start the KNO-07 reading (one model call, the same job as the single-shot route would create)."""
    s = _row(store, tenant_id, workspace_id, session_id)
    if s is None:
        raise TenantScopeError("interview not found")
    refreshed = refresh(store, tenant_id, workspace_id, session_id)
    s = _row(store, tenant_id, workspace_id, session_id)                               # the refresh may have settled a pending model question into the turns
    if refreshed["status"] not in ("OPEN",):
        raise InterviewRefused("session_not_open", f"the interview is {refreshed['status'].lower().replace('_', ' ')}; use the structured controls or start a new interview on the current knowledge")
    if not isinstance(text, str) or not text.strip() or len(text) > 1200:
        raise InterviewRefused("text_invalid", "text (1-1200 characters) required")
    if PLACEHOLDER.search(text):                                                        # EUI-05: an unfilled sentence-starter slot is not an answer (no turn, no model call)
        raise InterviewRefused("unfilled_placeholder", "replace each [blank] of the sentence starter with your own words, or say that you do not know")
    turns = s["turns"]
    current = turns[-1]
    if current.get("question") is None:
        raise InterviewRefused("question_pending", "the next question is being prepared; refresh in a moment")
    if current.get("text") is not None:
        raise InterviewRefused("no_open_question", "every asked question is answered; refresh to get the next one")
    text = text.strip()
    current["text"], current["answered_at"], current["answered_by"] = text, datetime.datetime.now(datetime.timezone.utc).isoformat(), authority["account_id"]
    if gi.is_unknown(text):
        current["unknown"] = True
        _save(store, tenant_id, workspace_id, session_id, status="UNRESOLVED", turns=turns)
        with store._tx() as cur:
            _audit(cur, tenant_id, workspace_id, AUDIT["unresolved"], authority["account_id"], {"session_id": session_id, "reason": "unknown_declared", "turn": current["turn"]})
        return state(store, tenant_id, workspace_id, session_id)
    from infra import ai_admission, provider_gate
    from infra.v2_business_routes import _quota_for
    from infra.worker.gap_answer_interpret import KIND, job_id_for
    gap = _open_gap(store, tenant_id, workspace_id, s["gap_id"])
    if gap is None:
        _save(store, tenant_id, workspace_id, session_id, status="CLOSED", turns=turns)
        raise InterviewRefused("gap_closed", "the question is no longer open")
    provider_gate.require_provider(KIND)
    jid = job_id_for(s["snapshot_id"], s["gap_id"], text)
    try:
        rec = store.job_record(tenant_id, workspace_id, jid)
    except TenantScopeError:
        rec = None
    if rec is None:
        limit, reservation = _quota_for(KIND)
        head = store.head(tenant_id, workspace_id)
        store.create_job(tenant_id, workspace_id, jid, KIND, {"gap_id": s["gap_id"], "text": text, "snapshot_id": s["snapshot_id"]}, expected_version=head.version,
                         idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
        ai_admission.admit(store, tenant_id, workspace_id, jid, KIND, authority)
    current["job_id"] = jid
    _save(store, tenant_id, workspace_id, session_id, status="OPEN", turns=turns)
    with store._tx() as cur:
        _audit(cur, tenant_id, workspace_id, AUDIT["turn"], authority["account_id"], {"session_id": session_id, "turn": current["turn"], "job_id": jid, "chars": len(text)})
    return state(store, tenant_id, workspace_id, session_id)


def refresh(store, tenant_id, workspace_id, session_id) -> dict:
    """Pull the readings of answered turns from their jobs, append the next question when due, derive the status."""
    s = _row(store, tenant_id, workspace_id, session_id)
    if s is None:
        raise TenantScopeError("interview not found")
    if s["status"] in ("CONFIRMED", "CLOSED"):
        return state(store, tenant_id, workspace_id, session_id)
    turns = s["turns"]
    _, snap = v2_business._latest_view(store, tenant_id, workspace_id)
    pin_moved = snap is None or snap["snapshot"]["snapshot_id"] != s["snapshot_id"]
    changed = False
    for t in turns:
        if t.get("job_id") and t.get("reading") is None and not t.get("unknown"):
            try:
                rec = store.job_record(tenant_id, workspace_id, t["job_id"])
            except TenantScopeError:
                rec = None
            if rec and rec.get("execution_status") == "COMPLETED" and rec.get("execution_result"):
                res = rec["execution_result"]
                t["reading"] = {k: res.get(k) for k in READING_KEYS}
                t["reading_status"] = res.get("status")
                changed = True
            elif rec and rec.get("execution_status") in ("FAILED", "CANCELLED", "REJECTED"):
                t["reading"] = {"status": "FAILED", "proposed_answer": None, "quote": None, "uncovered": [], "message": rec.get("execution_reason"), "preview": None}
                t["reading_status"] = "FAILED"
                changed = True
    for t in turns:                                                                     # AIX-K2 model questioner: a pending question job settles into the turn (model text or the deterministic fallback)
        if t.get("question") is None and t.get("question_job_id"):
            try:
                rec = store.job_record(tenant_id, workspace_id, t["question_job_id"])
            except TenantScopeError:
                rec = None
            if rec and rec.get("execution_status") == "COMPLETED" and rec.get("execution_result"):
                res = rec["execution_result"]
                t.update(question=res["question"], decision=res["decision"], offers_unknown=True, questioner=res["questioner"], fallback_reason=res.get("fallback_reason"))
                changed = True
            elif rec and rec.get("execution_status") in ("FAILED", "CANCELLED", "REJECTED"):
                det = t.get("deterministic") or {}
                t.update(question=det.get("question"), decision=det.get("decision"), offers_unknown=True, questioner="deterministic_fallback", fallback_reason=f"job_{rec.get('execution_status', '').lower()}:{rec.get('execution_reason')}")
                changed = True
            if changed and t.get("question") is not None:
                with store._tx() as cur:
                    _audit(cur, tenant_id, workspace_id, AUDIT["question"], "server", {"session_id": session_id, "turn": t["turn"], "questioner": t["questioner"], "fallback_reason": t.get("fallback_reason")})
    answered = [t for t in turns if t.get("text") is not None]
    pending = any(t.get("job_id") and t.get("reading") is None and not t.get("unknown") for t in turns) or any(t.get("question") is None and t.get("question_job_id") for t in turns)
    gap = _open_gap(store, tenant_id, workspace_id, s["gap_id"]) if not pin_moved else None
    if not pin_moved and gap is None and s["status"] != "CONFIRMED":
        status = "CLOSED"                                                           # answered through the controls or dismissed: the interview has nothing left to do
    else:
        status = gi.session_status(turns, cap=s["cap"], pin_moved=pin_moved, confirmed=False)
    if status == "OPEN" and not pending and turns and turns[-1].get("text") is not None and gap is not None:
        nq = gi.next_question(gap, answered)
        if nq is not None:
            if questioner_mode() == "model":
                qjid = _question_job(store, tenant_id, workspace_id, s, nq, answered)
                turns.append(dict(nq, question=None if qjid else nq["question"], deterministic=dict(nq), question_job_id=qjid, questioner="model_pending" if qjid else "deterministic_fallback",
                                  fallback_reason=None if qjid else "provider_unavailable",
                                  text=None, job_id=None, reading=None, reading_status=None, unknown=False, asked_at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
            else:
                turns.append(dict(nq, questioner="deterministic", text=None, job_id=None, reading=None, reading_status=None, unknown=False, asked_at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
            changed = True
    if changed or status != s["status"]:
        _save(store, tenant_id, workspace_id, session_id, status=status, turns=turns)
        if status == "UNRESOLVED" and s["status"] != "UNRESOLVED":
            with store._tx() as cur:
                _audit(cur, tenant_id, workspace_id, AUDIT["unresolved"], "server", {"session_id": session_id, "reason": gi.unresolved_reason(turns, cap=s["cap"])})
    return state(store, tenant_id, workspace_id, session_id)


def _question_job(store, tenant_id, workspace_id, s: dict, nq: dict, answered: list):
    """Create the next-question job (one per session and turn; 1 provider call). Returns None when the provider is disarmed or the budget refuses: the deterministic question is asked."""
    from infra import ai_admission, provider_gate
    from infra.v2_business_routes import _quota_for
    from infra.worker.gap_interview_question import KIND, job_id_for
    jid = job_id_for(s["session_id"], nq["turn"])
    try:
        store.job_record(tenant_id, workspace_id, jid)
        return jid
    except TenantScopeError:
        pass
    transcript = [{"turn": t["turn"], "question": t.get("question"), "text": t.get("text"), "reading_status": t.get("reading_status"), "uncovered": (t.get("reading") or {}).get("uncovered") or [],
                   "contradictions": gi.contradictions(answered)} for t in answered]
    try:
        provider_gate.require_provider(KIND)
        limit, reservation = _quota_for(KIND)
        head = store.head(tenant_id, workspace_id)
        store.create_job(tenant_id, workspace_id, jid, KIND, {"session_id": s["session_id"], "turn": nq["turn"], "gap_id": s["gap_id"], "snapshot_id": s["snapshot_id"], "deterministic": dict(nq),
                                                               "transcript": transcript, "requested_by": s["requested_by"]},
                         expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=s["requested_by"])
        roles = v2_business.current_roles(store, tenant_id, workspace_id, s["requested_by"])
        ai_admission.admit(store, tenant_id, workspace_id, jid, KIND, {"account_id": s["requested_by"], "roles": roles, "tenant_id": tenant_id, "workspace_id": workspace_id, "actor_kind": "human"})
        return jid
    except Exception:                                                                 # noqa: BLE001 - disarmed provider, budget, concurrency: the deterministic question is asked instead
        return None


PLACEHOLDER = re.compile(r"\[[^\[\]]{1,40}\]")
VOCAB_LABEL = "Used in other accepted rules of this workspace - not established for this rule"
VOCAB_CAP = 12
STARTERS = {                                                                            # blank slots only: no business value is asserted by a starter
    "missing_actor": ["[role] decides this.", "[role] reviews it when [condition].", "It is decided automatically when [condition]; nobody approves it."],
    "missing_condition": ["It applies when [measure] is more than [amount and unit].", "It applies only to [category]."],
    "missing_outcome": ["When it applies, [role] must [approve / review / be informed].", "When it applies, the request is [blocked / sent to a person]."],
    "missing_exception": ["It does not apply when [case].", "[role] may skip it when [case]."],
    "conflicting_facts": ["The business rule is [one of the values], because [reason]."],
}


def vocabulary(store, tenant_id, workspace_id, session_id) -> dict:
    """EUI-05 wording help for the open question, read-only, no model call: words used by OTHER rules of the workspace's ACCEPTED business knowledge (actor roles,
    outcome kinds, measured fields; never amounts, quotes, ids or people), each labelled as not established for this rule, plus blank sentence starters. Not the
    member directory; nothing here is evidence or an answer."""
    s = _row(store, tenant_id, workspace_id, session_id)
    if s is None:
        raise TenantScopeError("interview not found")
    gap = _open_gap(store, tenant_id, workspace_id, s["gap_id"])
    kind = (gap or {}).get("kind")
    accepted = (v2_business.business_view(store, tenant_id, workspace_id).get("accepted") or {}).get("rules") or []
    own = set((gap or {}).get("affected_rule_ids") or [])
    others = [r for r in accepted if r["rule_id"] not in own]
    from collections import Counter
    from core.change_intelligence.v2.business_index import _leaf_fields

    def top(counter):
        return [{"text": t, "used_in": n} for t, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:VOCAB_CAP]]
    actors = Counter(str(r["actor_ref"]).replace("role-", "").replace("-", " ") for r in others if r.get("actor_ref"))
    outcomes = Counter(str((r.get("outcome") or {}).get("kind")).replace("_", " ") for r in others if (r.get("outcome") or {}).get("kind"))
    measures = Counter(f for r in others for f in _leaf_fields(r.get("condition")))
    groups = {"missing_actor": [("actor", actors)], "missing_outcome": [("outcome", outcomes), ("actor", actors)], "missing_condition": [("measure", measures)],
              "missing_exception": [("actor", actors)], "conflicting_facts": []}.get(kind, [("actor", actors)])
    return {"session_id": session_id, "gap_kind": kind, "label": VOCAB_LABEL, "accepted_knowledge": bool(accepted),
            "vocabulary": [{"group": g, **item} for g, c in groups for item in top(c)],
            "starters": STARTERS.get(kind, ["[who] does [what] when [condition]."]), "placeholder_rule": "a [blank] left unfilled cannot be sent"}


def stop(store, tenant_id, workspace_id, session_id, *, authority: dict) -> dict:
    """The person stops (skips / cancels) the interview: UNRESOLVED with reason stopped_by_person; nothing is written to the knowledge and no model call is made."""
    s = _row(store, tenant_id, workspace_id, session_id)
    if s is None:
        raise TenantScopeError("interview not found")
    if s["status"] in ("CONFIRMED", "CLOSED", "STALE", "UNRESOLVED"):
        raise InterviewRefused("session_not_open", f"the interview is already {s['status'].lower().replace('_', ' ')}")
    turns = s["turns"]
    turns[-1]["stopped"], turns[-1]["stopped_at"], turns[-1]["stopped_by"] = True, datetime.datetime.now(datetime.timezone.utc).isoformat(), authority["account_id"]
    _save(store, tenant_id, workspace_id, session_id, status="UNRESOLVED", turns=turns)
    with store._tx() as cur:
        _audit(cur, tenant_id, workspace_id, AUDIT["unresolved"], authority["account_id"], {"session_id": session_id, "reason": "stopped_by_person", "turn": turns[-1].get("turn")})
    return state(store, tenant_id, workspace_id, session_id)


def confirm(store, tenant_id, workspace_id, session_id, *, authority: dict) -> dict:
    """The person confirms the latest READY reading with their own words of that turn through the existing answer path. Refused when the session is not ready, a contradiction is open
    or the preview moved."""
    st = refresh(store, tenant_id, workspace_id, session_id)
    if st["status"] != "READY_TO_CONFIRM":
        raise InterviewRefused("not_ready", f"the interview is {st['status'].lower().replace('_', ' ')}; nothing is confirmed")
    s = _row(store, tenant_id, workspace_id, session_id)
    last = [t for t in s["turns"] if t.get("text") is not None][-1]
    reading = last["reading"]
    if not gi.assess(last)["confirmable"]:                                             # completeness rechecked at confirm with the current rules: the person confirms only a complete reading
        raise InterviewRefused("incomplete", "the reading is not complete (uncertain or vague words, or a part not covered); nothing is confirmed")
    preview = v2_business.preview_answer(store, tenant_id, workspace_id, gap_id=s["gap_id"], answer=reading["proposed_answer"])
    out = v2_business.confirm_answer(store, tenant_id, workspace_id, actor_id=authority["account_id"], gap_id=s["gap_id"], answer=reading["proposed_answer"], quote=last["text"],
                                     preview_hash=preview["preview_hash"])
    _save(store, tenant_id, workspace_id, session_id, status="CONFIRMED", turns=s["turns"], decision_id=out.get("decision_id"))
    with store._tx() as cur:
        _audit(cur, tenant_id, workspace_id, AUDIT["confirm"], authority["account_id"], {"session_id": session_id, "gap_id": s["gap_id"], "decision_id": out.get("decision_id"), "turns": len(s["turns"])})
    return {**state(store, tenant_id, workspace_id, session_id), "decision": out}


def state(store, tenant_id, workspace_id, session_id) -> dict:
    s = _row(store, tenant_id, workspace_id, session_id)
    if s is None:
        raise TenantScopeError("interview not found")
    answered = [t for t in s["turns"] if t.get("text") is not None]
    last = s["turns"][-1] if s["turns"] else None
    open_q = last if s["status"] == "OPEN" and last and last.get("text") is None and last.get("question") is not None else None    # a stopped / stale session asks nothing
    question_pending = bool(last and last.get("text") is None and last.get("question") is None and last.get("question_job_id"))
    return {"schema": gi.SCHEMA, "session_id": session_id, "gap_id": s["gap_id"], "snapshot_id": s["snapshot_id"], "status": s["status"], "cap": s["cap"], "turns_answered": len(answered),
            "unresolved_reason": gi.unresolved_reason(s["turns"], cap=s["cap"]) if s["status"] == "UNRESOLVED" else None,
            "turns_left": max(0, s["cap"] - len(answered)), "questioner_mode": questioner_mode(), "question_pending": question_pending,
            "open_question": {**{k: open_q[k] for k in ("turn", "question", "decision", "offers_unknown")}, "questioner": open_q.get("questioner", "deterministic"), "fallback_reason": open_q.get("fallback_reason")} if open_q else None,
            "turns": _assessed(s["turns"], s["status"]), "contradictions": gi.contradictions(answered), "transcript_hash": gi.transcript_hash(s["turns"]), "requested_by": s["requested_by"], "decision_id": s["decision_id"],
            "created_at": s["created_at"], "updated_at": s["updated_at"],
            "statement": {"OPEN": "Answer the open question in your own words; a model reads it into the form and you see exactly what confirming would change.",
                          "READY_TO_CONFIRM": "The last reading covers your answer; confirming records your words as the decision.", "UNRESOLVED": "The interview stopped without a decision (you said you do not know, you stopped it, the turn limit was reached, or no further question could make the answer exact); nothing was saved and the structured controls continue.",
                          "CONFIRMED": "Your words were recorded as the decision through the ordinary answer path.", "STALE": "The knowledge version changed; nothing from this interview is saved; start again on the current version.",
                          "CLOSED": "The question is no longer open."}[s["status"]]}


def _assessed(turns: list, status: str) -> list:
    """Each answered turn with what its reading is worth now (history kept): role proposed_answer / unresolved_interpretation / no_reading, or confirmed_fact for the
    turn whose words the person confirmed; `superseded` when a later answer replaced it. Problems include the recheck with the current completeness rules."""
    answered = [i for i, t in enumerate(turns) if t.get("text") is not None]
    out = []
    for i, t in enumerate(turns):
        if t.get("text") is None:
            out.append(t)
            continue
        a = gi.assess(t)
        last = i == answered[-1]
        role = "confirmed_fact" if status == "CONFIRMED" and last and a["confirmable"] else a["role"]
        out.append(dict(t, assessment={**a, "role": role, "superseded": not last}))
    return out


def sessions_for_gap(store, tenant_id, workspace_id, gap_id) -> list:
    with store._tx() as cur:
        cur.execute("SELECT session_id, status, snapshot_id, created_at FROM v2_gap_interviews WHERE tenant_id=%s AND workspace_id=%s AND gap_id=%s ORDER BY created_at", (tenant_id, workspace_id, gap_id))
        return [{"session_id": r[0], "status": r[1], "snapshot_id": r[2], "created_at": r[3].isoformat()} for r in cur.fetchall()]
