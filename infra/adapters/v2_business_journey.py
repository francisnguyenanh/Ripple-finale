"""SYN-04: the facts behind the per-axis journey of one business change, read from stored rows only (nothing written, no model), then the pure read model."""
from __future__ import annotations

from core.change_intelligence.v2 import business_journey as bj
from core.change_intelligence.v2 import code_recommendation
from infra.adapters import v2_business, v2_business_review
from infra.adapters.v2_policy import permissions_for


def _understanding(rec) -> dict:
    st = rec.get("execution_status")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELLED": "cancelled"}.get(st) or ("waiting_for_approval" if rec.get("status") == "PENDING_APPROVAL" else "running")
    res = rec.get("execution_result") if st == "COMPLETED" else None
    ready = bool(res and res.get("status") == "READY")
    can_confirm = bool(res and ready and not res.get("questions") and not res.get("refused") and not res.get("uncovered_parts"))
    return {"status": status, "ready": ready, "can_confirm": can_confirm, "clarify": bool(res and (res.get("status") == "CLARIFY" or res.get("questions"))), "reason": rec.get("execution_reason") if st == "FAILED" else None}


def journey_view(store, tenant_id, workspace_id, change_id, *, account_id: str, roles) -> dict:
    rec = store.job_record(tenant_id, workspace_id, change_id)
    perms = permissions_for(roles)
    facts: dict = {"understanding": _understanding(rec), "confirmed": False}
    view = v2_business_review.review_view(store, tenant_id, workspace_id, change_id, account_id=account_id, roles=roles)
    labels = {}
    if view is not None:
        can = view["can"]
        facts["confirmed"] = True
        facts["knowledge_decision"] = {"action": can["knowledge_decision"]["action"], "by": can["knowledge_decision"]["actor_id"]} if can.get("knowledge_decision") else None
        facts["file_writes_decision"] = {"action": can["file_writes_decision"]["action"], "by": can["file_writes_decision"]["actor_id"]} if can.get("file_writes_decision") else None
        facts["knowledge_moved"] = view["knowledge_moved"]
        facts["committed"] = view["committed"]
        facts["completion"] = view["completion"]
        src = view["source"]
        facts["source"] = {"automatic_edit_available": src["automatic_edit_available"], "file_writes_approved": src["file_writes_approved"], "job": src["job"], "manual_count": len(src["manual"])}
        draft, intent = view["draft"], view["intent"]
        head = store.head(tenant_id, workspace_id)
        bview = v2_business.business_view(store, tenant_id, workspace_id)
        accepted = bview["accepted"]
        current = v2_business._working(bview)
        rec_code = code_recommendation.build_recommendations(draft, intent["operations"], head={"source_revision_id": head.source_revision_id, "snapshot_id": current["snapshot"]["snapshot_id"] if current else None})
        facts["code"] = {s.lower(): rec_code["counts"].get(s, 0) for s in code_recommendation.STATUSES}
        statuses = {}
        if accepted:
            rules = {r["rule_id"]: r for r in accepted["rules"]}
            for rid in intent["write_scope"]:
                t = rules.get(rid + "-target") or (rules.get(rid) if rid.endswith("-target") else None)
                if t is not None:
                    statuses[t["rule_id"]] = t["implementation_status"]
                    obs = rules.get(rid[:-7] if rid.endswith("-target") else rid)
                    labels[t["rule_id"]] = {"accepted_target": (t.get("condition") or {}).get("value"), "observed_current": (obs.get("condition") or {}).get("value") if obs else None}
        pending = bview.get("pending")
        facts["readback"] = {"target_statuses": statuses, "pending_snapshot": bool(pending and (pending["snapshot"].get("coverage") or {}).get("origin") == "rebase")}
        facts["documents"] = {"freshness": v2_business.knowledge_status(store, tenant_id, workspace_id)["documents"]["freshness"]}
    out = bj.build_journey({**facts, "labels": labels if "evidence.read" in perms else {}})
    out["change_id"] = change_id
    return out
