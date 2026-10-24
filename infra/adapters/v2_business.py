"""Business knowledge snapshots (contract fkl/1, docs/contracts/FINAL_KNOWLEDGE_LOOP.md): atomic commit, read model, legacy backfill.

Rules this module keeps:
  * One commit is ONE transaction under the workspace head lock: snapshot, rules, relations, gaps, decisions, head pointer, workspace version,
    idempotency ledger, audit and outbox. Any failure rolls all of it back.
  * Compare-and-set on the workspace version, the pinned source / knowledge / decision-epoch heads and the business revision. A moved pin is
    `StaleVersion`; nothing is written.
  * A commit never writes the observed state: `v2_workspaces.source_revision_id`, `knowledge_revision_id` and `decision_epoch` are untouched,
    so a decided target can never edit the source head or pose as an observed fact. The rule `authority` of an id cannot flip between snapshots.
  * Every reference is resolved inside the transaction (source revisions, decisions, rules named by relations and gaps); unknown ones refuse the
    whole commit. The current authority of the committing actor is rechecked under the same locks for an ACCEPTED snapshot.
  * Reads take one REPEATABLE READ READ ONLY snapshot and are scoped by tenant and workspace.
"""
from __future__ import annotations

from typing import Optional

from psycopg2.extras import Json

from core.change_intelligence.v2 import business_answers, business_graph, business_knowledge as bk
from core.change_intelligence.v2.serialize import canonical_hash
from core.change_intelligence.v2.store_ports import PublishResult
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, _as_object
from infra.adapters.v2_restore import read_snapshot

OPERATION = "business.commit"
EVENT = "BUSINESS_KNOWLEDGE_COMMITTED"
CONFIRM_PERMISSION = "knowledge.confirm"
LIFECYCLE_STATES = ("PENDING_HUMAN", "ACCEPTED")


class BusinessKnowledgeRefused(StoreConflict):
    """A commit refused for a reason in the contract (`code`): unknown_reference, duplicate_id, authority_change."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _ids(items, key):
    seen = set()
    for item in items:
        if item[key] in seen:
            raise BusinessKnowledgeRefused("duplicate_id", f"{key} {item[key]!r} appears twice in one snapshot")
        seen.add(item[key])
    return seen


def _decision_ids_in(rule_or_edge):
    return [p["decision_id"] for p in (rule_or_edge.get("provenance") or []) if p.get("kind") == "human_decision"]


def commit_snapshot(store, tenant_id, workspace_id, *, snapshot_id, actor_id, lifecycle_state, rules, relations=(), gaps=(), decisions=(),
                    expected_version, expected_business_revision, source_revision_id, km_revision_id, decision_epoch,
                    idempotency_key, coverage=None, recheck_authority=False) -> PublishResult:
    """Persist one business snapshot. `ACCEPTED` moves the business head (revision + 1); `PENDING_HUMAN` is a proposal and never does."""
    if lifecycle_state not in LIFECYCLE_STATES:
        raise ValueError(f"lifecycle_state must be one of {LIFECYCLE_STATES}")
    for name, value in (("snapshot_id", snapshot_id), ("actor_id", actor_id), ("idempotency_key", idempotency_key), ("source_revision_id", source_revision_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} required")
    rules, relations, gaps, decisions = [dict(r) for r in rules], [dict(r) for r in relations], [dict(g) for g in gaps], [dict(d) for d in decisions]
    for rule in rules:
        bk.validate_business_rule(rule)
    for rel in relations:
        bk.validate_relation(rel)
    for gap in gaps:
        bk.validate_gap(gap)
    for d in decisions:
        for k in ("decision_id", "kind", "quote"):
            if not isinstance(d.get(k), str) or not d[k].strip():
                raise bk.ContractError("invalid_field", f"decision {k} required")
    rule_ids = _ids(rules, "rule_id")
    _ids(relations, "edge_id")
    _ids(gaps, "gap_id")
    own_decisions = _ids(decisions, "decision_id")
    for rel in relations:
        for end in (rel["from"], rel["to"]):
            if end.startswith("rule-") and end not in rule_ids:
                raise BusinessKnowledgeRefused("unknown_reference", f"relation {rel['edge_id']} names {end}, which is not a rule of this snapshot")
    for gap in gaps:
        for rid in gap["affected_rule_ids"]:
            if rid not in rule_ids:
                raise BusinessKnowledgeRefused("unknown_reference", f"gap {gap['gap_id']} names {rid}, which is not a rule of this snapshot")

    payload_hash = canonical_hash({
        "snapshot_id": snapshot_id, "actor_id": actor_id, "lifecycle_state": lifecycle_state, "rules": rules, "relations": relations, "gaps": gaps,
        "decisions": decisions, "expected_version": expected_version, "expected_business_revision": expected_business_revision,
        "source_revision_id": source_revision_id, "km_revision_id": km_revision_id, "decision_epoch": decision_epoch, "coverage": coverage or {}})
    content_hash = canonical_hash({"rules": rules, "relations": relations, "gaps": gaps})

    with store._tx() as cur:
        # AH-03 lock order (account, membership, then workspace head: the order `revoke_membership` takes, membership then workspace): the authority recheck comes
        # BEFORE the head lock; the reverse order deadlocked against a concurrent revocation (two-connection test). An idempotent replay is still answered first.
        prior = store._replay(cur, tenant_id, workspace_id, OPERATION, idempotency_key, payload_hash)
        if prior is not None:
            return prior
        if lifecycle_state == "ACCEPTED" or recheck_authority:
            store._recheck_actor_authority(cur, tenant_id, workspace_id, actor_id, CONFIRM_PERMISSION)
        head = store._lock_head(cur, tenant_id, workspace_id)
        prior = store._replay(cur, tenant_id, workspace_id, OPERATION, idempotency_key, payload_hash)     # a commit that won the race while this one waited
        if prior is not None:
            return prior
        if head.version != expected_version:
            raise StaleVersion("stale_workspace_version")
        if (head.source_revision_id != source_revision_id or head.knowledge_revision_id != km_revision_id
                or head.decision_epoch != decision_epoch):
            raise StaleVersion("stale_snapshot_pin: the source, knowledge or decision head moved")
        cur.execute("""INSERT INTO v2_business_heads (tenant_id, workspace_id) VALUES (%s,%s) ON CONFLICT DO NOTHING""", (tenant_id, workspace_id))
        cur.execute("""SELECT business_revision, accepted_snapshot_id FROM v2_business_heads
            WHERE tenant_id=%s AND workspace_id=%s FOR UPDATE""", (tenant_id, workspace_id))
        current_revision, parent_snapshot_id = cur.fetchone()
        if lifecycle_state == "ACCEPTED" and current_revision != expected_business_revision:
            raise StaleVersion("stale_business_revision")

        # references, resolved inside the transaction
        bindings = {b["revision_id"] for r in rules for b in r["source_bindings"]} | {source_revision_id}
        cur.execute("""SELECT revision_id FROM v2_source_revisions WHERE tenant_id=%s AND workspace_id=%s AND revision_id = ANY(%s)""",
                    (tenant_id, workspace_id, sorted(bindings)))
        missing = bindings - {row[0] for row in cur.fetchall()}
        if missing:
            raise BusinessKnowledgeRefused("unknown_reference", f"source revision(s) {sorted(missing)} do not exist in this workspace")
        wanted = {d for r in rules for d in _decision_ids_in(r)} | {ref for rel in relations if rel["status"] == "CONFIRMED" for ref in rel["provenance_refs"]}
        wanted -= own_decisions
        if wanted:
            cur.execute("""SELECT decision_id FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND decision_id = ANY(%s)
                UNION SELECT decision_id FROM v2_decisions WHERE tenant_id=%s AND workspace_id=%s AND decision_id = ANY(%s)""",
                        (tenant_id, workspace_id, sorted(wanted), tenant_id, workspace_id, sorted(wanted)))
            unknown = wanted - {row[0] for row in cur.fetchall()}
            if unknown:
                raise BusinessKnowledgeRefused("unknown_reference", f"decision(s) {sorted(unknown)} do not exist in this workspace")
        if parent_snapshot_id is not None:
            cur.execute("""SELECT rule_id, authority FROM v2_business_rules WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s AND rule_id = ANY(%s)""",
                        (tenant_id, workspace_id, parent_snapshot_id, sorted(rule_ids)))
            before = dict(cur.fetchall())
            flipped = sorted(r["rule_id"] for r in rules if r["rule_id"] in before and before[r["rule_id"]] != r["authority"])
            if flipped:
                raise BusinessKnowledgeRefused("authority_change", f"rule(s) {flipped} would change between OBSERVED and TARGET")

        new_revision = current_revision + 1 if lifecycle_state == "ACCEPTED" else None
        cur.execute("""SELECT 1 FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s""", (tenant_id, workspace_id, snapshot_id))
        if cur.fetchone() is not None:
            raise BusinessKnowledgeRefused("duplicate_id", f"snapshot {snapshot_id!r} already exists; a snapshot is immutable")
        cur.execute("""INSERT INTO v2_business_snapshots (tenant_id, workspace_id, snapshot_id, lifecycle_state, business_revision, parent_snapshot_id,
                source_revision_id, km_revision_id, decision_epoch, schema_version, content_hash, coverage, committed_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, snapshot_id, lifecycle_state, new_revision, parent_snapshot_id, source_revision_id, km_revision_id,
                     decision_epoch, bk.CONTRACT_VERSION, content_hash, Json(coverage or {}), actor_id))
        for rule in rules:
            cur.execute("""INSERT INTO v2_business_rules (tenant_id, workspace_id, snapshot_id, rule_id, authority, status, implementation_status, body)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, snapshot_id, rule["rule_id"], rule["authority"], rule["status"], rule["implementation_status"], Json(rule)))
        for rel in relations:
            cur.execute("""INSERT INTO v2_business_relations (tenant_id, workspace_id, snapshot_id, edge_id, status, body) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, snapshot_id, rel["edge_id"], rel["status"], Json(rel)))
        for gap in gaps:
            cur.execute("""INSERT INTO v2_business_gaps (tenant_id, workspace_id, snapshot_id, gap_id, status, body) VALUES (%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, snapshot_id, gap["gap_id"], gap["status"], Json(gap)))
        for d in decisions:
            cur.execute("""INSERT INTO v2_business_decisions (tenant_id, workspace_id, decision_id, snapshot_id, actor_id, kind, quote, body)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (tenant_id, workspace_id, d["decision_id"], snapshot_id, actor_id, d["kind"], d["quote"], Json(d.get("body") or {})))
        if lifecycle_state == "ACCEPTED":
            cur.execute("""UPDATE v2_business_heads SET business_revision=%s, accepted_snapshot_id=%s, updated_at=now()
                WHERE tenant_id=%s AND workspace_id=%s""", (new_revision, snapshot_id, tenant_id, workspace_id))
        version = head.version + 1
        cur.execute("UPDATE v2_workspaces SET version=%s WHERE tenant_id=%s AND workspace_id=%s", (version, tenant_id, workspace_id))
        result = PublishResult(version, snapshot_id)
        store._record_commit(cur, tenant_id, workspace_id, OPERATION, idempotency_key, payload_hash, result, EVENT,
                             {"snapshot_id": snapshot_id, "lifecycle_state": lifecycle_state, "business_revision": new_revision,
                              "source_revision_id": source_revision_id, "content_hash": content_hash, "committed_by": actor_id})
        return result


def _snapshot_view(cur, tenant_id, workspace_id, snapshot_id):
    cur.execute("""SELECT snapshot_id, lifecycle_state, business_revision, parent_snapshot_id, source_revision_id, km_revision_id, decision_epoch,
            schema_version, content_hash, coverage, committed_by, created_at FROM v2_business_snapshots
        WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s""", (tenant_id, workspace_id, snapshot_id))
    row = cur.fetchone()
    if row is None:
        return None
    meta = {"snapshot_id": row[0], "lifecycle_state": row[1], "business_revision": row[2], "parent_snapshot_id": row[3], "source_revision_id": row[4],
            "km_revision_id": row[5], "decision_epoch": row[6], "schema_version": row[7], "content_hash": row[8], "coverage": _as_object(row[9]),
            "committed_by": row[10], "created_at": row[11].isoformat()}
    out = {"snapshot": meta}
    for key, table, order in (("rules", "v2_business_rules", "rule_id"), ("relations", "v2_business_relations", "edge_id"), ("gaps", "v2_business_gaps", "gap_id")):
        cur.execute(f"SELECT body FROM {table} WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s ORDER BY {order}", (tenant_id, workspace_id, snapshot_id))
        out[key] = [_as_object(r[0]) for r in cur.fetchall()]
    cur.execute("""SELECT decision_id, actor_id, kind, quote, body FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s
        ORDER BY decision_id""", (tenant_id, workspace_id, snapshot_id))
    out["decisions"] = [{"decision_id": r[0], "actor_id": r[1], "kind": r[2], "quote": r[3], "body": _as_object(r[4])} for r in cur.fetchall()]
    return out


def business_view(store, tenant_id, workspace_id) -> dict:
    """The read model: workspace heads, the latest ACCEPTED business snapshot, the latest pending proposal, and the per-rule implementation state.

    `observed_stale` tells that the source moved after the accepted snapshot; the accepted target stays valid, its observed rules are then old news."""
    with read_snapshot(store) as cur:
        cur.execute("""SELECT version, source_revision_id, knowledge_revision_id, decision_epoch FROM v2_workspaces WHERE tenant_id=%s AND workspace_id=%s""",
                    (tenant_id, workspace_id))
        row = cur.fetchone()
        if row is None:
            from infra.adapters.v2_postgres import TenantScopeError
            raise TenantScopeError("workspace not found")
        workspace = {"version": row[0], "source_revision_id": row[1], "knowledge_revision_id": row[2], "decision_epoch": row[3]}
        cur.execute("SELECT business_revision, accepted_snapshot_id FROM v2_business_heads WHERE tenant_id=%s AND workspace_id=%s", (tenant_id, workspace_id))
        head_row = cur.fetchone() or (0, None)
        accepted = _snapshot_view(cur, tenant_id, workspace_id, head_row[1]) if head_row[1] else None
        cur.execute("""SELECT snapshot_id FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND lifecycle_state='PENDING_HUMAN'
            ORDER BY created_at DESC, snapshot_id DESC LIMIT 1""", (tenant_id, workspace_id))
        pending_row = cur.fetchone()
        pending = _snapshot_view(cur, tenant_id, workspace_id, pending_row[0]) if pending_row else None
    counts: dict = {}
    for rule in (accepted["rules"] if accepted else []):
        key = f"{rule['authority']}:{rule['implementation_status']}"
        counts[key] = counts.get(key, 0) + 1
    return {"workspace": workspace, "business_head": {"business_revision": head_row[0], "accepted_snapshot_id": head_row[1]}, "accepted": accepted,
            "pending": pending, "implementation": counts,
            "observed_stale": bool(accepted and accepted["snapshot"]["source_revision_id"] != workspace["source_revision_id"])}


def legacy_candidates(store, tenant_id, workspace_id) -> list[dict]:
    """The candidates of COMPLETED extraction jobs for the CURRENT source revision (newest job wins per candidate id). Read-only."""
    head = store.head(tenant_id, workspace_id)
    with read_snapshot(store) as cur:
        cur.execute("""SELECT j.execution_result FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction'
              AND j.execution_status='COMPLETED' AND j.execution_result IS NOT NULL
            ORDER BY (SELECT max(o.id) FROM v2_outbox o WHERE o.tenant_id=j.tenant_id AND o.workspace_id=j.workspace_id
                      AND o.kind='JOB_QUEUED' AND o.payload->>'job_id'=j.job_id) ASC NULLS FIRST, j.job_id ASC""", (tenant_id, workspace_id))
        results = [_as_object(r[0]) or {} for r in cur.fetchall()]
    by_id: dict = {}
    for result in results:
        for cand in result.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("candidate_id") and cand.get("source_revision_id") == head.source_revision_id:
                by_id[cand["candidate_id"]] = cand
    return [by_id[k] for k in sorted(by_id)]


def current_candidates(store, tenant_id, workspace_id) -> tuple:
    """The candidates the CURRENT revision's files stand for: per file, those of the newest completed extraction of this revision, or, when the file was not
    extracted again, of the newest earlier revision whose bytes are identical (carried, restamped to this revision and reported). A file that changed and was
    not extracted again contributes nothing (its rules are reported missing, never kept stale). -> (candidates, [{artifact_id, from_revision}])."""
    head = store.head(tenant_id, workspace_id)
    with read_snapshot(store) as cur:
        cur.execute("""SELECT source_revision_id, artifact_id, blob_hash FROM v2_artifact_revisions WHERE tenant_id=%s AND workspace_id=%s""", (tenant_id, workspace_id))
        blobs = {(r[0], r[1]): r[2] for r in cur.fetchall()}
        cur.execute("""SELECT j.execution_result FROM v2_jobs j WHERE j.tenant_id=%s AND j.workspace_id=%s AND j.kind='extraction'
              AND j.execution_status='COMPLETED' AND j.execution_result IS NOT NULL
            ORDER BY (SELECT max(a.recorded_at) FROM v2_job_attempts a WHERE a.tenant_id=j.tenant_id AND a.workspace_id=j.workspace_id AND a.job_id=j.job_id)
                     ASC NULLS FIRST, j.job_id ASC""", (tenant_id, workspace_id))             # newest completed attempt last (a person-approved job has no JOB_QUEUED outbox row)
        results = [_as_object(r[0]) or {} for r in cur.fetchall()]
    current = {aid: h for (rev, aid), h in blobs.items() if rev == head.source_revision_id}
    per_file: dict = {}                                                       # artifact -> (is current revision, revision, {candidate_id: candidate}); newest job last
    for result in results:
        by_file: dict = {}
        for cand in result.get("candidates") or []:
            if isinstance(cand, dict) and cand.get("candidate_id") and cand.get("artifact_revision_id") in current:
                by_file.setdefault((cand["artifact_revision_id"], cand.get("source_revision_id")), {})[cand["candidate_id"]] = cand
        for (aid, rev), cands in by_file.items():
            same = rev == head.source_revision_id or blobs.get((rev, aid)) == current[aid]
            if not same:
                continue
            prev = per_file.get(aid)
            if prev is None or not prev[0] or rev == head.source_revision_id:
                per_file[aid] = (rev == head.source_revision_id, rev, cands)
    out, carried = [], []
    for aid in sorted(per_file):
        is_current, rev, cands = per_file[aid]
        for cid in sorted(cands):
            out.append(cands[cid] if is_current else dict(cands[cid], source_revision_id=head.source_revision_id, carried_from_revision=rev))
        if not is_current:
            carried.append({"artifact_id": aid, "from_revision": rev})
    return out, carried


def backfill_observed_snapshot(store, tenant_id, workspace_id, *, snapshot_id, actor_id, idempotency_key, confirmations=None) -> dict:
    """Map the existing candidates to a PENDING_HUMAN snapshot of OBSERVED rules, keeping ids and evidence. Never accepts and never confirms a rule
    without the human decision id in `confirmations` (candidate id -> decision id). Candidates without evidence are reported, not invented."""
    head = store.head(tenant_id, workspace_id)
    confirmations = confirmations or {}
    rules, skipped, seen = [], [], set()
    for cand in legacy_candidates(store, tenant_id, workspace_id):
        try:
            rule = bk.observed_rule_from_candidate(cand, confirmation_decision_id=confirmations.get(cand["candidate_id"]))
        except bk.ContractError as exc:
            skipped.append({"candidate_id": cand["candidate_id"], "reason": exc.code})
            continue
        if rule["rule_id"] in seen:
            skipped.append({"candidate_id": cand["candidate_id"], "reason": "duplicate_rule_id"})
            continue
        seen.add(rule["rule_id"])
        rules.append(rule)
    gaps = business_graph.derive_gaps(rules, kinds=("missing_condition", "missing_actor"))
    # A re-run that finds the same snapshot id with identical content returns it (the head version has moved since, so the idempotency ledger alone cannot).
    with read_snapshot(store) as cur:
        cur.execute("""SELECT content_hash FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s""",
                    (tenant_id, workspace_id, snapshot_id))
        row = cur.fetchone()
    if row is not None and row[0] == canonical_hash({"rules": rules, "relations": [], "gaps": gaps}):
        return {"result": PublishResult(head.version, snapshot_id, True), "rules": len(rules), "skipped": skipped, "gaps": len(gaps)}
    result = commit_snapshot(store, tenant_id, workspace_id, snapshot_id=snapshot_id, actor_id=actor_id, lifecycle_state="PENDING_HUMAN", rules=rules,
                             gaps=gaps, expected_version=head.version, expected_business_revision=0, source_revision_id=head.source_revision_id,
                             km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch, idempotency_key=idempotency_key,
                             coverage={"origin": "legacy_candidates", "candidates": len(rules) + len(skipped), "skipped": skipped})
    return {"result": result, "rules": len(rules), "skipped": skipped, "gaps": len(gaps)}


# ---- graph, gap queue and analysis (KNO-03) -----------------------------------------------------------------------------------

def _working(view):
    """The snapshot people are working on: the latest proposal when it is newer than the accepted knowledge, else the accepted knowledge."""
    accepted, pending = view["accepted"], view["pending"]
    if pending is not None and (accepted is None or pending["snapshot"]["created_at"] > accepted["snapshot"]["created_at"]):
        return pending
    return accepted


def _latest_view(store, tenant_id, workspace_id):
    view = business_view(store, tenant_id, workspace_id)
    return view, _working(view)


def _merge_state(rules, base_relations, base_gaps):
    """Relations and gaps of `rules`: freshly derived ones, with CONFIRMED relations and every gap a person already touched carried over unchanged."""
    relations = {r["edge_id"]: r for r in business_graph.derive_relations(rules)}
    relations.update({r["edge_id"]: r for r in base_relations if r["status"] == "CONFIRMED"})
    old = {g["gap_id"]: g for g in base_gaps}
    gaps = {g["gap_id"]: old.get(g["gap_id"], g) for g in business_graph.derive_gaps(rules)}
    gaps.update({k: g for k, g in old.items() if k not in gaps and (g["status"] != "OPEN" or g["kind"] == "uncertain_link" or any(str(r).startswith("decision-") for r in g["evidence_refs"]))})
    return [relations[k] for k in sorted(relations)], [gaps[k] for k in sorted(gaps)]


def _scalar_reader(store, tenant_id, workspace_id, source_revision_id):
    """read_scalar(rule, binding) over the observations of one source revision: a YAML key or a cell by its parser label, resolved by artifact id or by a unique path."""
    from core.change_intelligence.v2 import business_draft as bd
    view = store.source_revision_view(tenant_id, workspace_id, source_revision_id)
    artifacts = {a["artifact_id"]: a.get("logical_path") or a["artifact_id"] for a in view["artifacts"]}
    by_art: dict = {}
    for o in store.source_observations(tenant_id, workspace_id, source_revision_id):
        by_art.setdefault(o.get("artifact_id"), []).append(o)

    def read_scalar(rule, binding):
        from core.change_intelligence.v2 import business_rebase as br
        path, _, loc = str(binding["evidence_ref"]).partition("#")
        aid, _rebound = bd._resolve_artifact(binding["artifact_id"], path, artifacts)
        if aid is None:
            return br.MISSING_ARTIFACT                                                  # the file is gone from this revision: a lost mapping, not an unreadable value
        if not loc:
            return None
        obs, how = bd._find_observation(by_art.get(aid, []), loc)
        if obs is None or how != "label":
            return None
        raw = obs.get("raw_value")
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return None
        if isinstance(raw, str) and bd._num(raw) is None:
            return None
        if aid != binding["artifact_id"]:
            binding["rebound_from"] = binding["artifact_id"]                            # visible in the Technical view and to SYN-01: the knowledge was re-bound
            binding["artifact_id"] = aid                                                # to the artifact that now holds this path
        return raw
    return read_scalar


def derive_from_head(store, tenant_id, workspace_id, *, actor_id, idempotency_key) -> dict:
    """Make the business knowledge follow the CURRENT head (contract section 13): a PENDING_HUMAN snapshot that carries the accepted TARGET rules, confirmed relations and
    answered gaps over the new source revision, with the OBSERVED rules re-read from the new sources (extraction candidates of this revision when they exist, otherwise the
    structured scalars the rules are bound to). Deterministic, no model; the person accepts it like any proposal (KNO-05). Refuses `no_snapshot` when there is neither an
    accepted snapshot nor extraction output for this revision. Idempotent on the resulting content."""
    from core.change_intelligence.v2 import business_rebase as br
    head = store.head(tenant_id, workspace_id)
    view = business_view(store, tenant_id, workspace_id)
    accepted = view["accepted"]
    pending = view["pending"] if _working(view) is view["pending"] else None
    base = accepted or pending
    candidates, carried = current_candidates(store, tenant_id, workspace_id)
    if not any(not c.get("carried_from_revision") for c in candidates) and base is not None:
        candidates, carried = [], []                                           # nothing extracted for this revision: the structured read-back follows the files (SYN-02)
    report_fp = candidates_fingerprint(candidates)
    if base is None and not candidates:
        raise BusinessKnowledgeRefused("no_snapshot", "there is no business snapshot to analyse; import and extract first")
    previous_observed = [r for r in (base["rules"] if base else []) if r["authority"] == "OBSERVED"]
    report: dict = {"origin": "rebase" if base else "backfill", "from_snapshot_id": base["snapshot"]["snapshot_id"] if base else None, "source_revision_id": head.source_revision_id,
                    "candidates_fingerprint": report_fp}
    sources_report = None
    if candidates:
        # rules, supporting facts and incomplete statements; code conditions and links to configuration values of THIS revision (business_sources)
        from core.change_intelligence.v2 import business_sources as bs
        rev = store.source_revision_view(tenant_id, workspace_id, head.source_revision_id)
        paths = {a["artifact_id"]: a.get("logical_path") or a["artifact_id"] for a in rev["artifacts"]}
        observations = store.source_observations(tenant_id, workspace_id, head.source_revision_id)
        structured = bs.structured_values(observations, paths)
        observed, sources_report = bs.observed_rules(candidates, structured, paths, bs.code_facts(observations, paths))
        sources_report["carried_files"] = carried                                  # files whose candidates come from an earlier revision with identical bytes
        report.update(observed_from="extraction_candidates", candidates=len(candidates), skipped=sources_report["skipped"], sources=sources_report, carried_files=carried)
    else:
        observed, reread = br.reobserve_rules(previous_observed, _scalar_reader(store, tenant_id, workspace_id, head.source_revision_id), new_revision_id=head.source_revision_id)
        report.update(observed_from="structured_read_back", reread=reread)
    merged = br.rebase(base["rules"] if base else [], observed, previous_observed=previous_observed)
    rules = merged["rules"]
    if candidates:                                                              # a file extracted again in this revision: its earlier observed rules that the new extraction did not
        re_read = {c.get("artifact_revision_id") for c in candidates if not c.get("carried_from_revision")}   # reproduce are withdrawn (kept for history, marked), never left live
        for r in rules:
            home = ((r.get("source_bindings") or [{}])[0]).get("artifact_id")       # the file the rule was extracted from (a linked configuration value is appended after it)
            if r["rule_id"] in merged["missing_observed"] and r.get("authority") == "OBSERVED" and home in re_read and r.get("status") != "SUPERSEDED":
                r["status"] = "SUPERSEDED"
                r.setdefault("unknown_fields", [])
        report["withdrawn_observed"] = sorted(r["rule_id"] for r in rules if r["rule_id"] in merged["missing_observed"] and r.get("status") == "SUPERSEDED")
    reread = report.get("reread") or []
    lost_roots = {e["rule_id"] for e in reread if e.get("mapping_lost")}
    changed_refs = {str(b["evidence_ref"]) for e in reread if e.get("changed") for r in rules if r["rule_id"] == e["rule_id"] for b in r.get("source_bindings") or []
                    if b.get("revision_id") == head.source_revision_id}                  # the locations that were actually re-read with a new value
    lost_targets = []
    for r in rules:
        if r.get("authority") == "TARGET" and br.root_of(r["rule_id"]) in lost_roots:
            r["implementation_status"] = "unverifiable"
            lost_targets.append(r)
            for st in merged["statuses"]:
                if st["target_rule_id"] == r["rule_id"]:
                    st.update(status="unverifiable", basis="the source location of the observed rule is no longer in the sources")
    report.update(carried_targets=merged["carried_targets"], statuses=merged["statuses"], missing_observed=merged["missing_observed"], changed_observed=merged["changed_observed"],
                  mapping_lost=sorted(lost_roots))
    rel_list, gap_list = _merge_state(rules, base["relations"] if base else [], base["gaps"] if base else [])
    rv = br.revalidate(rel_list, gap_list, changed_observed=merged["changed_observed"], missing_observed=set(merged["missing_observed"]) | lost_roots, changed_refs=changed_refs,
                       lost_mapping_targets=lost_targets)
    rel_list, gap_list = rv["relations"], rv["gaps"]
    if sources_report:
        gap_list = _link_gaps(gap_list, sources_report["unresolved_references"], {r["rule_id"]: r for r in rules})
    gap_list = sorted(gap_list, key=lambda g: g["gap_id"])
    report.update(relations_recheck=rv["relations_recheck"], gaps_reopened=rv["gaps_reopened"], uncertain_links=rv["uncertain_links"],
                  supersedes_pending=(view["pending"]["snapshot"]["snapshot_id"] if (accepted is not None and view["pending"] is not None and _working(view) is view["pending"]) else None))
    content_hash = canonical_hash({"rules": rules, "relations": rel_list, "gaps": gap_list})
    snapshot_id = "rebase-" + canonical_hash({"content": content_hash, "source": head.source_revision_id, "km": head.knowledge_revision_id, "epoch": head.decision_epoch})[:20]
    with read_snapshot(store) as cur:
        cur.execute("SELECT 1 FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (tenant_id, workspace_id, snapshot_id))
        exists = cur.fetchone() is not None
    if exists:
        return {"snapshot_id": snapshot_id, "replayed": True, "report": report}
    result = commit_snapshot(store, tenant_id, workspace_id, snapshot_id=snapshot_id, actor_id=actor_id, lifecycle_state="PENDING_HUMAN", rules=rules, relations=rel_list,
                             gaps=gap_list, decisions=[], expected_version=head.version, expected_business_revision=view["business_head"]["business_revision"],
                             source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                             idempotency_key=f"{idempotency_key}-derive", coverage={"origin": report["origin"], "from_snapshot_id": report["from_snapshot_id"], "observed_from": report["observed_from"],
                                                                                   "candidates_fingerprint": report_fp,
                                                                                   "carried_targets": merged["carried_targets"], "statuses": merged["statuses"], "missing_observed": merged["missing_observed"],
                                                                                   "changed_observed": merged["changed_observed"], "mapping_lost": report["mapping_lost"], "relations_recheck": report["relations_recheck"],
                                                                                   "gaps_reopened": report["gaps_reopened"], "uncertain_links": report["uncertain_links"], "supersedes_pending": report["supersedes_pending"],
                                                                                   "behavioral_verification": "not_tested", **_gap_counts(gap_list),
                                                                                   **({"sources": sources_report} if sources_report else {})})
    return {"snapshot_id": snapshot_id, "replayed": result.replayed, "version": result.version, "report": report}


def _fingerprint_of_lineage(store, tenant_id, workspace_id, base) -> Optional[str]:
    """The fingerprint recorded on the snapshot the base was analysed / accepted from (an analysis or an acceptance copies rules without re-reading them)."""
    seen = set()
    cov = base["snapshot"]["coverage"] or {}
    with read_snapshot(store) as cur:
        while True:
            parent = cov.get("base_snapshot_id") or cov.get("from_snapshot_id")
            if not parent or parent in seen:
                return None
            seen.add(parent)
            cur.execute("SELECT coverage FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (tenant_id, workspace_id, parent))
            row = cur.fetchone()
            if row is None:
                return None
            cov = _as_object(row[0]) or {}
            if cov.get("candidates_fingerprint"):
                return cov["candidates_fingerprint"]


def candidates_fingerprint(candidates) -> str:
    """Identity of the extraction candidates the observed rules were read from (sorted candidate ids); a new extraction of a file changes it."""
    return canonical_hash(sorted(c["candidate_id"] for c in candidates if isinstance(c, dict) and c.get("candidate_id")))


def _link_gaps(gaps: list, unresolved: list, rules_by_id: Mapping) -> list:
    """One `uncertain_link` question per comparison whose configuration value could not be found exactly once in this revision; an existing one is kept."""
    out = {g["gap_id"]: g for g in gaps}
    for u in unresolved:
        rule = rules_by_id.get(u["rule_id"])
        if rule is None or not u.get("reference"):
            continue
        gid = business_graph.gap_id_for("uncertain_link", [u["rule_id"]], u["reference"])
        if gid in out:
            continue
        where = ("no configuration value with that name" if u["reason"] == "not_found"
                 else "several values with that name: " + ", ".join(u["candidates"]) if u["reason"] == "several_values" else "a value with that name that is not a number")
        out[gid] = {"gap_id": gid, "kind": "uncertain_link", "status": "OPEN", "affected_rule_ids": [u["rule_id"]],
                    "question": f"The code of \"{rule['title']}\" compares {u.get('field') or 'a field'} with {u['reference']}, but this source version has {where}. "
                                "Which value does the rule use?", "evidence_refs": business_graph._evidence_refs(rule) or [u["reference"]],
                    "requires_permission": "knowledge.confirm", "priority": "normal"}
    return list(out.values())


def analyse_workspace(store, tenant_id, workspace_id, *, actor_id, idempotency_key) -> dict:
    """Derive relation proposals and gaps for the latest business snapshot and store them as a PENDING_HUMAN snapshot.

    Deterministic (no model call). Idempotent: the snapshot id is derived from the content, so a re-run returns the same snapshot and never repeats a
    question. When there is no snapshot yet, or the source/knowledge/decision head moved since the latest one, the knowledge is first made to follow the current head
    (`derive_from_head`: carries the decided rules, confirmed relations and answered gaps; re-reads the observed rules) and the analysis runs on that; stale observed
    rules never enter the graph silently. CONFIRMED relations and gaps that a person already answered are carried over unchanged."""
    head = store.head(tenant_id, workspace_id)
    view, base = _latest_view(store, tenant_id, workspace_id)
    derived = None
    moved = base is None or (base["snapshot"]["source_revision_id"], base["snapshot"]["km_revision_id"], base["snapshot"]["decision_epoch"]) != (head.source_revision_id, head.knowledge_revision_id, head.decision_epoch)
    if not moved:                                                             # the same sources, extracted again (2026-10-22): the newest candidates must be read, not the old proposal replayed
        cands, _ = current_candidates(store, tenant_id, workspace_id)
        fp = candidates_fingerprint(cands)
        known = (base["snapshot"]["coverage"] or {}).get("candidates_fingerprint") or _fingerprint_of_lineage(store, tenant_id, workspace_id, base)
        moved = bool(cands) and any(not c.get("carried_from_revision") for c in cands) and fp != known
    if moved:
        derived = derive_from_head(store, tenant_id, workspace_id, actor_id=actor_id, idempotency_key=idempotency_key)
        head = store.head(tenant_id, workspace_id)
        view, base = _latest_view(store, tenant_id, workspace_id)
    meta = base["snapshot"]
    rules = base["rules"]
    rel_list, gap_list = _merge_state(rules, base["relations"], base["gaps"])
    content_hash = canonical_hash({"rules": rules, "relations": rel_list, "gaps": gap_list})
    derived_id = derived["snapshot_id"] if derived else None
    unbound = _unbound_values(store, tenant_id, workspace_id, meta["source_revision_id"], rules)
    sources = _sources_summary(meta.get("coverage"))
    if content_hash == meta["content_hash"]:                 # nothing to add: the base already holds exactly this
        return {"snapshot_id": meta["snapshot_id"], "replayed": derived is None or derived["replayed"], "relations": len(rel_list), "gaps": len(gap_list), "derived": derived_id,
                "unbound_values": unbound, "sources": sources}
    snapshot_id = "analysis-" + content_hash[:20]
    with read_snapshot(store) as cur:
        cur.execute("SELECT 1 FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (tenant_id, workspace_id, snapshot_id))
        exists = cur.fetchone() is not None
    if exists:
        return {"snapshot_id": snapshot_id, "replayed": True, "relations": len(rel_list), "gaps": len(gap_list), "derived": derived_id, "unbound_values": unbound,
                "sources": sources}
    decisions = []                       # analysis records no decision: it only proposes
    result = commit_snapshot(store, tenant_id, workspace_id, snapshot_id=snapshot_id, actor_id=actor_id, lifecycle_state="PENDING_HUMAN", rules=rules,
                             relations=rel_list, gaps=gap_list, decisions=decisions, expected_version=head.version, expected_business_revision=0,
                             source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                             idempotency_key=idempotency_key, coverage={"origin": "analysis", "base_snapshot_id": meta["snapshot_id"], "unbound_values": unbound,
                                                                        **({"sources": sources} if sources else {})})
    return {"snapshot_id": snapshot_id, "replayed": False, "version": result.version, "relations": len(rel_list), "gaps": len(gap_list), "derived": derived_id,
            "unbound_values": unbound, "sources": sources}


def _sources_summary(coverage) -> dict | None:
    """What reading the sources did with the extraction candidates (business_sources; contract section 18), short enough for the reading panel:
    how many became rules, supporting facts or incomplete statements, the incomplete ones by title and file, the links made and the references that could not be
    linked (each also an `uncertain_link` question). None when the rules were not read from extraction candidates."""
    src = (coverage or {}).get("sources")
    if not isinstance(src, dict):
        return None
    return {"supporting_facts": src.get("supporting_facts_count", 0), "incomplete": src.get("incomplete_count", 0), "source_links": src.get("source_links_count", 0),
            "code_branches": src.get("code_branches", 0), "uncovered_branches": src.get("uncovered_branches_count", 0),
            "uncovered_branch_items": [{"file": b.get("file"), "line": b.get("line")} for b in src.get("uncovered_branches") or []][:50],
            "incomplete_items": [{"title": i.get("title"), "file": i.get("file"), "missing": i.get("missing")} for i in src.get("incomplete") or []][:50],
            "unresolved_references": [{"reference": u.get("reference"), "reason": u.get("reason")} for u in src.get("unresolved_references") or []][:50],
            "carried_files": len(src.get("carried_files") or [])}


def _unbound_values(store, tenant_id, workspace_id, source_revision_id, rules) -> dict:
    """Numbers in the structured sources of the snapshot's revision that no rule uses (`business_graph.unbound_values`); a disclosure for a person, never a gap or a rule."""
    if not source_revision_id:
        return {"count": 0, "values": [], "truncated": False}
    view = store.source_revision_view(tenant_id, workspace_id, source_revision_id)
    paths = {a["artifact_id"]: a.get("logical_path") or a["artifact_id"] for a in view["artifacts"]}
    return business_graph.unbound_values(rules, store.source_observations(tenant_id, workspace_id, source_revision_id), paths)


def gap_queue(store, tenant_id, workspace_id, *, can_read_evidence: bool = False) -> dict:
    """Open questions of the latest snapshot, critical first. Read-only, scoped to the workspace."""
    view, snap = _latest_view(store, tenant_id, workspace_id)
    if snap is None:
        return {"snapshot_id": None, "gaps": []}
    titles = {r["rule_id"]: r["title"] for r in snap["rules"]}
    by_id = {r["rule_id"]: r for r in snap["rules"]}
    from core.change_intelligence.v2 import business_index as _bi

    def compare(rid):
        """EUI-06 A/B card of one conflicting option: the rule's own fields in words (the same fields graph_view already gives this reader); unknown stays unknown."""
        r = by_id.get(rid)
        if r is None:
            return None
        unknown = set(r.get("unknown_fields") or [])
        def word(f, v):
            return "unknown" if f in unknown or v in (None, "", [], {}) else v
        return {"actor": word("actor_ref", str(r.get("actor_ref") or "").replace("role-", "").replace("-", " ")),
                "condition": word("condition", _bi.condition_text(r.get("condition")) if r.get("condition") else None),
                "outcome": word("outcome", str((r.get("outcome") or {}).get("kind") or "").replace("_", " ")),
                "exceptions": [e["text"] for e in r.get("exceptions") or []], "scope": word("scope", _bi._scope_text(r.get("scope") or {})),
                "status": r.get("status"), "evidence_count": len(r.get("source_bindings") or [])}

    def shaped(g):
        out = dict(g, affected=[{"rule_id": r, "title": titles.get(r)} for r in g["affected_rule_ids"]])
        if g.get("kind") == "conflicting_facts" and g.get("options"):
            out["options"] = [dict(o, compare=compare(o.get("rule_id"))) for o in g["options"]]
        return out
    from infra.adapters import v2_business_proposals
    proposals = v2_business_proposals.proposals_for(store, tenant_id, workspace_id, snap, can_read_evidence=can_read_evidence)   # KNO-06: suggested by a model, labelled, open until a person acts
    open_gaps = sorted((shaped(g) for g in snap["gaps"] if g["status"] in ("OPEN", "REOPENED")), key=lambda g: (g["priority"] != "critical", g["gap_id"]))
    open_gaps += [shaped(g) for g in proposals["gaps"]]
    accepted_unknown = sorted((shaped(g) for g in snap["gaps"] if g["status"] == "ACCEPTED_UNKNOWN"), key=lambda g: g["gap_id"])
    summary = {k: proposals[k] for k in ("status", "coverage", "reason", "job_id", "statement")}
    summary.update(gaps=len(proposals["gaps"]), relations=len(proposals["relations"]))
    return {"snapshot_id": snap["snapshot"]["snapshot_id"], "lifecycle_state": snap["snapshot"]["lifecycle_state"], "gaps": open_gaps,
            "accepted_unknown": accepted_unknown, "critical_open": sum(1 for g in open_gaps if g["priority"] == "critical"),
            "critical_accepted_unknown": sum(1 for g in accepted_unknown if g["priority"] == "critical"), "proposals": summary}


def graph_view(store, tenant_id, workspace_id, *, focus=None) -> dict:
    """Nodes (rules, concepts, roles), the stored relations with their state, and, for `focus`, a bounded traversal with disclosed omissions."""
    view, snap = _latest_view(store, tenant_id, workspace_id)
    if snap is None:
        return {"snapshot_id": None, "nodes": [], "edges": []}
    from core.change_intelligence.v2 import business_index as _bi
    nodes = {r["rule_id"]: {"id": r["rule_id"], "type": "rule", "title": r["title"], "authority": r["authority"], "status": r["status"],
                            "implementation_status": r["implementation_status"], "actor_ref": r["actor_ref"], "condition_text": _bi.condition_text(r["condition"]),
                            "outcome_kind": r["outcome"].get("kind"), "exceptions": [e["text"] for e in r["exceptions"]], "scope": r["scope"],
                            "unknown_fields": r.get("unknown_fields", []), "decision_ids": [p["decision_id"] for p in r["provenance"] if p.get("kind") == "human_decision"],
                            "evidence_refs": [b["evidence_ref"] for b in r["source_bindings"]]} for r in snap["rules"]}
    for e in snap["relations"]:
        for end in (e["from"], e["to"]):
            nodes.setdefault(end, {"id": end, "type": "role" if end.startswith("role-") else "concept"})
    from infra.adapters import v2_business_proposals
    proposed = v2_business_proposals.proposals_for(store, tenant_id, workspace_id, snap)["relations"]      # KNO-06: PROPOSED edges suggested by a model, never confirmed here
    edges = list(snap["relations"]) + proposed
    out = {"snapshot_id": snap["snapshot"]["snapshot_id"], "nodes": [nodes[k] for k in sorted(nodes)], "edges": edges}
    if focus:
        out["traversal"] = business_graph.traverse(edges, focus)
    return out


# ---- answering a gap (KNO-04) ---------------------------------------------------------------------------------------------------

def _fresh_base(store, tenant_id, workspace_id):
    head = store.head(tenant_id, workspace_id)
    _, base = _latest_view(store, tenant_id, workspace_id)
    if base is None:
        raise BusinessKnowledgeRefused("no_snapshot", "there is no business snapshot to answer against")
    meta = base["snapshot"]
    if (meta["source_revision_id"], meta["km_revision_id"], meta["decision_epoch"]) != (head.source_revision_id, head.knowledge_revision_id, head.decision_epoch):
        raise BusinessKnowledgeRefused("stale_base", "the source, knowledge or decision head moved after this snapshot; extract and analyse again first")
    from infra.adapters import v2_business_proposals
    suggested = v2_business_proposals.proposals_for(store, tenant_id, workspace_id, base)["gaps"]
    if suggested:                                                     # an open suggested gap is answerable like any other; it enters the stored gaps only through the answer
        base = dict(base, gaps=list(base["gaps"]) + [{k: v for k, v in g.items() if k != "origin"} for g in suggested], suggested_gap_ids=[g["gap_id"] for g in suggested])
    return head, base


def _prior_answers(store, tenant_id, workspace_id, gap_id) -> list:
    with read_snapshot(store) as cur:
        cur.execute("""SELECT body FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND kind='answer_gap' AND body->>'gap_id'=%s
            ORDER BY created_at, decision_id""", (tenant_id, workspace_id, gap_id))
        return [(_as_object(r[0]) or {}).get("answer") for r in cur.fetchall()]


def preview_answer(store, tenant_id, workspace_id, *, gap_id, answer) -> dict:
    """What confirming this answer would change. Writes nothing."""
    head, base = _fresh_base(store, tenant_id, workspace_id)
    plan = business_answers.plan_answer(base["rules"], base["gaps"], gap_id, answer, decision_id=business_answers.PREVIEW_DECISION_ID,
                                        prior_answers=_prior_answers(store, tenant_id, workspace_id, gap_id))
    return {"gap_id": gap_id, "base_snapshot_id": base["snapshot"]["snapshot_id"], "preview_hash": business_answers.preview_hash(base["snapshot"]["content_hash"], plan),
            "noop": plan["noop"], "normalized_answer": plan["normalized"], "summary": plan["summary"], "resulting_rule": plan.get("target_rule"),
            "gap_updates": plan.get("gap_updates", {}), "new_gaps": plan.get("new_gaps", [])}


def confirm_answer(store, tenant_id, workspace_id, *, actor_id, gap_id, answer, quote, preview_hash) -> dict:
    """Record the person's decision and the resulting snapshot (PENDING_HUMAN) in one transaction. The preview hash must match what is current now."""
    if not isinstance(quote, str) or not quote.strip():
        raise bk.ContractError("invalid_field", "the answer in the person's own words (quote) is required")
    head, base = _fresh_base(store, tenant_id, workspace_id)
    prior = _prior_answers(store, tenant_id, workspace_id, gap_id)
    shown = business_answers.plan_answer(base["rules"], base["gaps"], gap_id, answer, decision_id=business_answers.PREVIEW_DECISION_ID, prior_answers=prior)
    if business_answers.preview_hash(base["snapshot"]["content_hash"], shown) != preview_hash:
        raise BusinessKnowledgeRefused("preview_stale", "the knowledge changed since this preview was shown; look at the preview again")
    if shown["noop"]:
        return {"noop": True, "snapshot_id": base["snapshot"]["snapshot_id"], "replayed": True}
    decision_id = business_answers.decision_id_for(gap_id, shown["normalized"], quote)
    plan = business_answers.plan_answer(base["rules"], base["gaps"], gap_id, answer, decision_id=decision_id, prior_answers=prior)
    rules = {r["rule_id"]: r for r in base["rules"]}
    if plan.get("target_rule"):
        rules[plan["target_rule"]["rule_id"]] = plan["target_rule"]
    rule_list = [rules[k] for k in sorted(rules)]
    gaps_after = business_answers.apply_plan(base["gaps"], plan)
    rel_list, gap_list = _merge_state(rule_list, base["relations"], gaps_after)
    snapshot_id = "answer-" + canonical_hash({"rules": rule_list, "relations": rel_list, "gaps": gap_list, "decision": decision_id})[:20]
    with read_snapshot(store) as cur:
        cur.execute("SELECT 1 FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s", (tenant_id, workspace_id, snapshot_id))
        if cur.fetchone() is not None:
            return {"noop": False, "snapshot_id": snapshot_id, "decision_id": decision_id, "replayed": True}
    decision = {"decision_id": decision_id, "kind": "answer_gap", "quote": quote.strip(),
                "body": {"gap_id": gap_id, "answer": plan["normalized"], "target_rule_id": (plan.get("target_rule") or {}).get("rule_id"),
                         "preview_hash": preview_hash, "summary": plan["summary"],
                         "gap_origin": "model_proposal" if gap_id in base.get("suggested_gap_ids", ()) else "analysis"}}
    result = commit_snapshot(store, tenant_id, workspace_id, snapshot_id=snapshot_id, actor_id=actor_id, lifecycle_state="PENDING_HUMAN", rules=rule_list,
                             relations=rel_list, gaps=gap_list, decisions=[decision], expected_version=head.version, expected_business_revision=0,
                             source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                             idempotency_key="answer-" + snapshot_id, recheck_authority=True,
                             coverage={"origin": "answer", "base_snapshot_id": base["snapshot"]["snapshot_id"]})
    return {"noop": False, "snapshot_id": snapshot_id, "decision_id": decision_id, "replayed": result.replayed, "version": result.version}


# ---- accept, history, diff, revoke (KNO-05) ------------------------------------------------------------------------------------------

def _gap_counts(gaps) -> dict:
    open_ = [g for g in gaps if g["status"] in ("OPEN", "REOPENED")]
    unknown = [g for g in gaps if g["status"] == "ACCEPTED_UNKNOWN"]
    return {"open": len(open_), "critical_open": sum(1 for g in open_ if g["priority"] == "critical"), "accepted_unknown": len(unknown),
            "critical_accepted_unknown": sum(1 for g in unknown if g["priority"] == "critical")}


def accept_snapshot(store, tenant_id, workspace_id, *, actor_id, snapshot_id, acknowledged_open_gaps=False) -> dict:
    """Make the latest PENDING_HUMAN snapshot the accepted business knowledge (head + 1), atomically with its audit and outbox marker.

    Not a source apply and not a result acceptance. Remaining questions are disclosed in the snapshot coverage (partial) and a critical open one needs an
    explicit acknowledgement. Repeating the acceptance of what is already accepted returns it."""
    head = store.head(tenant_id, workspace_id)
    view = business_view(store, tenant_id, workspace_id)
    accepted = view["accepted"]
    pending = view["pending"] if _working(view) is view["pending"] else None          # a proposal older than the accepted knowledge is history, not a candidate
    if pending is None or pending["snapshot"]["snapshot_id"] != snapshot_id:
        if accepted is not None and snapshot_id == accepted["snapshot"]["coverage"].get("from_snapshot_id"):
            return {"snapshot_id": accepted["snapshot"]["snapshot_id"], "business_revision": accepted["snapshot"]["business_revision"], "replayed": True}
        raise BusinessKnowledgeRefused("not_latest_pending", "only the latest proposal can be accepted; look at the current proposal again")
    meta = pending["snapshot"]
    if (meta["source_revision_id"], meta["km_revision_id"], meta["decision_epoch"]) != (head.source_revision_id, head.knowledge_revision_id, head.decision_epoch):
        raise BusinessKnowledgeRefused("stale_base", "the source, knowledge or decision head moved after this proposal; extract and analyse again first")
    if accepted is not None and accepted["snapshot"]["content_hash"] == meta["content_hash"]:
        return {"snapshot_id": accepted["snapshot"]["snapshot_id"], "business_revision": accepted["snapshot"]["business_revision"], "replayed": True}
    counts = _gap_counts(pending["gaps"])
    if counts["critical_open"] and not acknowledged_open_gaps:
        raise BusinessKnowledgeRefused("open_critical_gaps", f"{counts['critical_open']} important question(s) are still open; acknowledge them to accept a partial knowledge version")
    revision = view["business_head"]["business_revision"]
    new_id = "accepted-" + canonical_hash({"from": meta["snapshot_id"], "revision": revision + 1})[:20]
    coverage = {"origin": "accept", "from_snapshot_id": meta["snapshot_id"], "partial": bool(counts["open"] or counts["accepted_unknown"]), **counts,
                "acknowledged_open_gaps": bool(acknowledged_open_gaps)}
    result = commit_snapshot(store, tenant_id, workspace_id, snapshot_id=new_id, actor_id=actor_id, lifecycle_state="ACCEPTED", rules=pending["rules"],
                             relations=pending["relations"], gaps=pending["gaps"], decisions=[], expected_version=head.version, expected_business_revision=revision,
                             source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                             idempotency_key=f"accept-{meta['snapshot_id']}-{revision}", coverage=coverage)
    return {"snapshot_id": new_id, "business_revision": revision + 1, "partial": coverage["partial"], "coverage": coverage, "replayed": result.replayed}


def revoke_decision(store, tenant_id, workspace_id, *, actor_id, decision_id, reason) -> dict:
    """Withdraw one recorded business decision. A new ACCEPTED snapshot marks every rule it backed SUPERSEDED, rejects the relations it backed and reopens the
    question it answered, so nothing derived from it keeps standing. The earlier snapshots and the decision itself stay in the history."""
    if not isinstance(reason, str) or not reason.strip():
        raise bk.ContractError("invalid_field", "a reason is required to revoke a decision")
    head = store.head(tenant_id, workspace_id)
    view = business_view(store, tenant_id, workspace_id)
    base = view["accepted"]
    if base is None:
        raise BusinessKnowledgeRefused("no_snapshot", "there is no accepted business knowledge")
    meta = base["snapshot"]
    if (meta["source_revision_id"], meta["km_revision_id"], meta["decision_epoch"]) != (head.source_revision_id, head.knowledge_revision_id, head.decision_epoch):
        raise BusinessKnowledgeRefused("stale_base", "the source, knowledge or decision head moved after the accepted knowledge; extract and analyse again first")
    with read_snapshot(store) as cur:
        cur.execute("SELECT body FROM v2_business_decisions WHERE tenant_id=%s AND workspace_id=%s AND decision_id=%s AND kind='answer_gap'",
                    (tenant_id, workspace_id, decision_id))
        row = cur.fetchone()
    mark = {"kind": "human_decision", "decision_id": decision_id}
    backed = [r for r in base["rules"] if mark in r["provenance"] and r["status"] != "SUPERSEDED"]
    if row is None or not backed:
        raise BusinessKnowledgeRefused("unknown_reference", f"decision {decision_id!r} backs nothing in the accepted knowledge (unknown or already revoked)")
    backed_ids = {r["rule_id"] for r in backed}
    gap_id = (_as_object(row[0]) or {}).get("gap_id")
    rules = [dict(r, status="SUPERSEDED") if r["rule_id"] in backed_ids else r for r in base["rules"]]
    relations = [dict(e, status="REJECTED") if decision_id in e["provenance_refs"] else e for e in base["relations"]]
    gaps = [dict(g, status="REOPENED") if g["gap_id"] == gap_id and g["status"] in ("RESOLVED", "ACCEPTED_UNKNOWN") else g for g in base["gaps"]]
    rel_list, gap_list = _merge_state(rules, relations, gaps)
    revoke_id = "decision-revoke-" + canonical_hash({"revokes": decision_id})[:12]
    revision = view["business_head"]["business_revision"]
    snapshot_id = "revoked-" + canonical_hash({"base": meta["snapshot_id"], "revokes": decision_id})[:20]
    decision = {"decision_id": revoke_id, "kind": "revoke", "quote": reason.strip(), "body": {"revokes": decision_id, "superseded_rules": sorted(backed_ids)}}
    result = commit_snapshot(store, tenant_id, workspace_id, snapshot_id=snapshot_id, actor_id=actor_id, lifecycle_state="ACCEPTED", rules=rules,
                             relations=rel_list, gaps=gap_list, decisions=[decision], expected_version=head.version, expected_business_revision=revision,
                             source_revision_id=head.source_revision_id, km_revision_id=head.knowledge_revision_id, decision_epoch=head.decision_epoch,
                             idempotency_key=f"revoke-{decision_id}-{revision}", coverage={"origin": "revoke", "revokes": decision_id, **_gap_counts(gap_list)})
    return {"snapshot_id": snapshot_id, "business_revision": revision + 1, "superseded_rules": sorted(backed_ids), "reopened_gap": gap_id, "version": result.version}


_DIFF_FIELDS = ("title", "condition", "actor_ref", "outcome", "exceptions", "scope", "status", "authority", "implementation_status")


def _accepted_by_revision(cur, tenant_id, workspace_id, revision):
    cur.execute("SELECT snapshot_id FROM v2_business_snapshots WHERE tenant_id=%s AND workspace_id=%s AND business_revision=%s", (tenant_id, workspace_id, revision))
    row = cur.fetchone()
    return _snapshot_view(cur, tenant_id, workspace_id, row[0]) if row else None


def _decision_ids(snapshot) -> set:
    return {p["decision_id"] for r in snapshot["rules"] for p in r["provenance"] if p.get("kind") == "human_decision"}


def business_diff(store, tenant_id, workspace_id, from_revision: int, to_revision: int) -> dict:
    """What changed in the business meaning between two accepted revisions (rules added, removed, changed by field; questions; decisions)."""
    with read_snapshot(store) as cur:
        a, b = (_accepted_by_revision(cur, tenant_id, workspace_id, n) for n in (from_revision, to_revision))
    if a is None or b is None:
        raise BusinessKnowledgeRefused("unknown_reference", "both revisions must be accepted business knowledge versions of this workspace")
    ra, rb = ({r["rule_id"]: r for r in s["rules"]} for s in (a, b))
    changed = []
    for rid in sorted(set(ra) & set(rb)):
        fields = [f for f in _DIFF_FIELDS if ra[rid].get(f) != rb[rid].get(f)]
        if fields:
            changed.append({"rule_id": rid, "title": rb[rid]["title"], "fields": fields, "before": {f: ra[rid].get(f) for f in fields}, "after": {f: rb[rid].get(f) for f in fields}})
    ga, gb = ({g["gap_id"]: g["status"] for g in s["gaps"]} for s in (a, b))
    questions = [{"gap_id": g, "before": ga.get(g), "after": gb.get(g)} for g in sorted(set(ga) | set(gb)) if ga.get(g) != gb.get(g)]
    return {"from_revision": from_revision, "to_revision": to_revision,
            "rules_added": [{"rule_id": r, "title": rb[r]["title"], "authority": rb[r]["authority"]} for r in sorted(set(rb) - set(ra))],
            "rules_removed": sorted(set(ra) - set(rb)), "rules_changed": changed, "questions_changed": questions,
            "decisions_added": sorted(_decision_ids(b) - _decision_ids(a))}


def knowledge_status(store, tenant_id, workspace_id) -> dict:
    """What a person needs to decide about acceptance: head, counts, remaining questions, history, and the derived-state markers (metadata only)."""
    view = business_view(store, tenant_id, workspace_id)

    def brief(s):
        if s is None:
            return None
        m = s["snapshot"]
        return {"snapshot_id": m["snapshot_id"], "lifecycle_state": m["lifecycle_state"], "business_revision": m["business_revision"], "created_at": m["created_at"],
                "committed_by": m["committed_by"], "rules": len(s["rules"]), "target_rules": sum(1 for r in s["rules"] if r["authority"] == "TARGET"),
                "decisions": len(s["decisions"]), "questions": _gap_counts(s["gaps"]), "coverage": m["coverage"],
                "analysed": "unbound_values" in (m["coverage"] or {}),                           # F-ACC-1: an analysis snapshot keeps unbound_values in its coverage
                "source_revision_id": m.get("source_revision_id")}
    with read_snapshot(store) as cur:
        cur.execute("""SELECT snapshot_id, business_revision, committed_by, created_at, coverage FROM v2_business_snapshots
            WHERE tenant_id=%s AND workspace_id=%s AND lifecycle_state='ACCEPTED' ORDER BY business_revision DESC LIMIT 50""", (tenant_id, workspace_id))
        history = [{"snapshot_id": r[0], "business_revision": r[1], "committed_by": r[2], "created_at": r[3].isoformat(), "coverage": _as_object(r[4])} for r in cur.fetchall()]
        cur.execute("""SELECT count(*) FROM v2_outbox WHERE tenant_id=%s AND workspace_id=%s AND kind=%s AND status <> 'DONE' AND payload->>'lifecycle_state'='ACCEPTED'""",
                    (tenant_id, workspace_id, EVENT))
        index_pending = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind LIKE 'document%%'", (tenant_id, workspace_id))
        document_jobs = cur.fetchone()[0]
        documents = {"freshness": "not_requested", "jobs": document_jobs}
        if document_jobs:
            head_id = view["business_head"]["accepted_snapshot_id"]
            cur.execute("""SELECT snapshot_id, count(*) FROM v2_business_documents WHERE tenant_id=%s AND workspace_id=%s GROUP BY snapshot_id""", (tenant_id, workspace_id))
            rows = cur.fetchall()
            available = sum(n for sid, n in rows if sid == head_id)
            stale = sum(n for sid, n in rows if sid != head_id)
            cur.execute("""SELECT execution_status, count(*) FROM v2_jobs WHERE tenant_id=%s AND workspace_id=%s AND kind='document.generate' GROUP BY execution_status""",
                        (tenant_id, workspace_id))
            by_status = dict(cur.fetchall())
            generating = sum(n for st, n in by_status.items() if st not in ("COMPLETED", "FAILED", "CANCELLED"))
            failed = by_status.get("FAILED", 0) + by_status.get("CANCELLED", 0)
            summary = "available" if available else ("generating" if generating else ("stale" if stale else ("failed" if failed else "not_requested")))
            documents = {"freshness": summary, "jobs": document_jobs, "counts": {"available_for_head": available, "stale": stale, "generating": generating, "failed": failed},
                         "statement": "Documents are generated only on request and are never regenerated by a knowledge change; a document of an older version is served as stale."}
    pending = view["pending"] if _working(view) is view["pending"] else None
    return {"business_head": view["business_head"], "accepted": brief(view["accepted"]), "pending": brief(pending), "history": history,
            "source_revision_id": view["workspace"]["source_revision_id"],                       # F-ACC-1: the UI compares it with the pending snapshot's revision
            "observed_stale": view["observed_stale"], "index": {"pending_events": index_pending}, "documents": documents}


# ---- current rights of an account (used by jobs that run later than the request) ---------------------------------------------------------------

def current_roles(store, tenant_id, workspace_id, account_id) -> tuple:
    """The roles an account holds in this workspace RIGHT NOW (empty when the account is unknown, disabled, expired or no longer a member)."""
    if not account_id:
        return ()
    with read_snapshot(store) as cur:
        cur.execute("""SELECT a.disabled_at IS NOT NULL OR (a.credential_expires_at IS NOT NULL AND a.credential_expires_at <= now()), m.roles
            FROM v2_accounts a JOIN v2_memberships m ON m.account_id=a.account_id AND m.tenant_id=%s AND m.workspace_id=%s WHERE a.account_id=%s""",
                    (tenant_id, workspace_id, account_id))
        row = cur.fetchone()
    return () if row is None or row[0] else tuple(_as_object(row[1]) or ())


def current_permissions(store, tenant_id, workspace_id, account_id) -> frozenset:
    from infra.adapters.v2_policy import permissions_for
    return frozenset(permissions_for(current_roles(store, tenant_id, workspace_id, account_id)))
