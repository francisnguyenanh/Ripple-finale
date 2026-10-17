"""Pitch-scope scorer v0.2 (descriptive only). Implements RUBRIC.md v3.

Inputs
  labels      {artifact_id: label dict}   (gold_rules/traps/soft_guidance/observed_facts; optional per-gold
              `subject_aliases` / `value_aliases`, locked by hash)
  artifacts   {artifact_id: {"text": str, "format": "yaml"|"md"}
               | {"observations": [product observation dicts]}}
              Observations come from the PRODUCT parsers (core.change_intelligence.v2.source_ingest_adapters
              parse_yaml / parse_markdown); locators are {type:key_path,key_path} / {type:line_span,line_start,line_end}.
  runs        {run_id: {artifact_id: prediction}}  prediction = the extraction result dict production/D0 emit

STRICT (headline `found`) = subject/predicate, value, unit, operator, scope, modality AND evidence all correct.
LENIENT = found + partial (partial = value+subject right but unit missing, operator missing, scope wrong/superset,
modality observed, C6 exception omitted, merged tiers, or evidence not correct/unverifiable). Strict is always reported
first and is never merged with lenient. Evidence UNVERIFIABLE is never correct and stays in the denominator.
Subject matching is a FIXED pre-registered heuristic (GENERIC_WORDS below, exact distinctive-token-set equality,
alias list from the locked labels). Anything the heuristic cannot decide is emitted as `undecided` (own counter and list
with both texts) for blind adjudication, never silently strict or missed. No semantic matcher, no other synonym logic.
Expected labels are never derived from predictions. No thresholds, no significance claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

SCORER_VERSION = "pitch_scorer.4.0.0"
CLASSES = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8")
# Pre-registered generic words (stemmed form), fixed before any result; they never carry the distinguishing noun.
GENERIC_WORDS = frozenset({"cap", "limit", "max", "maximum", "min", "minimum", "per", "daily", "weekly", "monthly",
                           "yearly", "annual", "annually", "nightly", "hourly", "total", "threshold", "ceiling",
                           "floor", "default", "value", "amount", "number", "rule", "policy"})
_STOP = {"the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are", "be", "by", "with", "all",
         "at", "as", "that", "this", "must", "may"}
_NORMATIVE = frozenset({"must", "may"})
_MODALITY = frozenset({"must", "may", "observed"})  # product MODALITY_VALUES (core/change_intelligence/v2/schema.py)
_IGNORABLE_SCOPE_KEYS = {"section", "unresolved"}
_FAIL_STATUS = {"error", "timeout", "model_mismatch", "failed", "failure"}
_PASS_KEYS = ("error", "status", "model", "served_model", "latency_s", "latency_ms", "cost_usd", "cost",
              "input_tokens", "output_tokens", "attempts")
_UNIT_CANON = {"%": "%", "percent": "%", "pct": "%", "yen": "jpy", "day": "day", "hour": "hour",
               "minute": "minute", "min": "minute", "second": "second", "sec": "second"}
_CURRENCIES = {"jpy", "usd", "eur", "vnd", "gbp", "cny", "krw", "sgd", "aud", "thb"}
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_OPS = {">=": ">=", "<=": "<=", ">": ">", "<": "<", "==": "==", "=": "==", "≥": ">=", "≤": "<="}
_DIRECTIONAL = {">", ">=", "<", "<="}
_CMP_WORDS = (("no less than", ">="), ("not less than", ">="), ("at least", ">="), ("no more than", "<="),
              ("not more than", "<="), ("at most", "<="), ("up to", "<="), ("greater than", ">"),
              ("more than", ">"), ("fewer than", "<"), ("less than", "<"), ("above", ">"), ("over", ">"),
              ("under", "<"), ("below", "<"))
_NEGATION = re.compile(r"\b(not|outside|except|excluding|excluded|exclude|other than|apart from|without)\b")


_NEG_TOKENS = frozenset({"not", "no", "non", "except", "excluding", "without", "outside"})


def pair_id(aid, gold, cand) -> str:
    """Stable anonymous id of an undecided pair: artifact + gold id + both texts. No run, arm or candidate id."""
    blob = json.dumps([aid, gold.get("id"),
                       [gold.get(k) for k in ("subject", "predicate", "value", "unit", "scope")],
                       [cand.get(k) for k in ("subject", "predicate", "value", "unit", "scope")]],
                      sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class AliasLockError(ValueError):
    """Alias sets are refused unless their hash equals the hash locked with the labels."""


# ------------------------------------------------------------------ normalisation
def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(_text(x) for x in v)
    if isinstance(v, dict):
        return " ".join(f"{k} {_text(x)}" for k, x in v.items())
    return json.dumps(v) if isinstance(v, bool) else str(v)


def _tokens(obj) -> set[str]:
    if obj is None:
        return set()
    text = _text(obj)
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    out = set()
    for t in re.split(r"[^A-Za-z0-9.]+", text.lower()):
        t = t.strip(".")
        if not t or t in _STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return out


def _unit(u):
    if not isinstance(u, str) or not u.strip():
        return None
    u = u.strip().lower()
    if u in _UNIT_CANON:
        return _UNIT_CANON[u]
    if len(u) > 3 and u.endswith("s"):
        u = u[:-1]
    return _UNIT_CANON.get(u, u)


def _atoms(value, unit=None):
    """Normalised value atoms: list of (kind, payload, unit, operator). kind in num|bool|str."""
    base_unit = _unit(unit)
    if isinstance(value, bool):
        return [("bool", value, None, None)]
    if isinstance(value, (int, float)):
        return [("num", Decimal(str(value)), base_unit, None)]
    if isinstance(value, (list, tuple)):
        out = []
        for x in value:
            out += _atoms(x, unit)
        return out
    if isinstance(value, dict):
        out = []
        for x in value.values():
            out += _atoms(x, unit)
        return out
    if not isinstance(value, str):
        return []
    s = value.strip()
    low = s.lower()
    if low in ("true", "yes"):
        return [("bool", True, None, None)]
    if low in ("false", "no"):
        return [("bool", False, None, None)]
    op = None
    for sym in sorted(_OPS, key=len, reverse=True):
        if s.startswith(sym):
            op, s = _OPS[sym], s[len(sym):].strip()
            break
    if op is None:
        for phrase, o in _CMP_WORDS:
            if low.startswith(phrase + " ") or low == phrase:
                op, s = o, s[len(phrase):].strip()
                break
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][\d:.+Z-]*)?", s):  # ISO date (product encodes YAML dates as text)
        return [("str", s[:10], None, op)]
    nums = _NUM.findall(s)
    if nums:
        rest = _NUM.sub(" ", s).lower()
        u = base_unit
        if "%" in rest:
            u = "%"
        else:
            for w in re.findall(r"[a-z]+", rest):
                if w in _CURRENCIES:
                    u = w
                    break
                if _unit(w) in ("day", "hour", "minute", "second", "%"):
                    u = _unit(w)
                    break
        out = []
        for n in nums:
            try:
                out.append(("num", Decimal(n.replace(",", "")), u, op))
            except InvalidOperation:
                pass
        return out
    return [("str", " ".join(s.lower().split()), None, op)]


def _atom_cmp(g, c, cmpr=None) -> int:
    """2 = equal in value, unit and operator; 1 = value equal but unit missing on candidate; 0 = not equal."""
    if g[0] != c[0]:
        return 0
    if g[0] == "num":
        if g[1] != c[1]:
            return 0
        if cmpr and c[3] is not None and c[3] != cmpr:
            return 0
        if g[2] and c[2] and g[2] != c[2]:
            return 0
        if g[2] and not c[2]:
            return 1
        return 2
    return 2 if g[1] == c[1] else 0


def _norm_cmp(c):
    return _OPS.get(c) if isinstance(c, str) else None


# ------------------------------------------------------------------ scope / subject / exceptions
def _is_all(scope) -> bool:
    if scope is None:
        return True
    if isinstance(scope, str):
        return scope.strip().lower() in ("", "all", "any", "n/a")
    return isinstance(scope, dict) and not scope


def _restricts(scope) -> bool:
    if _is_all(scope):
        return False
    if isinstance(scope, dict):
        return any(k not in _IGNORABLE_SCOPE_KEYS for k in scope)
    return True


def _scope_values(scope):
    if isinstance(scope, dict):
        return {k: v for k, v in scope.items() if k not in _IGNORABLE_SCOPE_KEYS}
    return scope


def _cand_pool(c) -> set[str]:
    return _tokens(c.get("scope")) | _tokens(c.get("subject")) | _tokens(c.get("predicate"))


def _negated(cand) -> bool:
    sv = _scope_values(cand.get("scope"))
    vals = list(sv.values()) if isinstance(sv, dict) else [sv]
    return bool(_NEGATION.search(" ".join(_text(v) for v in vals).lower()))


def _scope_ok(gold, cand) -> bool:
    gs = gold.get("scope")
    if _negated(cand):
        return False
    if _is_all(gs):
        return not _restricts(cand.get("scope"))
    return _tokens(gs) <= _cand_pool(cand)


def _scope_superset(gold, cand) -> bool:
    gs = gold.get("scope")
    if _is_all(gs):
        return False
    sv = _scope_values(cand.get("scope"))
    vals = list(sv.values()) if isinstance(sv, dict) else [sv]
    extra = _tokens(vals) - _tokens(gs) - _tokens(gold.get("subject")) - _tokens(gold.get("predicate")) \
        - GENERIC_WORDS - _CURRENCIES - {_unit(gold.get("unit")) or "", _unit(cand.get("unit")) or ""}
    return bool(extra)


def _subject_state(gold, cand, forced: bool, merged: bool, adj=None, aid=None):
    """(state, how) with state in match | undecided | none. Fixed heuristic, subject+predicate tokens only."""
    if forced:
        return "match", "override"
    ct = _tokens(cand.get("subject")) | _tokens(cand.get("predicate"))
    for al in gold.get("subject_aliases") or []:
        at = _tokens(al)
        # aliases substitute subject wording only; they never override polarity (negation words must agree)
        if at and at <= ct and (ct & _NEG_TOKENS) == (at & _NEG_TOKENS):
            return "match", "alias"
    gfull = _tokens(gold.get("subject")) | _tokens(gold.get("predicate"))
    g, c = gfull - GENERIC_WORDS, ct - GENERIC_WORDS
    if not g or not c:  # stoplist must never remove the distinguishing noun: fall back to exact
        return ("match", "exact_full") if gfull and gfull == ct else ("none", None)
    if g == c:
        return "match", "exact"
    if merged and g <= c:
        return "match", "merged_superset"
    if g & c:
        dec = ((adj or {}).get(pair_id(aid, gold, cand)) or {}).get("decision")
        if dec == "match":
            return "match", "adjudicated"
        if dec == "no_match":
            return "none", None
        return "undecided", None
    return "none", None


def _exceptions_ok(gold, cand) -> bool:
    exc = gold.get("exceptions") or []
    if gold.get("class") != "C6" or not exc:
        return True
    have = _tokens(cand.get("exceptions")) | _tokens(cand.get("scope")) | _tokens(cand.get("predicate"))
    for e in exc:
        need = _tokens(e)
        if need and len(need & have) / len(need) < 0.5:
            return False
    return True


def _validate(c):
    """None if well-formed, else a short reason (fail closed)."""
    if not isinstance(c, dict):
        return "candidate_not_object"
    for k in ("subject", "predicate", "value"):
        if k not in c:
            return f"missing_{k}"
    if not isinstance(c["subject"], str) or not isinstance(c["predicate"], str):
        return "subject_or_predicate_not_string"
    if c.get("modality") not in _MODALITY:
        return "bad_modality"
    if "evidence" in c and c["evidence"] is not None and not isinstance(c["evidence"], list):
        return "evidence_not_list"
    if "scope" in c and c["scope"] is not None and not isinstance(c["scope"], (dict, str)):
        return "scope_not_dict_or_string"
    if "exceptions" in c and c["exceptions"] is not None and not isinstance(c["exceptions"], (list, tuple)):
        return "exceptions_not_list"
    return None


# ------------------------------------------------------------------ pairwise match
def _pair(gold, cand, forced: bool, adj=None, aid=None):
    """Return (rank, reasons, how). rank 3 content-strict, 2 partial, 1 wrong value, 0 none, -1 undecided."""
    gatoms = _atoms(gold.get("value"), gold.get("unit"))
    catoms = _atoms(cand.get("value"), cand.get("unit"))
    merged = len(catoms) > 1
    state, how = _subject_state(gold, cand, forced, merged, adj, aid)
    if state == "none" or not gatoms:
        return 0, [], None
    cmpr = _norm_cmp(gold.get("comparator"))
    cop = _norm_cmp(cand.get("comparator"))
    if cop:
        catoms = [(a[0], a[1], a[2], a[3] or cop) for a in catoms]
    alts = [gatoms[0]]
    value_alias = False
    if gatoms[0][0] == "str":  # value aliases substitute string-value text only (never bool/number/polarity)
        for al in gold.get("value_aliases") or []:
            alts += [x for x in _atoms(al, gold.get("unit"))[:1] if x[0] == "str"]
    best, via_alias = 0, False
    for i, ga in enumerate(alts):
        for a in catoms:
            sc = _atom_cmp(ga, a, cmpr)
            if sc > best:
                best, via_alias = sc, i > 0
    if best == 0 and cand.get("outcome") is not None:
        for ga in alts:
            if any(_atom_cmp(ga, a) for a in _atoms(cand["outcome"])):
                best = 2
    if best == 0:
        if state == "undecided":
            return 0, [], None
        return (1, [], how) if _scope_ok(gold, cand) else (0, [], None)
    if state == "undecided":
        return -1, [], None
    reasons = []
    if merged:
        reasons.append("merged_tiers")
    if best == 1:
        reasons.append("wrong_unit")
    if cmpr in _DIRECTIONAL and not any(a[3] for a in catoms):
        reasons.append("comparator_missing")
    if not _scope_ok(gold, cand):
        reasons.append("scope")
    elif _scope_superset(gold, cand):
        reasons.append("scope_superset")
    if cand.get("modality") == "observed":
        reasons.append("modality_observed")
    if not _exceptions_ok(gold, cand):
        reasons.append("exception_omitted")
    return (2 if reasons else 3), reasons, ("value_alias" if via_alias else how)


def _item_text(item) -> str:
    return item.get("text", "") if isinstance(item, dict) else str(item)


def _matches_item(item_text: str, cand) -> bool:
    ctoks = (_tokens(cand.get("subject")) | _tokens(cand.get("predicate"))) - GENERIC_WORDS
    ttoks = _tokens(item_text)
    if not ctoks or len(ctoks & ttoks) / len(ctoks) < 0.5:
        return False
    low = re.sub(r"(?<=\d),(?=\d)", "", item_text.lower())
    for a in _atoms(cand.get("value"), cand.get("unit")):
        if a[0] == "num" and any(Decimal(n) == a[1] for n in _NUM.findall(low)):
            return True
        if a[0] == "str" and a[1] and a[1] in low:
            return True
        if a[0] == "bool" and json.dumps(a[1]) in low:
            return True
    return False


# ------------------------------------------------------------------ source representation / evidence
def build_observations(artifact: dict) -> list[dict]:
    """Product parsed observations (parse_yaml / parse_markdown). Unparseable -> [] (evidence then unresolvable)."""
    if "observations" in artifact:
        return list(artifact["observations"])
    from core.change_intelligence.v2.source_ingest_adapters import parse_markdown, parse_yaml
    raw = artifact.get("text", "")
    data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
    fmt = str(artifact.get("format", "")).lower()
    parser = parse_yaml if fmt in ("yaml", "yml") else parse_markdown
    return list(parser(str(artifact.get("file", "artifact")), data).observations)


def _obs_text(o) -> str:
    v = o.get("raw_value")
    return v if isinstance(v, str) else json.dumps(v, default=str)


def _contains_value(text: str, gold) -> bool:
    low = re.sub(r"(?<=\d),(?=\d)", "", text.lower())
    for a in _atoms(gold.get("value"), gold.get("unit"))[:1]:
        if a[0] == "num":
            return any(Decimal(n) == a[1] for n in _NUM.findall(low))
        if a[0] == "bool":
            return bool(re.search(r"\b" + json.dumps(a[1]) + r"\b", low)) or \
                bool(re.search(r"\b" + ("yes" if a[1] else "no") + r"\b", low))
        return a[1] in " ".join(low.split())
    return False


EV_KEYS = ("checked", "correct", "unverifiable", "wrong_artifact", "foreign_tenant_workspace", "context_tampered",
           "wrong_path", "wrong_chunk", "not_containing_value", "unresolvable", "no_evidence", "hash_mismatch")
CONTENT_KEYS = ("correct", "unverifiable", "wrong_artifact", "wrong_path", "wrong_chunk", "not_containing_value",
                "unresolvable", "no_evidence")
OWNERSHIP_KEYS = ("valid", "unverifiable", "foreign_tenant_workspace", "context_tampered")
HASH_KEYS = ("valid", "unverifiable", "hash_mismatch")


def context_digest(ctx: dict) -> str:
    body = {k: v for k, v in ctx.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def make_run_context(tenant_id, workspace_id, source_revision_ids, artifact_revision_ids: dict, run_id=None,
                     artifact_content_hashes: dict | None = None) -> dict:
    """TRUSTED run context, normally produced by pitch.eval.run_context.build_run_context_from_import from the product's
    own import result / store records (never from a prediction). Binds tenant, workspace, run (when given), source
    revisions, artifact revisions and artifact content hashes (product `blob_hash` = "sha256:"+sha256(bytes)).
    The embedded sha256 is only a self-consistency check; protection against a re-hashed forgery comes ONLY from a hash
    obtained independently (run manifest written at import time, or recomputed from the store) and passed as
    `trusted_context_hashes`."""
    ctx = {"tenant_id": tenant_id, "workspace_id": workspace_id, "source_revision_ids": sorted(source_revision_ids),
           "artifact_revision_ids": {k: sorted(v) for k, v in artifact_revision_ids.items()}}
    if run_id is not None:
        ctx["run_id"] = run_id
    if artifact_content_hashes is not None:
        ctx["artifact_content_hashes"] = dict(artifact_content_hashes)
    ctx["sha256"] = context_digest(ctx)
    return ctx


def _ctx_state(ctx, trusted_hash=None, run_id=None) -> str:
    """ok | missing | unpinned | tampered | wrong_run. A context is usable only when an INDEPENDENT trusted hash is
    supplied and equals the context digest (unpinned otherwise), and, if it names a run, that run is this run."""
    if not isinstance(ctx, dict) or not ctx:
        return "missing"
    if not isinstance(ctx.get("sha256"), str) or ctx["sha256"] != context_digest(ctx):
        return "tampered"
    if not ctx.get("tenant_id") or not ctx.get("workspace_id") or not isinstance(ctx.get("source_revision_ids"), list):
        return "missing"
    if trusted_hash is None:
        return "unpinned"
    if trusted_hash != ctx["sha256"]:
        return "tampered"
    if ctx.get("run_id") is not None and ctx["run_id"] != run_id:
        return "wrong_run"
    return "ok"


def _ownership_row(ev, ctx_state, ctx, pred) -> str:
    """Row (b): tenant/workspace ownership against the TRUSTED context only. EvidenceRef has no tenant field; a claimed
    tenant_id/workspace_id on the evidence or prediction can only contradict (=> foreign), never validate."""
    if ctx_state in ("tampered", "wrong_run"):
        return "context_tampered"
    if ctx_state != "ok":
        return "unverifiable"
    for src in (ev, pred if isinstance(pred, dict) else {}):
        if src.get("tenant_id") is not None and src["tenant_id"] != ctx["tenant_id"]:
            return "foreign_tenant_workspace"
        if src.get("workspace_id") is not None and src["workspace_id"] != ctx["workspace_id"]:
            return "foreign_tenant_workspace"
    sr = ev.get("source_revision_id")
    if sr is None:
        return "unverifiable"
    return "valid" if sr in ctx["source_revision_ids"] else "foreign_tenant_workspace"


def _content_row(ev, aid, gold, obs, ctx_state, ctx) -> str:
    """Row (a): artifact revision / locator / content against EvidenceRef fields and the product parsers."""
    if not isinstance(ev, dict):
        return "unresolvable"
    allowed = (ctx.get("artifact_revision_ids") or {}).get(aid) if ctx_state == "ok" else None
    got = ev.get("artifact_revision_id")
    if allowed and got is not None and got not in allowed:
        return "wrong_artifact"
    loc = ev.get("locator")
    if isinstance(loc, str):
        try:
            loc = json.loads(loc)
        except ValueError:
            loc = None
    if not allowed or got is None or not isinstance(loc, dict) or not loc:
        return "unverifiable"
    hit = [o for o in obs if o.get("locator") == loc]
    if not hit:
        return "wrong_chunk" if loc.get("type") == "line_span" else \
            "wrong_path" if loc.get("type") == "key_path" else "unresolvable"
    return "correct" if any(_contains_value(_obs_text(o), gold) for o in hit) else "not_containing_value"


def _hash_row(ev, aid, ctx_state, ctx) -> str:
    """Row (c): EvidenceRef.content_hash must equal the artifact's product blob_hash recorded in the trusted context.
    Missing/"unknown" hash or missing expectation => unverifiable; different => hash_mismatch."""
    if not isinstance(ev, dict) or ctx_state != "ok":
        return "unverifiable"
    expected = (ctx.get("artifact_content_hashes") or {}).get(aid)
    got = ev.get("content_hash")
    if not expected or not isinstance(got, str) or not got or got == "unknown":
        return "unverifiable"
    return "valid" if got == expected else "hash_mismatch"


def _evidence_verdict(cand, gold, obs, aid, ctx_state, ctx, pred, hash_policy="reported_only"):
    """Returns (final, content_row, ownership_row, hash_row). Fully valid ("correct") only if all three rows pass."""
    evs = cand.get("evidence")
    if not evs:
        return "no_evidence", "no_evidence", "unverifiable", "unverifiable"
    rows = []
    for ev in evs:
        if isinstance(ev, dict):  # product result dicts carry revision ids at candidate level, not per evidence item
            ev = {**{k: cand[k] for k in ("source_revision_id", "artifact_revision_id") if k in cand}, **ev}
        c = _content_row(ev, aid, gold, obs, ctx_state, ctx)
        o = _ownership_row(ev, ctx_state, ctx, pred) if isinstance(ev, dict) else "unverifiable"
        h = _hash_row(ev, aid, ctx_state, ctx)
        if o in ("foreign_tenant_workspace", "context_tampered"):
            f = o
        elif c != "correct":
            f = c
        elif h == "hash_mismatch":
            f = "hash_mismatch"
        elif o != "valid" or (h != "valid" and hash_policy == "required"):
            f = "unverifiable"
        else:
            f = "correct"
        rows.append((f, c, o, h))
    for r in rows:
        if r[0] == "correct":
            return r
    return rows[0]


# ------------------------------------------------------------------ aliases
def alias_set_hash(labels: dict) -> str:
    items = []
    for aid in sorted(labels):
        for g in labels[aid].get("gold_rules", []):
            if g.get("subject_aliases") or g.get("value_aliases"):
                items.append([aid, g.get("id"), g.get("subject_aliases") or [], g.get("value_aliases") or []])
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ scoring one artifact in one run
_REASONS = ("scope", "scope_superset", "modality_observed", "exception_omitted", "merged_tiers", "wrong_unit",
            "comparator_missing", "evidence_not_correct")


def _empty_counts():
    d = {"T": 0, "found": 0, "partial": 0, "missed": 0, "missed_wrong_value": 0, "undecided": 0,
         "class_mismatch": 0, "normative_from_soft": 0, "normative_from_fact": 0, "spurious": 0}
    for r in _REASONS:
        d["partial_" + r] = 0
    return d


def _score_artifact(label, obs, pred, overrides, ctx_info=None, adj=None, hash_policy="reported_only"):
    ctx_state, ctx = ctx_info or ("missing", None)
    aid_ = label.get("artifact_id")
    golds_all = label.get("gold_rules", [])
    golds = [g for g in golds_all if g.get("status", "gold") == "gold"]
    excluded = [g for g in golds_all if g.get("status") == "excluded_ambiguous"]
    rec = {"verdicts": {}, "spurious": 0, "spurious_ids": [], "trap_hits": 0, "duplicates": 0,
           "soft_guidance_extracted": 0, "observed_fact_neutral": 0, "observed_reported": 0,
           "excluded_matched": 0, "unknown_rejected": 0, "malformed_candidates": [],
           "malformed_prediction": None, "failed": None, "candidates_seen": 0,
           "evidence": {k: 0 for k in EV_KEYS}, "evidence_content_row": {k: 0 for k in CONTENT_KEYS},
           "evidence_ownership_row": {k: 0 for k in OWNERSHIP_KEYS}, "evidence_hash_row": {k: 0 for k in HASH_KEYS},
           "normative_from_soft": 0, "normative_from_fact": 0,
           "unmatched_predictions": [], "undecided": [], "primary_class": label.get("primary_class"),
           "excluded_ambiguous": len(excluded), "passthrough": {}, "rejected_by_pipeline": 0}
    cands: list = []
    if not isinstance(pred, dict):
        rec["malformed_prediction"] = "prediction_missing" if pred is None else "prediction_not_object"
        rec["failed"] = rec["malformed_prediction"]
    else:
        rec["passthrough"] = {k: pred[k] for k in _PASS_KEYS if k in pred}
        rej = pred.get("rejected")
        rec["rejected_by_pipeline"] = len(rej) if isinstance(rej, list) else 0
        if pred.get("error") or str(pred.get("status", "")).lower() in _FAIL_STATUS or pred.get("model_mismatch"):
            rec["failed"] = str(pred.get("error") or pred.get("status") or "model_mismatch")
        elif not isinstance(pred.get("candidates"), list):
            rec["malformed_prediction"] = "candidates_not_list"
            rec["failed"] = "candidates_not_list"
        else:
            cands = pred["candidates"]
    valid = []
    for i, c in enumerate(cands):
        why = _validate(c)
        cid = (c.get("candidate_id") if isinstance(c, dict) and c.get("candidate_id") is not None else f"#{i}")
        if why:
            rec["malformed_candidates"].append({"candidate": cid, "reason": why})
        elif c.get("applicability") not in (None, "RECOGNIZED"):
            rec["unknown_rejected"] += 1
        else:
            valid.append((cid, c))
    rec["candidates_seen"] = len(cands)

    pairs, und_pairs = [], []
    for gi, g in enumerate(golds):
        for ci, (cid, c) in enumerate(valid):
            rank, reasons, how = _pair(g, c, overrides.get(cid) == g.get("id"), adj, aid_)
            if rank > 0:
                pairs.append((-rank, gi, ci, reasons, how))
            elif rank < 0:
                und_pairs.append((gi, ci))
    pairs.sort(key=lambda p: (p[0], p[1], p[2]))
    gold_done, cand_done, result = {}, {}, {}
    for npass in (1, 2):
        for negrank, gi, ci, reasons, how in pairs:
            rank = -negrank
            if (npass == 1 and rank == 1) or (npass == 2 and rank != 1):
                continue
            if gi in gold_done or ci in cand_done:
                continue
            gold_done[gi], cand_done[ci] = ci, gi
            result[gi] = (rank, reasons, how)
    und_gold = set()
    und_cand = set()
    for gi, ci in und_pairs:
        if gi in result or ci in cand_done:
            continue
        g, (cid, c) = golds[gi], valid[ci]
        und_gold.add(gi)
        und_cand.add(ci)
        rec["undecided"].append({"gold": g.get("id"), "candidate": cid,
                                 "pair_id": pair_id(aid_, g, c), "gold_text": {k: g.get(k) for k in ("subject", "predicate", "value", "unit", "scope")},
                                 "candidate_text": {k: c.get(k) for k in ("subject", "predicate", "value", "unit", "scope")}})
    for gi, g in enumerate(golds):
        entry = {"class": g.get("class"), "id": g.get("id")}
        if gi in result:
            rank, reasons, how = result[gi]
            cid, c = valid[gold_done[gi]]
            entry["candidate"] = cid
            entry["subject_basis"] = how
            if rank >= 2:
                cc = c.get("class", c.get("rule_class"))
                if cc is not None and cc != g.get("class"):
                    entry["class_mismatch"] = {"gold": g.get("class"), "reported": cc}
                ev, crow, orow, hrow = _evidence_verdict(c, g, obs, aid_, ctx_state, ctx, pred, hash_policy)
                entry["evidence"] = ev
                entry["evidence_rows"] = {"content": crow, "ownership": orow, "content_hash": hrow}
                rec["evidence_hash_row"][hrow] += 1
                rec["evidence_content_row"][crow] += 1
                rec["evidence_ownership_row"][orow] += 1
                rec["evidence"]["checked"] += 1
                rec["evidence"][ev] += 1
                if ev != "correct":
                    reasons = reasons + ["evidence_not_correct"]
                    rank = 2
            if rank == 3:
                entry["verdict"] = "FOUND_STRICT"
            elif rank == 2:
                entry["verdict"] = "FOUND_PARTIAL"
                entry["reasons"] = reasons
            else:
                entry["verdict"] = "MISSED"
                entry["missed_reason"] = "wrong_value"
        elif gi in und_gold:
            entry["verdict"] = "UNDECIDED"
        else:
            entry["verdict"] = "MISSED"
            entry["missed_reason"] = "absent"
        rec["verdicts"][g.get("id")] = entry

    soft = [_item_text(x) for x in label.get("soft_guidance", [])]
    obsf = [_item_text(x) for x in label.get("observed_facts", [])]
    traps = [_item_text(x) for x in label.get("traps", [])]

    def note(cid, c, cat):
        rec["unmatched_predictions"].append({"candidate": cid, "subject": c.get("subject"),
                                              "predicate": c.get("predicate"), "value": c.get("value"),
                                              "modality": c.get("modality"), "category": cat})

    for ci, (cid, c) in enumerate(valid):
        if ci in cand_done:
            continue
        normative = c.get("modality") in _NORMATIVE
        if any(gi in result and result[gi][0] >= 2 and
               _pair(golds[gi], c, overrides.get(cid) == golds[gi].get("id"), adj, aid_)[0] >= 2 for gi in range(len(golds))):
            rec["duplicates"] += 1
            note(cid, c, "duplicate")
            continue
        if ci in und_cand:
            note(cid, c, "undecided")
            continue
        if any(_pair(g, c, overrides.get(cid) == g.get("id"), adj, aid_)[0] >= 2 for g in excluded):
            rec["excluded_matched"] += 1
            note(cid, c, "excluded_matched")
            continue
        if any(_matches_item(t, c) for t in soft):
            if normative:
                rec["normative_from_soft"] += 1
                rec["spurious"] += 1
                rec["spurious_ids"].append(cid)
                note(cid, c, "normative_from_soft")
            else:
                rec["soft_guidance_extracted"] += 1
                note(cid, c, "soft_guidance_neutral")
            continue
        if any(_matches_item(t, c) for t in obsf):
            if normative:
                rec["normative_from_fact"] += 1
                rec["spurious"] += 1
                rec["spurious_ids"].append(cid)
                note(cid, c, "normative_from_fact")
            else:
                rec["observed_fact_neutral"] += 1
                note(cid, c, "observed_fact_neutral")
            continue
        if not normative:
            rec["observed_reported"] += 1
            note(cid, c, "observed_reported")
            continue
        rec["spurious"] += 1
        rec["spurious_ids"].append(cid)
        trap = any(_matches_item(t, c) for t in traps)
        if trap:
            rec["trap_hits"] += 1
        note(cid, c, "spurious_trap_hit" if trap else "spurious")
    return rec


# ------------------------------------------------------------------ aggregation
def _rates(c) -> dict:
    pred = c["found"] + c["partial"] + c["missed_wrong_value"] + c["spurious"]

    def d(n, m):
        return None if m == 0 else n / m
    return {"predictions_one_to_one": pred,
            "precision_strict": d(c["found"], pred), "precision_lenient": d(c["found"] + c["partial"], pred),
            "recall_strict": d(c["found"], c["T"]), "recall_lenient": d(c["found"] + c["partial"], c["T"])}


RATE_FORMULA = ("strict: found = content-correct (subject, value, unit, operator, scope, modality) AND evidence correct; "
                "lenient = found + partial. predictions_one_to_one = found + partial + wrong-value attributions + "
                "spurious (duplicates, neutral buckets, undecided, malformed excluded; each listed separately). "
                "precision_X = X / predictions_one_to_one; recall_X = X / T (T includes missed, undecided).")


def _finish(classes, overall_other_spurious=None):
    overall = _empty_counts()
    for c in classes.values():
        for k in overall:
            overall[k] += c[k]
    overall["wrong_value"] = overall["missed_wrong_value"]
    overall["wrong_scope"] = overall["partial_scope"] + overall["partial_scope_superset"]
    rates = {k: _rates(v) for k, v in classes.items()}
    rates["overall"] = _rates(overall)
    text = {k: f"{k}: {v['found']} of {v['T']}" for k, v in classes.items()}
    text["overall"] = f"overall: {overall['found']} of {overall['T']}"
    return overall, rates, text


def _summarise(artifact_recs: dict) -> dict:
    classes = {c: _empty_counts() for c in CLASSES}
    other = {"spurious": 0, "trap_hits": 0, "duplicates": 0, "soft_guidance_extracted": 0,
             "observed_fact_neutral": 0, "observed_reported": 0, "excluded_matched": 0, "unknown_rejected": 0,
             "malformed_candidates": 0, "malformed_predictions": 0, "failed_jobs": 0, "excluded_ambiguous": 0,
             "rejected_by_pipeline": 0, "candidates_seen": 0, "normative_from_soft": 0,
             "normative_from_fact": 0, "undecided_pairs": 0, "failed_artifacts": [], "artifacts_in_sample": 0}
    ev = {k: 0 for k in EV_KEYS}
    evc = {k: 0 for k in CONTENT_KEYS}
    evo = {k: 0 for k in OWNERSHIP_KEYS}
    evh = {k: 0 for k in HASH_KEYS}
    lat = cost = 0.0
    n_lat = n_cost = 0
    for aid, r in artifact_recs.items():
        for v in r["verdicts"].values():
            c = classes.setdefault(v["class"], _empty_counts())
            c["T"] += 1
            if v["verdict"] == "FOUND_STRICT":
                c["found"] += 1
            elif v["verdict"] == "FOUND_PARTIAL":
                c["partial"] += 1
                for rs in v["reasons"]:
                    c["partial_" + rs] += 1
            elif v["verdict"] == "UNDECIDED":
                c["undecided"] += 1
            else:
                c["missed"] += 1
                if v.get("missed_reason") == "wrong_value":
                    c["missed_wrong_value"] += 1
            if v.get("class_mismatch"):
                c["class_mismatch"] += 1
        for k in ("spurious", "trap_hits", "duplicates", "soft_guidance_extracted", "observed_fact_neutral",
                  "observed_reported", "excluded_matched", "unknown_rejected", "excluded_ambiguous",
                  "rejected_by_pipeline", "candidates_seen", "normative_from_soft", "normative_from_fact"):
            other[k] += r[k]
        other["undecided_pairs"] += len(r["undecided"])
        other["artifacts_in_sample"] += 1
        if r["failed"]:
            other["failed_artifacts"].append(aid)
        pc = classes.setdefault(r.get("primary_class") or "?", _empty_counts())
        for k in ("spurious", "normative_from_soft", "normative_from_fact"):
            pc[k] += r[k]
        other["malformed_candidates"] += len(r["malformed_candidates"])
        other["malformed_predictions"] += 1 if r["malformed_prediction"] else 0
        other["failed_jobs"] += 1 if r["failed"] else 0
        for k in ev:
            ev[k] += r["evidence"][k]
        for k in evc:
            evc[k] += r["evidence_content_row"][k]
        for k in evo:
            evo[k] += r["evidence_ownership_row"][k]
        for k in evh:
            evh[k] += r["evidence_hash_row"][k]
        p = r["passthrough"]
        for key in ("latency_s", "latency_ms"):
            if isinstance(p.get(key), (int, float)) and not isinstance(p.get(key), bool):
                lat += p[key] if key == "latency_s" else p[key] / 1000.0
                n_lat += 1
        for key in ("cost_usd", "cost"):
            if isinstance(p.get(key), (int, float)) and not isinstance(p.get(key), bool):
                cost += p[key]
                n_cost += 1
                break
    classes = {k: v for k, v in classes.items() if v["T"] or k in CLASSES or v["spurious"]}
    overall, rates, text = _finish(classes)
    return {"classes": classes, "overall": overall, "other": other, "evidence": ev, "evidence_content_row": evc,
            "evidence_ownership_row": evo, "evidence_hash_row": evh, "rates": rates,
            "latency_s_sum": lat if n_lat else None, "latency_jobs": n_lat,
            "cost_sum": cost if n_cost else None, "cost_jobs": n_cost, "text": text}


def _add(a, b):
    out = {}
    for k in a:
        if isinstance(a[k], dict):
            out[k] = _add(a[k], b[k])
        elif isinstance(a[k], (int, float)) and not isinstance(a[k], bool) and isinstance(b[k], (int, float)):
            out[k] = a[k] + b[k]
        elif isinstance(a[k], list):
            out[k] = a[k] + b[k]
        else:
            out[k] = a[k]
    return out


_ADJ_FIELDS = ("decision", "adjudicator", "basis", "timestamp")


def _check_adjudications(adj):
    for pid, e in (adj or {}).items():
        if not isinstance(e, dict) or e.get("decision") not in ("match", "no_match") \
                or any(not e.get(k) for k in _ADJ_FIELDS):
            raise ValueError(f"adjudication {pid!r} needs decision(match|no_match), adjudicator, basis, timestamp")
        if set(e) - set(_ADJ_FIELDS):
            raise ValueError(f"adjudication {pid!r} carries unexpected fields {sorted(set(e) - set(_ADJ_FIELDS))}")


def anonymised_pairs(result: dict) -> list[dict]:
    """Undecided pairs for blind adjudication: pair_id + artifact + both texts. No run, arm or candidate id."""
    seen = {}
    for run in result["runs"].values():
        for aid, rec in run["artifacts"].items():
            for u in rec["undecided"]:
                seen.setdefault(u["pair_id"], {"pair_id": u["pair_id"], "artifact_id": aid,
                                                "gold_text": u["gold_text"], "candidate_text": u["candidate_text"]})
    return [seen[k] for k in sorted(seen)]


def _core(labels, obs_cache, artifacts, runs, overrides, adj, run_contexts=None, trusted_hashes=None,
          hash_policy="reported_only"):
    out_runs, run_errors = {}, {}
    for run_id, preds in runs.items():
        if not isinstance(preds, dict):
            run_errors[run_id] = "run_missing" if preds is None else "run_not_object"
        recs = {}
        ctx = run_contexts.get(run_id) if isinstance(run_contexts, dict) else None
        ctx_info = (_ctx_state(ctx, (trusted_hashes or {}).get(run_id) if isinstance(trusted_hashes, dict) else None,
                               run_id), ctx)
        for aid, label in labels.items():  # denominator = label set, never the prediction set
            pred = preds.get(aid) if isinstance(preds, dict) else None
            recs[aid] = _score_artifact(label, obs_cache[aid], pred, overrides, ctx_info, adj, hash_policy)
        s = _summarise(recs)
        s["artifacts"] = recs
        s["run_error"] = run_errors.get(run_id)
        s["context_state"] = ctx_info[0]
        s["coverage"] = {"gold_total": s["overall"]["T"], "undecided": s["overall"]["undecided"],
                         "decided": s["overall"]["T"] - s["overall"]["undecided"]}
        s["unlabelled_prediction_artifacts"] = sorted(a for a in (preds or {}) if a not in labels) \
            if isinstance(preds, dict) else []
        out_runs[run_id] = s
    agg = None
    for s in out_runs.values():
        core = {k: s[k] for k in ("classes", "overall", "other", "evidence", "evidence_content_row",
                                  "evidence_ownership_row", "evidence_hash_row")}
        agg = core if agg is None else _add(agg, core)
    if agg is not None:
        agg["overall"], agg["rates"], agg["text"] = _finish(agg["classes"])
        agg["coverage"] = {"gold_total": agg["overall"]["T"], "undecided": agg["overall"]["undecided"],
                           "decided": agg["overall"]["T"] - agg["overall"]["undecided"]}
    return out_runs, agg, run_errors


FINAL_FORMULA = ("FINAL = the same strict/lenient formulas recomputed after applying the separate adjudication file: "
                 "decision match -> that pair is a subject match (value, unit, operator, scope, modality, evidence rules "
                 "still apply); no_match -> not a match. Pairs without an adjudication stay undecided and not-correct in "
                 "strict. PROVISIONAL (top-level runs/aggregate_over_runs) = no adjudication applied: undecided counted "
                 "as not-correct in strict and kept in T.")


def score(labels: dict, artifacts: dict, runs: dict, subject_overrides: dict | None = None,
          expected_runs: list | None = None, locked_alias_hash: str | None = None,
          adjudications: dict | None = None, run_contexts: dict | None = None,
          trusted_context_hashes: dict | None = None, content_hash_policy: str = "reported_only") -> dict:
    if content_hash_policy not in ("reported_only", "required"):
        raise ValueError("content_hash_policy must be reported_only or required")
    ahash = alias_set_hash(labels)
    has_alias = ahash != alias_set_hash({})
    if (has_alias and locked_alias_hash is None) or (locked_alias_hash is not None and locked_alias_hash != ahash):
        raise AliasLockError(f"alias set hash {ahash} does not equal the locked hash {locked_alias_hash!r}; refused")
    _check_adjudications(adjudications)
    overrides = dict(subject_overrides or {})
    obs_cache = {aid: build_observations(artifacts.get(aid, {"observations": []})) for aid in labels}
    runs = dict(runs)
    for rid in (expected_runs or []):
        if rid not in runs:
            runs[rid] = None
    out_runs, agg, run_errors = _core(labels, obs_cache, artifacts, runs, overrides, None, run_contexts, trusted_context_hashes,
                                       content_hash_policy)
    final = None
    if adjudications:
        f_runs, f_agg, _ = _core(labels, obs_cache, artifacts, runs, overrides, adjudications, run_contexts,
                             trusted_context_hashes, content_hash_policy)
        final = {"runs": {k: {x: v[x] for x in ("classes", "overall", "other", "evidence", "rates", "text", "coverage")}
                          for k, v in f_runs.items()}, "aggregate_over_runs": f_agg,
                 "adjudications_applied": adjudications, "formula": FINAL_FORMULA}
    return {"scorer_version": SCORER_VERSION, "n_artifacts": len(labels), "runs": out_runs,
            "aggregate_over_runs": agg, "metrics_status": "FINAL_AVAILABLE" if final else "PROVISIONAL_ONLY",
            "final": final, "provisional_formula": FINAL_FORMULA,
            "subject_overrides": overrides, "run_errors": run_errors,
            "content_hash_policy": content_hash_policy, "alias_set_sha256": ahash, "alias_lock_checked": locked_alias_hash is not None,
            "generic_words": sorted(GENERIC_WORDS), "formula": RATE_FORMULA,
            "notes": ["descriptive counts only; no threshold, no significance claim; on this label set",
                      "strict (`found`) is reported first; lenient = found + partial, never merged with strict",
                      "aggregate_over_runs is a SUM of per-run counts (rates recomputed from sums; no averaging)",
                      "undecided pairs are listed with both texts for blind adjudication; not strict, not missed",
                      "wrong_scope = partial_scope + partial_scope_superset"]}


def main(argv=None) -> int:
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(description="Pitch-scope scorer (descriptive)")
    ap.add_argument("--labels", required=True, help="dir of Axx.json label files")
    ap.add_argument("--artifacts", required=True, help="dir of artifact files (label `file` names them)")
    ap.add_argument("--runs", required=True, help="JSON {run_id: {artifact_id: prediction}}")
    ap.add_argument("--out", required=True)
    ap.add_argument("--overrides", help="optional JSON {candidate_id: gold_id} (adjudication, disclosed)")
    ap.add_argument("--adjudications", help="optional JSON {pair_id: {decision, adjudicator, basis, timestamp}}")
    ap.add_argument("--alias-hash", help="locked alias-set sha256 (required when labels carry aliases)")
    ap.add_argument("--run-contexts", help="runner-supplied TRUSTED JSON {run_id: context} (see make_run_context)")
    ap.add_argument("--context-hashes", help="optional runner-held JSON {run_id: sha256}")
    a = ap.parse_args(argv)
    labels, artifacts = {}, {}
    for f in sorted(Path(a.labels).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        labels[d["artifact_id"]] = d
    for aid, d in labels.items():
        p = Path(a.artifacts) / d["file"]
        artifacts[aid] = {"text": p.read_bytes(), "format": d.get("format") or p.suffix.lstrip("."),
                          "file": d["file"]}
    runs = json.loads(Path(a.runs).read_text(encoding="utf-8"))
    ov = json.loads(Path(a.overrides).read_text(encoding="utf-8")) if a.overrides else None
    Path(a.out).write_text(json.dumps(score(labels, artifacts, runs, ov, locked_alias_hash=a.alias_hash,
                                      adjudications=json.loads(Path(a.adjudications).read_text(encoding="utf-8")) if a.adjudications else None,
                                      run_contexts=json.loads(Path(a.run_contexts).read_text(encoding="utf-8")) if a.run_contexts else None,
                                      trusted_context_hashes=json.loads(Path(a.context_hashes).read_text(encoding="utf-8")) if a.context_hashes else None),
                                      indent=1, sort_keys=True, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
