"""J2: per-node timeline (who / why / before-after / revisions) over rows the store already holds. Read-only.

Candidate ids change on every re-extraction, so a rule's history is followed through a LINEAGE KEY
(artifact_id, normalised subject, predicate, first evidence locator), never through the candidate id. A key that
matches more than one candidate inside one extraction job is ambiguous and is NOT merged (reported in `lineage_notes`).
Events are categorised so a knowledge decision is never presented as a source change:
  analysis  = a model extraction produced/changed a value (not a person's decision, not a file edit)
  knowledge = a human decision / answer / accepted knowledge revision (the source file is NOT rewritten)
  source    = the file itself changed (new source revision)
"""

from __future__ import annotations

import json


def _obj(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _norm(s) -> str:
    return " ".join(str(s or "").lower().split())


def lineage_key(artifact_id: str, c: dict):
    ev = [e for e in (c.get("evidence") or []) if isinstance(e, dict)]
    locator = json.dumps(ev[0].get("locator"), sort_keys=True, default=str) if ev else None
    return (artifact_id, _norm(c.get("subject")), _norm(c.get("predicate")), locator)


def apply_provenance(store, tenant: str, workspace_id: str, extraction_job_ids) -> tuple[dict, dict]:
    """Stored provenance of changes made by an apply. -> (published_by, caused_by):
    published_by[revision_id] = {"apply_job_id", "artifact_id"}  (the revision an apply published and the one file it edited, from the apply job's result)
    caused_by[extraction_job_id] = apply_job_id                   (extraction jobs scheduled by an apply, from their own payload)"""
    published_by: dict = {}
    caused_by: dict = {}
    with store._tx() as cur:
        cur.execute("""SELECT job_id, execution_result FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='apply'
            AND execution_status='COMPLETED'""", (tenant, workspace_id))
        for job_id, raw in cur.fetchall():
            res = _obj(raw)
            if res.get("published_revision_id") and res.get("target_artifact_id") and not res.get("is_no_op"):
                published_by[res["published_revision_id"]] = {"apply_job_id": job_id, "artifact_id": res["target_artifact_id"]}
        ids = sorted({i for i in extraction_job_ids if i})
        if ids:
            cur.execute("""SELECT job_id, payload FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)""",
                        (tenant, workspace_id, ids))
            for job_id, raw in cur.fetchall():
                cause = _obj(raw).get("caused_by_apply_job_id")
                if cause:
                    caused_by[job_id] = cause
    return published_by, caused_by


def build_timeline(store, tenant: str, workspace_id: str, node_id: str, *, with_evidence: bool) -> dict | None:
    with store._tx() as cur:
        cur.execute("""SELECT j.job_id, j.execution_status, j.execution_result, j.payload,
                j.pinned_source_revision_id, j.execution_model_id,
                (SELECT max(a.recorded_at) FROM v2_job_attempts a WHERE a.tenant_id=j.tenant_id
                   AND a.workspace_id=j.workspace_id AND a.job_id=j.job_id) AS done_at
            FROM v2_jobs j
            WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction' AND j.execution_status='COMPLETED'
            ORDER BY done_at NULLS FIRST, j.job_id""", (tenant, workspace_id))
        jobs = cur.fetchall()
        cur.execute("""SELECT decision_id, epoch, actor_id, target, action, rationale, evidence_ref, provenance,
                scope, superseded_by, revoked_by, created_at FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND action IS NOT NULL ORDER BY epoch""", (tenant, workspace_id))
        decisions = cur.fetchall()
        cur.execute("""SELECT question_id, actor_id, answer_text, version_after, created_at FROM v2_hitl_answers
            WHERE tenant_id=%s AND workspace_id=%s ORDER BY id""", (tenant, workspace_id))
        answers = cur.fetchall()
        cur.execute("""SELECT revision_id, source_revision_id, decision_epoch, created_at FROM v2_knowledge_revisions
            WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at""", (tenant, workspace_id))
        kms = cur.fetchall()
        cur.execute("""SELECT revision_id, parent_revision_id, trigger, created_by, created_at FROM v2_source_revisions
            WHERE tenant_id=%s AND workspace_id=%s ORDER BY created_at""", (tenant, workspace_id))
        revs = {r[0]: r for r in cur.fetchall()}
        cur.execute("""SELECT source_revision_id, artifact_id, blob_hash, logical_path FROM v2_artifact_revisions
            WHERE tenant_id=%s AND workspace_id=%s""", (tenant, workspace_id))
        arevs = cur.fetchall()

    # every candidate of every completed extraction job, with the job it came from
    members = []   # (job_id, artifact_id, revision, model, candidate)
    for job_id, _st, raw_result, raw_payload, pinned, model, done_at in jobs:
        aid = _obj(raw_payload).get("artifact_id")
        for c in _obj(raw_result).get("candidates") or []:
            if isinstance(c, dict) and c.get("candidate_id"):
                members.append((job_id, aid, c.get("source_revision_id") or pinned, model, c, done_at))
    target = next((m for m in members if m[4]["candidate_id"] == node_id), None)
    if target is None:
        return None

    key = lineage_key(target[1], target[4])
    per_job: dict = {}
    for m in members:
        if lineage_key(m[1], m[4]) == key:
            per_job.setdefault(m[0], []).append(m)
    notes, lineage = [], []
    for job_id, ms in per_job.items():
        if len(ms) > 1:
            notes.append(f"job {job_id}: {len(ms)} candidates share the lineage key; not merged")
            ms = [m for m in ms if m[4]["candidate_id"] == node_id]
        lineage.extend(ms)
    ids = {m[4]["candidate_id"] for m in lineage}
    path_by_art = {}
    for _rev, aid, _h, path in arevs:
        path_by_art[aid] = path

    finding_ids = {f.get("finding_id") for f in store.knowledge_draft(tenant, workspace_id, target[1])["findings"]
                   if isinstance(f, dict) and set(f.get("involved_claim_ids") or []) & ids}
    events = []
    prev = None
    published_by, caused_by = apply_provenance(store, tenant, workspace_id, {m[0] for m in lineage})
    for job_id, aid, rev, model, c, done_at in lineage:
        value = {"value": c.get("value"), "unit": c.get("unit"), "scope": c.get("scope"), "modality": c.get("modality")}
        ts = done_at
        events.append({"ts": ts.isoformat() if ts else None, "category": "analysis", "kind": "extracted",
                       "actor": "model:" + str(model) if model else "model", "candidate_id": c["candidate_id"],
                       "job_id": job_id, "source_revision_id": rev, "after": value,
                       "read_again_after_apply": caused_by.get(job_id),
                       "why": "read again after an applied change; not a human decision" if caused_by.get(job_id)
                       else "extraction of the file at this source revision (not a human decision)"})
        if prev is not None and prev != value:
            value_differs = (prev["value"], prev["unit"]) != (value["value"], value["unit"])
            edited = published_by.get(rev)
            events.append({"ts": ts.isoformat() if ts else None, "category": "analysis", "kind": "value_changed_in_source",
                           "actor": "model", "candidate_id": c["candidate_id"], "job_id": job_id, "before": prev,
                           "after": value, "source_revision_id": rev,
                           "change_kind": "value_changed" if value_differs else "scope_only",
                           "source_edited_by_apply": edited["apply_job_id"] if edited and edited["artifact_id"] == aid else None,
                           "why": ("the rule's value reads differently than in the earlier reading" if value_differs
                                   else "the rule was read again with a different scope; its value is unchanged")})
        prev = value

    for did, epoch, actor, tgt, action, rationale, ref, prov, scope, sup, rev_by, ts in decisions:
        if tgt in ids or tgt in finding_ids:
            events.append({"ts": ts.isoformat() if ts else None, "category": "knowledge", "kind": "decision:" + str(action),
                           "actor": actor, "decision_id": did, "epoch": epoch, "why": rationale,
                           "evidence_ref": ref if with_evidence else None, "provenance": prov, "candidate_id": tgt, "target_kind": "finding" if tgt in finding_ids else "rule",
                           "state": "active" if sup is None and rev_by is None else ("superseded" if sup else "revoked"),
                           "note": "knowledge decision - the source file is not changed by it"})
    for qid, actor, text, version, ts in answers:
        if qid == f"question-confirm-{node_id}" or any(qid.endswith(i) for i in ids):
            events.append({"ts": ts.isoformat() if ts else None, "category": "knowledge", "kind": "answer",
                           "actor": actor, "why": text, "version_after": version, "question_id": qid})

    # the file itself: artifact revisions of this artifact whose blob changed
    last_hash = None
    ordered = sorted((r for r in arevs if r[1] == target[1]),
                     key=lambda r: (str(revs[r[0]][4]) if r[0] in revs else "", r[0]))
    for rev, aid, bh, path in ordered:
        if last_hash is not None and bh != last_hash:
            info = revs.get(rev)
            events.append({"ts": info[4].isoformat() if info and info[4] else None, "category": "source",
                           "kind": "source_changed", "actor": info[3] if info else None, "source_revision_id": rev,
                           "before": {"blob_hash": last_hash}, "after": {"blob_hash": bh},
                           "edited_by_apply": (published_by[rev]["apply_job_id"] if rev in published_by and published_by[rev]["artifact_id"] == aid else None),
                           "why": (f"file {path} was edited by an applied change"
                                   if rev in published_by and published_by[rev]["artifact_id"] == aid
                                   else f"file {path} has new content ({(info[2] or 'trigger not recorded') if info else 'trigger not recorded'})"),
                           "note": "a source change is a different event from a knowledge decision"})
        last_hash = bh
    first_ts = min((e["ts"] for e in events if e["ts"]), default=None)
    for rid, srev, epoch, ts in kms:
        if first_ts is None or (ts and ts.isoformat() >= first_ts):
            events.append({"ts": ts.isoformat() if ts else None, "category": "knowledge", "kind": "knowledge_revision",
                           "actor": None, "knowledge_revision_id": rid, "source_revision_id": srev, "epoch": epoch,
                           "why": "knowledge accepted/published at this decision epoch (workspace-wide marker)"})
    events.sort(key=lambda e: (e["ts"] or "", e["kind"]))
    return {"node_id": node_id, "lineage_candidate_ids": sorted(ids), "path": path_by_art.get(target[1]),
            "lineage_key": {"artifact_id": key[0], "subject": key[1], "predicate": key[2], "locator": key[3] if with_evidence else None},
            "lineage_notes": notes, "events": events}


def previous_in_lineage(store, tenant: str, workspace_id: str, current_ids: set) -> dict:
    """For each current candidate id: the most recent EARLIER candidate of the same lineage (a different id), with its
    value and the active human decisions that were recorded on that earlier id. A lineage key shared by several
    candidates inside one extraction job is ambiguous and yields nothing (two rules are never merged)."""
    with store._tx() as cur:
        cur.execute("""SELECT j.job_id, j.execution_result, j.payload, j.pinned_source_revision_id,
                (SELECT max(a.recorded_at) FROM v2_job_attempts a WHERE a.tenant_id=j.tenant_id
                   AND a.workspace_id=j.workspace_id AND a.job_id=j.job_id) AS done_at
            FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction'
              AND j.execution_status='COMPLETED' ORDER BY done_at NULLS FIRST, j.job_id""", (tenant, workspace_id))
        jobs = cur.fetchall()
        cur.execute("""SELECT decision_id, actor_id, target, action, rationale, epoch FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND action IS NOT NULL
              AND superseded_by IS NULL AND revoked_by IS NULL""", (tenant, workspace_id))
        active = cur.fetchall()
    chains: dict = {}
    for job_id, raw_result, raw_payload, pinned, _done in jobs:
        aid = _obj(raw_payload).get("artifact_id")
        in_job: dict = {}
        for c in _obj(raw_result).get("candidates") or []:
            if isinstance(c, dict) and c.get("candidate_id"):
                in_job.setdefault(lineage_key(aid, c), []).append((job_id, c, c.get("source_revision_id") or pinned))
        for k, ms in in_job.items():
            chains.setdefault(k, []).append(ms)         # list of per-job member lists, oldest job first
    out = {}
    for k, per_job in chains.items():
        for i, ms in enumerate(per_job):
            if len(ms) != 1 or ms[0][1]["candidate_id"] not in current_ids or i == 0:
                continue
            prev = per_job[i - 1]
            if len(prev) != 1:
                continue
            pj, pc, prev_rev = prev[0]
            out[ms[0][1]["candidate_id"]] = {
                "candidate_id": pc["candidate_id"], "job_id": pj, "source_revision_id": prev_rev,
                "value": pc.get("value"), "unit": pc.get("unit"), "scope": pc.get("scope"),
                "decisions": [{"decision_id": d[0], "actor_id": d[1], "action": d[3], "rationale": d[4], "epoch": d[5]}
                              for d in active if d[2] == pc["candidate_id"]]}
    return out
