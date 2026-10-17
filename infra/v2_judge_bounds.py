"""Bounds on the AI investigation of UNKNOWN rules (`impact.judge`), enforced where the job is created - not only in the page.

One judgement per (source revision, target rule, file): asking again for the same file would only spend another call for the same answer.
At most `JUDGE_CAP` investigations per target rule on one revision, so a person (or a script) cannot chain calls. A FAILED job does not count
against either bound (a retry of it is the existing bounded retry route). Everything else - permission (`ai.use`), the pinned role model, the
reservation in the ledger, the provider gate - is the same as for every other model call."""
from __future__ import annotations

import json

JUDGE_CAP = 3


def refusal(store, tenant: str, ws: str, rev: str | None, payload: dict) -> str | None:
    with store._tx() as cur:
        cur.execute("""SELECT payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='impact.judge'
            AND pinned_source_revision_id IS NOT DISTINCT FROM %s AND execution_status NOT IN ('FAILED', 'CANCELLED')""", (tenant, ws, rev))
        rows = [json.loads(r[0]) if isinstance(r[0], str) else (r[0] or {}) for r in cur.fetchall()]
    same_target = [p for p in rows if p.get("target_candidate_id") == payload.get("target_candidate_id")]
    if any(p.get("artifact_id") == payload.get("artifact_id") for p in same_target):
        return "impact.judge: this file was already investigated for this rule on this revision"
    if len(same_target) >= JUDGE_CAP:
        return f"impact.judge: at most {JUDGE_CAP} files are investigated per rule on one revision"
    return None
