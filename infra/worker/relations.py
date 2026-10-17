"""Rule/relation identity discrimination and semantic graph,
registered as job kind "relation".

Deterministic, zero-provider-call executor (no external model reached; no
budget config is required, unlike "analysis"/"extraction") that consumes
already-persisted "extraction" job candidates and
proposes a bounded set of relation edges between them.

Identity discrimination is decided from the SAME structured fields the
schema already treats as identity-bearing (subject, predicate, scope) --
never from a literal shared number or free-text similarity alone, which
would wrongly merge an unrelated rule that happens to cite the same figure
(e.g. "500,000 VND approval threshold" vs "form 500,000 tax registration").
A shared explicit id or literal value never overrides a real scope
difference (CAPEX-only vs all-orders stays DIFFERENT_RULE / CONFLICTING).

Endpoint validation: a `candidate_id` that does not resolve to a real,
persisted candidate in this tenant/workspace is never silently linked -- it
is reported in `rejected_edges` and excluded from `proposed_edges`. Once an
edge is written, re-reading the job is a pure store read (no re-decision),
so a "REJECTED" edge cannot flip to "confirmed" merely because a second
worker/process re-touches the row -- there is no separate confirm step here
at all (human confirmation is a separate, later stage).

Bounded traversal: pairwise comparison is capped at
`_MAX_PAIR_COMPARISONS`; a candidate set large enough to exceed the cap
still terminates, and the omitted pairs are counted (never silently
completed as if nothing was skipped).
"""

from __future__ import annotations

import itertools

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.rule_identity import identity_key, scope_key, unit_key

#: A real ceiling on pairwise identity comparisons in one job -- an
#: unbounded O(n^2) traversal over a large candidate set must never hang the
#: worker; comparisons beyond this cap are recorded as omitted, not silently
#: skipped without a trace.
_MAX_PAIR_COMPARISONS = 500


_scope_key = scope_key  # shared canonical scope (rule_identity)


def _decide(a: dict, b: dict) -> tuple[str, str]:
    same_subject_predicate = identity_key(a) == identity_key(b)
    if not same_subject_predicate:
        return "DIFFERENT_RULE", "subject_or_predicate_differs"
    if unit_key(a.get("unit")) != unit_key(b.get("unit")):
        # currency/unit mismatch: never comparable even with identical scope.
        return "UNKNOWN", "unit_not_comparable"
    if a.get("applicability") == "UNKNOWN" or b.get("applicability") == "UNKNOWN":
        return "UNKNOWN", "scope_unresolved"
    scope_a, scope_b = _scope_key(a.get("scope")), _scope_key(b.get("scope"))
    if scope_a is None or scope_b is None:
        return "UNKNOWN", "scope_unresolved"
    if scope_a == scope_b:
        return "SAME_RULE", "matching_subject_predicate_scope"
    return "DIFFERENT_RULE", "conflicting_scope"


class RelationGraphExecutor:
    """See module docstring. Zero provider calls (`external = False`); its
    entire input is already-persisted extraction output, so no live model
    account/budget config is needed to run it."""

    external = False
    executor_id = "relations.identity_graph.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        candidate_ids = payload.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            raise NonRetryableExecutionError("candidate_ids_required",
                                             "payload.candidate_ids must be a list")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        # De-dup while keeping order -- a caller repeating the same id twice
        # must not double the pairwise comparison count.
        candidate_ids = list(dict.fromkeys(job["payload"].get("candidate_ids") or []))
        found, missing = ctx.store.find_candidates_by_id(
            job["tenant_id"], job["workspace_id"], candidate_ids)

        rejected_edges = [{"candidate_id": cid, "reason": "candidate_not_found"}
                          for cid in missing]

        ids = [cid for cid in candidate_ids if cid in found]
        pairs = list(itertools.combinations(ids, 2))
        omitted_pairs = 0
        omitted_identity_pairs = 0
        identity_pairs_total = 0
        if len(pairs) > _MAX_PAIR_COMPARISONS:
            # Never truncate by enumeration order (that dropped every
            # pair of one large bundle). Pairs that can actually be the same
            # rule (same normalised subject+predicate, cross-artifact first)
            # are compared first; only the no-information
            # subject_or_predicate_differs pairs fill the rest of the budget.
            def _priority(pair):
                a, b = found[pair[0]], found[pair[1]]
                if identity_key(a) != identity_key(b):
                    return 2
                cross = a.get("artifact_revision_id") != b.get("artifact_revision_id")
                return 0 if cross else 1
            ranked = sorted(pairs, key=_priority)  # stable: order kept per class
            identity_pairs_total = sum(1 for p in ranked if _priority(p) < 2)
            pairs = ranked[:_MAX_PAIR_COMPARISONS]
            omitted_pairs = len(ranked) - len(pairs)
            omitted_identity_pairs = max(0, identity_pairs_total - _MAX_PAIR_COMPARISONS)

        identity_decisions: list[dict] = []
        proposed_edges: list[dict] = []
        for a_id, b_id in pairs:
            a, b = found[a_id], found[b_id]
            decision, reason = _decide(a, b)
            identity_decisions.append({
                "candidate_a_id": a_id, "candidate_b_id": b_id,
                "decision": decision, "reason": reason})
            lifecycle_state = "PROPOSED" if decision == "SAME_RULE" else "REJECTED"
            proposed_edges.append({
                "relation_id": f"rel-{a_id}-{b_id}",
                "relation_type": "SAME_RULE" if decision == "SAME_RULE" else "RELATED_CANDIDATE",
                "from_candidate_id": a_id, "to_candidate_id": b_id,
                "lifecycle_state": lifecycle_state, "reason": reason})

        result = {
            "proposed_edges": proposed_edges,
            "identity_decisions": identity_decisions,
            "rejected_edges": rejected_edges,
            "omitted_comparisons": omitted_pairs,
            # Coverage disclosure: omitted pairs that shared a subject+predicate
            # (could have been SAME_RULE). 0 means every comparable pair was
            # examined; > 0 means the result is INCOMPLETE for those pairs.
            "omitted_identity_comparisons": omitted_identity_pairs,
            "pair_cap": _MAX_PAIR_COMPARISONS,
            "total_pairs": len(list(itertools.combinations(ids, 2))) if omitted_pairs
                           else len(pairs),
            "compared_candidate_count": len(ids),
            "executor_id": self.executor_id,
        }
        return ExecutionOutcome(result=result, model_id=None)
