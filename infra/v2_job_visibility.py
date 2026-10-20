"""Who may read a job record through the GENERIC job read (`GET /v2/jobs/{job_id}`), beyond `project.read` (RQA-01, contract REQUEST_TASKS_AND_ASSISTANT section 2).

Several job kinds have their own read route with a narrower rule (a business change is visible to its requester, knowledge confirmers and approvers; an interview reading
to knowledge confirmers; a chat reply to the conversation's creator or a knowledge confirmer; source excerpts and code to `evidence.read`; an ROI analyst memo to its asker
or a knowledge confirmer, never with its records). Before this module the generic route returned those records to every `project.read` member (the business-change text,
interview answers, quotes under keys the quote redaction does not know, code). Each rule here is the SAME rule as the kind's own route; a reader who fails it gets the
generic "job not found". Kinds not listed keep the existing generic rule. The same rule guards the generic job ACTIONS (approve, publish, cancel, retry): a person who may not read
a job may not act on it either (before, any holder of the kind's action permission, e.g. `ai.use`, could cancel or retry another person's business-change or interview job).
Nothing here grants anything.
"""
from __future__ import annotations

from infra.adapters.v2_policy import permissions_for
from infra.adapters.v2_postgres import TenantScopeError

NOT_FOUND = "job not found"
EVIDENCE_KINDS = frozenset({"source.correspondence", "code.diff_proposal", "ci.semantic_drift"})       # their own routes require evidence.read
CONFIRMER_KINDS = frozenset({"gap.answer_interpret", "gap.interview_question"})                          # their own routes require knowledge.confirm
THREAD_ONLY_KINDS = frozenset({"request.qa"})                                                              # request assistant: private thread routes only


def _change_readable(store, tenant, ws, change_id: str, authority, perms) -> bool:
    try:
        rec = store.job_record(tenant, ws, change_id)
    except TenantScopeError:
        return False
    return rec.get("kind") == "change.understand" and (rec.get("requested_by") == authority["account_id"] or bool({"knowledge.confirm", "change.approve"} & perms))


def readable(store, tenant: str, ws: str, record: dict, authority: dict) -> bool:
    kind, payload = record.get("kind"), (record.get("payload") or {})
    if kind in THREAD_ONLY_KINDS:
        return False                                                                        # only the private thread routes read or act on these (RQA-02)
    perms = permissions_for(authority.get("roles"))
    if kind == "change.understand":
        if not _change_readable(store, tenant, ws, record["job_id"], authority, perms):
            return False
    change_id = payload.get("change_id")
    if isinstance(change_id, str) and change_id.startswith("chg-") and not _change_readable(store, tenant, ws, change_id, authority, perms):
        return False
    if kind in CONFIRMER_KINDS and "knowledge.confirm" not in perms:
        return False
    if kind in EVIDENCE_KINDS and "evidence.read" not in perms:
        return False
    if kind == "ci.semantic_drift":
        from infra.adapters import v2_code_change
        if not v2_code_change.repo_visible(ws, payload.get("repo_id", ""), authority.get("roles")):
            return False
    if kind == "roi.analyst" and record.get("requested_by") != authority["account_id"] and "knowledge.confirm" not in perms:
        return False
    if kind == "intent.interpret":
        cid = payload.get("conversation_id")
        if isinstance(cid, str):
            from infra import v2_chat_store
            from infra.v2_chat_routes import _visible
            conv = v2_chat_store.get(store, tenant, ws, cid)
            if conv is None or not _visible(authority, conv):
                return False
    return True


def shaped(record: dict) -> dict:
    """What the generic read returns of a readable record: the ROI analyst's records are never returned (contract SUR-07), even to its asker."""
    if record.get("kind") == "roi.analyst" and isinstance(record.get("payload"), dict) and "records" in record["payload"]:
        record = dict(record, payload={k: v for k, v in record["payload"].items() if k != "records"} | {"records_withheld": len(record["payload"]["records"] or [])})
    return record
