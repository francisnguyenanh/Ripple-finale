"""Which COMPLETED extraction jobs still describe the current source revision.

An extraction is pinned to the revision it read. After an apply only the edited file has a job pinned to the new head, but the other files did not change, so
their earlier reading is still valid (the Sources page and the Explorer already say so: "a file whose bytes are identical at the head"; an unknown hash counts as
changed, never optimistic). The draft and the coverage panel therefore must not look at the head revision only: after the first apply a draft would see 1 file of 13.

Rule, per artifact: the jobs pinned to `rev` when there are any; otherwise the jobs of the most recent earlier revision whose artifact blob hash equals the blob hash at
`rev` (the file did not change since). Read-only; nothing is created or changed. The cursor form runs inside a caller's transaction; the store form opens its own."""
from __future__ import annotations

import json


def completed_jobs(cur, tenant: str, ws: str, kind: str) -> list[dict]:
    cur.execute("""SELECT job_id, payload, execution_result, pinned_source_revision_id FROM v2_jobs
        WHERE tenant_id=%s AND workspace_id=%s AND kind=%s AND execution_status='COMPLETED' ORDER BY job_id""", (tenant, ws, kind))
    out = []
    for job_id, payload, result, pinned in cur.fetchall():
        payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
        result = json.loads(result) if isinstance(result, str) else (result or {})
        out.append({"job_id": job_id, "payload": payload, "result": result, "pinned_source_revision_id": pinned})
    return out


def revision_rank(cur, tenant: str, ws: str) -> dict[str, int]:
    cur.execute("SELECT revision_id FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at ASC, revision_id ASC", (tenant, ws))
    return {r[0]: i for i, r in enumerate(cur.fetchall())}


def valid_extraction_jobs_cur(cur, tenant: str, ws: str, rev: str, artifact_ids=None, jobs=None) -> list[dict]:
    jobs = completed_jobs(cur, tenant, ws, "extraction") if jobs is None else jobs
    wanted = set(artifact_ids) if artifact_ids is not None else None
    cur.execute("SELECT artifact_id, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s", (tenant, ws, rev))
    head_hash = {r[0]: r[1] for r in cur.fetchall()}
    order = revision_rank(cur, tenant, ws)
    older_revs = sorted({j["pinned_source_revision_id"] for j in jobs if j["pinned_source_revision_id"] not in (None, rev)})
    hash_at: dict = {}
    if older_revs:
        cur.execute("SELECT source_revision_id, artifact_id, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id = ANY(%s)",
                    (tenant, ws, older_revs))
        hash_at = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    here: dict[str, list[dict]] = {}
    older: dict[str, list[dict]] = {}
    for j in jobs:
        aid = (j["payload"] or {}).get("artifact_id")
        if not aid or (wanted is not None and aid not in wanted):
            continue
        if j["pinned_source_revision_id"] == rev:
            here.setdefault(aid, []).append(j)
        elif head_hash.get(aid) and hash_at.get((j["pinned_source_revision_id"], aid)) == head_hash[aid]:
            older.setdefault(aid, []).append(j)
    out: list[dict] = []
    for aid in sorted(set(here) | set(older)):
        if aid in here:
            out += here[aid]
        else:                                                        # the latest earlier revision at which this (unchanged) file was read: every job of THAT revision
            best = max(order.get(j["pinned_source_revision_id"], -1) for j in older[aid])
            out += [j for j in older[aid] if order.get(j["pinned_source_revision_id"], -1) == best]
    return out


def valid_extraction_jobs(store, tenant: str, ws: str, rev: str, artifact_ids=None) -> list[dict]:
    with store._tx() as cur:
        return valid_extraction_jobs_cur(cur, tenant, ws, rev, artifact_ids)
