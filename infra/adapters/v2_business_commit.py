"""Commit of an APPROVED business change into the accepted knowledge (contract fkl/1, CHG-05).

What is committed is the draft the approver saw (CHG-03/CHG-04): for every changed rule the after state becomes the TARGET rule `<rule_id>-target` (authority TARGET, status
CONFIRMED, implementation_status `pending`, provenance = earlier human decisions + this change decision), exactly as an answered gap does (`business_answers`). The OBSERVED rule
stays what the sources say, so the difference between the decided meaning and the files/code is visible until the sources are changed (SYN-02/SYN-03). Relations and gaps are
re-derived with the confirmed ones carried (`_merge_state`). The snapshot, its rules, relations, gaps, the change decision, the business head, the audit row and the outbox marker
are written by `commit_snapshot` in ONE transaction (compare-and-set on the workspace version and the business revision; the actor's `knowledge.confirm` is rechecked inside).
Refused with zero writes when: the request is not confirmed, the hashes sent differ from the current draft, there is no CURRENT knowledge approval, the knowledge moved after
confirmation, a semantic blocker is open, or a decided rule already exists for a changed observed rule (the change must then be stated against the decided rule).
Nothing here touches a source file, applies an edit, runs code or creates a document job; `pending_implementation` lists what people still have to do.
"""
from __future__ import annotations

import copy

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters import v2_business, v2_business_review
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_policy import PermissionDenied, permissions_for
from infra.adapters.v2_postgres import StaleVersion, TenantScopeError, _as_object

DECISION_KIND = "business_change"
AXES = ("knowledge_result", "source_sync", "code_sync", "behavioral_verification")


def change_decision_id(change_id: str, semantic_diff_hash: str) -> str:
    return "change-" + canonical_hash({"intent": change_id, "semantic": semantic_diff_hash})[:16]


def pending_implementation(draft) -> dict:
    """What people still have to do after the knowledge commit, per axis and per file action; nothing here is done by the product."""
    items = [i for i in draft["file_actions"] if i["role"] == "to_change"]
    code = [i for i in items if i["kind"] == "code"]
    other = [i for i in items if i["kind"] != "code"]
    def axis(group):
        if not group:
            return "not_required"
        if all(i["action"] == "NO_OP" for i in group):
            return "verified_unchanged"
        return "pending"
    return {"source_sync": axis(other), "code_sync": axis(code), "behavioral_verification": "not_tested",
            "counts": {a: sum(1 for i in items if i["action"] == a) for a in bd.ACTIONS},
            "items": [{"ref": i["ref"], "path": i["path"], "locator": i["locator"], "kind": i["kind"], "action": i["action"], "reason": i["reason"], "rule_ids": i["rule_ids"]} for i in items],
            "unresolved": [{"ref": u["ref"], "kind": u["kind"], "rule_id": u.get("rule_id")} for u in draft["unresolved"]],
            "statement": "The decided meaning is in the knowledge; the files and code still show what they showed. Automatic edits need their own approval and apply; manual items need a person."}


def committed_state(store, tenant_id, workspace_id, change_id) -> dict | None:
    """The change decision row and snapshot of an earlier commit of this request, if any (reads only)."""
    with v2_business.read_snapshot(store) as cur:
        cur.execute("""SELECT d.decision_id, d.snapshot_id, d.actor_id, d.quote, d.body, s.business_revision, s.created_at FROM v2_business_decisions d
            JOIN v2_business_snapshots s ON s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id AND s.snapshot_id=d.snapshot_id
            WHERE d.tenant_id=%s AND d.workspace_id=%s AND d.kind=%s AND d.body->>'intent_id'=%s ORDER BY s.business_revision DESC LIMIT 1""",
                    (tenant_id, workspace_id, DECISION_KIND, change_id))
        row = cur.fetchone()
    if row is None:
        return None
    body = _as_object(row[4]) or {}
    return {"decision_id": row[0], "snapshot_id": row[1], "committed_by": row[2], "reason": row[3], "business_revision": row[5], "created_at": row[6].isoformat(),
            "semantic_diff_hash": body.get("semantic_diff_hash"), "approve_decision_id": body.get("approve_decision_id"), "pending_implementation": body.get("pending_implementation"),
            "target_rule_ids": body.get("target_rule_ids")}


def completion(draft, committed, source_apply=None) -> dict:
    """The four separate axes of a business change. Only `knowledge_result` is decided here; the others are shown as pending until their own tasks prove them.
    `source_apply` (SYN-03) is the automatic edit job view, if one exists: a COMPLETED job makes `source_sync` `applied_pending_readback` (bytes published, not yet
    re-observed and accepted); it never makes it `verified` and never touches `behavioral_verification`."""
    pend = (committed or {}).get("pending_implementation") or pending_implementation(draft)
    source_sync = pend["source_sync"]
    if source_apply is not None and source_apply.get("status") == "COMPLETED" and source_sync == "pending":
        source_sync = "applied_pending_readback"
    return {"knowledge_result": "committed" if committed else "not_committed", "source_sync": source_sync, "code_sync": pend["code_sync"],
            "behavioral_verification": pend["behavioral_verification"], "knowledge_only_complete": bool(committed),
            "statement": ("The knowledge change is committed and is the decided meaning from now on. " if committed else "The knowledge change is not committed yet. ")
                         + "Source and code sync are separate: they stay pending until the files are changed and read back; behaviour is not tested by this product."}


def commit_change(store, tenant_id, workspace_id, change_id, *, actor_id: str, roles, seen: dict) -> dict:
    perms = permissions_for(roles)
    if "knowledge.confirm" not in perms:
        raise PermissionDenied("knowledge.confirm")
    rec = store.job_record(tenant_id, workspace_id, change_id)
    if rec.get("kind") != "change.understand":
        raise TenantScopeError("change request not found")
    view = v2_business_review.review_view(store, tenant_id, workspace_id, change_id, account_id=actor_id, roles=roles)
    if view is None:
        raise BusinessKnowledgeRefused("not_confirmed", "the request is not confirmed")
    draft = view["draft"]
    if not isinstance(seen, dict) or seen.get("semantic_diff_hash") != draft["semantic_diff_hash"]:
        raise BusinessKnowledgeRefused("diff_mismatch", "the change you are committing is not the change as it is now; look at it again")
    if seen.get("draft_hash") != draft["draft_hash"]:
        raise BusinessKnowledgeRefused("draft_revised", "the draft was revised since you looked at it; look at the current version again")
    already = committed_state(store, tenant_id, workspace_id, change_id)
    if already is not None:
        return {"replayed": True, "snapshot_id": already["snapshot_id"], "business_revision": already["business_revision"], "decision_id": already["decision_id"],
                "completion": completion(draft, already), "pending_implementation": already["pending_implementation"]}
    approve = view["can"]["knowledge_decision"]
    if approve is None or approve["action"] != "approve":
        raise BusinessKnowledgeRefused("not_approved", "the knowledge change has no current approval for this version of the draft")
    if view["knowledge_moved"]:
        raise StaleVersion("stale_snapshot: the business knowledge moved after this request was confirmed")
    blockers = bd.semantic_blockers(draft, acknowledged_flags=bool((approve.get("binding") or {}).get("acknowledged_flags")))
    if blockers or draft["status"] == "INVALID":
        raise BusinessKnowledgeRefused("open_blockers", "; ".join(b["text"] for b in blockers[:6]) or "the changed rule is not valid")
    head = store.head(tenant_id, workspace_id)
    bview = v2_business.business_view(store, tenant_id, workspace_id)
    base = bview["accepted"]
    if base is None or base["snapshot"]["snapshot_id"] != draft["pin"]["snapshot_id"]:
        raise StaleVersion("stale_snapshot: the business knowledge moved after this request was confirmed")
    rules = {r["rule_id"]: copy.deepcopy(r) for r in base["rules"]}
    decision_id = change_decision_id(change_id, draft["semantic_diff_hash"])
    targets = []
    for rc in draft["rule_changes"]:
        rid = rc["rule_id"]
        after = copy.deepcopy(rc["after"])
        if rid.endswith("-target"):
            tid = rid
        else:
            tid = rid + "-target"
            if tid in rules:
                raise BusinessKnowledgeRefused("target_exists", f"“{rc['title']}” already has a decided version; state the change against the decided rule")
        after.update(rule_id=tid, authority="TARGET", status="CONFIRMED", implementation_status="pending")
        after["provenance"] = [p for p in after.get("provenance") or [] if p.get("kind") == "human_decision"] + [{"kind": "human_decision", "decision_id": decision_id}]
        changed_fields = {d["field"] for d in rc["diff"]}                                    # a field the change sets is no longer unknown
        after["unknown_fields"] = [u for u in after.get("unknown_fields", []) if u not in changed_fields]
        bk.validate_business_rule(after)
        rules[tid] = after
        targets.append(tid)
    rule_list = [rules[k] for k in sorted(rules)]
    rel_list, gap_list = v2_business._merge_state(rule_list, base["relations"], base["gaps"])
    pend = pending_implementation(draft)
    decision = {"decision_id": decision_id, "kind": DECISION_KIND, "quote": approve.get("reason") or "approved",
                "body": {"intent_id": change_id, "approve_decision_id": approve["decision_id"], "approved_by": approve["actor_id"], "semantic_diff_hash": draft["semantic_diff_hash"],
                         "draft_hash": draft["draft_hash"], "report_hash": draft["report_hash"], "operations": [o["op_id"] for o in draft["operations"]], "write_scope": draft["write_scope"],
                         "target_rule_ids": targets, "relation_updates": [{k: r[k] for k in ("edge_id", "type", "action")} for r in draft["relation_updates"]],
                         "pending_implementation": pend}}
    snapshot_id = "change-" + canonical_hash({"intent": change_id, "semantic": draft["semantic_diff_hash"], "decision": decision_id})[:20]
    revision = bview["business_head"]["business_revision"]
    coverage = {"origin": "business_change", "from_snapshot_id": base["snapshot"]["snapshot_id"], "intent_id": change_id, "approve_decision_id": approve["decision_id"],
                "semantic_diff_hash": draft["semantic_diff_hash"], **v2_business._gap_counts(gap_list), "partial": bool(any(g["status"] in ("OPEN", "REOPENED") for g in gap_list))}
    result = v2_business.commit_snapshot(store, tenant_id, workspace_id, snapshot_id=snapshot_id, actor_id=actor_id, lifecycle_state="ACCEPTED", rules=rule_list, relations=rel_list,
                                         gaps=gap_list, decisions=[decision], expected_version=head.version, expected_business_revision=revision,
                                         source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                                         idempotency_key=f"change-{snapshot_id}", coverage=coverage)
    committed = {"decision_id": decision_id, "snapshot_id": snapshot_id, "business_revision": revision + 1, "pending_implementation": pend}
    return {"replayed": result.replayed, "snapshot_id": snapshot_id, "business_revision": revision + 1, "decision_id": decision_id, "target_rule_ids": targets,
            "completion": completion(draft, committed), "pending_implementation": pend}
