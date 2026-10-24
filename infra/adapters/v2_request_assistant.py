"""Request assistant: build the context pack of ONE request for the asking person (RQA-01; contract docs/contracts/REQUEST_TASKS_AND_ASSISTANT.md section 4).

Visibility comes first: every fact is taken from a read service executed AS the asking person (the task view, which applies the family's read rule; the business-change review
shaping in Business mode with the reader's evidence right; the intent and review notes that `project.read` already shows). Never from a privileged whole-row read, never from
the creation chat (not even the reader's own: v1 keeps it out), audit payloads, raw job records, other requests or the workspace's whole knowledge. Account ids are not passed:
people appear as their role in the workflow. A reader who may not read the request gets the generic "request not found" before any fact is collected. No model call here.
"""
from __future__ import annotations

import json

from core.change_intelligence.v2 import request_assistant as ra
from infra.adapters import v2_request_tasks
from infra.adapters.v2_policy import permissions_for
from infra.adapters.v2_postgres import TenantScopeError


IMPACT_LIMIT = 12                                                                        # upstream cap of the impact list (recorded in the pack's coverage)


def _intent_facts(store, tenant, ws, tv, authority, upstream: dict) -> list:
    from infra import v2_work_routes as w
    out = []
    iid = tv["request_ref"]["id"]
    if not iid.startswith("intent-"):
        return out                                                                       # a conversation-only request: the conversation itself is never context
    intent = store.get_intent(tenant, ws, iid)
    form = intent.get("structured_form") or {}
    omitted = 0
    for k in sorted(form):                                                               # one fact per field, whole JSON values only: a value is never cut mid-object (AIG-02)
        text = f"Structured request, {k}: " + json.dumps(form[k], sort_keys=True, ensure_ascii=False)
        if len(" ".join(text.split())) > ra.MAX_FACT_CHARS:
            omitted += 1
            continue
        out.append(ra.fact("request", text, "intent", key=f"request:form:{k}"))
    if omitted:
        upstream["form_fields_omitted"] = omitted
    pid = (tv.get("coverage") or {}).get("proposal_job_id")
    if pid:
        digest = w.review_digest(store, tenant, ws, pid, newest=ra.MAX_FACTS)              # AH-02: at most as many notes as one pack can hold, read in SQL order
        notes = sorted(digest["notes"], key=lambda n: (n["created_at"], str(n["note_id"])), reverse=True)   # newest first, by stored identity
        if digest["total"] > len(digest["notes"]):
            upstream["review_notes_omitted"] = digest["total"] - len(digest["notes"])        # counted, never silent
        for n in notes:                                                                  # the notes route shows these to project.read; the reviewer stays "a reviewer"
            out.append(ra.fact("review_note", f"A reviewer's note ({n['verdict']}): {n['text']}" if n["text"] else f"A reviewer's note: {n['verdict']}", "review_note",
                               tier="history", key=f"review_note:{n['note_id']}"))
    return out


def _business_facts(store, tenant, ws, rid, authority, upstream: dict) -> list:
    from infra.adapters import v2_business_review
    from infra.v2_business_routes import _review_payload
    view = v2_business_review.review_view(store, tenant, ws, rid, account_id=authority["account_id"], roles=authority.get("roles"))
    if view is None:
        return []
    can_evidence = "evidence.read" in permissions_for(authority.get("roles"))
    p = _review_payload(view, mode="business", can_evidence=can_evidence, change_id=rid)      # the review page's own shaping for THIS reader (no ids, paths, hashes)
    out = [ra.fact("request", f"The confirmed request: {p.get('request')}", "review", key="request:confirmed")]
    out += [ra.fact("rule_change", f"Rule to change: {t}", "review", key=f"rule_change:scope:{t}") for t in p.get("changes") or []]
    for i, rc in enumerate((p.get("draft") or {}).get("rule_changes") or []):           # the stored draft's own order
        out.append(ra.fact("rule_change", f"Before: {rc.get('before')} | After: {rc.get('after')}", "draft", key=f"rule_change:draft:{i:03d}"))
    rules = (p.get("impact") or {}).get("affected_rules") or []
    if len(rules) > IMPACT_LIMIT:
        upstream["impact_omitted"] = len(rules) - IMPACT_LIMIT
    for i, r in enumerate(rules[:IMPACT_LIMIT]):
        vs = r.get("versions") or []                                                     # observed and target versions of one logical rule, each with its own link
        out.append(ra.fact("impact", f"Affected rule: {r.get('title')} ({r.get('link')})" + (" - versions: " + "; ".join(f"{v['view']} {v['link']}" for v in vs) if vs else ""), "impact",
                           display=f"Affected rule: {r.get('title')} ({r.get('meaning') or r.get('link')})" + (f", reached in {len(vs)} versions (observed in the sources and the accepted target)" if vs else ""),
                           key=f"impact:{r.get('ref_key') or i}"))                                  # one fact per logical rule (the link kind is not an identity)
    decisions = sorted(p.get("decisions") or [], key=lambda d: (str(d.get("at")), str(d.get("kind")), str(d.get("action"))), reverse=True)   # newest first
    for d in decisions:
        state = "current" if d.get("current") else f"not current ({d.get('stale_reason')})"
        words = {"approve": "approved", "reject": "rejected", "request_changes": "changes requested", "clarify": "clarified"}.get(d.get("action"), str(d.get("action")))
        what = {"knowledge": "Knowledge decision", "file_writes": "File edit decision", "clarification": "Clarification"}.get(d.get("kind"), "Decision")
        out.append(ra.fact("decision", f"{d.get('kind')} decision: {d.get('action')}, {state}; reason given: {d.get('reason')}", "decision",
                           display=f"{what}: {words}{'' if d.get('current') else ' (no longer current: ' + str(d.get('stale_reason')) + ')'}. Reason given: {d.get('reason')}",
                           tier="support" if d.get("current") else "history", key=f"decision:{d.get('at')}:{d.get('kind')}:{d.get('action')}"))
    return out


def auth_fingerprint(family: str, authority: dict) -> dict:
    """The reader's authorization as bound into the pack (and stored with each question)."""
    return {"roles": sorted(authority.get("roles") or ()), "evidence_read": "evidence.read" in permissions_for(authority.get("roles")), "family": family}


def build_context(store, tenant: str, ws: str, family: str, rid: str, authority: dict) -> dict:
    """-> the pack (`request_assistant.pack`). Raises TenantScopeError("request not found") when the reader may not read the request."""
    tv = v2_request_tasks.task_view(store, tenant, ws, family, rid, authority)              # applies the family read rule first
    facts, upstream = ra.task_facts(tv), {}
    if family == "intent_flow":
        facts += _intent_facts(store, tenant, ws, tv, authority, upstream)
    else:
        facts += _business_facts(store, tenant, ws, rid, authority, upstream)
    auth = auth_fingerprint(family, authority)
    return ra.pack(tv["request_ref"], tv["as_of"], facts, auth=auth, upstream=upstream)   # raises ra.ContextOverflow when the mandatory facts do not fit


__all__ = ["build_context", "auth_fingerprint", "TenantScopeError", "ContextOverflow"]
ContextOverflow = ra.ContextOverflow
