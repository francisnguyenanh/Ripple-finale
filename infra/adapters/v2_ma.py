"""Multi-agent runs (MA-00 coordinator, MA-R1 Review Board) over the existing job, admission, budget, ledger and audit primitives.

A run is started for a CONFIRMED change by a person who may review or approve changes (`ma.run`, plus `ai.use` for the model calls). The coordinator is deterministic server
code: it builds ONE authorized context from the review view (what that person may read), reserves the root budget (max children x per-job worst case) as a ledger row of the
run, dispatches the round-1 specialists as ordinary provider jobs (each child allocates its reservation out of the root row), joins when the children are terminal, runs at most one
bounded rebuttal round, and stores an immutable report. Children write their validated result into their own row before the join job is created, so a join never races the
worker's completion write; a late or cancelled child is ignored by compare-and-set on the run status. Nothing here approves, commits or applies; `REVIEW_READY` means a person
can read the report in the review.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
from decimal import Decimal

from psycopg2.extras import Json

from core.change_intelligence.v2 import ma_orchestration as ma
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_postgres import TenantScopeError, _as_object

KIND_SPECIALIST = "ma.specialist"
KIND_JOIN = "ma.join"
KIND_SANDBOX = "ma.sandbox"
KIND_SIMULATE = "ma.simulate"
ROLE = "ma.specialist"
CAPABILITY = "ma.run"
CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "ma_review_board.json"
AUDIT = {"start": "MA_RUN_STARTED", "final": "MA_RUN_FINALIZED", "cancel": "MA_RUN_CANCELLED"}


class MaRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_config(workflow: str = "review_board") -> tuple[dict, str]:
    path = CONFIG_PATH.parent / f"ma_{workflow}.json"
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


# ---- authorized context -------------------------------------------------------------------------------------------------------------------------------------------

def validate_calibration(cal) -> dict:
    """The optional calibration block of a simulation run: observed holdout KPIs (numbers the caller is allowed to use, with source and n) and per-KPI relative tolerances the domain
    owner declared; seeds for the convergence check. Nothing here is fetched: the caller supplies the numbers and remains accountable for their provenance."""
    if cal is None:
        return {}
    if not isinstance(cal, dict) or any(k not in ("observed", "tolerance_rel", "seeds", "convergence_tolerance_rel") for k in cal):
        raise MaRefused("calibration_invalid", "calibration may hold observed, tolerance_rel, seeds, convergence_tolerance_rel")
    out: dict = {}
    obs = cal.get("observed")
    if obs is not None:
        if not isinstance(obs, dict) or not isinstance(obs.get("source"), str) or not obs["source"].strip() or not isinstance(obs.get("n"), int) or obs["n"] <= 0:
            raise MaRefused("calibration_invalid", "observed needs a source (text) and n (positive integer)")
        keep = {"source": obs["source"][:200], "n": obs["n"]}
        for k in ("cycle_time_mean_days", "cycle_time_p95_days", "sla_breach_share"):
            if k in obs:
                v = obs[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 10000:
                    raise MaRefused("calibration_invalid", f"observed.{k} must be a non-negative number")
                keep[k] = float(v)
        out["observed"] = keep
    tol = cal.get("tolerance_rel")
    if tol is not None:
        if not isinstance(tol, dict) or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 < v <= 2 for v in tol.values()):
            raise MaRefused("calibration_invalid", "tolerance_rel values must be numbers in (0, 2]")
        out["tolerance_rel"] = {str(k)[:40]: float(v) for k, v in tol.items()}
    seeds = cal.get("seeds", 3)
    if isinstance(seeds, bool) or not isinstance(seeds, int) or not 2 <= seeds <= 6:
        raise MaRefused("calibration_invalid", "seeds must be an integer between 2 and 6")
    out["seeds"] = seeds
    ctol = cal.get("convergence_tolerance_rel", 0.1)
    if isinstance(ctol, bool) or not isinstance(ctol, (int, float)) or not 0 < ctol <= 1:
        raise MaRefused("calibration_invalid", "convergence_tolerance_rel must be in (0, 1]")
    out["convergence_tolerance_rel"] = float(ctol)
    return out


def authorized_context(store, tenant_id, workspace_id, change_id, *, account_id: str, roles, workflow: str = "review_board", candidate_job_id: str | None = None, calibration=None) -> tuple[dict, list, dict, bool]:
    """(context, allowed_refs, binding, knowledge_moved) for the person starting the run: built from the review view (nothing the person could not read). The arbiter workflow
    adds what the observed CODE text proves (the SYN-01 recommendations) and therefore needs `evidence.read`."""
    from infra.adapters import v2_business_review
    from infra.adapters.v2_policy import permissions_for
    view = v2_business_review.review_view(store, tenant_id, workspace_id, change_id, account_id=account_id, roles=roles)
    if view is None:
        raise MaRefused("not_confirmed", "the change request is not confirmed; a review board reads a confirmed change")
    draft = view["draft"]
    evidence = "evidence.read" in permissions_for(roles)
    m = ma.manifest(workflow)
    if m.get("requires_evidence_rights") and not evidence:
        raise MaRefused("evidence_rights_required", "this workflow reads code evidence; it needs the evidence.read permission")
    rules = [{"rule_id": c["rule_id"], "title": c.get("title"), "before": c.get("before_text"), "after": c.get("after_text"), "valid": c.get("valid", True)} for c in draft["rule_changes"]]
    ops = [{"op_id": o.get("op_id"), "kind": o.get("kind"), "rule_id": o.get("rule_id"), "outcome": o.get("outcome") or o.get("status"), "flags": [f.get("text") for f in o.get("flags", [])]}
           for o in draft.get("operations", [])]
    counter = [{"rule_id": c.get("rule_id"), "with": c.get("with"), "title": c.get("title"), "text": c.get("text")} for c in draft.get("counterevidence", [])]
    open_q = [{"ref": u.get("ref"), "text": u.get("text") or u.get("reason") or u.get("why")} for u in draft.get("unresolved", [])]
    files = [{"ref": f["ref"], "path": f.get("path"), "action": f.get("action"), "kind": f.get("kind")} for f in draft.get("file_actions", [])] if evidence else \
            [{"ref": f["ref"], "action": f.get("action"), "kind": f.get("kind")} for f in draft.get("file_actions", [])]
    refs = sorted({r["rule_id"] for r in rules if r.get("rule_id")} | {c["rule_id"] for c in counter if c.get("rule_id")} | {c["with"] for c in counter if c.get("with")}
                  | {o["op_id"] for o in ops if o.get("op_id")} | {f["ref"] for f in files if f.get("ref")} | {q["ref"] for q in open_q if q.get("ref")})
    context = {"change_id": change_id, "request": (view.get("intent") or {}).get("request") or (view.get("intent") or {}).get("text"), "rules": rules, "operations": ops,
               "counterevidence": counter, "open_questions": open_q, "files": files, "evidence_access": evidence, "code": []}
    if workflow == "simulation":
        context["rule_after"] = [{"rule_id": c["rule_id"], "after": c["after"]} for c in draft["rule_changes"] if c.get("valid", True) and isinstance(c.get("after"), dict)]
        actors = sorted({r.get("actor_ref") for c in draft["rule_changes"] for r in (c.get("before"), c.get("after")) if isinstance(r, dict) and r.get("actor_ref")})
        context["approver_roles"] = actors
        context["calibration"] = validate_calibration(calibration)
        refs = sorted(set(refs) | set(actors))
    if workflow == "arbiter":
        from core.change_intelligence.v2 import code_recommendation
        head = store.head(tenant_id, workspace_id)
        rec = code_recommendation.build_recommendations(draft, (view.get("intent") or {}).get("operations") or [], head={"source_revision_id": head.source_revision_id, "snapshot_id": draft["pin"].get("snapshot_id")})
        context["code"] = [{"ref": i["ref"], "rule_id": i.get("rule_id"), "path": i.get("path"), "status": i["status"], "why": i.get("why"), "observed_logic": (i.get("observed_logic") or "")[:600],
                            "literals": [{"line": l["line"], "literal": l["literal"]} for l in (i.get("literals") or [])[:6]], "desired": (i.get("desired") or {}).get("text"), "binding_status": i.get("binding_status")}
                           for i in rec["items"]]
        context["code_stale"] = rec.get("stale")
        refs = sorted(set(refs) | {c["ref"] for c in context["code"] if c.get("ref")})
    if workflow == "coder_tester":
        if not isinstance(candidate_job_id, str) or not candidate_job_id.startswith("cand-"):
            raise MaRefused("candidate_required", "the coder/tester workflow needs the completed candidate patch job of this change (candidate_job_id)")
        try:
            rec_job = store.job_record(tenant_id, workspace_id, candidate_job_id)
        except TenantScopeError:
            rec_job = None
        res = (rec_job or {}).get("execution_result") or {}
        if not rec_job or rec_job.get("kind") != "code.candidate" or rec_job.get("execution_status") != "COMPLETED" or res.get("change_id") != change_id or not res.get("diff"):
            raise MaRefused("candidate_not_completed", "the candidate patch job is missing, not completed, empty or belongs to another change")
        from infra.worker import code_change as cc
        repo = cc.repo_config(workspace_id, res["repo_id"])
        from core.change_intelligence.v2 import code_recommendation
        head = store.head(tenant_id, workspace_id)
        rec = code_recommendation.build_recommendations(draft, (view.get("intent") or {}).get("operations") or [], head={"source_revision_id": head.source_revision_id, "snapshot_id": draft["pin"].get("snapshot_id")})
        import re as _re
        code = []
        repo_files = {str(f.get("path")).rsplit("/", 1)[-1]: str(f.get("path")) for f in (res.get("files") or []) if isinstance(f, dict) and f.get("path")}   # the candidate's files in the repository's own layout
        for i in rec["items"]:
            art_path = i.get("path") or ""
            path = repo_files.get(art_path.rsplit("/", 1)[-1], art_path)
            module = path[:-3].replace("/", ".") if path.endswith(".py") else None
            funcs = sorted(set(_re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)\(", i.get("observed_logic") or "", _re.M)))
            code.append({"ref": i["ref"], "rule_id": i.get("rule_id"), "path": path, "module": module, "functions": funcs, "status": i["status"], "why": i.get("why"),
                         "observed_logic": (i.get("observed_logic") or "")[:600], "literals": [{"line": l["line"], "literal": l["literal"]} for l in (i.get("literals") or [])[:6]], "desired": (i.get("desired") or {}).get("text")})
        context["code"] = code
        context["candidate"] = {"candidate_job_id": candidate_job_id, "patch_hash": res["patch_hash"], "base_sha": res["base_sha"], "files": res.get("files"), "implemented": res.get("implemented"),
                                "repo_id": res["repo_id"], "allowlist": repo.get("allowlist"), "protected_commands": repo.get("validate") or []}
        context["allowed_modules"] = sorted({c["module"] for c in code if c.get("module")})
        context["allowed_functions"] = sorted({f for c in code for f in c.get("functions", [])})
        refs = sorted(set(refs) | {c["ref"] for c in code if c.get("ref")})
    binding = {"change_id": change_id, "semantic_diff_hash": draft["semantic_diff_hash"], "draft_hash": draft["draft_hash"], "pin": draft["pin"]}
    return context, refs, binding, bool(view["knowledge_moved"])


def pin_moved(store, tenant_id, workspace_id, change_id, pin, workflow: str) -> bool:
    """Did the knowledge move under this run? A review-stage workflow uses the review's own rule (any move after confirmation); a post-commit workflow (coder/tester) uses the
    code-change rule: the change's own commit is expected, only a move AGAIN after the commit (or moved sources) makes the binding stale."""
    from infra.adapters import v2_business, v2_business_review
    if workflow != "coder_tester":
        return bool(v2_business_review._knowledge_moved(store, tenant_id, workspace_id, pin))
    from infra.adapters import v2_code_change
    head = store.head(tenant_id, workspace_id)
    current = v2_business._working(v2_business.business_view(store, tenant_id, workspace_id))
    moved, _reasons = v2_code_change.binding_moved(store, tenant_id, workspace_id, change_id, pin, head, current)
    return bool(moved)


# ---- rows ---------------------------------------------------------------------------------------------------------------------------------------------------------

def _run(store, tenant_id, workspace_id, run_id) -> dict | None:
    with store._tx() as cur:
        cur.execute("""SELECT workflow, manifest_hash, change_id, binding, context, context_hash, status, caps, root_reserved_usd, requested_by, deadline_at, report, report_hash, created_at, updated_at
                       FROM v2_ma_runs WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s""", (tenant_id, workspace_id, run_id))
        r = cur.fetchone()
    if not r:
        return None
    return {"run_id": run_id, "workflow": r[0], "manifest_hash": r[1], "change_id": r[2], "binding": _as_object(r[3]), "context": _as_object(r[4]), "context_hash": r[5], "status": r[6],
            "caps": _as_object(r[7]), "root_reserved_usd": str(r[8]), "requested_by": r[9], "deadline_at": r[10].isoformat() if r[10] else None, "report": _as_object(r[11]),
            "report_hash": r[12], "created_at": r[13].isoformat(), "updated_at": r[14].isoformat()}


def _children(store, tenant_id, workspace_id, run_id) -> list:
    with store._tx() as cur:
        cur.execute("""SELECT c.child_id, c.role, c.round_no, c.attempt, c.job_id, c.status, c.result_hash, c.result, j.execution_status, j.execution_reason, l.entry_state, l.cost_usd, l.reserved_usd
                       FROM v2_ma_children c
                       LEFT JOIN v2_jobs j ON j.tenant_id=c.tenant_id AND j.workspace_id=c.workspace_id AND j.job_id=c.job_id
                       LEFT JOIN v2_job_cost_ledger l ON l.tenant_id=c.tenant_id AND l.workspace_id=c.workspace_id AND l.job_id=c.job_id
                       WHERE c.tenant_id=%s AND c.workspace_id=%s AND c.run_id=%s ORDER BY c.round_no, c.role, c.child_id""", (tenant_id, workspace_id, run_id))
        rows = cur.fetchall()
    return [{"child_id": r[0], "role": r[1], "round": r[2], "attempt": r[3], "job_id": r[4], "status": r[5], "result_hash": r[6], "result": _as_object(r[7]), "job_status": r[8],
             "job_reason": r[9], "entry_state": r[10], "cost_usd": str(r[11]) if r[11] is not None else None, "reserved_usd": str(r[12]) if r[12] is not None else None} for r in rows]


def _event(cur, tenant_id, workspace_id, run_id, event_type, actor_id, payload: dict) -> None:
    cur.execute("INSERT INTO v2_ma_events (tenant_id, workspace_id, run_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s,%s)",
                (tenant_id, workspace_id, run_id, event_type, actor_id, Json(payload)))


def _root_row(cur, tenant_id, workspace_id, run_id):
    cur.execute("SELECT entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s FOR UPDATE", (tenant_id, workspace_id, run_id))
    return cur.fetchone()


def _root_adjust(store, tenant_id, workspace_id, run_id, delta: Decimal) -> None:
    """Move `delta` out of (negative) or back into (positive) the root reservation row; the remainder never goes below zero."""
    with store._tx() as cur:
        row = _root_row(cur, tenant_id, workspace_id, run_id)
        if not row or row[0] != "RESERVED":
            return
        new = max(Decimal("0"), Decimal(row[2]) + delta)
        cur.execute("UPDATE v2_job_cost_ledger SET cost_usd=%s, reserved_usd=%s WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (str(new), str(new), tenant_id, workspace_id, run_id))


def _root_release(cur, tenant_id, workspace_id, run_id) -> None:
    cur.execute("""UPDATE v2_job_cost_ledger SET cost_usd='0', reserved_usd='0', entry_state='SETTLED', settled_at=now()
                   WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND entry_state='RESERVED'""", (tenant_id, workspace_id, run_id))


# ---- start ---------------------------------------------------------------------------------------------------------------------------------------------------------

def _budget():
    import os
    from infra.worker import job_quota
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        raise MaRefused("budget_config_required", "a multi-agent run needs the budget configuration (role ma.specialist pinned)")
    config = job_quota.load_worker_budget_config()
    if ROLE not in (config.role_model_ids or {}):
        raise MaRefused("role_model_not_pinned", "role ma.specialist is not pinned in the budget configuration")
    job_quota.check_job_admission(config, job_kind=KIND_SPECIALIST, real_calls_per_attempt=1)
    per_job, cap = job_quota.compute_budget_reservation(config)
    return config, Decimal(per_job), Decimal(cap)


def start_run(store, tenant_id, workspace_id, change_id, *, workflow: str, authority: dict, candidate_job_id: str | None = None, calibration=None) -> dict:
    from infra import ai_admission
    from infra.worker import job_quota
    from infra.worker.verification import ensure_job
    from infra.worker.budget_config import BudgetExceeded
    try:
        m = ma.manifest(workflow)
    except ValueError:
        raise MaRefused("unknown_workflow", f"no workflow {workflow!r}")
    context, refs, binding, moved = authorized_context(store, tenant_id, workspace_id, change_id, account_id=authority["account_id"], roles=authority.get("roles"), workflow=workflow,
                                                       candidate_job_id=candidate_job_id, calibration=calibration)
    if workflow == "coder_tester":
        moved = pin_moved(store, tenant_id, workspace_id, change_id, binding["pin"], workflow)
    if candidate_job_id:
        binding = dict(binding, candidate_job_id=candidate_job_id, patch_hash=context["candidate"]["patch_hash"])
    if moved:
        raise MaRefused("knowledge_moved", "the business knowledge moved after this change was confirmed; the review must be revalidated first")
    run_id = ma.run_id_for(workspace_id, workflow, binding)
    existing = _run(store, tenant_id, workspace_id, run_id)
    if existing is not None:
        return {"run_id": run_id, "created": False, "status": existing["status"]}
    config, per_job, cap = _budget()
    n = ma.max_children(m)
    root = per_job * n
    spent = job_quota.project_spent_usd(tenant_id=tenant_id, workspace_id=workspace_id)
    if n and spent + root > cap:
        raise MaRefused("budget_exceeded", f"the root reservation {root} USD for {n} possible children does not fit the remaining cap")
    caps = {"max_children": n, "per_child_worst_usd": str(per_job), "root_reserved_usd": str(root), "max_concurrency": m["max_concurrency"], "rounds": m["rounds"],
            "rebuttal_max_per_role": m["rebuttal_max_per_role"], "deadline_minutes": m["deadline_minutes"]}
    deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=m["deadline_minutes"])
    context_hash = canonical_hash(context)
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_ma_runs (tenant_id, workspace_id, run_id, workflow, manifest_hash, change_id, binding, context, context_hash, status, caps, root_reserved_usd, requested_by, deadline_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PLANNED',%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (tenant_id, workspace_id, run_id, workflow, ma.manifest_hash(m), change_id, Json(binding), Json(context), context_hash, Json(caps), str(root), authority["account_id"], deadline))
        cur.execute("""INSERT INTO v2_job_cost_ledger (tenant_id, workspace_id, job_id, cost_usd, entry_state, reserved_usd) VALUES (%s,%s,%s,%s,'RESERVED',%s) ON CONFLICT DO NOTHING""",
                    (tenant_id, workspace_id, run_id, str(root), str(root)))
        _event(cur, tenant_id, workspace_id, run_id, AUDIT["start"], authority["account_id"], {"workflow": workflow, "manifest_hash": ma.manifest_hash(m), "binding": binding, "context_hash": context_hash,
                                                                                               "caps": caps, "refs": len(refs)})
    for role in sorted(r for r in m["roles"] if ma.role_round(m, r) == 1):
        _plan_child(store, tenant_id, workspace_id, run_id, role=role, round_no=1, payload_extra={})
    with store._tx() as cur:
        cur.execute("UPDATE v2_ma_runs SET status='RUNNING', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s AND status='PLANNED'", (tenant_id, workspace_id, run_id))
    _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job)
    return {"run_id": run_id, "created": True, "status": "RUNNING"}


def _plan_child(store, tenant_id, workspace_id, run_id, *, role: str, round_no: int, payload_extra: dict) -> str:
    """A planned child is a row without a job yet: it takes a concurrency slot later (fair scheduling: a run never floods the worker pool or the workspace limit)."""
    child_id = ma.child_id_for(run_id, role, round_no)
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_ma_children (tenant_id, workspace_id, run_id, child_id, role, round_no, attempt, job_id, status, result) VALUES (%s,%s,%s,%s,%s,%s,1,%s,'PLANNED',%s) ON CONFLICT DO NOTHING""",
                    (tenant_id, workspace_id, run_id, child_id, role, round_no, child_id, Json({"planned_payload": payload_extra}) if payload_extra else None))
    return child_id


def _slot_limit(m) -> int:
    import os
    from infra.worker import job_quota
    limit = int(m["max_concurrency"])
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        ws_limit = job_quota.load_concurrency_limit()
        if ws_limit:
            limit = min(limit, int(ws_limit))
    return max(1, limit)


def _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job: Decimal) -> int:
    """Dispatch PLANNED children of the lowest open round while fewer than the slot limit are in flight. Returns how many were dispatched."""
    children = _children(store, tenant_id, workspace_id, run_id)
    in_flight = sum(1 for c in children if c["status"] == "DISPATCHED")
    planned = sorted((c for c in children if c["status"] == "PLANNED"), key=lambda c: (c["round"], c["role"]))
    n = 0
    for c in planned:
        if in_flight >= _slot_limit(m):
            break
        extra = ((c.get("result") or {}).get("planned_payload")) or {}
        _dispatch_child(store, tenant_id, workspace_id, run_id, role=c["role"], round_no=c["round"], authority=authority, per_job=per_job, payload_extra=extra)
        in_flight += 1
        n += 1
    return n


def _dispatch_child(store, tenant_id, workspace_id, run_id, *, role: str, round_no: int, authority: dict, per_job: Decimal, payload_extra: dict) -> str:
    from infra import ai_admission
    from infra.worker.verification import ensure_job
    from infra.worker.budget_config import BudgetExceeded
    run = _run(store, tenant_id, workspace_id, run_id)
    child_id = ma.child_id_for(run_id, role, round_no)
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_ma_children (tenant_id, workspace_id, run_id, child_id, role, round_no, attempt, job_id, status) VALUES (%s,%s,%s,%s,%s,%s,1,%s,'PLANNED') ON CONFLICT DO NOTHING""",
                    (tenant_id, workspace_id, run_id, child_id, role, round_no, child_id))
    m = ma.manifest(run["workflow"])
    deterministic = ma.is_deterministic(m, role)
    if not deterministic:
        _root_adjust(store, tenant_id, workspace_id, run_id, -per_job)                               # the child allocates its worst case out of the root reservation
    payload = {"run_id": run_id, "child_id": child_id, "role": role, "round": round_no, "change_id": run["change_id"], "requested_by": authority["account_id"], **payload_extra}
    if deterministic and not payload.get("candidate_job_id"):
        payload["candidate_job_id"] = (run["binding"] or {}).get("candidate_job_id")
    kind = ((m["roles"].get(role) or {}).get("job_kind") or KIND_SANDBOX) if deterministic else KIND_SPECIALIST
    try:
        created = ensure_job(store, tenant_id, workspace_id, child_id, kind, payload, reserve=not deterministic)
    except BudgetExceeded as exc:
        _root_adjust(store, tenant_id, workspace_id, run_id, per_job)
        with store._tx() as cur:
            cur.execute("UPDATE v2_ma_children SET status='FAILED', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND child_id=%s", (tenant_id, workspace_id, child_id))
            _event(cur, tenant_id, workspace_id, run_id, "MA_CHILD_REFUSED", "coordinator", {"child_id": child_id, "role": role, "round": round_no, "reason": "budget_exceeded"})
        return child_id
    rec = store.job_record(tenant_id, workspace_id, child_id)
    if not deterministic and rec.get("status") == "PENDING_APPROVAL":                               # a system-created job may already be admitted; policy admits a waiting one
        ai_admission.admit(store, tenant_id, workspace_id, child_id, KIND_SPECIALIST, authority)
    with store._tx() as cur:
        cur.execute("UPDATE v2_ma_children SET status='DISPATCHED', result=NULL, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND child_id=%s AND status='PLANNED'", (tenant_id, workspace_id, child_id))
        _event(cur, tenant_id, workspace_id, run_id, "MA_CHILD_DISPATCHED", "coordinator", {"child_id": child_id, "role": role, "round": round_no, "created": created})
    return child_id


# ---- child results (written by the specialist executor BEFORE its join job exists) -------------------------------------------------------------------------------

def record_child_result(store, tenant_id, workspace_id, child_id, *, status: str, result: dict | None) -> None:
    rh = canonical_hash(result) if result is not None else None
    with store._tx() as cur:
        cur.execute("""UPDATE v2_ma_children SET status=%s, result=%s, result_hash=%s, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND child_id=%s AND status='DISPATCHED'""",
                    (status, Json(result) if result is not None else None, rh, tenant_id, workspace_id, child_id))
        changed = cur.rowcount
        if not changed:                                                                      # a late result of a cancelled/ignored child: recorded as such, never joined
            cur.execute("UPDATE v2_ma_children SET status='LATE_IGNORED', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND child_id=%s AND status IN ('CANCELLED')", (tenant_id, workspace_id, child_id))


# ---- advance / join / cancel ---------------------------------------------------------------------------------------------------------------------------------------

def _sync_children(store, tenant_id, workspace_id, children: list) -> list:
    """A DISPATCHED child whose job ended without writing its row (worker died, refusal): the job axis decides."""
    for c in children:
        if c["status"] == "DISPATCHED" and c["job_status"] in ("FAILED", "CANCELLED", "REJECTED"):
            with store._tx() as cur:
                cur.execute("UPDATE v2_ma_children SET status=%s, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND child_id=%s AND status='DISPATCHED'",
                            ("CANCELLED" if c["job_status"] == "CANCELLED" else "FAILED", tenant_id, workspace_id, c["child_id"]))
            c["status"] = "CANCELLED" if c["job_status"] == "CANCELLED" else "FAILED"
    return children


def advance(store, tenant_id, workspace_id, run_id, *, actor_id: str = "coordinator") -> dict:
    """Idempotent coordinator step: sync children, dispatch the rebuttal round when round 1 is complete and warranted, finalize when everything is terminal or the deadline passed."""
    run = _run(store, tenant_id, workspace_id, run_id)
    if run is None:
        raise TenantScopeError("run not found")
    if run["status"] in ma.TERMINAL:
        return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)
    m = ma.manifest(run["workflow"])
    children = _sync_children(store, tenant_id, workspace_id, _children(store, tenant_id, workspace_id, run_id))
    pending = [c for c in children if c["status"] in ("PLANNED", "DISPATCHED")]
    now = datetime.datetime.now(datetime.timezone.utc)
    deadline = datetime.datetime.fromisoformat(run["deadline_at"]) if run["deadline_at"] else None
    if pending and deadline and now > deadline:
        _cancel_children(store, tenant_id, workspace_id, pending, reason="deadline")
        return _finalize(store, tenant_id, workspace_id, run_id, m, reason="deadline_passed", actor_id=actor_id)
    if pending:
        if any(c["status"] == "PLANNED" for c in pending):
            _, per_job, _cap = _budget()
            authority = {"account_id": run["requested_by"], "roles": _roles_of(store, tenant_id, workspace_id, run["requested_by"])}
            _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job)
        return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)
    round1 = [c for c in children if c["round"] == 1]
    round2 = [c for c in children if c["round"] == 2]
    synth = ma.synthesis_roles(m)
    required_first = [r for r, spec in m["roles"].items() if spec.get("required") and ma.role_round(m, r) == 1]
    if not round2 and synth and any(c["status"] != "COMPLETED" for c in round1 if c["role"] in required_first):
        return _finalize(store, tenant_id, workspace_id, run_id, m, reason=None, actor_id=actor_id)        # a required analysis failed: no synthesis over a hole, the report says who is missing
    if not round2 and synth:
        # MA-K1: the synthesis role receives the validated round-1 analyses as DATA (never the raw model text)
        inputs = [{"role": c["role"], "status": (c["result"] or {}).get("status"), "findings": (c["result"] or {}).get("findings") or [], "notes": (c["result"] or {}).get("notes") or [],
                   "cases": (c["result"] or {}).get("cases") or [], "scenarios": (c["result"] or {}).get("scenarios") or []} for c in round1 if c["status"] == "COMPLETED"]
        _, per_job, _cap = _budget()
        with store._tx() as cur:
            cur.execute("UPDATE v2_ma_runs SET status='JOINING', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s AND status='RUNNING'", (tenant_id, workspace_id, run_id))
            _event(cur, tenant_id, workspace_id, run_id, "MA_SYNTHESIS_ROUND", actor_id, {"roles": synth, "inputs": [i["role"] for i in inputs]})
        authority = {"account_id": run["requested_by"], "roles": _roles_of(store, tenant_id, workspace_id, run["requested_by"])}
        for role in synth:
            _plan_child(store, tenant_id, workspace_id, run_id, role=role, round_no=2, payload_extra={"synthesis_inputs": inputs})
        _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job)
        return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)
    if not round2 and m["rounds"] >= 2 and m["rebuttal_max_per_role"] >= 1:
        targets = ma.rebuttal_targets(round1, m)
        completed_roles = [c["role"] for c in round1 if c["status"] == "COMPLETED"]
        roles2 = ma.rebuttal_roles(targets, m, completed_roles)
        if roles2:
            _, per_job, _cap = _budget()
            with store._tx() as cur:
                cur.execute("UPDATE v2_ma_runs SET status='JOINING', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s AND status='RUNNING'", (tenant_id, workspace_id, run_id))
                _event(cur, tenant_id, workspace_id, run_id, "MA_REBUTTAL_ROUND", actor_id, {"claims": [t["claim_id"] for t in targets], "roles": sorted(roles2)})
            authority = {"account_id": run["requested_by"], "roles": _roles_of(store, tenant_id, workspace_id, run["requested_by"])}
            for role, claims in roles2.items():
                extra = {"rebuttal_claims": [{k: t[k] for k in ("claim_id", "category", "severity", "statement", "evidence_refs", "raised_by")} for t in claims]}
                _plan_child(store, tenant_id, workspace_id, run_id, role=role, round_no=2, payload_extra=extra)
            _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job)
            return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)
    if m.get("repair_max"):
        nxt = _repair_step(store, tenant_id, workspace_id, run, m, children, actor_id=actor_id)
        if nxt is not None:
            return nxt
    return _finalize(store, tenant_id, workspace_id, run_id, m, reason=None, actor_id=actor_id)


def _repair_step(store, tenant_id, workspace_id, run, m, children, *, actor_id: str):
    """AIX-C1 bounded repair loop (coder_tester). After the latest sandbox round: review_ready -> finalize; otherwise ask the repairer (at most `repair_max` times, attempts never reset);
    an accepted repair becomes a new candidate job (new patch hash) and one more sandbox round once that candidate is COMPLETED. Stops with `needs_human:<why>` and the whole
    failed history when the cap is reached, the repairer proposes nothing, the candidate fails or makes no progress (a patch hash seen before). Returns None to finalize now."""
    run_id = run["run_id"]
    sandboxes = sorted((c for c in children if ma.role_kind(m, c["role"]) == "sandbox"), key=lambda c: c["round"])
    repairers = sorted((c for c in children if ma.role_kind(m, c["role"]) == "repair"), key=lambda c: c["round"])
    if not sandboxes or sandboxes[-1]["status"] != "COMPLETED":
        return None
    last = sandboxes[-1]
    res = last["result"] or {}
    if res.get("verdict") == "review_ready":
        return None
    after = [r for r in repairers if r["round"] > last["round"]]
    hashes = {s["result"].get("patch_hash") for s in sandboxes if s["result"]}
    if after:
        rep = after[-1]
        rres = rep["result"] or {}
        if rep["status"] != "COMPLETED" or not rres.get("new_candidate_job_id"):
            return _finalize(store, tenant_id, workspace_id, run_id, m, reason=f"needs_human:repairer_proposed_nothing (attempt {len(repairers)} of {m['repair_max']})", actor_id=actor_id)
        try:
            cand = store.job_record(tenant_id, workspace_id, rres["new_candidate_job_id"])
        except TenantScopeError:
            cand = None
        if cand is None or cand.get("execution_status") in (None, "QUEUED", "RUNNING", "PENDING"):
            return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)             # the repair candidate has not run yet; its completion wakes the coordinator
        cres = cand.get("execution_result") or {}
        if cand.get("execution_status") != "COMPLETED" or not cres.get("diff"):
            return _finalize(store, tenant_id, workspace_id, run_id, m, reason=f"needs_human:repair_candidate_failed ({cand.get('execution_reason') or cres.get('status')})", actor_id=actor_id)
        if cres.get("patch_hash") in hashes:
            return _finalize(store, tenant_id, workspace_id, run_id, m, reason="needs_human:no_progress (the repair produced a patch already tried)", actor_id=actor_id)
        new_round = last["round"] + 2
        existing = [c for c in sandboxes if c["round"] == new_round]
        if not existing:
            _, per_job, _cap = _budget()
            authority = {"account_id": run["requested_by"], "roles": _roles_of(store, tenant_id, workspace_id, run["requested_by"])}
            with store._tx() as cur:
                _event(cur, tenant_id, workspace_id, run_id, "MA_REPAIR_ROUND", actor_id, {"attempt": len(repairers), "candidate_job_id": rres["new_candidate_job_id"], "patch_hash": cres.get("patch_hash"), "round": new_round})
            _plan_child(store, tenant_id, workspace_id, run_id, role=last["role"], round_no=new_round, payload_extra={"candidate_job_id": rres["new_candidate_job_id"], "synthesis_inputs": (last["result"] or {}).get("synthesis_inputs") or _tester_inputs(children, m)})
            _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job)
        return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)
    if len(repairers) >= int(m["repair_max"]):
        return _finalize(store, tenant_id, workspace_id, run_id, m, reason=f"needs_human:repairs_exhausted ({len(repairers)} of {m['repair_max']} repairs used; {len(sandboxes)} candidates tried)", actor_id=actor_id)
    role = next(r for r in m["roles"] if ma.role_kind(m, r) == "repair")
    regressions = [c for c in (res.get("cases") or []) if c.get("reading") == "regression"]
    feedback = {"candidate_job_id": res.get("candidate_job_id"), "patch_hash": res.get("patch_hash"), "verdict": res.get("verdict"), "protected": res.get("protected"),
                "commands": [{"exit": c.get("exit"), "output_tail": c.get("output_tail")} for c in (res.get("protected") or {}).get("commands_patched", [])], "regressions": regressions}
    _, per_job, _cap = _budget()
    authority = {"account_id": run["requested_by"], "roles": _roles_of(store, tenant_id, workspace_id, run["requested_by"])}
    with store._tx() as cur:
        _event(cur, tenant_id, workspace_id, run_id, "MA_REPAIR_REQUESTED", actor_id, {"attempt": len(repairers) + 1, "of": m["repair_max"], "after_round": last["round"], "verdict": res.get("verdict")})
    _plan_child(store, tenant_id, workspace_id, run_id, role=role, round_no=last["round"] + 1, payload_extra={"feedback": feedback, "attempt": len(repairers) + 1})
    _dispatch_slots(store, tenant_id, workspace_id, run_id, m, authority, per_job)
    return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)


def _tester_inputs(children, m) -> list:
    return [{"role": c["role"], "status": (c["result"] or {}).get("status"), "findings": [], "notes": [], "cases": (c["result"] or {}).get("cases") or [], "scenarios": []}
            for c in children if c["round"] == 1 and c["status"] == "COMPLETED"]


def _roles_of(store, tenant_id, workspace_id, account_id):
    from infra.adapters import v2_business
    return tuple(v2_business.current_roles(store, tenant_id, workspace_id, account_id))


def _finalize(store, tenant_id, workspace_id, run_id, m, *, reason, actor_id) -> dict:
    run = _run(store, tenant_id, workspace_id, run_id)
    children = _children(store, tenant_id, workspace_id, run_id)
    moved = pin_moved(store, tenant_id, workspace_id, run["change_id"], run["binding"]["pin"], run["workflow"])
    if moved and not reason:
        reason = "pin_moved: the business knowledge moved after the run started; the report is stale"
    rows = [{"child_id": c["child_id"], "role": c["role"], "round": c["round"], "status": c["status"].lower() if c["status"] in ("COMPLETED", "FAILED", "CANCELLED") else "pending",
             "result": c["result"]} for c in children]
    report = ma.join(rows, m, binding=run["binding"], reason_incomplete=reason)
    with store._tx() as cur:
        cur.execute("""UPDATE v2_ma_runs SET status=%s, report=%s, report_hash=%s, updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s AND status IN ('RUNNING','JOINING')""",
                    (report["status"], Json(report), report["report_hash"], tenant_id, workspace_id, run_id))
        if cur.rowcount:
            _root_release(cur, tenant_id, workspace_id, run_id)
            _event(cur, tenant_id, workspace_id, run_id, AUDIT["final"], actor_id, {"status": report["status"], "report_hash": report["report_hash"], "findings": len(report["findings"]),
                                                                                    "unresolved": len(report["unresolved_disagreements"]), "missing_required_roles": report["missing_required_roles"], "reason": reason})
    return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)


def _cancel_children(store, tenant_id, workspace_id, children: list, *, reason: str) -> None:
    for c in children:
        try:
            store.request_cancel(tenant_id, workspace_id, c["job_id"], reason)
        except Exception:                                                                   # already executing or finished: the worker's own result decides; the join ignores it if late
            pass
        with store._tx() as cur:
            cur.execute("UPDATE v2_ma_children SET status='CANCELLED', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND child_id=%s AND status IN ('PLANNED','DISPATCHED')", (tenant_id, workspace_id, c["child_id"]))


def cancel(store, tenant_id, workspace_id, run_id, *, actor_id: str, reason: str) -> dict:
    run = _run(store, tenant_id, workspace_id, run_id)
    if run is None:
        raise TenantScopeError("run not found")
    if run["status"] in ma.TERMINAL:
        return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)
    children = _children(store, tenant_id, workspace_id, run_id)
    _cancel_children(store, tenant_id, workspace_id, [c for c in children if c["status"] in ("PLANNED", "DISPATCHED")], reason=reason)
    with store._tx() as cur:
        cur.execute("UPDATE v2_ma_runs SET status='CANCELLED', updated_at=now() WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s AND status IN ('PLANNED','RUNNING','JOINING')", (tenant_id, workspace_id, run_id))
        if cur.rowcount:
            _root_release(cur, tenant_id, workspace_id, run_id)
            _event(cur, tenant_id, workspace_id, run_id, AUDIT["cancel"], actor_id, {"reason": reason[:200]})
    return state(store, tenant_id, workspace_id, run_id, can_read_evidence=True)


# ---- read model ------------------------------------------------------------------------------------------------------------------------------------------------------

def state(store, tenant_id, workspace_id, run_id, *, can_read_evidence: bool) -> dict:
    run = _run(store, tenant_id, workspace_id, run_id)
    if run is None:
        raise TenantScopeError("run not found")
    children = _children(store, tenant_id, workspace_id, run_id)
    with store._tx() as cur:
        cur.execute("SELECT entry_state, cost_usd, reserved_usd FROM v2_job_cost_ledger WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s", (tenant_id, workspace_id, run_id))
        root = cur.fetchone()
        cur.execute("SELECT event_type, actor_id, payload, created_at FROM v2_ma_events WHERE tenant_id=%s AND workspace_id=%s AND run_id=%s ORDER BY id", (tenant_id, workspace_id, run_id))
        events = [{"event": r[0], "actor": r[1], "payload": _as_object(r[2]), "at": r[3].isoformat()} for r in cur.fetchall()]
    ledger_rows = [{"entry_state": c["entry_state"], "cost_usd": c["cost_usd"], "reserved_usd": c["reserved_usd"]} for c in children if c["entry_state"]]
    remainder = Decimal(str(root[2])) if root and root[0] == "RESERVED" else Decimal("0")
    invariant = ma.budget_invariant(run["root_reserved_usd"], ledger_rows + [{"root_remainder_usd": str(remainder)}])
    report = run["report"]
    if report is not None and not can_read_evidence:
        report = dict(report, findings=[{k: v for k, v in f.items() if k != "evidence_refs"} | {"evidence_refs_count": len(f.get("evidence_refs", []))} for f in report["findings"]],
                      coverage={k: v for k, v in report["coverage"].items() if k != "evidence_sources"},
                      mismatch_table=[{k: v for k, v in r.items() if k not in ("policy_refs", "code_refs", "code_shows")} | {"code_shows": "[needs evidence rights]"} for r in report.get("mismatch_table", [])])
    return {"run_id": run_id, "workflow": run["workflow"], "status": run["status"], "change_id": run["change_id"], "binding": run["binding"], "context_hash": run["context_hash"], "caps": run["caps"],
            "requested_by": run["requested_by"], "deadline_at": run["deadline_at"], "created_at": run["created_at"], "updated_at": run["updated_at"],
            "children": [{"child_id": c["child_id"], "role": c["role"], "round": c["round"], "status": c["status"], "job_status": c["job_status"], "job_reason": c["job_reason"],
                          "findings": len((c["result"] or {}).get("findings") or []), "rejected": len((c["result"] or {}).get("rejected") or []), "disputes": len((c["result"] or {}).get("disputes") or []),
                          "cost_usd": c["cost_usd"], "entry_state": c["entry_state"]} for c in children],
            "budget": invariant, "report": report, "report_hash": run["report_hash"], "events": events,
            "statement": {"RUNNING": "Specialists are working; nothing is decided.", "JOINING": "A bounded second round is running (rebuttal or synthesis).", "PLANNED": "Planned.",
                          "REVIEW_READY": "A person reads this report in the review; agreement among specialists is not approval.", "INCOMPLETE": "The report is incomplete and says why; a person decides what to do.",
                          "FAILED": "The run failed.", "CANCELLED": "The run was cancelled; late results are ignored."}[run["status"]]}


def runs_for_change(store, tenant_id, workspace_id, change_id) -> list:
    with store._tx() as cur:
        cur.execute("SELECT run_id, workflow, status, created_at FROM v2_ma_runs WHERE tenant_id=%s AND workspace_id=%s AND change_id=%s ORDER BY created_at", (tenant_id, workspace_id, change_id))
        return [{"run_id": r[0], "workflow": r[1], "status": r[2], "created_at": r[3].isoformat()} for r in cur.fetchall()]
