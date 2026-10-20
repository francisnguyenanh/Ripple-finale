"""SUR-05: proposed semantic correspondence between the old and the new side of a source refresh. Pure functions: no I/O, no provider.

A model reads bounded excerpts of the deleted / changed artifacts (old side, with the rules bound to them) and of the added / changed artifacts (new side) and proposes which
old artifact or obligation continues where (moved, rewritten, split, merged, changed, or no counterpart). The server keeps a proposal only if its paths were offered, its
quotes are verbatim in the excerpts, its rule ids are bound to the old artifact and its numbers come from the quotes or those rules. It adds the exact facts the model cannot
claim: whether the bytes are identical (`bytes_identical`) and which rename proof the refresh contract would accept (`content_hash` only for identical bytes; otherwise only a
person's `human_confirmed` proof with a reason). Every proposal is PROPOSED with `is_rename_proof: false`: it never links identities, never marks anything verified and never
changes the refresh.
"""
from __future__ import annotations

import re
from typing import Mapping

SCHEMA = "ripple-source-correspondence/1"
MODEL_KEYS = frozenset({"status", "pairs", "notes"})
STATUSES = ("pairs", "none_found", "unsure")
RELATIONS = ("moved_same_content", "moved_rewritten", "split", "merged", "obligation_changed", "no_counterpart")
CONFIDENCE = ("low", "medium", "high")
PAIR_KEYS = frozenset({"old_path", "new_path", "relation", "rule_ids", "old_quote", "new_quote", "rationale", "confidence"})
LABEL = ("Proposed correspondence written by a model over excerpts of the refresh. It is not a rename proof and links nothing; only identical bytes allow a content_hash "
         "proof, anything else needs a person's human_confirmed proof with a reason.")

_NUMBER = re.compile(r"(?<![A-Za-z0-9_])(\d{1,3}(?:[,_]\d{3})+|\d+)(?:\.(\d+))?")
_NOT_PLAIN = re.compile(r"[<>`|\\]|\*\*|^#|\]\(|https?://", re.M)


def norm(text: str) -> str:
    return " ".join(str(text or "").split())


def numbers_in(text: str) -> set:
    out = set()
    for whole, frac in _NUMBER.findall(str(text or "")):
        n = re.sub(r"[,_]", "", whole)
        if frac and frac.strip("0"):
            n += "." + frac.rstrip("0")
        out.add(n)
    return out


def side(path: str, text: str, *, hash_: str, max_chars: int, rules=()) -> dict:
    t = str(text or "")
    return {"path": path, "excerpt": t[:max_chars], "truncated": len(t) > max_chars, "hash": hash_, "rules": [{"rule_id": r["rule_id"], "title": r["title"]} for r in rules]}


def prompt_block(old_sides: list, new_sides: list) -> tuple[str, str]:
    def block(sides, label):
        out = []
        for s in sides:
            rules = "".join(f"\n  bound rule id={r['rule_id']}: {r['title']}" for r in s["rules"])
            out.append(f"=== {label} path={s['path']}{' (excerpt cut)' if s['truncated'] else ''}{rules}\n{s['excerpt']}\n=== end {s['path']}")
        return "\n".join(out) or "(none)"
    return block(old_sides, "OLD"), block(new_sides, "NEW")


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(MODEL_KEYS):
        raise ValueError(f"keys must be {sorted(MODEL_KEYS)}")
    if obj["status"] not in STATUSES or not isinstance(obj["pairs"], list) or not isinstance(obj["notes"], list):
        raise ValueError("status / pairs / notes invalid")
    if len(obj["pairs"]) > int(limits["max_pairs"]) * 3:
        raise ValueError("too many pairs")
    for p in obj["pairs"]:
        if not isinstance(p, dict) or set(p) != set(PAIR_KEYS):
            raise ValueError("pair keys invalid")
        if not all(isinstance(p[k], str) for k in PAIR_KEYS - {"rule_ids"}) or not isinstance(p["rule_ids"], list) or not all(isinstance(r, str) for r in p["rule_ids"]):
            raise ValueError("pair fields invalid")
        if p["relation"] not in RELATIONS or p["confidence"] not in CONFIDENCE:
            raise ValueError("pair relation or confidence invalid")
    if not all(isinstance(n, str) for n in obj["notes"]):
        raise ValueError("notes must be strings")
    return obj


def check(model: Mapping, old_sides: list, new_sides: list, limits: Mapping) -> dict:
    """-> {kept, rejected, notes, unmatched_old}. A proposal is refused whole with its reason, never edited."""
    olds, news = {s["path"]: s for s in old_sides}, {s["path"]: s for s in new_sides}
    kept, rejected, seen = [], [], set()
    max_chars = int(limits["max_rationale_chars"])
    for i, p in enumerate(model["pairs"]):
        o, n = olds.get(p["old_path"]), news.get(p["new_path"]) if p["new_path"] else None
        oq, nq, rationale = norm(p["old_quote"]), norm(p["new_quote"]), norm(p["rationale"])
        rule_ids = list(dict.fromkeys(p["rule_ids"]))
        reason = None
        if len(kept) >= int(limits["max_pairs"]):
            reason = "over_pair_cap"
        elif o is None:
            reason = "old_path_not_offered"
        elif p["relation"] == "no_counterpart" and p["new_path"]:
            reason = "no_counterpart_with_new_path"
        elif p["relation"] != "no_counterpart" and n is None:
            reason = "new_path_not_offered"
        elif not rationale or len(rationale) > max_chars or _NOT_PLAIN.search(rationale):
            reason = "not_plain_text"
        elif not oq or oq not in norm(o["excerpt"]):
            reason = "old_quote_not_in_excerpt"
        elif n is not None and (not nq or nq not in norm(n["excerpt"])):
            reason = "new_quote_not_in_excerpt"
        elif any(r not in {x["rule_id"] for x in o["rules"]} for r in rule_ids):
            reason = "rule_not_bound_to_old_artifact"
        else:
            allowed = numbers_in(oq) | numbers_in(nq) | {x for r in o["rules"] if r["rule_id"] in rule_ids for x in numbers_in(r["title"])}
            stray = sorted(numbers_in(rationale) - allowed)
            if stray:
                reason = "figure_not_in_quotes_or_rules:" + ",".join(stray[:3])
            elif (p["old_path"], p["new_path"], p["relation"]) in seen:
                reason = "duplicate"
        if reason:
            rejected.append({"index": i, "old_path": p["old_path"], "new_path": p["new_path"], "relation": p["relation"], "reason": reason})
            continue
        seen.add((p["old_path"], p["new_path"], p["relation"]))
        identical = bool(n is not None and o["hash"] and o["hash"] == n["hash"])
        kept.append({"old_path": p["old_path"], "new_path": p["new_path"] or None, "relation": p["relation"], "rule_ids": rule_ids, "old_quote": oq, "new_quote": nq or None,
                     "rationale": rationale, "confidence": p["confidence"], "status": "PROPOSED", "is_rename_proof": False, "bytes_identical": identical,
                     "proof_possible": ("content_hash" if identical else "human_confirmed_with_reason") if n is not None and p["old_path"] != p["new_path"] else "none_needed",
                     "origin": "model"})
    covered = {k["old_path"] for k in kept}
    notes = [norm(x)[:max_chars] for x in model["notes"][: int(limits["max_notes"])] if norm(x) and not _NOT_PLAIN.search(x)]
    return {"kept": kept, "rejected": rejected, "notes": notes,
            "unmatched_old": [{"path": s["path"], "rule_ids": [r["rule_id"] for r in s["rules"]], "origin": "server"} for s in old_sides if s["path"] not in covered]}
