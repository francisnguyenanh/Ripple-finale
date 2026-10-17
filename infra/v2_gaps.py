"""GAP clarification loop over rows the store already holds (read model) + one confirm route.

A GAP of a `change.draft` is anything the draft left for a person: a question the model asked, an item left
AMBIGUOUS/MANUAL/UNSUPPORTED, an unclassified evidence ref, a model/intent mismatch, a stored finding kept open.
Three different things can happen to a GAP and they are never mixed:

* a COMMENT  = `POST /v2/hitl/answer` (existing, `knowledge.answer`): a PENDING draft text. Not authoritative, not read
               by any later draft, closes nothing.
* a SUGGESTION = a `gap.answer` model job (existing, needs approval, budget-guarded): a proposal with citations. Not
               authoritative either; the person may copy it into a confirmation.
* a CONFIRMATION = `POST /v2/change/gap/confirm` (`knowledge.confirm`, human): a durable decision (who, what, why, epoch).
               Only confirmed clarifications are given to the NEXT `change.draft`, as stated assumptions - never as source
               evidence - and recording one moves the decision epoch, so every earlier draft is shown STALE.
Nothing here edits a source file, closes a finding or hides residual counterevidence.
"""

from __future__ import annotations

import json

GAP_PREFIX = "gap:"
SCOPE_KIND = "draft_gap_answer"
MAX_ANSWER = 600


def gap_id(draft_job_id: str, key: str) -> str:
    return f"{GAP_PREFIX}{draft_job_id}:{key}"


def _items(result: dict) -> list[dict]:
    """The GAPs of a draft result, with stable keys."""
    out = []
    for i, q in enumerate(result.get("questions_for_human") or []):
        out.append({"key": f"q{i}", "kind": "question", "text": str(q)})
    for x in result.get("items") or []:
        if x.get("status") in ("AMBIGUITY", "MANUAL_CHANGE", "UNSUPPORTED") or (x.get("status") == "PROPOSED" and x.get("disposition") == "proposed_remediation"):
            out.append({"key": f"ref:{x.get('ref')}", "kind": "item", "status": x.get("status"), "ref": x.get("ref"),
                        "logical_path": x.get("logical_path"),
                        **({"disposition": x.get("disposition"), "carried_from": x.get("carried_from"), "disposition_basis": x.get("disposition_basis")} if (x.get("carried_from") or x.get("disposition") not in (None, "unresolved")) else {}),
                        "text": f"{x.get('logical_path')}: {x.get('status')} - {x.get('reason') or x.get('rationale') or ''}"})
    for u in result.get("unaddressed") or []:
        out.append({"key": f"ref:{u.get('ref')}", "kind": "unaddressed", "ref": u.get("ref"),
                    "logical_path": u.get("logical_path"),
                    **({"disposition": u.get("disposition"), "carried_from": u.get("carried_from"), "disposition_basis": u.get("disposition_basis")} if (u.get("carried_from") or u.get("disposition") not in (None, "unresolved")) else {}),
                    "text": f"{u.get('logical_path')} {u.get('location') or ''}: not classified by the model - {u.get('text') or ''}"})
    for i, m in enumerate(result.get("mismatches") or []):
        out.append({"key": f"mismatch{i}", "kind": "mismatch",
                    "text": f"The model read {m.get('field')} as {m.get('model')}; your confirmed value {m.get('intent')} is used."})
    seen = set()
    for f in (result.get("open_items") or [])[:50]:
        sig = (f.get("type"), str(f.get("reason")))
        if sig in seen:
            continue
        seen.add(sig)
        out.append({"key": f"finding{len(seen) - 1}", "kind": "finding",
                    "text": f"Stored finding kept open: {f.get('type') or 'finding'} - {f.get('reason')}"})
        if len(seen) >= 10:
            break
    return out


def reading_items(store, tenant: str, workspace_id: str, draft_job_id: str) -> list[dict]:
    """The questions and options of the stored AI reading of THE simulation built from this draft, as open points of the draft (so they are answered, decided and
    carried into the next draft through the SAME comment / confirm / revise path, not a parallel one). Only the latest reading, only a COMPLETED one; the
    text is what the AI wrote, labelled as such. Nothing here measures anything or applies anything."""
    with store._tx() as cur:
        cur.execute("""SELECT a.job_id, a.execution_result FROM v2_jobs a JOIN v2_jobs s ON s.tenant_id=a.tenant_id AND s.workspace_id=a.workspace_id
              AND s.job_id = a.payload->'snapshot'->>'simulation_job_id'
            WHERE a.tenant_id=%s AND a.workspace_id=%s AND a.kind='sim.analysis' AND a.execution_status='COMPLETED'
              AND s.kind='simulation' AND s.payload->>'from_draft_job_id'=%s ORDER BY a.job_id DESC LIMIT 1""", (tenant, workspace_id, draft_job_id))
        row = cur.fetchone()
    if not row:
        return []
    res = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
    tag = str(row[0])[-8:]
    out = []
    for i, q in enumerate((res.get("questions") or [])[:6]):
        out.append({"key": f"reading:{tag}:q{i}", "kind": "reading_question", "reading_job_id": row[0], "text": f"AI reading of the simulation asks: {str(q)[:260]}"})
    for i, o in enumerate((res.get("options") or [])[:4]):
        if isinstance(o, dict) and o.get("option"):
            out.append({"key": f"reading:{tag}:o{i}", "kind": "reading_option", "reading_job_id": row[0],
                        "text": f"AI reading of the simulation suggests: {str(o['option'])[:200]} (it assumes: {str(o.get('assumption') or 'nothing stated')[:120]}). Should the next draft take this into account?"})
    return out


_REFUSED = {"empty_or_flagged_as_instruction": "the answer was empty or read like an instruction to the model, so this draft did not use it"}


def _feedback(item: dict, decisions: list, used_ids: set, refused: dict, pinned_epoch) -> dict:
    """What happened to the feedback on ONE open point, for THIS draft, from recorded facts only (comments, the confirm decision, what the draft stored as used or refused).
    pending: only comments exist, a comment is never a decision. applied: a confirmed answer this draft used. rejected: a confirmed answer this draft refused (with the reason).
    not_applied_yet: confirmed, but this draft was made before it (revise). withdrawn: a confirmation that was later revoked or replaced. none: nothing was said."""
    conf = item.get("confirmation")
    if conf:
        base = {"by": conf["actor_id"], "decision_id": conf["decision_id"], "epoch": conf["epoch"], "at": conf.get("at")}
        if conf["decision_id"] in used_ids:
            return {"status": "applied", "reason": "this draft was given the confirmed answer as a stated assumption", **base}
        if conf["decision_id"] in refused:
            return {"status": "rejected", "reason": _REFUSED.get(refused[conf["decision_id"]], str(refused[conf["decision_id"]])), **base}
        return {"status": "not_applied_yet", "reason": "confirmed after this draft was made; revise the draft to use it" if (pinned_epoch is None or conf["epoch"] > pinned_epoch)
                else "confirmed, but this draft did not take it; revise the draft", **base}
    if any(not d[7] for d in decisions):
        last = [d for d in decisions if not d[7]][-1]
        return {"status": "withdrawn", "reason": "a confirmed answer was later revoked or replaced; it is not used", "by": last[2], "decision_id": last[0], "epoch": last[1]}
    if item.get("comments"):
        last = item["comments"][-1]
        return {"status": "pending", "reason": "a comment is not a decision: it is not given to the next draft until a person with the right confirms it", "by": last["actor_id"], "at": last["at"]}
    return {"status": "none", "reason": "nothing was said about this point"}


def _feedback_log(store, tenant: str, workspace_id: str, intent_id, used_ids: set, refused: dict, pinned_epoch) -> list[dict]:
    """Every confirmed answer on this REQUEST (any of its drafts), and what THIS draft did with it: applied / rejected (refused, with the reason) / not_applied_yet / withdrawn.
    Comments are not listed here: they are never decisions and stay on their open point."""
    if not intent_id:
        return []
    with store._tx() as cur:
        cur.execute("""SELECT decision_id, epoch, actor_id, target, rationale, scope, created_at,
                NOT EXISTS (SELECT 1 FROM v2_decisions AS s WHERE s.tenant_id=v2_decisions.tenant_id AND s.workspace_id=v2_decisions.workspace_id
                            AND (s.supersedes=v2_decisions.decision_id OR s.revokes=v2_decisions.decision_id)) FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND action='confirm' AND target LIKE 'gap:%%'
              AND scope->>'kind'='draft_gap_answer' AND scope->>'intent_id'=%s ORDER BY epoch""", (tenant, workspace_id, intent_id))
        rows = cur.fetchall()
    out = []
    for r in rows:
        sc = r[5] if isinstance(r[5], dict) else (json.loads(r[5]) if r[5] else {})
        if not r[7]:
            status, reason = "withdrawn", "a confirmed answer was later revoked or replaced; it is not used"
        elif r[0] in used_ids:
            status, reason = "applied", "this draft was given the confirmed answer as a stated assumption"
        elif r[0] in refused:
            status, reason = "rejected", _REFUSED.get(refused[r[0]], str(refused[r[0]]))
        elif pinned_epoch is None or r[1] > pinned_epoch:
            status, reason = "not_applied_yet", "confirmed after this draft was made; revise the draft to use it"
        else:
            status, reason = "not_applied_yet", "confirmed, but this draft did not take it; revise the draft"
        out.append({"decision_id": r[0], "epoch": r[1], "by": r[2], "question": str(sc.get("question") or "")[:300], "answer": str(sc.get("answer") or "")[:600],
                    "why": r[4], "at": r[6].isoformat() if r[6] else None, "status": status, "reason": reason})
    return out


def build_gaps(store, tenant: str, workspace_id: str, draft_job_id: str) -> dict | None:
    try:
        rec = store.job_record(tenant, workspace_id, draft_job_id)
    except Exception:
        return None
    if rec.get("kind") != "change.draft":
        return None
    result = rec.get("execution_result") if rec.get("execution_status") == "COMPLETED" else None
    head = store.head(tenant, workspace_id)
    if not isinstance(result, dict):
        return {"draft_job_id": draft_job_id, "completed": False, "gaps": [], "stale": None}
    pin = result.get("pinned_input") or {}
    reasons = []
    if pin.get("pinned_decision_epoch") != head.decision_epoch:
        reasons.append(f"decision epoch moved ({pin.get('pinned_decision_epoch')} -> {head.decision_epoch}): a decision was recorded after this draft")
    if pin.get("pinned_source_revision_id") != head.source_revision_id:
        reasons.append("source revision moved after this draft")
    items = _items(result) + reading_items(store, tenant, workspace_id, draft_job_id)
    used_ids = {c.get("decision_id") for c in result.get("clarifications_used") or []}
    refused = {u.get("ref"): u.get("reason") for u in result.get("unsupported") or [] if isinstance(u, dict) and u.get("kind") == "clarification"}
    ids = [gap_id(draft_job_id, i["key"]) for i in items]
    with store._tx() as cur:
        cur.execute("""SELECT question_id, actor_id, answer_text, version_after, created_at FROM v2_hitl_answers
            WHERE tenant_id=%s AND workspace_id=%s AND question_id = ANY(%s) ORDER BY id""", (tenant, workspace_id, ids))
        comments = cur.fetchall()
        cur.execute("""SELECT decision_id, epoch, actor_id, target, rationale, scope, created_at,
                NOT EXISTS (SELECT 1 FROM v2_decisions AS s WHERE s.tenant_id=v2_decisions.tenant_id AND s.workspace_id=v2_decisions.workspace_id
                            AND (s.supersedes=v2_decisions.decision_id OR s.revokes=v2_decisions.decision_id)) FROM v2_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND action='confirm' AND target = ANY(%s) ORDER BY epoch""",
                    (tenant, workspace_id, ids))
        decisions = cur.fetchall()
        cur.execute("""SELECT job_id, execution_status, status, payload FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='gap.answer' AND payload->>'question_id' = ANY(%s)
            ORDER BY job_id""", (tenant, workspace_id, ["suggest:" + i for i in ids]))
        suggestions = cur.fetchall()
    for it in items:
        gid = gap_id(draft_job_id, it["key"])
        it["gap_id"] = gid
        it["comments"] = [{"actor_id": c[1], "text": c[2], "version": c[3], "at": c[4].isoformat() if c[4] else None}
                          for c in comments if c[0] == gid]
        confirmed = [d for d in decisions if d[3] == gid and d[7]]
        scope = lambda d: d[5] if isinstance(d[5], dict) else (json.loads(d[5]) if d[5] else {})
        it["confirmation"] = None if not confirmed else {
            "decision_id": confirmed[-1][0], "epoch": confirmed[-1][1], "actor_id": confirmed[-1][2],
            "answer": scope(confirmed[-1]).get("answer"), "rationale": confirmed[-1][4],
            "at": confirmed[-1][6].isoformat() if confirmed[-1][6] else None}
        it["suggestion_jobs"] = [s[0] for s in suggestions if (s[3] if isinstance(s[3], dict) else json.loads(s[3])).get("question_id") == "suggest:" + gid]
        it["state"] = "confirmed" if it["confirmation"] else ("commented" if it["comments"] else "unresolved")
        it["feedback"] = _feedback(it, [d for d in decisions if d[3] == gid], used_ids, refused, pin.get("pinned_decision_epoch"))
    intent_id = (result.get("intent") or {}).get("intent_id")
    return {"draft_job_id": draft_job_id, "completed": True, "intent_id": intent_id,
            "feedback_log": _feedback_log(store, tenant, workspace_id, intent_id, used_ids, refused, pin.get("pinned_decision_epoch")),
            "stale": bool(reasons), "stale_reasons": reasons,
            "pinned_decision_epoch": pin.get("pinned_decision_epoch"), "head_decision_epoch": head.decision_epoch,
            "clarifications_used": result.get("clarifications_used") or [], "gaps": items,
            "note": "Comments and suggestions are not authoritative. Only a confirmation is given to the next draft."}
