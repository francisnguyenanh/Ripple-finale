"""Advisory memos written by a model over an immutable pack of server facts (SUR-06 risk / trade-off memo of a confirmed change, SUR-08 synthesis memo of a review
board report). Pure functions: no I/O, no provider.

The model chooses what matters, explains trade-offs and asks questions; the server decides what may be kept:
  * every statement cites at least one ref of the pack and only refs of the pack;
  * every number in a statement is a number written in one of its cited items (no invented money, probability, percentage or count);
  * forecast / certainty wording and status claims the facts do not hold are refused;
  * materiality is never below the algorithm's or the board's severity of a cited item (the floor is applied and shown, never lowered);
  * every item the pack marks `must_address` (a HIGH severity, an open question, a minority or unresolved finding, a missing required role) that no kept statement
    cites is listed by the server in `not_addressed`: a memo can leave it out, it cannot hide it.
A memo never changes a severity, a verdict, a required review or the change itself; it is advisory and labelled as such.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

SCHEMA = "ripple-advisory-memo/1"
MODEL_KEYS = frozenset({"status", "statements", "open_questions"})
STATUSES = ("memo", "insufficient")
MATERIALITY = ("low", "medium", "high", "critical")
_RANK = {m: i for i, m in enumerate(MATERIALITY)}
_SEVERITY_ALIASES = {"low": "low", "medium": "medium", "moderate": "medium", "high": "high", "critical": "critical"}
LABEL = "Advisory memo written by a model over the listed facts. It does not change any severity, verdict, required review or the change; a person decides."

_NUMBER = re.compile(r"(?<![A-Za-z0-9_])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s*%)?")
_NOT_PLAIN = re.compile(r"[<>`|\\]|\*\*|^#|\]\(|https?://", re.M)
_FORECAST = re.compile(r"\b(will (?:save|cost|lose|reduce|increase|cut|prevent)|guarantee[sd]?|certain(?:ly)?|definitely|undoubtedly|probability|likelihood of|"
                       r"chance of|expected loss|expected saving|return on investment|ROI|forecast)\b", re.I)
_STATUS_WORDS = ("approved", "compliant", "implemented", "verified", "signed off", "accepted")


def severity_rank(value) -> int | None:
    v = _SEVERITY_ALIASES.get(str(value or "").strip().lower())
    return _RANK[v] if v else None


def numbers_in(text: str) -> set:
    """Normalised numbers written in a text: '1,000,000' -> '1000000', '12.50' -> '12.5', '15 %' -> '15%' (a percentage is a different number from its digits)."""
    out = set()
    for whole, frac, pct in _NUMBER.findall(str(text or "")):
        n = whole.replace(",", "")
        if frac and frac.strip("0"):
            n += "." + frac.rstrip("0")
        out.add(n + ("%" if pct else ""))
    return out


def pack_item(ref: str, kind: str, text: str, *, floor=None, must_address: bool = False, reason: str = "") -> dict:
    return {"ref": ref, "kind": kind, "text": " ".join(str(text).split()), "floor": _SEVERITY_ALIASES.get(str(floor or "").lower()), "must_address": bool(must_address),
            "reason": reason}


def prompt_lines(pack: Iterable[Mapping]) -> str:
    return "\n".join(f"- ref={p['ref']} [{p['kind']}{', severity ' + p['floor'] if p['floor'] else ''}{', must be addressed' if p['must_address'] else ''}]: {p['text']}"
                     for p in pack)


def validate_model_object(obj, limits: Mapping, kinds: Iterable[str]) -> dict:
    """Shape only (the content checks are in `check`). Raises ValueError."""
    if not isinstance(obj, dict) or set(obj) != set(MODEL_KEYS):
        raise ValueError(f"keys must be {sorted(MODEL_KEYS)}")
    if obj["status"] not in STATUSES:
        raise ValueError("status invalid")
    if not isinstance(obj["statements"], list) or not isinstance(obj["open_questions"], list):
        raise ValueError("statements and open_questions must be lists")
    if len(obj["statements"]) > int(limits["max_statements"]) * 3:                                # far beyond the cap: the answer is refused, not trimmed
        raise ValueError("too many statements")
    kinds = set(kinds)
    for s in obj["statements"]:
        if not isinstance(s, dict) or set(s) != {"kind", "statement", "refs", "materiality", "rationale"}:
            raise ValueError("statement keys invalid")
        if not isinstance(s["statement"], str) or not isinstance(s["rationale"], str) or not isinstance(s["refs"], list) or not all(isinstance(r, str) for r in s["refs"]):
            raise ValueError("statement fields invalid")
        if s["kind"] not in kinds or s["materiality"] not in MATERIALITY:
            raise ValueError("statement kind or materiality invalid")
    if not all(isinstance(q, str) for q in obj["open_questions"]):
        raise ValueError("open_questions must be strings")
    return obj


def _plain(text: str, max_chars: int) -> bool:
    return bool(text.strip()) and len(text) <= max_chars and not _NOT_PLAIN.search(text)


def canonical_ref(raw, by_ref: Mapping) -> str | None:
    """The offered ref a written ref names, or None. Exact match first; then two deterministic repairs only: the prompt line's own `ref=` label copied with the value
    (observed live, EV3-RISKMEMO-20261019-01), and a bare id (`rule-x` for `rule:rule-x`) that names EXACTLY ONE offered ref. Only refs of this pack can result (the pack is
    already built from the pinned snapshot and the reader's rights); an unknown, ambiguous or purely numeric bare ref stays unmatched and the statement is refused."""
    r = str(raw).strip()
    if r in by_ref:
        return r
    if r[:4].lower() == "ref=":
        r = r[4:].strip()
        if r in by_ref:
            return r
    if not r or r.isdigit() or ":" in r:
        return None
    hits = [k for k in by_ref if k.split(":", 1)[-1] == r]
    return hits[0] if len(hits) == 1 else None


def check(model: Mapping, pack: list, limits: Mapping) -> dict:
    """Keep what the facts support. -> {kept, rejected, open_questions, not_addressed, addressed, counts}. A statement is refused whole (with its reason), never edited."""
    by_ref = {p["ref"]: p for p in pack}
    kept, rejected, seen_text = [], [], set()
    max_chars, max_refs, cap = int(limits["max_statement_chars"]), int(limits["max_refs"]), int(limits["max_statements"])
    for i, s in enumerate(model["statements"]):
        text = " ".join(s["statement"].split())
        written = list(dict.fromkeys(s["refs"]))
        canon = [canonical_ref(r, by_ref) for r in written]
        refs = list(dict.fromkeys(c if c is not None else r for c, r in zip(canon, written)))
        reason = None
        if len(kept) >= cap:
            reason = "over_statement_cap"
        elif not _plain(text, max_chars) or not _plain(s["rationale"] or "-", max_chars):
            reason = "not_plain_text"
        elif not refs:
            reason = "no_refs"
        elif len(refs) > max_refs:
            reason = "too_many_refs"
        elif any(c is None for c in canon):
            reason = "ref_not_offered"
        elif text.lower() in seen_text:
            reason = "duplicate"
        else:
            cited_numbers = set().union(*(numbers_in(by_ref[r]["text"]) for r in refs))
            stray = sorted(n for n in numbers_in(text) | numbers_in(s["rationale"]) if n not in cited_numbers)
            cited_text = " ".join(by_ref[r]["text"].lower() for r in refs)
            if stray:
                reason = "figure_not_in_cited_facts:" + ",".join(stray[:3])
            elif _FORECAST.search(text) or _FORECAST.search(s["rationale"]):
                reason = "forecast_or_certainty"
            elif any(re.search(r"\b" + w + r"\b", text.lower()) and w not in cited_text and not re.search(r"\bnot\s+(?:yet\s+)?" + w, text.lower()) for w in _STATUS_WORDS):
                reason = "status_claim_not_in_facts"
        if reason:
            rejected.append({"index": i, "statement": text[:max_chars], "refs": written[:max_refs], "reason": reason})
            continue
        seen_text.add(text.lower())
        floors = [by_ref[r]["floor"] for r in refs if by_ref[r]["floor"]]
        floor = max(floors, key=lambda f: _RANK[f]) if floors else None
        effective = s["materiality"] if floor is None or _RANK[s["materiality"]] >= _RANK[floor] else floor
        kept.append({"kind": s["kind"], "statement": text, "rationale": " ".join(s["rationale"].split()), "refs": refs, "materiality_proposed": s["materiality"],
                     "materiality": effective, "floor_applied": effective != s["materiality"], "origin": "model",
                     **({"refs_written": written} if written != refs else {})})
    questions = []
    for q in model["open_questions"][: int(limits["max_questions"])]:
        q = " ".join(q.split())
        if _plain(q, max_chars) and not numbers_in(q) - set().union(*(numbers_in(p["text"]) for p in pack)) and not _FORECAST.search(q):
            questions.append(q)
    cited = {r for k in kept for r in k["refs"]}
    not_addressed = [{"ref": p["ref"], "kind": p["kind"], "text": p["text"], "floor": p["floor"], "reason": p["reason"] or "must be addressed",
                      "origin": "server"} for p in pack if p["must_address"] and p["ref"] not in cited]
    return {"kept": kept, "rejected": rejected, "open_questions": questions, "not_addressed": not_addressed,
            "addressed": sorted(p["ref"] for p in pack if p["must_address"] and p["ref"] in cited),
            "counts": {"offered": len(pack), "must_address": sum(1 for p in pack if p["must_address"]), "kept": len(kept), "rejected": len(rejected),
                       "floor_applied": sum(1 for k in kept if k["floor_applied"]), "not_addressed": len(not_addressed),
                       "refs_canonicalized": sum(1 for k in kept if "refs_written" in k)}}


def render_memo(result: Mapping) -> dict:
    """What every reader of a completed memo sees: the label, kept statements, the server's list of items the memo did not address, refusals by reason."""
    m = result["memo"]
    from core.change_intelligence.v2.business_impact import ref_key
    statements = [{**k, "ref_keys": sorted({ref_key(r.split(":", 1)[1]) for r in k.get("refs") or [] if r.startswith("rule:")})} for k in m["kept"]]   # EUI-07 typed links
    return {"label": LABEL, "status": result["model_status"], "statements": statements, "not_addressed": m["not_addressed"], "open_questions": m["open_questions"],
            "rejected": [{"reason": r["reason"], "refs": r["refs"]} for r in m["rejected"]], "counts": m["counts"], "complete": not m["not_addressed"],
            "advisory_only": True}
