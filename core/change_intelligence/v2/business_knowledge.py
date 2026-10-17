# -*- coding: utf-8 -*-
"""Final knowledge-loop contract (pure validation, no I/O).

Specified in `docs/contracts/FINAL_KNOWLEDGE_LOOP.md`. Business rules, relations, gaps,
change operations/sets, snapshot pins and document requests/artifacts as plain JSON-able
mappings, validated against a finite typed grammar. Nothing here executes an expression,
reads a store or calls a model. Tenant, workspace, actor and permissions are NOT part of
these shapes: the server takes them from the session.

Reuses `ValidationError`, the existing lifecycle vocabularies and `canonical_hash`; it does
not add a second state machine.
"""

from __future__ import annotations

import copy

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

from core.change_intelligence.v2.schema import (
    FINDING_LIFECYCLE_STATES, RELATION_LIFECYCLE_STATES, RULE_CONFIRMATION_STATES, ValidationError,
)
from core.change_intelligence.v2.serialize import canonical_hash

CONTRACT_VERSION = "fkl/1"
SUPPORTED_CONTRACT_VERSIONS = frozenset({CONTRACT_VERSION})

# Finite limits (docs/contracts/FINAL_KNOWLEDGE_LOOP.md section 8); configurable ceilings, not measured capacity.
MAX_OPERATIONS_PER_REQUEST = 10
MAX_CONDITION_DEPTH = 4
MAX_CONDITION_LEAVES = 20
MAX_RULES_PER_ARTIFACT = 100
MAX_ARTIFACT_BYTES = 1024 * 1024

ERROR_CODES = frozenset({
    "unsupported_contract_version", "unsupported_expression", "invalid_field", "ambiguous_rule", "invented_rule_id",
    "missing_provenance", "forbidden_provenance", "unsupported_operation", "limit_exceeded", "stale_snapshot",
})


class ContractError(ValidationError):
    """A contract violation with a machine-readable `code` (one of `ERROR_CODES`)."""

    def __init__(self, code: str, message: str):
        assert code in ERROR_CODES, code
        super().__init__(f"{code}: {message}")
        self.code = code


AUTHORITIES = frozenset({"OBSERVED", "TARGET"})
IMPLEMENTATION_STATUSES = frozenset({"not_required", "pending", "partial", "verified", "divergent", "unverifiable"})
PROVENANCE_KINDS = frozenset({"source_evidence", "human_decision", "ai_inference"})
RELATION_TYPES = frozenset({"defines", "depends_on", "approves", "exception_to", "conflicts_with", "implements"})
GAP_KINDS = frozenset({"missing_actor", "missing_condition", "missing_outcome", "missing_exception",
                       "conflicting_facts", "uncertain_link", "unsupported_expression"})
OPERATION_KINDS = frozenset({"set_value", "set_actor", "add_or_change_condition", "set_exception", "set_scope"})
OPERATION_STATUSES = frozenset({"proposed", "needs_clarification", "confirmed", "unsupported"})
DOCUMENT_MODES = frozenset({"business", "technical"})
DOCUMENT_FRESHNESS = frozenset({"not_requested", "generating", "available", "stale", "failed"})
# Operations that stay refused until a task adds and verifies them (never silently accepted).
UNSUPPORTED_OPERATION_KINDS = frozenset({"schedule_effective_date", "set_effective_date", "future_activation"})

_COMPARE_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
_ORDER_OPS = frozenset({"gt", "gte", "lt", "lte"})
_MEMBER_OPS = frozenset({"in", "not_in"})
_FIELD_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\Z")
_ID_RE = {"rule": re.compile(r"rule-[a-z0-9][a-z0-9-]*\Z"), "role": re.compile(r"role-[a-z0-9][a-z0-9-]*\Z"),
          "concept": re.compile(r"concept-[a-z0-9][a-z0-9-]*\Z"), "edge": re.compile(r"edge-[a-z0-9][a-z0-9-]*\Z"),
          "gap": re.compile(r"gap-[a-z0-9][a-z0-9-]*\Z"), "op": re.compile(r"op-[a-z0-9][a-z0-9-]*\Z")}


def _fail(code: str, message: str):
    raise ContractError(code, message)


def _need_mapping(value: Any, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        _fail("invalid_field", f"{what} must be an object")
    return value


def _need_keys(obj: Mapping, required: Iterable[str], allowed: Iterable[str], what: str) -> None:
    missing = [k for k in required if k not in obj]
    if missing:
        _fail("invalid_field", f"{what} missing {missing}")
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        _fail("invalid_field", f"{what} has unknown keys {unknown}")


def _need_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("invalid_field", f"{what} must be a non-empty string")
    return value


def _need_id(kind: str, value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE[kind].fullmatch(value):
        _fail("invalid_field", f"{what} must look like '{kind}-<slug>', got {value!r}")
    return value


def _need_enum(value: Any, allowed, what: str) -> str:
    if value not in allowed:
        _fail("invalid_field", f"{what} {value!r} not in {sorted(allowed)}")
    return value


def check_contract_version(obj: Mapping) -> None:
    version = obj.get("contract_version")
    if version not in SUPPORTED_CONTRACT_VERSIONS:
        _fail("unsupported_contract_version", f"{version!r}; supported {sorted(SUPPORTED_CONTRACT_VERSIONS)}")


def _scalar(value: Any, what: str, *, numeric: bool) -> Any:
    """JSON scalar: str, bool, int or decimal string. Floats are refused (no silent binary rounding); booleans are never numbers."""
    if isinstance(value, bool):
        if numeric:
            _fail("unsupported_expression", f"{what}: a boolean is not a number")
        return value
    if isinstance(value, float):
        _fail("unsupported_expression", f"{what}: floats are not allowed; use an integer or a decimal string")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if numeric:
            try:
                if not Decimal(value).is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                _fail("unsupported_expression", f"{what}: {value!r} is not a decimal number")
        return value
    _fail("unsupported_expression", f"{what}: unsupported value type {type(value).__name__}")


def validate_condition(expr: Any, *, _depth: int = 1, _leaves: Optional[list] = None) -> None:
    """Finite typed grammar: leaf {field, op, value[, unit]}, or {all:[..]}, {any:[..]}, {not: expr}. Anything else is refused."""
    leaves = _leaves if _leaves is not None else [0]
    if _depth > MAX_CONDITION_DEPTH:
        _fail("limit_exceeded", f"condition deeper than {MAX_CONDITION_DEPTH}")
    node = _need_mapping(expr, "condition")
    if len(node) == 1 and next(iter(node)) in ("all", "any"):
        key = next(iter(node))
        children = node[key]
        if not isinstance(children, list) or not children:
            _fail("unsupported_expression", f"'{key}' needs a non-empty list")
        for child in children:
            validate_condition(child, _depth=_depth + 1, _leaves=leaves)
        return
    if len(node) == 1 and "not" in node:
        validate_condition(node["not"], _depth=_depth + 1, _leaves=leaves)
        return
    unknown = sorted(set(node) - {"field", "op", "value", "unit"})
    if unknown or not {"field", "op", "value"} <= set(node):
        _fail("unsupported_expression", f"not a comparison, all/any/not node: keys {sorted(node)}")
    leaves[0] += 1
    if leaves[0] > MAX_CONDITION_LEAVES:
        _fail("limit_exceeded", f"more than {MAX_CONDITION_LEAVES} comparisons")
    if not isinstance(node["field"], str) or not _FIELD_RE.fullmatch(node["field"]):
        _fail("invalid_field", f"condition field {node['field']!r}")
    op = node["op"]
    if op in _MEMBER_OPS:
        if not isinstance(node["value"], list) or not node["value"]:
            _fail("unsupported_expression", f"'{op}' needs a non-empty list")
        for item in node["value"]:
            _scalar(item, "membership literal", numeric=False)
    elif op in _COMPARE_OPS:
        _scalar(node["value"], f"value for {op}", numeric=op in _ORDER_OPS)
    else:
        _fail("unsupported_expression", f"operator {op!r} is outside the grammar")
    if "unit" in node:
        _need_text(node["unit"], "condition unit")


def normalize_condition(expr: Mapping) -> Mapping:
    """Order-independent form for all/any so two spellings of one condition compare and hash equal (no-op detection)."""
    validate_condition(expr)

    def norm(node):
        if "all" in node or "any" in node:
            key = "all" if "all" in node else "any"
            kids = sorted((norm(c) for c in node[key]), key=canonical_hash)
            return {key: kids}
        if "not" in node:
            return {"not": norm(node["not"])}
        out = dict(node)
        if out["op"] in _MEMBER_OPS:
            out["value"] = sorted(out["value"], key=lambda v: (type(v).__name__, str(v)))
        return out
    return norm(expr)


def validate_provenance(items: Any, *, status: str, authority: Optional[str] = None) -> None:
    if not isinstance(items, list) or not items:
        _fail("missing_provenance", "at least one provenance entry is required")
    kinds = set()
    for item in items:
        item = _need_mapping(item, "provenance entry")
        kind = item.get("kind")
        if kind not in PROVENANCE_KINDS:
            # Generated documents are derived views, never evidence (no circular self-confirmation).
            _fail("forbidden_provenance", f"provenance kind {kind!r} is not allowed")
        if kind == "human_decision":
            _need_text(item.get("decision_id"), "decision_id")
        elif kind == "source_evidence":
            for k in ("artifact_id", "revision_id", "evidence_ref"):
                _need_text(item.get(k), k)
        else:
            _need_text(item.get("model_run_id"), "model_run_id")
        kinds.add(kind)
    if status == "CONFIRMED" and "human_decision" not in kinds:
        _fail("forbidden_provenance", "a CONFIRMED item needs a human_decision; AI inference or source text alone does not confirm it")
    if authority == "TARGET" and "human_decision" not in kinds:
        _fail("forbidden_provenance", "TARGET authority requires a human_decision")
    if authority == "OBSERVED" and "source_evidence" not in kinds:
        _fail("forbidden_provenance", "OBSERVED authority requires source_evidence")


def validate_business_rule(rule: Any) -> None:
    rule = _need_mapping(rule, "business_rule")
    keys = ("rule_id", "concept_ids", "title", "condition", "actor_ref", "outcome", "exceptions", "scope", "status", "authority",
            "provenance", "source_bindings", "implementation_status")
    _need_keys(rule, keys, keys + ("contract_version", "unknown_fields", "candidate_ids"), "business_rule")
    if "contract_version" in rule:
        check_contract_version(rule)
    _need_id("rule", rule["rule_id"], "rule_id")
    for cid in rule["concept_ids"] if isinstance(rule["concept_ids"], list) else _fail("invalid_field", "concept_ids must be a list"):
        _need_id("concept", cid, "concept id")
    _need_text(rule["title"], "title")
    if rule["condition"] is not None:  # None = condition unknown; the gap queue asks, nothing guesses it
        validate_condition(rule["condition"])
    if rule["actor_ref"] is not None:
        _need_id("role", rule["actor_ref"], "actor_ref")
    outcome = _need_mapping(rule["outcome"], "outcome")
    _need_text(outcome.get("kind"), "outcome.kind")
    for exc in rule["exceptions"] if isinstance(rule["exceptions"], list) else _fail("invalid_field", "exceptions must be a list"):
        exc = _need_mapping(exc, "exception")
        _need_text(exc.get("exception_id"), "exception_id")
        _need_text(exc.get("text"), "exception text")
        if exc.get("condition") is not None:
            validate_condition(exc["condition"])
    _need_mapping(rule["scope"], "scope")
    if not isinstance(rule.get("candidate_ids", []), list) or not all(isinstance(c, str) and c for c in rule.get("candidate_ids", [])):
        _fail("invalid_field", "candidate_ids must be a list of non-empty strings")
    _need_enum(rule["status"], RULE_CONFIRMATION_STATES, "status")
    _need_enum(rule["authority"], AUTHORITIES, "authority")
    _need_enum(rule["implementation_status"], IMPLEMENTATION_STATUSES, "implementation_status")
    validate_provenance(rule["provenance"], status=rule["status"], authority=rule["authority"])
    if not isinstance(rule["source_bindings"], list):
        _fail("invalid_field", "source_bindings must be a list")
    for b in rule["source_bindings"]:
        b = _need_mapping(b, "source binding")
        for k in ("artifact_id", "revision_id", "evidence_ref"):
            _need_text(b.get(k), k)


def validate_relation(rel: Any) -> None:
    rel = _need_mapping(rel, "relation")
    keys = ("edge_id", "from", "to", "type", "status", "provenance_refs")
    _need_keys(rel, keys, keys, "relation")
    _need_id("edge", rel["edge_id"], "edge_id")
    _need_text(rel["from"], "from")
    _need_text(rel["to"], "to")
    _need_enum(rel["type"], RELATION_TYPES, "type")
    _need_enum(rel["status"], RELATION_LIFECYCLE_STATES, "status")
    if not isinstance(rel["provenance_refs"], list):
        _fail("invalid_field", "provenance_refs must be a list")
    if rel["status"] == "CONFIRMED" and not rel["provenance_refs"]:
        _fail("missing_provenance", "a CONFIRMED relation needs provenance_refs")


def gap_id_for(kind: str, affected_rule_ids: Iterable[str], key: str = "") -> str:
    """Stable question id: same kind + same affected rules + same key -> same id, so a re-run never duplicates a question."""
    _need_enum(kind, GAP_KINDS, "gap kind")
    return "gap-" + canonical_hash({"kind": kind, "rules": sorted(affected_rule_ids), "key": key})[:16]


def validate_gap(gap: Any) -> None:
    gap = _need_mapping(gap, "gap")
    keys = ("gap_id", "kind", "question", "affected_rule_ids", "status", "evidence_refs", "requires_permission")
    _need_keys(gap, keys, keys + ("priority", "options"), "gap")
    _need_id("gap", gap["gap_id"], "gap_id")
    _need_enum(gap["kind"], GAP_KINDS, "kind")
    _need_text(gap["question"], "question")
    if not isinstance(gap["affected_rule_ids"], list) or not gap["affected_rule_ids"]:
        _fail("invalid_field", "a gap names at least one affected rule")
    for rid in gap["affected_rule_ids"]:
        _need_id("rule", rid, "affected rule id")
    _need_enum(gap["status"], FINDING_LIFECYCLE_STATES, "status")
    if gap.get("priority", "normal") not in ("critical", "normal"):
        _fail("invalid_field", "priority must be critical or normal")
    _need_text(gap["requires_permission"], "requires_permission")


def _validate_desired(kind: str, desired: Any) -> None:
    if kind == "set_value":
        if isinstance(desired, Mapping):
            _scalar(desired.get("value"), "desired.value", numeric=True)
            _need_text(desired.get("unit"), "desired.unit")
        else:
            _scalar(desired, "desired", numeric=False)
    elif kind == "set_actor":
        _need_id("role", desired, "desired actor")
    elif kind == "add_or_change_condition":
        validate_condition(desired)
    elif kind == "set_exception":
        if not isinstance(desired, list):
            _fail("invalid_field", "desired exceptions must be a list (empty = remove all)")
        for exc in desired:
            _need_text(_need_mapping(exc, "exception").get("text"), "exception text")
            if exc.get("condition") is not None:
                validate_condition(exc["condition"])
    elif kind == "set_scope":
        _need_mapping(desired, "desired scope")


def validate_change_operation(op: Any, *, known_rule_ids: Optional[Iterable[str]] = None) -> None:
    op = _need_mapping(op, "change_operation")
    keys = ("op_id", "kind", "rule_id", "before", "desired", "user_quote", "evidence_refs", "status")
    _need_keys(op, keys, keys + ("candidate_rule_ids", "reason"), "change_operation")
    _need_id("op", op["op_id"], "op_id")
    if op["kind"] in UNSUPPORTED_OPERATION_KINDS:
        _fail("unsupported_operation", f"{op['kind']} is not supported by this contract version")
    _need_enum(op["kind"], OPERATION_KINDS, "kind")
    _need_enum(op["status"], OPERATION_STATUSES, "status")
    _need_text(op["user_quote"], "user_quote")  # the user's own words are always kept
    if not isinstance(op["evidence_refs"], list):
        _fail("invalid_field", "evidence_refs must be a list")
    if op["rule_id"] is None:
        candidates = op.get("candidate_rule_ids") or []
        if op["status"] != "needs_clarification" or len(candidates) < 2:
            _fail("ambiguous_rule", "an operation without a rule_id must be needs_clarification with two or more candidate_rule_ids")
        return
    _need_id("rule", op["rule_id"], "rule_id")
    if known_rule_ids is not None and op["rule_id"] not in set(known_rule_ids):
        _fail("invented_rule_id", f"{op['rule_id']} is not a rule of the pinned snapshot")
    if op["status"] == "unsupported":
        _need_text(op.get("reason"), "reason")
        return
    _validate_desired(op["kind"], op["desired"])


def validate_change_set(change_set: Any, *, known_rule_ids: Optional[Iterable[str]] = None) -> None:
    cs = _need_mapping(change_set, "change_set")
    _need_keys(cs, ("operations", "expected_snapshot"), ("operations", "expected_snapshot", "contract_version", "draft_hash"), "change_set")
    if "contract_version" in cs:
        check_contract_version(cs)
    validate_snapshot_pin(cs["expected_snapshot"])
    ops = cs["operations"]
    if not isinstance(ops, list) or not ops:
        _fail("invalid_field", "a change set needs at least one operation")
    if len(ops) > MAX_OPERATIONS_PER_REQUEST:
        _fail("limit_exceeded", f"{len(ops)} operations; the limit is {MAX_OPERATIONS_PER_REQUEST} per request (split the request)")
    known = list(known_rule_ids) if known_rule_ids is not None else None
    seen = set()
    for op in ops:
        validate_change_operation(op, known_rule_ids=known)
        if op["op_id"] in seen:
            _fail("invalid_field", f"duplicate op_id {op['op_id']}")
        seen.add(op["op_id"])


_SNAPSHOT_KEYS = ("snapshot_id", "business_revision", "source_revision_id", "km_revision_id", "decision_epoch", "schema_version")


def validate_snapshot_pin(pin: Any) -> None:
    pin = _need_mapping(pin, "snapshot pin")
    _need_keys(pin, _SNAPSHOT_KEYS, _SNAPSHOT_KEYS, "snapshot pin")
    for k in ("snapshot_id", "source_revision_id", "km_revision_id", "schema_version"):
        _need_text(pin[k], k)
    for k in ("business_revision", "decision_epoch"):
        if not isinstance(pin[k], int) or isinstance(pin[k], bool) or pin[k] < 0:
            _fail("invalid_field", f"{k} must be a non-negative integer")


def check_snapshot_current(pin: Mapping, current: Mapping) -> None:
    """Optimistic check: the request was built against `pin`; refuse when any pinned part has moved."""
    validate_snapshot_pin(pin)
    moved = [k for k in _SNAPSHOT_KEYS if pin[k] != current.get(k)]
    if moved:
        _fail("stale_snapshot", f"pinned snapshot moved: {moved}")


def canonical_scope_hash(scope: Mapping) -> str:
    """Selection order never matters: id lists are sorted and deduplicated before hashing."""
    scope = _need_mapping(scope, "scope")
    cleaned = {}
    for key, value in scope.items():
        cleaned[key] = sorted(set(value)) if isinstance(value, list) else value
    return canonical_hash(cleaned)


def validate_document_request(req: Any) -> None:
    req = _need_mapping(req, "document_request")
    keys = ("snapshot_id", "mode", "scope", "locale", "template_version")
    _need_keys(req, keys, keys, "document_request")
    _need_text(req["snapshot_id"], "snapshot_id")
    _need_enum(req["mode"], DOCUMENT_MODES, "mode")
    _need_mapping(req["scope"], "scope")
    _need_text(req["locale"], "locale")
    _need_text(req["template_version"], "template_version")


def artifact_key(*, tenant_id: str, workspace_id: str, snapshot_id: str, scope: Mapping, mode: str, locale: str,
                 template_version: str, generator_version: str, schema_version: str, prompt_version: str = "",
                 model_version: str = "", redaction_scope: str = "") -> str:
    """Cache identity of a document artifact (plan 2.3). Authorization is NOT part of the key: it is rechecked on every serve."""
    _need_enum(mode, DOCUMENT_MODES, "mode")
    return canonical_hash({
        "tenant": tenant_id, "workspace": workspace_id, "snapshot": snapshot_id, "scope": canonical_scope_hash(scope), "mode": mode,
        "locale": locale, "template": template_version, "generator": generator_version, "schema": schema_version,
        "prompt": prompt_version, "model": model_version, "redaction": redaction_scope,
    })


def validate_document_artifact(art: Any) -> None:
    art = _need_mapping(art, "document_artifact")
    keys = ("artifact_id", "snapshot_id", "mode", "content_hash", "template_version", "freshness")
    _need_keys(art, keys, keys + ("rule_count", "size_bytes"), "document_artifact")
    _need_text(art["artifact_id"], "artifact_id")
    _need_text(art["snapshot_id"], "snapshot_id")
    _need_enum(art["mode"], DOCUMENT_MODES, "mode")
    if not isinstance(art["content_hash"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", art["content_hash"]):
        _fail("invalid_field", "content_hash must be 'sha256:' plus 64 hex digits")
    _need_enum(art["freshness"], DOCUMENT_FRESHNESS, "freshness")
    if art.get("rule_count", 0) > MAX_RULES_PER_ARTIFACT or art.get("size_bytes", 0) > MAX_ARTIFACT_BYTES:
        _fail("limit_exceeded", "artifact over the rule/size ceiling; split by topic with a manifest")


# ---- legacy candidate -> observed rule (backfill, pure) ---------------------------------------------------------------------

_LEGACY_OPERATORS = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "==": "eq", "!=": "ne"}


def _slug(text: Any, sep: str = "-") -> str:
    return re.sub(r"[^a-z0-9]+", sep, str(text).lower()).strip(sep)


def legacy_rule_id(candidate_id: str) -> str:
    """Stable id derived from the candidate id. Kept readable when the candidate id is already a slug; otherwise a short hash keeps two ids that slug alike apart."""
    slug = _slug(candidate_id)
    if not slug:
        _fail("invalid_field", "candidate id has no usable characters")
    if slug == str(candidate_id).lower():
        return "rule-" + slug
    return f"rule-{slug}-{hashlib.sha256(str(candidate_id).encode('utf-8')).hexdigest()[:8]}"


def _legacy_condition(cand: Mapping) -> Optional[dict]:
    op = _LEGACY_OPERATORS.get(cand.get("operator"))
    value = cand.get("value")
    field = _slug(cand.get("subject"), "_")
    if op is None or not field or isinstance(value, (dict, list)) or value is None:
        return None
    if field[0].isdigit():
        field = "f_" + field
    if isinstance(value, float):
        value = format(Decimal(str(value)), "f")
    leaf = {"field": field, "op": op, "value": value}
    if cand.get("unit"):
        leaf["unit"] = str(cand["unit"])
    try:
        validate_condition(leaf)
    except ContractError:
        return None
    return leaf


_MONETARY_FIELD = re.compile(r"jpy|amount|value|price|cost|fee|charge|threshold|limit|budget|total", re.I)
_CURRENCY_AFTER = r"\s*(JPY|yen|\u5186|USD|EUR)\b"


def _numeric_leaves_of(cond) -> list:
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        v = cond.get("value")
        return [cond] if cond.get("op") in ("gt", "gte", "lt", "lte", "eq", "ne") and isinstance(v, (int, float, str)) and not isinstance(v, bool) and _as_number(v) is not None else []
    out = []
    for key in ("all", "any"):
        for c in cond.get(key, []):
            out += _numeric_leaves_of(c)
    if "not" in cond:
        out += _numeric_leaves_of(cond["not"])
    return out


def _as_number(v):
    try:
        return float(str(v).replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        return None


def unit_from_quotes(value, quotes: Iterable[str]) -> Optional[str]:
    """The currency a quote spells right after this very number ("500,000 JPY" -> "JPY"); None when no quote does. Deterministic; never a guess from other numbers."""
    n = _as_number(value)
    if n is None:
        return None
    for q in quotes:
        if not isinstance(q, str):
            continue
        for m in re.finditer(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)" + _CURRENCY_AFTER, q):
            if _as_number(m.group(1)) == n:
                cur = m.group(2)
                return "JPY" if cur.lower() in ("yen", "\u5186") else cur.upper()
    return None


_COUNT_FIELD = re.compile(r"quantity|count|number|days|percent|pct|ratio|age|hours|minutes|items", re.I)


def _looks_monetary(leaf: Mapping) -> bool:
    """Heuristic for "this number is an amount of money": the field name says so, or the number is 1,000 or more and the field is not a count/duration/percentage."""
    field = str(leaf.get("field") or "")
    if _MONETARY_FIELD.search(field):
        return True
    n = _as_number(leaf.get("value"))
    return n is not None and abs(n) >= 1000 and not _COUNT_FIELD.search(field)


def resolve_units(condition, quotes: Iterable[str]) -> tuple:
    """-> (condition, unknown_unit_fields). Every monetary numeric leaf without a unit gets the unit the evidence spells next to its number; a leaf no quote can settle is
    left WITHOUT a unit and its field is reported, so the rule carries `unit:<field>` in `unknown_fields` (a gap and a conservative evaluator), never a normalised value."""
    if condition is None:
        return None, []
    cond = copy.deepcopy(condition)
    unknown = []
    for leaf in _numeric_leaves_of(cond):
        if leaf.get("unit"):
            continue
        unit = unit_from_quotes(leaf["value"], quotes)                         # always tried: the quote is the only honest source of a unit
        if unit:
            leaf["unit"] = unit
        elif _looks_monetary(leaf):
            unknown.append("unit:" + str(leaf["field"]))
    return cond, sorted(set(unknown))


def observed_rule_from_candidate(cand: Mapping, *, confirmation_decision_id: Optional[str] = None) -> dict:
    """An existing extraction candidate as an OBSERVED business rule. It never invents what the candidate does not carry:
    no operator -> `condition` is null; no actor slot -> `actor_ref` is null; both are listed in `unknown_fields`.
    Model output is never promoted: status is CONFIRMED only when a human decision id is passed in. Raises `ContractError(missing_provenance)`
    for a candidate without evidence (the caller reports it as skipped)."""
    cid = _need_text(cand.get("candidate_id"), "candidate_id")
    evidence = [e for e in (cand.get("evidence") or []) if isinstance(e, Mapping) and isinstance(e.get("locator"), str) and e["locator"].strip()]
    artifact, revision = cand.get("artifact_revision_id"), cand.get("source_revision_id")
    if not evidence or not isinstance(artifact, str) or not isinstance(revision, str):
        _fail("missing_provenance", f"candidate {cid} has no usable source evidence")
    bindings = [{"artifact_id": artifact, "revision_id": revision, "evidence_ref": e["locator"]} for e in evidence]
    provenance = [dict(kind="source_evidence", **b) for b in bindings]
    if cand.get("model_run_id"):
        provenance.append({"kind": "ai_inference", "model_run_id": str(cand["model_run_id"])})
    status = "VALIDATED" if cand.get("applicability") == "RECOGNIZED" else "DRAFT"
    if confirmation_decision_id:
        provenance.append({"kind": "human_decision", "decision_id": confirmation_decision_id})
        status = "CONFIRMED"
    condition = cand.get("condition") if cand.get("condition") is not None else _legacy_condition(cand)
    unknown_units: list = []
    if condition is not None:
        condition, unknown_units = resolve_units(condition, [e.get("quote") for e in evidence])
        validate_condition(condition)
    actor_ref = cand.get("actor") if isinstance(cand.get("actor"), str) and _ID_RE["role"].fullmatch(cand["actor"]) else None
    unknown = [name for name, value in (("condition", condition), ("actor_ref", actor_ref)) if value is None] + unknown_units
    concept = _slug(cand.get("subject"))[:60].strip("-")
    title = " - ".join(str(x) for x in (cand.get("subject"), cand.get("predicate")) if x) or cid
    scope = cand.get("scope") if isinstance(cand.get("scope"), Mapping) and cand.get("scope") else {"unspecified": True}
    return {
        "rule_id": legacy_rule_id(cid), "concept_ids": ["concept-" + concept] if concept else [], "title": title, "condition": condition,
        "actor_ref": actor_ref, "outcome": {"kind": str(cand.get("outcome") or "unspecified"), "predicate": str(cand.get("predicate") or "")},
        "exceptions": [{"exception_id": f"exc-{i + 1}", "text": str(t), "condition": None} for i, t in enumerate(cand.get("exceptions") or [])],
        "scope": dict(scope), "status": status, "authority": "OBSERVED", "provenance": provenance, "source_bindings": bindings,
        "implementation_status": "not_required", "unknown_fields": unknown, "candidate_ids": [cid],
    }
