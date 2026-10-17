"""The change read model, the human result decision and the
closed-loop completion gate.

Everything here is derived from durable rows written by the reviewed chain:
the apply job and its `SOURCE_PUBLISHED` publication, the
`verification` report and `km_rebuild` result
(`infra/worker/verification.py`), the KM head / index manifests, the
approval decision ledger and the tables added by
`infra/migrations/ai8_t04_result_decision.sql`.

Authority model (contracts §4): the caller's roles / actor kind come from the
route (`infra/v2_change_routes.py`) which resolves them fresh from the session;
nothing in a request body is trusted. The gate below is evaluated INSIDE the
decision transaction, under the workspace-head row lock, so staleness is judged
against the locked head and two simultaneous accepts serialize. A completion is
an ATTESTATION: it writes the decision row, an audit row and one
`CHANGE_COMPLETED` outbox row, and never moves `version`,
`source_revision_id`, `knowledge_revision_id` or `decision_epoch`, never
writes a source / KM revision or a semantic decision, and never edits the
immutable machine report.
"""

from __future__ import annotations

from infra import v2_current_findings as CF
import hashlib
import json
import uuid

import psycopg2
from psycopg2.extras import Json

from core.change_intelligence.v2.lifecycle import (
    AuthorizationError, LifecycleError, TransitionContext, authorize_transition,
)
from core.change_intelligence.v2.store_ports import WorkspaceHead
from infra.adapters.v2_policy import PermissionDenied, RESULT_ACCEPT_SUBJECT_PERMISSION
from infra.adapters.v2_postgres import StoreConflict, TenantScopeError, _as_object

INDEX_VERSION = "v1"
ACTIONS = ("accept", "request_changes", "reject")
MACHINE_FAILED = ("FAILED", "NOT_RUN")


class GateRefused(Exception):
    """The completion gate (or a lifecycle rule) refused; `reasons` are the
    stable codes, `findings` the blocking gap ids (critical_gap_open)."""

    def __init__(self, reasons, findings=()):
        super().__init__(", ".join(reasons))
        self.reasons = list(reasons)
        self.findings = list(findings)


class InvalidWorkIndex(ValueError):
    pass


def apply_subject(store, tenant_id: str, workspace_id: str, apply_job_id: str) -> str:
    """The approval SUBJECT ("CHANGE" | "RESTORE") of an apply job,
    read from its immutable payload. An unknown job is treated as CHANGE (the
    caller then needs `change.accept_result` before any lookup result is
    revealed, exactly as before)."""
    try:
        job = store.job_record(tenant_id, workspace_id, apply_job_id)
    except TenantScopeError:
        return "CHANGE"
    if job.get("kind") == "apply" and isinstance(job.get("payload"), dict)             and job["payload"].get("restore_approval_id") is not None:
        return "RESTORE"
    return "CHANGE"


def verification_job_id(apply_job_id: str) -> str:
    return f"verify-{apply_job_id}"


def rebuild_job_id(apply_job_id: str) -> str:
    return f"rebuild-{apply_job_id}"


# ----------------------------------------------------------------------------- facts

def _job(cur, tenant_id, workspace_id, job_id):
    cur.execute("""SELECT kind, status, execution_status, execution_result, execution_reason,
            payload, pinned_knowledge_revision_id
        FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s""",
                (tenant_id, workspace_id, job_id))
    row = cur.fetchone()
    if row is None:
        return None
    return {"job_id": job_id, "kind": row[0], "status": row[1], "execution_status": row[2],
            "result": _as_object(row[3]), "reason": row[4],
            "payload": _as_object(row[5]) or {}, "pinned_knowledge_revision_id": row[6]}


def _head(cur, tenant_id, workspace_id, lock=False) -> WorkspaceHead:
    cur.execute("""SELECT version, source_revision_id, knowledge_revision_id, decision_epoch,
        policy_version FROM v2_workspaces WHERE tenant_id=%s AND workspace_id=%s"""
                + (" FOR UPDATE" if lock else ""), (tenant_id, workspace_id))
    row = cur.fetchone()
    if row is None:
        raise TenantScopeError("workspace not found")
    return WorkspaceHead(tenant_id, workspace_id, *row)


def _statuses(cur, tenant_id, workspace_id, job_ids):
    if not job_ids:
        return {}
    cur.execute("""SELECT job_id, execution_status FROM v2_jobs
        WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s)""",
                (tenant_id, workspace_id, list(job_ids)))
    return dict(cur.fetchall())


def load_facts(cur, tenant_id, workspace_id, apply_job_id, head):
    apply = _job(cur, tenant_id, workspace_id, apply_job_id)
    if apply is None or apply["kind"] != "apply":
        raise TenantScopeError("change not found")
    apply_result = apply["result"] or {}
    published_revision_id = apply_result.get("published_revision_id") or f"rev-apply-{apply_job_id}"
    cur.execute("""SELECT parent_revision_id FROM v2_source_revisions
        WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, published_revision_id))
    row = cur.fetchone()
    published = row is not None
    parent_revision_id = row[0] if row else None

    verify = _job(cur, tenant_id, workspace_id, verification_job_id(apply_job_id))
    rebuild = _job(cur, tenant_id, workspace_id, rebuild_job_id(apply_job_id))
    stage_conflict = [j["job_id"] for j, k in ((verify, "verification"), (rebuild, "km_rebuild"))
                      if j is not None and j["kind"] != k]
    if verify is not None and verify["kind"] != "verification":
        verify = None
    if rebuild is not None and rebuild["kind"] != "km_rebuild":
        rebuild = None
    report = (verify["result"] if verify and verify["execution_status"] == "COMPLETED"
              and verify["result"] else None)
    rebuild_result = (rebuild["result"] if rebuild and rebuild["execution_status"] == "COMPLETED"
                      and rebuild["result"] else None)

    reanalysis_ids, invalidation_ids, downstream_ids = [], [], []
    if rebuild_result and rebuild_result.get("status") == "COMPLETED":
        reanalysis_ids = list((rebuild_result.get("reanalysis") or {}).get("job_ids") or [])
        invalidation_ids = [i["job_id"] for i in (rebuild_result.get("invalidation") or [])]
        cur.execute("""SELECT reanalysis_job_id FROM v2_reanalysis_causation
            WHERE tenant_id=%s AND workspace_id=%s AND decision_id=%s""",
                    (tenant_id, workspace_id, f"apply-{apply_job_id}"))
        downstream_ids = [r[0] for r in cur.fetchall()]
    job_status = _statuses(cur, tenant_id, workspace_id,
                           reanalysis_ids + invalidation_ids + downstream_ids)
    # Provider attempts each failed re-analysis job has used, against the approved limit: a job that used them all is refused by the worker AND by the
    # retry route, so the read model must not present it as something that can still run or be retried.
    attempts_used, attempt_limit = {}, None
    failed_ids = [j for j in reanalysis_ids + invalidation_ids + downstream_ids if job_status.get(j) == "FAILED"]
    if failed_ids:
        cur.execute("""SELECT job_id, count(*) FROM v2_job_attempts
            WHERE tenant_id=%s AND workspace_id=%s AND job_id = ANY(%s) AND provider_calls > 0 GROUP BY job_id""",
                    (tenant_id, workspace_id, failed_ids))
        attempts_used = {r[0]: int(r[1]) for r in cur.fetchall()}
        try:
            from infra.worker import job_quota
            attempt_limit = job_quota.provider_attempt_limit()
        except Exception:                      # no readable budget config: the page then keeps the retry it had
            attempt_limit = None

    cur.execute("""SELECT decision_id, action, actor_id, reason, report_digest,
            new_proposal_job_id, published_revision_id, knowledge_revision_id, created_at, acceptance_scope
        FROM v2_change_result_decisions
        WHERE tenant_id=%s AND workspace_id=%s AND apply_job_id=%s""",
                (tenant_id, workspace_id, apply_job_id))
    row = cur.fetchone()
    decision = None if row is None else {
        "decision_id": row[0], "action": row[1], "actor_id": row[2], "reason": row[3],
        "report_digest": row[4], "new_proposal_job_id": row[5],
        "published_revision_id": row[6], "knowledge_revision_id": row[7],
        "created_at": row[8].isoformat(), "acceptance_scope": row[9]}
    cur.execute("""SELECT item_id, item_kind, disposition, reason, actor_id, created_at, basis FROM v2_change_item_dispositions
        WHERE tenant_id=%s AND workspace_id=%s AND apply_job_id=%s ORDER BY item_id""", (tenant_id, workspace_id, apply_job_id))
    dispositions_recorded = [{"item_id": r[0], "item_kind": r[1], "disposition": r[2], "reason": r[3], "actor_id": r[4], "at": r[5].isoformat(), "basis": r[6], "decided_by": "person"} for r in cur.fetchall()]

    cur.execute("""SELECT work_index, evidence_ref, actor_id, created_at FROM v2_change_work_proofs
        WHERE tenant_id=%s AND workspace_id=%s AND apply_job_id=%s ORDER BY work_index""",
                (tenant_id, workspace_id, apply_job_id))
    proofs = {r[0]: {"evidence_ref": r[1], "actor_id": r[2], "recorded_at": r[3].isoformat()}
              for r in cur.fetchall()}

    if report is not None:
        manual_items = [dict(m) for m in (report.get("manual_work") or [])]
    else:
        manual_items = [{"index": i, "action": e.get("action"), "reason": e.get("reason"),
                         "proof": None}
                        for i, e in enumerate(apply_result.get("pending_human_work") or [])]

    scope_ids = list((apply_result.get("verification_scope") or {}).get("target_artifact_ids")
                     or ([apply_result["target_artifact_id"]]
                         if apply_result.get("target_artifact_id") else []))

    cur.execute("""SELECT status FROM v2_index_builds
        WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""",
                (tenant_id, workspace_id, published_revision_id))
    row = cur.fetchone()
    index_build_status = row[0] if row else None

    candidate = ((rebuild_result or {}).get("knowledge_candidate_revision_id")
                 if rebuild_result and rebuild_result.get("status") == "COMPLETED" else None)
    if candidate:
        # A published knowledge revision is immutable and stands for ONE decision epoch. If a person recorded a decision after it was published (the head moved on to a later epoch),
        # that version no longer describes the workspace and cannot be published again under the same id: the candidate for the CURRENT epoch is a new id, which a person can accept.
        cur.execute("""SELECT decision_epoch FROM v2_knowledge_revisions WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""", (tenant_id, workspace_id, candidate))
        published_epoch = cur.fetchone()
        if published_epoch is not None and published_epoch[0] != head.decision_epoch:
            candidate = f"{candidate}-e{head.decision_epoch}"
    km_ids = [k for k in (candidate, (decision or {}).get("knowledge_revision_id"),
                          head.knowledge_revision_id) if k]
    km_manifest = {}
    if km_ids:
        cur.execute("""SELECT knowledge_revision_id, status FROM v2_index_manifests
            WHERE tenant_id=%s AND workspace_id=%s AND index_version=%s
              AND knowledge_revision_id = ANY(%s)""",
                    (tenant_id, workspace_id, INDEX_VERSION, km_ids))
        km_manifest = dict(cur.fetchall())

    cur.execute("""SELECT count(DISTINCT published_revision_id) FROM v2_change_result_decisions
        WHERE tenant_id=%s AND workspace_id=%s AND action='accept'""",
                (tenant_id, workspace_id))
    verified_version = cur.fetchone()[0]

    approval_job_id = (apply["payload"] or {}).get("approval_job_id")
    approval = _job(cur, tenant_id, workspace_id, approval_job_id) if approval_job_id else None
    approval_result = (approval or {}).get("result") or {}
    approval_decision_id = None
    if approval_job_id:
        cur.execute("""SELECT decision_id FROM v2_change_decisions
            WHERE tenant_id=%s AND workspace_id=%s AND job_id=%s AND action='approve'
              AND approval_digest=%s ORDER BY id LIMIT 1""",
                    (tenant_id, workspace_id, approval_job_id,
                     (apply["payload"] or {}).get("expected_approval_digest")))
        row = cur.fetchone()
        approval_decision_id = row[0] if row else None

    # Which findings are in force and which of them concern THIS change: one shared read model (`infra/v2_current_findings.py`), the same one the Questions list uses.
    findings_model = CF.build(cur, tenant_id, workspace_id)
    findings_view = CF.classify_for_change(cur, tenant_id, workspace_id, findings_model, approval_result.get("proposal_job_id"), scope_ids)
    blocking_evidence = {}
    for v in findings_view["blocking"]:
        rows = []
        for c in v.get("claims", [])[:3]:
            info = findings_model.claims.get(c.get("candidate_id")) or {}
            for e in ((info.get("c") or {}).get("evidence") or [])[:2]:
                if isinstance(e, dict) and e.get("quote"):
                    loc = e.get("locator")
                    if isinstance(loc, str) and loc.lstrip().startswith("{"):
                        try:
                            loc = json.loads(loc)
                        except ValueError:
                            pass
                    loc_text = (loc.get("key_path") or loc.get("type")) if isinstance(loc, dict) else (str(loc) if loc else None)      # a locator is an object, or plain text in older readings
                    rows.append({"file": (c.get("logical_path") or "").split("/")[-1], "rule": c.get("subject"), "locator": loc_text, "quote": str(e["quote"])[:300]})
        blocking_evidence[v["finding_id"]] = rows
    open_gaps = [v["finding_id"] for v in findings_view["blocking"]]
    resolved_gaps = [v["finding_id"] for v in findings_view["resolved"]]

    facts = {
        "findings_model_counts": findings_model.counts, "findings_view": findings_view, "blocking_evidence": blocking_evidence, "dispositions_recorded": dispositions_recorded, "decision_basis_pending": True,
        "apply_job_id": apply_job_id, "apply": apply, "apply_result": apply_result,
        "head": head, "published": published, "published_revision_id": published_revision_id,
        "parent_revision_id": parent_revision_id, "verify": verify, "rebuild": rebuild,
        "report": report, "rebuild_result": rebuild_result,
        "reanalysis_ids": reanalysis_ids, "invalidation_ids": invalidation_ids,
        "downstream_ids": downstream_ids, "job_status": job_status,
        "attempts_used": attempts_used, "attempt_limit": attempt_limit,
        "stage_conflict": stage_conflict,
        "decision": decision, "proofs": proofs, "manual_items": manual_items,
        "scope_ids": scope_ids, "open_gaps": open_gaps, "resolved_gaps": resolved_gaps,
        "index_build_status": index_build_status, "candidate": candidate,
        "km_manifest": km_manifest, "verified_version": verified_version,
        "approval_job_id": approval_job_id,
        "proposal_job_id": approval_result.get("proposal_job_id"),
        "review_job_id": approval_result.get("review_job_id"),
        "approval_decision_id": approval_decision_id,
    }
    facts["decision_basis"] = decision_basis(facts)
    return facts


# ----------------------------------------------------------------------------- analysis

def analyze(f):
    head, report, decision = f["head"], f["report"], f["decision"]
    stale = bool(f["published"] and decision is None
                 and head.source_revision_id != f["published_revision_id"])
    rebuild, rebuild_result = f["rebuild"], f["rebuild_result"]
    if rebuild is None:
        re_status = "NOT_STARTED"
    elif rebuild["execution_status"] in ("QUEUED", "RUNNING"):
        re_status = "PENDING"
    elif rebuild["execution_status"] != "COMPLETED":
        re_status = "FAILED"
    elif (rebuild_result or {}).get("status") == "SUPERSEDED":
        re_status = "SUPERSEDED"
    else:
        statuses = [f["job_status"].get(j) for j in
                    f["reanalysis_ids"] + f["invalidation_ids"] + f["downstream_ids"]]
        if any(s in ("FAILED", "CANCELLED") for s in statuses):
            re_status = "FAILED"
        elif all(s == "COMPLETED" for s in statuses):
            re_status = "COMPLETED"
        else:
            re_status = "RUNNING"
    plan = (rebuild_result or {}).get("reanalysis") or {}
    reanalysis = {"status": re_status, "scope": plan.get("scope"), "reason": plan.get("reason"),
                  "job_ids": list(f["reanalysis_ids"]),
                  "invalidation_job_ids": list(f["invalidation_ids"]),
                  "downstream_job_ids": list(f["downstream_ids"])}
    failed = [j for j in f["reanalysis_ids"] + f["invalidation_ids"] + f["downstream_ids"] if f["job_status"].get(j) == "FAILED"]
    limit = f["attempt_limit"]
    exhausted = [j for j in failed if limit is not None and f["attempts_used"].get(j, 0) >= limit]
    reanalysis.update({"attempt_limit": limit,
                       "exhausted_job_ids": exhausted,
                       "retryable_job_ids": [j for j in failed if j not in exhausted],
                       # TERMINAL: a failed job has no provider attempt left, so this re-analysis can never complete and the change cannot be completed.
                       # The published source and the verification report are untouched; the human answers are Request changes or Reject.
                       "terminal": bool(exhausted)})
    if rebuild is not None and rebuild["execution_status"] == "FAILED":
        reanalysis["reason"] = rebuild.get("reason")

    candidate = f["candidate"] if re_status == "COMPLETED" else None
    if decision is not None and decision["action"] == "accept":
        km_id, accepted = decision["knowledge_revision_id"], True
    else:
        km_id = candidate
        accepted = bool(candidate and head.knowledge_revision_id == candidate and not stale)
    km_index_status = f["km_manifest"].get(km_id) if (accepted and km_id) else None
    if f["index_build_status"] is not None:
        retrieval_status = f["index_build_status"]
    elif rebuild is not None and rebuild["execution_status"] == "FAILED":
        retrieval_status = "FAILED"
    else:
        retrieval_status = "PENDING"
    if stale:
        km_status = "STALE"
    elif accepted:
        km_status = "ACCEPTED"
    elif candidate:
        km_status = "PENDING_HUMAN"
    else:
        km_status = "NOT_READY"
    knowledge = {"candidate_revision_id": candidate, "status": km_status, "accepted": accepted,
                 "accepted_revision_id": km_id if accepted else None,
                 "retrieval_index_status": retrieval_status, "km_index_status": km_index_status}

    manual_open = [m["index"] for m in f["manual_items"] if m["index"] not in f["proofs"]]
    status = (report or {}).get("status")
    machine_ok = bool(report and (status == "PASSED"
                                  or (status == "PARTIAL" and f["manual_items"])))
    return {"stale": stale, "reanalysis": reanalysis, "knowledge": knowledge,
            "manual_open": manual_open, "machine_ok": machine_ok, "report_status": status,
            "km_id": km_id if accepted else None}


DISPOSITIONS = ("accepted_limit", "judged_unrelated", "unhandled")
MIN_REASON = 10


def items_needing_disposition(f) -> dict[str, str]:
    """item id -> kind ("finding" | "reading") of everything the person who accepts the result must decide ITEM BY ITEM: serious findings with evidence of a possible effect and readings elsewhere
    that state a limit of the same kind. A finding that is already resolved is not here."""
    fv = f["findings_view"]
    out = {v["finding_id"]: "finding" for v in fv.get("possible_effect", [])}
    out.update({x["item_id"]: "reading" for x in fv.get("same_concept", [])})
    return out


def item_meta(f) -> dict[str, dict]:
    """item id -> what the decider was shown about it (kind, file, severity, why it is listed). Stored with the decision as the BASIS of the person's judgement."""
    fv = f["findings_view"]
    out = {}
    for v in fv.get("possible_effect", []):
        sl = CF.slim(v)
        out[v["finding_id"]] = {"kind": "finding", "file": sl.get("file"), "severity": sl.get("severity"), "rule": sl.get("rule"), "listed_because": "evidence of a possible effect (AI judgement or text scan)"}
    for x in fv.get("same_concept", []):
        out[x["item_id"]] = {"kind": "reading", "file": x.get("logical_path"), "severity": None, "rule": x.get("subject"), "value": x.get("value"), "stance": x.get("stance"),
                             "listed_because": "states the same concept with a number (" + str(x.get("stance")) + ")"}
    return out


def decision_basis(f) -> str:
    """Digest of the state the person reviewed: which items need a decision, on which source revision, decision epoch and verification report. The client echoes it; a different state
    (a new finding, a new revision, another decision in between, a new report) makes the old answer stale instead of silently applying it to something else."""
    head, report = f["head"], f["report"] or {}
    body = {"items": sorted(item_meta(f).items()), "published_revision": f["published_revision_id"], "head_revision": head.source_revision_id,
            "epoch": head.decision_epoch, "report": report.get("report_digest")}
    return "dbasis-" + hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


def duplicate_ids(raw) -> set[str]:
    seen, dup = set(), set()
    for d in raw or []:
        i = d.get("item_id") if isinstance(d, dict) else None
        if isinstance(i, str):
            (dup if i in seen else seen).add(i)
    return dup


def normalise_dispositions(raw) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in raw or []:
        if isinstance(d, dict) and isinstance(d.get("item_id"), str):
            out[d["item_id"]] = {"disposition": d.get("disposition"), "reason": (d.get("reason") or "").strip() if isinstance(d.get("reason"), str) else ""}
    return out


def acceptance_scope_of(need, given) -> str:
    """FULL when every item that may affect the change is accepted as a limit or judged unrelated; NARROW when any is left unhandled (then the acceptance covers the change itself, not those items)."""
    return "NARROW" if any((given.get(i) or {}).get("disposition") == "unhandled" for i in need) else "FULL"


def gate_reasons(f, a, *, action="accept", expected_digest=None, for_view=False, dispositions=None, basis_given=None, duplicates=()):
    """Stable codes of every failing gate (empty => eligible). `request_changes`
    / `reject` need only an existing report bound to the presented digest."""
    reasons = []
    report = f["report"]
    if not f["published"]:
        reasons.append("source_not_published")
    if f["stage_conflict"]:
        reasons.append("stage_job_id_conflict")
    if report is None:
        reasons.append("report_missing")
    elif expected_digest is not None and expected_digest != report.get("report_digest"):
        reasons.append("report_digest_mismatch")
    if action != "accept":
        return reasons
    if a["stale"]:
        reasons.append("report_stale")
    if report is not None and not a["machine_ok"]:
        reasons.append("machine_verification_not_pass")
    if a["manual_open"]:
        reasons.append("manual_work_unproven")
    if a["reanalysis"]["status"] != "COMPLETED":
        reasons.append("reanalysis_incomplete")
    k = a["knowledge"]
    if not k["accepted"]:
        reasons.append("knowledge_not_accepted")
    if k["retrieval_index_status"] != "READY" or (k["accepted"]
                                                  and k["km_index_status"] != "READY"):
        reasons.append("index_not_ready")
    if f["open_gaps"]:
        reasons.append("critical_gap_open")
    if f["findings_view"]["unchecked_related"]:
        reasons.append("findings_not_rechecked")      # the changed file was read again but nobody checked the new readings yet (a deterministic check, no AI)
    if not for_view:
        # serious open findings with EVIDENCE of a possible effect on this change are never ignored: the person who accepts the result names each one (they are recorded with the decision)
        need = items_needing_disposition(f)
        given = dispositions or {}
        if duplicates:
            reasons.append("disposition_duplicate_item")
        if any(i not in need for i in given):
            reasons.append("disposition_unknown_item")           # an id that is not in force for THIS request and workspace: never silently ignored
        if (need or given) and basis_given != f["decision_basis"]:
            reasons.append("decision_basis_stale")                # the state the person reviewed is not the state in force now
        meta = item_meta(f)
        if any(given.get(i, {}).get("disposition") in ("accepted_limit", "judged_unrelated") and meta[i].get("severity") == "CRITICAL" for i in need):
            reasons.append("disposition_not_allowed_critical")    # a CRITICAL item cannot be waved through with a reason: resolve it, or leave it unhandled (narrow scope)
        if any(i not in given or given[i].get("disposition") not in DISPOSITIONS for i in need):
            reasons.append("items_without_disposition")
        elif any(given[i]["disposition"] in ("accepted_limit", "judged_unrelated") and len(given[i].get("reason") or "") < MIN_REASON for i in need):
            reasons.append("disposition_reason_required")
    return reasons


def derive_state(f, a, eligible):
    decision, report = f["decision"], f["report"]
    if decision is not None:
        return {"accept": "COMPLETED", "request_changes": "STALE",
                "reject": "RECOVERY_REQUIRED"}[decision["action"]]
    apply = f["apply"]
    if apply["execution_status"] in ("FAILED", "CANCELLED"):
        return "FAILED"
    if apply["execution_status"] != "COMPLETED" and not f["published"]:
        return "STAGED"
    if not f["published"]:
        return "APPROVED"
    if a["stale"]:
        return "STALE"
    verify, rebuild = f["verify"], f["rebuild"]
    if verify is None and rebuild is None:
        return "SOURCE_PUBLISHED"
    if verify is not None and verify["execution_status"] in ("FAILED", "CANCELLED"):
        return "FAILED"
    if report is None or a["reanalysis"]["status"] != "COMPLETED":
        return "REANALYSING"
    if a["report_status"] in MACHINE_FAILED:
        return "FAILED"
    if not a["machine_ok"] or a["manual_open"]:
        return "SOURCE_PUBLISHED"
    return "AWAITING_ACCEPTANCE" if eligible else "MACHINE_VERIFIED"


# ----------------------------------------------------------------------------- view

def _roles_with(permission: str) -> list[str]:
    from infra.adapters.v2_policy import ROLE_PERMISSIONS
    return sorted(r for r, perms in ROLE_PERMISSIONS.items() if permission in perms)


def decision_card(v: dict, evidence: list, viewer: dict | None) -> dict:
    """What a person needs to decide ONE blocking finding: the business question, the source passage (only for a role that may read source text), the options with their effect,
    who may decide, and whether the decision edits a source or only records knowledge. Nothing here decides anything."""
    sl = CF.slim(v)
    rule, file = sl.get("rule") or "this rule", sl.get("file") or "the file"
    reason = str(v.get("reason"))
    if reason == "missing_scope":
        question = f"Which orders does the rule \"{rule}\" in {file} apply to? The source does not say (for example only CAPEX purchases, or every purchase)."
        options = [
            {"key": "state_scope", "title": "State where the rule applies",
             "effect": "Recorded as a knowledge decision with your name and reason. No source file is edited. It clears this one finding, so the result is no longer blocked by it; the other findings and the knowledge status are not changed.",
             "edits_source": False, "who": _roles_with("conflict.resolve"), "where": "Questions & Decisions, \"Record where this rule applies\""},
            {"key": "edit_source", "title": "Edit the source so it states the scope",
             "effect": "A new change request through review, approval and apply. The source changes; this finding clears once the file is read again and checked again.",
             "edits_source": True, "who": _roles_with("intent.create"), "where": "Changes"},
            {"key": "leave_open", "title": "Leave it open",
             "effect": "Accept result stays blocked. The approver can still ask for changes or reject the result; nothing is accepted by default.",
             "edits_source": False, "who": _roles_with("change.accept_result"), "where": "this page"},
        ]
    else:
        question = sl.get("plain") or "A check found something that needs a person."
        options = [{"key": "decide", "title": "Decide it where it is listed", "effect": sl.get("todo") or "A person who owns this knowledge looks at it.",
                    "edits_source": False, "who": _roles_with("conflict.resolve"), "where": "Questions & Decisions"}]
    can_read = bool(viewer and viewer.get("can_read_evidence"))
    decider = _roles_with("conflict.resolve")
    return {"question": question, "options": options, "deciders": decider, "you_can_decide": bool(viewer and viewer.get("can_resolve")),
            "evidence": evidence if can_read else [], "evidence_hidden": (not can_read) and bool(evidence),
            "evidence_note": None if can_read else "The source passage is hidden from your role by design; a role that may read source text (reviewer, administrator) sees it."}


def build_view(f, viewer=None):
    a = analyze(f)
    reasons = gate_reasons(f, a, for_view=True)
    decision, report = f["decision"], f["report"]
    # Report-level alias so a reader of the immutable report sees the same code.
    view_reasons = list(reasons)
    if "manual_work_unproven" in view_reasons:
        view_reasons.append("manual_action_unproven")
    eligible = not reasons and decision is None
    state = derive_state(f, a, eligible)
    if decision is not None:
        completion_status = {"accept": "COMPLETED", "request_changes": "CHANGES_REQUESTED",
                             "reject": "REJECTED"}[decision["action"]]
        view_reasons = []
    elif a["stale"]:
        completion_status = "STALE"
    elif eligible:
        completion_status = "AWAITING_ACCEPTANCE"
    else:
        completion_status = "PENDING"

    manual_work = []
    for item in f["manual_items"]:
        entry = dict(item)
        entry["proof"] = f["proofs"].get(item["index"])
        manual_work.append(entry)

    checks = (report or {}).get("checks") or {}
    failed_checks = [{"name": n, "status": c.get("status"), "detail": c.get("detail")}
                     for n, c in checks.items()
                     if c.get("required") and c.get("status") in ("FAIL", "NOT_RUN")]
    residual = ([{"kind": "manual_work", "index": i} for i in a["manual_open"]]
                + [{"kind": "critical_gap", "finding_id": g} for g in f["open_gaps"]])
    scope_ids = f["scope_ids"]
    target_hashes = {}
    if report:
        target_hashes = {aid: (report.get("artifacts") or {}).get(aid, {}).get("blob_hash")
                         for aid in scope_ids}
    parent_hashes = {e["artifact_id"]: e.get("before_blob_hash")
                     for e in ((report or {}).get("actual_edits") or [])}
    result_view = {
        "expected": (report or {}).get("expected_edits") or [],
        "actual": {"values": (report or {}).get("actual_values") or {},
                   "edits": (report or {}).get("actual_edits") or []},
        "simulation": {
            "before": {"source_revision_id": f["parent_revision_id"],
                       "blob_hashes": parent_hashes},
            "after": {"source_revision_id": f["published_revision_id"] if f["published"] else None,
                      "status": ((report or {}).get("simulation_result") or {}).get("status"),
                      "blob_hashes": target_hashes}},
        "conflicts": {"resolved": list(f["resolved_gaps"]), "residual": list(f["open_gaps"])},
        "scope": list(scope_ids),
        "failed_checks": failed_checks,
        "knowledge_delta": ({
            "source": {"from": f["parent_revision_id"], "to": f["published_revision_id"]},
            "knowledge": {"prior": f["apply"].get("pinned_knowledge_revision_id"),
                          "candidate": a["knowledge"]["candidate_revision_id"],
                          "accepted": a["knowledge"]["accepted"]}} if f["published"] else {}),
    }

    fv, counts = f["findings_view"], f["findings_model_counts"]
    findings_block = {"basis": fv["basis"], "blocking": [{**CF.slim(v), "decision": decision_card(v, f.get("blocking_evidence", {}).get(v["finding_id"], []), viewer)} for v in fv["blocking"]], "needs_review": [CF.slim(v) for v in fv["needs_review"][:50]],
                      "needs_review_count": len(fv["needs_review"]), "needs_review_serious": [CF.slim(v) for v in fv["needs_review"] if v.get("severity") in CF.SEVERE and v.get("authority_required")], "possible_effect": [CF.slim(v) for v in fv.get("possible_effect", [])],
                      "same_concept": [{k: x.get(k) for k in ("item_id", "candidate_id", "logical_path", "subject", "value", "shared_words", "stance", "numbers")} for x in fv.get("same_concept", [])],
                      "dispositions": f["dispositions_recorded"], "decision_basis": f["decision_basis"], "resolved_count": len(fv["resolved"]),
                      "unchecked_related": [{"candidate_id": c["candidate_id"], "rule": c["subject"], "file": (c["logical_path"] or "").split("/")[-1]} for c in fv["unchecked_related"]],
                      "unchecked_other_count": fv["unchecked_other"], "workspace": counts}
    knowledge_complete = bool(a["knowledge"]["accepted"] and not fv["needs_review"] and not fv.get("possible_effect") and not fv.get("same_concept") and not fv["blocking"] and not fv["unchecked_related"]
                              and not fv["unchecked_other"] and not counts.get("current_open"))
    execution_status = ("FAILED" if a["report_status"] in MACHINE_FAILED else "VERIFIED" if (report is not None and a["machine_ok"] and not a["manual_open"])
                        else "PARTIAL" if report is not None else "PENDING")
    status_axes = {                                                # three separate facts; none is derived from another
        "execution": {"status": execution_status, "detail": a["report_status"]},
        "request_result": {"status": completion_status, "scope": (decision or {}).get("acceptance_scope"),
                           "unhandled": sum(1 for d in f["dispositions_recorded"] if d["disposition"] == "unhandled"),
                           "limits_accepted": sum(1 for d in f["dispositions_recorded"] if d["disposition"] == "accepted_limit")},
        "knowledge": {"status": "COMPLETE" if knowledge_complete else "PARTIAL", "version_accepted": bool(a["knowledge"]["accepted"]),
                      "needs_review": len(fv["needs_review"]), "possible_effect": len(fv.get("possible_effect", [])), "same_concept": len(fv.get("same_concept", [])), "workspace_open_issues": counts.get("current_open", 0), "not_checked": len(fv["unchecked_related"]) + fv["unchecked_other"]}}

    chain = []

    def add(kind, ref):
        if ref:
            chain.append({"kind": kind, "ref": ref})

    subject = ("RESTORE" if (f["apply"].get("payload") or {}).get("restore_approval_id")
               else "CHANGE")
    add("source_before", f["parent_revision_id"])
    add("restore_approval", (f["apply"].get("payload") or {}).get("restore_approval_id"))
    add("proposal", f["proposal_job_id"])
    add("review", f["review_job_id"])
    add("approval", f["approval_job_id"])
    add("approval_decision", f["approval_decision_id"])
    add("apply", f["apply_job_id"])
    add("source_after", f["published_revision_id"] if f["published"] else None)
    add("verification", (report or {}).get("report_digest"))
    add("knowledge", a["km_id"] or a["knowledge"]["candidate_revision_id"])
    if decision is not None and decision["action"] == "accept":
        add("completion", decision["decision_id"])
    lineage = {
        "chain": chain,
        "source_revision_id": f["published_revision_id"] if f["published"] else None,
        "parent_source_revision_id": f["parent_revision_id"],
        "knowledge_candidate_revision_id": a["knowledge"]["candidate_revision_id"],
        "prior_knowledge_revision_id": f["apply"].get("pinned_knowledge_revision_id"),
        "verified_version": f["verified_version"],
    }
    rebuild_result = f["rebuild_result"] or {}
    return {
        "apply_job_id": f["apply_job_id"],
        "subject": subject,
        "state": state,
        "published_revision_id": f["published_revision_id"] if f["published"] else None,
        "verification": ({"job_id": f["verify"]["job_id"], "status": report["status"],
                          "report_digest": report["report_digest"]} if report else None),
        "reanalysis": a["reanalysis"],
        "knowledge": a["knowledge"],
        "completion": {"status": completion_status, "eligible": eligible,
                       "blocked_reasons": view_reasons, "decision": decision,
                       "acceptance_scope": (decision or {}).get("acceptance_scope")},
        "findings": findings_block,
        "status_axes": status_axes,
        "manual_work": manual_work,
        "alignment": {"global": "NOT_ASSESSED", "scope": list(scope_ids),
                      "residual": residual},
        "result_view": result_view,
        "carry_forward": rebuild_result.get("carry_forward"),
        "counterevidence": rebuild_result.get("counterevidence"),
        "lineage": lineage,
    }


def get_change_view(store, tenant_id, workspace_id, apply_job_id, *, viewer=None):
    with store._tx() as cur:
        head = _head(cur, tenant_id, workspace_id)
        return build_view(load_facts(cur, tenant_id, workspace_id, apply_job_id, head), viewer)


# ----------------------------------------------------------------------------- mutations

def record_result_decision(store, tenant_id, workspace_id, apply_job_id, *, actor_id,
                           actor_kind, permissions, action, reason, expected_digest,
                           idempotency_key=None, dispositions=None, decision_basis=None):
    """One transaction under the workspace-head lock: replay / already_decided
    check, the full gate (accept) or the report-bound check (request_changes /
    reject), the lifecycle authorization, then decision row + audit (+ outbox
    for accept, + follow-up proposal job for request_changes)."""
    if action not in ACTIONS:
        raise ValueError("unsupported action")
    with store._tx() as cur:
        head = _head(cur, tenant_id, workspace_id, lock=True)
        f = load_facts(cur, tenant_id, workspace_id, apply_job_id, head)
        existing = f["decision"]
        if existing is not None:
            if existing["action"] == action and existing["report_digest"] == expected_digest:
                return {"decision_id": existing["decision_id"], "action": action,
                        "apply_job_id": apply_job_id, "report_digest": expected_digest,
                        "replayed": True,
                        "state": derive_state(f, analyze(f), False),
                        "new_proposal_job_id": existing["new_proposal_job_id"]}
            raise GateRefused(["already_decided"])
        # The permission that may decide this result depends
        # on the apply's approval SUBJECT (immutable payload), re-applied here
        # under the head lock -- not only at the route.
        subject = ("RESTORE" if (f["apply"].get("payload") or {}).get("restore_approval_id")
                   else "CHANGE")
        needed = RESULT_ACCEPT_SUBJECT_PERMISSION[subject]
        if needed not in frozenset(permissions):
            raise PermissionDenied(needed)
        a = analyze(f)
        given = normalise_dispositions(dispositions)
        reasons = gate_reasons(f, a, action=action, expected_digest=expected_digest, dispositions=given, basis_given=decision_basis, duplicates=duplicate_ids(dispositions))
        # A request to change / reject an unpublished or stale result is still a
        # legitimate human answer; only the accept path needs the whole gate.
        if action != "accept":
            reasons = [r for r in reasons if r not in ("source_not_published",)]
        if reasons:
            raise GateRefused(reasons, findings=f["open_gaps"]
                              if "critical_gap_open" in reasons else ())
        report = f["report"]
        if action == "accept":
            perms = frozenset(permissions)
            try:
                authorize_transition("change", "AWAITING_ACCEPTANCE", "COMPLETED", TransitionContext(
                    actor_id=actor_id, actor_kind=actor_kind, permissions=perms,
                    current_permissions=perms,
                    expected_revision=f["published_revision_id"],
                    current_revision=head.source_revision_id,
                    policy_version=str(head.policy_version),
                    current_policy_version=str(head.policy_version),
                    decision_epoch=head.decision_epoch,
                    current_decision_epoch=head.decision_epoch,
                    machine_verified=a["machine_ok"],
                    open_required_work=tuple(str(i) for i in a["manual_open"]),
                    operation="source.restore" if subject == "RESTORE" else ""))
            except (LifecycleError, AuthorizationError) as exc:
                raise GateRefused([f"lifecycle_refused: {exc}"]) from exc
        new_proposal_job_id = None
        if action == "request_changes":
            prev_id = f["proposal_job_id"]
            prev = _job(cur, tenant_id, workspace_id, prev_id) if prev_id else None
            if prev is None:
                raise GateRefused(["proposal_missing"])
            payload = dict(prev["payload"])
            payload["action"] = "revise"
            payload["previous_proposal_job_id"] = prev_id
            new_id = f"prop-revise-{apply_job_id}"
            try:
                store._create_job_core(
                    cur, tenant_id, workspace_id, new_id, "proposal", payload, head=head,
                    expected_version=head.version, idempotency_key=f"job-create-{new_id}",
                    requested_by=actor_id)
            except (psycopg2.IntegrityError, StoreConflict) as exc:
                raise GateRefused(["stage_job_id_conflict"]) from exc
            new_proposal_job_id = new_id
        decision_id = "resdec-" + uuid.uuid4().hex
        km_revision = a["km_id"] if action == "accept" else None
        need = items_needing_disposition(f) if action == "accept" else {}
        meta = item_meta(f)
        acceptance_scope = acceptance_scope_of(need, given)
        try:
            cur.execute("""INSERT INTO v2_change_result_decisions
                (tenant_id, workspace_id, apply_job_id, decision_id, action, actor_id, reason,
                 report_digest, published_revision_id, knowledge_revision_id, idempotency_key,
                 new_proposal_job_id, acceptance_scope)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, apply_job_id, decision_id, action, actor_id,
                         reason, report["report_digest"], f["published_revision_id"],
                         km_revision, idempotency_key, new_proposal_job_id, acceptance_scope))
            for item_id, kind in need.items():                       # the item-by-item decisions are part of THIS decision: same transaction, never separate
                cur.execute("""INSERT INTO v2_change_item_dispositions (tenant_id, workspace_id, apply_job_id, item_id, item_kind, disposition, reason, actor_id, decision_basis, basis)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (tenant_id, workspace_id, apply_job_id, item_id, kind, given[item_id]["disposition"], given[item_id]["reason"] or None, actor_id,
                                                                decision_basis, Json({**meta[item_id], "statement": "user_judgement", "not_a_system_conclusion": True})))
        except psycopg2.IntegrityError as exc:
            raise StoreConflict(f"result decision conflict: {exc}") from exc
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,'CHANGE_RESULT_DECISION_RECORDED',%s,%s)""",
                    (tenant_id, workspace_id, actor_id, Json({
                        "apply_job_id": apply_job_id, "decision_id": decision_id,
                        "action": action, "report_digest": report["report_digest"],
                        "published_revision_id": f["published_revision_id"],
                        "new_proposal_job_id": new_proposal_job_id,
                        "acceptance_scope": acceptance_scope,
                        "dispositions": [{"item_id": i, "kind": k, "disposition": given[i]["disposition"]} for i, k in sorted(need.items())]})))
        if action == "accept":
            cur.execute("""INSERT INTO v2_outbox (tenant_id, workspace_id, kind, payload)
                VALUES (%s,%s,'CHANGE_COMPLETED',%s)""",
                        (tenant_id, workspace_id, Json({
                            "apply_job_id": apply_job_id, "decision_id": decision_id,
                            "report_digest": report["report_digest"],
                            "published_revision_id": f["published_revision_id"],
                            "knowledge_revision_id": km_revision, "actor_id": actor_id})))
        return {"decision_id": decision_id, "action": action, "apply_job_id": apply_job_id,
                "report_digest": report["report_digest"], "replayed": False,
                "state": {"accept": "COMPLETED", "request_changes": "STALE",
                          "reject": "RECOVERY_REQUIRED"}[action],
                "new_proposal_job_id": new_proposal_job_id}


def record_work_proof(store, tenant_id, workspace_id, apply_job_id, *, actor_id, work_index,
                      evidence_ref):
    """Durable, idempotent (per index) proof of one required manual item."""
    with store._tx() as cur:
        head = _head(cur, tenant_id, workspace_id, lock=True)
        f = load_facts(cur, tenant_id, workspace_id, apply_job_id, head)
        if f["decision"] is not None:
            raise StoreConflict("already_decided: the result decision is terminal")
        if work_index not in {m["index"] for m in f["manual_items"]}:
            raise InvalidWorkIndex(f"no required manual work item {work_index}")
        prior = f["proofs"].get(work_index)
        if prior is not None:
            if prior["evidence_ref"] != evidence_ref:
                raise StoreConflict("a different proof is already recorded for this item")
            return {"apply_job_id": apply_job_id, "work_index": work_index,
                    "evidence_ref": evidence_ref, "actor_id": prior["actor_id"],
                    "replayed": True}
        cur.execute("""INSERT INTO v2_change_work_proofs
            (tenant_id, workspace_id, apply_job_id, work_index, evidence_ref, actor_id)
            VALUES (%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, apply_job_id, work_index, evidence_ref, actor_id))
        cur.execute("""INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload)
            VALUES (%s,%s,'CHANGE_WORK_PROOF_RECORDED',%s,%s)""",
                    (tenant_id, workspace_id, actor_id, Json({
                        "apply_job_id": apply_job_id, "work_index": work_index,
                        "evidence_ref": evidence_ref})))
        return {"apply_job_id": apply_job_id, "work_index": work_index,
                "evidence_ref": evidence_ref, "actor_id": actor_id, "replayed": False}
