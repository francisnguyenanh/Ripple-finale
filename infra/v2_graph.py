"""J1: read-only graph projection of the CURRENT workspace state (files -> evidence -> rules -> findings/decisions).

A projection over rows the store already holds (extraction/relation/finding job results, decisions); it is not a
graph database and persists nothing. Relation edges are stored PROPOSED identity edges and are marked
`projection: true`; they are never causal dependencies. Anything not covered (no extraction, not COMPLETED,
omitted comparisons, stale revision, no evidence) is disclosed in `coverage`, never silently dropped.
"""

from __future__ import annotations

import json

MAX_NODES = 1500
_KIND_STATE = {"CONFLICT": "conflict", "scope_mismatch": "scope-mismatch", "GAP": "gap"}


def _obj(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _label(c: dict) -> str:
    value = c.get("value")
    unit = f" {c['unit']}" if c.get("unit") else ""
    return f"{c.get('subject')} / {c.get('predicate')} = {value}{unit}"


def build_graph(store, tenant: str, workspace_id: str, *, with_evidence: bool) -> dict:
    head = store.head(tenant, workspace_id)
    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: set[str] = set()

    def add_node(node: dict) -> None:
        if node["id"] not in node_ids:
            node_ids.add(node["id"])
            nodes.append(node)

    artifacts = []
    if head.source_revision_id:
        artifacts = store.source_revision_view(tenant, workspace_id, head.source_revision_id)["artifacts"]

    with store._tx() as cur:
        cur.execute("""SELECT decision_id, epoch, actor_id, target, action, provenance, scope,
                superseded_by, revoked_by FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND action IS NOT NULL ORDER BY epoch""",
                    (tenant, workspace_id))
        decision_rows = cur.fetchall()
        cur.execute("""SELECT job_id, execution_status, execution_result, payload FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='relation' ORDER BY job_id""",
                    (tenant, workspace_id))
        relation_rows = cur.fetchall()

    decisions = [{"decision_id": r[0], "epoch": r[1], "actor_id": r[2], "target": r[3], "action": r[4],
                  "provenance": r[5], "scope": _obj(r[6]), "active": r[7] is None and r[8] is None}
                 for r in decision_rows]
    confirmed = {d["target"]: d for d in decisions if d["active"] and d["action"] == "confirm"}
    resolved = {d["target"]: d for d in decisions if d["active"] and d["action"] == "resolve"}

    artifact_cov, all_candidates, findings, seen_f = [], {}, [], set()
    for art in artifacts:
        aid = art["artifact_id"]
        draft = store.knowledge_draft(tenant, workspace_id, aid)
        cands = [c for c in draft["candidates"] if isinstance(c, dict)]
        status = draft["status"] if draft["execution_status"] == "COMPLETED" else (
            "NO_EXTRACTION" if draft["extraction_job_id"] is None else f"EXTRACTION_{draft['execution_status']}")
        artifact_cov.append({"artifact_id": aid, "logical_path": art["logical_path"], "extraction": status,
                             "extraction_job_id": draft["extraction_job_id"], "candidates": len(cands)})
        add_node({"id": aid, "type": "artifact", "label": art["logical_path"],
                  "state": "observed" if status == "PENDING_HUMAN" else "not-analysed",
                  "provenance": "parser", "source_revision_id": head.source_revision_id,
                  "detail": {"media_type": art.get("media_type"), "extraction": status,
                             "extraction_job_id": draft["extraction_job_id"], "candidates": len(cands)}})
        for c in cands:
            all_candidates[c["candidate_id"]] = (aid, draft["extraction_job_id"], c)
        for f in draft["findings"]:
            if isinstance(f, dict) and f.get("finding_id") not in seen_f:
                seen_f.add(f.get("finding_id"))
                findings.append(f)

    finding_state = {}
    for f in findings:
        d = resolved.get(f["finding_id"])
        state = _KIND_STATE.get(f.get("kind"), "ambiguity")
        finding_state[f["finding_id"]] = "authority-decided" if d else state
    in_open_finding = {}
    for f in findings:
        if f["finding_id"] not in resolved:
            for cid in f.get("involved_claim_ids") or []:
                in_open_finding.setdefault(cid, finding_state[f["finding_id"]])

    from infra.v2_timeline import previous_in_lineage
    previous = previous_in_lineage(store, tenant, workspace_id, set(all_candidates))
    orphan = stale = updated = unchanged_older = 0
    # A rule read at an EARLIER revision is out of date only if its file changed since; when the file's bytes are identical at the head
    # the output is still valid (it is only "read at an earlier revision"). Unknown hash -> treated as changed (never optimistic).
    head_hash = {a["artifact_id"]: a.get("blob_hash") for a in artifacts}
    older_revs = {c.get("source_revision_id") for _a, _j, c in all_candidates.values()
                  if c.get("source_revision_id") and c.get("source_revision_id") != head.source_revision_id}
    hash_at: dict = {}
    if older_revs:
        with store._tx() as cur:
            cur.execute("""SELECT source_revision_id, artifact_id, blob_hash FROM v2_artifact_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id = ANY(%s)""",
                        (tenant, workspace_id, sorted(older_revs)))
            hash_at = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    # Stored provenance only (never inferred from time or from a revision's name): which apply published a revision and which file it edited,
    # and which extraction jobs were scheduled BY an apply.
    from infra.v2_timeline import apply_provenance
    published_by, caused_by = apply_provenance(store, tenant, workspace_id, {j for _a, j, _c in all_candidates.values() if j})
    for cid, (aid, job_id, c) in all_candidates.items():
        rev_c = c.get("source_revision_id")
        is_older = bool(rev_c) and rev_c != head.source_revision_id
        file_unchanged = is_older and bool(head_hash.get(aid)) and hash_at.get((rev_c, aid)) == head_hash.get(aid)
        is_stale = is_older and not file_unchanged
        stale += is_stale
        unchanged_older += file_unchanged
        evidence = [e for e in (c.get("evidence") or []) if isinstance(e, dict)]
        orphan += not evidence
        prev = previous.get(cid)
        changed = bool(prev) and (prev["value"], prev["unit"], prev["scope"]) != (c.get("value"), c.get("unit"), c.get("scope"))
        edited = published_by.get(rev_c)
        history = {"update_kind": (None if not changed else "value_changed" if (prev["value"], prev["unit"]) != (c.get("value"), c.get("unit"))
                                   else "scope_only"),
                   "source_edited_by_apply": (edited["apply_job_id"] if edited and edited["artifact_id"] == aid else None),
                   "read_again_after_apply": caused_by.get(job_id),
                   "file_unchanged_since_read": file_unchanged}
        if is_stale:
            state = "stale"
        elif changed and cid not in confirmed and cid not in in_open_finding:
            state = "updated"
            updated += 1
        elif cid in confirmed:
            state = "confirmed"
        elif cid in in_open_finding:
            state = in_open_finding[cid]
        else:
            state = "pending-confirmation"
        add_node({"id": cid, "type": "candidate", "label": _label(c), "state": state, "provenance": "model",
                  "source_revision_id": c.get("source_revision_id"), "stale": is_stale,
                  "read_at_earlier_revision": is_older, "file_unchanged_since_read": file_unchanged,
                  "detail": {k: c.get(k) for k in ("subject", "predicate", "value", "unit", "modality", "scope",
                                                   "outcome", "applicability", "applicability_reason")}
                  | {"extraction_job_id": job_id, "logical_path": next(
                      (a["logical_path"] for a in artifacts if a["artifact_id"] == aid), None),
                     "has_evidence": bool(evidence), "evidence_redacted": not with_evidence}
                  | {"history": history}
                  | ({"previous": prev, "update_reason": (
                      "value/unit/scope read differently than in the earlier extraction of the same rule "
                      f"(was {prev['value']} at source revision {prev['source_revision_id']}); the earlier "
                      "human decisions were recorded on the earlier candidate id and are NOT carried over")
                      if changed else "re-extracted with the same value; earlier decisions are not carried over"}
                     if prev else {})})
        edges.append({"id": f"src-{aid}-{cid}", "type": "source-of", "from": aid, "to": cid, "state": "observed",
                      "reason": "extracted from this file", "job_id": job_id, "projection": False})
        if with_evidence:
            for i, e in enumerate(evidence):
                eid = f"ev-{cid}-{i}"
                add_node({"id": eid, "type": "evidence", "label": str(e.get("locator")), "state": "observed",
                          "provenance": "parser", "detail": {"locator": e.get("locator"), "quote": e.get("quote")}})
                edges.append({"id": f"evof-{eid}", "type": "evidence-of", "from": eid, "to": cid,
                              "state": "observed", "reason": "quoted source text", "projection": False})
        if cid in confirmed:
            d = confirmed[cid]
            add_node({"id": d["decision_id"], "type": "decision", "label": f"confirm by {d['actor_id']}",
                      "state": "confirmed", "provenance": "human",
                      "detail": {"epoch": d["epoch"], "actor_id": d["actor_id"], "action": "confirm"}})
            edges.append({"id": f"dec-{d['decision_id']}-{cid}", "type": "decided-by", "from": d["decision_id"],
                          "to": cid, "state": "confirmed", "reason": "human confirmation (source file unchanged)",
                          "projection": False})

    for f in findings:
        fid = f["finding_id"]
        add_node({"id": fid, "type": "finding", "label": f"{f.get('kind')}: {f.get('reason') or ''}"[:160],
                  "state": finding_state[fid], "provenance": "model",
                  "detail": {"kind": f.get("kind"), "severity": f.get("severity"), "reason": f.get("reason"),
                             "authority_required": f.get("authority_required"),
                             "lifecycle_state": f.get("lifecycle_state")}})
        for cid in f.get("involved_claim_ids") or []:
            if cid in node_ids:
                edges.append({"id": f"inv-{fid}-{cid}", "type": "involved-in", "from": cid, "to": fid,
                              "state": finding_state[fid], "reason": f.get("kind"), "projection": False})
        d = resolved.get(fid)
        if d:
            add_node({"id": d["decision_id"], "type": "decision", "label": f"resolve by {d['actor_id']}",
                      "state": "authority-decided", "provenance": "human",
                      "detail": {"epoch": d["epoch"], "actor_id": d["actor_id"], "action": "resolve",
                                 "authoritative_claim_id": d["scope"].get("authoritative_claim_id")}})
            edges.append({"id": f"dec-{d['decision_id']}-{fid}", "type": "decided-by", "from": d["decision_id"],
                          "to": fid, "state": "authority-decided",
                          "reason": "authority chosen; the opposing source still exists unchanged",
                          "projection": False})

    omitted = identity_omitted = compared_not_same = 0
    seen_rel = set()
    rel_jobs, rel_covered = [], set()
    for job_id, status, raw_result, raw_payload in relation_rows:
        if status != "COMPLETED" or not (set(_obj(raw_payload).get("candidate_ids") or []) & set(all_candidates)):
            continue
        result = _obj(raw_result)
        rel_jobs.append(job_id)
        rel_covered |= set(_obj(raw_payload).get("candidate_ids") or []) & set(all_candidates)
        omitted += int(result.get("omitted_comparisons") or 0)
        identity_omitted += int(result.get("omitted_identity_comparisons") or 0)
        for e in result.get("proposed_edges") or []:
            rid = e.get("relation_id")
            if rid in seen_rel:
                continue
            seen_rel.add(rid)
            if e.get("relation_type") != "SAME_RULE":
                compared_not_same += 1
                continue
            a, b = e.get("from_candidate_id"), e.get("to_candidate_id")
            if a in node_ids and b in node_ids:
                edges.append({"id": rid, "type": "relation", "relation_type": "SAME_RULE", "from": a, "to": b,
                              "state": "proposed", "reason": e.get("reason"), "job_id": job_id,
                              "projection": True})

    truncated = len(nodes) > MAX_NODES
    if truncated:
        nodes = nodes[:MAX_NODES]
        keep = {n["id"] for n in nodes}
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    return {"workspace_id": workspace_id,
            "head": {"source_revision_id": head.source_revision_id,
                     "knowledge_revision_id": head.knowledge_revision_id, "decision_epoch": head.decision_epoch},
            "nodes": nodes, "edges": edges,
            "coverage": {"artifacts": artifact_cov, "stale_candidates": stale, "unchanged_since_read_candidates": unchanged_older, "candidates_without_evidence": orphan,
                         "relation_omitted_comparisons": omitted, "relation_omitted_identity_comparisons": identity_omitted,
                         "relation_compared_not_same_rule": compared_not_same,
                         "updated_candidates": updated, "relation_job_ids": rel_jobs, "relation_covered_candidate_ids": sorted(rel_covered), "truncated": truncated,
                         "evidence_redacted": not with_evidence,
                         "note": "relation edges are proposed identity edges, not causal dependencies; "
                                 "missing relation coverage means UNKNOWN, not unaffected"}}


def impact_context(store, tenant: str, workspace_id: str, target_candidate_id: str) -> dict | None:
    """J3: everything the impact executor needs, taken from ONE snapshot (the same one the graph shows), so the browser
    never assembles candidate/relation/finding ids by hand. The ids are references only: the executor re-resolves them
    tenant/workspace-scoped and derives every conclusion itself. Candidates with no relation evidence are listed in
    `uncovered_candidate_ids`; the executor reports them UNKNOWN (never 'unaffected')."""
    g = build_graph(store, tenant, workspace_id, with_evidence=False)
    current = [n for n in g["nodes"] if n["type"] == "candidate" and not n.get("stale")]
    ids = [n["id"] for n in current]
    if target_candidate_id not in ids:
        return None
    cov = g["coverage"]
    covered = set(cov["relation_covered_candidate_ids"])
    return {"workspace_id": workspace_id, "target_candidate_id": target_candidate_id,
            "candidate_ids": ids, "relation_job_ids": cov["relation_job_ids"],
            "existing_finding_ids": [n["id"] for n in g["nodes"] if n["type"] == "finding"
                                     and n["state"] != "authority-decided"],
            "uncovered_candidate_ids": [i for i in ids if i not in covered],
            "context_truncated": bool(cov["truncated"]),
            "expected_decision_epoch": g["head"]["decision_epoch"], "head": g["head"],
            "candidates": {n["id"]: {"label": n["label"], "logical_path": n["detail"].get("logical_path"),
                                     "state": n["state"]} for n in current}}
