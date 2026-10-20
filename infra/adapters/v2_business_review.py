"""Review and decisions on a CONFIRMED business change (contract fkl/1, CHG-04).

The current draft is recomputed from stored rows every time (CHG-03 `draft_for_intent` + the dependency and guidance job states + people's clarifications). A decision is a
ledger row in `v2_business_change_decisions` plus an audit event: it changes no rule, snapshot, head, epoch, workspace version or source. Three kinds are kept apart:
`knowledge` (accept / reject / request changes on the semantic diff, `change.approve`), `file_writes` (approve / reject the AI_EDIT_SUPPORTED proposal edits, `change.approve`,
never implied by a knowledge decision) and `clarification` (a person's disposition or answer for one open item; requester or `knowledge.confirm`; it revises the draft).
Every decision binds to the draft the person saw (`semantic_diff_hash`, `draft_hash`, pin). Inside ONE transaction under the intent job row lock and the workspace head lock the
server rechecks: the actor still holds the permission, the knowledge did not move after confirmation, the hashes sent equal the current ones, no terminal decision exists for
this draft version, the distinct-approver policy, and (for approve) no semantic blocker is open. Any refusal writes nothing.
"""
from __future__ import annotations

import uuid

from psycopg2.extras import Json

from core.change_intelligence.v2 import business_draft as bd
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters import v2_business, v2_business_change
from infra.adapters.v2_business import BusinessKnowledgeRefused
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require_distinct_approver
from infra.adapters.v2_postgres import StaleVersion, TenantScopeError, _as_object

KINDS = ("knowledge", "file_writes", "clarification")
ACTIONS = {"knowledge": ("approve", "reject", "request_changes"), "file_writes": ("approve", "reject"), "clarification": ("clarify",)}
AUDIT_EVENT = "BUSINESS_CHANGE_DECISION_RECORDED"
MAX_REASON = 1000
MAX_ANSWER = 1000


def _dep_state(store, tenant, ws, change_id, probe):
    from infra import v2_business_routes as routes
    return routes._dependency_input(store, tenant, ws, change_id, probe)


def _guidance(store, tenant, ws, change_id, draft):
    from infra import v2_business_routes as routes
    return routes._merge_guidance_state(draft, store, tenant, ws, change_id, can_evidence=True)


def list_clarifications(store, tenant_id, workspace_id, intent_id) -> list:
    with store._tx() as cur:
        cur.execute("""SELECT decision_id, actor_id, reason, binding, draft_hash, created_at FROM v2_business_change_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND kind='clarification' ORDER BY created_at, decision_id""", (tenant_id, workspace_id, intent_id))
        rows = cur.fetchall()
    out = []
    for r in rows:
        b = _as_object(r[3]) or {}
        out.append({"decision_id": r[0], "actor_id": r[1], "reason": r[2], "ref": b.get("ref"), "disposition": b.get("disposition"), "answer": b.get("answer"), "draft_hash": r[4],
                    "created_at": r[5].isoformat()})
    return out


def list_decisions(store, tenant_id, workspace_id, intent_id) -> list:
    with store._tx() as cur:
        cur.execute("""SELECT decision_id, kind, action, actor_id, reason, semantic_diff_hash, draft_hash, pin, binding, self_approval, created_at FROM v2_business_change_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s ORDER BY created_at, decision_id""", (tenant_id, workspace_id, intent_id))
        rows = cur.fetchall()
    return [{"decision_id": r[0], "kind": r[1], "action": r[2], "actor_id": r[3], "reason": r[4], "semantic_diff_hash": r[5], "draft_hash": r[6], "pin": _as_object(r[7]),
             "binding": _as_object(r[8]) or {}, "self_approval": bool(r[9]), "created_at": r[10].isoformat()} for r in rows]


def current_draft(store, tenant_id, workspace_id, change_id):
    """-> (draft with dependency state, guidance and clarifications merged, guidance_written_with_evidence, titles) or None when not confirmed. Reads only."""
    probe = v2_business_change.impact_for_intent(store, tenant_id, workspace_id, change_id)
    if probe is None:
        return None
    deps, dep_state = _dep_state(store, tenant_id, workspace_id, change_id, probe)
    got = v2_business_change.draft_for_intent(store, tenant_id, workspace_id, change_id, proposed_dependencies=deps, dependency_state=dep_state)
    draft, written_with_evidence = _guidance(store, tenant_id, workspace_id, change_id, got["draft"])
    draft = bd.merge_clarifications(draft, list_clarifications(store, tenant_id, workspace_id, change_id))
    return draft, written_with_evidence, got


def _knowledge_moved(store, tenant_id, workspace_id, pin) -> bool:
    current = v2_business._working(v2_business.business_view(store, tenant_id, workspace_id))
    return bool(current and current["snapshot"]["snapshot_id"] != pin["snapshot_id"])


def decision_states(decisions: list, draft: dict) -> list:
    """Each recorded decision with whether it is still current for the draft as it is now, and why not."""
    out = []
    for d in decisions:
        if d["kind"] == "clarification":
            out.append(dict(d, current=True, stale_reason=None))
            continue
        if d["semantic_diff_hash"] != draft["semantic_diff_hash"] or d["pin"] != draft["pin"]:
            why = "the meaning of the change or the knowledge it was confirmed against changed"
        elif d["kind"] == "file_writes" and (d["binding"] or {}).get("edits_hash") != bd.edits_hash(draft):
            why = "the proposed file edits changed"
        elif d["draft_hash"] != draft["draft_hash"]:
            why = "the draft was revised after this decision (new guidance or a clarification)"
        else:
            why = None
        out.append(dict(d, current=why is None, stale_reason=why))
    return out


def _latest_current(states: list, kind: str):
    cur = [s for s in states if s["kind"] == kind and s["current"] and s["action"] in ("approve", "reject", "request_changes")]
    return cur[-1] if cur else None


def what_actor_can(draft: dict, states: list, *, perms, account_id: str, requester_id: str, confirmer_id: str, policy: dict, knowledge_moved: bool) -> dict:
    """What THIS person may do now, with the reason when not. Decided again inside the transaction; this is for the page."""
    blockers = bd.semantic_blockers(draft, acknowledged_flags=False)
    hard = [b for b in blockers if b["code"] != "flag_not_acknowledged"]
    flags = [b for b in blockers if b["code"] == "flag_not_acknowledged"]
    has_edits = bd.edits_hash(draft) is not None
    k_cur, f_cur = _latest_current(states, "knowledge"), _latest_current(states, "file_writes")
    self_party = account_id in (requester_id, confirmer_id)
    out = {"approve_knowledge": False, "approve_file_writes": False, "clarify": False, "reasons": {}, "blockers": blockers, "flags_to_acknowledge": [f["text"] for f in flags],
           "knowledge_decision": k_cur, "file_writes_decision": f_cur, "has_proposed_edits": has_edits}
    if "change.approve" not in perms:
        out["reasons"]["approve_knowledge"] = out["reasons"]["approve_file_writes"] = "your role cannot decide this change"
    elif knowledge_moved:
        out["reasons"]["approve_knowledge"] = out["reasons"]["approve_file_writes"] = "the business knowledge moved after this request was confirmed; it has to be stated and confirmed again"
    elif self_party and (policy["require_distinct_approver"] or flags):
        out["reasons"]["approve_knowledge"] = out["reasons"]["approve_file_writes"] = ("a different person has to decide this change" if policy["require_distinct_approver"]
                                                                                         else "a high-risk change is never decided by the person who asked for it")
    else:
        if k_cur is not None and k_cur["action"] in ("approve", "reject"):
            out["reasons"]["approve_knowledge"] = f"already decided for this version of the draft ({k_cur['action']})"
        elif draft["status"] == "INVALID":
            out["reasons"]["approve_knowledge"] = "the changed rule is not valid"
        elif hard:
            out["reasons"]["approve_knowledge"] = "open questions block the knowledge decision"
        else:
            out["approve_knowledge"] = True
        if not has_edits:
            out["reasons"]["approve_file_writes"] = "there is no automatic edit to approve"
        elif f_cur is not None:
            out["reasons"]["approve_file_writes"] = f"already decided for these edits ({f_cur['action']})"
        else:
            out["approve_file_writes"] = True
    if "knowledge.confirm" in perms or ("intent.confirm_own" in perms and account_id == requester_id):
        out["clarify"] = True
    else:
        out["reasons"]["clarify"] = "only the person who asked or a knowledge owner can clarify"
    return out


def review_view(store, tenant_id, workspace_id, change_id, *, account_id: str, roles) -> dict | None:
    """Everything the review page needs, computed from stored rows (nothing written, no model). None if the request is not confirmed."""
    got = current_draft(store, tenant_id, workspace_id, change_id)
    if got is None:
        return None
    draft, written_with_evidence, parts = got
    rec = store.job_record(tenant_id, workspace_id, change_id)
    perms = permissions_for(roles)
    decisions = decision_states(list_decisions(store, tenant_id, workspace_id, change_id), draft)
    moved = _knowledge_moved(store, tenant_id, workspace_id, draft["pin"])
    policy = store.workspace_policy(tenant_id, workspace_id)
    can = what_actor_can(draft, decisions, perms=perms, account_id=account_id, requester_id=rec.get("requested_by"), confirmer_id=parts["intent"]["confirmed_by"], policy=policy,
                         knowledge_moved=moved)
    from infra.adapters import v2_business_commit
    committed = v2_business_commit.committed_state(store, tenant_id, workspace_id, change_id)
    k = can["knowledge_decision"]
    if committed is not None:
        can["commit_knowledge"], can["reasons"]["commit_knowledge"] = False, "the knowledge change is already committed"
    elif "knowledge.confirm" not in perms:
        can["commit_knowledge"], can["reasons"]["commit_knowledge"] = False, "only a knowledge owner commits a change"
    elif moved:
        can["commit_knowledge"], can["reasons"]["commit_knowledge"] = False, "the business knowledge moved after this request was confirmed"
    elif k is None or k["action"] != "approve":
        can["commit_knowledge"], can["reasons"]["commit_knowledge"] = False, "the knowledge change needs a current approval first"
    else:
        can["commit_knowledge"] = True
    from infra.adapters import v2_business_source
    source = v2_business_source.source_state(store, tenant_id, workspace_id, change_id, draft, committed, perms=perms)
    f = can["file_writes_decision"]
    if source["job"] is not None:
        can["apply_source_edits"], can["reasons"]["apply_source_edits"] = False, f"the automatic edit job exists ({source['job']['status']})"
    elif not source["automatic_edit_available"]:
        can["apply_source_edits"], can["reasons"]["apply_source_edits"] = False, "there is no automatic edit; the items are manual"
    elif v2_business_source.APPLY_PERMISSION not in perms:
        can["apply_source_edits"], can["reasons"]["apply_source_edits"] = False, "your role cannot apply source edits"
    elif f is None or f["action"] != "approve":
        can["apply_source_edits"], can["reasons"]["apply_source_edits"] = False, "the file edits need a current approval first"
    elif committed is None:
        can["apply_source_edits"], can["reasons"]["apply_source_edits"] = False, "commit the knowledge change first"
    else:                                                                             # the commit itself moved the knowledge; the file edit is bound to the approved edits and the source revision, not to the business pin
        can["apply_source_edits"] = True
    return {"draft": draft, "guidance_written_with_evidence": written_with_evidence, "report": parts["report"], "intent": parts["intent"], "titles": parts["titles"], "decisions": decisions,
            "can": can, "knowledge_moved": moved, "requester_id": rec.get("requested_by"), "policy": policy, "edits_hash": bd.edits_hash(draft),
            "committed": committed, "source": source, "completion": v2_business_commit.completion(draft, committed, source_apply=source["job"])}


def _clean(text, limit, what):
    if not isinstance(text, str) or not text.strip():
        raise BusinessKnowledgeRefused("invalid_field", f"{what} is required")
    t = " ".join(text.split())
    if len(t) > limit or bd._UNSAFE_TEXT.search(t):
        raise BusinessKnowledgeRefused("invalid_field", f"{what} must be plain text of at most {limit} characters")
    return t


def record_decision(store, tenant_id, workspace_id, change_id, *, actor_id: str, roles, kind: str, action: str, reason: str, seen: dict, body: dict | None = None,
                    idempotency_key: str | None = None) -> dict:
    """Record one human decision on a confirmed change. Refuses with zero writes when anything the decision depends on is not as the person saw it."""
    body = dict(body or {})
    if kind not in KINDS or action not in ACTIONS[kind]:
        raise BusinessKnowledgeRefused("invalid_field", "unknown decision kind or action")
    reason = _clean(reason, MAX_REASON, "a reason in the person's own words")
    perms = permissions_for(roles)
    if kind in ("knowledge", "file_writes") and "change.approve" not in perms:
        raise PermissionDenied("change.approve")
    rec = store.job_record(tenant_id, workspace_id, change_id)
    if rec.get("kind") != "change.understand":
        raise TenantScopeError("change request not found")
    requester = rec.get("requested_by")
    if kind == "clarification":
        if not ("knowledge.confirm" in perms or ("intent.confirm_own" in perms and actor_id == requester)):
            raise PermissionDenied("only the person who asked or a knowledge owner can clarify")
        disposition = body.get("disposition")
        if disposition not in bd.DISPOSITIONS or not isinstance(body.get("ref"), str):
            raise BusinessKnowledgeRefused("invalid_field", "ref and a known disposition are required")
        if body.get("answer") is not None:
            body["answer"] = _clean(body["answer"], MAX_ANSWER, "the answer")
        elif disposition in ("answered", "resolved_by_person"):
            raise BusinessKnowledgeRefused("invalid_field", "this disposition needs an answer")
    got = current_draft(store, tenant_id, workspace_id, change_id)
    if got is None:
        raise BusinessKnowledgeRefused("not_confirmed", "the request is not confirmed")
    draft, _w, parts = got
    confirmer = parts["intent"]["confirmed_by"]
    if not isinstance(seen, dict) or seen.get("semantic_diff_hash") != draft["semantic_diff_hash"]:
        raise BusinessKnowledgeRefused("diff_mismatch", "the change you are deciding is not the change as it is now; look at it again")
    if seen.get("draft_hash") != draft["draft_hash"]:
        raise BusinessKnowledgeRefused("draft_revised", "the draft was revised since you looked at it; look at the current version again")
    if kind == "clarification" and body["ref"] not in bd.open_refs(draft):
        raise BusinessKnowledgeRefused("unknown_reference", "that item is not an open item of this draft")
    if _knowledge_moved(store, tenant_id, workspace_id, draft["pin"]):
        raise StaleVersion("stale_snapshot: the business knowledge moved after this request was confirmed")
    policy = store.workspace_policy(tenant_id, workspace_id)
    self_approval = False
    binding = {"acknowledged_flags": bool(body.get("acknowledged_flags"))}
    if kind in ("knowledge", "file_writes"):
        high = any(f["severity"] == "HIGH" for o in draft["operations"] for f in o["flags"])
        for party in {requester, confirmer} - {None}:
            if high and party == actor_id:
                raise PermissionDenied("a high-risk change is never decided by the person who asked for it")
            require_distinct_approver(party, actor_id, require_distinct=policy["require_distinct_approver"])
        self_approval = actor_id in (requester, confirmer)
        if action == "approve" and draft["status"] == "INVALID":
            raise BusinessKnowledgeRefused("invalid_draft", "the changed rule is not valid; it cannot be accepted")
        if kind == "knowledge" and action == "approve":
            blockers = bd.semantic_blockers(draft, acknowledged_flags=binding["acknowledged_flags"])
            if blockers:
                raise BusinessKnowledgeRefused("open_blockers", "; ".join(b["text"] for b in blockers[:6]))
        if kind == "file_writes":
            eh = bd.edits_hash(draft)
            if eh is None:
                raise BusinessKnowledgeRefused("nothing_to_approve", "this draft proposes no automatic edit")
            if seen.get("edits_hash") != eh:
                raise BusinessKnowledgeRefused("edits_changed", "the proposed edits are not the ones you looked at")
            binding["edits_hash"] = eh
            binding["edit_refs"] = [i["ref"] for i in draft["file_actions"] if i["action"] == "AI_EDIT_SUPPORTED"]
    else:
        binding.update(ref=body["ref"], disposition=body["disposition"], answer=body.get("answer"))
    permission = "change.approve" if kind != "clarification" else ("knowledge.confirm" if "knowledge.confirm" in perms else "intent.confirm_own")
    payload_hash = canonical_hash({"kind": kind, "action": action, "reason": reason, "seen": seen, "binding": binding, "actor": actor_id})
    with store._tx() as cur:
        cur.execute("SELECT 1 FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s FOR UPDATE", (tenant_id, workspace_id, change_id))
        if cur.fetchone() is None:
            raise TenantScopeError("change request not found")
        head = store._lock_head(cur, tenant_id, workspace_id)
        if idempotency_key:
            cur.execute("""SELECT decision_id, payload_hash FROM v2_business_change_decisions WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND idempotency_key=%s""",
                        (tenant_id, workspace_id, change_id, idempotency_key))
            prior = cur.fetchone()
            if prior is not None:
                if prior[1] != payload_hash:
                    raise BusinessKnowledgeRefused("idempotency_conflict", "the same key was used for a different decision")
                return {"decision_id": prior[0], "replayed": True, "kind": kind, "action": action}
        store._recheck_actor_authority(cur, tenant_id, workspace_id, actor_id, permission)
        cur.execute("SELECT accepted_snapshot_id, business_revision FROM v2_business_heads WHERE tenant_id=%s AND workspace_id=%s", (tenant_id, workspace_id))
        row = cur.fetchone() or (None, 0)
        pin = draft["pin"]
        if (row[0] != pin["snapshot_id"] or row[1] != pin["business_revision"] or head.source_revision_id != pin["source_revision_id"]
                or head.knowledge_revision_id != pin["km_revision_id"] or head.decision_epoch != pin["decision_epoch"]):
            raise StaleVersion("stale_snapshot: the business knowledge moved after this request was confirmed")
        if action in ("approve", "reject"):
            cur.execute("""SELECT action FROM v2_business_change_decisions WHERE tenant_id=%s AND workspace_id=%s AND intent_id=%s AND kind=%s AND draft_hash=%s
                AND action IN ('approve','reject')""", (tenant_id, workspace_id, change_id, kind, draft["draft_hash"]))
            prior = cur.fetchone()
            if prior is not None:
                raise BusinessKnowledgeRefused("already_decided", f"this version of the draft was already decided ({prior[0]})")
        decision_id = "bcd-" + uuid.uuid4().hex[:20]
        cur.execute("""INSERT INTO v2_business_change_decisions (tenant_id, workspace_id, intent_id, decision_id, kind, action, actor_id, reason, semantic_diff_hash, draft_hash, pin,
                binding, self_approval, idempotency_key, payload_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, change_id, decision_id, kind, action, actor_id, reason, draft["semantic_diff_hash"], draft["draft_hash"], Json(pin), Json(binding),
                     self_approval, idempotency_key, payload_hash))
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, AUDIT_EVENT, actor_id, Json({"intent_id": change_id, "decision_id": decision_id, "kind": kind, "action": action, "self_approval": self_approval,
                                                                           "semantic_diff_hash": draft["semantic_diff_hash"], "draft_hash": draft["draft_hash"], **({"ref": binding["ref"]} if kind == "clarification" else {})})))
    return {"decision_id": decision_id, "replayed": False, "kind": kind, "action": action, "self_approval": self_approval, "draft_hash": draft["draft_hash"],
            "semantic_diff_hash": draft["semantic_diff_hash"]}

MAX_DIFF_FILES = 10
MAX_DIFF_LINES_PER_FILE = 120


def file_diff(store, tenant_id, workspace_id, change_id) -> dict | None:
    """EUI-03: the candidate byte diff of every automatic (AI_EDIT_SUPPORTED) file edit of a confirmed request, computed IN MEMORY from the exact bytes of the
    draft's pinned source revision with the same scalar-edit function the apply job uses. Nothing is staged, written or applied; a candidate is not an applied
    change. Bounded: at most MAX_DIFF_FILES files and MAX_DIFF_LINES_PER_FILE lines each, with the coverage stated. None if the request is not confirmed."""
    import difflib
    import hashlib
    from infra.worker.business_source_apply import apply_scalar_edits
    got = current_draft(store, tenant_id, workspace_id, change_id)
    if got is None:
        return None
    draft = got[0]
    pin = (draft.get("pin") or {}).get("source_revision_id")
    actions = draft.get("file_actions") or []
    auto = [i for i in actions if i.get("action") == "AI_EDIT_SUPPORTED"]
    by_artifact: dict = {}
    for it in auto:
        by_artifact.setdefault(it.get("resolved_artifact_id") or it.get("path"), []).append(it)
    files = []
    for artifact_id, its in list(by_artifact.items())[:MAX_DIFF_FILES]:
        entry = {"artifact_id": artifact_id, "path": its[0].get("path"), "refs": [i.get("ref") for i in its], "lines": [], "truncated": False}
        supported = all(i.get("kind") == "config" and str(i.get("path") or "").lower().endswith((".yaml", ".yml")) and isinstance(i.get("proposal_edit"), dict) for i in its)
        if not supported:
            files.append(dict(entry, state="no_byte_diff", reason="only a YAML scalar edit has a byte diff"))
            continue
        try:
            art = store.read_artifact_blob(tenant_id, workspace_id, pin, artifact_id)
            text = art["content"].decode("utf-8")
        except (TenantScopeError, UnicodeDecodeError, KeyError, TypeError):
            files.append(dict(entry, state="unavailable", reason="the pinned bytes of this file could not be read"))
            continue
        except Exception:                                                                 # noqa: BLE001 - blob integrity or storage: never a guessed diff
            files.append(dict(entry, state="unavailable", reason="the pinned bytes of this file could not be read"))
            continue
        try:
            new_text, _applied = apply_scalar_edits(text, [dict(i["proposal_edit"]) for i in its])
        except Exception as exc:                                                          # noqa: BLE001 - the apply job would refuse the same edit
            files.append(dict(entry, state="refused", reason=str(getattr(exc, "args", [""])[-1] or type(exc).__name__)[:200]))
            continue
        lines = []
        for raw in difflib.unified_diff(text.splitlines(), new_text.splitlines(), n=2, lineterm=""):
            if raw.startswith(("---", "+++")):
                continue
            kind = "hunk" if raw.startswith("@@") else "add" if raw.startswith("+") else "remove" if raw.startswith("-") else "context"
            lines.append({"kind": kind, "text": raw if kind == "hunk" else raw[1:]})
            if len(lines) >= MAX_DIFF_LINES_PER_FILE:
                entry["truncated"] = True
                break
        files.append(dict(entry, state="candidate", lines=lines, before_hash=art.get("blob_hash"), after_hash="sha256:" + hashlib.sha256(new_text.encode("utf-8")).hexdigest()))
    return {"change_id": change_id, "pinned_source_revision_id": pin, "files": files,
            "coverage": {"auto_files": len(by_artifact), "files_shown": len(files), "files_not_shown": max(0, len(by_artifact) - MAX_DIFF_FILES),
                         "manual_items": sum(1 for i in actions if i.get("action") == "MANUAL"), "unresolved_items": sum(1 for i in actions if i.get("action") == "UNRESOLVED")},
            "statement": "Candidate bytes computed from the pinned version for review only. Nothing was staged, written or applied; manual and unresolved items have no byte diff."}
