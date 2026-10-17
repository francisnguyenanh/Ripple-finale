"""D0 (deterministic baseline) vs A1 (live model) ablation
comparison for one ingest/run id (an `artifact_id`).

This module is PROPOSED, not a binding interface any other reviewed task
depends on yet. It reads
only already-persisted, real job state -- the "extraction"/"relation"/
"finding" jobs chained to `run_id` exactly the way
`PostgresV2Store.knowledge_draft` chains them (same candidate-id-membership
boundary, not a second correlation mechanism) -- never a hand-built or
replayed fixture.

D0 (the deterministic baseline: the relation-graph and findings
comparators) contributes zero rules/relations by
construction: neither `RelationGraphExecutor` nor `FindingsExecutor` reaches
a model or invents a candidate on its own; every "rule" and "relation" this
report counts as new is something the live model's extraction stage (A1)
produced that the deterministic baseline could never have proposed on its
own (D0 has no source of candidates without A1's extraction). Reporting
`new_rules`/`new_useful_relations` as the A1-only counts is therefore an
honest "vs. D0" comparison, not an inflated one. The comparison is safe
without a held-out oracle (no answer-mapping is consulted here; this only
counts what already happened in this run).

"useful relation" counts every extraction candidate that reached the
relation/finding stage as a real node in the semantic graph (i.e. was
resolved by `find_candidates_by_id` and compared/grouped), not only a
candidate pair that happened to produce an edge -- a single-candidate run
still has a real, useful (if edge-less) relation-graph placement, and a
multi-candidate run's proposed SAME_RULE edges count too. This definition is
recorded here explicitly (not assumed) because no other module defines
"useful relation".
"""

from __future__ import annotations

from infra.adapters.v2_postgres import _as_object


def compare_d0_a1(store, tenant_id: str, workspace_id: str, *, run_id: str) -> dict:
    draft = store.knowledge_draft(tenant_id, workspace_id, run_id)

    new_rules = sum(1 for c in draft.get("candidates", [])
                    if isinstance(c, dict) and c.get("applicability") == "RECOGNIZED")
    abstentions = sum(1 for c in draft.get("candidates", [])
                      if isinstance(c, dict) and c.get("applicability") == "UNKNOWN")

    false_positives = 0
    with store._tx() as cur:
        cur.execute("""SELECT execution_status, execution_result, payload FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='extraction'
              AND payload->>'artifact_id'=%s""",
            (tenant_id, workspace_id, run_id))
        extraction_rows = cur.fetchall()
    for status, result, _payload in extraction_rows:
        r = _as_object(result) or {}
        if status == "COMPLETED":
            false_positives += len(r.get("rejected") or [])

    with store._tx() as cur:
        cur.execute("""SELECT execution_status, execution_result, payload FROM v2_jobs
            WHERE tenant_id=%s AND workspace_id=%s AND kind='relation'""",
            (tenant_id, workspace_id))
        relation_rows = cur.fetchall()
    candidate_ids = {c.get("candidate_id") for c in draft.get("candidates", [])
                     if isinstance(c, dict)}
    same_rule_edges = 0
    compared_nodes = 0
    for status, result, payload in relation_rows:
        p = _as_object(payload) or {}
        ids = set(p.get("candidate_ids") or [])
        if not (ids & candidate_ids) or status != "COMPLETED":
            continue
        r = _as_object(result) or {}
        compared_nodes += int(r.get("compared_candidate_count") or 0)
        same_rule_edges += sum(1 for edge in (r.get("proposed_edges") or [])
                               if edge.get("lifecycle_state") == "PROPOSED")
        false_positives += len(r.get("rejected_edges") or [])

    # A "useful relation" is either a real proposed SAME_RULE edge, or (with
    # too few candidates for a pair to exist) the candidate itself being
    # placed as a real, resolved node in the semantic-relation stage.
    new_useful_relations = same_rule_edges or compared_nodes

    return {"run_id": run_id, "new_rules": new_rules,
            "new_useful_relations": new_useful_relations,
            "false_positives": false_positives, "abstentions": abstentions,
            "note": "D0 (deterministic relation/finding comparators) proposes "
                    "zero rules/relations without A1's live extraction; all "
                    "counts above are A1-only, so 'new' vs D0 is exact, not "
                    "an overstatement."}
