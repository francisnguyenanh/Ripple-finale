"""Semantic conflict mining over one knowledge version (extension AIX-K1): request (one job per planned rule group), view (hypotheses + coverage + dismissals), dismiss.

The plan (groups, skipped rules, pair counts) is recomputed deterministically from the snapshot on every call, so no run table is needed: a group's job id is a function of
(snapshot, group, attempt). The view marks the result `stale` when the latest knowledge version is not the one mined; a dismissal is refused on a stale result.
"""
from __future__ import annotations

from psycopg2.extras import Json

from core.change_intelligence.v2 import business_mining as bm
from infra.adapters import v2_business
from infra.adapters.v2_business import BusinessKnowledgeRefused, read_snapshot
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.knowledge_mining import KIND, load_config

REASON_WORDS = {"authority_revoked": "the person who asked no longer has permission", "snapshot_moved": "the knowledge changed after the request", "group_moved": "the rule group changed",
                "schema_violation": "the model answer could not be used", "budget_exceeded": "the rule group was too large to send", "config_moved": "the mining configuration changed"}


def _state(rec) -> str:
    st = rec.get("execution_status")
    return {"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "failed"}.get(st) or ("waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running")


def _snapshot(store, tenant_id, workspace_id, snapshot_id=None):
    _, latest = v2_business._latest_view(store, tenant_id, workspace_id)
    if latest is None:
        raise BusinessKnowledgeRefused("no_snapshot", "There is no business knowledge to look at yet.")
    if snapshot_id is None or snapshot_id == latest["snapshot"]["snapshot_id"]:
        return latest, latest
    with read_snapshot(store) as cur:
        snap = v2_business._snapshot_view(cur, tenant_id, workspace_id, snapshot_id)
    if snap is None:
        raise TenantScopeError("knowledge version not found")
    return snap, latest


def request(store, tenant_id, workspace_id, *, attempt: int, authority: dict, limit: int, reservation) -> dict:
    """Create the missing group jobs of the latest knowledge version (idempotent per snapshot/group/attempt); returns the view."""
    snap, _ = _snapshot(store, tenant_id, workspace_id)
    frozen, sha = load_config()
    plan = bm.plan_groups(snap["rules"], frozen["limits"])
    sid = snap["snapshot"]["snapshot_id"]
    created, deferred = [], []
    if plan["groups"]:
        from infra import ai_admission
        from infra.adapters.v2_postgres import ConcurrencyLimitExceeded
        for g in plan["groups"]:
            jid = bm.group_job_id(sid, g["group_id"], attempt)
            try:
                store.job_record(tenant_id, workspace_id, jid)
                continue
            except TenantScopeError:
                pass
            if deferred:                                                                 # the workspace limit was hit: the remaining groups wait for the next request (same ids, idempotent)
                deferred.append(g["group_id"])
                continue
            head = store.head(tenant_id, workspace_id)
            try:
                store.create_job(tenant_id, workspace_id, jid, KIND, {"snapshot_id": sid, "group_id": g["group_id"], "attempt": attempt, "config_sha256": sha, "rule_ids": g["rule_ids"]},
                                 expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
            except ConcurrencyLimitExceeded:
                deferred.append(g["group_id"])
                continue
            ai_admission.admit(store, tenant_id, workspace_id, jid, KIND, authority)
            created.append(jid)
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'KNOWLEDGE_MINING_REQUESTED',%s,%s)",
                    (tenant_id, workspace_id, authority["account_id"], Json({"snapshot_id": sid, "attempt": attempt, "groups": len(plan["groups"]), "jobs_created": len(created), "config_sha256": sha})))
    return {**view(store, tenant_id, workspace_id, snapshot_id=sid, attempt=attempt), "jobs_created": created, "jobs_deferred": deferred,
            "statement_request": ("Some groups wait for a free slot of the workspace; request mining again once the running groups finish (the same groups keep the same job ids)." if deferred else "Every planned group has its job.")}


def view(store, tenant_id, workspace_id, *, snapshot_id: str | None = None, attempt: int = 1) -> dict:
    snap, latest = _snapshot(store, tenant_id, workspace_id, snapshot_id)
    frozen, sha = load_config()
    plan = bm.plan_groups(snap["rules"], frozen["limits"])
    sid = snap["snapshot"]["snapshot_id"]
    states, hypotheses, rejected, failures, jobs = {}, [], [], [], []
    for g in plan["groups"]:
        jid = bm.group_job_id(sid, g["group_id"], attempt)
        try:
            rec = store.job_record(tenant_id, workspace_id, jid)
        except TenantScopeError:
            states[g["group_id"]] = "not_requested"
            jobs.append({"group_id": g["group_id"], "job_id": jid, "state": "not_requested"})
            continue
        st = _state(rec)
        states[g["group_id"]] = st
        entry = {"group_id": g["group_id"], "job_id": jid, "state": st}
        if st == "completed" and rec.get("execution_result"):
            res = rec["execution_result"]
            hypotheses += [dict(h, job_id=jid) for h in res["hypotheses"]]
            rejected += [dict(r, group_id=g["group_id"]) for r in res["rejected"]]
            entry.update(model_status=res["model_status"], notes=res["notes"], config_sha256=res["config_sha256"])
        elif st == "failed":
            key = next((k for k in REASON_WORDS if (rec.get("execution_reason") or "").startswith(k)), None)
            entry["reason"] = "This group was not compared" + (f": {REASON_WORDS[key]}" if key else "") + "."
            failures.append(entry["reason"])
        jobs.append(entry)
    seen, unique, duplicates = set(), [], 0
    for h in hypotheses:                                                                 # the same hypothesis found by two groups is one hypothesis (its id is content-addressed)
        if h["hypothesis_id"] in seen:
            duplicates += 1
            continue
        seen.add(h["hypothesis_id"])
        unique.append(h)
    hypotheses = unique
    dismissals = _dismissals(store, tenant_id, workspace_id, [h["hypothesis_id"] for h in hypotheses])
    for h in hypotheses:
        d = dismissals.get(h["hypothesis_id"])
        h["status"] = "DISMISSED" if d else "PROPOSED"
        h["dismissal"] = d
    hypotheses.sort(key=lambda h: (h["status"] != "PROPOSED", h["kind"] != "potential_conflict", h["hypothesis_id"]))
    stale = sid != latest["snapshot"]["snapshot_id"]
    cov = bm.coverage(plan, states)
    return {"schema": bm.SCHEMA, "snapshot_id": sid, "attempt": attempt, "stale": stale, "config_sha256": sha, "hypotheses": hypotheses, "rejected": rejected, "coverage": cov, "jobs": jobs, "duplicates_merged": duplicates,
            "open": sum(1 for h in hypotheses if h["status"] == "PROPOSED"),
            "statement": (("This mining result is of an older knowledge version; request it again on the current one. " if stale else "")
                          + f"{len(hypotheses)} hypothesis(es) over {cov['pairs_compared']} of {cov['pairs_total']} rule pairs; " + ("every planned group was compared" if cov["complete"] else "coverage is partial")
                          + ". A hypothesis is a question for a person: dismiss it with a reason or raise it through the structured controls; nothing here changes the knowledge.")}


def _dismissals(store, tenant_id, workspace_id, ids) -> dict:
    if not ids:
        return {}
    with store._tx() as cur:
        cur.execute("SELECT hypothesis_id, snapshot_id, reason, actor_id, created_at FROM v2_mining_dismissals WHERE tenant_id=%s AND workspace_id=%s AND hypothesis_id = ANY(%s)", (tenant_id, workspace_id, list(ids)))
        return {r[0]: {"snapshot_id": r[1], "reason": r[2], "by": r[3], "at": r[4].isoformat()} for r in cur.fetchall()}


def dismiss(store, tenant_id, workspace_id, *, hypothesis_id: str, reason: str, actor_id: str, attempt: int = 1) -> dict:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise BusinessKnowledgeRefused("reason_invalid", "reason (1-500 characters) required")
    v = view(store, tenant_id, workspace_id, attempt=attempt)
    h = next((x for x in v["hypotheses"] if x["hypothesis_id"] == hypothesis_id), None)
    if h is None:
        raise TenantScopeError("hypothesis not found on the current knowledge version")
    if h["status"] == "DISMISSED":
        return {"hypothesis": h, "created": False}
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_mining_dismissals (tenant_id, workspace_id, hypothesis_id, snapshot_id, job_id, reason, actor_id) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tenant_id, workspace_id, hypothesis_id, v["snapshot_id"], h["job_id"], reason.strip(), actor_id))
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'KNOWLEDGE_MINING_DISMISSED',%s,%s)",
                    (tenant_id, workspace_id, actor_id, Json({"hypothesis_id": hypothesis_id, "snapshot_id": v["snapshot_id"], "kind": h["kind"], "rule_ids": h["rule_ids"], "chars": len(reason)})))
    h = next(x for x in view(store, tenant_id, workspace_id, attempt=attempt)["hypotheses"] if x["hypothesis_id"] == hypothesis_id)
    return {"hypothesis": h, "created": True}
