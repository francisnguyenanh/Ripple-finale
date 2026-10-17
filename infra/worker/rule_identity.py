"""Shared deterministic normalisation of the identity-bearing fields
(subject, predicate, unit, scope, value/outcome text) used by BOTH the relation
linker and the findings generator.

Why: extraction output is free text produced independently per artifact, so
"Purchase Order" / "purchase_order " or {"applies_to": "All Orders"} /
{"Applies-To": "all  orders"} never compared equal under raw string equality.
Only presentation differences are folded. Precision-first decisions:
  * subject, predicate and scope (keys and values) are compared CASE-INSENSITIVELY;
  * UNIT and VALUE/OUTCOME strings are compared CASE-SENSITIVELY (only Unicode NFKC,
    whitespace, `_`/`-` between alphanumerics and trailing `.,;:` are folded), so
    "MB" != "Mb" and "Premium" != "premium". KNOWN RECALL LIMIT: case-only differences
    in units/values are not folded (a missed link/conflict, never a false one);
  * a sign or decimal point is significant: "-5" != "5", "-5%" != "5%", "+5" != "5",
    ".5" != "5" (a `-`/`_` is a separator ONLY between two alphanumerics, so
    "e-mail" == "e mail" but a leading minus is never dropped);
  * numbers are never rewritten ("500,000" != "500000"); no synonym/fuzzy matching;
  * any placeholder/unknown scope entry (also nested in a list or dict) makes the
    whole scope UNRESOLVED (None), so it can never be asserted equal to anything.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_PLACEHOLDERS = frozenset({
    "", "unknown", "unresolved", "n/a", "na", "none", "null", "not stated",
    "not specified", "unspecified", "tbd",
})


_INNER_SEP = re.compile(r"(?<=[^\W_])[_\-]+(?=[^\W_])")


def norm_text(value, fold_case: bool = True) -> str | None:
    """Canonical comparison form of a free-text field; None stays None."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    if fold_case:
        text = text.casefold()
    text = _INNER_SEP.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(" .,;:")


def unit_key(unit):
    """Case-SENSITIVE unit comparison form."""
    return norm_text(unit, fold_case=False)


def outcome_key(outcome):
    return norm_text(outcome, fold_case=False)


def _norm_scope_value(value):
    """Canonical form of one scope value, or raises _Unresolved."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict):
        out = _scope_items(value)
        if out is None:
            raise _Unresolved
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_norm_scope_value(v) for v in value]
        if not items:
            raise _Unresolved
        return tuple(sorted(items, key=repr))
    text = norm_text(value)
    if text is None or text in _PLACEHOLDERS:
        raise _Unresolved
    return text


class _Unresolved(Exception):
    pass


def _scope_items(scope):
    if not isinstance(scope, dict) or not scope:
        return None
    try:
        items = {}
        for key, value in scope.items():
            nkey = norm_text(key)
            if nkey in _PLACEHOLDERS:
                return None
            items[nkey] = _norm_scope_value(value)
    except _Unresolved:
        return None
    return tuple(sorted(items.items(), key=repr))


def scope_key(scope):
    """Hashable canonical scope, or None when the scope is empty, malformed,
    carries the extraction `{"unresolved": True}` marker, or contains any
    placeholder value (such a scope cannot assert identity)."""
    return _scope_items(scope)


def identity_key(candidate: dict) -> tuple:
    return (norm_text(candidate.get("subject")), norm_text(candidate.get("predicate")))


def value_key(value):
    """Strings are compared on normalised text; every other JSON scalar is
    compared as-is (no numeric reformatting)."""
    return norm_text(value, fold_case=False) if isinstance(value, str) else value

# Scope vocabulary. NO general reasoner: a scope relation is decided only from (a) text identity, (b) universal
# wording, (c) the CLOSED member table below. Anything else is "undecidable" (-> INSUFFICIENT_CONTEXT downstream).
#
# Universal: a dimension value is universal when it is a bare universal word, or "all|any|every" + ONE noun
# ("all orders"), or "<noun> of any|all <noun>". A universal phrase KEEPS its noun: "all customers" and "all vendors"
# are different universes (undecidable, never equivalent). "all premium customers" is restricted. An unresolved scope
# (scope_key None) is NOT universal.
_UNIVERSAL_WORDS = frozenset({"all", "any", "every", "everyone", "everything", "global", "universal", "unrestricted"})
_UNIVERSAL_PHRASE = re.compile(r"^(?:(?:all|any|every) (?P<n1>\w+)|(?P<n2>\w+) of (?:any|all) \w+)$")

# CLOSED member table (evidence-backed: wordings seen on the DEV oracle `tune` split; NOT validated on any held-out). A group holds
# MUTUALLY EXCLUSIVE members (so two different members of one group are provably disjoint). Nested members are
# deliberately absent (Japan/Tokyo are inside APAC: JP, Japan, Tokyo, "EU except Germany" stay unrecognised ->
# undecidable). Aliases are matched on the whole normalised atom only, so a list string such as "capex, opex" or a
# qualified wording is never recognised. Recall limit: any wording outside the table is undecidable, not conflicting.
_REGION_SFX = ("", "region", "market", "customers")
_SCOPE_GROUPS = {
    "region": {
        "us": (("us", "usa", "u.s", "u.s.a", "united states", "united states of america"), _REGION_SFX, ()),
        "eu": (("eu", "european union", "eu member states", "member states of the eu",
                "member states of the european union"), _REGION_SFX, ()),
        "apac": (("apac", "asia pacific"), _REGION_SFX, ()),
    },
    "tier": {
        w: ((w, w + " tier", w + " plan"), ("", "customers", "customer", "accounts", "account", "users"),
            (f"customers on the {w} plan", f"customers on the {w} tier", f"accounts on the {w} plan"))
        for w in ("standard", "premium")
    },
    "expense": {
        "capex": (("capex", "capital expenditure", "capital expense"), ("", "orders", "order", "spend"),
                  ("orders booked as capital expense", "orders booked as capital expenditure",
                   "orders booked as capex")),
        "opex": (("opex", "operating expense", "operating expenditure"), ("", "orders", "order", "spend"),
                 ("orders booked as operating expense", "orders booked as operating expenditure",
                  "orders booked as opex")),
    },
    "channel": {
        "online": (("online", "web shop", "website"), ("", "sales", "channel", "orders"),
                   ("orders placed on the website", "orders placed online", "orders placed on the web shop")),
        "retail": (("retail", "in store", "store", "stores", "shop"), ("", "sales", "channel", "orders", "stores"),
                   ("orders placed in a shop", "orders placed in a store", "orders placed in store")),
    },
}


def _build_alias_index() -> dict:
    index: dict = {}
    for group, members in _SCOPE_GROUPS.items():
        for member, (cores, suffixes, explicit) in members.items():
            phrases = [f"{c} {s}".strip() for c in cores for s in suffixes] + list(explicit)
            for ph in phrases:
                for full in (ph, f"the {ph}"):
                    key = norm_text(full)
                    if index.get(key, (group, member)) != (group, member):
                        raise AssertionError(f"scope alias collision: {key}")
                    index[key] = (group, member)
    return index


_ALIAS_INDEX = _build_alias_index()

# Nouns a restriction of each group can qualify. A universal phrase over ONE noun ("all vendors") is nested over a
# restriction only when that noun is one of these (so "all customers" is nested over {tier: premium}, but "all vendors" /
# "all employees" vs "premium customers" is UNDECIDABLE: a different noun/dimension). A restriction
# outside the closed table has no known noun -> undecidable too. A bare universal ("all", "any", "everything") has no noun.
_GROUP_NOUNS = {
    "region": frozenset({"customer", "account", "user", "order", "market"}),
    "tier": frozenset({"customer", "account", "user"}),
    "expense": frozenset({"order", "spend", "expense", "cost"}),
    "channel": frozenset({"order", "sale", "customer"}),
}


# Provenance dimensions are not applicability dimensions ("source: SOP" vs "source: code" is the SAME rule stated in two
# places -- the core SOP-vs-code conflict). Closed list; any other key is a business dimension.
_PROVENANCE_DIMS = frozenset({"source", "origin", "provenance", "document", "artifact", "file", "system"})


def _singular(noun: str) -> str:
    return noun[:-1] if len(noun) > 3 and noun.endswith("s") and not noun.endswith("ss") else noun


def _universal_noun(v):
    """None -> not universal; "" -> bare universal word; else the (singular) noun the universality is over."""
    if not isinstance(v, str):
        return None
    if v in _UNIVERSAL_WORDS:
        return ""
    m = _UNIVERSAL_PHRASE.match(v)
    return _singular(m.group("n1") or m.group("n2")) if m else None


def _token(atom: str):
    hit = _ALIAS_INDEX.get(atom)
    return ("m",) + hit if hit else ("t", atom)


def _model(scope):
    """-> None (unresolved) | (constraints {dim: frozenset(tokens)}, universes {dim: frozenset(nouns)}); raises
    ValueError for a nested value (undecidable). Universal-valued dimensions are not constraints."""
    key = scope_key(scope)
    if key is None:
        return None
    cons, univ = {}, {}
    for dim, val in key:
        if dim in _PROVENANCE_DIMS:
            continue  # where a statement was found is evidence, not applicability: never narrows or separates a scope
        if isinstance(val, str):
            atoms = (val,)
        elif isinstance(val, tuple) and all(isinstance(x, str) for x in val):
            atoms = val
        else:
            raise ValueError("nested scope value")
        nouns = [_universal_noun(x) for x in atoms]
        if all(n is not None for n in nouns):
            univ[dim] = frozenset(n for n in nouns if n)
            continue
        cons[dim] = frozenset(_token(x) for x in atoms)
    return cons, univ


def scope_is_universal(scope) -> bool:
    try:
        m = _model(scope)
    except ValueError:
        return False
    return m is not None and m[0] == {}


def _dim_relation(x: frozenset, y: frozenset) -> str:
    """eq | sub (x inside y) | sup | overlap | disjoint | undecidable, for the token sets of ONE dimension.
    disjoint / sub / sup / overlap need POSITIVE evidence: every token a closed-table member of ONE group. Differing
    free text is never evidence of disjointness."""
    if x == y:
        return "eq"
    groups = {t[1] for t in x | y if t[0] == "m"}
    if any(t[0] != "m" for t in x | y) or len(groups) != 1:
        return "undecidable"
    if not (x & y):
        return "disjoint"
    if x < y:
        return "sub"
    if y < x:
        return "sup"
    return "overlap"


def scope_relation(a, b) -> str:
    """Relation of two scopes decided ONLY from text identity, universal wording and the closed member table:
    equivalent | nested (one strictly contains the other, incl. universal vs restricted) | overlapping |
    disjoint (positive evidence: distinct members of one closed group) | unknown (either scope unresolved) |
    undecidable (anything else: free-text differences, different dimensions/groups, different universes, nested values)."""
    try:
        ma, mb = _model(a), _model(b)
    except ValueError:
        return "undecidable"
    if ma is None or mb is None:
        return "unknown"
    (ca, ua), (cb, ub) = ma, mb
    rels = {d: _dim_relation(ca[d], cb[d]) for d in ca.keys() & cb.keys()}
    if "disjoint" in rels.values():
        return "disjoint"
    if "undecidable" in rels.values():
        return "undecidable"
    for d in ua.keys() & ub.keys():
        if ua[d] and ub[d] and ua[d] != ub[d]:
            return "undecidable"  # "all customers" vs "all vendors": different universes, the noun is kept
    for (cx, ux), cy in (((ca, ua), cb), ((cb, ub), ca)):
        nouns = frozenset().union(*ux.values())
        for d in cy.keys() - cx.keys():
            if nouns and any(t[0] != "m" or not nouns <= _GROUP_NOUNS[t[1]] for t in cy[d]):
                return "undecidable"  # universal over one noun vs a restriction about another noun / unknown noun
    a_in_b = all(d in ca and rels[d] in ("eq", "sub") for d in cb)
    b_in_a = all(d in cb and rels[d] in ("eq", "sup") for d in ca)
    if a_in_b and b_in_a:
        return "equivalent"
    if a_in_b or b_in_a:
        return "nested"
    return "overlapping" if set(ca) == set(cb) else "undecidable"


# ---- value contract ----
_OP_VALUE = re.compile(r"^(>=|<=|\u2265|\u2264|>|<|=)\s*(-?\d+(?:\.\d+)?)$")
_NUM = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CURRENCIES = frozenset("USD EUR JPY GBP VND CNY KRW AUD CAD CHF SGD INR THB IDR MYR PHP HKD TWD NZD".split())
_CUR_SYMBOLS = {"$": "USD", "US$": "USD", "\u20ac": "EUR", "\u00a3": "GBP", "\u20ab": "VND", "\u20a9": "KRW",
                "\u20b9": "INR"}  # "\u00a5" (yen/yuan) is ambiguous -> deliberately absent
_DURATIONS = frozenset("second minute hour day week month year".split())
_PCT_UNITS = frozenset({"%", "percent", "percentage", "pct"})
_MONEY_PRE = re.compile(rf"^(?P<c>US\$|[$\u20ac\u00a3\u20ab\u20a9\u20b9]|[A-Z]{{3}}) ?(?P<n>{_NUM})$")
_MONEY_SUF = re.compile(rf"^(?P<n>{_NUM}) ?(?P<c>[A-Z]{{3}}|[\u20ac\u00a3\u20ab\u20a9\u20b9])$")
_DUR = re.compile(rf"^(?P<n>{_NUM}) ?(?P<u>[a-z]+)$")
_PCT = re.compile(rf"^(?P<n>{_NUM}) ?%$")
_PLAIN = re.compile(rf"^{_NUM}$", re.ASCII)
_MAX_BITS = 140   # > 40 decimal digits; the int itself is the payload so equal huge ints still agree
_NON_FINITE = frozenset({"nan", "inf", "infinity"})
_MAX_DIGITS = 40


def _dec(text: str):
    """Decimal of a strict number string, or None (leading zeros, too many digits)."""
    t = text.replace(",", "")
    digits = t.lstrip("+-").split(".")[0]
    if len(t) > _MAX_DIGITS or (len(digits) > 1 and digits.startswith("0")):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def _canon_unit(u):
    """Canonical comparable unit, or None for no unit. Currency codes/symbols, duration words (singular/plural folding
    only) and percent are folded; any other unit stays its case-sensitive normalised text."""
    if u is None:
        return None
    t = unicodedata.normalize("NFKC", str(u)).strip()
    if not t:
        return None
    if t in _CURRENCIES:
        return ("cur", t)
    if t in _CUR_SYMBOLS:
        return ("cur", _CUR_SYMBOLS[t])
    low = t.casefold()
    if low in _PCT_UNITS:
        return ("pct",)
    if low in _DURATIONS or (low.endswith("s") and low[:-1] in _DURATIONS):
        return ("dur", low.rstrip("s") if low.endswith("s") and low[:-1] in _DURATIONS else low)
    return ("u", unit_key(t))


def _parse_value(value, field_unit):
    """-> (kind, unit, magnitude_or_payload). kinds: num qty op text raw amb."""
    fu = _canon_unit(field_unit)
    if isinstance(value, bool) or value is None or isinstance(value, (list, tuple, dict, set, frozenset)):
        return ("raw", None, repr(value))
    if isinstance(value, int) and value.bit_length() > _MAX_BITS:
        return ("amb", None, value)    # checked BEFORE str()/repr(): those raise ValueError past 4300 digits
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ("amb", None, repr(value))
        d = _dec(format(Decimal(repr(value)), "f")) if not isinstance(value, int) else _dec(str(value))
        return ("amb", None, repr(value)) if d is None else ("num", fu, d)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.casefold().lstrip("+-") in _NON_FINITE:
        return ("amb", None, text)
    if not re.search(r"\d", text):
        return ("text", None, norm_text(value, fold_case=False))   # digit-free text: exact normalised text, as before
    m = _OP_VALUE.match(text)
    if m:
        d = _dec(m.group(2))
        return ("amb", None, text) if d is None else ("op", (m.group(1), fu), d)
    if _PLAIN.match(text):
        d = _dec(text)
        return ("amb", None, text) if d is None else ("num", fu, d)
    for rx, kind in ((_MONEY_PRE, "cur"), (_MONEY_SUF, "cur"), (_PCT, "pct"), (_DUR, "dur")):
        m = rx.match(text)
        if not m:
            continue
        d = _dec(m.group("n"))
        if d is None:
            return ("amb", None, text)
        if kind == "cur":
            code = _CUR_SYMBOLS.get(m.group("c"), m.group("c"))
            if code not in _CURRENCIES:
                return ("amb", None, text)
            emb = ("cur", code)
        elif kind == "pct":
            emb = ("pct",)
        else:
            emb = _canon_unit(m.group("u"))
            if emb is None or emb[0] != "dur":
                return ("amb", None, text)
        if fu is not None and fu != emb:
            return ("amb", None, text)    # embedded unit contradicts the unit field
        return ("qty", emb, d)
    return ("amb", None, text)


def value_comparison_detail(a, b, unit_a=None, unit_b=None) -> tuple[str, str]:
    """-> (same | differ | undecidable, reason). Typed comparison, no conversion; see the module's value contract.
    reason in {"", "operator_not_comparable", "value_not_comparable"}."""
    ka, ua, pa = _parse_value(a, unit_a)
    kb, ub, pb = _parse_value(b, unit_b)
    if "amb" in (ka, kb):
        return ("same", "") if ka == kb and pa == pb else ("undecidable", "value_not_comparable")
    if ka == "op" or kb == "op":
        if ka == kb and ua == ub:
            return ("same", "") if pa == pb else ("differ", "")
        return "undecidable", "operator_not_comparable"
    if ka != kb and not (ka in ("num", "qty") and kb in ("num", "qty")):
        return "undecidable", "value_not_comparable"
    if ka in ("text", "raw"):
        if pa == pb:
            return "same", ""
        # Exclusivity gate: different text / raw values never prove that they exclude each other
        # (different names, or members of one scope-vocabulary group, are NOT evidence about a predicate's values: the
        # closed scope table is evidence about scope only). The ONLY exclusivity-by-type route is two booleans. Everything else is INSUFFICIENT_CONTEXT, never a CONFLICT on inequality alone.
        if ka == "raw" and pa in ("True", "False") and pb in ("True", "False"):
            return "differ", ""
        return "undecidable", "value_exclusivity_not_established"
    if ua != ub:                       # different unit/currency, or a unit on one side only
        return "undecidable", "value_not_comparable"
    return ("same", "") if pa == pb else ("differ", "")


def value_comparison(a, b, unit_a=None, unit_b=None) -> str:
    return value_comparison_detail(a, b, unit_a, unit_b)[0]
