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


from core.change_intelligence.v2.business_impact import _LINK_WORDS                       # R12: impact relations in the words the review page uses

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


def _business_facts(store, tenant, ws, rid, authority, upstream: dict, view=..., ) -> list:
    from infra.adapters import v2_business_review
    from infra.v2_business_routes import _review_payload
    if view is ...:                                                                       # R20: the task view's own review view is reused (same reader, same read)
        view = v2_business_review.review_view(store, tenant, ws, rid, account_id=authority["account_id"], roles=authority.get("roles"))
    if view is None:
        return []
    can_evidence = "evidence.read" in permissions_for(authority.get("roles"))
    p = _review_payload(view, mode="business", can_evidence=can_evidence, change_id=rid)      # the review page's own shaping for THIS reader (no ids, paths, hashes)
    out = [ra.fact("request", f"The confirmed request: {p.get('request')}", "review", key="request:confirmed")]
    out += [ra.fact("rule_change", f"Rule to change: {t}", "review", key=f"rule_change:scope:{t}") for t in p.get("changes") or []]
    from core.change_intelligence.v2 import business_draft
    stored = (view.get("draft") or {}).get("rule_changes") or []
    for i, rc in enumerate((p.get("draft") or {}).get("rule_changes") or []):           # the stored draft's own order
        src = stored[i] if i < len(stored) else {}
        if src.get("before") and src.get("after"):                                       # R14: each part labelled (who acts under the rule is not its scope)
            words = f"Before: {business_draft.rule_words(src['before'])} || After: {business_draft.rule_words(src['after'])}"
        else:
            words = f"Before: {rc.get('before')} | After: {rc.get('after')}"
        out.append(ra.fact("rule_change", words if len(words) <= ra.MAX_FACT_CHARS else f"Before: {rc.get('before')} | After: {rc.get('after')}", "draft",
                           key=f"rule_change:draft:{i:03d}"))
        change = business_draft.condition_change((src.get("before") or {}).get("condition"), (src.get("after") or {}).get("condition"))
        if change is not None:                                                           # R12: computed by the server from the stored conditions (exact), never by the model
            sb, sa = (src.get("before") or {}).get("scope") or {}, (src.get("after") or {}).get("scope") or {}
            scope = (business_draft._scope_text(sa) + ", unchanged") if sb == sa and sa else ("changes too, see the rule change" if sb != sa else None)
            out.append(ra.fact("rule_change", ra.computed_range_text(rc.get("title"), change, scope), "computed", {"computed": bool(change.get("computed"))},
                               key=f"rule_change:draft:{i:03d}:range"))
    if stored:                                                                           # context/7 (R16): who the record names as affected (nobody; the actor applies the rule)
        out.append(ra.parties_fact((business_draft._role_words((rc.get(side) or {}).get("actor_ref")) for rc in stored for side in ("before", "after") if (rc.get(side) or {}).get("actor_ref")),
                                   ranged=any(str(f.get("key") or "").endswith(":range") for f in out if f)))   # context/10: no pointer to a range fact the pack lacks
    rules = (p.get("impact") or {}).get("affected_rules") or []
    if len(rules) > IMPACT_LIMIT:
        upstream["impact_omitted"] = len(rules) - IMPACT_LIMIT
    for i, r in enumerate(rules[:IMPACT_LIMIT]):
        vs = r.get("versions") or []                                                     # observed and target versions of one logical rule, each with its own link
        rel = (r.get("meaning") or r.get("link")) + ("; not a confirmed effect of this request" if r.get("link") == "inferred_dependency" else "")
        if not vs:
            vers = ""
        elif all(v.get("link") == r.get("link") for v in vs):                            # R12: the same relation in every version is said once
            vers = f" - the same in its {len(vs)} versions ({', '.join(v['view'] for v in vs)})"
        else:
            vers = " - versions: " + "; ".join(f"{v['view']} {_LINK_WORDS.get(v['link'], v['link'])}" for v in vs)
        text = f"Affected rule: {r.get('title')} ({rel}){vers}"
        if len(text) > ra.MAX_FACT_CHARS:                                                # whole, never cut: the versions detail gives way to their count first
            text = f"Affected rule: {r.get('title')} ({rel}) - {len(vs)} versions"
        out.append(ra.fact("impact", text, "impact",
                           display=f"Affected rule: {r.get('title')} ({r.get('meaning') or r.get('link')})" + (f", reached in {len(vs)} versions (observed in the sources and the accepted target)" if vs else ""),
                           key=f"impact:{r.get('ref_key') or i}"))                                  # one fact per logical rule (the link kind is not an identity)
    decisions = sorted(p.get("decisions") or [], key=lambda d: (str(d.get("at")), str(d.get("kind")), str(d.get("action"))), reverse=True)   # newest first
    out.append(ra.decision_status_fact(decisions))                                       # R12: which decisions are in effect now (mandatory)
    for d in decisions:
        # A104 / A106 (H-13, H2-13): a no-longer-current decision is history; the model twice read its request for changes as still pending
        state = "current" if d.get("current") else (f"NO LONGER CURRENT ({d.get('stale_reason')}); it was answered by a later version of the draft and nothing "
                                                    "is pending from it")
        words = {"approve": "approved", "reject": "rejected", "request_changes": "changes requested", "clarify": "clarified"}.get(d.get("action"), str(d.get("action")))
        what = {"knowledge": "Knowledge decision", "file_writes": "File edit decision", "clarification": "Clarification"}.get(d.get("kind"), "Decision")
        out.append(ra.fact("decision", f"{d.get('kind')} decision: {d.get('action')}, {state}; reason given: {d.get('reason')}", "decision",
                           display=f"{what}: {words}{'' if d.get('current') else ' (no longer current: ' + str(d.get('stale_reason')) + ')'}. Reason given: {d.get('reason')}",
                           tier="support" if d.get("current") else "history", key=f"decision:{d.get('at')}:{d.get('kind')}:{d.get('action')}"))
    return out


def auth_fingerprint(family: str, authority: dict) -> dict:
    """The reader's authorization as bound into the pack (and stored with each question)."""
    return {"roles": sorted(authority.get("roles") or ()), "evidence_read": "evidence.read" in permissions_for(authority.get("roles")), "family": family}


def build_context(store, tenant: str, ws: str, family: str, rid: str, authority: dict, *, keep: dict | None = None) -> dict:
    """-> the pack (`request_assistant.pack`). Raises TenantScopeError("request not found") when the reader may not read the request. `keep` (R19) receives the
    task view the pack was built from, so a caller that also needs it (the worker's next steps) does not read it a second time."""
    seen: dict = {}
    tv = v2_request_tasks.task_view(store, tenant, ws, family, rid, authority, keep=seen)  # applies the family read rule first
    if keep is not None:
        keep["task_view"] = tv
    facts, upstream = ra.task_facts(tv), {}
    if family == "intent_flow":
        facts += _intent_facts(store, tenant, ws, tv, authority, upstream)
    else:
        facts += _business_facts(store, tenant, ws, rid, authority, upstream, view=seen.get("review_view", ...))
    auth = auth_fingerprint(family, authority)
    return ra.pack(tv["request_ref"], tv["as_of"], facts, auth=auth, upstream=upstream)   # raises ra.ContextOverflow when the mandatory facts do not fit


__all__ = ["build_context", "auth_fingerprint", "TenantScopeError", "ContextOverflow"]
ContextOverflow = ra.ContextOverflow
