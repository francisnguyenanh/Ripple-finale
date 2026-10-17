"""The approved Ripple loop, driven through the PRODUCT routes by the three demo accounts (requester, administrator, approver):

  free text -> clarification -> structured understanding -> person confirms -> request confirmed -> GenAI draft -> impact + simulation -> proposal
  -> contract -> review -> approval -> apply (supported edit only) -> re-extraction wave -> loop status -> query on the new revision -> explanation

Every decision step is performed by a demo account and is recorded here as SIMULATED automation: it is NOT human acceptance and records no G10/G11 result. Nothing is auto-confirmed
for a person who is not in the script, nothing is retried to get a better answer, nothing is replaced by a stub, and a failure stops the journey with the finding and the stored
job ids. No `impact.judge` job is created. The functions take their clients and a `pump` (the server's worker does the work; tests pump their own worker), so they run unchanged against
a stub provider in the test suite and against the real provider in live-local.
"""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

REQUEST_TEXT = "I want to lower the standard manager approval limit from 1,000,000 yen to 500,000 yen"
CLARIFY_TEXT = ("The manager approval limit for standard orders, JPY, net goods amount; it is 1,000,000 now and the new amount should be 500,000; "
                "it applies to standard and emergency orders")
TARGET_Y = "routing_rules.standard_manager_threshold_jpy"
QUESTION = "What is the Manager approval limit for standard orders?"
PROVENANCE = {"execution": "automated demo-account simulation", "human_acceptance": "NOT_RUN"}
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "REJECTED")


class JourneyStop(Exception):
    def __init__(self, step: str, finding: str, **ids):
        super().__init__(f"{step}: {finding}")
        self.step, self.finding, self.ids = step, finding, ids


def _post(c, ws, path, **body):
    return c.post(path, json={"workspace_id": ws, **body})


def _get(c, ws, path, **params):
    return c.get(path, params={"workspace_id": ws, **params})


def wait_job(ctx, job_id, step, timeout=900):
    t0 = time.time()
    while True:
        ctx.pump()
        rec = _get(ctx.adm, ctx.ws, f"/v2/jobs/{job_id}").json()
        if rec.get("execution_status") in TERMINAL:
            return rec
        if time.time() - t0 > timeout:
            raise JourneyStop(step, "timed out waiting for the job", job_id=job_id)
        time.sleep(ctx.poll)


def make_job(ctx, client, kind, payload, step, *, approve=False):
    jid = f"job-{kind.replace('.', '-')}-{uuid.uuid4().hex[:10]}"
    r = _post(client, ctx.ws, "/v2/jobs", kind=kind, job_id=jid, payload=payload)
    if r.status_code != 200:
        raise JourneyStop(step, f"the job was refused (HTTP {r.status_code}): {r.text[:200]}")
    if approve and r.json().get("status") == "PENDING_APPROVAL":     # a job the member's own AI permission admitted needs no second person
        a = _post(ctx.adm, ctx.ws, f"/v2/jobs/{jid}/approve")
        if a.status_code != 200:
            raise JourneyStop(step, f"approval of the job was refused (HTTP {a.status_code}): {a.text[:200]}", job_id=jid)
    rec = wait_job(ctx, jid, step)
    if rec.get("execution_status") != "COMPLETED":
        raise JourneyStop(step, f"the job ended {rec.get('execution_status')}: {rec.get('execution_reason')}", job_id=jid)
    ctx.log(f"{step}: job {jid} COMPLETED")
    return jid, rec["execution_result"]


def free_text_to_request(ctx) -> dict:
    r = _post(ctx.req, ctx.ws, "/v2/chat/start", text=REQUEST_TEXT)
    if r.status_code != 200:
        raise JourneyStop("chat", f"HTTP {r.status_code}: {r.text[:200]}")
    cid = r.json()["conversation_id"]
    if not r.json().get("awaiting_ai"):                                          # the requester's own AI permission starts the reply; nobody allows it
        raise JourneyStop("chat", f"the AI was not started ({r.json().get('ai_block') or 'the provider is off'})", conversation_id=cid)
    sent_clarification = False
    for _ in range(3):
        t0 = time.time()
        while True:
            ctx.pump()
            conv = _get(ctx.req, ctx.ws, f"/v2/chat/{cid}").json()
            if not conv.get("awaiting_ai") and conv.get("understanding") and conv["ai_used"] >= (2 if sent_clarification else 1):
                break
            if time.time() - t0 > 600:
                raise JourneyStop("chat", "timed out waiting for the interpreter", conversation_id=cid)
            time.sleep(ctx.poll)
        u = conv["understanding"]
        ctx.log(f"chat: understanding status {u['status']} (SIMULATED person: requester)")
        if u["status"] == "READY":
            break
        if sent_clarification:
            raise JourneyStop("chat", f"still {u['status']} after the clarification; stored for a person", conversation_id=cid)
        m = _post(ctx.req, ctx.ws, f"/v2/chat/{cid}/message", text=CLARIFY_TEXT)
        if m.status_code != 200:
            raise JourneyStop("chat", f"message refused (HTTP {m.status_code})", conversation_id=cid)
        sent_clarification = True
    else:
        raise JourneyStop("chat", "no READY understanding", conversation_id=cid)
    fields = conv["understanding"]["fields"]
    if fields.get("new_value") != 500000 or fields.get("old_value") != 1000000:
        raise JourneyStop("chat", f"the understanding does not carry the numbers the person typed: {fields.get('old_value')} -> {fields.get('new_value')}", conversation_id=cid)
    c = _post(ctx.req, ctx.ws, f"/v2/chat/{cid}/confirm", version=conv["version"], acknowledged=True)
    if c.status_code != 200:
        raise JourneyStop("chat", f"confirm refused (HTTP {c.status_code}): {c.text[:200]}", conversation_id=cid)
    iid = c.json()["intent_id"]
    ci = _post(ctx.adm, ctx.ws, "/v2/intent/confirm", intent_id=iid)
    if ci.status_code != 200:
        raise JourneyStop("request", f"administrator confirmation refused (HTTP {ci.status_code}): {ci.text[:200]}", intent_id=iid)
    ctx.log("request: confirmed by the administrator account (SIMULATED)")
    return {"conversation_id": cid, "intent_id": iid}


def target_candidate(ctx, extraction_ids, approval_artifact_id) -> str:
    for jid in extraction_ids:
        rec = _get(ctx.adm, ctx.ws, f"/v2/jobs/{jid}").json()
        if (rec.get("payload") or {}).get("artifact_id") != approval_artifact_id:
            continue
        for c in (rec.get("execution_result") or {}).get("candidates") or []:
            text = f"{c.get('subject')} {c.get('predicate')}".lower()
            if c.get("value") == 1000000 and "manager" in text and "threshold" in text.replace("limit", "threshold"):
                return c["candidate_id"]
    raise JourneyStop("impact", "extraction produced no candidate for the standard Manager limit (value 1,000,000): recorded as a GAP, nothing is invented")


def run_journey(ctx) -> dict:
    """ctx: ws, req, adm, apv (clients), approval_path, approval_artifact_id, extraction_ids, scope_artifact_ids, pump(), poll, log(). Returns the ids it created."""
    out: dict = {}
    out.update(free_text_to_request(ctx))
    iid = out["intent_id"]
    out["draft"], draft = make_job(ctx, ctx.req, "change.draft", {"intent_id": iid, "extraction_job_ids": ctx.extraction_ids}, "draft", approve=True)
    tid = target_candidate(ctx, ctx.extraction_ids, ctx.approval_artifact_id)
    out["impact"], _ = make_job(ctx, ctx.req, "impact", {"intent_id": iid, "candidate_ids": [tid], "target_candidate_id": tid, "scope_artifact_ids": ctx.scope_artifact_ids}, "impact")
    out["simulation"], _ = make_job(ctx, ctx.req, "simulation", {"adapter": "harbor_cli", "case": "route", "edits": [{"path": TARGET_Y, "value": 500000, "expected_current": 1000000}]}, "simulation")
    out["proposal"], _ = make_job(ctx, ctx.req, "proposal", {"intent_id": iid, "impact_job_id": out["impact"], "simulation_job_id": out["simulation"],
                                                             "target_artifact_id": ctx.approval_artifact_id, "edit_kind": "yaml_typed_edit", "draft_job_id": out["draft"]}, "proposal")
    rc = _post(ctx.apv, ctx.ws, "/v2/verification/contracts", proposal_job_id=out["proposal"], kind="yaml_value_equals", logical_path=ctx.approval_path,
               locator=TARGET_Y, expected=500000, provenance=PROVENANCE)
    if rc.status_code != 200:
        raise JourneyStop("contract", f"HTTP {rc.status_code}: {rc.text[:200]}")
    out["review"], review = make_job(ctx, ctx.req, "review", {"proposal_job_id": out["proposal"], "verification_contract_id": rc.json()["contract_id"]}, "review")
    if review.get("readiness") != "READY_FOR_HUMAN_REVIEW":
        raise JourneyStop("review", f"readiness {review.get('readiness')}", review_job_id=out["review"])
    out["approval"], appr = make_job(ctx, ctx.req, "approval", {"proposal_job_id": out["proposal"], "review_job_id": out["review"]}, "approval")
    d = _post(ctx.apv, ctx.ws, f"/v2/jobs/{out['approval']}/decision", action="approve", reason="SIMULATED demo-account approval (not human acceptance)",
              expected_approval_digest=appr["approval_digest"])
    if d.status_code != 200:
        raise JourneyStop("decision", f"HTTP {d.status_code}: {d.text[:200]}")
    ctx.log("decision: approved by the approver account (SIMULATED, distinct from the requester)")
    jid = f"job-apply-{uuid.uuid4().hex[:10]}"
    r = _post(ctx.req, ctx.ws, "/v2/jobs", kind="apply", job_id=jid, payload={"approval_job_id": out["approval"], "expected_approval_digest": appr["approval_digest"]})
    if r.status_code != 200:
        raise JourneyStop("apply", f"HTTP {r.status_code}: {r.text[:200]}")
    out["apply"] = jid
    wait_job(ctx, jid, "apply")
    t0, last = time.time(), None
    while time.time() - t0 < 1800:                                              # the re-extraction wave: every artifact of the new revision
        ctx.pump()
        st = _get(ctx.adm, ctx.ws, "/v2/change/loop-status", draft_job_id=out["draft"], apply_job_id=jid)
        if st.status_code == 200:
            s = st.json()
            ra = s["stages"].get("reanalysis")
            cur = (s["status"], str(ra))
            if cur != last:
                ctx.log(f"loop: {s['status']} reanalysis {ra}")
                last = cur
            if isinstance(ra, dict) and ra.get("total") and (ra.get("completed", 0) + ra.get("deferred_for_budget", 0) + ra.get("failed", 0)) >= ra["total"]:
                break
        time.sleep(ctx.poll)
    out["loop_status"] = s if st.status_code == 200 else None
    out["query"], answer = make_job(ctx, ctx.req, "gap.answer", {"question": QUESTION, "question_id": "q-" + uuid.uuid4().hex[:8], "artifact_ids": [ctx.approval_artifact_id]}, "query", approve=True)
    ex = _post(ctx.req, ctx.ws, "/v2/explanations", subject="approval", subject_id=out["approval"])
    if ex.status_code == 200 and ex.json().get("job_id"):
        if ex.json().get("status") == "waiting_for_approval":
            _post(ctx.adm, ctx.ws, f"/v2/jobs/{ex.json()['job_id']}/approve")
        wait_job(ctx, ex.json()["job_id"], "explanation")
        out["explanation"] = ex.json()["job_id"]
    out["answer_status"] = answer.get("status")
    ctx.log("journey finished; every decision above is SIMULATED automation, not human acceptance")
    return out


def new_ctx(**kw):
    base = dict(pump=lambda: None, poll=3, log=print)
    base.update(kw)
    return SimpleNamespace(**base)
