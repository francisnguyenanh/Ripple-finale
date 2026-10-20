"""Semantic conflict mining over one knowledge version (extension AIX-K1): request (one job per planned rule group), view (hypotheses + coverage + dismissals), dismiss.

The plan (groups, skipped rules, pair counts) is recomputed deterministically from the snapshot on every call, so no run table is needed: a group's job id is a function of
(snapshot, group, attempt). The view marks the result `stale` when the latest knowledge version is not the one mined; a dismissal is refused on a stale result.
LHA-K1: the first request also creates the semantic candidate job of the snapshot (one call); once it is completed, its kept pairs join the plan as semantic groups, created
by the next request (same ids). The candidate pairs are suggestions to compare, shown with their state; they never become relations or findings.
SUR-02: for a requester with `evidence.read`, the request also creates the unbound-value triage job when the version has source numbers no rule uses; the view shows its
classifications (values and locations only to a reader with `evidence.read`). A classification is a proposal for a person, never a rule, a gap or a binding.
"""
from __future__ import annotations

from psycopg2.extras import Json

from core.change_intelligence.v2 import business_mining as bm, unbound_triage as ut
from infra.adapters import v2_business
from infra.adapters.v2_business import BusinessKnowledgeRefused, read_snapshot
from infra.adapters.v2_postgres import TenantScopeError
from infra.worker.knowledge_mining import KIND, load_config, semantic_pairs

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


def _unbound(store, tenant_id, workspace_id, snap) -> dict:
    return v2_business._unbound_values(store, tenant_id, workspace_id, snap["snapshot"]["source_revision_id"], snap["rules"])


def _unbound_view(store, tenant_id, workspace_id, sid, attempt, bind, viewer) -> dict:
    try:
        rec = store.job_record(tenant_id, workspace_id, ut.job_id(sid, attempt, bind))
    except TenantScopeError:
        return {"state": "not_requested", "items": [], "statement": "Not requested (it needs evidence.read and a source number no rule uses)."}
    res = rec.get("execution_result") or {}
    can = viewer is not None and "evidence.read" in v2_business.current_permissions(store, tenant_id, workspace_id, viewer)
    items = res.get("classified", [])
    if not can:
        items = [{k: i[k] for k in ("class", "class_proposed", "disposition", "rule_ids", "status", "origin", "file")} for i in items]
    return {"job_id": rec.get("job_id") or ut.job_id(sid, attempt, bind), "state": _state(rec), "items": items, "values_withheld": not can,
            "counts": {"classified": len(res.get("classified", [])), "rejected": len(res.get("rejected", [])), "not_classified": len(res.get("not_classified", [])),
                       "not_offered": res.get("not_offered"), "total": res.get("total")},
            "not_classified": res.get("not_classified", []) if can else [],
            "statement": "The model's reading of source numbers no rule uses: a proposal for a person. A policy candidate is not a rule; unclassified or not offered values stay unknown."}


def _wants_candidates(snap, frozen) -> bool:
    return int(frozen["limits"].get("max_semantic_pairs", 0)) > 0 and len(bm.candidate_offer(snap["rules"], frozen["limits"])["rules"]) >= 2


def request(store, tenant_id, workspace_id, *, attempt: int, authority: dict, limit: int, reservation) -> dict:
    """Create the missing group jobs (and the semantic candidate job) of the latest knowledge version (idempotent per snapshot/group/attempt); returns the view."""
    snap, _ = _snapshot(store, tenant_id, workspace_id)
    frozen, sha = load_config()
    sid = snap["snapshot"]["snapshot_id"]
    created, deferred = [], []
    from infra.worker import job_binding
    bind = job_binding.fingerprint(KIND)
    extra, cand = semantic_pairs(store, tenant_id, workspace_id, sid, attempt, bind)
    plan = bm.plan_groups(snap["rules"], frozen["limits"], extra)
    todo = [(bm.candidate_job_id(sid, attempt, bind), {"stage": "candidates"}, None)] if cand is None and _wants_candidates(snap, frozen) else []
    if "evidence.read" in v2_business.current_permissions(store, tenant_id, workspace_id, authority["account_id"]) and _unbound(store, tenant_id, workspace_id, snap)["count"]:
        todo.append((ut.job_id(sid, attempt, bind), {"stage": "unbound"}, "unbound"))
    todo += [(bm.group_job_id(sid, g["group_id"], attempt, bind), {"group_id": g["group_id"], "rule_ids": g["rule_ids"]}, g["group_id"]) for g in plan["groups"]]
    if todo:
        from infra import ai_admission
        from infra.adapters.v2_postgres import ConcurrencyLimitExceeded
        for jid, extra_payload, gid in todo:
            try:
                store.job_record(tenant_id, workspace_id, jid)
                continue
            except TenantScopeError:
                pass
            if deferred:                                                                 # the workspace limit was hit: the remaining groups wait for the next request (same ids, idempotent)
                deferred.append(gid or "candidates")
                continue
            head = store.head(tenant_id, workspace_id)
            try:
                store.create_job(tenant_id, workspace_id, jid, KIND, {"snapshot_id": sid, "attempt": attempt, "config_sha256": sha, **extra_payload},
                                 expected_version=head.version, idempotency_key=f"job-create-{jid}", max_concurrent_jobs=limit, budget_reservation=reservation, requested_by=authority["account_id"])
            except ConcurrencyLimitExceeded:
                deferred.append(gid or "candidates")
                continue
            ai_admission.admit(store, tenant_id, workspace_id, jid, KIND, authority)
            created.append(jid)
    with store._tx() as cur:
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'KNOWLEDGE_MINING_REQUESTED',%s,%s)",
                    (tenant_id, workspace_id, authority["account_id"], Json({"snapshot_id": sid, "attempt": attempt, "groups": len(plan["groups"]), "jobs_created": len(created), "config_sha256": sha})))
    return {**view(store, tenant_id, workspace_id, snapshot_id=sid, attempt=attempt, viewer=authority["account_id"]), "jobs_created": created, "jobs_deferred": deferred,
            "statement_request": ("Some groups wait for a free slot of the workspace; request mining again once the running groups finish (the same groups keep the same job ids)." if deferred else "Every planned group has its job.")}


def view(store, tenant_id, workspace_id, *, snapshot_id: str | None = None, attempt: int = 1, viewer: str | None = None) -> dict:
    snap, latest = _snapshot(store, tenant_id, workspace_id, snapshot_id)
    frozen, sha = load_config()
    sid = snap["snapshot"]["snapshot_id"]
    states, hypotheses, rejected, failures, jobs = {}, [], [], [], []
    from infra.worker import job_binding
    bind = job_binding.fingerprint(KIND)
    extra, cand = semantic_pairs(store, tenant_id, workspace_id, sid, attempt, bind)
    plan = bm.plan_groups(snap["rules"], frozen["limits"], extra)
    cres = (cand or {}).get("execution_result") or {}
    candidates = {"job_id": bm.candidate_job_id(sid, attempt, bind), "state": _state(cand) if cand else ("not_requested" if _wants_candidates(snap, frozen) else "not_needed"),
                  "pairs": extra, "rejected": cres.get("rejected", []), "offered": cres.get("offered"), "not_offered": cres.get("not_offered", []),
                  "statement": "Pairs the model suggested comparing although they share no field; each kept pair is compared as its own group. A suggestion is not a relation or a finding."}
    for g in plan["groups"]:
        jid = bm.group_job_id(sid, g["group_id"], attempt, bind)
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
            "semantic_candidates": candidates, "unbound_triage": _unbound_view(store, tenant_id, workspace_id, sid, attempt, bind, viewer),
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
