"""The 2026-10-08 live trial, inside the authorization the user gave (and nothing more):

  a. at most ONE re-extraction job of `harbor_demo/app/emergency.py` with the current (R1) code, at most 2 provider attempts;
  b. at most TWO `simulation.analyze` jobs (`sim.analysis`), at most 4 provider calls in all; the second one only with --second-analysis.

  python -m tools.judge_local.live_trial            # milestone 1, then milestone 2 (one analysis job)
  python -m tools.judge_local.live_trial --second-analysis   # only the optional second analysis job (a failure to check again)

The requester and admin passwords are typed at the prompt (or taken from RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD / RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD);
they are never printed, written or logged. Before anything is created the driver announces jobs, calls, retries and the worst-case reservation,
recounts the ledger (settled and pending) and refuses if the remaining local cap does not cover the worst case. The durable ceilings
(`live_local authorize-trial`) cap the number of jobs even if this driver were run twice. Evidence goes to <home>/trial_<time>.json (no credentials).

It never applies, never re-extracts anything else, never changes a model or a cap, and stops at the first provider failure.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from decimal import Decimal

from tools.judge_local import canary as K
from tools.judge_local import live_local as L
from tools.judge_local import live_run as R

EMERGENCY = "harbor_demo/app/emergency.py"
MARKER = "full_statement.v1"
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "REJECTED")


def _log_factory():
    path = L.HOME / f"trial_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    evidence: dict = {"started": datetime.datetime.now(datetime.timezone.utc).isoformat(), "steps": []}

    def log(msg, **data):
        print(f"{datetime.datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)
        evidence["steps"].append({"msg": msg, **data})
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return log, evidence, path


def _ceilings() -> dict:
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT kind, min(max_total) FROM v2_job_ceilings WHERE tenant_id=%s AND workspace_id=%s GROUP BY kind", (L.TENANT, L.WS))
        return {k: int(v) for k, v in cur.fetchall()}


def _count(kind: str) -> int:
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind=%s", (L.TENANT, L.WS, kind))
        return int(cur.fetchone()[0])


def preflight(log, evidence, want_extraction: bool, want_analysis_jobs: int) -> bool:
    cfg = R._config()
    from infra.worker.budget_invariants import worst_call_usd
    per_attempt = worst_call_usd(cfg)
    attempts = cfg.max_retries + 1
    per_job = per_attempt * attempts * cfg.max_provider_calls_per_job
    jobs = (1 if want_extraction else 0) + want_analysis_jobs
    worst = per_job * jobs
    calls = jobs * attempts * cfg.max_provider_calls_per_job
    settled, pending = R._spend()
    remaining = cfg.total_cap_usd - settled - pending
    log(f"ANNOUNCE: {jobs} job(s) ({'1 emergency.py re-extraction' if want_extraction else 'no extraction'}; {want_analysis_jobs} simulation.analyze), "
        f"at most {calls} provider call(s) including the 1 retry per job; worst-case reservation {worst} USD "
        f"({per_job} per job = {per_attempt} per attempt x {attempts}); this is a ceiling, NOT an expected cost",
        jobs=jobs, max_calls=calls, worst_case_usd=str(worst), per_job_usd=str(per_job))
    log(f"LEDGER: settled {settled} USD, pending reservations {pending} USD, local cap {cfg.total_cap_usd}, remaining {remaining}",
        settled=str(settled), pending=str(pending), cap=str(cfg.total_cap_usd), remaining=str(remaining))
    problems = []
    if not R._server_armed():
        problems.append("the server's last start was not armed")
    if pending != 0:
        problems.append(f"pending reservations exist ({pending})")
    if remaining < worst:
        problems.append("the remaining local cap does not cover the worst case")
    if cfg.role_model_ids.get("simulation.analyze") != "gemini-3.8-flash":
        problems.append("simulation.analyze is not pinned to gemini-3.8-flash in the budget config")
    if cfg.generation_max_output_tokens is None or cfg.max_output_tokens < 2 * cfg.generation_max_output_tokens:
        problems.append("the provider request cap is not set below half of the reservation ceiling")
    ceilings = _ceilings()
    if ceilings.get("sim.analysis", 99) > 2:
        problems.append("the sim.analysis job ceiling (2) is not in force: run `live_local authorize-trial`")
    if want_extraction and ceilings.get("extraction") is None:
        problems.append("the extraction job ceiling is not in force: run `live_local authorize-trial`")
    st = R._state()
    if st.get("halt"):
        problems.append("a HALT is in force: " + str(st["halt"]))
    if problems:
        log("REFUSED: " + "; ".join(problems), problems=problems)
        return False
    return True


def _job_attempts(job_id: str) -> list[dict]:
    attempts, _ledgers = K.read(L.dsn(), L.TENANT, L.WS)
    return [a for a in attempts if a["job_id"] == job_id]


def _cost_facts(job_id: str) -> dict:
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT attempt_key, outcome, reason, provider_calls, input_tokens, output_tokens, request_context, cost_usd, cost_estimate_usd "
                    "FROM v2_job_attempts WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s ORDER BY recorded_at", (L.TENANT, L.WS, job_id))
        rows = cur.fetchall()
        cur.execute("SELECT entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (L.TENANT, L.WS, job_id))
        led = cur.fetchone()
    atts = []
    for key, outcome, reason, calls, tin, tout, rc, cost, est in rows:
        rc = rc or {}
        atts.append({"outcome": outcome, "reason": reason, "provider_calls": calls, "input_tokens": tin, "output_tokens": tout,
                     "thought_tokens": rc.get("thought_tokens"), "generation_cap": rc.get("generation_cap"),
                     "output_plus_thinking": rc.get("output_plus_thinking"), "over_generation_cap": rc.get("over_generation_cap"),
                     "cost_usd": str(cost), "worst_case_estimate_usd": str(est)})
    return {"attempts": atts, "ledger": {"state": led[0], "cost_usd": str(led[1]), "reserved_usd": str(led[2])} if led else None}


def _wait(adm, job_id: str, timeout=900) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        body = adm.get(f"/v2/jobs/{job_id}", params={"workspace_id": L.WS}).json()
        if body.get("execution_status") in TERMINAL:
            return body
        time.sleep(4)
    return {"execution_status": "TIMEOUT"}


def judge_emergency(res: dict) -> dict:
    """What the stored result shows about the two compatible branches. A rule the MODEL did not emit and a rule the SERVER dropped are reported apart."""
    cands = res.get("candidates") or []

    def has(op, test):
        return [c["candidate_id"] for c in cands if c.get("operator") == op and test(c.get("value"))]
    le0 = has("<=", lambda v: v in (0, "0"))
    ge1m = has(">=", lambda v: v in (1000000, "1000000", "1_000_000"))
    rejected = res.get("rejected") or []
    return {"candidates": len(cands), "le_0_ids": le0, "ge_1000000_ids": ge1m, "both_present": bool(le0 and ge1m),
            "marker": res.get("merge_signature"), "rejected": rejected,
            "dropped_for_conflict": res.get("dropped_for_conflict"),
            "server_dropped_a_rule": any("operator_conflict" in str(r) for r in rejected) or bool(res.get("dropped_for_conflict")),
            "model_did_not_emit": ([] if le0 else ["<= 0"]) + ([] if ge1m else [">= 1000000"]),
            "summary": [(c.get("subject"), c.get("predicate"), c.get("operator"), c.get("value"), c.get("outcome")) for c in cands]}


def milestone_1(log, evidence, req, adm, store) -> dict | None:
    arts = R._artifacts(store)
    art = arts[EMERGENCY]
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT job_id, execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction' AND execution_status='COMPLETED' "
                    "AND payload->>'artifact_id'=%s ORDER BY job_id", (L.TENANT, L.WS, art["artifact_id"]))
        prior = cur.fetchall()
    with_marker = [(j, r) for j, r in prior if (r or {}).get("merge_signature") == MARKER]
    for j, r in with_marker:
        verdict = judge_emergency(r)
        if verdict["both_present"]:
            log(f"M1: reuse of the existing R1-code output {j}: both rules present, no new call", job_id=j, verdict=verdict)
            return {"job_id": j, "reused": True, **verdict}
    log(f"M1: no existing output from the R1 code proves both rules ({len(prior)} older outputs kept, {len(with_marker)} from R1 code); re-extracting emergency.py ONCE",
        older=[j for j, _ in prior])
    if _count("extraction") >= _ceilings().get("extraction", 0):
        log("M1: the extraction ceiling is already used: nothing is created")
        return None
    out = R._run_job(req, adm, art, "implementation")
    log(f"M1: job {out}", job=out)
    if out.get("refused") or out.get("status") != "COMPLETED":
        facts = _cost_facts(out["job_id"]) if not out.get("refused") else {}
        log("M1: the job did not complete: HALT for this trial (nothing is retried to get a nicer result)", facts=facts)
        st = R._state()
        st["halt"] = f"trial emergency.py re-extraction ended {out}"
        R._save(st)
        return {"job_id": out.get("job_id"), "completed": False, **out}
    body = adm.get(f"/v2/jobs/{out['job_id']}", params={"workspace_id": L.WS}).json()
    res = body["execution_result"]
    verdict = judge_emergency(res)
    facts = _cost_facts(out["job_id"])
    findings = K.report(_job_attempts(out["job_id"]), K.read(L.dsn(), L.TENANT, L.WS)[1], R._config(), ["extraction"])["extraction"]["findings"]
    log(f"M1: result {verdict['summary']}", verdict=verdict, facts=facts, canary_findings=findings)
    return {"job_id": out["job_id"], "reused": False, "completed": True, **verdict, "cost": facts, "canary_findings": findings}


def trace_branches(log, req, adm, m1: dict) -> dict:
    """Store -> relations -> findings -> Explorer for the two branches (deterministic jobs, no provider call)."""
    from tools.judge_local import live_journey as J
    ids = (m1.get("le_0_ids") or [])[:1] + (m1.get("ge_1000000_ids") or [])[:1]
    if len(ids) < 2:
        log("TRACE: skipped, the two branches are not both in the store")
        return {"skipped": True}
    ctx = J.new_ctx(ws=L.WS, req=req, adm=adm, apv=None, poll=3, log=log)
    out: dict = {"candidate_ids": ids}
    try:
        rj, rres = J.make_job(ctx, req, "relation", {"candidate_ids": ids}, "relations", approve=False)
        fj, fres = J.make_job(ctx, req, "finding", {"candidate_ids": ids}, "findings", approve=False)
    except J.JourneyStop as exc:
        log(f"TRACE: stopped at {exc.step}: {exc.finding}")
        return {**out, "stopped": exc.finding}
    findings = fres.get("findings") or []
    out.update(relation_job=rj, finding_job=fj,
               relation_edges=[(e.get("relation_type"), e.get("reason")) for e in (rres.get("proposed_edges") or [])],
               finding_kinds=[(f.get("kind"), f.get("reason")) for f in findings],
               false_conflict=any(f.get("kind") == "CONFLICT" for f in findings))
    cg = req.get("/v2/knowledge/graph", params={"workspace_id": L.WS})
    out["explorer_graph_http"] = cg.status_code
    out["explorer_lists_both"] = all(i in cg.text for i in ids) if cg.status_code == 200 else None
    log(f"TRACE: relations {out['relation_edges']}, findings {out['finding_kinds']}, false CONFLICT: {out['false_conflict']}, Explorer lists both: {out['explorer_lists_both']}", trace=out)
    return out


def milestone_2(log, req, adm, store, second: bool) -> dict:
    from tools.judge_local import live_journey as J
    import psycopg2
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:
        cur.execute("SELECT job_id FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='change.draft' AND execution_status='COMPLETED' ORDER BY job_id", (L.TENANT, L.WS))
        drafts = [r[0] for r in cur.fetchall()]
    if len(drafts) != 1:
        log(f"M2: expected exactly one stored draft, found {len(drafts)}: nothing is created", drafts=drafts)
        return {"skipped": True}
    ctx = J.new_ctx(ws=L.WS, req=req, adm=adm, apv=None, poll=3, log=log)
    with psycopg2.connect(L.dsn()) as cx, cx.cursor() as cur:       # reuse a stored, executed forward simulation of this draft (no new job)
        cur.execute("SELECT job_id, execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='simulation' AND execution_status='COMPLETED' "
                    "AND payload->>'from_draft_job_id'=%s ORDER BY job_id DESC", (L.TENANT, L.WS, drafts[0]))
        stored = [(j, r) for j, r in cur.fetchall() if (r or {}).get("status") == "EXECUTED" and (r.get("coverage") or {}).get("submitted")]
    if stored:
        sj, sres = stored[0]
        log(f"M2: reusing the stored executed forward simulation {sj} (no new simulation job)")
    else:
        try:
            sj, sres = J.make_job(ctx, req, "simulation", {"adapter": "harbor_cli", "from_draft_job_id": drafts[0]}, "forward simulation (no provider call)")
        except J.JourneyStop as exc:
            log(f"M2: the simulation stopped: {exc.finding}")
            return {"stopped": exc.finding}
    h = sres.get("harbor") or {}
    sim = {"job_id": sj, "status": sres.get("status"), "direction": sres.get("direction"), "built_from": sres.get("built_from"),
           "cases": (sres.get("coverage") or {}).get("submitted"), "changed": h.get("changed_count"), "unexplained": h.get("unexplained_changes"),
           "edits": h.get("edits"), "untested_edits": sres.get("untested_edits"), "reason": sres.get("reason")}
    log(f"M2: simulation {sim}", simulation=sim)
    if sres.get("status") != "EXECUTED" or not sim["cases"]:
        log("M2: no executed simulation with real cases: the AI reading is NOT requested")
        return {"simulation": sim, "analysis": None}
    ar = req.post("/v2/simulation-analyses", json={"workspace_id": L.WS, "simulation_job_id": sj, "retry": True})
    if ar.status_code != 200:
        log(f"M2: the analysis job was refused (HTTP {ar.status_code}): {ar.text[:200]}")
        return {"simulation": sim, "refused": ar.text[:200]}
    jid = ar.json()["job_id"]
    ap = adm.post(f"/v2/jobs/{jid}/approve", json={"workspace_id": L.WS}) if ar.json().get("status") == "waiting_for_approval" else None
    if ap is not None and ap.status_code != 200:
        log(f"M2: approval refused (HTTP {ap.status_code}): {ap.text[:200]}")
        return {"simulation": sim, "job_id": jid, "refused": ap.text[:200]}
    body = _wait(adm, jid)
    facts = _cost_facts(jid)
    got = adm.get("/v2/simulation-analyses", params={"workspace_id": L.WS, "simulation_job_id": sj}).json()
    calls_before = sum(a["provider_calls"] or 0 for a in facts["attempts"])
    for _ in range(3):                                     # read-only: refresh / poll / toggle must not create a call
        adm.get("/v2/simulation-analyses", params={"workspace_id": L.WS, "simulation_job_id": sj})
    calls_after = sum(a["provider_calls"] or 0 for a in _cost_facts(jid)["attempts"])
    out = {"simulation": sim, "job_id": jid, "status": body.get("execution_status"), "reason": body.get("execution_reason"),
           "analysis": got.get("analysis"), "currentness": got.get("currentness"), "cost": facts, "reads_added_calls": calls_after - calls_before}
    log(f"M2: analysis job {jid} {body.get('execution_status')} {body.get('execution_reason') or ''}; calls {calls_before}; reads added {calls_after - calls_before}", m2=out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--second-analysis", action="store_true", help="only the optional second simulation.analyze job (for a failure to check again)")
    ap.add_argument("--skip-extraction", action="store_true", help="do not look at or create the emergency.py job")
    a = ap.parse_args(argv)
    log, evidence, path = _log_factory()
    store = R._env()
    want_extraction = not a.second_analysis and not a.skip_extraction
    if not preflight(log, evidence, want_extraction, 1):
        return 2
    req = R._client("live-requester", R._secret("RIPPLE_LIVE_LOCAL_REQUESTER_PASSWORD", "live-requester"))
    adm = R._client("live-admin", R._secret("RIPPLE_LIVE_LOCAL_ADMIN_PASSWORD", "live-admin"))
    code = 0
    if want_extraction:
        m1 = milestone_1(log, evidence, req, adm, store)
        evidence["milestone_1"] = m1
        if m1 is None or m1.get("completed") is False:
            log("STOP: milestone 1 did not complete; milestone 2 is not started")
            return 1
        evidence["trace"] = trace_branches(log, req, adm, m1)
    if not R._state().get("halt"):
        evidence["milestone_2"] = milestone_2(log, req, adm, store, a.second_analysis)
    settled, pending = R._spend()
    evidence["ledger_after"] = {"settled": str(settled), "pending": str(pending), "remaining": str(R._config().total_cap_usd - settled - pending)}
    from infra.worker.budget_invariants import attempts_over_estimate
    evidence["attempts_over_estimate"] = attempts_over_estimate(L.dsn(), L.TENANT, L.WS)
    log(f"LEDGER AFTER: {evidence['ledger_after']}; attempts over their own worst-case estimate: {evidence['attempts_over_estimate']}")
    log(f"evidence file: {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
