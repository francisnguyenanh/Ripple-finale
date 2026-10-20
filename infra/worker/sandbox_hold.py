"""Sandbox approval hold + sandbox configuration, shared by EVERY consumer of the job queue (stock worker, bounded worker
loop, sandbox pump) because it is enforced inside `job_worker.execute_admitted_claim`, the one place a queued job is run.

A workspace is a *sandbox* when either (a) `RIPPLE_SANDBOX_CONFIG` names it, or (b) shared database state says so: any
`SANDBOX_LEASE_*` audit event exists for it. (b) is what makes the hold independent of how a particular worker process was
configured: jobs in a sandbox can only be created under a lease (the web guard), so by the time such a job exists the
marker exists. Workspaces that never had a sandbox lease (production) are untouched: `approval_hold` is False for them.

Hold rule (sandbox workspaces only): a job whose executor is EXTERNAL (it would call a provider) and whose status is
PENDING_APPROVAL - a person asked for it and nobody approved it - is not executed: its claim is deferred (lease pushed out,
attempt not consumed), zero provider calls are made. System-created jobs (no requester) are not held.

No web framework imports here: the worker image can import this module cheaply.
"""

from __future__ import annotations

import json
import os

LEASE_EVENTS = ("SANDBOX_LEASE_ACQUIRED", "SANDBOX_LEASE_RENEWED", "SANDBOX_LEASE_RELEASED")
HOLD_SECONDS = 5
DEFAULTS = {"lease_minutes": 45, "max_jobs_per_lease": 40, "max_provider_jobs_per_lease": 12, "pump_max_jobs": 3,
            "pump_seconds": 60, "min_remaining_usd": 0.5, "seed_revision_id": None}
ALWAYS_HELD = ("explain", "sim.analysis", "change.draft", "gap.answer")
PROVIDER_JOB_KINDS = ("extraction", "analysis", "gap.answer", "impact.judge", "change.draft", "intent.interpret", "explain", "sim.analysis", "knowledge.qa", "change.understand", "change.impact_deps", "change.draft_guidance", "ma.specialist", "document.variant", "knowledge.mining", "gap.interview_question", "change.risk_memo", "ma.synthesis_memo", "ci.semantic_drift", "source.correspondence", "code.diff_proposal", "roi.analyst", "request.qa")


def config() -> dict:
    """{workspace_id: settings}; empty when the sandbox feature is off or the value is unusable (then only the shared-state
    marker below can make a workspace a sandbox)."""
    raw = os.environ.get("RIPPLE_SANDBOX_CONFIG", "").strip()
    if not raw:
        return {}
    try:
        if not raw.startswith("{"):
            with open(raw, encoding="utf-8") as fh:
                raw = fh.read()
        data = json.loads(raw).get("workspaces") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {w: {**DEFAULTS, **(s if isinstance(s, dict) else {})} for w, s in data.items() if isinstance(w, str)}


def is_sandbox(store, tenant_id: str, workspace_id: str) -> bool:
    if workspace_id in config():
        return True
    with store._tx() as cur:
        cur.execute("""SELECT 1 FROM v2_audit WHERE tenant_id=%s AND workspace_id=%s AND event_type = ANY(%s) LIMIT 1""",
                    (tenant_id, workspace_id, list(LEASE_EVENTS)))
        return cur.fetchone() is not None


def approval_hold(store, tenant_id: str, workspace_id: str, job_id: str, job_kind: str) -> bool:
    """True = do NOT run this job now (sandbox workspace, external executor, still PENDING_APPROVAL)."""
    if not job_id or not job_kind:
        return False
    from infra.worker.job_worker import resolve_executor
    executor = resolve_executor(job_kind)
    if executor is None or not getattr(executor, "external", True):
        return False
    # A stored wording of a verified result, a requester-asked simulation reading and an AI draft are created by a person and need ANOTHER person's allowance in every workspace.
    if job_kind not in ALWAYS_HELD and not is_sandbox(store, tenant_id, workspace_id):
        return False
    with store._tx() as cur:
        cur.execute("SELECT status FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s",
                    (tenant_id, workspace_id, job_id))
        row = cur.fetchone()
    return bool(row) and row[0] == "PENDING_APPROVAL"
