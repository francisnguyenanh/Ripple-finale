"""Extension contract ext/1 (FEX-03..08), LOCAL ONLY: the read model and the human decisions of a candidate code patch for a committed business change.

Candidate, validation, promotion, verification and rollback are deterministic jobs (`infra/worker/code_change.py`) with ids derived from their inputs (`ensure_job`);
approvals, PR intents and the mock promotion/verification/rollback records are rows of the append-only ledger `v2_ext_code_decisions`. Publishing (push, PR, merge) and any
real deployment are refused: no grant exists in this build, and the refusal is recorded as the PR intent that WOULD have been published.
"""
from __future__ import annotations

import uuid

from psycopg2.extras import Json

from infra.adapters.v2_postgres import TenantScopeError, _as_object
from infra.adapters.v2_restore import read_snapshot
from infra.worker import code_change as cc

AUDIT_EVENT = "EXT_CODE_DECISION"
MAX_REASON = 600


class CodeChangeRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _job(store, tenant_id, workspace_id, job_id):
    try:
        return store.job_record(tenant_id, workspace_id, job_id)
    except TenantScopeError:
        return None


def _state(rec) -> str:
    if rec is None:
        return "not_requested"
    st = rec.get("execution_status")
    return {"COMPLETED": "completed", "FAILED": "failed", "CANCELLED": "failed"}.get(st) or "pending"


def _result(rec):
    return rec.get("execution_result") if rec and rec.get("execution_status") == "COMPLETED" else None


# ---- ledger ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def ledger(store, tenant_id, workspace_id, change_id, candidate_job_id, *, kind, actor_id, reason, patch_hash, validation_hash, body) -> dict:
    decision_id = "ecd-" + uuid.uuid4().hex[:20]
    with store._tx() as cur:
        cur.execute("""INSERT INTO v2_ext_code_decisions (tenant_id, workspace_id, change_id, candidate_job_id, decision_id, kind, actor_id, reason, patch_hash, validation_hash, body)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, change_id, candidate_job_id, decision_id, kind, actor_id, reason, patch_hash, validation_hash, Json(body)))
        cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)",
                    (tenant_id, workspace_id, AUDIT_EVENT, actor_id, Json({"change_id": change_id, "candidate_job_id": candidate_job_id, "decision_id": decision_id, "kind": kind,
                                                                          "patch_hash": patch_hash, "validation_hash": validation_hash})))
    return {"decision_id": decision_id, "kind": kind, "actor_id": actor_id}


def ledger_rows(store, tenant_id, workspace_id, candidate_job_id) -> list:
    with read_snapshot(store) as cur:
        cur.execute("""SELECT decision_id, kind, actor_id, reason, patch_hash, validation_hash, body, created_at FROM v2_ext_code_decisions
                       WHERE tenant_id=%s AND workspace_id=%s AND candidate_job_id=%s ORDER BY created_at, decision_id""", (tenant_id, workspace_id, candidate_job_id))
        return [{"decision_id": r[0], "kind": r[1], "actor_id": r[2], "reason": r[3], "patch_hash": r[4], "validation_hash": r[5], "body": _as_object(r[6]) or {}, "created_at": r[7].isoformat()}
                for r in cur.fetchall()]


def current_approval(store, tenant_id, workspace_id, candidate_job_id, patch_hash) -> dict | None:
    """The latest approve/reject row for this patch hash; approve only when it is the latest terminal row."""
    rows = [r for r in ledger_rows(store, tenant_id, workspace_id, candidate_job_id) if r["kind"] in ("approve", "reject") and r["patch_hash"] == patch_hash]
    if not rows or rows[-1]["kind"] != "approve":
        return None
    return rows[-1]


# ---- requests (jobs) ---------------------------------------------------------------------------------------------------------------------------------------------------------

def _recommendation_hash(store, tenant_id, workspace_id, change_id):
    from infra.adapters import v2_business, v2_business_review
    from core.change_intelligence.v2 import code_recommendation
    got = v2_business_review.current_draft(store, tenant_id, workspace_id, change_id)
    if got is None:
        raise CodeChangeRefused("not_confirmed", "confirm the request first")
    draft, _w, parts = got
    head = store.head(tenant_id, workspace_id)
    current = v2_business._working(v2_business.business_view(store, tenant_id, workspace_id))
    rec = code_recommendation.build_recommendations(draft, parts["intent"]["operations"], head={"source_revision_id": head.source_revision_id, "snapshot_id": current["snapshot"]["snapshot_id"] if current else None})
    rec["stale"], rec["stale_reasons"] = binding_moved(store, tenant_id, workspace_id, change_id, draft["pin"], head, current)
    return rec


def binding_moved(store, tenant_id, workspace_id, change_id, pin, head, current) -> tuple[bool, list]:
    """The patch binds to the decided knowledge: the commit of THIS change moves the knowledge by design and is not staleness; a moved source revision or a later knowledge
    version is (the recommendations' observed logic may be out of date)."""
    from infra.adapters import v2_business_commit
    reasons = []
    if head.source_revision_id != pin.get("source_revision_id"):
        reasons.append("the source files moved after this change was confirmed: the observed logic may be out of date")
    committed = v2_business_commit.committed_state(store, tenant_id, workspace_id, change_id)
    current_id = current["snapshot"]["snapshot_id"] if current else None
    if current_id not in (pin.get("snapshot_id"), (committed or {}).get("snapshot_id")):
        reasons.append("the business knowledge moved again after this change was committed")
    return bool(reasons), reasons


def repo_declared(workspace_id, repo_id) -> bool:
    try:
        cc.repo_config(workspace_id, repo_id)
        return True
    except Exception:
        return False


def check_candidate(store, tenant_id, workspace_id, change_id, *, repo_id) -> dict:
    """The state conditions of building a candidate (shared by the action and its `can` hint); returns the recommendation binding."""
    from infra.adapters import v2_business_commit
    if repo_id is not None and not repo_declared(workspace_id, repo_id):
        raise CodeChangeRefused("repo_not_declared", "no declared repository with that id for this workspace")
    if v2_business_commit.committed_state(store, tenant_id, workspace_id, change_id) is None:
        raise CodeChangeRefused("knowledge_not_committed", "commit the knowledge change first; a patch implements decided knowledge only")
    rec = _recommendation_hash(store, tenant_id, workspace_id, change_id)
    if rec["stale"]:
        raise CodeChangeRefused("stale_binding", "; ".join(rec["stale_reasons"]))
    return rec


def request_candidate(store, tenant_id, workspace_id, change_id, *, repo_id, actor_id) -> dict:
    from infra.worker.verification import ensure_job
    rec = check_candidate(store, tenant_id, workspace_id, change_id, repo_id=repo_id)
    jid = cc.candidate_job_id(change_id, repo_id, rec["recommendation_hash"])
    created = ensure_job(store, tenant_id, workspace_id, jid, "code.candidate", {"change_id": change_id, "repo_id": repo_id, "requested_by": actor_id})
    return {"job_id": jid, "created": created, "status": _state(_job(store, tenant_id, workspace_id, jid))}


def _candidate(store, tenant_id, workspace_id, change_id, candidate_job_id):
    cand = _job(store, tenant_id, workspace_id, candidate_job_id)
    if cand is None or cand.get("kind") != "code.candidate" or (cand.get("payload") or {}).get("change_id") != change_id:
        raise TenantScopeError("candidate not found")
    return cand


def check_validation(store, tenant_id, workspace_id, change_id, *, candidate_job_id) -> dict:
    """The state conditions of validating (shared by the action and its `can` hint)."""
    cand = _candidate(store, tenant_id, workspace_id, change_id, candidate_job_id)
    res = _result(cand)
    if res is None:
        raise CodeChangeRefused("candidate_not_completed", "the candidate is not ready")
    if not res.get("diff"):
        raise CodeChangeRefused("nothing_to_validate", "the candidate holds no change")
    return cand


def request_validation(store, tenant_id, workspace_id, change_id, *, candidate_job_id, actor_id) -> dict:
    from infra.worker.verification import ensure_job
    check_validation(store, tenant_id, workspace_id, change_id, candidate_job_id=candidate_job_id)
    jid = cc.validation_job_id(candidate_job_id)
    created = ensure_job(store, tenant_id, workspace_id, jid, "code.validate", {"candidate_job_id": candidate_job_id, "requested_by": actor_id})
    return {"job_id": jid, "created": created, "status": _state(_job(store, tenant_id, workspace_id, jid))}


def check_decide(store, tenant_id, workspace_id, change_id, *, candidate_job_id, actor_id, action, patch_hash, validation_hash) -> dict:
    """The state conditions of approving/rejecting (shared by the action and its `can` hint); returns the candidate result."""
    cand = _candidate(store, tenant_id, workspace_id, change_id, candidate_job_id)
    res = _result(cand)
    if res is None or not res.get("patch_hash"):
        raise CodeChangeRefused("candidate_not_completed", "the candidate holds no patch to decide")
    if (cand.get("payload") or {}).get("requested_by") == actor_id or cand.get("requested_by") == actor_id:
        raise CodeChangeRefused("distinct_approver", "the person who asked for the patch cannot decide it")
    val = _result(_job(store, tenant_id, workspace_id, cc.validation_job_id(candidate_job_id)))
    if res["patch_hash"] != patch_hash or val is None or val.get("validation_hash") != validation_hash:
        raise CodeChangeRefused("stale_binding", "the patch or its validation is not the one you saw; look again")
    if action == "approve" and val.get("status") != "PASSED":
        raise CodeChangeRefused("validation_not_passed", "only a candidate whose validation passed can be approved")
    from infra.adapters import v2_business_commit
    committed = v2_business_commit.committed_state(store, tenant_id, workspace_id, change_id)
    if committed is None or committed["snapshot_id"] != res["target_pin"]["snapshot_id"]:
        raise CodeChangeRefused("knowledge_moved", "the decided knowledge the patch implements is no longer the committed one")
    for row in ledger_rows(store, tenant_id, workspace_id, candidate_job_id):
        if row["kind"] in ("approve", "reject") and row["patch_hash"] == patch_hash and row["validation_hash"] == validation_hash:
            raise CodeChangeRefused("already_decided", f"this candidate and validation were already decided ({row['kind']})")
    return res


def decide(store, tenant_id, workspace_id, change_id, *, candidate_job_id, actor_id, action, reason, patch_hash, validation_hash) -> dict:
    """A human approves or rejects a candidate bound to BOTH hashes; refused when anything differs from what is current, when validation did not pass, or when the person asked for the patch."""
    if action not in ("approve", "reject"):
        raise CodeChangeRefused("invalid_field", "action must be approve or reject")
    reason = (reason or "").strip()
    if not reason or len(reason) > MAX_REASON:
        raise CodeChangeRefused("invalid_field", "a reason in the person's own words is required")
    res = check_decide(store, tenant_id, workspace_id, change_id, candidate_job_id=candidate_job_id, actor_id=actor_id, action=action,
                       patch_hash=patch_hash, validation_hash=validation_hash)
    return ledger(store, tenant_id, workspace_id, change_id, candidate_job_id, kind=action, actor_id=actor_id, reason=reason, patch_hash=patch_hash, validation_hash=validation_hash,
                  body={"base_sha": res["base_sha"], "target_pin": res["target_pin"]})


def publish_grant(store, tenant_id, workspace_id, change_id, candidate_job_id) -> bool:
    """True when the candidate's repository declares a remote with the `pull_request` grant (operator configuration; never inferred)."""
    from infra.worker import git_remote
    cand = _job(store, tenant_id, workspace_id, candidate_job_id)
    if cand is None or cand.get("kind") != "code.candidate" or (cand.get("payload") or {}).get("change_id") != change_id:
        raise TenantScopeError("candidate not found")
    try:
        return git_remote.has_grant(cc.repo_config(workspace_id, cand["payload"]["repo_id"]), "pull_request")
    except Exception:                                                                       # noqa: BLE001 - an undeclared or invalid remote is no grant
        return False


def check_publish(store, tenant_id, workspace_id, change_id, *, candidate_job_id) -> tuple:
    """The state conditions of publishing (shared by both publish paths and the `can` hint): a completed candidate with an approval bound to its patch."""
    cand = _candidate(store, tenant_id, workspace_id, change_id, candidate_job_id)
    res = _result(cand)
    if res is None:
        raise CodeChangeRefused("candidate_not_completed", "the candidate is not ready")
    approval = current_approval(store, tenant_id, workspace_id, candidate_job_id, res["patch_hash"])
    if approval is None:
        raise CodeChangeRefused("not_approved", "an approval bound to this patch is required before anything is published")
    return res, approval


def request_publish(store, tenant_id, workspace_id, change_id, *, candidate_job_id, actor_id) -> dict:
    """With a `pull_request` grant: the code.publish job for the current approval (one job per candidate + validation; the worker re-checks every binding)."""
    from infra.worker import git_remote
    from infra.worker.verification import ensure_job
    res, approval = check_publish(store, tenant_id, workspace_id, change_id, candidate_job_id=candidate_job_id)
    jid = git_remote.publish_job_id(candidate_job_id, approval["validation_hash"])
    created = ensure_job(store, tenant_id, workspace_id, jid, git_remote.PUBLISH_KIND, {"candidate_job_id": candidate_job_id, "requested_by": actor_id})
    return {"job_id": jid, "created": created, "status": _state(_job(store, tenant_id, workspace_id, jid)), "branch": f"ripple/{change_id}-{res['patch_hash'][:12]}"}


def publish(store, tenant_id, workspace_id, change_id, *, candidate_job_id, actor_id) -> dict:
    """What WOULD be published without a grant: refused; the intent is recorded once per approval."""
    res, approval = check_publish(store, tenant_id, workspace_id, change_id, candidate_job_id=candidate_job_id)
    intent = {"branch": f"ripple/{change_id}", "title": f"Implement business change {change_id}", "patch_hash": res["patch_hash"], "base_sha": res["base_sha"], "target_pin": res["target_pin"],
              "validation_hash": approval["validation_hash"], "approved_by": approval["actor_id"], "status": "REFUSED", "reason": "no_publish_grant"}
    existing = [r for r in ledger_rows(store, tenant_id, workspace_id, candidate_job_id) if r["kind"] == "pr_intent" and r["validation_hash"] == approval["validation_hash"]]
    if not existing:
        ledger(store, tenant_id, workspace_id, change_id, candidate_job_id, kind="pr_intent", actor_id=actor_id, reason="publish requested; refused: no publish grant exists in this build",
               patch_hash=res["patch_hash"], validation_hash=approval["validation_hash"], body=intent)
    return intent


def request_mock(store, tenant_id, workspace_id, change_id, *, candidate_job_id, environment_id, actor_id, kind) -> dict:
    """Promotion, verification or rollback job against the mock target; the worker enforces the grants and bindings again when it runs."""
    from infra.worker.verification import ensure_job
    cand = _job(store, tenant_id, workspace_id, candidate_job_id)
    if cand is None or cand.get("kind") != "code.candidate" or (cand.get("payload") or {}).get("change_id") != change_id:
        raise TenantScopeError("candidate not found")
    res = _result(cand)
    if res is None:
        raise CodeChangeRefused("candidate_not_completed", "the candidate is not ready")
    if kind == "code.promote":
        val = _result(_job(store, tenant_id, workspace_id, cc.validation_job_id(candidate_job_id)))
        approval = current_approval(store, tenant_id, workspace_id, candidate_job_id, res["patch_hash"])
        if val is None or val.get("status") != "PASSED":
            raise CodeChangeRefused("validation_not_passed", "the candidate has no passed validation")
        if approval is None or approval["validation_hash"] != val["validation_hash"]:
            raise CodeChangeRefused("not_approved", "no current approval binds this patch and validation")
        jid = cc.promotion_job_id(candidate_job_id, environment_id)
    elif kind == "code.verify":
        prom = _result(_job(store, tenant_id, workspace_id, cc.promotion_job_id(candidate_job_id, environment_id)))
        if prom is None:
            raise CodeChangeRefused("promotion_not_completed", "promote first")
        jid = cc.verify_job_id(candidate_job_id, environment_id, prom["artifact_hash"])
    else:
        n = sum(1 for r in ledger_rows(store, tenant_id, workspace_id, candidate_job_id) if r["kind"] == "rollback")
        jid = cc.rollback_job_id(candidate_job_id, environment_id, n)
    created = ensure_job(store, tenant_id, workspace_id, jid, kind, {"candidate_job_id": candidate_job_id, "environment_id": environment_id, "requested_by": actor_id})
    return {"job_id": jid, "created": created, "status": _state(_job(store, tenant_id, workspace_id, jid))}


# ---- read model ----------------------------------------------------------------------------------------------------------------------------------------------------------------

def state(store, tenant_id, workspace_id, change_id, *, candidate_job_id=None, can_read_evidence: bool) -> dict:
    """Everything a person needs about the code change of this business change: candidate, validation, approval, PR intent, mock promotions. Reads only."""
    if candidate_job_id is None:
        with read_snapshot(store) as cur:
            cur.execute("""SELECT j.job_id FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='code.candidate' AND j.payload->>'change_id'=%s
                           ORDER BY (SELECT max(o.id) FROM v2_outbox o WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=j.job_id) DESC NULLS LAST, j.job_id LIMIT 1""",
                        (tenant_id, workspace_id, change_id))
            row = cur.fetchone()
        candidate_job_id = row[0] if row else None
    out = {"change_id": change_id, "candidate": None, "validation": None, "approval": None, "pr_intent": None, "pull_request": None, "promotions": [], "verifications": [], "rollbacks": [],
           "boundary": "The candidate is built and validated on a disposable clone of a declared repository; a pull request is opened only when the repository declares a pull_request grant, and nothing is ever merged; real deployment has no grant and the mock target is a local directory."}
    if candidate_job_id is None:
        return out
    cand = _job(store, tenant_id, workspace_id, candidate_job_id)
    if cand is None or cand.get("kind") != "code.candidate":
        raise TenantScopeError("candidate not found")
    res = _result(cand)
    c = {"job_id": candidate_job_id, "status": _state(cand), "reason": cand.get("execution_reason") if cand.get("execution_status") == "FAILED" else None}
    if res:
        c.update({k: res.get(k) for k in ("status", "patch_hash", "base_sha", "repo_id", "target_pin", "implemented", "not_implemented", "statement")})
        c["files"] = [{k: f[k] for k in ("path", "replacements")} if can_read_evidence else {"replacements": f["replacements"]} for f in res.get("files", [])]
        if can_read_evidence:
            c["diff"] = res.get("diff")
        else:
            c["not_implemented"] = [{"ref": i["ref"], "reason": i["reason"]} for i in res.get("not_implemented", [])]
    out["candidate"] = c
    val = _job(store, tenant_id, workspace_id, cc.validation_job_id(candidate_job_id))
    if val is not None:
        v = {"job_id": val["job_id"], "status": _state(val), "reason": val.get("execution_reason") if val.get("execution_status") == "FAILED" else None}
        vr = _result(val)
        if vr:
            v.update({k: vr.get(k) for k in ("status", "validation_hash", "patch_hash", "base_sha", "runtime_digest", "isolation", "statement")})
            v["commands"] = [{"argv": x["argv"], "exit": x["exit"], "seconds": x["seconds"], **({"output_tail": x["output_tail"]} if can_read_evidence else {})} for x in vr.get("commands", [])]
            v["report_status"] = vr.get("status")
        out["validation"] = v
    rows = ledger_rows(store, tenant_id, workspace_id, candidate_job_id)
    if res:
        out["approval"] = current_approval(store, tenant_id, workspace_id, candidate_job_id, res["patch_hash"])
    out["decisions"] = [{k: r[k] for k in ("decision_id", "kind", "actor_id", "reason", "patch_hash", "validation_hash", "created_at")} for r in rows]
    out["pr_intent"] = next((r["body"] for r in reversed(rows) if r["kind"] == "pr_intent"), None)
    out["pull_request"] = next((dict(r["body"], at=r["created_at"]) for r in reversed(rows) if r["kind"] == "pr_published"), None)
    out["promotions"] = [dict(r["body"], at=r["created_at"]) for r in rows if r["kind"] == "promotion"]
    out["verifications"] = [dict(r["body"], at=r["created_at"]) for r in rows if r["kind"] == "verification"]
    out["rollbacks"] = [dict(r["body"], at=r["created_at"]) for r in rows if r["kind"] == "rollback"]
    out["current_binding"] = {"patch_hash": res["patch_hash"] if res else None, "validation_hash": (_result(val) or {}).get("validation_hash") if val else None}
    return out


# ---------------------------------------------------------------- read hints for the interface (ext/1 UI contract)
REPO_SCHEMA = "ripple-repositories/1"


def repo_visible(workspace_id: str, repo_id: str, roles) -> bool:
    """A declared repository of this workspace that this member may see. Optional `visible_to` (role names) narrows it; without it every reader of the
    workspace sees it. An undeclared and a hidden repository are indistinguishable to the caller."""
    ws = (cc.extension_config().get("workspaces") or {}).get(workspace_id) or {}
    repo = (ws.get("repos") or {}).get(repo_id)
    if not isinstance(repo, dict):
        return False
    allowed = repo.get("visible_to")
    return not allowed or bool(set(roles or ()) & set(allowed))


def repositories(workspace_id: str, *, roles, perms) -> dict:
    """The declared repositories this member may see: ids, a display name, local or remote, the declared grants and whether a sync is possible for them.
    Never a path, URL, repository name on the remote host, token variable or token."""
    from infra.worker import git_remote
    ws = (cc.extension_config().get("workspaces") or {}).get(workspace_id) or {}
    out = []
    for rid in sorted((ws.get("repos") or {})):
        if not repo_visible(workspace_id, rid, roles):
            continue
        raw = ws["repos"][rid]
        try:
            repo = cc.repo_config(workspace_id, rid)
            ready, why = True, None
        except Exception:                                                                  # noqa: BLE001 - a declaration that cannot be used is reported, not raised
            repo, ready, why = dict(raw), False, "repo_not_ready"
        remote = git_remote.has_remote(repo)
        try:
            grants = {"read": (not remote) or git_remote.has_grant(repo, "read"), "pull_request": remote and git_remote.has_grant(repo, "pull_request")}
        except Exception:                                                                  # noqa: BLE001 - an invalid remote block grants nothing
            grants, ready, why = {"read": False, "pull_request": False}, False, "remote_config_invalid"
        sync = (None if "source.import" in perms else "permission_missing") or (None if ready else why) or (None if grants["read"] else "no_read_grant")
        out.append({"repo_id": rid, "name": str(raw.get("display_name") or rid), "source": "remote" if remote else "local",
                    "provider": (raw.get("remote") or {}).get("provider") if remote else None, "grants": grants, "ready": ready, "unavailable_reason": why,
                    "sync": {"allowed": sync is None, "reason_code": sync}})
    return {"schema": REPO_SCHEMA, "workspace_id": workspace_id, "configured": bool(ws.get("repos")), "repositories": out,
            "statement": "Repositories are declared by the operator. This list shows only those you may see; it never shows locations or credentials."}


def _hint(fn) -> dict:
    try:
        fn()
        return {"allowed": True, "reason_code": None, "reason": None}
    except CodeChangeRefused as exc:
        return {"allowed": False, "reason_code": exc.code, "reason": str(exc)}
    except TenantScopeError:
        return {"allowed": False, "reason_code": "no_candidate", "reason": "no candidate exists for this change yet"}


def action_hints(store, tenant_id, workspace_id, change_id, *, authority, perms, state_out: dict, repo_id=None) -> dict:
    """What this person could do next on the code candidate, from the SAME checks the actions run (check_*), at the state just read. A hint for the
    interface, never an authorisation: every action checks permission and state again and may still be refused."""
    from infra.adapters.v2_policy import PermissionDenied, require_human_actor
    actor = authority.get("account_id")
    try:
        require_human_actor(authority)
        human = True
    except PermissionDenied:
        human = False
    cand = (state_out.get("candidate") or {}).get("job_id")
    binding = state_out.get("current_binding") or {}

    def gate(permission, fn):
        if not human:
            return {"allowed": False, "reason_code": "human_actor_required", "reason": "a person must do this"}
        if permission not in perms:
            return {"allowed": False, "reason_code": "permission_missing", "reason": f"your role does not include {permission}"}
        if repo_id is not None and fn is create and not repo_visible(workspace_id, repo_id, authority.get("roles")):
            return {"allowed": False, "reason_code": "repo_not_declared", "reason": "no declared repository with that id for this workspace"}
        return _hint(fn)

    def need_cand(f):
        def g():
            if cand is None:
                raise TenantScopeError("no candidate")
            f()
        return g

    def create():
        check_candidate(store, tenant_id, workspace_id, change_id, repo_id=repo_id)

    def publish_check():
        check_publish(store, tenant_id, workspace_id, change_id, candidate_job_id=cand)
        if not publish_grant(store, tenant_id, workspace_id, change_id, cand):
            raise CodeChangeRefused("no_publish_grant", "the repository declares no pull_request grant: a publish request is recorded as an intent only")

    out = {
        "create_candidate": gate("code.patch", create),
        "validate": gate("code.patch", need_cand(lambda: check_validation(store, tenant_id, workspace_id, change_id, candidate_job_id=cand))),
        "approve": gate("code.approve", need_cand(lambda: check_decide(store, tenant_id, workspace_id, change_id, candidate_job_id=cand, actor_id=actor, action="approve",
                                                                      patch_hash=binding.get("patch_hash"), validation_hash=binding.get("validation_hash")))),
        "reject": gate("code.approve", need_cand(lambda: check_decide(store, tenant_id, workspace_id, change_id, candidate_job_id=cand, actor_id=actor, action="reject",
                                                                     patch_hash=binding.get("patch_hash"), validation_hash=binding.get("validation_hash")))),
        "publish": gate("code.approve", need_cand(publish_check)),
    }
    return {"for_candidate": cand, "binding": {"patch_hash": binding.get("patch_hash"), "validation_hash": binding.get("validation_hash")}, "repo_id": repo_id,
            "actions": out, "statement": "A hint read with the state above; every action is checked again when it is sent and may be refused if anything changed."}
