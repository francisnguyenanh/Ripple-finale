"""Semantic conflict / GAP finding generator, job kind "finding".

Deterministic, zero-provider-call executor (`external = False`, same
posture as `infra.worker.relations.RelationGraphExecutor`) that groups
already-persisted "extraction" candidates by (subject, predicate) and
reports a `Finding` for every group that is genuinely comparable yet
disagrees, WITHOUT ever letting a majority of sources silently outvote a
real single-source conflict ("minority source" case: every group member
that disagrees on `value`/`scope`/`outcome` is reported, not just the
majority position).

Not-comparable pairs are never compared as conflicts (currency/period
boundary): a `unit` mismatch is an explicit, structural "not comparable"
signal (VND vs USD), so it never becomes a CONFLICT finding -- it is instead
recorded as `insufficient_context` so the boundary is visible, not silently
dropped. (This module has no period-range field to compare on structurally;
a source-text period distinction that never reaches the persisted candidate
fields is out of this executor's reach by construction, and is not guessed.)

Duplicate chunks: candidate identity is already deduplicated inside the
"extraction" job (bucket-by-fingerprint merge), and this executor
additionally de-dupes the caller's own `candidate_ids` list, so the same
evidence can never cast two support votes here.

Insufficient evidence: a referenced candidate whose `applicability` is
"UNKNOWN" (the extraction missing-field guard) becomes a GAP finding plus an
open, unanswerable `question` -- never a silently-defaulted answer.

Prompt-injection / instruction-to-close resistance: this executor makes
no provider call at all and never emits `lifecycle_state` "RESOLVED" or
"VERIFIED_RESOLVED" for any finding under any circumstance -- only a human
decision can resolve a finding, so text embedded in a source document
has no channel to close one here.

Supporting-ref / stale-context boundary: every finding's
`involved_claim_ids`/evidence only ever cites the pinned candidate/source
revision ids already resolved by `find_candidates_by_id` -- never a
workspace's current head -- so a source republished after extraction cannot
retroactively appear as this finding's evidence.
"""

from __future__ import annotations

from infra.worker.execution_types import (
    ExecutionContext, ExecutionOutcome, ExecutionRequest,
    NonRetryableExecutionError, ProviderCallBudget,
)
from infra.worker.rule_identity import (
    identity_key, outcome_key, scope_key, scope_relation, unit_key, value_comparison_detail,
)


_scope_key = scope_key  # shared canonical scope (rule_identity)


# `Finding.severity`/`Finding.authority_required` are declared on the schema
# (core/change_intelligence/v2/schema.py:751,753); populating them gives the
# critical-gate a real signal to check besides a caller-asserted boolean.
# `RuleCandidate.modality` (must/may/observed,
# core/change_intelligence/v2/schema.py:490, already validated/populated by
# extraction) is the only real, already-populated signal available at
# this point that distinguishes a normative/binding rule from an
# observational one -- so it is the basis for this derivation. This is a
# deliberately small, explicitly-documented mapping, not a fabricated "critical" flag invented from nothing:
# a `must`-modality candidate involved in a finding makes that finding HIGH
# severity / policy_owner authority; `may` makes it MEDIUM / knowledge_owner;
# `observed`-only (or no modality data at all, e.g. a referenced candidate
# that could not be found) makes it LOW / knowledge_owner -- LOW is the
# lowest-confidence value FINDING_SEVERITIES actually allows (there is no
# "UNKNOWN" severity in the schema's enum), so a no-signal case is recorded
# honestly as low-confidence rather than silently defaulted to a value that
# would let a genuinely critical finding pass as non-critical.
def _severity_and_authority(modalities):
    modality_set = {m for m in modalities if m}
    if "must" in modality_set:
        return "HIGH", "policy_owner"
    if "may" in modality_set:
        return "MEDIUM", "knowledge_owner"
    return "LOW", "knowledge_owner"


# Scope-aware conflict rule: the SAME value stated for a scope that
# contains / overlaps another scope (universal vs restricted, nested, overlapping) is a scope_mismatch,
# non-equivalent, and is NOT a CONFLICT (LABELING_RUBRIC: conflicts_with = "cannot both hold"; not_equivalent = "both can
# hold together"). It is recorded in `scope_comparisons`, never dropped. This module-level flag lets the decision be
# flipped without a code change; its default encodes the decision and is echoed in every result's `scope_policy`.
NESTED_SAME_VALUE_IS_CONFLICT = False
SCOPE_RULE_VERSION = "wp5.scope_aware.v1"


def scope_policy() -> dict:
    return {"rule_version": SCOPE_RULE_VERSION,
            "nested_same_value_is_conflict": bool(NESTED_SAME_VALUE_IS_CONFLICT)}


def _operator_interval(c: dict):
    """(lo, hi, lo_closed, hi_closed) for a numeric comparison statement (`operator` + numeric `value`), else None."""
    op, v = c.get("operator"), c.get("value")
    if op not in ("<", "<=", ">", ">=", "==") or isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    inf = float("inf")
    return {"<": (-inf, v, False, False), "<=": (-inf, v, False, True), ">": (v, inf, False, False),
            ">=": (v, inf, True, False), "==": (v, v, True, True)}[op]


def _intervals_overlap(x, y) -> bool:
    lo, hi = max(x[0], y[0]), min(x[1], y[1])
    if lo < hi:
        return True
    if lo > hi:
        return False
    closed = lambda i, point: (i[2] if point == i[0] else True) and (i[3] if point == i[1] else True)
    return closed(x, lo) and closed(y, lo)


def _operator_verdict(a: dict, b: dict):
    """Two statements of one family whose `operator` fields differ (or are only on one side): compatible branches such as
    `amount <= 0 -> invalid` and `amount >= 1_000_000 -> needs Manager` are NOT a conflict because their numbers differ.
    -> None (not applicable: use the plain value comparison) | (verdict, reason)."""
    oa, ob = a.get("operator"), b.get("operator")
    if oa is None and ob is None:
        return None
    if oa == ob:
        return None                       # same operator: the plain value/outcome comparison applies
    ia, ib = _operator_interval(a), _operator_interval(b)
    if ia is None or ib is None:
        return "unresolved", "operator_not_comparable"
    if not _intervals_overlap(ia, ib):
        return "none", "disjoint_operator_ranges"
    if outcome_key(a.get("outcome")) != outcome_key(b.get("outcome")):
        return "conflict", "overlapping_operator_ranges_different_outcome"
    return "unresolved", "overlapping_operator_ranges_same_outcome"


def _pair_verdict(a: dict, b: dict) -> tuple[str, str, str]:
    """-> (verdict, reason, scope_relation); verdict in conflict | none | unresolved.
    conflict   : the two statements cannot both hold (same applicability, different value/outcome; or overlapping
                 scopes where the rule applies on the intersection and the value/outcome differs).
    none       : agree, or scopes disjoint (both can hold: not_equivalent).
    unresolved : scope unknown / intersection undecidable from the canonical vocabulary, or an operator-bearing value
                 whose comparison is not decidable. Never a conflict, never silently dropped."""
    rel = scope_relation(a.get("scope"), b.get("scope"))
    if rel == "disjoint":
        return "none", "scope_mismatch_disjoint", rel
    op_verdict = _operator_verdict(a, b)
    if op_verdict is not None and rel not in ("unknown", "undecidable"):
        return op_verdict[0], op_verdict[1], rel
    vcmp, vreason = value_comparison_detail(a.get("value"), b.get("value"), a.get("unit"), b.get("unit"))
    outcome_same = outcome_key(a.get("outcome")) == outcome_key(b.get("outcome"))
    if rel in ("unknown", "undecidable") and vcmp == "same" and outcome_same:
        # Same value and outcome: whatever the scopes are, the statements agree. No finding (no INSUFFICIENT_CONTEXT
        # noise for an unscoped / undecidable pair that states the same thing).
        return "none", "agree_scope_not_decidable", rel
    if rel == "unknown":
        return "unresolved", "scope_unresolved_not_comparable", rel
    if rel == "undecidable":
        return "unresolved", "scope_intersection_undecidable", rel
    if vcmp == "undecidable":
        return "unresolved", vreason, rel
    outcome_differs = outcome_key(a.get("outcome")) != outcome_key(b.get("outcome"))
    if vcmp == "differ" or outcome_differs:
        return "conflict", "same_subject_predicate_disagreeing_value_scope_or_outcome", rel
    if rel != "equivalent":  # same value + outcome, containing / overlapping scopes (row 2)
        if NESTED_SAME_VALUE_IS_CONFLICT:
            return "conflict", "same_subject_predicate_disagreeing_value_scope_or_outcome", rel
        return "none", "scope_mismatch_same_value", rel
    return "none", "agree", rel


def _components(ids: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


class FindingsExecutor:
    """See module docstring.

    A findings job created by the manual-refresh consumer carries
    `payload.refresh_reanalysis = {extraction_job_ids, bound_candidate_ids}`. Its candidate
    set is the candidates the bounded re-extraction of the changed artifacts produced
    plus the candidates bound to an ACTIVE human decision that share a
    (subject, predicate) with them, so an external edit that contradicts an accepted
    decision opens a CONFLICT finding through the ordinary grouping below. Nothing is
    accepted, revoked or reverted here."""

    external = False
    executor_id = "findings.semantic_conflict_gap.v1"

    def prepare(self, ctx: ExecutionContext) -> ExecutionRequest:
        payload = ctx.job["payload"]
        candidate_ids = payload.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            raise NonRetryableExecutionError("candidate_ids_required",
                                             "payload.candidate_ids must be a list")
        return ExecutionRequest(prompt=None,
                                schema={"type": "object", "executor": self.executor_id},
                                provider_calls_planned=0, external=False)

    @staticmethod
    def _binding_fresh(ctx: ExecutionContext, decision: dict, claim_ids, found: dict) -> bool:
        """Is the evidence state `decision` was taken on still the current state?
        Decisions recorded before bindings existed fall back to the D1 check (claim vs
        the revision it was extracted from)."""
        job, store = ctx.job, ctx.store
        binding = decision.get("binding")
        for cid in claim_ids:
            recorded = ((binding or {}).get("claims") or {}).get(cid)
            if recorded is not None:
                if recorded.get("artifact_id") is None:
                    continue  # nothing was bound: nothing can go stale
                if store.artifact_head_hash(job["tenant_id"], job["workspace_id"],
                                            recorded["artifact_id"]) != recorded.get("blob_hash"):
                    return False
            elif cid in found and store.claim_binding_stale(
                    job["tenant_id"], job["workspace_id"], found[cid]):
                return False
        return True

    @staticmethod
    def _refresh_candidates(ctx: ExecutionContext, reanalysis: dict, base_ids: list) -> list:
        from infra.adapters.v2_postgres import TenantScopeError
        from infra.worker.execution_types import ProviderExecutionError
        job = ctx.job
        new_ids: list = []
        for extraction_job_id in reanalysis.get("extraction_job_ids") or []:
            try:
                extraction = ctx.store.job_record(job["tenant_id"], job["workspace_id"],
                                                  extraction_job_id)
            except TenantScopeError:
                continue  # the re-extraction was never scheduled (reported by the consumer)
            if extraction["execution_status"] in ("QUEUED", "RUNNING"):
                # Not yet terminal: retry later, never compare against a partial set.
                raise ProviderExecutionError(f"re-extraction {extraction_job_id} still running")
            if extraction["execution_status"] != "COMPLETED":
                continue  # failed/cancelled: nothing new to compare (visible in the status)
            for cand in (extraction["execution_result"] or {}).get("candidates") or []:
                if isinstance(cand, dict) and cand.get("candidate_id"):
                    new_ids.append(cand["candidate_id"])
        new_ids = list(dict.fromkeys(new_ids))
        if not new_ids:
            return list(base_ids)
        new_found, _ = ctx.store.find_candidates_by_id(job["tenant_id"], job["workspace_id"], new_ids)
        keys = {identity_key(c) for c in new_found.values()}
        bound_ids = list(dict.fromkeys(reanalysis.get("bound_candidate_ids") or []))
        bound_found, _ = ctx.store.find_candidates_by_id(job["tenant_id"], job["workspace_id"], bound_ids)
        related = [cid for cid in bound_ids if cid in bound_found
                   and identity_key(bound_found[cid]) in keys]
        return list(dict.fromkeys([*base_ids, *new_ids, *related]))

    @staticmethod
    def _compare_unit_group(members, found, findings, scope_comparisons, next_id, pair_log):
        """Pairwise scope-aware comparison of one comparable (same subject/predicate/unit) group. Conflicting pairs
        are merged into connected components (one CONFLICT per component, member order preserved -- identical to the
        old single group finding whenever every member conflicts); unresolved pairs likewise become one
        INSUFFICIENT_CONTEXT per component; disjoint / same-value-nested pairs are only recorded."""
        conflict_edges, unresolved_edges, unresolved_reason = [], [], {}
        for i, ca in enumerate(members):
            for cb in members[i + 1:]:
                a, b = found[ca], found[cb]
                verdict, reason, rel = _pair_verdict(a, b)
                pair_log.append({"claim_ids": [ca, cb], "verdict": verdict, "reason": reason, "scope_relation": rel})
                if verdict == "conflict":
                    conflict_edges.append((ca, cb))
                elif verdict == "unresolved":
                    unresolved_edges.append((ca, cb))
                    unresolved_reason[(ca, cb)] = reason
                elif reason.startswith("scope_mismatch"):
                    scope_comparisons.append({
                        "kind": "scope_mismatch", "claim_ids": [ca, cb], "scope_relation": rel,
                        "reason": reason, "conflict": False})
        for kind, edges, reason_of in (
                ("CONFLICT", conflict_edges, lambda comp: "same_subject_predicate_disagreeing_value_scope_or_outcome"),
                ("INSUFFICIENT_CONTEXT", unresolved_edges,
                 lambda comp: next(unresolved_reason[e] for e in unresolved_edges if e[0] in comp))):
            for comp in _components(members, edges):
                severity, authority_required = _severity_and_authority(
                    found[cid].get("modality") for cid in comp)
                findings.append({
                    "finding_id": next_id(), "kind": kind, "lifecycle_state": "OPEN",
                    "reason": reason_of(comp), "involved_claim_ids": comp,
                    "severity": severity, "authority_required": authority_required})

    def execute(self, ctx: ExecutionContext, request: ExecutionRequest,
               calls: ProviderCallBudget) -> ExecutionOutcome:
        job = ctx.job
        candidate_ids = list(dict.fromkeys(job["payload"].get("candidate_ids") or []))
        reanalysis = job["payload"].get("refresh_reanalysis")
        if isinstance(reanalysis, dict):
            candidate_ids = self._refresh_candidates(ctx, reanalysis, candidate_ids)
        found, missing = ctx.store.find_candidates_by_id(
            job["tenant_id"], job["workspace_id"], candidate_ids)

        findings: list[dict] = []
        questions: list[dict] = []
        scope_comparisons: list[dict] = []
        pair_log: list[dict] = []
        finding_seq = 0

        def _next_id():
            nonlocal finding_seq
            finding_seq += 1
            return f"finding-{job['job_id']}-{finding_seq:04d}"

        for cid in missing:
            # No candidate could be resolved at all -- no modality data
            # exists to derive a real signal from; see _severity_and_authority.
            severity, authority_required = _severity_and_authority([])
            findings.append({
                "finding_id": _next_id(), "kind": "GAP",
                "lifecycle_state": "OPEN",
                "reason": "referenced_candidate_not_found",
                "involved_claim_ids": [cid],
                "severity": severity, "authority_required": authority_required,
            })

        unknown_ids = [cid for cid in candidate_ids if cid in found
                      and found[cid].get("applicability") == "UNKNOWN"]
        for cid in unknown_ids:
            finding_id = _next_id()
            severity, authority_required = _severity_and_authority(
                [found[cid].get("modality")])
            findings.append({
                "finding_id": finding_id, "kind": "GAP",
                "lifecycle_state": "OPEN",
                "reason": found[cid].get("applicability_reason") or "insufficient_evidence",
                "involved_claim_ids": [cid],
                "severity": severity, "authority_required": authority_required,
            })
            questions.append({
                "id": f"question-{finding_id}", "finding_ids": [finding_id],
                "options": [], "dependents": []})

        # Group only the resolvable, non-UNKNOWN candidates by (subject,
        # predicate) -- the same identity-bearing fields the relation graph uses.
        groups: dict[tuple, list[str]] = {}
        for cid in candidate_ids:
            candidate = found.get(cid)
            if candidate is None or candidate.get("applicability") == "UNKNOWN":
                continue
            key = identity_key(candidate)
            groups.setdefault(key, []).append(cid)

        for (subject, predicate), members in groups.items():
            if len(members) < 2:
                continue
            # Only compare members sharing a comparable unit -- a currency
            # (or otherwise unit-) mismatch is never silently treated as a
            # conflict, per the currency-mismatch boundary.
            by_unit: dict[object, list[str]] = {}
            for cid in members:
                by_unit.setdefault(unit_key(found[cid].get("unit")), []).append(cid)
            if len(by_unit) > 1:
                severity, authority_required = _severity_and_authority(
                    found[cid].get("modality") for cid in members)
                findings.append({
                    "finding_id": _next_id(), "kind": "INSUFFICIENT_CONTEXT",
                    "lifecycle_state": "OPEN",
                    "reason": "unit_mismatch_not_comparable",
                    "involved_claim_ids": members,
                    "severity": severity, "authority_required": authority_required,
                })
            for unit, unit_members in by_unit.items():
                self._compare_unit_group(unit_members, found, findings, scope_comparisons, _next_id, pair_log)

        # The ONE place a finding is generated. A finding over exactly the
        # claim set (and kind) a human already decided (resolve / accepted_unknown, still
        # active) is not opened again under a new id -- the reanalysis of a dependent job
        # re-derives the same conflict from the same immutable claims. It is reported in
        # `suppressed_decided`, never dropped silently, and only while the decision's
        # binding is still fresh: if a claim's artifact changed since the claim was
        # extracted (or a refresh deleted it) the finding opens again. A different claim
        # set (new evidence, refresh, restore) is never matched, so a NEW conflict opens
        # and blocks completion as before. Nothing is accepted or legitimised here.
        suppressed: list[dict] = []
        decided: dict = {}
        for row in ctx.store.decided_findings(job["tenant_id"], job["workspace_id"]):
            decided.setdefault((row["kind"], frozenset(row["involved_claim_ids"])), []).append(row)
        if decided and findings:
            kept_findings = []
            for finding in findings:
                key = (finding["kind"], frozenset(finding.get("involved_claim_ids") or []))
                # D2: a decision stays valid for the evidence state it was TAKEN on. Any one
                # active decision over this exact (kind, claim set) whose binding is still
                # fresh covers the regenerated finding. A resolve taken AFTER a source change
                # binds to the changed bytes, so it sticks; a decision whose bytes moved since
                # it was taken (a later edit / refresh / restore) is stale, so the finding
                # opens again (G-1 / D1 semantics).
                prior = next((r for r in decided.get(key, [])
                              if self._binding_fresh(ctx, r, key[1], found)), None)
                if prior is None:
                    kept_findings.append(finding)
                else:
                    suppressed.append({"kind": finding["kind"],
                                       "involved_claim_ids": sorted(key[1]),
                                       "decided_finding_id": prior["finding_id"],
                                       "decision_id": prior["decision_id"],
                                       "action": prior["action"]})
            gone = {f["finding_id"] for f in findings} - {f["finding_id"] for f in kept_findings}
            findings = kept_findings
            questions = [q for q in questions if not set(q.get("finding_ids") or []) <= gone
                         or not q.get("finding_ids")]
        # A decided CONFLICT must never vanish silently: if this run neither re-opened it nor suppressed it (the claim
        # set no longer forms a CONFLICT under the current rules), report it with the current verdict of every pair.
        not_reproduced: list[dict] = []
        covered = {(s["kind"], frozenset(s["involved_claim_ids"])) for s in suppressed}
        covered |= {(f["kind"], frozenset(f.get("involved_claim_ids") or [])) for f in findings}
        for (kind, claim_set), rows in decided.items():
            if kind != "CONFLICT" or (kind, claim_set) in covered or not claim_set <= set(found):
                continue
            not_reproduced.append({
                "kind": kind, "involved_claim_ids": sorted(claim_set),
                "decided_finding_id": rows[0]["finding_id"], "decision_id": rows[0]["decision_id"],
                "current_pair_verdicts": [p for p in pair_log if set(p["claim_ids"]) <= claim_set]})
        result = {
            "findings": findings,
            "decided_not_reproduced": not_reproduced,
            "questions": questions,
            "suppressed_decided": suppressed,
            "scope_comparisons": scope_comparisons,
            "scope_policy": scope_policy(),
            "executor_id": self.executor_id,
        }
        return ExecutionOutcome(result=result, model_id=None)
