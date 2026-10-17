"""Scoped retrieval ranking, pure (no I/O).

ADR-005 defers the retrieval index's concrete technology.
The design choice here, recorded explicitly rather than presented as a re-derived
contract fact: implement LEXICAL (token-overlap ratio) and VECTOR (cosine
similarity of a deterministic term-frequency vector, `embedding_version=
"termfreq-v1"`) paths over the durable projection built from the real
observations, plus a bounded 1-HOP STRUCTURAL path (same-artifact
neighbors). A real embedding-provider path needs the shared budget/model
policy wiring built for priced provider calls; that integration is
out of scope here and can plug in as a distinct `embedding_version`
alongside this baseline without displacing it.

Results are always keyed by `observation_id` and never grouped or merged by
their raw value -- two chunks that happen to cite the same number for two
different rules are always two separate results (R1: "cùng số ở rule khác
không tự merge"). Structural expansion is exactly one hop (`prev`/`next`
`observation_id`, precomputed at build time), so there is no unbounded graph
walk and therefore no traversal-limit/cycle risk to guard against.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Mapping

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: object) -> list[str]:
    return _TOKEN_RE.findall(str(text if text is not None else "").lower())


def term_vector(text: object) -> dict[str, int]:
    return dict(Counter(tokenize(text)))


def _cosine(a: Mapping[str, int], b: Mapping[str, int]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _lexical(query_tokens: set[str], doc_terms: Mapping[str, int]) -> float:
    if not query_tokens or not doc_terms:
        return 0.0
    hits = sum(1 for t in query_tokens if t in doc_terms)
    return hits / len(query_tokens)


def _public_fields(row: Mapping[str, object]) -> dict:
    return {"observation_id": row["observation_id"], "artifact_id": row["artifact_id"],
            "locator": row["locator"], "parser_version": row["parser_version"],
            "raw_value": row["raw_value"], "blob_hash": row.get("blob_hash")}


def rank(index_rows: list[Mapping[str, object]], query_text: str, *, top_k: int = 10) -> dict:
    """Score every durable index row by the lexical and vector paths, apply
    `top_k`, then add a bounded structural expansion. Pure: every input is
    already-fetched projection data (`PostgresV2Store.retrieve`), no store
    access happens here.

    `coverage` describes the ACTUAL
    final bundle (ranked hits + structural additions), never the pre-
    expansion ranked slice alone. A structural neighbor that happens to
    ALSO be a real scored candidate (it matched the query on its own, but
    lost to `top_k`) is recognized as such -- it keeps its real
    lexical/vector/score and is REMOVED from `omitted_count`, since it is,
    in fact, present in the response. Only a candidate genuinely absent
    from the final bundle counts as omitted, so `omitted_count`/`truncated`
    can never disagree with what a reader actually received.
    """
    q_tokens = set(tokenize(query_text))
    q_vector = term_vector(query_text)

    by_id = {r["observation_id"]: r for r in index_rows}
    scored = []
    for row in index_rows:
        terms = row.get("term_vector") or {}
        lexical = _lexical(q_tokens, terms)
        vector = _cosine(q_vector, terms)
        if lexical <= 0 and vector <= 0:
            continue
        via = sorted({p for p, s in (("lexical", lexical), ("vector", vector)) if s > 0})
        scored.append({**_public_fields(row), "lexical_score": lexical, "vector_score": vector,
                       "score": max(lexical, vector), "via": via})

    scored.sort(key=lambda r: (-r["score"], r["observation_id"]))
    scored_by_id = {r["observation_id"]: r for r in scored}
    total_candidates = len(scored)
    top = scored[:top_k] if top_k is not None else scored

    seen_ids = {r["observation_id"] for r in top}
    structural = []
    for hit in top:
        row = by_id.get(hit["observation_id"])
        if row is None:
            continue
        for neighbor_id in (row.get("prev_observation_id"), row.get("next_observation_id")):
            if not neighbor_id or neighbor_id in seen_ids or neighbor_id not in by_id:
                continue
            seen_ids.add(neighbor_id)
            already_scored = scored_by_id.get(neighbor_id)
            if already_scored is not None:
                # A real ranked candidate that lost to top_k -- reuse its
                # actual score, just mark it as ALSO reached via structural.
                via = sorted(set(already_scored["via"]) | {"structural"})
                structural.append({**already_scored, "via": via})
            else:
                structural.append({**_public_fields(by_id[neighbor_id]), "lexical_score": 0.0,
                                   "vector_score": 0.0, "score": 0.0, "via": ["structural"]})

    final_ids = {r["observation_id"] for r in top} | {r["observation_id"] for r in structural}
    omitted_count = sum(1 for r in scored if r["observation_id"] not in final_ids)

    return {"results": top + structural,
            "coverage": {"total_candidates": total_candidates, "ranked_returned": len(top),
                        "structural_added": len(structural), "returned": len(top) + len(structural),
                        "omitted_count": omitted_count, "truncated": omitted_count > 0},
            "embedding_version": "termfreq-v1"}
