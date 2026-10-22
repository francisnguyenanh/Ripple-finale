"""From extraction candidates to OBSERVED business rules with traceable links between source files (fkl/1; journey finding JOURNEY-V1-J1, 2026-10-21).

Pure and deterministic; no model, no I/O. The caller (`infra.adapters.v2_business.derive_from_head`) passes the candidates and the structured values of ONE
source revision. Nothing here confirms anything: every rule stays OBSERVED and is accepted, or not, by a person.

1. Classification. A candidate becomes a rule only when it says who acts or what happens (actor or outcome):
   - a structured value (YAML key, sheet cell) with neither is a SUPPORTING FACT, kept as evidence and as a link target, not a rule;
   - a statement with neither actor nor outcome is INCOMPLETE, listed with what is missing, not a rule that raises three questions.
2. Code conditions. When the model's condition is missing or outside the grammar and the candidate's evidence quotes a code `if` line, that verbatim line is
   parsed with `ast` (never evaluated): and / or / not of comparisons between a dotted name and a literal. Calls and anything else are not interpreted.
3. References. A comparison against an attribute (`config.capex_finance_threshold_jpy`) is resolved to a structured value of the same revision whose key path
   ends with exactly that name, when there is exactly one such numeric value. Not found, several, or not a number: the comparison stays unresolved and the
   caller asks a person (`uncertain_link`). Each resolution is a `source_link` provenance entry and adds the value's binding (`path#key_path`).
4. Enum members. A code literal `CategoryType.CAPEX` is compared as `CAPEX` (recorded as a `source_link` of method `enum_member`).
5. Actors. A rule whose outcome is a code value (`RouteType.NEEDS_FINANCE`) and that names no actor takes the actor of the one rule of the same revision that
   states who acts on exactly that value (its condition is that value, its actor is named). Several different actors, or none: the actor stays unknown.
"""
from __future__ import annotations

import ast
import copy
import json
import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_knowledge as bk

STRUCTURED_LOCATORS = frozenset({"key_path", "cell"})
CODE_LOCATORS = frozenset({"code_span"})
MAX_LISTED = 50
_ENUM_MEMBER = re.compile(r"[A-Z][A-Za-z0-9]*\.([A-Z][A-Z0-9_]*)\Z")
_DOTTED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_ORDER_OPS = frozenset({"gt", "gte", "lt", "lte"})
_AST_OPS = {ast.Eq: "eq", ast.NotEq: "ne", ast.Gt: "gt", ast.GtE: "gte", ast.Lt: "lt", ast.LtE: "lte"}


# ---- locators -----------------------------------------------------------------------------------------------------------------------------------------------

def locator(evidence: Mapping) -> Optional[dict]:
    loc = evidence.get("locator")
    if isinstance(loc, Mapping):
        return dict(loc)
    if isinstance(loc, str) and loc.startswith("{"):
        try:
            out = json.loads(loc)
        except ValueError:
            return None
        return out if isinstance(out, dict) else None
    return None


def label_of(loc: Optional[Mapping]) -> Optional[str]:
    if not isinstance(loc, Mapping):
        return None
    if loc.get("type") == "key_path" and isinstance(loc.get("key_path"), str) and loc["key_path"]:
        return loc["key_path"]
    if isinstance(loc.get("sheet"), str) and isinstance(loc.get("cell"), str):
        return f"{loc['sheet']}!{loc['cell']}"
    return None


def structured_ref(loc: Optional[Mapping], logical_path: Optional[str]) -> Optional[str]:
    """The canonical binding of a structured value, `path#label` (what the read-back of a refresh and the draft resolve)."""
    label = label_of(loc)
    return f"{logical_path}#{label}" if label and logical_path else None


def structured_values(observations: Iterable[Mapping], artifact_paths: Mapping[str, str]) -> list:
    """Scalar values of the structured sources of one revision: [{artifact_id, path, label, value, ref}]."""
    out = []
    for o in observations:
        loc = o.get("locator") if isinstance(o.get("locator"), Mapping) else locator({"locator": o.get("locator")})
        label = label_of(loc) if loc and (loc.get("type") in STRUCTURED_LOCATORS or "sheet" in loc) else None
        raw = o.get("raw_value")
        if label is None or isinstance(raw, (dict, list)) or raw is None:
            continue
        path = artifact_paths.get(o.get("artifact_id")) or o.get("artifact_id")
        out.append({"artifact_id": o.get("artifact_id"), "path": path, "label": label, "value": raw, "ref": f"{path}#{label}"})
    return out


# ---- classification -----------------------------------------------------------------------------------------------------------------------------------------

def _evidence_kinds(cand: Mapping) -> set:
    return {(locator(e) or {}).get("type") for e in cand.get("evidence") or [] if isinstance(e, Mapping)}


def classify(cand: Mapping) -> tuple:
    """-> ("rule" | "supporting_fact" | "incomplete", missing fields)."""
    has_actor = isinstance(cand.get("actor"), str) and bool(cand["actor"].strip())
    has_outcome = isinstance(cand.get("outcome"), str) and bool(cand["outcome"].strip())
    if has_actor or has_outcome:
        return "rule", []
    kinds = _evidence_kinds(cand)
    if kinds and kinds <= STRUCTURED_LOCATORS:
        return "supporting_fact", ["actor", "outcome"]
    return "incomplete", ["actor", "outcome"]


# ---- code conditions --------------------------------------------------------------------------------------------------------------------------------------------

class _Unsupported(Exception):
    pass


def _dotted(node) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _operand(node):
    """-> ("literal", value) | ("enum", member, original) | ("ref", dotted name)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str, bool)) and not (isinstance(node.value, float)):
        return ("literal", node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, int):
        return ("literal", -node.operand.value)
    name = _dotted(node)
    if name is None:
        raise _Unsupported("operand")
    m = _ENUM_MEMBER.fullmatch(name)
    if m:
        return ("enum", m.group(1), name)
    return ("ref", name)


def _tree(node):
    if isinstance(node, ast.BoolOp):
        key = "all" if isinstance(node.op, ast.And) else "any"
        return {key: [_tree(v) for v in node.values]}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {"not": _tree(node.operand)}
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _AST_OPS:
        field = _dotted(node.left)
        if field is None:
            raise _Unsupported("left side is not a name")
        field = field.lower()
        if not bk._FIELD_RE.fullmatch(field):
            raise _Unsupported("field")
        return {"field": field, "op": _AST_OPS[type(node.ops[0])], "operand": _operand(node.comparators[0])}
    raise _Unsupported(type(node).__name__)


def code_condition(quote: str) -> Optional[dict]:
    """The condition tree of one verbatim code `if` / `elif` line, operands unresolved; None when the line is not a supported comparison expression."""
    text = " ".join(str(quote or "").split())
    m = re.fullmatch(r"(?:if|elif)\s+(.+?):?", text)
    if not m:
        return None
    try:
        expr = ast.parse(m.group(1), mode="eval").body
        return _tree(expr)
    except (SyntaxError, _Unsupported, ValueError, RecursionError):
        return None


def model_condition_tree(raw) -> Optional[dict]:
    """The model's own condition with attribute references marked (a value of an order comparison that is a dotted name, not a number); None if not a tree."""
    if not isinstance(raw, Mapping):
        return None
    try:
        if len(raw) == 1 and next(iter(raw)) in ("all", "any"):
            key = next(iter(raw))
            children = [model_condition_tree(c) for c in raw[key]]
            return None if not children or any(c is None for c in children) else {key: children}
        if len(raw) == 1 and "not" in raw:
            inner = model_condition_tree(raw["not"])
            return None if inner is None else {"not": inner}
        field, op, value = raw.get("field"), raw.get("op"), raw.get("value")
        if not isinstance(field, str) or not isinstance(op, str):
            return None
        if isinstance(value, str) and op in _ORDER_OPS and _DOTTED.fullmatch(value) and not re.fullmatch(r"-?\d+(\.\d+)?", value):
            operand = ("ref", value)
        elif isinstance(value, str) and _ENUM_MEMBER.fullmatch(value):
            operand = ("enum", _ENUM_MEMBER.fullmatch(value).group(1), value)
        else:
            operand = ("literal", value)
        leaf = {"field": field, "op": op, "operand": operand}
        if isinstance(raw.get("unit"), str):
            leaf["unit"] = raw["unit"]
        return leaf
    except (TypeError, AttributeError):
        return None


# ---- resolution ---------------------------------------------------------------------------------------------------------------------------------------------

def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


_KEY_CURRENCY = re.compile(r"_(jpy|usd|eur|gbp)\Z", re.I)


def resolve(tree: Mapping, values: list, *, from_ref: str, unit: Optional[str] = None) -> dict:
    """-> {"condition": grammar condition or None, "links": [source_link provenance], "unresolved": [{ref, reason, candidates}]}.
    A resolved value takes its unit from the currency suffix of its key name (`..._jpy`); otherwise, when the tree has exactly one order comparison, the
    candidate's own `unit` (what the model stated). No other unit is assumed."""
    links, unresolved = [], []

    def walk(node):
        if "all" in node or "any" in node:
            key = "all" if "all" in node else "any"
            return {key: [walk(c) for c in node[key]]}
        if "not" in node:
            return {"not": walk(node["not"])}
        kind = node["operand"][0]
        leaf = {"field": node["field"], "op": node["op"]}
        if kind == "literal":
            leaf["value"] = node["operand"][1]
        elif kind == "enum":
            leaf["value"] = node["operand"][1]
            links.append({"kind": "source_link", "method": "enum_member", "from_ref": from_ref, "to_ref": node["operand"][2], "value": node["operand"][1]})
        else:
            name = node["operand"][1]
            attr = name.rsplit(".", 1)[-1]
            hits = [v for v in values if v["label"].rsplit(".", 1)[-1] == attr]
            nums = [v for v in hits if _numeric(v["value"]) is not None]
            if len(hits) == 1 and len(nums) == 1:
                leaf["value"] = _numeric(nums[0]["value"])
                link = {"kind": "source_link", "method": "attribute_name", "from_ref": from_ref, "to_ref": nums[0]["ref"], "reference": name,
                        "artifact_id": nums[0]["artifact_id"], "value": leaf["value"]}
                m = _KEY_CURRENCY.search(nums[0]["label"])
                if m and not node.get("unit"):
                    leaf["unit"], link["unit_from"] = m.group(1).upper(), "key_name"
                links.append(link)
            else:
                reason = "not_found" if not hits else ("several_values" if len(hits) > 1 else "not_a_number")
                unresolved.append({"reference": name, "reason": reason, "candidates": sorted(v["ref"] for v in hits)[:5], "field": node["field"]})
                leaf["value"] = None
        if node.get("unit"):
            leaf["unit"] = node["unit"]
        return leaf

    cond = walk(copy.deepcopy(dict(tree)))
    order = [leaf for leaf in _leaves(cond) if leaf.get("op") in _ORDER_OPS]
    if unit and len(order) == 1 and not order[0].get("unit") and order[0].get("value") is not None:
        order[0]["unit"] = unit
    if unresolved:
        return {"condition": None, "links": links, "unresolved": unresolved}
    try:
        bk.validate_condition(cond)
    except bk.ContractError:
        return {"condition": None, "links": links, "unresolved": [{"reference": None, "reason": "outside_grammar", "candidates": [], "field": None}]}
    return {"condition": cond, "links": links, "unresolved": []}


def _leaves(cond) -> list:
    if not isinstance(cond, Mapping):
        return []
    if "all" in cond or "any" in cond:
        return [x for c in cond.get("all") or cond.get("any") or [] for x in _leaves(c)]
    if "not" in cond:
        return _leaves(cond["not"])
    return [cond]



# ---- code facts: branches of the code itself (exact, from the AST; never evaluated) ----------------------------------------------------------------------------

_ROLE_TARGET = re.compile(r"role", re.I)


def _outcome_of(stmt):
    """The outcome one branch statement states: ("enum", member, original) for a returned / assigned enum member, ("ref", dotted) for a returned configuration
    reference, ("assign", target, member, original) for `target = Enum.MEMBER`; None for anything else (a call with no enum argument, a nested if, ...)."""
    if isinstance(stmt, ast.Return) and stmt.value is not None:
        values = [stmt.value]
        if isinstance(stmt.value, ast.Call):
            values = list(stmt.value.args) + [k.value for k in stmt.value.keywords]
        for v in values:
            name = _dotted(v)
            if name is None:
                continue
            m = _ENUM_MEMBER.fullmatch(name)
            if m:
                return ("enum", m.group(1), name)
        for v in values:                                                      # a configured route (`config.high_risk_route`) is a reference to resolve
            name = _dotted(v)
            if name and "." in name and not _ENUM_MEMBER.fullmatch(name) and name.split(".")[0] not in ("request", "self"):
                return ("ref", name)
        return None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        name = _dotted(stmt.value)
        m = _ENUM_MEMBER.fullmatch(name or "")
        if m:
            return ("assign", stmt.targets[0].id, m.group(1), name)
    return None


def _role_of(outcome, qualname) -> Optional[str]:
    """The role a branch states for its decision value: `name = Role.MEMBER` with "role" in the name, or `return Role.MEMBER` inside a function whose name says role."""
    if not outcome:
        return None
    if outcome[0] == "assign" and _ROLE_TARGET.search(outcome[1]):
        return "role-" + bk._slug(outcome[2])
    if outcome[0] == "enum" and _ROLE_TARGET.search(outcome[2].split(".")[0]) and _ROLE_TARGET.search(str(qualname or "")):
        return "role-" + bk._slug(outcome[1])
    return None


def code_facts(observations: Iterable[Mapping], artifact_paths: Mapping[str, str]) -> dict:
    """Branches of every code block of one revision: {"branches": [{artifact_id, path, qualname, line, line_no, tree, outcome, outcome_line, from_ref}],
    "role_mappings": {VALUE: {actor_ref: [evidence_ref]}}}. A role mapping is a branch `x == Enum.VALUE` whose body assigns `Role.MEMBER` to a name containing
    "role". Unparsable text and unsupported statements are skipped; nothing is executed."""
    import textwrap
    branches, mappings = [], {}
    for o in observations:
        loc = o.get("locator") if isinstance(o.get("locator"), Mapping) else locator({"locator": o.get("locator")})
        raw = o.get("raw_value")
        if not loc or loc.get("type") not in CODE_LOCATORS or not isinstance(raw, str):
            continue
        try:
            tree = ast.parse(textwrap.dedent(raw))
        except (SyntaxError, ValueError, RecursionError):
            continue
        lines = textwrap.dedent(raw).splitlines()
        start = int(loc.get("line_start") or 1)
        path = artifact_paths.get(o.get("artifact_id")) or o.get("artifact_id")
        span = json.dumps({k: loc[k] for k in sorted(loc) if k in ("line_end", "line_start", "qualname", "type")}, sort_keys=True)
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            try:
                cond = _tree(node.test)
            except _Unsupported:
                continue
            outcome, out_line = None, None
            for stmt in node.body:
                outcome = _outcome_of(stmt)
                if outcome is not None:
                    out_line = start + stmt.lineno - 1
                    break
                if isinstance(stmt, (ast.If, ast.Return, ast.Raise)):
                    break
            line = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
            b = {"artifact_id": o.get("artifact_id"), "path": path, "qualname": loc.get("qualname"), "line": line, "line_no": start + node.lineno - 1, "tree": cond,
                 "outcome": outcome, "outcome_line": out_line, "from_ref": f"{path}#{span}"}
            branches.append(b)
            actor = _role_of(outcome, loc.get("qualname"))
            if actor and "field" in cond and cond["operand"][0] == "enum":
                mappings.setdefault(cond["operand"][1], {}).setdefault(actor, []).append(f"{path}#{loc.get('qualname')}:{out_line}")
    return {"branches": branches, "role_mappings": mappings}


def _branch_for(cand: Mapping, quote: Optional[str], facts: Optional[Mapping]):
    if not facts or not quote:
        return None
    line = quote.strip()
    for b in facts["branches"]:
        if b["artifact_id"] == cand.get("artifact_revision_id") and b["line"] == line:
            return b
    return None


def _apply_branch(rule: dict, cand: Mapping, branch: Mapping, structured: list, links: list) -> None:
    """Outcome (and a role actor) from the branch the candidate quotes, when the model gave none; every fill is a traced link."""
    out = branch.get("outcome")
    if out is None:
        return
    kind = str((rule.get("outcome") or {}).get("kind") or "")
    to_ref = f"{branch['path']}#{branch['qualname']}:{branch['outcome_line']}"
    stated = out[2] if out[0] == "enum" else (out[3] if out[0] == "assign" else out[1])
    if kind in ("", "unspecified", "unknown") or (stated and stated in kind and kind != stated and out[0] != "assign"):   # the model's text of the branch (a copied return line): the value it names
        rule["outcome"] = {"kind": "unspecified", "predicate": rule["outcome"].get("predicate", ""), "stated_text": kind} if kind not in ("", "unspecified", "unknown") else rule["outcome"]
        kind = "unspecified"
    if kind in ("", "unspecified", "unknown"):
        if out[0] == "enum":
            value = out[2]
        elif out[0] == "assign":
            value = f"{out[1]} = {out[3]}"
        else:                                                                  # ("ref", config.x): the configured value of this revision, exactly one key
            attr = out[1].rsplit(".", 1)[-1]
            hits = [v for v in structured if v["label"].rsplit(".", 1)[-1] == attr]
            if len(hits) != 1:
                return
            value = str(hits[0]["value"])
            links.append({"kind": "source_link", "method": "attribute_name", "from_ref": branch["from_ref"], "to_ref": hits[0]["ref"], "reference": out[1],
                          "artifact_id": hits[0]["artifact_id"], "value": value})
        rule["outcome"] = {"kind": value, "predicate": rule["outcome"].get("predicate", "")}
        links.append({"kind": "source_link", "method": "branch_outcome", "from_ref": branch["from_ref"], "to_ref": to_ref, "value": value})
    actor = _role_of(out, branch.get("qualname"))
    if actor and not rule.get("actor_ref"):
        rule["actor_ref"] = actor
        rule["unknown_fields"] = [f for f in rule.get("unknown_fields") or [] if f != "actor_ref"]
        links.append({"kind": "source_link", "method": "branch_actor", "from_ref": branch["from_ref"], "to_ref": to_ref, "value": rule["actor_ref"]})


# ---- rules from candidates -----------------------------------------------------------------------------------------------------------------------------------

def _first_code_quote(cand: Mapping) -> tuple:
    """-> (the first verbatim `if` / `elif` line of the code evidence or None, the locator of the code evidence or None)."""
    first_ref = None
    for e in cand.get("evidence") or []:
        loc = locator(e) or {}
        if loc.get("type") in CODE_LOCATORS:
            ref = e.get("locator") if isinstance(e.get("locator"), str) else json.dumps(loc, sort_keys=True)
            first_ref = first_ref or ref
            for line in str(e.get("quote") or "").splitlines() or [str(e.get("quote") or "")]:
                if re.match(r"\s*(if|elif)\s", line):
                    return line.strip(), ref
    return None, first_ref


def _normalize_bindings(rule: dict, cand: Mapping, artifact_paths: Mapping[str, str]) -> None:
    path = artifact_paths.get(cand.get("artifact_revision_id"))
    for item in rule["source_bindings"] + [p for p in rule["provenance"] if p.get("kind") == "source_evidence"]:
        ref = structured_ref(locator({"locator": item.get("evidence_ref")}), path)
        if ref:
            item["evidence_ref"] = ref


def _enum_normalized(cond, from_ref: str, links: list):
    if isinstance(cond, Mapping):
        if "all" in cond or "any" in cond:
            key = "all" if "all" in cond else "any"
            return {key: [_enum_normalized(c, from_ref, links) for c in cond[key]]}
        if "not" in cond:
            return {"not": _enum_normalized(cond["not"], from_ref, links)}
        v = cond.get("value")
        if isinstance(v, str) and _ENUM_MEMBER.fullmatch(v):
            links.append({"kind": "source_link", "method": "enum_member", "from_ref": from_ref, "to_ref": v, "value": _ENUM_MEMBER.fullmatch(v).group(1)})
            return dict(cond, value=_ENUM_MEMBER.fullmatch(v).group(1))
    return cond


def _outcome_token(rule: Mapping) -> Optional[str]:
    kind = str((rule.get("outcome") or {}).get("kind") or "")
    m = _ENUM_MEMBER.fullmatch(kind)
    return m.group(1) if m else None


def _single_eq_value(cond) -> Optional[str]:
    if isinstance(cond, Mapping) and cond.get("op") == "eq" and isinstance(cond.get("value"), str):
        v = cond["value"]
        m = _ENUM_MEMBER.fullmatch(v)
        return m.group(1) if m else v
    if isinstance(cond, Mapping) and isinstance(cond.get("all"), list) and len(cond["all"]) == 1:
        return _single_eq_value(cond["all"][0])
    return None


def link_actors(rules: list, role_mappings: Optional[Mapping] = None) -> list:
    """Fill an unknown actor from the one place that states who acts on exactly this outcome value: a code branch that assigns a role for it (`role_mappings`
    from code_facts) or a rule whose condition is that value and that names an actor; -> unresolved [{rule_id, value, actors}]."""
    who: dict = {}
    for v, actors in (role_mappings or {}).items():
        for actor, refs in actors.items():
            who.setdefault(v, {}).setdefault(actor, []).append({"rule_id": refs[0], "from_code": True})
    for r in rules:
        v = _single_eq_value(r.get("condition"))
        if v and r.get("actor_ref"):
            who.setdefault(v, {}).setdefault(r["actor_ref"], []).append(r)
    unresolved = []
    for r in rules:
        token = _outcome_token(r)
        if r.get("actor_ref") or not token or token not in who:
            continue
        actors = who[token]
        if len(actors) != 1:
            unresolved.append({"rule_id": r["rule_id"], "value": token, "actors": sorted(actors)})
            continue
        actor, sources = next(iter(actors.items()))
        r["actor_ref"] = actor
        r["unknown_fields"] = [f for f in r.get("unknown_fields") or [] if f != "actor_ref"]
        src = sources[0]
        r["provenance"].append({"kind": "source_link", "method": "outcome_actor", "from_ref": str((r.get("outcome") or {}).get("kind")), "to_ref": src["rule_id"],
                                "value": actor})                                # to_ref: a rule id, or the code line that assigns the role
    return unresolved


def observed_rules(candidates: Iterable[Mapping], structured: list, artifact_paths: Mapping[str, str], code: Optional[Mapping] = None) -> tuple:
    """-> (rules, report). `code` = code_facts(...) of the revision (optional). report: supporting_facts, incomplete, skipped, links, unresolved_references,
    unresolved_actors, code_branches, uncovered_branches (lists capped at MAX_LISTED)."""
    rules, facts, incomplete, skipped, links_all, unresolved_refs = [], [], [], [], [], []
    matched_lines: set = set()
    for cand in candidates:
        cls, missing = classify(cand)
        branch = _branch_for(cand, _first_code_quote(cand)[0], code)
        if branch is not None:
            matched_lines.add((branch["artifact_id"], branch["line"]))
            if cls == "incomplete" and branch.get("outcome") is not None:
                cls = "rule"                                                  # the code states the outcome of this branch; the model only left it out
        path = artifact_paths.get(cand.get("artifact_revision_id"))
        refs = [structured_ref(locator(e), path) or str(e.get("locator")) for e in cand.get("evidence") or [] if isinstance(e, Mapping)]
        title = " - ".join(str(x) for x in (cand.get("subject"), cand.get("predicate")) if x)
        if cls == "supporting_fact":
            facts.append({"candidate_id": cand.get("candidate_id"), "title": title, "value": cand.get("value"), "evidence_refs": refs})
            continue
        if cls == "incomplete":
            incomplete.append({"candidate_id": cand.get("candidate_id"), "title": title, "missing": missing, "file": path, "evidence_refs": refs[:3]})
            continue
        try:
            rule = bk.observed_rule_from_candidate(cand)
        except bk.ContractError as exc:
            skipped.append({"candidate_id": cand.get("candidate_id"), "reason": exc.code})
            continue
        _normalize_bindings(rule, cand, artifact_paths)
        quote, code_ref = _first_code_quote(cand)
        links: list = []
        if code_ref:
            from_ref = f"{path}#{code_ref}" if path else code_ref
            model_ok = cand.get("condition") is not None and not cand.get("business_fields_rejected")
            if not model_ok:
                tree = code_condition(quote) if quote else None
                tree = tree or model_condition_tree(cand.get("condition_raw"))
                if tree is not None:
                    res = resolve(tree, structured, from_ref=from_ref, unit=cand.get("unit") if isinstance(cand.get("unit"), str) else None)
                    if res["condition"] is not None:
                        cond, unknown_units = bk.resolve_units(res["condition"], [e.get("quote") for e in cand.get("evidence") or []])
                        bk.validate_condition(cond)
                        rule["condition"] = cond
                        rule["unknown_fields"] = [f for f in rule["unknown_fields"] if f != "condition"] + [u for u in unknown_units if u not in rule["unknown_fields"]]
                        links += res["links"]
                    else:
                        unresolved_refs += [dict(u, rule_id=rule["rule_id"]) for u in res["unresolved"]]
            elif rule["condition"] is not None:
                rule["condition"] = _enum_normalized(rule["condition"], from_ref, links)
            if branch is not None:
                _apply_branch(rule, cand, branch, structured, links)
            for link in links:
                rule["provenance"].append(link)
                if link["method"] == "attribute_name" and link.get("artifact_id"):
                    binding = {"artifact_id": link["artifact_id"], "revision_id": cand.get("source_revision_id"), "evidence_ref": link["to_ref"]}
                    if binding not in rule["source_bindings"]:
                        rule["source_bindings"].append(binding)
                        rule["provenance"].append(dict(kind="source_evidence", **binding))
            links_all += [dict(link, rule_id=rule["rule_id"]) for link in links]
        bk.validate_provenance(rule["provenance"], status=rule["status"])
        rules.append(rule)
    unresolved_actors = link_actors(rules, (code or {}).get("role_mappings"))
    links_all += [dict(p, rule_id=r["rule_id"]) for r in rules for p in r["provenance"] if p.get("method") == "outcome_actor"]
    uncovered = [{"file": b["path"], "qualname": b["qualname"], "line": b["line"], "line_no": b["line_no"]} for b in (code or {}).get("branches") or []
                 if b.get("outcome") is not None and (b["artifact_id"], b["line"]) not in matched_lines]
    report = {"supporting_facts": facts[:MAX_LISTED], "supporting_facts_count": len(facts), "incomplete": incomplete[:MAX_LISTED], "incomplete_count": len(incomplete),
              "skipped": skipped, "source_links": links_all[:MAX_LISTED], "source_links_count": len(links_all),
              "unresolved_references": unresolved_refs[:MAX_LISTED], "unresolved_actors": unresolved_actors[:MAX_LISTED],
              "code_branches": len((code or {}).get("branches") or []), "uncovered_branches": uncovered[:MAX_LISTED], "uncovered_branches_count": len(uncovered)}
    return rules, report
