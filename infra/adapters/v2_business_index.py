"""Business retrieval index and context pack (contract fkl/1). Reads only; the one write is the derived index of a stored snapshot.

  * The index of a snapshot is built in ONE transaction with its READY marker. A failed build leaves a FAILED marker (own transaction) and no items. Readers
    use index rows only when a READY marker of the CURRENT index version exists for that snapshot; otherwise the pack is computed directly from the stored
    snapshot and says so (`served_from`), so a half-built or old-version index is never presented as current.
  * A pack is pinned to one snapshot (and the source revision that snapshot saw): rules, decisions, observations and graph paths all come from that pin.
  * Access is decided before anything is put in the pack: without `evidence.read` no quote or source text is included, only the count of what was withheld.
  * Generated documents are not an input anywhere in this module.
"""
from __future__ import annotations

import functools
import os
import pathlib

from psycopg2.extras import Json

from core.change_intelligence.v2 import business_index as bi
from infra.adapters import semantic_retrieval as sr
from core.change_intelligence.v2.retrieval import rank as rank_observations
from infra.adapters.v2_business import BusinessKnowledgeRefused, _as_object, _snapshot_view, business_view, read_snapshot


@functools.lru_cache(maxsize=1)
def _lexicon() -> dict:
    """The generic business lexicon (`config/business_lexicon.json`, override with RIPPLE_BUSINESS_LEXICON_PATH); its sha256 is stamped into every pack."""
    path = os.environ.get("RIPPLE_BUSINESS_LEXICON_PATH") or str(pathlib.Path(__file__).resolve().parents[2] / "config" / "business_lexicon.json")
    return bi.lexicon_from_bytes(pathlib.Path(path).read_bytes())


_ADAPTER = None


def set_semantic_adapter(adapter) -> None:
    """Tests and deployments inject the adapter; None goes back to the environment default."""
    global _ADAPTER
    _ADAPTER = adapter


def semantic_adapter() -> sr.SemanticAdapter:
    """Default: disarmed (no call of any kind). RIPPLE_SEMANTIC_RETRIEVAL=replay with RIPPLE_SEMANTIC_REPLAY_PATH reads a SYNTHETIC replay fixture. A model-backed provider does not exist."""
    global _ADAPTER
    if _ADAPTER is None:
        from infra import provider_gate
        provider = sr.DisarmedProvider()
        if os.environ.get("RIPPLE_SEMANTIC_RETRIEVAL", "").lower() == "replay" and os.environ.get("RIPPLE_SEMANTIC_REPLAY_PATH"):
            import json
            provider = sr.ReplayProvider(json.loads(pathlib.Path(os.environ["RIPPLE_SEMANTIC_REPLAY_PATH"]).read_text(encoding="utf-8")), usage={"input_tokens": 0, "output_tokens": 0})
        elif os.environ.get("RIPPLE_SEMANTIC_RETRIEVAL", "").lower() == "vertex":
            # The external provider: selected explicitly, pinned to a priced model of the budget config, and still only callable under a job budget (adapter boundary).
            from infra.adapters.semantic_vertex import VertexSemanticProvider
            from infra.worker import job_quota
            from infra.worker.budget_config import model_for_role
            if os.environ.get("RIPPLE_BUDGET_CONFIG_PATH"):
                config = job_quota.load_worker_budget_config()
                if "retrieval.semantic" in (config.role_model_ids or {}):
                    provider = VertexSemanticProvider(model_for_role(config, "retrieval.semantic"))
            if not isinstance(provider, VertexSemanticProvider):
                provider = sr.DisarmedProvider()                  # no pinned, priced role: stay disarmed rather than guess a model
        _ADAPTER = sr.SemanticAdapter(provider, gate=provider_gate.is_disarmed)
    return _ADAPTER


def _covered_ids(entries) -> list:
    return [v for e in entries for v in [e["item_id"]] + [m["item_id"] for m in e.get("versions", [])]]


def _decisions_until(cur, tenant_id, workspace_id, snapshot_id) -> list:
    cur.execute("""SELECT d.decision_id, d.actor_id, d.kind, d.quote, d.body FROM v2_business_decisions d
        JOIN v2_business_snapshots ds ON ds.tenant_id=d.tenant_id AND ds.workspace_id=d.workspace_id AND ds.snapshot_id=d.snapshot_id
        JOIN v2_business_snapshots s ON s.tenant_id=d.tenant_id AND s.workspace_id=d.workspace_id AND s.snapshot_id=%s
        WHERE d.tenant_id=%s AND d.workspace_id=%s AND ds.created_at <= s.created_at ORDER BY d.created_at, d.decision_id""", (snapshot_id, tenant_id, workspace_id))
    return [{"decision_id": r[0], "actor_id": r[1], "kind": r[2], "quote": r[3], "body": _as_object(r[4])} for r in cur.fetchall()]


def _items_of(cur, tenant_id, workspace_id, snapshot_id) -> list:
    snap = _snapshot_view(cur, tenant_id, workspace_id, snapshot_id)
    if snap is None:
        raise BusinessKnowledgeRefused("unknown_reference", f"snapshot {snapshot_id!r} does not exist in this workspace")
    return bi.build_items(snap["rules"], snap["gaps"], _decisions_until(cur, tenant_id, workspace_id, snapshot_id))


def build_index(store, tenant_id, workspace_id, snapshot_id, *, version=bi.INDEX_VERSION) -> dict:
    """Idempotent: delete, insert and mark READY in one transaction; a failure marks FAILED separately and re-raises."""
    try:
        with read_snapshot(store) as cur:
            items = _items_of(cur, tenant_id, workspace_id, snapshot_id)
        with store._tx() as cur:
            cur.execute("DELETE FROM v2_business_index_items WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s AND index_version=%s",
                        (tenant_id, workspace_id, snapshot_id, version))
            for it in items:
                body = {k: v for k, v in it.items() if k != "term_vector"}
                cur.execute("""INSERT INTO v2_business_index_items (tenant_id, workspace_id, snapshot_id, index_version, item_id, kind, term_vector, body)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (tenant_id, workspace_id, snapshot_id, version, it["item_id"], it["kind"], Json(it["term_vector"]), Json(body)))
            cur.execute("""INSERT INTO v2_business_index_builds (tenant_id, workspace_id, snapshot_id, index_version, status, item_count)
                VALUES (%s,%s,%s,%s,'READY',%s) ON CONFLICT (tenant_id, workspace_id, snapshot_id, index_version)
                DO UPDATE SET status='READY', item_count=EXCLUDED.item_count, reason=NULL, built_at=now()""", (tenant_id, workspace_id, snapshot_id, version, len(items)))
        return {"snapshot_id": snapshot_id, "index_version": version, "status": "READY", "item_count": len(items)}
    except Exception as exc:
        try:
            with store._tx() as cur:
                cur.execute("""INSERT INTO v2_business_index_builds (tenant_id, workspace_id, snapshot_id, index_version, status, reason)
                    VALUES (%s,%s,%s,%s,'FAILED',%s) ON CONFLICT (tenant_id, workspace_id, snapshot_id, index_version)
                    DO UPDATE SET status='FAILED', reason=EXCLUDED.reason, built_at=now()""", (tenant_id, workspace_id, snapshot_id, version, type(exc).__name__))
        except Exception:
            pass                                  # the marker is best effort; the original failure is what the caller sees
        raise


def index_state(cur, tenant_id, workspace_id, snapshot_id, lifecycle_state) -> dict:
    cur.execute("SELECT index_version, status, item_count, reason FROM v2_business_index_builds WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s",
                (tenant_id, workspace_id, snapshot_id))
    rows = {r[0]: r for r in cur.fetchall()}
    cur_row = rows.get(bi.INDEX_VERSION)
    if cur_row is not None:
        return {"status": cur_row[1], "index_version": bi.INDEX_VERSION, "item_count": cur_row[2], "reason": cur_row[3]}
    if rows:
        return {"status": "STALE", "index_version": bi.INDEX_VERSION, "reason": f"built with {sorted(rows)} only"}
    return {"status": "BUILDING" if lifecycle_state == "ACCEPTED" else "NOT_INDEXED", "index_version": bi.INDEX_VERSION}


def _index_items(cur, tenant_id, workspace_id, snapshot_id) -> list:
    cur.execute("SELECT term_vector, body FROM v2_business_index_items WHERE tenant_id=%s AND workspace_id=%s AND snapshot_id=%s AND index_version=%s ORDER BY kind, item_id",
                (tenant_id, workspace_id, snapshot_id, bi.INDEX_VERSION))
    return [dict(_as_object(r[1]), term_vector=_as_object(r[0])) for r in cur.fetchall()]


def _observation_pack(cur, tenant_id, workspace_id, source_revision_id, query, top_k) -> dict:
    cur.execute("SELECT status FROM v2_index_builds WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s", (tenant_id, workspace_id, source_revision_id))
    row = cur.fetchone()
    if row is None or row[0] != "READY":
        return {"status": "NOT_READY" if row is None else row[0], "source_revision_id": source_revision_id, "results": []}
    cur.execute("""SELECT artifact_id, observation_id, locator, parser_version, raw_value, term_vector, prev_observation_id, next_observation_id
        FROM v2_retrieval_index WHERE tenant_id=%s AND workspace_id=%s AND source_revision_id=%s""", (tenant_id, workspace_id, source_revision_id))
    rows = [{"artifact_id": r[0], "observation_id": r[1], "locator": r[2], "parser_version": r[3], "raw_value": r[4], "term_vector": r[5],
             "prev_observation_id": r[6], "next_observation_id": r[7]} for r in cur.fetchall()]
    bundle = rank_observations(rows, query, top_k=top_k)
    return {"status": "READY", "source_revision_id": source_revision_id, **bundle}


def context_pack(store, tenant_id, workspace_id, query, *, view="target", can_read_evidence=False, top_k=bi.DEFAULT_TOP_K, max_chars=bi.DEFAULT_MAX_CHARS,
                 observation_top_k=10, semantic=False, expected_snapshot_id=None, semantic_budget=None, allow_external_semantic=False) -> dict:
    """The evidence and knowledge to answer `query`, pinned to ONE snapshot.

    view "target": the accepted business knowledge (decided rules and what the sources say, each labelled), its decisions and open questions.
    view "observed": only what the sources say (OBSERVED rules; no decisions), from the accepted snapshot or, if none, the latest proposal (labelled)."""
    if view not in bi.VIEWS:
        raise BusinessKnowledgeRefused("invalid_view", f"view must be one of {bi.VIEWS}")
    if not isinstance(query, str) or not query.strip():
        raise BusinessKnowledgeRefused("invalid_query", "query required")
    view_all = business_view(store, tenant_id, workspace_id)
    accepted, pending = view_all["accepted"], view_all["pending"]
    snap = accepted if view == "target" else (accepted or pending)
    head = view_all["workspace"]
    pack = {"view": view, "query": query, "pin": None, "index": {"status": "NONE"}, "served_from": None, "items": [], "counterevidence": [], "graph": [], "sufficiency": "unknown",
            "retrieval": {"method": bi.METHOD, "learned_embeddings": False, "embedding_version": None},
            "coverage": {}, "observations": {"status": "NOT_REQUESTED", "results": [], "withheld": 0}, "notes": []}
    with read_snapshot(store) as cur:
        if snap is not None:
            meta = snap["snapshot"]
            if expected_snapshot_id is not None and meta["snapshot_id"] != expected_snapshot_id:
                raise BusinessKnowledgeRefused("stale_snapshot", "the knowledge moved after the question was asked")
            state = index_state(cur, tenant_id, workspace_id, meta["snapshot_id"], meta["lifecycle_state"])
            if state["status"] == "READY":
                items, served = _index_items(cur, tenant_id, workspace_id, meta["snapshot_id"]), "index"
            else:
                items, served = _items_of(cur, tenant_id, workspace_id, meta["snapshot_id"]), "snapshot_direct"
                pack["notes"].append(f"The business index is {state['status']}; this answer was read directly from the stored snapshot.")
            if view == "observed":
                items = [i for i in items if i["kind"] == "rule" and i["authority"] == "OBSERVED"]
            ranked = bi.rank_items(items, query, top_k=top_k, max_chars=max_chars, lexicon=_lexicon(), relations=snap["relations"])
            pack["semantic"] = {"status": "off", "reason": "off_by_request"}
            if semantic:
                sem = semantic_adapter().run(query, items, snapshot_id=meta["snapshot_id"], view=view, index_version=bi.INDEX_VERSION,
                                             access_scope="evidence" if can_read_evidence else "business", lexical_candidates=_covered_ids(ranked["items"]),
                                             budget=semantic_budget, allow_external=allow_external_semantic)
                pack["semantic"] = {k: sem.get(k, 0) for k in ("status", "provider", "config_version", "synthetic", "layers", "calls", "tokens", "tokens_in", "tokens_out", "cache", "payload_truncated", "sufficiency")}
                if sem["status"] in ("applied", "partial"):
                    ranked = bi.rank_items(items, query, top_k=top_k, max_chars=max_chars, lexicon=_lexicon(), relations=snap["relations"], semantic=sem)
                    for entry in ranked["items"]:
                        label = next((sem["labels"][v] for v in _covered_ids([entry]) if v in sem["labels"]), None)
                        entry["relevance"] = label or "unknown"
                else:
                    pack["notes"].append(f"Semantic retrieval was not applied ({sem['layers']['expand'].get('reason') or sem['status']}); the lexical method above was used.")
                pack["sufficiency"] = sem["sufficiency"]
            selected = [v for i in ranked["items"] if i["kind"] == "rule" for v in [i["item_id"]] + [m["item_id"] for m in i.get("versions", [])]]
            pack.update(items=ranked["items"], coverage=ranked["coverage"], retrieval=ranked["retrieval"], index=state, served_from=served,
                        counterevidence=bi.counterevidence(selected, snap["rules"], snap["relations"], snap["gaps"]),
                        graph=bi.graph_paths(selected, snap["relations"]),
                        pin={"snapshot_id": meta["snapshot_id"], "snapshot_state": meta["lifecycle_state"], "business_revision": meta["business_revision"],
                             "source_revision_id": meta["source_revision_id"], "km_revision_id": meta["km_revision_id"], "decision_epoch": meta["decision_epoch"],
                             "index_version": bi.INDEX_VERSION, "view": view})
            pack["observed_stale"] = meta["source_revision_id"] != head["source_revision_id"]
            source_revision = meta["source_revision_id"]
            if meta["lifecycle_state"] != "ACCEPTED":
                pack["notes"].append("There is no accepted business knowledge; this is a proposal and has not been confirmed by a person.")
        else:
            source_revision = head["source_revision_id"]
            pack["notes"].append("There is no business knowledge yet" + (" (nothing accepted)" if view == "target" else "") + "; only the sources are searched.")
            pack["pin"] = {"snapshot_id": None, "business_revision": 0, "source_revision_id": source_revision, "km_revision_id": head["knowledge_revision_id"],
                           "decision_epoch": head["decision_epoch"], "index_version": bi.INDEX_VERSION, "view": view}
            pack["observed_stale"] = False
        if source_revision is not None:
            obs = _observation_pack(cur, tenant_id, workspace_id, source_revision, query, observation_top_k)
            if can_read_evidence:
                pack["observations"] = obs
            else:
                pack["observations"] = {"status": obs["status"], "source_revision_id": source_revision, "results": [], "withheld": len(obs.get("results", []))}
    return pack
