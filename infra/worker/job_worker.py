"""A real worker entry point that claims AND EXECUTES durable jobs.

Lifecycle of one `JOB_QUEUED` outbox claim:

  claim -> provider-call-ceiling admission (prediction table, fail: CANCELLED + ack)
        -> heartbeat (lease lost: LEASE_LOST, untouched)
        -> store.begin_execution: claim-time CONCURRENCY admission + QUEUED->RUNNING,
           one transaction under the workspace lock
             (at limit: release claim without consuming an attempt -> DEFERRED)
             (already terminal: ack -> ALREADY_PROCESSED)
             (cancel requested: CANCELLED + ack)
        -> resolve executor for the job kind
             (none registered: FAILED 'no_executor_registered', non-retryable + ack --
              NEVER a silent no-op success)
        -> executor.prepare(ctx) -> ExecutionRequest
        -> MODEL-DATA POLICY CHECKPOINT (project-scoped, v2_project_model_policy):
             external_provider='deny' + request would reach an external model
               -> FAILED 'model_data_policy_denied' (no call made)
             prompt_storage decides what of the request may be persisted
               (default 'hash_only': hashes only, never raw/excerpted prompt)
        -> PROVIDER-CALL CEILING at the call site: a `ProviderCallBudget` with
           limit = config.max_provider_calls_per_job is handed to the executor,
           which must `consume()` before every provider call; the (N+1)th call
           raises before it is made -> FAILED 'provider_call_ceiling_exceeded'
        -> executor.execute(ctx, request, calls)
             success -> store.finish_execution(COMPLETED, attempt=...) which, in ONE
                        transaction, writes the attempt row (model id, prompt/schema
                        hash, request-context IDs, usage, cost, duration), the
                        terminal execution state, and SETTLES the cost ledger
             retryable failure, retries remaining -> store.record_attempt (cost
                        accrued) and leave the claim CLAIMED (lease expiry reclaims it)
             retryable failure, exhausted / non-retryable -> FAILED + ack

Executors. `BUILTIN_EXECUTORS` holds the real executors for the job kinds
reachable from `POST /v2/jobs`. For `change` and `restore`, content is fully
specified by the client payload that a human then approves, so their
execution is a deterministic, zero-provider-call check that the job's PINNED
inputs still exist and its payload is a canonical mapping, producing a
result bound to the pinned source revision and payload digest. They make no
model call and say so (`model_id=None`, `provider_calls=0`, cost 0) -- no
fabricated model id or usage. `analysis` reads a published source artifact
through the pinned revision and invokes the configured ai_module account with
the priced budget model and its max-output limit. Other kinds (for example
`chunked_extraction`) have no executor and fail closed.

`PROVIDER_EXECUTORS` is the override/injection registry (tests, or a future
real provider integration). A value may be a `JobExecutor`-shaped object or a
bare `callable(job_kind, payload)`; a bare callable is treated as an EXTERNAL
provider attempt that makes `JOB_KIND_REAL_CALLS_PER_ATTEMPT[kind]` calls,
each consumed from the call budget before it runs.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping

from core.change_intelligence.v2.serialize import canonical_hash
from core.change_intelligence.v2.store_ports import OutboxClaim
from infra.adapters.v2_postgres import LeaseLost, PostgresV2Store, StoreConflict
from infra.provider_gate import AI_KINDS as _AI_KINDS
from infra.worker import job_quota
from infra.worker.budget_config import model_for_role
from infra.worker.execution_types import (  # noqa: F401  (re-exported: executors/tests import these from here)
    REASON_CALL_CEILING, ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget, ProviderCallCeilingExceeded,
    ProviderExecutionError, ProviderInvalidResponseError, ProviderRateLimitError,
    ProviderTimeoutError,
)

#: Fallback retry ceiling when no budget config is configured at all (opt-in
#: quota is off, so `config.max_retries` is unavailable).
_DEFAULT_MAX_RETRIES = 2

#: Observable reasons (durable in `v2_jobs.execution_reason` / attempt rows).
REASON_NO_EXECUTOR = "no_executor_registered"
REASON_POLICY_DENIED = "model_data_policy_denied"
REASON_POLICY_NOT_ADMITTED = "model_policy_not_admitted"


def retry_backoff_seconds(attempt: int, exc: Exception) -> int:
    """Bounded exponential backoff with jitter before the NEXT attempt of a job.

    Only transient provider conditions wait (rate limit 429, timeout); a malformed answer retries
    at the next lease reclaim as before. The wait moves the outbox lease, so it never creates an
    extra attempt: the attempt ceiling (`max_retries`), the per-job call ceiling and the budget
    reservation are exactly the ones already enforced. The job stays QUEUED/RUNNING during the
    wait, so it keeps counting against the workspace concurrency pin. Returns whole seconds >= 0.
    Tune with RIPPLE_RETRY_BACKOFF_BASE_SECONDS (rate limit, default 6),
    RIPPLE_RETRY_BACKOFF_TIMEOUT_SECONDS (timeouts, default 0 = immediate) and
    RIPPLE_RETRY_BACKOFF_MAX_SECONDS; 0 disables the wait."""
    try:
        base = float(os.environ.get("RIPPLE_RETRY_BACKOFF_BASE_SECONDS", "6"))
        cap = float(os.environ.get("RIPPLE_RETRY_BACKOFF_MAX_SECONDS", "60"))
        timeout_base = float(os.environ.get("RIPPLE_RETRY_BACKOFF_TIMEOUT_SECONDS", "0"))
    except ValueError:
        base, cap, timeout_base = 6.0, 60.0, 0.0
    if isinstance(exc, ProviderRateLimitError):
        unit = base
    elif isinstance(exc, ProviderTimeoutError):
        unit = timeout_base
    else:
        return 0
    if unit <= 0:
        return 0
    raw = min(cap, unit * (2 ** max(0, attempt - 1)))
    return max(1, int(math.ceil(raw * random.uniform(0.5, 1.0))))


class PinnedInputExecutor:
    """Real executor for `change`/`restore`: verifies the job's pinned
    source revision still exists in this workspace and the payload is a
    canonical mapping; result is bound to both digests. Zero provider calls."""

    external = False

    def __init__(self, kind: str):
        self.kind = kind
        self.executor_id = f"deterministic.pinned_input.{kind}.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
                calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        payload = job["payload"]
        if not isinstance(payload, dict):
            raise NonRetryableExecutionError("invalid_payload", "job payload is not a mapping")
        pinned = job["pinned_source_revision_id"]
        if not pinned or not ctx.store.source_revision_exists(
                job["tenant_id"], job["workspace_id"], pinned):
            raise NonRetryableExecutionError("pinned_source_revision_missing", str(pinned))
        return ExecutionOutcome(result={
            "executor": self.executor_id,
            "pinned_source_revision_id": pinned,
            "pinned_knowledge_revision_id": job["pinned_knowledge_revision_id"],
            "pinned_decision_epoch": job["pinned_decision_epoch"],
            "payload_digest": canonical_hash(payload),
        })


class _InjectedCallableExecutor:
    """Adapter for a bare `callable(job_kind, payload)` placed in
    `PROVIDER_EXECUTORS`: an EXTERNAL provider attempt whose planned call
    count comes from `job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT`."""

    external = True

    def __init__(self, kind: str, fn: Callable):
        self.kind, self.fn = kind, fn
        self.executor_id = f"injected.{getattr(fn, '__name__', 'callable')}"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        text = json.dumps({"kind": self.kind, "payload": ctx.job["payload"]},
                          sort_keys=True, default=str)
        return ExecutionRequest(
            prompt=text, schema=None, external=True,
            provider_calls_planned=job_quota.JOB_KIND_REAL_CALLS_PER_ATTEMPT.get(self.kind, 1))

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
                calls: ProviderCallBudget) -> ExecutionOutcome:
        calls.consume(request.provider_calls_planned)
        outcome = self.fn(self.kind, ctx.job["payload"])
        model_id = outcome.get("model_id") if isinstance(outcome, dict) else None
        return ExecutionOutcome(result={"outcome": outcome} if outcome is not None else {},
                                model_id=model_id)


from infra.worker.change_brief import ChangeBriefExecutor
from infra.worker.change_draft import ChangeDraftExecutor
from infra.worker.apply import ApplyExecutor
from infra.worker.approval import ApprovalExecutor
from infra.worker.extraction import ExtractRulesExecutor
from infra.worker.findings import FindingsExecutor
from infra.worker.gap_answer import GapAnswerExecutor
from infra.worker.change_understand import ChangeUnderstandExecutor
from infra.worker.impact_dependencies import ImpactDependencyExecutor
from infra.worker.gap_answer_interpret import GapAnswerInterpretExecutor
from infra.worker.knowledge_gap_proposals import GapProposalExecutor
from infra.worker.draft_guidance import DraftGuidanceExecutor
from infra.worker.code_change import CodeCandidateExecutor, CodePromoteExecutor, CodeRollbackExecutor, CodeValidateExecutor, CodeVerifyExecutor
from infra.worker.document_generate import DocumentGenerateExecutor
from infra.worker.git_sync import GitSyncExecutor
from infra.worker.git_remote import CodePublishExecutor, GitFetchExecutor
from infra.worker.business_source_apply import BusinessSourceApplyExecutor
from infra.worker.knowledge_qa import KnowledgeQaExecutor
from infra.worker.impact import ImpactAnalysisExecutor
from infra.worker.impact_judge import ImpactJudgeExecutor
from infra.worker.explain import ExplainExecutor
from infra.worker.sim_analysis import SimAnalysisExecutor
from infra.worker.intent_interpret import IntentInterpretExecutor
from infra.worker.invalidation import InvalidationExecutor
from infra.worker.live_analysis import LiveAnalysisExecutor
from infra.worker.proposal import ProposalExecutor
from infra.worker.ma_sandbox import MaSandboxExecutor
from infra.worker.document_variant import DocumentVariantExecutor
from infra.worker.advisory_memo import RiskMemoExecutor, SynthesisMemoExecutor
from infra.worker.ci_semantic_drift import SemanticDriftExecutor
from infra.worker.source_correspondence import SourceCorrespondenceExecutor
from infra.worker.code_diff_proposal import CodeDiffProposalExecutor
from infra.worker.roi_analyst import RoiAnalystExecutor
from infra.worker.request_assistant import RequestAssistantExecutor
from infra.worker.knowledge_mining import KnowledgeMiningExecutor
from infra.worker.gap_interview_question import GapInterviewQuestionExecutor
from infra.worker.ma_simulate import MaSimulateExecutor
from infra.worker.ma_specialist import MaJoinExecutor, MaSpecialistExecutor
from infra.worker.relations import RelationGraphExecutor
from infra.worker.review import ReviewExecutor
from infra.worker.simulation import SimulationExecutor
from infra.worker.source_ingest import SourceIngestExecutor
from infra.worker.verification import (
    KmRebuildExecutor, VerificationExecutor, consume_source_published,
)
from infra.worker.whatif import WhatifPreviewExecutor


BUILTIN_EXECUTORS: dict = {
    "change": PinnedInputExecutor("change"),
    "restore": PinnedInputExecutor("restore"),
    "analysis": LiveAnalysisExecutor(),
    # Structured rule-candidate extraction (RuleCandidate schema).
    "extraction": ExtractRulesExecutor(),
    # One question answered from a pinned, authorized artifact list, or an abstention.
    "gap.answer": GapAnswerExecutor(),
    # Final: a business question answered from a snapshot-pinned context pack, one model call, claims validated by the server.
    "knowledge.qa": KnowledgeQaExecutor(),
    # Final: a business change request understood as validated operations over the pinned snapshot, one model call; changes nothing.
    "change.understand": ChangeUnderstandExecutor(),
    # Final: AI-proposed dependencies of a confirmed change over the pinned snapshot; proposals only, one model call, changes nothing.
    "change.impact_deps": ImpactDependencyExecutor(),
    # Final (KNO-06): gaps and relations a model suggests over one knowledge version; suggestions only, one model call, changes nothing.
    "knowledge.gap_proposals": GapProposalExecutor(),
    # Final (KNO-07): a free-text gap answer read into the structured answer; one model call, previewed, confirmed by the person through the existing route.
    "gap.answer_interpret": GapAnswerInterpretExecutor(),
    # Final: model guidance over the server-built structured draft of a confirmed change; one model call, downgrade-only, changes nothing.
    "change.draft_guidance": DraftGuidanceExecutor(),
    # One model dependency judgment per (target rule, bundle artifact) pair.
    "impact.judge": ImpactJudgeExecutor(),
    # Rule/relation identity + semantic graph over extracted candidates.
    "relation": RelationGraphExecutor(),
    # Semantic conflict/GAP findings over extracted candidates.
    "finding": FindingsExecutor(),
    # Created by an import commit; zero provider calls.
    "source_ingest": SourceIngestExecutor(),
    # Final: one requested Business/Technical document of an accepted snapshot; deterministic, zero provider calls (DOC-02).
    "document.generate": DocumentGenerateExecutor(),
    # Extension ma/1 (local build): one specialist call of a multi-agent run (pinned role ma.specialist) and the deterministic coordinator step.
    "ma.specialist": MaSpecialistExecutor(),
    "ma.join": MaJoinExecutor(),
    # MA-C1: the deterministic sandbox child (typed cases rendered by the server, run on base and patched clones next to the protected tests)
    "ma.sandbox": MaSandboxExecutor(),
    # MA-S1: the deterministic process-simulation child (seeded engine over baseline and validated scenarios)
    "ma.simulate": MaSimulateExecutor(),
    # Extension AIX-D1: one audience variant of a generated document through the pinned role document.wording (1 provider call; server-checked wording or labelled fallback)
    "document.variant": DocumentVariantExecutor(),
    "change.risk_memo": RiskMemoExecutor(),
    "ma.synthesis_memo": SynthesisMemoExecutor(),
    "ci.semantic_drift": SemanticDriftExecutor(),
    "source.correspondence": SourceCorrespondenceExecutor(),
    "code.diff_proposal": CodeDiffProposalExecutor(),
    "roi.analyst": RoiAnalystExecutor(),
    "request.qa": RequestAssistantExecutor(),
    # Extension AIX-K1: one rule group compared for typed conflict hypotheses (pinned role knowledge.mining; 1 provider call; server-verified cases)
    "knowledge.mining": KnowledgeMiningExecutor(),
    # Extension AIX-K2: the model questioner of an interview session (pinned role gap.interview; 1 provider call; server-grounded with a deterministic fallback)
    "gap.interview_question": GapInterviewQuestionExecutor(),
    # Extension ext/1 (local only): candidate patch, validation, mock promotion, verification, rollback; deterministic, 0 provider calls.
    "code.candidate": CodeCandidateExecutor(), "code.validate": CodeValidateExecutor(), "code.promote": CodePromoteExecutor(), "code.verify": CodeVerifyExecutor(), "code.rollback": CodeRollbackExecutor(),
    # Extension ext/1 FEX-01/02 (local only): a declared local repository read into one source refresh revision; deterministic.
    "git.sync": GitSyncExecutor(),
    # Extension ext/1 FEX-01/FEX-05 (product-side connector, explicit grants only): fetch the declared remote branch into the managed mirror; open ONE pull request
    # for an approved, validated candidate (never a merge); deterministic, 0 provider calls.
    "git.fetch": GitFetchExecutor(), "code.publish": CodePublishExecutor(),
    # Final: the approved YAML scalar edits of a committed business change, published as one source revision; deterministic, zero provider calls (SYN-03).
    "business.source_apply": BusinessSourceApplyExecutor(),
    # Dependency-scan + stale-marking + deduplicated reanalysis
    # scheduling over a committed decision event. Zero provider calls (same
    # deterministic shape as "relation"/"finding").
    "invalidation": InvalidationExecutor(),
    # Impact graph traversal + bounded investigation over extracted
    # candidates / the relation-job identity graph / the
    # finding-job output. Zero provider calls (same deterministic shape as
    # "relation"/"finding"/"invalidation").
    "impact": ImpactAnalysisExecutor(),
    # Pitch: deterministic, read-only draft brief (zero provider calls); see infra/worker/change_brief.py.
    "change.brief": ChangeBriefExecutor(),
    # GenAI grounded draft (external, one provider call); see infra/worker/change_draft.py.
    "change.draft": ChangeDraftExecutor(),
    # Free-text request interpretation (external, one provider call); see infra/worker/intent_interpret.py.
    "intent.interpret": IntentInterpretExecutor(),
    # Stored plain-English wording of a verified result (external, one provider call); see infra/worker/explain.py.
    "explain": ExplainExecutor(),
    # Stored AI reading of a simulation result (external, one provider call); see infra/worker/sim_analysis.py.
    "sim.analysis": SimAnalysisExecutor(),
    # Simulation over FIXED internal programs -- resource-limited subprocess (not a security boundary)
    # runner underneath core.change_intelligence.simulation's pure
    # comparison layer. Zero provider calls (same deterministic shape as
    # "impact"/"relation"/"finding").
    "simulation": SimulationExecutor(),
    # What-if preview / risk-readiness report over an already-
    # persisted "impact" + "simulation" job pair. Zero provider calls (same
    # deterministic shape as "impact"/"simulation").
    "whatif": WhatifPreviewExecutor(),
    # Contextual patch generation over an already-confirmed intent
    # + the impact report + the simulation report. Zero
    # provider calls (same deterministic shape as "impact"/"simulation"/
    # "whatif"); a revision ("revise") is always a NEW job id, never a
    # mutation of a prior proposal row (see infra/worker/proposal.py's
    # module docstring for the multi-revision decision).
    "proposal": ProposalExecutor(),
    # Stage validation + independent BUSINESS/EVIDENCE/TECHNICAL
    # review board over an already-persisted, COMPLETED "proposal" job. Zero
    # provider calls (same deterministic shape as its siblings); never
    # writes a patch/approval, never trusts a caller-supplied evaluated
    # hash, and never executes the staged python:
    # `simulation_exact_patch` is UNAVAILABLE for a python proposal.
    "review": ReviewExecutor(),
    # Canonical approval digest + immutable readiness view over an
    # already-persisted, COMPLETED "proposal" + "review" job pair. Zero
    # provider calls (same deterministic shape as its siblings); a pure
    # snapshot READ -- never writes a patch/approval, never advances
    # store.head(...). See infra/worker/approval.py's module docstring.
    "approval": ApprovalExecutor(),
    # Replays an already-approved proposal's recorded typed edits
    # against FRESHLY re-read current source bytes and stages the result
    # (never publishes). Zero provider calls (same deterministic shape as
    # its siblings); see infra/worker/apply.py's module docstring.
    "apply": ApplyExecutor(),
    # Post-publish stages, created ONLY by the SOURCE_PUBLISHED
    # consumer in `claim_and_admit_one` (never by POST /v2/jobs). Zero
    # provider calls; independent of each other (a failed rebuild never
    # rewrites the immutable report, a FAILED report never blocks the rebuild).
    "verification": VerificationExecutor(),
    "km_rebuild": KmRebuildExecutor(),
}

#: Override/injection registry (tests, or a future real provider integration).
PROVIDER_EXECUTORS: dict = {}


def resolve_executor(kind: str | None):
    """Returns the executor for `kind`, or None (-> fail closed)."""
    if kind is None:
        return None
    if kind in PROVIDER_EXECUTORS:
        value = PROVIDER_EXECUTORS[kind]
        if hasattr(value, "prepare") and hasattr(value, "execute"):
            return value
        return _InjectedCallableExecutor(kind, value)
    return BUILTIN_EXECUTORS.get(kind)


# ----------------------------------------------------------------------------- model-data policy

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token|credential)(\"?\s*[:=]\s*\"?)[^\s\",}]+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{8,}\b"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)
_EXCERPT_LIMIT = 256


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + m.group(2) + "[REDACTED]")
                           if m.lastindex and m.lastindex >= 2 else "[REDACTED]", text)
    return text


def apply_model_data_policy(policy: Mapping, request: ExecutionRequest | None, *,
                            source_import_id: str | None = None) -> dict:
    """The project model-data policy checkpoint. Runs for EVERY attempt,
    including the fail-closed no-executor boundary (request=None). Returns
    what may be persisted about the request; raises
    `NonRetryableExecutionError(model_data_policy_denied)` if the request
    would send job content to an external model the project forbids."""
    if request is not None and request.external and policy.get("external_provider") == "deny":
        raise NonRetryableExecutionError(REASON_POLICY_DENIED,
                                         "project policy forbids external model calls")
    if request is not None and request.external and source_import_id is not None             and not policy.get("configured"):
        # Uploaded source is private data; it may reach an external
        # model only after the project's model-data policy is explicitly set.
        raise NonRetryableExecutionError(REASON_POLICY_NOT_ADMITTED,
                                         "project model-data policy not configured")
    excerpt = None
    if (request is not None and request.prompt
            and policy.get("prompt_storage") == "redacted_excerpt"):
        excerpt = redact(request.prompt)[:_EXCERPT_LIMIT]
    return {"request_excerpt": excerpt,
            "prompt_storage": policy.get("prompt_storage", "hash_only")}


# ----------------------------------------------------------------------------- helpers

class _NullBlobs:
    def put_immutable(self, digest, content):
        raise NotImplementedError

    def read(self, digest):
        raise NotImplementedError


def _load_config_if_configured() -> object | None:
    if not os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        return None
    return job_quota.load_worker_budget_config()


def _job_id_and_kind(claim: OutboxClaim) -> tuple[str | None, str | None]:
    payload = claim.payload if isinstance(claim.payload, dict) else {}
    return payload.get("job_id"), payload.get("kind")


def _ack(store, tenant_id, workspace_id, claim, worker_id) -> bool:
    try:
        store.ack_outbox(tenant_id, workspace_id, claim.id, worker_id, claim.fencing_token)
        return True
    except LeaseLost:
        return False


#: Seconds of lease kept beyond a provider call's own timeout, so a slow call never outlives
#: its lease (a reclaim during a live call would start a second, unbudgeted call).
LEASE_MARGIN_SECONDS = 15


def effective_lease_seconds(config, lease_seconds: int) -> int:
    """`lease_seconds`, raised to cover the configured provider request timeout plus a margin."""
    timeout = getattr(config, "request_timeout_seconds", None)
    if not timeout:
        return lease_seconds
    return max(lease_seconds, int(math.ceil(timeout)) + LEASE_MARGIN_SECONDS)


#: job kind -> the budget ROLE whose pinned model prices it, where the two names differ. Without this a kind that is not itself a role key was
#: priced at the default model: a failed `sim.analysis` attempt was booked at the lite estimate (0.0379) instead of the flash worst case (0.12816).
_PRICING_ROLE = {"sim.analysis": "simulation.analyze", "explain": "intent.explain"}


def _attempt_cost(config, calls_used: int, outcome: ExecutionOutcome | None,
                  kind: str | None = None) -> tuple:
    """(cost_estimate_usd, cost_usd). Zero real calls cost exactly 0. With
    calls made: reported usage is priced as reported; missing usage is
    priced fail-closed per call (budget contract §7.3). An unpriced model id
    raises KeyError (contract §7.2) -- propagated, so the job is neither
    completed nor acked."""
    if calls_used == 0:
        return Decimal("0"), Decimal("0")
    if config is None:
        return None, Decimal("0")
    kind = _PRICING_ROLE.get(kind, kind)
    per_call_worst = job_quota.actual_call_cost_usd(
        config, model_id=model_for_role(config, kind), input_tokens=None, output_tokens=None)
    estimate = per_call_worst * calls_used
    if outcome is not None:
        billed_output = outcome.output_tokens
        if billed_output is not None and outcome.thought_tokens is not None:
            billed_output += outcome.thought_tokens
        actual = job_quota.actual_call_cost_usd(
            config, model_id=outcome.model_id or model_for_role(config, kind),
            input_tokens=outcome.input_tokens, output_tokens=billed_output)
    else:
        actual = estimate
    return estimate, actual


def _cost_basis(calls_used: int, outcome: ExecutionOutcome | None) -> str:
    """What the booked cost of an attempt rests on. `provider_usage`: both token sides were reported by the provider. `estimate_*`: a side was
    missing and was priced at its ceiling (fail closed) - an ESTIMATE, never reported as a confirmed provider cost."""
    if not calls_used:
        return "none"
    if outcome is None or (outcome.input_tokens is None and outcome.output_tokens is None):
        return "estimate_no_usage"
    if outcome.input_tokens is None or outcome.output_tokens is None:
        return "estimate_partial_usage"
    return "provider_usage"


def _answered_usage(ctx, calls_used: int) -> ExecutionOutcome | None:
    """The usage every call of this attempt reported before the executor refused its result (recorded by `explain.call_pinned_json`). Only when EACH call used is
    recorded; otherwise None, and the attempt stays priced fail-closed. A missing token side stays missing (priced at its ceiling)."""
    rows = (getattr(ctx, "request_context", None) or {}).get("provider_usage") or []
    if not calls_used or len(rows) != calls_used:
        return None
    def total(key):
        vals = [r.get(key) for r in rows]
        return None if any(v is None for v in vals) else sum(vals)
    return ExecutionOutcome(result={}, model_id=rows[-1].get("model_id"), input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
                            thought_tokens=total("thought_tokens") if any(r.get("thought_tokens") is not None for r in rows) else None)


def _note_failure(attempt: dict, exc: BaseException, calls_used: int, outcome: ExecutionOutcome | None) -> None:
    """Keep the safe provider metadata and the cost basis of a failed attempt on its row (no prompt, no source text)."""
    rc = attempt["request_context"]
    rc["cost_basis"] = _cost_basis(calls_used, outcome)
    diag = getattr(exc, "diagnostics", None)
    if isinstance(diag, dict) and diag:
        rc["provider_diagnostics"] = diag


def _base_attempt(claim: OutboxClaim, worker_id: str, job: dict | None,
                  executor, request: ExecutionRequest | None, stored: dict) -> dict:
    job = job or {}
    context = {
        "tenant_id": job.get("tenant_id"), "workspace_id": job.get("workspace_id"),
        "project_id": job.get("project_id"), "job_id": job.get("job_id"),
        "outbox_event_id": claim.id, "fencing_token": claim.fencing_token,
        "attempt": claim.attempts, "worker_id": worker_id,
        "pinned_source_revision_id": job.get("pinned_source_revision_id"),
        "pinned_knowledge_revision_id": job.get("pinned_knowledge_revision_id"),
        "pinned_decision_epoch": job.get("pinned_decision_epoch"),
        "prompt_storage": stored.get("prompt_storage"),
    }
    if request is not None and request.prompt:
        prompt_hash = canonical_hash({"prompt": request.prompt})
        context["prompt_hash_basis"] = "prompt"
        context["prompt_chars"] = len(request.prompt)
    else:
        prompt_hash = canonical_hash({"kind": job.get("kind"), "payload": job.get("payload"),
                                      "pinned_source_revision_id":
                                          job.get("pinned_source_revision_id")})
        context["prompt_hash_basis"] = "input_descriptor"
    return {
        "attempt_key": f"outbox:{claim.id}:attempt:{claim.attempts}:token:{claim.fencing_token}",
        "executor_id": getattr(executor, "executor_id", None),
        "prompt_hash": prompt_hash,
        "schema_hash": (canonical_hash(dict(request.schema))
                        if request is not None and request.schema is not None else None),
        "request_context": context,
        "request_excerpt": stored.get("request_excerpt"),
    }


# ----------------------------------------------------------------------------- execution

class _DurableCallBudget(ProviderCallBudget):
    """The call-site ceiling plus a hook that runs JUST BEFORE the provider is contacted (after the ceiling check): the worker uses it to write a durable "provider attempt in flight"
    row first. If that write fails, `consume` raises and the call is NOT made (fail closed). (A subclass here, not a change of `execution_types.py`, which is pinned to its reviewed baseline.)"""

    before_call = None

    def consume(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise ProviderCallCeilingExceeded(self.limit, self.used + n)
        if self.before_call is not None:
            self.before_call(self.used + n)
        self.used += n


def execute_admitted_claim(store: PostgresV2Store, tenant_id: str, workspace_id: str,
                           claim: OutboxClaim, worker_id: str, *,
                           config: object | None = None, lease_seconds: int = 30,
                           max_running: int | None = None) -> str:
    """AH-05: a job created while this claim runs is linked to it as its parent (and never to an HTTP request ID inherited from a calling thread)."""
    from infra.correlation import PARENT_JOB, REQUEST_ID
    job_id, _kind = _job_id_and_kind(claim)
    parent, request = PARENT_JOB.set(job_id or ""), REQUEST_ID.set("")
    try:
        return _execute_admitted_claim(store, tenant_id, workspace_id, claim, worker_id, config=config, lease_seconds=lease_seconds, max_running=max_running)
    finally:
        REQUEST_ID.reset(request)
        PARENT_JOB.reset(parent)


def _execute_admitted_claim(store: PostgresV2Store, tenant_id: str, workspace_id: str,
                            claim: OutboxClaim, worker_id: str, *,
                            config: object | None = None, lease_seconds: int = 30,
                            max_running: int | None = None) -> str:
    """Run an admitted `JOB_QUEUED` claim to a real outcome, exactly once.
    Returns one of `DONE`, `RETRY_SCHEDULED`, `CANCELLED` (cancel checkpoint,
    or retries exhausted -- the outcome string for exhausted retries), `FAILED`
    (non-retryable: no executor / policy denied / call ceiling / invalid
    input), `DEFERRED` (claim-time concurrency limit reached; claim released
    without consuming an attempt), `ALREADY_PROCESSED`, `LEASE_LOST`."""
    job_id, job_kind = _job_id_and_kind(claim)
    if not job_id:
        return "DONE" if _ack(store, tenant_id, workspace_id, claim, worker_id) else "LEASE_LOST"

    try:
        store.heartbeat_outbox(tenant_id, workspace_id, claim.id, worker_id,
                               claim.fencing_token,
                               lease_seconds=effective_lease_seconds(config, lease_seconds))
    except LeaseLost:
        return "LEASE_LOST"

    # Sandbox approval hold (infra/worker/sandbox_hold.py): applies to EVERY consumer of the queue because it sits in the
    # one function that runs a job. Only sandbox workspaces are affected; zero provider calls are made for a held job.
    from infra.worker import sandbox_hold
    if sandbox_hold.approval_hold(store, tenant_id, workspace_id, job_id, job_kind):
        try:
            store.defer_outbox(tenant_id, workspace_id, claim.id, worker_id, claim.fencing_token,
                               seconds=sandbox_hold.HOLD_SECONDS)
        except LeaseLost:
            return "LEASE_LOST"
        return "HELD_FOR_APPROVAL"

    try:
        job = store.begin_execution(tenant_id, workspace_id, job_id, max_running=max_running)
    except StoreConflict:
        # Duplicate/redundant delivery of an already-terminal execution.
        return ("ALREADY_PROCESSED" if _ack(store, tenant_id, workspace_id, claim, worker_id)
                else "LEASE_LOST")
    if job is None:
        try:
            store.release_outbox(tenant_id, workspace_id, claim.id, worker_id,
                                 claim.fencing_token)
        except LeaseLost:
            return "LEASE_LOST"
        return "DEFERRED"

    def _terminal(terminal: str, reason: str | None, attempt: dict | None,
                  result: dict | None = None) -> str:
        try:
            written = store.finish_execution(tenant_id, workspace_id, job_id, terminal,
                                             reason=reason, result=result, attempt=attempt)
        except StoreConflict:
            return ("ALREADY_PROCESSED" if _ack(store, tenant_id, workspace_id, claim, worker_id)
                    else "LEASE_LOST")
        if not _ack(store, tenant_id, workspace_id, claim, worker_id):
            return "LEASE_LOST"
        return written

    if job["cancel_requested"]:
        written = _terminal("CANCELLED", "cancel_requested_before_execution", None)
        _notify_unrun(resolve_executor(job_kind or job["kind"]), store, tenant_id, workspace_id, job_id, "cancel_requested_before_execution")
        return written

    policy = job["model_policy"]
    executor = resolve_executor(job_kind or job["kind"])
    if executor is None:
        # Fail closed: a kind with no real executor never reaches COMPLETED.
        stored = apply_model_data_policy(policy, None)
        attempt = _base_attempt(claim, worker_id, job, None, None, stored)
        reason = f"{REASON_NO_EXECUTOR}: kind={job['kind']!r}"
        attempt.update(outcome="FAILED", reason=reason, retryable=False, provider_calls=0,
                       cost_usd=Decimal("0"), cost_estimate_usd=Decimal("0"), duration_ms=0)
        return _terminal("FAILED", reason, attempt)

    if config is not None and (job_kind or job["kind"]) in _AI_KINDS:
        # Role allowlist (infra/ai_kinds.py), read from the configuration loaded for THIS claim: a kind removed after the job was queued
        # ends here, before any provider reservation, attempt journal entry or call; nothing is charged. Checked after the cancel and executor
        # checks so a cancelled job still reads as cancelled.
        from infra import ai_kinds
        code = ai_kinds.refusal(getattr(config, "enabled_roles", None), job_kind or job["kind"])
        if code:
            reason = f"{code}: kind={job['kind']!r}; nothing was sent and nothing was reserved"
            attempt = _base_attempt(claim, worker_id, job, executor, None, apply_model_data_policy(policy, None))
            attempt.update(outcome="FAILED", reason=reason, retryable=False, provider_calls=0,
                           cost_usd=Decimal("0"), cost_estimate_usd=Decimal("0"), duration_ms=0)
            written = _terminal("FAILED", reason, attempt)
            _notify_unrun(executor, store, tenant_id, workspace_id, job_id, reason)
            return written

    request: ExecutionRequest | None = None
    stored = {"prompt_storage": policy.get("prompt_storage"), "request_excerpt": None}
    ceiling = (config.max_provider_calls_per_job if config is not None else None)
    calls = _DurableCallBudget(limit=ceiling if ceiling is not None else 0)
    started = time.monotonic()
    outcome: ExecutionOutcome | None = None
    from infra.provider_gate import AI_KINDS
    if (job_kind or job["kind"]) in AI_KINDS:
        # Provider attempts are bounded by the approved retry limit INDEPENDENTLY of how many times the claim is reclaimed (the outbox allows up to 5 claims for crashes
        # of any kind; those are not provider attempts). An attempt that was started and whose usage was never recorded (the worker died) is `USAGE_UNKNOWN`: potentially
        # charged, booked at its ceiling, counted here. A reclaim can therefore never widen the number of provider calls the retry limit approved.
        live_prefix = f"outbox:{claim.id}:attempt:{claim.attempts}:token:{claim.fencing_token}"
        summary = store.mark_orphaned_provider_attempts(tenant_id, workspace_id, job_id, live_prefix)
        max_attempts = (config.max_retries if config is not None else _DEFAULT_MAX_RETRIES) + 1
        if summary["provider_attempts"] >= max_attempts:
            reason = (f"provider_attempt_limit_reached: {summary['provider_attempts']} provider attempt(s) already made (limit {max_attempts}); "
                      "none will be repeated; the cost booked includes attempts whose usage is unknown")
            written = _terminal("FAILED", reason, None)
            _notify_unrun(executor, store, tenant_id, workspace_id, job_id, reason)
            return written
        per_call_worst = _attempt_cost(config, 1, None, job_kind or job["kind"])[0]
        cap = getattr(config, "per_job_cap_usd", None) if config is not None else None
        if per_call_worst is not None and cap is not None and Decimal(str(summary["cost_usd"])) + per_call_worst > cap:
            reason = "per_job_cap_would_be_exceeded: the attempts already booked plus one more call would pass the per-job cap"
            written = _terminal("FAILED", reason, None)
            _notify_unrun(executor, store, tenant_id, workspace_id, job_id, reason)
            return written
        calls.before_call = lambda n: store.begin_provider_attempt(
            tenant_id, workspace_id, job_id, f"{live_prefix}:inflight:{n}", ceiling_usd=per_call_worst if per_call_worst is not None else Decimal("0"),
            context={"outbox_event_id": claim.id, "fencing_token": claim.fencing_token, "claim_attempt": claim.attempts, "worker_id": worker_id, "call": n,
                     "cost_basis": "ceiling_until_usage_is_recorded"})
    ctx = None
    try:
        ctx = ExecutionContext(store=store, job=job, config=config,
                               lease=(claim.id, worker_id, claim.fencing_token))
        request = executor.prepare(ctx)
        if ceiling is None:
            # No configured ceiling: the executor may not exceed its own
            # declared plan (still enforced at the call site).
            calls.limit = request.provider_calls_planned
        stored = apply_model_data_policy(policy, request,
                                         source_import_id=job.get("source_import_id"))
        if request.provider_calls_planned > calls.limit:
            raise ProviderCallCeilingExceeded(calls.limit, request.provider_calls_planned)
        outcome = executor.execute(ctx, request, calls)
    except LeaseLost:
        # Fenced: another worker owns this claim now. Write nothing terminal
        # (the new owner decides the job's outcome); the stale worker stops.
        return "LEASE_LOST"
    except NonRetryableExecutionError as exc:
        # a result rejected AFTER the provider answered carries the usage the provider reported: settle that, not the worst-case estimate
        used_outcome = getattr(exc, "usage_outcome", None) or _answered_usage(ctx, calls.used)
        estimate, cost = _attempt_cost(config, calls.used, used_outcome, job_kind or job["kind"])
        attempt = _base_attempt(claim, worker_id, job, executor, request, stored)
        attempt.update(outcome="FAILED", reason=str(exc), retryable=False,
                       provider_calls=calls.used, cost_usd=cost, cost_estimate_usd=estimate,
                       duration_ms=int((time.monotonic() - started) * 1000))
        _note_failure(attempt, exc, calls.used, used_outcome)
        written = _terminal("FAILED", str(exc), attempt)
        if written == "FAILED":
            _checks_for_new_readings(store, tenant_id, workspace_id, job)      # the readings that did arrive are still checked (a failed last reading must not leave them unchecked)
        return written
    except ProviderExecutionError as exc:
        used_outcome = getattr(exc, "usage_outcome", None)
        estimate, cost = _attempt_cost(config, calls.used, used_outcome, job_kind or job["kind"])
        reason = f"provider_execution_failed: {type(exc).__name__}: {exc}"
        attempt = _base_attempt(claim, worker_id, job, executor, request, stored)
        attempt.update(outcome="RETRYABLE_FAILURE", reason=reason, retryable=True,
                       provider_calls=calls.used, cost_usd=cost, cost_estimate_usd=estimate,
                       duration_ms=int((time.monotonic() - started) * 1000))
        _note_failure(attempt, exc, calls.used, used_outcome)
        max_retries = config.max_retries if config is not None else _DEFAULT_MAX_RETRIES
        if claim.attempts > max_retries:
            written = _terminal("FAILED", reason, attempt)
            if written == "FAILED":
                _checks_for_new_readings(store, tenant_id, workspace_id, job)
            return "CANCELLED" if written == "FAILED" else written
        # Retries remain: the attempt (and its cost) is durable now; the
        # claim stays CLAIMED until its lease expires and is reclaimed.
        store.record_attempt(tenant_id, workspace_id, job_id, attempt)
        delay = retry_backoff_seconds(claim.attempts, exc)
        if not delay and effective_lease_seconds(config, lease_seconds) != lease_seconds:
            # The lease was stretched to cover the provider timeout; the call is over, so the
            # retry is reclaimable after the normal lease again (not after the stretched one).
            delay = lease_seconds
        if delay:
            # Push the reclaim time out by the backoff (same claim, same fencing token): the retry
            # is the attempt already reserved, only later.
            try:
                store.heartbeat_outbox(tenant_id, workspace_id, claim.id, worker_id,
                                       claim.fencing_token, lease_seconds=delay)
            except LeaseLost:
                return "LEASE_LOST"
        return "RETRY_SCHEDULED"

    duration_ms = int((time.monotonic() - started) * 1000)
    estimate, cost = _attempt_cost(config, calls.used, outcome, job_kind or job["kind"])
    attempt = _base_attempt(claim, worker_id, job, executor, request, stored)
    if outcome.thought_tokens is not None:
        # Keep provider candidates_token_count unchanged in the usage column;
        # expose thinking separately while pricing both as output tokens.
        attempt["request_context"]["thought_tokens"] = outcome.thought_tokens
    gen_cap = getattr(config, "generation_max_output_tokens", None) or getattr(config, "max_output_tokens", None)
    if gen_cap and outcome.output_tokens is not None:
        total_out = outcome.output_tokens + (outcome.thought_tokens or 0)
        # evidence for the open question "does the provider enforce the request cap over output + thinking": recorded every call
        attempt["request_context"]["generation_cap"] = gen_cap
        attempt["request_context"]["output_plus_thinking"] = total_out
        attempt["request_context"]["over_generation_cap"] = total_out > gen_cap
    attempt["request_context"]["cost_basis"] = _cost_basis(calls.used, outcome)
    attempt.update(outcome="COMPLETED", model_id=outcome.model_id, retryable=None,
                   provider_calls=calls.used, input_tokens=outcome.input_tokens,
                   output_tokens=outcome.output_tokens, cost_usd=cost,
                   cost_estimate_usd=estimate, duration_ms=duration_ms)
    from infra.worker import ai_outcome
    written = _terminal("COMPLETED", None, attempt, result=ai_outcome.annotate(job_kind or job["kind"], outcome.result, calls.used))   # what the model contributed (LLM-00)
    if written == "COMPLETED":
        _checks_for_new_readings(store, tenant_id, workspace_id, job)
        return "DONE"
    return written


def _checks_for_new_readings(store, tenant_id: str, workspace_id: str, job: dict) -> None:
    """After an apply the changed file is read again (reanalysis extraction). When the LAST of those readings is in, the relation and finding checks for the NEW readings are
    created (deterministic ids, no AI, idempotent). Before this nothing ever checked the new readings: the checks re-run after an apply only covered the OLD candidate ids, so the
    result decision found the edited rule unchecked. A failure here never changes the job that just finished."""
    try:
        if job.get("kind") != "extraction":
            return
        apply_id = (job.get("payload") or {}).get("caused_by_apply_job_id")
        if not apply_id:
            return
        from infra import v2_coverage
        from infra.worker.verification import rebuild_job_id
        rec = store.job_record(tenant_id, workspace_id, rebuild_job_id(apply_id))
        ids = (((rec.get("execution_result") or {}).get("reanalysis")) or {}).get("job_ids") or []
        for jid in ids:
            if store.job_record(tenant_id, workspace_id, jid).get("execution_status") not in ("COMPLETED", "FAILED", "CANCELLED"):
                return                                               # another reanalysis reading of this apply is still to come: it will do this
        v2_coverage.ensure(store, tenant_id, workspace_id)
    except Exception:                                                # noqa: BLE001 - never turns a finished job into a failure
        return


def _notify_unrun(executor, store, tenant_id: str, workspace_id: str, job_id: str, reason: str) -> None:
    """A job ended terminally before any executor code ran (retry limit reached after a dead attempt, per-job cap, cancelled before execution, refused at admission).
    An executor that coordinates with others (a multi-agent child) gets told, so its coordinator never waits for a result that cannot come. Best effort: the terminal state
    is already durable; a failing hook never changes it."""
    hook = getattr(executor, "on_terminal_without_execution", None)
    if hook is None:
        return
    try:
        hook(store, tenant_id, workspace_id, job_id, reason)
    except Exception:                                                                   # noqa: BLE001 - the operator's advance route remains the recovery path
        pass


def claim_and_admit_one(store: PostgresV2Store, tenant_id: str, workspace_id: str,
                        worker_id: str, *, lease_seconds: int = 30) -> OutboxClaim | None:
    """Claim one outbox event; for a `JOB_QUEUED` event, re-check the
    provider-call ceiling for its kind (refuse: execution CANCELLED with a
    `budget_exceeded` reason, reservation released, event acked -> returns
    None), then run it via `execute_admitted_claim` with the configured
    claim-time concurrency limit. Returns the claim, or None."""
    from infra import operator_flags as _flags
    if not _flags.claims_on():
        return None                                   # operator switch: claim NOTHING (queued jobs stay queued); fail closed when the switch cannot be read
    skip = () if (_flags.ai_on() and not _flags.window_over()) else tuple(sorted(_AI_KINDS))   # AI off: AI jobs stay QUEUED, deterministic ones still run
    claim = store.claim_outbox(tenant_id, workspace_id, worker_id, lease_seconds=lease_seconds, skip_job_kinds=skip)
    if claim is None:
        return None
    if claim.kind == "BUSINESS_KNOWLEDGE_COMMITTED":
        # Business-knowledge commit (contract fkl/1). An ACCEPTED snapshot gets its retrieval index built here (deterministic, no model call); the event is
        # acknowledged only after the index is READY, so a failed build is retried on the next claim and ends as a failed outbox row after five claims.
        # A proposal has no index and is acknowledged at once.
        payload = claim.payload if isinstance(claim.payload, dict) else {}
        if payload.get("lifecycle_state") == "ACCEPTED" and payload.get("snapshot_id"):
            from infra.adapters import v2_business_index
            try:
                v2_business_index.build_index(store, tenant_id, workspace_id, payload["snapshot_id"])
            except Exception:
                return claim
        _ack(store, tenant_id, workspace_id, claim, worker_id)
        return claim
    if claim.kind == "SOURCE_PUBLISHED":
        # The durable post-publish link written by the apply gate's
        # publish transaction. Only an APPLY publish (payload.kind == "apply")
        # is consumed here -- create-then-ack, deterministic job ids; every
        # other SOURCE_PUBLISHED (imports, knowledge, legacy) is returned
        # untouched exactly as before (other consumers unaffected).
        payload = claim.payload if isinstance(claim.payload, dict) else {}
        if payload.get("kind") == "apply":
            consume_source_published(store, tenant_id, workspace_id, claim, worker_id, _ack)
        elif payload.get("trigger") == "refresh":
            # A committed MANUAL source refresh (an external observation).
            # Imported lazily: `refresh_impact` imports `verification`, which imports
            # this module.
            from infra.worker.refresh_impact import consume_refresh_published
            consume_refresh_published(store, tenant_id, workspace_id, claim, worker_id, _ack)
        return claim
    if claim.kind != "JOB_QUEUED":
        return claim

    job_id, job_kind = _job_id_and_kind(claim)
    config = _load_config_if_configured()
    max_running = None
    if config is not None and job_id and job_kind:
        try:
            job_quota.check_job_admission(config, job_kind=job_kind)
        except job_quota.JobQuotaError as exc:
            try:
                store.cancel_execution(tenant_id, workspace_id, job_id,
                                       f"budget_exceeded: {exc}")
            except StoreConflict:
                pass
            _ack(store, tenant_id, workspace_id, claim, worker_id)
            _notify_unrun(resolve_executor(job_kind), store, tenant_id, workspace_id, job_id, f"budget_exceeded: {exc}")
            return None
        max_running = job_quota.load_concurrency_limit()

    execute_admitted_claim(store, tenant_id, workspace_id, claim, worker_id,
                           config=config, lease_seconds=lease_seconds,
                           max_running=max_running)
    return claim


def _make_store(dsn: str) -> PostgresV2Store:
    bucket_name = os.environ.get("RIPPLE_V2_BLOB_BUCKET")
    if bucket_name:
        from google.cloud import storage
        from infra.adapters.v2_blobs import ImmutableGCSBlobs
        blobs = ImmutableGCSBlobs(storage.Client().bucket(bucket_name))
    else:
        blobs = _NullBlobs()
    return PostgresV2Store(dsn, blobs)


def run_worker_loop(dsn: str, tenant_id: str, workspace_id: str, *,
                    worker_id: str | None = None, poll_seconds: float = 1.0,
                    max_iterations: int | None = None) -> None:
    store = _make_store(dsn)
    wid = worker_id or f"worker-{uuid.uuid4().hex}"
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        claim_and_admit_one(store, tenant_id, workspace_id, wid)
        iterations += 1
        if max_iterations is None:
            time.sleep(poll_seconds)


# ----------------------------------------------------------------------------- bounded loop

class WorkerConfigError(ValueError):
    """An invalid RIPPLE_WORKER_* setting (the entry point exits 2)."""


def parse_workspace_allowlist(raw: str) -> list[tuple[str, str]]:
    """`"tenant:workspace,tenant2:workspace2"` -> ordered, de-duplicated pairs. Fails closed on any
    malformed or empty entry: the allowlist is the ONLY set of workspaces the worker may serve."""
    pairs: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            raise WorkerConfigError("RIPPLE_WORKER_WORKSPACES has an empty entry")
        tenant, sep, workspace = part.partition(":")
        tenant, workspace = tenant.strip(), workspace.strip()
        if not sep or not tenant or not workspace or ":" in workspace:
            raise WorkerConfigError(
                f"RIPPLE_WORKER_WORKSPACES entry {part!r} must be 'tenant:workspace'")
        if (tenant, workspace) not in pairs:
            pairs.append((tenant, workspace))
    if not pairs:
        raise WorkerConfigError("RIPPLE_WORKER_WORKSPACES is empty")
    return pairs


def _positive_number(env: Mapping, key: str, *, integer: bool) -> float | int | None:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as exc:
        raise WorkerConfigError(f"{key}={raw!r} is not a valid number") from exc
    if value <= 0 or (not integer and not math.isfinite(value)):
        raise WorkerConfigError(f"{key}={raw!r} must be positive")
    return value


def load_worker_settings(env: Mapping | None = None) -> dict:
    """Worker selection + bounds from the environment. Single-workspace mode
    (`RIPPLE_WORKER_TENANT_ID` + `RIPPLE_WORKER_WORKSPACE_ID`) stays as it was; the allowlist
    (`RIPPLE_WORKER_WORKSPACES`) is the multi-workspace form and must not be combined with it
    (ambiguous scope fails closed). The bounds are optional; unset = unbounded (the previous,
    forever-running behaviour)."""
    env = os.environ if env is None else env
    tenant, workspace = env.get("RIPPLE_WORKER_TENANT_ID"), env.get("RIPPLE_WORKER_WORKSPACE_ID")
    allow = env.get("RIPPLE_WORKER_WORKSPACES")
    if allow is not None and allow.strip():
        if tenant or workspace:
            raise WorkerConfigError("set either RIPPLE_WORKER_WORKSPACES or the single "
                                    "RIPPLE_WORKER_TENANT_ID/RIPPLE_WORKER_WORKSPACE_ID, not both")
        workspaces = parse_workspace_allowlist(allow)
    elif tenant and workspace:
        workspaces = [(tenant, workspace)]
    else:
        raise WorkerConfigError("RIPPLE_WORKER_WORKSPACES or both RIPPLE_WORKER_TENANT_ID and "
                                "RIPPLE_WORKER_WORKSPACE_ID required")
    return {
        "workspaces": workspaces,
        "max_runtime_seconds": _positive_number(env, "RIPPLE_WORKER_MAX_RUNTIME_SECONDS", integer=False),
        "max_jobs": _positive_number(env, "RIPPLE_WORKER_MAX_JOBS", integer=True),
        "idle_exit_seconds": _positive_number(env, "RIPPLE_WORKER_IDLE_EXIT_SECONDS", integer=False),
        "poll_seconds": _positive_number(env, "RIPPLE_WORKER_POLL_SECONDS", integer=False) or 1.0,
    }


def runtime_until_window(max_runtime_seconds, now=None):
    """The claim loop's runtime bound: the configured bound, or the time left until the recorded trial window ends, whichever is shorter (so the worker stops CLAIMING when the web
    stops accepting new AI actions; an attempt already in flight finishes inside the task timeout's margin). No window recorded: the configured bound unchanged."""
    from datetime import datetime, timezone
    from infra import operator_flags
    if not operator_flags.enabled():
        return max_runtime_seconds
    end = operator_flags.window_ends_at()
    if end is None:
        return max_runtime_seconds
    left = max(0.0, ((end - (now or datetime.now(timezone.utc))).total_seconds()))
    return left if max_runtime_seconds is None else min(float(max_runtime_seconds), left)


def close_switches_after_window(summary: dict) -> list[str]:
    """When the worker ends because its bound was reached (`max_runtime`/`idle`) with `RIPPLE_WORKER_CLOSE_ON_EXIT=1`, it switches BOTH switches OFF itself, audited, so that nothing
    is left accepting AI actions with nobody to process them, whether or not an operator is around."""
    if os.environ.get("RIPPLE_WORKER_CLOSE_ON_EXIT") != "1" or summary.get("stopped") not in ("max_runtime", "idle"):
        return []
    from infra import operator_flags
    done = []
    for key in ("worker_claims", "ai_switch"):
        operator_flags.set_flag(key, "off", "worker", f"worker window ended ({summary.get('stopped')}): switched off by the worker itself")
        done.append(key)
    return done


def run_bounded_worker(store, workspaces: list[tuple[str, str]], *, worker_id: str | None = None,
                       poll_seconds: float = 1.0, max_runtime_seconds: float | None = None,
                       max_jobs: int | None = None, idle_exit_seconds: float | None = None,
                       claim_fn=None, clock=time.monotonic, sleep=time.sleep, heartbeat: bool = False) -> dict:
    """Poll the allowlisted workspaces round-robin (one claim attempt per workspace per pass) until
    a bound is hit. `max_jobs` counts claimed `JOB_QUEUED` events (jobs started); `max_runtime_seconds`
    is a wall-clock cap; `idle_exit_seconds` exits after that long with nothing claimed in any
    workspace. Executors, claim and budget admission are exactly `claim_and_admit_one`'s: this loop
    only decides WHICH workspace to poll and WHEN to stop. Returns a summary
    `{"stopped": reason, "jobs": n, "claims": n, "per_workspace": {...}, "passes": n}`."""
    claim_fn = claim_fn or claim_and_admit_one
    wid = worker_id or f"worker-{uuid.uuid4().hex}"
    from infra.worker import wake as _wake
    last_beat = [None]

    def _beat(force: bool = False) -> None:
        # Tells the web side's wake-up that a worker is alive (so it starts no second execution). Best effort: a failed heartbeat only means an extra, harmless execution may start.
        if not heartbeat:
            return
        t = clock()
        if force or last_beat[0] is None or t - last_beat[0] >= 10:
            last_beat[0] = t
            try:
                _wake.beat(store, wid)
            except Exception:
                pass

    started = clock()
    last_work = started
    wall0 = time.time()
    jobs = claims = passes = 0
    per_workspace = {f"{t}:{w}": 0 for t, w in workspaces}

    def _stop_reason() -> str | None:
        if max_jobs is not None and jobs >= max_jobs:
            return "max_jobs"
        if max_runtime_seconds is not None and clock() - started >= max_runtime_seconds:
            return "max_runtime"
        return None

    while True:
        reason = _stop_reason()
        if reason:
            break
        passes += 1
        worked = False
        _beat()
        for tenant_id, workspace_id in workspaces:
            reason = _stop_reason()
            if reason:
                break
            claim = claim_fn(store, tenant_id, workspace_id, wid)
            if claim is None:
                continue
            worked = True
            claims += 1
            per_workspace[f"{tenant_id}:{workspace_id}"] += 1
            if getattr(claim, "kind", None) == "JOB_QUEUED":
                jobs += 1
        if reason:
            break
        now = clock()
        if worked:
            last_work = now
            continue
        if idle_exit_seconds is not None and now - last_work >= idle_exit_seconds:
            if heartbeat:
                # Leaving: drop the heartbeat FIRST, then one last sweep. A job queued from here on either is claimed by this sweep or finds no live worker and starts a new execution.
                try:
                    _wake.clear_beat(store, wid)
                except Exception:
                    pass
                late = False
                for tenant_id, workspace_id in workspaces:
                    claim = claim_fn(store, tenant_id, workspace_id, wid)
                    if claim is not None:
                        late = True
                        claims += 1
                        per_workspace[f"{tenant_id}:{workspace_id}"] += 1
                        if getattr(claim, "kind", None) == "JOB_QUEUED":
                            jobs += 1
                if late:
                    last_work = clock()
                    _beat(force=True)
                    continue
            reason = "idle"
            break
        sleep(poll_seconds)
    if heartbeat:
        try:                                    # replace the reservation of THIS Cloud Run execution (if its kick made one) by what it really ran (infra self-limit, infra/worker/wake.py)
            _wake.record_worker_exit(store, time.time() - wall0, os.environ.get("CLOUD_RUN_EXECUTION"))
        except Exception:
            pass
    return {"stopped": reason, "jobs": jobs, "claims": claims, "passes": passes,
            "per_workspace": per_workspace}


if __name__ == "__main__":
    import sys

    _dsn = os.environ.get("RIPPLE_V2_DSN")
    if not _dsn:
        print("RIPPLE_V2_DSN required", file=sys.stderr)
        raise SystemExit(2)
    try:
        _settings = load_worker_settings()
    except WorkerConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
        try:
            _load_config_if_configured()
        except job_quota.BudgetConfigError as exc:
            print(f"invalid worker budget config: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    _bounded = any(_settings[k] is not None
                   for k in ("max_runtime_seconds", "max_jobs", "idle_exit_seconds"))
    if len(_settings["workspaces"]) == 1 and not _bounded:
        # Unchanged legacy behaviour: one workspace, forever.
        run_worker_loop(_dsn, *_settings["workspaces"][0], poll_seconds=_settings["poll_seconds"])
    else:
        _summary = run_bounded_worker(
            _make_store(_dsn), _settings["workspaces"], poll_seconds=_settings["poll_seconds"],
            max_runtime_seconds=runtime_until_window(_settings["max_runtime_seconds"]), max_jobs=_settings["max_jobs"],
            idle_exit_seconds=_settings["idle_exit_seconds"], heartbeat=bool(os.environ.get("RIPPLE_WORKER_HEARTBEAT")))
        _summary["switches_closed"] = close_switches_after_window(_summary)
        print(json.dumps(_summary, sort_keys=True))
