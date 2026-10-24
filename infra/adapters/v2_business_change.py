"""Business change intents (contract fkl/1): confirm a validated understanding of a change request. Writes one immutable request row, never a rule.

Under the workspace head lock: idempotent replay, the preview hash the person saw must equal the stored understanding, only a READY understanding can be confirmed, the pinned
snapshot and heads must still be current, and the confirming person must be the one who asked (`intent.confirm_own`) or hold `knowledge.confirm`, rechecked in the transaction.
"""
from __future__ import annotations

from psycopg2.extras import Json

from core.change_intelligence.v2.serialize import canonical_hash
from core.change_intelligence.v2.store_ports import PublishResult
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_postgres import StaleVersion, TenantScopeError, _as_object

OPERATION = "business.change_intent"
EVENT = "BUSINESS_CHANGE_INTENT_CONFIRMED"


def confirm_change_intent(store, tenant_id, workspace_id, *, actor_id, job_id, preview_hash) -> dict:
    try:
        rec = store.job_record(tenant_id, workspace_id, job_id)
    except TenantScopeError:
        raise BusinessKnowledgeRefused("unknown_reference", "no such change request in this workspace") from None
    result = rec.get("execution_result") if rec.get("execution_status") == "COMPLETED" else None
    if rec.get("kind") != "change.understand" or not result:
        raise BusinessKnowledgeRefused("not_ready", "the request has not been understood yet")
    if result["status"] != "READY" or result["questions"] or result["refused"] or result["uncovered_parts"]:
        raise BusinessKnowledgeRefused("not_ready", "the request is not fully understood; answer the open questions or rephrase it first")
    if preview_hash != result["preview_hash"]:
        raise BusinessKnowledgeRefused("preview_stale", "what you are confirming is not what was shown")
    pin = result["pin"]
    payload_hash = canonical_hash({"job": job_id, "preview": preview_hash, "actor": actor_id})
    # the requester confirms with `intent.confirm_own`; a person who may confirm knowledge (`knowledge.confirm`) confirms any change, their own included: the route
    # admits either, so the re-check under the lock accepts either too (live run SUR03-V2-A10: an admin's own change was refused "no longer holds" at publish)
    from infra.adapters.v2_business import current_permissions
    own = actor_id == rec.get("requested_by") and "intent.confirm_own" in current_permissions(store, tenant_id, workspace_id, actor_id)
    with store._tx() as cur:
        prior = store._replay(cur, tenant_id, workspace_id, OPERATION, f"intent-{job_id}", payload_hash)
        if prior is not None:
            return {"intent_id": job_id, "snapshot_id": pin["snapshot_id"], "operations": len(result["operations"]), "replayed": True}
        store._recheck_actor_authority(cur, tenant_id, workspace_id, actor_id, "intent.confirm_own" if own else "knowledge.confirm")   # AH-03: before the head lock
        head = store._lock_head(cur, tenant_id, workspace_id)
        prior = store._replay(cur, tenant_id, workspace_id, OPERATION, f"intent-{job_id}", payload_hash)
        if prior is not None:
            return {"intent_id": job_id, "snapshot_id": pin["snapshot_id"], "operations": len(result["operations"]), "replayed": True}
        cur.execute("SELECT accepted_snapshot_id, business_revision FROM v2_business_heads WHERE tenant_id=%s AND workspace_id=%s", (tenant_id, workspace_id))
        row = cur.fetchone() or (None, 0)
        if (row[0] != pin["snapshot_id"] or row[1] != pin["business_revision"] or head.source_revision_id != pin["source_revision_id"]
                or head.knowledge_revision_id != pin["km_revision_id"] or head.decision_epoch != pin["decision_epoch"]):
            raise StaleVersion("stale_snapshot: the business knowledge moved after this request was understood")
        content_hash = canonical_hash({"operations": result["operations"], "write_scope": result["write_scope"], "pin": pin})
        cur.execute("""INSERT INTO v2_business_change_intents (tenant_id, workspace_id, intent_id, snapshot_id, business_revision, request_text, operations, write_scope,
                read_scope, content_hash, confirmed_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, job_id, pin["snapshot_id"], pin["business_revision"], result["request"], Json(result["operations"]), Json(result["write_scope"]),
                     Json(result["read_scope"]), content_hash, actor_id))
        version = head.version + 1
        cur.execute("UPDATE v2_workspaces SET version=%s WHERE tenant_id=%s AND workspace_id=%s", (version, tenant_id, workspace_id))
        store._record_commit(cur, tenant_id, workspace_id, OPERATION, f"intent-{job_id}", payload_hash, PublishResult(version, job_id), EVENT,
                             {"intent_id": job_id, "snapshot_id": pin["snapshot_id"], "operations": len(result["operations"]), "confirmed_by": actor_id})
    return {"intent_id": job_id, "snapshot_id": pin["snapshot_id"], "operations": len(result["operations"]), "replayed": False}


def read_intent(store, tenant_id, workspace_id, intent_id):
    with store._tx() as cur:
        cur.execute("""SELECT snapshot_id, business_revision, request_text, operations, write_scope, read_scope, content_hash, confirmed_by FROM v2_business_change_intents
            WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s""", (tenant_id, workspace_id, intent_id))
        row = cur.fetchone()
    if row is None:
        return None
    return {"intent_id": intent_id, "snapshot_id": row[0], "business_revision": row[1], "request": row[2], "operations": _as_object(row[3]), "write_scope": _as_object(row[4]),
            "read_scope": _as_object(row[5]), "content_hash": row[6], "confirmed_by": row[7]}


def pinned_for_intent(store, tenant_id, workspace_id, intent_id):
    """(intent, pinned accepted snapshot, artifact ids of its source revision, pin) of a CONFIRMED intent, or None. Reads rows only."""
    from infra.adapters import v2_business
    intent = read_intent(store, tenant_id, workspace_id, intent_id)
    if intent is None:
        return None
    with store._tx() as cur:
        snap = v2_business._accepted_by_revision(cur, tenant_id, workspace_id, intent["business_revision"])
        if snap is None:
            raise BusinessKnowledgeRefused("unknown_reference", "the knowledge this request was confirmed against is not available")
        cur.execute("SELECT artifact_id FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s",
                    (tenant_id, workspace_id, snap["snapshot"]["source_revision_id"]))
        artifacts = [r[0] for r in cur.fetchall()]
    meta = snap["snapshot"]
    pin = {"snapshot_id": meta["snapshot_id"], "business_revision": meta["business_revision"], "source_revision_id": meta["source_revision_id"],
           "km_revision_id": meta["km_revision_id"], "decision_epoch": meta["decision_epoch"]}
    return intent, snap, artifacts, pin


def impact_for_intent(store, tenant_id, workspace_id, intent_id):
    """The impact report of a CONFIRMED intent, computed from its pinned snapshot (pure and deterministic: no model, nothing stored, nothing run). None if not confirmed."""
    from core.change_intelligence.v2 import business_impact
    got = pinned_for_intent(store, tenant_id, workspace_id, intent_id)
    if got is None:
        return None
    intent, snap, artifacts, pin = got
    rules = {r["rule_id"]: r for r in snap["rules"]}
    report = business_impact.build_report(intent["operations"], rules, snap["relations"], pin=pin, known_artifacts=artifacts)
    return {"intent": intent, "report": report, "titles": {rid: r["title"] for rid, r in rules.items()}, "rules": rules}


def draft_for_intent(store, tenant_id, workspace_id, intent_id, *, proposed_dependencies=None, dependency_state=None):
    """The server-built structured draft of a CONFIRMED intent (contract CHG-03): rule before/after, relation updates, file actions resolved against the observations of the
    PINNED source revision, per-operation outcomes, carried coverage/unknowns/counterevidence. Pure and deterministic over stored rows; nothing stored, nothing run, no model.
    `proposed_dependencies`/`dependency_state` come from the completed `change.impact_deps` job when the caller has one. None if the intent is not confirmed."""
    from core.change_intelligence.v2 import business_draft, business_impact
    got = pinned_for_intent(store, tenant_id, workspace_id, intent_id)
    if got is None:
        return None
    intent, snap, artifact_ids, pin = got
    rules = {r["rule_id"]: r for r in snap["rules"]}
    report = business_impact.build_report(intent["operations"], rules, snap["relations"], pin=pin, known_artifacts=artifact_ids)
    try:
        view = store.source_revision_view(tenant_id, workspace_id, pin["source_revision_id"])
        observations = store.source_observations(tenant_id, workspace_id, pin["source_revision_id"])
    except TenantScopeError:
        view, observations = {"artifacts": []}, []                      # the pinned revision is not readable: every file stays UNRESOLVED, never "no impact"
    artifacts = {a["artifact_id"]: a.get("logical_path") or a["artifact_id"] for a in view["artifacts"]}
    draft = business_draft.build_draft(intent, report, rules, snap["relations"], pin=pin, artifacts=artifacts, observations=observations,
                                       proposed_dependencies=proposed_dependencies, dependency_state=dependency_state)
    return {"intent": intent, "report": report, "draft": draft, "titles": {rid: r["title"] for rid, r in rules.items()}}
