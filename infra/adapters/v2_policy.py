"""The single centralized permission evaluator.

Every route that reads restricted data or mutates workspace state must call
`require` (or `require_any`) with the permission that authorizes it. The
caller's authority always comes from `PostgresIdentityStore.current_authority`
(or an equivalent server-side session recheck) -- it is never taken from a
request body or header field named actor/role/tenant. This module is the only
place role bundles are mapped to permissions, so the mapping cannot drift
between routes.
"""

from __future__ import annotations


class PermissionDenied(PermissionError):
    """The current, freshly-resolved authority does not grant this permission."""


# Demo role bundles. Roles are server-managed (granted only through
# PostgresIdentityStore.grant_membership by an account holding
# membership.manage); a client never selects its own role.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "requester": frozenset({
        "project.read", "source.import", "knowledge.answer", "intent.create", "change.prepare",
        # Production role model: the requester uses the AI under the workspace policy (no per-reply grant), confirms the
        # understanding of THEIR OWN request (never anyone else's, never knowledge), and starts an apply only after a valid approval.
        "ai.use", "intent.confirm_own", "change.apply",
        # Extension (local only): build and validate a candidate code patch on a disposable clone; never approve, publish or deploy it.
        "code.patch",
        # `change.approve` is NOT part of the default requester bundle (production role split: the requester creates, the approver decides).
        # An account that must both request and decide (the relaxed-mode demo policy) holds BOTH role bundles; the distinct-approver rule
        # still compares accounts, so holding two roles never lets anyone decide their own request in the strict default.
    }),
    "knowledge_owner": frozenset({
        "project.read", "knowledge.confirm", "conflict.resolve",
        # Raw source-quote evidence in the human work queue is
        # gated separately from "project.read" -- only a role that can
        # actually confirm/resolve knowledge may see the underlying quotes.
        "evidence.read",
    }),
    "change_approver": frozenset({
        "project.read", "change.approve", "change.accept_result", "ai.use",
        # Extension (local only): approve or reject a candidate code patch bound to its validation (never their own request).
        "code.approve",
        # Extension ma/1 (local build): start, advance or cancel a multi-agent review board over a confirmed change; the report is read, never an approval.
        "ma.run",
        # The operator-supplied acceptance contract -- expected values of a proposal
        # come from an authenticated approver/admin, never from the requester's own output.
        "verification.contract.set",
    }),
    # A person who may only LOOK (every read surface of the workspace), nothing else: no import, no AI run, no request, no decision, no restore.
    "viewer": frozenset({"project.read"}),
    # A reviewer reads the proposal, its evidence, impact, simulation and gaps, may use the AI to help read them, and records a review note
    # (comment / request changes / ready for approval). No approve, no apply, no import, no request of their own.
    "reviewer": frozenset({"project.read", "evidence.read", "ai.use", "change.review", "ma.run"}),
    # Marker role: removes ai.use from the member's other roles (a per-member revoke of AI use). Grants nothing itself.
    "ai_suspended": frozenset(),
    "restore_operator": frozenset({
        "project.read", "source.restore",
    }),
    "admin": frozenset({
        "project.read", "source.import", "knowledge.answer", "intent.create",
        "knowledge.confirm", "conflict.resolve", "change.approve",
        "change.accept_result", "source.restore", "membership.manage",
        "evidence.read", "verification.contract.set",
        "ai.use", "change.apply", "change.prepare",
        # Extension (local only): candidate, approval and the MOCK promotion path; no publish/merge/deploy capability exists for anyone.
        "code.patch", "code.approve", "code.promote", "ma.run",
    }),
    # Demo/judge profile: a person who may state a change in free text, confirm their OWN request, and prepare it up to a proposal that WAITS for a reviewer/approver
    # (draft, impact, simulation, what-if, proposal, AI reading). No source import, no analysis/extraction/re-extraction, no review/approval bundle, no apply, no membership, no operator switches.
    "judge": frozenset({"project.read", "intent.create", "intent.confirm_own", "ai.use", "change.prepare"}),
}

# Only these role names may ever be granted through the public membership
# routes; this is the policy ceiling for granting and revoking roles. "admin" (which alone carries
# membership.manage) is deliberately excluded: it is only ever provisioned by
# the private bootstrap/demo-provisioning calls, never by an authenticated
# member through the public grant route -- an admin granting "admin" over
# HTTP would be an unaudited privilege-escalation ceiling with no distinct
# operator step.
GRANTABLE_ROLES = frozenset(ROLE_PERMISSIONS) - {"admin"}

# Which permission gates each job kind's creation (preview) and approval step.
# A restore job is gated by source.restore at both steps: "Preview và approve
# rollback qua cùng gate" (00_CONTRACTS.md §4).
JOB_CREATE_PERMISSION = {"change": "intent.create", "restore": "source.restore",
                         "analysis": "source.import",
                         # Reads one already-published, ACL-scoped
                         # source artifact exactly like "analysis" does, so it
                         # is gated by the same permission.
                         "extraction": "source.import",
                         # Reads authorized artifacts like "extraction"; machine answer, not the
                         # human `knowledge.answer` (/v2/hitl/answer).
                         "gap.answer": "source.import",
                         "impact.judge": "source.import",
                         # Consume already-persisted "extraction"
                         # candidates (no new source read); no specific permission is named for them,
                         # so both are gated by the same
                         # "source.import" ceiling as their upstream "extraction"
                         # input, not a laxer one.
                         "relation": "source.import",
                         "finding": "source.import",
                         # Consumes an already-persisted decision
                         # event + existing candidate ids (no new source
                         # read, no artifact_id in payload) -- gated by the
                         # same "source.import" ceiling as relation/finding,
                         # not the stricter analysis/extraction storage gate
                         # (it does not read a fresh source
                         # blob, so that gate does not apply here).
                         "invalidation": "source.import",
                         # Consumes already-persisted extraction/
                         # relation/finding job output (no new source read,
                         # no live model call) -- gated by the same
                         # "source.import" ceiling as its siblings.
                         "impact": "change.prepare",
                         # Pitch: deterministic draft brief over a confirmed intent + extraction/impact output
                         # (no source read of its own beyond the pinned revision, no model call): same ceiling.
                         "change.brief": "change.prepare",
                         # A2: GenAI grounded draft: reads the pinned revision + extraction output, one model call.
                         "change.draft": "change.prepare",
                         # Consumes an already-persisted impact-job
                         # report + caller-supplied deterministic corpus/
                         # candidate (no new source read, no live model
                         # call) -- gated by the same "source.import"
                         # ceiling as its sibling "impact".
                         "simulation": "change.prepare",
                         # Consumes already-persisted "impact" +
                         # "simulation" job output (no new source read, no
                         # live model call) -- gated by the same
                         # "source.import" ceiling as its siblings.
                         "whatif": "change.prepare",
                         # Consumes an already-confirmed intent +
                         # already-persisted "impact"/"simulation" job
                         # output (no new source read, no live model call)
                         # -- gated by the same "source.import" ceiling as
                         # its siblings.
                         "proposal": "change.prepare",
                         # Consumes an already-persisted, COMPLETED
                         # "proposal" job's own real result (no new source
                         # read, no live model call, never writes a patch or
                         # approval) -- gated by the same "source.import"
                         # ceiling as its siblings, not the legacy
                         # `api_contract.py` "review.run" naming registry's
                         # "intent.create" (that registry is never wired to
                         # this real dispatch surface; `proposal` itself did
                         # not copy it either).
                         "review": "source.import",
                         # Consumes an already-persisted, COMPLETED
                         # "proposal" + "review" job pair (no new source
                         # read, no live model call, never writes a patch/
                         # approval -- a pure snapshot digest/readiness
                         # read) -- gated by the same "source.import"
                         # ceiling as its siblings.
                         "approval": "source.import",
                         # Consumes an already-persisted, COMPLETED
                         # "approval" job's own real readiness/digest (no new
                         # source read at submission time, no live model
                         # call, never publishes) -- gated by the same
                         # "source.import" ceiling as its siblings.
                         "apply": "change.apply"}
JOB_APPROVE_PERMISSION = {"change": "change.approve", "restore": "source.restore",
                          "analysis": "source.import",
                          "extraction": "source.import",
                          "gap.answer": "source.import",
                          "impact.judge": "source.import",
                          "relation": "source.import",
                          "finding": "source.import",
                          "invalidation": "source.import",
                          "impact": "change.prepare",
                          "change.brief": "change.prepare",
                         # A2: GenAI grounded draft: reads the pinned revision + extraction output, one model call.
                         "change.draft": "change.prepare",
                          # UX: free-text interpretation; created only by the chat route (absent from JOB_CREATE_PERMISSION).
                          "intent.interpret": "ai.use",
                          # UX: stored wording of a verified result; created only by /v2/explanations (absent from JOB_CREATE_PERMISSION).
                          "explain": "ai.use",
                          # Final: business Q&A over a context pack; created only by POST /v2/business/questions.
                          "knowledge.qa": "ai.use",
                          "change.understand": "ai.use",
                          "change.impact_deps": "ai.use",
                          # Final (KNO-06): gaps and relations a model suggests over one knowledge version; created only by POST /v2/business/gaps/propose.
                          "knowledge.gap_proposals": "ai.use",
                          # Extension ext/1 (local only, deterministic): candidate / validation / mock promotion / verification / rollback jobs
                          "code.candidate": "code.patch", "code.validate": "code.patch", "code.promote": "code.promote", "code.verify": "code.promote", "code.rollback": "code.promote",
                          "git.sync": "source.import", "git.fetch": "source.import", "code.publish": "code.approve",
                          # Extension ma/1: one specialist call of a multi-agent run (created only by the coordinator) and the deterministic join step
                          "ma.specialist": "ai.use", "ma.join": "ma.run", "ma.sandbox": "code.patch", "ma.simulate": "change.prepare",
                          # Final (KNO-07): a person's free-text gap answer read into the structured form; created only by POST /v2/business/gaps/{gap_id}/interpret.
                          "gap.answer_interpret": "ai.use",
                          # Extension AIX-D1: an audience variant of a generated document (pinned role document.wording); created only by POST .../documents/{artifact_id}/variants.
                          "document.variant": "ai.use",
                          # Extension AIX-K1: one rule group of a knowledge version compared for conflict hypotheses (pinned role knowledge.mining); created only by POST /v2/business/mining.
                          "knowledge.mining": "ai.use",
                          # Extension AIX-K2 model questioner: the next question of an interview session (pinned role gap.interview); created only by the interview refresh.
                          "gap.interview_question": "ai.use",
                          # Final: model guidance over the server-built draft of a confirmed change; created only by POST .../draft/guidance (change.prepare + ai.use).
                          "change.draft_guidance": "change.prepare",
                          # R3: stored AI reading of a simulation result; created only by /v2/simulation-analyses.
                          "sim.analysis": "ai.use",
                          "simulation": "change.prepare",
                          "whatif": "change.prepare",
                          "proposal": "change.prepare",
                          "review": "source.import",
                          "approval": "source.import",
                          "apply": "change.apply",
                          # Created only by an import commit (never by
                          # POST /v2/jobs); cancel/retry need source.import.
                          "source_ingest": "source.import",
                          # Final: created only by POST /v2/business/documents (never by POST /v2/jobs); cancel/retry need project.read.
                          "document.generate": "project.read",
                          # Final: created only by POST /v2/business/changes/{id}/source/apply (never by POST /v2/jobs); cancel/retry need change.apply.
                          "business.source_apply": "change.apply",
                          # Created only by the SOURCE_PUBLISHED
                          # consumer (absent from JOB_CREATE_PERMISSION, so
                          # POST /v2/jobs answers 400 for both); a FAILED
                          # stage may be retried under the same permission
                          # that later attests the result (api_contract.py
                          # `verification.retry`).
                          "verification": "change.accept_result",
                          "km_rebuild": "change.accept_result"}


# Approval SUBJECT -> permission. `JOB_*_PERMISSION` above are
# keyed by job KIND; the same `apply` kind now carries two subjects, so each
# step that authorizes a human decision maps the subject explicitly and every
# step re-applies it (decision, apply-job creation, the in-transaction
# authority recheck at publish, result acceptance). No role bundle is widened:
# a CHANGE holder can never act on a RESTORE and vice versa.
APPROVAL_SUBJECT_PERMISSION = {"CHANGE": "change.approve", "RESTORE": "source.restore"}
APPLY_CREATE_SUBJECT_PERMISSION = {"CHANGE": "change.apply", "RESTORE": "source.restore"}
RESULT_ACCEPT_SUBJECT_PERMISSION = {"CHANGE": "change.accept_result",
                                    "RESTORE": "source.restore"}


def permissions_for(roles) -> frozenset[str]:
    """Union of permissions for a membership's role bundle. Unknown role names
    grant nothing -- an unrecognized role must never fail open."""
    granted: set[str] = set()
    for role in roles or ():
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    if "ai_suspended" in (roles or ()):
        granted.discard("ai.use")
    return frozenset(granted)


def require(authority: dict, permission: str) -> None:
    """Raise unless the authority's CURRENT roles grant this permission.

    `authority` must be the dict returned by a fresh server-side lookup
    (`current_authority`/`resolve_context`), never anything read from the
    request body or headers.
    """
    if permission not in permissions_for(authority.get("roles")):
        raise PermissionDenied(permission)


def require_human_actor(authority: dict) -> None:
    """Confirmations/approvals/decisions are human-only; a service/job identity
    must never satisfy this check by claiming to be the deciding actor."""
    if authority.get("actor_kind") != "human":
        raise PermissionDenied("human actor required")


def require_distinct_approver(requester_id: str, approver_id: str, *,
                              require_distinct: bool) -> None:
    """Self-approval policy, both modes.

    When `require_distinct` is True (the pilot default), the requester of a
    change/restore may never also be its approver, even holding the approval
    permission. When False (demo-only relaxation), self-approval is allowed
    but the caller MUST still write an audit event recording it -- callers
    enforce that separately; this function only gates the decision itself.
    """
    if require_distinct and requester_id == approver_id:
        raise PermissionDenied("distinct approver required")


# Base path of the manual source-refresh routes (infra/v2_source_routes.py);
# composed here so the concrete route strings live in exactly one production module.
_SOURCE_REFRESH = "/v2/sources/refresh"

# Route registry matrix: (method, path template) -> permission. This is the
# single source of truth a wiring audit can walk against the live FastAPI
# app to confirm every mutating/restricted v2 route is actually enforced
# (see tests/security/test_ai2_t02_permission.py::test_route_registry_matches_app).
ROUTE_PERMISSIONS: dict[tuple[str, str], str | None] = {
    ("POST", "/v2/jobs"): None,  # dispatches per job kind; see JOB_CREATE_PERMISSION
    ("GET", "/v2/jobs/{job_id}"): "project.read",
    ("POST", "/v2/jobs/{job_id}/approve"): None,  # per job kind; see JOB_APPROVE_PERMISSION
    ("POST", "/v2/jobs/{job_id}/publish"): None,  # rechecked against the recorded approver
    ("POST", "/v2/jobs/{job_id}/cancel"): None,  # per job kind; see JOB_APPROVE_PERMISSION
    ("POST", "/v2/jobs/{job_id}/retry"): None,  # per job kind; see JOB_APPROVE_PERMISSION
    ("POST", "/v2/knowledge/confirm"): "knowledge.confirm",
    ("POST", "/v2/membership/grant"): "membership.manage",
    ("POST", "/v2/membership/revoke"): "membership.manage",
    ("GET", "/v2/audit/{workspace_id}"): "project.read",
    # Source admission (infra/v2_source_routes.py).
    ("POST", "/v2/sources/imports"): "source.import",
    ("GET", "/v2/sources/imports/{import_id}"): "project.read",
    ("POST", "/v2/sources/imports/{import_id}/commit"): "source.import",
    ("GET", "/v2/sources/revisions/{revision_id}"): "project.read",
    # Manual refresh: an external OBSERVATION (never a ChangeSet), so the
    # import permission gates prepare/commit; the status read is project.read.
    ("POST", _SOURCE_REFRESH + "/prepare"): "source.import",
    ("POST", _SOURCE_REFRESH + "/commit"): "source.import",
    ("GET", _SOURCE_REFRESH + "/{refresh_id}"): "project.read",
    # Human work queue -- listing is project.read (evidence itself
    # is redacted per-caller inside the DTO by the "evidence.read"
    # permission, not gated as a separate route-level ceiling); answering is
    # the existing "knowledge.answer" permission (already granted to
    # "requester", never to "knowledge_owner" -- the answerer/confirmer
    # split this keeps).
    ("GET", "/v2/hitl/queue"): "project.read",
    ("POST", "/v2/hitl/answer"): "knowledge.answer",
    # Fuller append-only decision ledger + authority semantics.
    ("POST", "/v2/conflict/resolve"): "conflict.resolve",
    ("POST", "/v2/decisions/supersede"): "knowledge.confirm",
    ("POST", "/v2/decisions/revoke"): "knowledge.confirm",
    ("POST", "/v2/gap/accepted_unknown"): "knowledge.confirm",
    # Intent.parse/intent.confirm/stored-intent read.
    ("POST", "/v2/intent/parse"): "intent.create",
    ("POST", "/v2/intent/confirm"): "intent.confirm_own",   # or knowledge.confirm for anyone's request; see the handler
    ("GET", "/v2/intent/{intent_id}"): "project.read",
    # Human content decision on a `kind=approval` job -- distinct
    # from `JOB_APPROVE_PERMISSION["approval"]` ("source.import", the
    # job-EXECUTION gate on `/v2/jobs/{job_id}/approve`). `change.approve`
    # was already reserved for exactly this decision (`ROLE_PERMISSIONS`
    # above) and is exercised by this route.
    ("POST", "/v2/jobs/{job_id}/decision"): "change.approve",
    ("GET", "/v2/jobs/{job_id}/decisions"): "project.read",
    # Change routes (infra/v2_change_routes.py): the change read model, the
    # human result decision (accept / request_changes / reject) and the
    # durable proof of required manual work.
    ("GET", "/v2/changes/{apply_job_id}"): "project.read",
    ("POST", "/v2/changes/{apply_job_id}/result-decision"): "change.accept_result",
    ("POST", "/v2/changes/{apply_job_id}/work-proof"): "change.accept_result",
    # Hand-off: reviewer notes on a proposal and the per-person work inbox (read; what each person may DO is checked on each action).
    ("POST", "/v2/changes/review-note"): "change.review",
    ("GET", "/v2/changes/{proposal_job_id}/review-notes"): "project.read",
    ("GET", "/v2/work/inbox"): "project.read",
    ("GET", "/v2/work/chain"): "project.read",
    # Restore routes (infra/v2_restore_routes.py). The two result-decision /
    # work-proof entries above name the CHANGE subject's permission; a RESTORE
    # apply is authorized by `RESULT_ACCEPT_SUBJECT_PERMISSION["RESTORE"]`
    # (source.restore), resolved from the apply job's own payload.
    ("GET", "/v2/history/{workspace_id}"): "project.read",
    ("GET", "/v2/history/{workspace_id}/diff"): "project.read",
    ("POST", "/v2/restore/preview"): "source.restore",
    ("POST", "/v2/restore/approve"): "source.restore",
    # Operator-supplied verification contract (infra/v2_verification_routes.py).
    ("POST", "/v2/verification/contracts"): "verification.contract.set",
    ("GET", "/v2/verification/contracts"): "project.read",
    ("GET", "/v2/verification/suggestion"): "project.read",
    ("GET", "/v2/change/coverage"): "project.read",
    # Business knowledge graph and gap queue (infra/v2_business_routes.py): reads, and the deterministic analysis that only proposes.
    ("GET", "/v2/business/graph"): "project.read",
    ("GET", "/v2/business/gaps"): "project.read",
    ("POST", "/v2/business/analyse"): "knowledge.confirm",
    ("POST", "/v2/business/gaps/answer-preview"): "knowledge.confirm",
    ("POST", "/v2/business/gaps/answer"): "knowledge.confirm",
    ("POST", "/v2/business/gaps/propose"): "knowledge.confirm",
    ("POST", "/v2/business/gaps/{gap_id}/interpret"): "knowledge.confirm",
    ("GET", "/v2/business/gaps/{gap_id}/interpret/{job_id}"): "knowledge.confirm",
    ("GET", "/v2/business/knowledge"): "project.read",
    ("GET", "/v2/business/diff"): "project.read",
    ("POST", "/v2/business/context"): "project.read",
    ("POST", "/v2/business/questions"): "knowledge.answer",
    ("POST", "/v2/business/changes"): "intent.create",
    ("GET", "/v2/business/changes/{change_id}"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/confirm"): "intent.confirm_own",
    ("GET", "/v2/business/changes/{change_id}/impact"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/impact/dependencies"): "intent.create",
    ("POST", "/v2/business/changes/{change_id}/impact/simulate"): "change.prepare",
    # Extension ext/1 (local only)
    ("GET", "/v2/business/changes/{change_id}/code/candidate"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/code/candidate"): "code.patch",
    ("POST", "/v2/business/changes/{change_id}/code/candidate/validate"): "code.patch",
    ("POST", "/v2/business/changes/{change_id}/code/candidate/decide"): "code.approve",
    ("POST", "/v2/business/changes/{change_id}/code/candidate/publish"): "code.approve",
    ("POST", "/v2/business/changes/{change_id}/code/candidate/promote"): "code.promote",
    ("POST", "/v2/business/changes/{change_id}/code/candidate/verify"): "code.promote",
    ("POST", "/v2/business/changes/{change_id}/code/candidate/rollback"): "code.promote",
    # Governance (local build)
    ("POST", "/v2/business/policy/decide"): "project.read",
    ("POST", "/v2/business/audit/export"): "project.read",
    ("POST", "/v2/business/roi/replay"): "project.read",
    ("POST", "/v2/ma/runs"): "ma.run",
    ("GET", "/v2/ma/runs/{run_id}"): "project.read",
    ("POST", "/v2/ma/runs/{run_id}/advance"): "ma.run",
    ("POST", "/v2/ma/runs/{run_id}/cancel"): "ma.run",
    ("GET", "/v2/business/changes/{change_id}/ma"): "project.read",
    ("POST", "/v2/business/gaps/{gap_id}/interview"): "knowledge.confirm",
    ("GET", "/v2/business/interviews/{session_id}"): "knowledge.confirm",
    ("POST", "/v2/business/interviews/{session_id}/answer"): "knowledge.confirm",
    ("POST", "/v2/business/interviews/{session_id}/refresh"): "knowledge.confirm",
    ("POST", "/v2/business/interviews/{session_id}/confirm"): "knowledge.confirm",
    ("POST", "/v2/business/documents/{artifact_id}/variants"): "project.read",
    ("GET", "/v2/business/documents/{artifact_id}/variants"): "project.read",
    ("GET", "/v2/business/documents/variants/{variant_id}"): "project.read",
    ("GET", "/v2/business/documents/variants/{variant_id}/content"): "project.read",
    ("POST", "/v2/business/mining"): "knowledge.confirm",
    ("GET", "/v2/business/mining"): "knowledge.confirm",
    ("POST", "/v2/business/mining/hypotheses/{hypothesis_id}/dismiss"): "knowledge.confirm",
    ("POST", "/v2/auth/external/links"): "membership.manage",
    ("POST", "/v2/auth/external/links/revoke"): "membership.manage",
    ("GET", "/v2/auth/external/links"): "membership.manage",
    ("GET", "/v2/projection/status"): "project.read",
    ("GET", "/v2/projection/feeds"): "project.read",
    ("POST", "/v2/projection/feed-token"): "project.read",
    ("POST", "/v2/projection/run"): "membership.manage",
    ("POST", "/v2/notifications/tokens"): "project.read",
    ("POST", "/v2/notifications/tokens/revoke"): "project.read",
    ("GET", "/v2/notifications/inbox"): "project.read",
    ("POST", "/v2/notifications/inbox/{item_id}/read"): "project.read",
    ("POST", "/v2/notifications/dispatch"): "membership.manage",
    ("POST", "/v2/projection/conversations"): "membership.manage",
    ("GET", "/v2/business/documents/{artifact_id}/offline-manifest"): "project.read",
    ("POST", "/v2/sources/git/sync"): "source.import",
    ("GET", "/v2/sources/repositories"): "project.read",
    ("GET", "/v2/business/changes/{change_id}/draft"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/draft/guidance"): "change.prepare",
    ("GET", "/v2/business/changes/{change_id}/review"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/review/decide"): "change.approve",
    ("POST", "/v2/business/changes/{change_id}/review/clarify"): "intent.confirm_own",
    ("POST", "/v2/business/changes/{change_id}/commit"): "knowledge.confirm",
    ("GET", "/v2/business/changes/{change_id}/code"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/code/apply"): "project.read",
    ("GET", "/v2/business/changes/{change_id}/source"): "project.read",
    ("GET", "/v2/business/changes/{change_id}/journey"): "project.read",
    ("POST", "/v2/business/changes/{change_id}/source/apply"): "project.read",  # ai mode additionally needs change.apply in the adapter
    ("POST", "/v2/business/documents"): "project.read",
    ("GET", "/v2/business/documents"): "project.read",
    ("GET", "/v2/business/documents/{artifact_id}"): "project.read",
    ("GET", "/v2/business/documents/{artifact_id}/content"): "project.read",
    ("GET", "/v2/business/documents/{artifact_id}/download"): "project.read",
    ("POST", "/v2/business/documents/export"): "project.read",  # technical needs evidence.read in the route
    ("GET", "/v2/business/questions/{question_id}"): "project.read",
    ("POST", "/v2/business/accept"): "knowledge.confirm",
    ("POST", "/v2/business/decisions/revoke"): "knowledge.confirm",
    ("POST", "/v2/change/coverage/ensure"): "change.prepare",
    ("GET", "/v2/verification/contracts/{contract_id}"): "project.read",
}
