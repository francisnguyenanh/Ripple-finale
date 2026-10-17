# -*- coding: utf-8 -*-
"""Business retrieval index items, ranking and the context pack parts (pure, no I/O). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md.

Items are built only from a stored business snapshot and its recorded decisions, never from a generated document. The method (`METHOD`) is lexical and
term-frequency cosine over NORMALIZED tokens, with a bounded expansion from a generic business lexicon and graph neighbors that disagree with a hit.
It is not a learned embedding and the pack says so (ADR-FKL-RETRIEVAL). Nothing is invented to fill the requested number of items.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_graph
from core.change_intelligence.v2.retrieval import _cosine

#: v2: item vectors are built from normalized tokens (numbers, stop words, plural and verb endings).
INDEX_VERSION = "business-index-v2/tf-normalized"
METHOD = "lexicon-expansion+tf-cosine+version-grouping+graph"
DEFAULT_TOP_K = 40
DEFAULT_MAX_CHARS = 24000
GRAPH_NEIGHBOR_SEEDS = 3
GRAPH_NEIGHBOR_MAX = 5
SEMANTIC_WEIGHT = 0.3          # final = 0.7 x lexical + 0.3 x rank-layer score, for items the rank layer scored (fixed, not tuned)
SEMANTIC_TERM_WEIGHT = 0.5     # validated semantic terms weigh like lexicon expansions
VIEWS = ("target", "observed")
_WITHDRAWN = frozenset({"SUPERSEDED", "REJECTED"})
_OPS = {"eq": "is", "ne": "is not", "gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most", "in": "is one of", "not_in": "is none of"}
STOP_WORDS = frozenset("a an and are as at be been but by can could did do does for from had has have how i if in into is it its of on or our shall should so such than that the "
                       "their then there these they this to was we were what when where which will with would you your me my not no yes any all".split())
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(million|m)?(?![a-z0-9])", re.I)


def _numbers(text: str) -> str:
    def repl(m):
        digits = m.group(1).replace(",", "")
        value = float(digits) * (1_000_000 if m.group(2) else 1)
        return " " + (str(int(value)) if value == int(value) else digits) + " "
    return _NUMBER.sub(repl, text)


def stem(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) > len(suffix) + 3:
            return word[: -len(suffix)]
    if word.endswith("es") and len(word) > 4 and word[-3] in "sxz":
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def tokens(text) -> list:
    """Normalized tokens: numbers without separators ("1.2 million" -> 1200000), lower case, hyphen split, stop words dropped, light stemming."""
    text = _numbers(str(text if text is not None else "").lower().replace("-", " "))
    return [raw if raw.isdigit() else stem(raw) for raw in re.findall(r"[a-z0-9_]+", text) if raw not in STOP_WORDS]


def term_vector(text) -> dict:
    return dict(Counter(tokens(text)))


def lexicon_from_bytes(raw: bytes) -> dict:
    """Parse the lexicon file content (the caller reads the file: this module owns no storage)."""
    lex = json.loads(raw.decode("utf-8"))
    lex["sha256"] = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    return lex


def expand(query_tokens: Iterable[str], lexicon: Optional[Mapping]) -> dict:
    """token -> weight: the query's own tokens (1.0) and, bounded, the other members of their lexicon groups (`expansion_weight`)."""
    weights = {t: 1.0 for t in query_tokens}
    if not lexicon:
        return weights
    lim = lexicon["limits"]
    groups = [[t for word in group for t in tokens(word)[:1]] for group in lexicon["groups"]]
    added = 0
    for t in list(weights):
        per = 0
        for group in groups:
            if t not in group:
                continue
            for member in group:
                if member not in weights and per < lim["max_expansions_per_token"] and added < lim["max_expanded_terms"]:
                    weights[member] = lim["expansion_weight"]
                    per += 1
                    added += 1
    return weights


def condition_text(cond) -> str:
    if cond is None:
        return "condition unknown"
    if "field" in cond:
        unit = f" {cond['unit']}" if cond.get("unit") else ""
        return f"{cond['field'].replace('_', ' ').replace('.', ' ')} {_OPS[cond['op']]} {cond['value']}{unit}"
    if "all" in cond:
        return " and ".join(condition_text(c) for c in cond["all"])
    if "any" in cond:
        return "(" + " or ".join(condition_text(c) for c in cond["any"]) + ")"
    return "not (" + condition_text(cond["not"]) + ")"


def _scope_text(scope: Mapping) -> str:
    return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in sorted(scope.items()))


def rule_item(rule: Mapping) -> dict:
    actor = rule["actor_ref"].replace("role-", "").replace("-", " ") if rule["actor_ref"] else "actor unknown"
    exceptions = "; ".join(e["text"] for e in rule["exceptions"])
    text = " | ".join(p for p in (rule["title"], f"actor {actor}", condition_text(rule["condition"]), f"outcome {rule['outcome'].get('kind')} {rule['outcome'].get('predicate', '')}".strip(),
                                  f"exceptions {exceptions}" if exceptions else "", f"scope {_scope_text(rule['scope'])}") if p)
    return {"item_id": rule["rule_id"], "kind": "rule", "text": text, "authority": rule["authority"], "status": rule["status"],
            "implementation_status": rule["implementation_status"], "title": rule["title"], "actor_ref": rule["actor_ref"], "condition": rule["condition"],
            "exceptions": rule["exceptions"], "scope": rule["scope"], "outcome": rule["outcome"],
            "evidence_refs": business_graph._evidence_refs(rule), "decision_ids": business_graph._decision_refs(rule), "unknown_fields": rule.get("unknown_fields", [])}


def decision_item(decision: Mapping) -> dict:
    body = decision.get("body") or {}
    summary = " ".join(str(s) for s in body.get("summary", []))
    return {"item_id": decision["decision_id"], "kind": "decision", "text": f"{decision['quote']} {summary}".strip(), "decision_kind": decision["kind"],
            "quote": decision["quote"], "summary": body.get("summary", []), "target_rule_id": body.get("target_rule_id"), "gap_id": body.get("gap_id")}


def gap_item(gap: Mapping) -> dict:
    return {"item_id": gap["gap_id"], "kind": "gap", "text": gap["question"], "gap_kind": gap["kind"], "priority": gap["priority"], "status": gap["status"],
            "affected_rule_ids": gap["affected_rule_ids"], "evidence_refs": gap["evidence_refs"]}


def build_items(rules: Iterable[Mapping], gaps: Iterable[Mapping], decisions: Iterable[Mapping]) -> list:
    """Index items of one snapshot. Withdrawn rules and revoked decisions are not retrievable; open questions are, so an answer can say what is unknown."""
    decisions = list(decisions)
    revoked = {(d.get("body") or {}).get("revokes") for d in decisions if d["kind"] == "revoke"}
    items = [rule_item(r) for r in rules if r["status"] not in _WITHDRAWN]
    items += [decision_item(d) for d in decisions if d["kind"] == "answer_gap" and d["decision_id"] not in revoked]
    items += [gap_item(g) for g in gaps if g["status"] in ("OPEN", "REOPENED")]
    for it in items:
        it["term_vector"] = term_vector(it["text"])
    return sorted(items, key=lambda i: (i["kind"], i["item_id"]))


def _public(it: Mapping) -> dict:
    return {k: v for k, v in it.items() if k != "term_vector"}


#: Version grouping is on. tf-idf weighting, distinctive-term qualification and greedy coverage are OFF: measured on the development and validation sets they did not help
#: (see the ablation in tests/final_loop/test_qry02_retrieval.py); they remain switchable so the measurement can be repeated.
DEFAULT_FEATURES = ("group",)
DISTINCT_DF = 0.25          # a term in more than this share of the items is "common": it can rank an item but cannot qualify it (fixed before measuring)
COVERAGE_WEIGHT = 0.5       # weight of newly covered distinctive query terms in the greedy selection (fixed before measuring)


def _fingerprint(item: Mapping) -> str:
    """Two rule items that say the same thing (same actor, condition, outcome, exceptions, scope) are versions of one statement: an observed rule and the
    decided rule copied from it. A rule that differs in any of these (a conflicting threshold) has another fingerprint and stays separate."""
    if item["kind"] != "rule":
        return item["kind"] + ":" + item["item_id"]
    cond = business_graph_normalized(item["condition"])
    return "rule:" + hashlib.sha256(json.dumps([item["actor_ref"], cond, item["outcome"].get("kind"), [e["text"] for e in item["exceptions"]], item["scope"]],
                                              sort_keys=True, default=str).encode("utf-8")).hexdigest()


def business_graph_normalized(cond):
    from core.change_intelligence.v2.business_knowledge import normalize_condition
    return normalize_condition(cond) if cond is not None else None


def rank_items(items: Iterable[Mapping], query: str, *, top_k: int = DEFAULT_TOP_K, max_chars: int = DEFAULT_MAX_CHARS, lexicon: Optional[Mapping] = None,
               relations: Iterable[Mapping] = (), features: Iterable[str] = DEFAULT_FEATURES, semantic: Optional[Mapping] = None) -> dict:
    """tf-idf score over the query tokens plus the bounded lexicon expansion (weighted), 0.5 x coverage + 0.5 x cosine.

    * An item qualifies only if it shares at least one DISTINCTIVE term (document share below DISTINCT_DF) with the query: words found in most rules ("approve",
      "purchase") can order results but never make an unrelated rule relevant, and a query with no distinctive match returns nothing.
    * Items with the same fingerprint are versions of one statement and share one slot (the decided one represents it); every version stays listed.
    * Selection is greedy over the query's distinctive terms: the next slot goes to the item that adds the most uncovered distinctive weight, so a question
      that needs two different rules is not answered with three copies of one.
    * Rules that conflict with one of the best hits are added from the graph (via "graph") so contradicting evidence is not lost."""
    import math
    items = list(items)
    features = set(features)
    n = len(items) or 1
    df: Counter = Counter()
    for it in items:
        df.update(it["term_vector"].keys())
    idf = (lambda t: math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0) if "idf" in features else (lambda t: 1.0)
    own = tokens(query)
    weights = expand(own, lexicon)
    semantic = semantic or {}
    semantic_terms = set()
    for phrase in semantic.get("terms", []):                  # validated terms from the semantic adapter: extra query words, weighted like lexicon expansions
        for t in tokens(phrase):
            if t not in weights:
                weights[t] = SEMANTIC_TERM_WEIGHT
                semantic_terms.add(t)
    qw = {t: w * idf(t) for t, w in weights.items()}
    total = sum(qw.values()) or 1.0
    distinct = {t for t in qw if df.get(t, 0) / n < DISTINCT_DF}
    distinct_total = sum(qw[t] for t in distinct if weights[t] == 1.0) or 1.0
    scored = []
    for it in items:
        tv = it["term_vector"]
        if "qualify" in features and not (distinct & tv.keys()):
            continue
        lex = sum(w for t, w in qw.items() if t in tv) / total
        vec = _cosine(qw, {t: c * idf(t) for t, c in tv.items()})
        if lex <= 0 and vec <= 0:
            continue                                  # no shared term: not a candidate, and never filler for top_k
        scored.append(_public(it) | {"lexical_score": round(lex, 6), "vector_score": round(vec, 6), "score": round(0.5 * lex + 0.5 * vec, 6),
                                     "via": sorted({p for p, s in (("lexical", lex), ("vector", vec)) if s > 0}), "_fp": _fingerprint(it), "_tv": tv})
    # semantic adapter: the rank layer reorders items it scored; the expand layer may add validated candidates (ids that exist in `items`), never anything else
    rank_scores, by_item = semantic.get("scores", {}), {it["item_id"]: it for it in items}
    for r in scored:
        if r["item_id"] in rank_scores:
            r["score"] = round((1 - SEMANTIC_WEIGHT) * r["score"] + SEMANTIC_WEIGHT * rank_scores[r["item_id"]], 6)
            r["via"] = sorted(set(r["via"]) | {"semantic"})
            r["semantic_rank_score"] = rank_scores[r["item_id"]]
    present_ids = {r["item_id"] for r in scored}
    for cid in semantic.get("candidate_ids", []):
        if cid in by_item and cid not in present_ids:
            it = by_item[cid]
            rs = rank_scores.get(cid)
            scored.append(_public(it) | {"lexical_score": 0.0, "vector_score": 0.0, "score": round(SEMANTIC_WEIGHT * rs, 6) if rs is not None else 0.0, "via": ["semantic"],
                                         "semantic_candidate": True, "_fp": _fingerprint(it), "_tv": it["term_vector"]})
            present_ids.add(cid)
    scored.sort(key=lambda r: (-r["score"], r["kind"], r["item_id"]))
    groups: dict = {}
    for r in scored:
        groups.setdefault(r["_fp"] if "group" in features else r["item_id"], []).append(r)
    slots = []
    for members in groups.values():
        rep = sorted(members, key=lambda r: (r.get("authority") != "TARGET", -r["score"], r["item_id"]))[0]
        slots.append((rep, members))
    chosen, used, covered = [], 0, set()
    pool = sorted(slots, key=lambda s: (-s[0]["score"], s[0]["item_id"]))
    while pool and len(chosen) < top_k:
        def gain(slot):
            fresh = sum(qw[t] for t in distinct if weights[t] == 1.0 and t in slot[0]["_tv"] and t not in covered)
            return slot[0]["score"] + (COVERAGE_WEIGHT * fresh / distinct_total if "coverage" in features else 0.0)
        best = max(pool, key=lambda s: (gain(s), -len(s[0]["item_id"]), s[0]["item_id"]))
        if used + len(best[0]["text"]) > max_chars:
            break
        pool.remove(best)
        rep, members = best
        chosen.append(rep | {"versions": [{"item_id": m["item_id"], "authority": m.get("authority"), "status": m.get("status")}
                                          for m in sorted(members, key=lambda m: m["item_id"])] if len(members) > 1 else []})
        used += len(rep["text"])
        covered |= {t for t in distinct if t in rep["_tv"]}
    for r in chosen:
        r.pop("_fp", None)
        r.pop("_tv", None)
    by_id = {it["item_id"]: it for it in items if it["kind"] == "rule"}
    present = {v for r in chosen for v in ([r["item_id"]] + [m["item_id"] for m in r["versions"]])}
    seeds = {v for r in [r for r in chosen if r["kind"] == "rule"][:GRAPH_NEIGHBOR_SEEDS] for v in [r["item_id"]] + [m["item_id"] for m in r["versions"]]}
    graph_added = []
    for e in sorted(relations, key=lambda e: e["edge_id"]):
        if e["type"] != "conflicts_with" or e["status"] == "REJECTED":
            continue
        for seed, other in ((e["from"], e["to"]), (e["to"], e["from"])):
            if seed in seeds and other in by_id and other not in present and len(graph_added) < GRAPH_NEIGHBOR_MAX and len(chosen) + len(graph_added) < top_k:
                graph_added.append(_public(by_id[other]) | {"lexical_score": 0.0, "vector_score": 0.0, "score": 0.0, "via": ["graph"], "graph_from": seed, "versions": []})
                present.add(other)
    return {"items": chosen + graph_added,
            "retrieval": {"method": METHOD, "learned_embeddings": False, "embedding_version": None, "lexicon_version": (lexicon or {}).get("lexicon_version"),
                          "lexicon_sha256": (lexicon or {}).get("sha256"), "query_tokens": sorted(t for t, w in weights.items() if w == 1.0),
                          "expanded_terms": sorted(t for t, w in weights.items() if w != 1.0 and t not in semantic_terms), "semantic_terms": sorted(semantic_terms),
                          "distinctive_terms": sorted(distinct)},
            "coverage": {"total_candidates": len(scored), "returned": len(chosen) + len(graph_added), "graph_added": len(graph_added),
                         "versions_merged": sum(len(m) - 1 for _, m in slots), "omitted_count": len(slots) - len(chosen), "truncated": len(slots) > len(chosen),
                         "max_items": top_k, "max_chars": max_chars}}


def counterevidence(selected_rule_ids: Iterable[str], rules: Iterable[Mapping], relations: Iterable[Mapping], gaps: Iterable[Mapping]) -> list:
    """What argues against or qualifies the selected rules: rules they conflict with and open questions about them, even when those did not match the query."""
    selected, by_id = set(selected_rule_ids), {r["rule_id"]: r for r in rules}
    out = []
    for e in sorted(relations, key=lambda e: e["edge_id"]):
        if e["type"] == "conflicts_with" and e["status"] != "REJECTED" and (e["from"] in selected or e["to"] in selected):
            other = e["to"] if e["from"] in selected else e["from"]
            if other in by_id and by_id[other]["status"] not in _WITHDRAWN:
                out.append({"kind": "conflicting_rule", "rule_id": other, "title": by_id[other]["title"], "with": e["from"] if other == e["to"] else e["to"],
                            "edge_id": e["edge_id"], "evidence_refs": e["provenance_refs"], "authority": by_id[other]["authority"], "text": rule_item(by_id[other])["text"]})
    for g in sorted(gaps, key=lambda g: g["gap_id"]):
        if g["status"] in ("OPEN", "REOPENED") and selected & set(g["affected_rule_ids"]):
            out.append({"kind": "open_question", "gap_id": g["gap_id"], "question": g["question"], "priority": g["priority"], "affected_rule_ids": g["affected_rule_ids"]})
    return out


def graph_paths(selected_rule_ids: Iterable[str], relations: list, *, max_hops: int = business_graph.MAX_HOPS, max_nodes: int = business_graph.MAX_NODES) -> list:
    paths = []
    for rid in sorted(set(selected_rule_ids)):
        t = business_graph.traverse([e for e in relations if e["status"] != "REJECTED"], rid, max_hops=max_hops, max_nodes=max_nodes)
        paths.append({"start": rid, "nodes": t["nodes"], "paths": t["paths"], "omitted": t["omitted"]})
    return paths
