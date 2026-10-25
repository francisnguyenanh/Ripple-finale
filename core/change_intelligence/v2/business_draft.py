# -*- coding: utf-8 -*-
"""Structured draft of a CONFIRMED business change (pure, no I/O, no model). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md sections 2.4, 5, 6.

The SERVER builds the draft from pinned data only: the confirmed operations, the pinned accepted snapshot (rules, relations), the impact report of the
same pin (`business_impact.build_report`) and the observations of the pinned source revision. It produces:
  * the rule before/after state of every operation (the semantic diff), checked against the contract validator;
  * the relation updates a knowledge commit would need (PROPOSED, applied by nothing here);
  * one action per mapped file location: AI_EDIT_SUPPORTED (a YAML scalar whose stored value equals the confirmed before value), MANUAL (a person edits:
    documents, code, structured edits, unsupported formats, unexpected stored value), NO_OP (the stored value already equals the new one) or UNRESOLVED
    (the knowledge names a file or rule the pinned revision cannot locate);
  * one outcome row per requested operation, never dropped;
  * the coverage, unknown items, counterevidence, excluded rules and model-PROPOSED dependencies of the impact report, carried as they are.
A model may only add GUIDANCE over the server's numbered items (summary, manual instructions, questions, and a `hold` that downgrades an edit to MANUAL).
It can neither add, drop nor upgrade an item. Code is a recommendation; nothing here applies, runs or writes anything.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_knowledge as bk
from core.change_intelligence.v2.business_impact import classify_file
from core.change_intelligence.v2.business_index import condition_text
from core.change_intelligence.v2.serialize import canonical_hash

DRAFT_VERSION = "1.0.0"
ACTIONS = ("AI_EDIT_SUPPORTED", "MANUAL", "NO_OP", "UNRESOLVED")
MAX_ITEMS = 400
GUIDE_KEYS = {"summary", "items", "questions_for_human"}
GUIDE_QUESTION_KEYS = {"question", "needed", "ref"}
GUIDE_ITEM_KEYS = {"ref", "instruction", "hold", "rationale"}
STATEMENT = ("Draft only: it changes no rule, file or code. Every requested change has an outcome below; items marked unresolved or manual still need a person. "
             "Code entries are recommendations, not edits. Items marked unknown were not checked; they are not “no impact”.")
_UNSAFE_TEXT = re.compile(r"<[^>]*>|\]\(|https?://|www\.|`|javascript:|\x00", re.I)
_HEADING = re.compile(r"(?m)^\s*#+\s*(\S+)")
PROPOSED_LABEL = "suggested by a model; not confirmed; not part of what is changed"


def _plain(text, limit) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t if t and len(t) <= limit and not _UNSAFE_TEXT.search(t) else None


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return None
    return None


def _role_words(ref) -> str:
    return str(ref or "unknown").replace("role-", "").replace("-", " ")


def _scope_text(scope: Mapping) -> str:
    return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in sorted((scope or {}).items())) or "no scope stated"


def rule_text(rule: Mapping) -> str:
    """One plain sentence of what the rule says: who, when, exceptions, where."""
    exc = "; ".join(e["text"] for e in rule.get("exceptions") or [])
    parts = [f"actor: {_role_words(rule.get('actor_ref'))}", f"when: {condition_text(rule.get('condition'))}",
             f"exceptions: {exc}" if exc else "exceptions: none", f"scope: {_scope_text(rule.get('scope') or {})}"]
    return " | ".join(parts)


# ---- rule before/after -------------------------------------------------------------------------------------------------------------------------------

def _set_leaf_value(cond, value, unit):
    """The single numeric comparison leaf gets the new value (and unit); the rest of the expression is untouched."""
    if isinstance(cond, Mapping) and "field" in cond:
        out = dict(cond)
        out["value"] = value
        if unit:
            out["unit"] = unit
        return out
    out = dict(cond)
    for key in ("all", "any"):
        if key in cond:
            out[key] = [_set_leaf_value(c, value, unit) if _numeric_leaf_count(c) else c for c in cond[key]]
    if "not" in cond:
        out["not"] = _set_leaf_value(cond["not"], value, unit)
    return out


def _numeric_leaf_count(cond) -> int:
    if not isinstance(cond, Mapping):
        return 0
    if "field" in cond:
        return 1 if cond.get("op") in ("gt", "gte", "lt", "lte", "eq", "ne") and _num(cond.get("value")) is not None and not isinstance(cond.get("value"), bool) else 0
    n = sum(_numeric_leaf_count(c) for key in ("all", "any") for c in cond.get(key, []))
    return n + (_numeric_leaf_count(cond["not"]) if "not" in cond else 0)


def apply_operation(rule: Mapping, op: Mapping) -> dict:
    """The rule after ONE confirmed operation. Only the field the operation names changes; everything else is copied as stored."""
    after = copy.deepcopy(dict(rule))
    kind, desired = op["kind"], op["desired"]
    if kind == "set_value":
        if _numeric_leaf_count(rule.get("condition")) != 1:
            raise ValueError("set_value needs exactly one numeric comparison in the stored condition")
        value = desired.get("value") if isinstance(desired, Mapping) else desired
        unit = desired.get("unit") if isinstance(desired, Mapping) else None
        after["condition"] = _set_leaf_value(rule["condition"], value, unit)
    elif kind == "set_actor":
        after["actor_ref"] = desired
    elif kind == "add_or_change_condition":
        after["condition"] = copy.deepcopy(desired)
    elif kind == "set_exception":
        out = []
        for i, e in enumerate(desired):
            text = e["text"]
            prior = next((x for x in rule.get("exceptions") or [] if x.get("text") == text), None)
            eid = (prior or {}).get("exception_id") or f"exc-{canonical_hash({'r': rule['rule_id'], 't': text, 'i': i})[:12]}"
            out.append({"exception_id": eid, "text": text, "condition": copy.deepcopy(e.get("condition"))})
        after["exceptions"] = out
    elif kind == "set_scope":
        after["scope"] = copy.deepcopy(dict(desired))
    else:
        raise ValueError(f"unsupported operation kind {kind!r}")
    return after


_DIFF_FIELDS = ("actor_ref", "condition", "exceptions", "scope", "title")


def rule_diff(before: Mapping, after: Mapping) -> list:
    out = []
    for f in _DIFF_FIELDS:
        if before.get(f) != after.get(f):
            if f == "actor_ref":
                text = f"who acts: {_role_words(before.get(f))} -> {_role_words(after.get(f))}"
            elif f == "condition":
                text = f"when it applies: {condition_text(before.get(f))} -> {condition_text(after.get(f))}"
            elif f == "exceptions":
                b = "; ".join(e["text"] for e in before.get(f) or []) or "none"
                a = "; ".join(e["text"] for e in after.get(f) or []) or "none"
                text = f"exceptions: {b} -> {a}"
            elif f == "scope":
                text = f"where it applies: {_scope_text(before.get(f) or {})} -> {_scope_text(after.get(f) or {})}"
            else:
                text = f"{f}: {before.get(f)} -> {after.get(f)}"
            out.append({"field": f, "before": copy.deepcopy(before.get(f)), "after": copy.deepcopy(after.get(f)), "text": text})
    return out


_OP_WORDS = {"eq": "is", "ne": "is not", "gt": "more than", "gte": "at least", "lt": "less than", "lte": "at most", "in": "is one of", "not_in": "is none of"}


def _leaves(cond) -> list:
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        return [cond]
    return [x for key in ("all", "any") for c in cond.get(key, []) for x in _leaves(c)] + (_leaves(cond["not"]) if "not" in cond else [])


def _shape(cond):
    """The expression without its leaf values: two conditions with the same shape differ only in leaves."""
    if not isinstance(cond, Mapping):
        return None
    if "field" in cond:
        return "leaf"
    return {k: ([_shape(c) for c in cond[k]] if k in ("all", "any") else _shape(cond[k])) for k in ("all", "any", "not") if k in cond}


def _value_words(v) -> str:
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v)                      # the stored value, unformatted (exact amounts, no locale rewrite)


def semantic_aspects(diff: Iterable[Mapping]) -> list:
    """EUI-03: each rule-diff entry as typed aspects, so a changed operator, actor, unit, exception or scope is as visible as a changed number. Values are the stored
    ones; no percentage or derived figure is computed here."""
    out = []
    for d in diff:
        f, b, a = d["field"], d["before"], d["after"]
        if f == "actor_ref":
            out.append({"aspect": "actor", "label": "Who acts", "before": _role_words(b), "after": _role_words(a)})
        elif f == "condition":
            lb, la = _leaves(b), _leaves(a)
            if b is None or a is None or _shape(b) != _shape(a) or len(lb) != len(la):
                out.append({"aspect": "condition", "label": "When it applies", "before": condition_text(b), "after": condition_text(a)})
                continue
            for x, y in zip(lb, la):
                what = str(x.get("field", "")).replace("_", " ").replace(".", " ")
                if x.get("field") != y.get("field"):
                    out.append({"aspect": "measure", "label": "What is measured", "before": what, "after": str(y.get("field", "")).replace("_", " ").replace(".", " ")})
                if x.get("op") != y.get("op"):
                    out.append({"aspect": "operator", "label": f"Comparison ({what})", "before": _OP_WORDS.get(x.get("op"), str(x.get("op"))),
                                "after": _OP_WORDS.get(y.get("op"), str(y.get("op")))})
                if x.get("value") != y.get("value"):
                    out.append({"aspect": "threshold", "label": f"Value ({what})", "before": _value_words(x.get("value")), "after": _value_words(y.get("value")),
                                "unit": y.get("unit") if x.get("unit") == y.get("unit") else None})
                if x.get("unit") != y.get("unit"):
                    out.append({"aspect": "unit", "label": f"Unit ({what})", "before": str(x.get("unit") or "none"), "after": str(y.get("unit") or "none")})
        elif f == "exceptions":
            tb, ta = [e["text"] for e in b or []], [e["text"] for e in a or []]
            out += [{"aspect": "exception_removed", "label": "Exception removed", "before": t, "after": None} for t in tb if t not in ta]
            out += [{"aspect": "exception_added", "label": "Exception added", "before": None, "after": t} for t in ta if t not in tb]
        elif f == "scope":
            sb, sa = dict(b or {}), dict(a or {})
            for k in sorted(set(sb) | set(sa)):
                if sb.get(k) != sa.get(k):
                    out.append({"aspect": "scope", "label": f"Where it applies ({k})", "before": _value_words(sb[k]) if k in sb else "any", "after": _value_words(sa[k]) if k in sa else "any"})
        else:
            out.append({"aspect": "title", "label": "Title", "before": str(b), "after": str(a)})
    return out


def _flags(op: Mapping, rule: Mapping, before: Mapping) -> list:
    flags = []
    k = op["kind"]
    if k == "set_exception" and (before.get("exceptions") or []) and not op["desired"]:
        flags.append({"flag": "removes_all_exceptions", "severity": "HIGH", "text": "Every exception of this rule would be removed; confirm this is meant."})
    elif k == "set_exception" and len(op["desired"]) < len(before.get("exceptions") or []):
        flags.append({"flag": "drops_an_exception", "severity": "HIGH", "text": "Fewer exceptions than before; each dropped exception must be meant."})
    if k == "set_actor":
        flags.append({"flag": "changes_who_decides", "severity": "HIGH", "text": "Changes who decides."})
    if k == "set_scope":
        flags.append({"flag": "changes_where_it_applies", "severity": "HIGH", "text": "Changes where the rule applies."})
    if rule.get("status") == "CONFIRMED" or rule.get("authority") == "TARGET":
        flags.append({"flag": "changes_a_decided_rule", "severity": "HIGH", "text": "This rule was decided by a person; the change needs the same authority."})
    return flags


def rule_changes(operations: Iterable[Mapping], rules: Mapping[str, Mapping]) -> list:
    """Before/after per changed rule, operations applied in order; every operation keeps an outcome even when the after state is refused."""
    ops = [dict(o) for o in operations]
    by_rule: dict = {}
    for o in ops:
        rid = o["rule_id"]
        if rid not in rules:
            raise ValueError(f"unknown_rule:{rid}")
        cur = by_rule.setdefault(rid, {"rule_id": rid, "title": rules[rid]["title"], "before": copy.deepcopy(dict(rules[rid])), "after": copy.deepcopy(dict(rules[rid])),
                                        "op_ids": [], "flags": [], "valid": True, "error": None, "op_errors": {}})
        cur["op_ids"].append(o["op_id"])
        cur["flags"] += _flags(o, rules[rid], cur["after"])
        try:
            cur["after"] = apply_operation(cur["after"], o)
        except (ValueError, KeyError, TypeError) as exc:
            cur["valid"] = False
            cur["op_errors"][o["op_id"]] = f"cannot_apply:{exc}"
    for rc in by_rule.values():
        if rc["valid"]:
            try:
                bk.validate_business_rule(rc["after"])
            except bk.ContractError as exc:
                rc["valid"], rc["error"] = False, f"{exc.code}: {exc}"
            except Exception as exc:                                                         # noqa: BLE001  the validator refused for another reason: never a silent pass
                rc["valid"], rc["error"] = False, f"invalid_after_state: {exc}"
        rc["diff"] = rule_diff(rc["before"], rc["after"])
        rc["before_text"], rc["after_text"] = rule_text(rc["before"]), rule_text(rc["after"])
        rc["invariants"] = {"exceptions_kept": rc["before"].get("exceptions") == rc["after"].get("exceptions") or any(o["kind"] == "set_exception" for o in ops if o["rule_id"] == rc["rule_id"]),
                            "provenance_kept": rc["before"].get("provenance") == rc["after"].get("provenance"),
                            "bindings_kept": rc["before"].get("source_bindings") == rc["after"].get("source_bindings"),
                            "id_kept": rc["before"].get("rule_id") == rc["after"].get("rule_id")}
        if not all(rc["invariants"].values()):
            rc["valid"], rc["error"] = False, "invariant_broken:" + ",".join(k for k, v in rc["invariants"].items() if not v)
    return list(by_rule.values())


# ---- relation updates -------------------------------------------------------------------------------------------------------------------------------

def relation_updates(relations: Iterable[Mapping], operations: Iterable[Mapping], rules: Mapping[str, Mapping]) -> list:
    """What a knowledge commit would have to do with the relations that touch a changed rule. PROPOSED; nothing applied."""
    ops = list(operations)
    write = {o["rule_id"] for o in ops}
    actor_change = {o["rule_id"]: (o["before"], o["desired"]) for o in ops if o["kind"] == "set_actor"}
    out = []
    for e in relations:
        if e["from"] not in write and e["to"] not in write:
            continue
        rid = e["from"] if e["from"] in write else e["to"]
        item = {"edge_id": e["edge_id"], "type": e["type"], "from": e["from"], "to": e["to"], "edge_status": e["status"], "rule_id": rid, "status": "PROPOSED"}
        if e["type"] == "conflicts_with":
            item.update(action="keep_as_counterevidence", text="Kept as read-only counterevidence; the other rule is not changed.")
        elif rid in actor_change and e["type"] == "approves" and actor_change[rid][0] in (e["from"], e["to"]):
            old, new = actor_change[rid]
            item.update(action="update_endpoint", before={"from": e["from"], "to": e["to"]},
                        after={"from": new if e["from"] == old else e["from"], "to": new if e["to"] == old else e["to"]},
                        text=f"The approving role moves from {_role_words(old)} to {_role_words(new)}.")
        else:
            item.update(action="recheck", text="One end of this relation changes; a person checks whether the relation still holds.")
        out.append(item)
    return sorted(out, key=lambda i: (i["rule_id"], i["edge_id"]))


# ---- file actions ----------------------------------------------------------------------------------------------------------------------------------

def _locator_label(loc) -> Optional[str]:
    if not isinstance(loc, Mapping):
        return None
    for k in ("key_path", "symbol", "qualname"):
        if isinstance(loc.get(k), str) and loc[k] and loc[k] != "<module>":
            return loc[k]
    if isinstance(loc.get("sheet"), str) and isinstance(loc.get("cell"), str):
        return f"{loc['sheet']}!{loc['cell']}"
    return None


def _obs_text(o: Mapping) -> Optional[str]:
    v = o.get("raw_value")
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str)


def _find_observation(observations: list, locator: str):
    """-> (observation, how) : structured by parser label; prose by a heading that carries the locator; else (None, None)."""
    for o in observations:
        if _locator_label(o.get("locator")) == locator:
            return o, "label"
    for o in observations:
        text = _obs_text(o)
        if isinstance(o.get("raw_value"), str) and text and any(h == locator or h.rstrip(".") == locator for h in _HEADING.findall(text)):
            return o, "heading"
    return None, None


def binding_location(binding: Mapping, artifacts: Mapping[str, str]) -> tuple:
    """-> (path, locator label or None) of a source binding. Two stored forms: "path#label" (config keys, document headings) and a JSON locator without a path
    (code spans, line spans: extraction candidates store the locator object; live finding SUR03-V2-A10, 2026-10-23, where every code binding lost its file and
    the coder was offered nothing). The JSON form takes its path from the bound artifact of the pinned revision and its label from the locator."""
    ref = str(binding.get("evidence_ref") or "")
    if ref.startswith("{"):
        try:
            loc = json.loads(ref)
        except ValueError:
            loc = None
        if isinstance(loc, Mapping):
            return str(artifacts.get(binding.get("artifact_id")) or ref), _locator_label(loc)
    path, _, label = ref.partition("#")
    return path, label or None


def _resolve_artifact(artifact_id: str, path: str, artifacts: Mapping[str, str]):
    """-> (artifact_id in the pinned revision or None, rebound: bool)."""
    if artifact_id in artifacts:
        return artifact_id, False
    hits = [aid for aid, p in artifacts.items() if p == path or str(p).endswith("/" + path)]
    return (hits[0], True) if len(hits) == 1 else (None, False)


def _value_edit(op: Mapping, obs: Mapping, kind: str):
    """-> (action, reason, proposal_edit) for a set_value operation at one observation."""
    raw = obs.get("raw_value")
    stored = _num(raw) if not isinstance(raw, str) or _num(raw) is not None else None
    before = _num(op["before"]["value"]) if isinstance(op.get("before"), Mapping) else None
    desired = op["desired"].get("value") if isinstance(op["desired"], Mapping) else op["desired"]
    unit = op["desired"].get("unit") if isinstance(op["desired"], Mapping) else None
    new = _num(desired)
    if stored is None:
        return "MANUAL", "stored_value_is_not_a_number", None
    if new is not None and stored == new:
        return "NO_OP", "already_equals_new_value", None
    if before is not None and stored != before:
        return "MANUAL", "stored_value_differs_from_confirmed_before_value", None
    key_path = obs.get("locator", {}).get("key_path") if isinstance(obs.get("locator"), Mapping) else None
    if kind == "config" and key_path and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return "AI_EDIT_SUPPORTED", "yaml_scalar_equals_confirmed_before_value", {"key_path": key_path, "expected_current": raw, "proposed": desired, **({"unit": unit} if unit else {})}
    return "MANUAL", "structured_value_not_a_yaml_scalar", None


def _instruction(action: str, reason: str, f: Mapping, op: Optional[Mapping], title: str, locator: Optional[str]) -> str:
    where = f"{f['path']}" + (f" at {locator}" if locator else "")
    what = _what(op, title) if op else f"check “{title}”"
    if action == "AI_EDIT_SUPPORTED":
        return f"Edit {where}: {what} (a reviewer approves the exact edit; nothing is applied by this draft)."
    if action == "NO_OP":
        return f"No edit needed in {where}: it already states the new value."
    if action == "UNRESOLVED":
        return f"The knowledge names {where} for “{title}”, but the pinned source revision has no such file. Locate the current file, then {what}; record where it is so the knowledge can be re-bound."
    if reason == "location_not_found":
        return f"Open {where}: the recorded location was not found in the pinned revision; find the passage about “{title}” and {what}."
    if reason == "related_rule_check":
        return f"Review {where} for “{title}”: it is related to a changed rule; check whether it must follow the change."
    if f["kind"] == "code":
        return f"Review the code at {where}: {what}. Recommendation only; a developer changes and tests the code."
    if f["kind"] == "document":
        return f"Update the wording in {where}: {what}."
    if f["kind"] == "other":
        return f"This file format is not edited automatically. Open {where} and {what}; record the exact cell or line changed."
    return f"Update {where} by hand: {what}."


def _what(op: Mapping, title: str) -> str:
    k, d = op["kind"], op["desired"]
    if k == "set_value":
        val = d if not isinstance(d, Mapping) else f"{d['value']} {d.get('unit') or ''}".strip()
        return f"change the value of “{title}” from {op['before']['value'] if isinstance(op.get('before'), Mapping) else op.get('before')} to {val}"
    if k == "set_actor":
        return f"make {_role_words(d)} the person who acts on “{title}” (was {_role_words(op.get('before'))})"
    if k == "add_or_change_condition":
        return f"make “{title}” apply when {condition_text(d)}"
    if k == "set_exception":
        return f"set the exceptions of “{title}” to: " + ("; ".join(e['text'] for e in d) if d else "none")
    return f"make “{title}” apply to: {_scope_text(d)}"


def file_actions(report: Mapping, operations: Iterable[Mapping], rules: Mapping[str, Mapping], artifacts: Mapping[str, str], observations: Iterable[Mapping]) -> list:
    """One action per (operation, bound source location) of a changed rule, plus one check item per bound location of a related rule. `artifacts` = {artifact_id: logical_path} of
    the pinned source revision; `observations` its rows. Locations come from each rule's own bindings, so an operation is only paired with the locations of its rule."""
    ops = [dict(o) for o in operations]
    write = {o["rule_id"] for o in ops}
    by_art: dict = {}
    for o in observations:
        by_art.setdefault(o.get("artifact_id"), []).append(o)
    order = list(dict.fromkeys([o["rule_id"] for o in ops] + [i["rule_id"] for i in report["affected_rules"]]))
    items, n = [], 0
    for rid in order:
        rule = rules.get(rid)
        if rule is None:
            continue
        role = "to_change" if rid in write else "check"
        for b in rule.get("source_bindings") or []:
            path, loc = binding_location(b, artifacts)
            kind = classify_file(path)
            sheet = path.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv"))            # a spreadsheet is a document for the impact inventory but is not prose: never a wording edit
            f = {"path": path, "kind": "other" if sheet else kind}
            aid, rebound = _resolve_artifact(b["artifact_id"], path, artifacts)
            for op in ([o for o in ops if o["rule_id"] == rid] if role == "to_change" else [None]):
                if len(items) >= MAX_ITEMS:
                    return items
                n += 1
                item = {"ref": f"F{n:03d}", "artifact_id": b["artifact_id"], "resolved_artifact_id": aid, "rebound_by_path": rebound, "path": path, "locator": loc, "kind": kind, "role": role,
                        "rule_ids": [rid], "op_ids": [op["op_id"]] if op else [], "stored_text": None, "resolution": None, "action": None, "reason": None, "proposal_edit": None,
                        "code_recommendation": kind == "code"}
                if aid is None:
                    item.update(action="UNRESOLVED", reason="missing_file", resolution="missing")
                else:
                    obs, how = _find_observation(by_art.get(aid, []), loc) if loc else (None, None)
                    if obs is None:
                        item.update(resolution="file_only", action="MANUAL", reason="location_not_found" if loc else "no_location_recorded")
                    else:
                        item.update(resolution=how, stored_text=(_obs_text(obs) or "")[:2000 if kind == "code" else 200])      # a code recommendation needs the whole function
                        if op is None:
                            item.update(action="MANUAL", reason="related_rule_check")
                        elif kind == "code":
                            item.update(action="MANUAL", reason="code_recommendation_only")
                        elif sheet:
                            item.update(action="MANUAL", reason="unsupported_format_manual")
                        elif kind == "document":
                            item.update(action="MANUAL", reason="wording_update")
                        elif kind == "config" and op["kind"] == "set_value":
                            action, reason, edit = _value_edit(op, obs, kind)
                            item.update(action=action, reason=reason, proposal_edit=edit)
                        elif kind == "config":
                            item.update(action="MANUAL", reason="structured_edit_not_auto_derived")
                        else:
                            item.update(action="MANUAL", reason="unsupported_format_manual")
                item["instruction"] = _instruction(item["action"], item["reason"], f, op, rule["title"], loc)
                items.append(item)
    return items


def unresolved_items(report: Mapping, rules: Mapping[str, Mapping], start: int) -> list:
    """The impact report's unknowns as draft items a person has to resolve (never dropped, never read as no impact)."""
    out = []
    n = start
    for g in report["gaps"]:
        if g["kind"] == "missing_file":
            continue                                                                  # already an UNRESOLVED file action
        n += 1
        if g["kind"] == "no_source_mapping":
            title = rules[g["ref"]]["title"] if g["ref"] in rules else g["ref"]
            text = f"Find where “{title}” is written down or implemented (document, setting or code); the knowledge has no mapped location, so its files are unknown. Record the location."
        elif g["kind"] == "stale_edge":
            text = "A stored relation points at knowledge that no longer exists; a person checks what it meant before the change is committed."
        elif g["kind"] == "graph_traversal_limited":
            names = [rules[r]["title"] for r in g.get("rule_ids") or [] if r in rules][:5]
            text = (f"The relation walk stopped at its limit; {g.get('count') or len(g.get('rule_ids') or [])} further connected rule(s) were not checked"
                    + (" (for example “" + "”, “".join(names) + "”)" if names else "") + ". Review them before committing.")
        else:
            text = g["text"]
        out.append({"ref": f"U{n:03d}", "kind": g["kind"], "rule_id": g["ref"] if g["kind"] in ("no_source_mapping", "stale_edge") else None, "action": "UNRESOLVED", "reason": g["kind"],
                    "instruction": text, "gap_text": g["text"]})
    return out


# ---- server holds: what the rules themselves show must be decided by a person ------------------------------------------------------------------------

HOLD_KINDS = ("approval_order_inverted", "dependent_rule_loses_trigger", "conflicts_with_effective_rule")
HOLD_BASES = ("rules", "accepted_relation", "rule_condition", "recorded_disagreement")      # a model-proposed dependency alone is never a basis: it is checked, then shown as a lead
_DEPENDENT_TYPES = ("depends_on", "approves", "implements", "exception_to")
DEPENDENCY_CHECKS = {"trigger_verified": "its own condition is the changed rule's act, so it loses its trigger for the cases the change drops (held above when cases are lost)",
                     "independent_trigger": "it decides on its own threshold of the same field; this change does not touch what triggers it",
                     "unverified": "not shown by the rules themselves; a person checks it",
                     "other_side": "the changed rule is the one said to depend; what triggers the other rule is not touched by this change"}


def _tokens(text) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t}


def _state_leaves(rule: Mapping) -> list:
    """The boolean leaves of a rule's condition whose field names a state another act sets ("cargo.master_approved", "interruption.customer_notice_issued",
    "tenant_notice.issued"): -> [(leaf, the words naming what is in that state)]. The subject is the words before the state word in the last path segment, or —
    when the last segment is the state word alone — the words of the segment before it (acceptance item agd-05, 2026-10-25)."""
    out = []
    for leaf in _hold_leaves(rule.get("condition")):
        if isinstance(leaf.get("value"), bool):
            segments = [[t for t in re.split(r"[^a-z0-9]+", seg.lower()) if t] for seg in str(leaf.get("field") or "").split(".")]
            segments = [s for s in segments if s]
            if segments and len(segments[-1]) >= 2:
                out.append((leaf, set(segments[-1][:-1])))
            elif len(segments) >= 2 and len(segments[-1]) == 1:
                out.append((leaf, set(segments[-2])))
    return out


def _triggered_by(dependent: Mapping, rule: Mapping) -> Optional[str]:
    """Is `dependent` triggered by the act of `rule`? Proven from the dependent's own condition as read from its source: a boolean state leaf named after the
    changed rule's actor or outcome ("cargo the Ship Master has approved", "when a customer notice is issued"). -> that leaf's field, or None."""
    names = _tokens(_role_words(rule.get("actor_ref"))) | _tokens((rule.get("outcome") or {}).get("kind"))
    for leaf, subject in _state_leaves(dependent):
        if subject and subject <= names:
            return leaf["field"]
    return None


def dependency_checks(changes: Iterable[Mapping], rules: Mapping[str, Mapping], proposed_dependencies: Iterable[Mapping]) -> dict:
    """What the server can say about each model-proposed dependency on a changed rule, from the rules themselves: `trigger_verified` (the proposed dependent's
    own condition is the changed rule's act), `independent_trigger` (it decides on its own threshold of the measured field), `unverified` (neither; a person
    checks). -> {dep_id: {"check", "text"}}. A proposal is a lead, never a hold on its own."""
    out = {}
    live = {rid: r for rid, r in rules.items() if r.get("status") not in ("REJECTED", "SUPERSEDED")}
    for c in changes:
        rid, before = c["rule_id"], c["before"]
        base = rid[:-len("-target")] if rid.endswith("-target") else rid
        measured = {l["field"] for l in _hold_leaves(before.get("condition")) if _hold_is_num(l.get("value"))}
        for d in proposed_dependencies or []:
            if d.get("status") != "PROPOSED" or not d.get("dep_id") or base not in (d.get("from_rule_id"), d.get("to_rule_id")):
                continue
            if d.get("to_rule_id") != base or d.get("from_rule_id") not in live:
                out.setdefault(d["dep_id"], {"check": "other_side", "text": DEPENDENCY_CHECKS["other_side"]})
                continue
            dep = live[d["from_rule_id"]]
            if _triggered_by(dep, before):
                check = "trigger_verified"
            elif measured and any(l.get("field") in measured and _hold_is_num(l.get("value")) for l in _hold_leaves(dep.get("condition"))):
                check = "independent_trigger"
            else:
                check = "unverified"
            out[d["dep_id"]] = {"check": check, "text": DEPENDENCY_CHECKS[check]}
    return out


def _hold_single_numeric(rule: Mapping):
    leaves = [l for l in _hold_leaves(rule.get("condition")) if _hold_is_num(l.get("value"))]
    return leaves[0] if len(leaves) == 1 else None


def _hold_leaves(cond) -> list:
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        return [cond]
    return [leaf for key in ("all", "any") for c in cond.get(key, []) for leaf in _hold_leaves(c)] + (_hold_leaves(cond["not"]) if "not" in cond else [])


def _hold_is_num(v) -> bool:
    if isinstance(v, bool):
        return False
    try:
        float(str(v).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _hold_num(v) -> float:
    return float(str(v).replace(",", ""))


def _narrows(before: Mapping, after: Mapping) -> Optional[str]:
    """Does the rule apply to fewer cases after the change? A raised lower bound, a lowered upper bound, a narrower scope or an added conjunct. -> a short
    statement of the cases that drop out, or None."""
    b, a = _hold_single_numeric(before), _hold_single_numeric(after)
    if b and a and b["field"] == a["field"] and b["op"] == a["op"]:
        x, y = _hold_num(b["value"]), _hold_num(a["value"])
        if b["op"] in ("gte", "gt") and y > x:
            return f"cases between {_value_words(b['value'])} and {_value_words(a['value'])} {b.get('unit') or ''}".rstrip()
        if b["op"] in ("lte", "lt") and y < x:
            return f"cases between {_value_words(a['value'])} and {_value_words(b['value'])} {b.get('unit') or ''}".rstrip()
    bs, as_ = before.get("scope") or {}, after.get("scope") or {}
    for key, vals in bs.items():
        bv = set(vals if isinstance(vals, list) else [vals])
        av = set(as_.get(key, []) if isinstance(as_.get(key, []), list) else [as_.get(key)])
        if key in as_ and av < bv:
            return f"the {key} values {', '.join(sorted(str(v) for v in bv - av))}"
    bl, al = _hold_leaves(before.get("condition")), _hold_leaves(after.get("condition"))
    if isinstance(after.get("condition"), Mapping) and "all" in after["condition"] and len(al) > len(bl) and all(l in al for l in bl):
        return "the cases that do not meet the added condition"
    return None


def server_holds(changes: Iterable[Mapping], rules: Mapping[str, Mapping], relations: Iterable[Mapping], proposed_dependencies: Iterable[Mapping],
                 counterevidence_rule_ids: Iterable[str], start: int = 0) -> list:
    """Holds the server raises from the rules themselves, each with a proven basis: `rules` (two tiers of the same decision — the same outcome kind — whose
    order the changed number inverts), `accepted_relation` (a confirmed relation of the knowledge), `rule_condition` (the dependent rule's own condition is the
    changed rule's act, as its source wrote it), `recorded_disagreement`. Two thresholds that merely differ, two actors with different titles, or a dependency a
    model proposed are no basis (the proposal is checked and shown as a lead: `dependency_checks`). A hold is an unresolved item (ref H00n) a person clarifies;
    until then it blocks the knowledge decision and downgrades the automatic edits of the held rule. Nothing here names a rule id or a number in advance: every
    figure comes from the pinned knowledge."""
    out = []
    n = start
    withdrawn = ("REJECTED", "SUPERSEDED")
    live = {rid: r for rid, r in rules.items() if r.get("status") not in withdrawn}
    rels = [e for e in relations if e.get("status") == "CONFIRMED" and e.get("type") in _DEPENDENT_TYPES]       # only a confirmed relation is a basis
    linked = {frozenset((e.get("from"), e.get("to"))) for e in rels}
    for c in changes:
        rid, before, after = c["rule_id"], c["before"], c["after"]
        base = rid[:-len("-target")] if rid.endswith("-target") else rid
        b, a = _hold_single_numeric(before), _hold_single_numeric(after)
        if b and a and b["field"] == a["field"] and _hold_num(b["value"]) != _hold_num(a["value"]):
            x, y = _hold_num(b["value"]), _hold_num(a["value"])
            kind = (before.get("outcome") or {}).get("kind")
            for oid, other in sorted(live.items()):
                obase = oid[:-len("-target")] if oid.endswith("-target") else oid
                if obase == base or not other.get("actor_ref") or other.get("actor_ref") == before.get("actor_ref"):
                    continue
                if not ((kind and (other.get("outcome") or {}).get("kind") == kind) or frozenset((base, obase)) in linked):
                    continue                                            # a different duty on the same number (a notice beside an approval) has no order to invert
                o = next((l for l in _hold_leaves(other.get("condition")) if l.get("field") == b["field"] and _hold_is_num(l.get("value"))), None)
                if o is None or (o["op"] in ("gte", "gt")) != (b["op"] in ("gte", "gt")):
                    continue
                s = _hold_num(o["value"])
                order = lambda v: (v > s) - (v < s)                       # noqa: E731
                if order(x) != order(y):
                    n += 1
                    out.append({"ref": f"H{n:03d}", "kind": "server_hold", "hold": "approval_order_inverted", "rule_id": rid, "other_rule_id": oid, "basis": "rules",
                                "evidence": {"field": b["field"], "before": b["value"], "after": a["value"], "other_value": o["value"], "actor": before.get("actor_ref"), "other_actor": other.get("actor_ref"),
                                             "outcome": kind, "relation": "confirmed" if frozenset((base, obase)) in linked else None},
                                "action": "UNRESOLVED", "reason": "approval_order_inverted",
                                "instruction": (f"“{before.get('title')}” ({_role_words(before.get('actor_ref'))}) moves from {_value_words(b['value'])} to {_value_words(a['value'])} "
                                                f"{b.get('unit') or ''}, which puts it on the other side of “{other.get('title')}” ({_role_words(other.get('actor_ref'))}, "
                                                f"{_value_words(o['value'])} {o.get('unit') or ''}), a tier of the same decision: the order of the two tiers would be inverted. "
                                                "A person decides how the two rules fit together before this change is made.").replace("  ", " "),
                                "gap_text": "approval order inverted"})
        lost = _narrows(before, after)
        if lost:
            dependents = []
            for e in rels:
                if base in (e.get("from"), e.get("to")):
                    other = e["to"] if e["from"] == base else e["from"]
                    if e.get("type") == "approves" and e.get("to") == base:
                        continue                                        # a role approving this rule is not a rule that depends on it
                    if other in live and other != base and e.get("from") == other:
                        dependents.append((other, "accepted_relation", e.get("type")))
            for did, dep in sorted(live.items()):                       # the dependent's own condition is this rule's act: proven from the rule as its source wrote it
                dbase = did[:-len("-target")] if did.endswith("-target") else did
                field = None if dbase == base else _triggered_by(dep, before)
                if field:
                    dependents.append((did, "rule_condition", field))
            seen = set()
            for did, basis, rel in dependents:
                if did in seen:
                    continue
                seen.add(did)
                n += 1
                label = f"a confirmed {rel} relation of the knowledge" if basis == "accepted_relation" else f"its own condition “{rel}” is what this rule decides"
                out.append({"ref": f"H{n:03d}", "kind": "server_hold", "hold": "dependent_rule_loses_trigger", "rule_id": rid, "other_rule_id": did, "basis": basis,
                            "evidence": {"relation": rel, "cases_lost": lost}, "action": "UNRESOLVED", "reason": "dependent_rule_loses_trigger",
                            "instruction": (f"“{live[did].get('title')}” follows from “{before.get('title')}” ({label}); after this change {lost} no longer reach "
                                            f"“{before.get('title')}”, so “{live[did].get('title')}” would no longer be triggered for them. A person decides whether that is meant."),
                            "gap_text": "dependent rule loses its trigger"})
        for oid in sorted(set(counterevidence_rule_ids)):
            if oid in live and oid != base:
                n += 1
                out.append({"ref": f"H{n:03d}", "kind": "server_hold", "hold": "conflicts_with_effective_rule", "rule_id": rid, "other_rule_id": oid, "basis": "recorded_disagreement",
                            "evidence": {}, "action": "UNRESOLVED", "reason": "conflicts_with_effective_rule",
                            "instruction": (f"“{before.get('title')}” disagrees with the effective rule “{live[oid].get('title')}” (a recorded disagreement). Changing one side "
                                            "does not settle it: a person decides which rule stands before this change is made."),
                            "gap_text": "conflicts with an effective rule"})
    return out


def dependency_requirement(report: Mapping, rules: Mapping[str, Mapping]) -> dict:
    """Whether a model dependency check is required before guidance: not applicable, with grounds, only when every live rule of the knowledge is already in the
    impact report (changed, affected or counter-evidence) — there is nothing left for a check to find. Otherwise required."""
    withdrawn = ("REJECTED", "SUPERSEDED")
    live = {rid for rid, r in rules.items() if r.get("status") not in withdrawn}
    covered = set(report.get("write_scope") or []) | {i["rule_id"] for i in report.get("affected_rules") or []} | set(report.get("counterevidence_rule_ids") or [])
    outside = sorted(live - covered)
    if not outside:
        return {"required": False, "reason": "every live rule is already in the impact report; nothing is left for a dependency check", "rules_outside": 0}
    return {"required": True, "reason": f"{len(outside)} live rule(s) lie outside the impact report", "rules_outside": len(outside)}


# ---- the draft -------------------------------------------------------------------------------------------------------------------------------------

def _op_outcome(op: Mapping, rc: Mapping, items: list, unresolved: list, rules: Mapping) -> dict:
    mine = [i for i in items if op["op_id"] in i["op_ids"]]
    counts = {a: sum(1 for i in mine if i["action"] == a) for a in ACTIONS}
    has_binding = bool(rules[op["rule_id"]].get("source_bindings"))
    unknown_rule = any(u["rule_id"] == op["rule_id"] and u["kind"] == "no_source_mapping" for u in unresolved)
    if not has_binding or unknown_rule:
        coverage = "unknown"
    elif counts["UNRESOLVED"]:
        coverage = "partial" if any(i["action"] != "UNRESOLVED" for i in mine) else "unknown"
    else:
        coverage = "complete"
    rule_change = "DRAFTED" if rc["valid"] else ("INVALID_AFTER_STATE" if op["op_id"] not in rc["op_errors"] else "CANNOT_APPLY")
    return {"op_id": op["op_id"], "kind": op["kind"], "rule_id": op["rule_id"], "title": rc["title"], "what": _what(op, rc["title"]), "rule_change": rule_change,
            "error": rc["op_errors"].get(op["op_id"]) or (rc["error"] if not rc["valid"] else None), "files": counts, "file_refs": [i["ref"] for i in mine], "file_coverage": coverage,
            "flags": [f for f in rc["flags"]], "simulation": None}


_LINK_ABOUT = {"confirmed_link": "linked to the changed rule by a confirmed relation", "inferred_dependency": "possibly related to the changed rule (not confirmed)",
               "counterevidence": "disagrees with the changed rule (kept as evidence, not changed)"}
_EDGE_ABOUT = {"defines": "it belongs to the same business concept", "approves": "the same role acts on it", "depends_on": "a stored relation links them",
               "implements": "a stored relation links them", "exception_to": "a stored relation links them", "conflicts_with": "the two disagree"}


LINK_KINDS = ("changed", "held", "follows_from", "independent_obligation", "same_topic_only", "counterevidence")


def _item_link(item: Mapping, rules: Mapping[str, Mapping], changes: Iterable[Mapping], affected: Mapping[str, Mapping], edges: Mapping[str, Mapping],
               held_by: Optional[Mapping[str, Mapping]] = None) -> tuple:
    """(link_kind, about) of a file item, in business words (rule titles, values, the kind of link) — never a file, location or id. A related rule is classed by
    what the knowledge proves, not by sharing a topic (continuation of the AI quality round, 2026-10-25: acceptance items agd-06 / agd-08 listed a rule reached
    only through the same concept or the same role as something to check, and the model then suggested aligning it):
      follows_from            its own condition is a changed rule's act, or a confirmed relation of the knowledge links it — check whether it should change too;
      independent_obligation  it decides on its own threshold of a field the changed rule measures — a separate duty this change does not alter;
      same_topic_only         reached only through the same business concept or role — nothing links its condition to the change;
      counterevidence         it disagrees with a changed rule (kept as evidence).
    Every related rule stays listed; only its label and the action it asks for differ."""
    rid = (item.get("rule_ids") or [None])[0]
    rule = rules.get(rid) or {}
    title = rule.get("title") or rid
    changes = list(changes)
    if item.get("role") == "to_change":
        c = next((x for x in changes if x["rule_id"] == rid), None)
        what = "; ".join(d["text"] for d in (c or {}).get("diff") or [])
        return "changed", (f"changes “{title}”: {what}" if what else f"changes “{title}”")
    a = affected.get(rid) or {}
    path = [edges[e] for e in a.get("path") or [] if e in edges]
    hold = (held_by or {}).get(rid)
    if hold is not None:                                                  # the server raised a hold about this rule: the item points to that decision, never "no action"
        return "held", f"related rule “{title}” is the subject of hold {hold['ref']} ({hold['hold'].replace('_', ' ')}): a person decides how it fits before the change goes on"
    if a.get("link") == "counterevidence":
        return "counterevidence", f"related rule “{title}” disagrees with the changed rule (kept as evidence, not changed); a person may need to settle which stands"
    trigger = next((f for c in changes for f in [_triggered_by(rule, c["before"])] if f), None)
    confirmed = path and all(e.get("status") == "CONFIRMED" and e.get("type") in _DEPENDENT_TYPES for e in path)
    if trigger or confirmed:
        basis = f"its own condition “{trigger}” is what the changed rule decides" if trigger else "a confirmed relation of the knowledge links them"
        # the direction of the effect, from the changed rule itself: a narrowing change that drops cases is held above (class "held"); here no case is lost
        widened = [w for c in changes for w in [_narrows(c["after"], c["before"])] if w]
        effect = (f"the change only adds cases ({widened[0]} now reach it); no case loses it, so nothing here needs a decision unless the extra cases matter"
                  if widened else "check whether it should change with it")
        return "follows_from", f"related rule “{title}” follows from the changed rule ({basis}); {effect}"
    measured = {l["field"] for c in changes for l in _hold_leaves((c.get("before") or {}).get("condition")) if _hold_is_num(l.get("value"))}
    own = [l for l in _hold_leaves(rule.get("condition")) if l.get("field") in measured and _hold_is_num(l.get("value"))]
    if own and any((rule.get("outcome") or {}).get("kind") == ((c.get("before") or {}).get("outcome") or {}).get("kind") for c in changes):
        return "independent_obligation", (f"related rule “{title}” is another tier of the same decision with its own threshold ({_value_words(own[0]['value'])}"
                                          f"{' ' + own[0]['unit'] if own[0].get('unit') else ''}); this change keeps the order of the two tiers and does not alter it; "
                                          "no action for this change")
    if own:
        return "independent_obligation", (f"related rule “{title}” is a separate obligation on the same measure with its own threshold ({_value_words(own[0]['value'])}"
                                          f"{' ' + own[0]['unit'] if own[0].get('unit') else ''}); this change does not alter it and nothing requires the two to match; "
                                          "no action for this change")
    how = sorted({_EDGE_ABOUT[e["type"]] for e in path if e.get("type") in _EDGE_ABOUT}) or ["it is listed by the impact report"]
    return "same_topic_only", (f"related rule “{title}” shares only the topic with the changed rule ({'; '.join(how)}); nothing links its condition to the change; "
                               "no action for this change")


def build_draft(intent: Mapping, report: Mapping, rules: Mapping[str, Mapping], relations: Iterable[Mapping], *, pin: Mapping, artifacts: Mapping[str, str],
                observations: Iterable[Mapping], proposed_dependencies: Optional[Iterable[Mapping]] = None, dependency_state: Optional[Mapping] = None) -> dict:
    ops = [dict(o) for o in intent["operations"]]
    changes = rule_changes(ops, rules)
    by_rule = {c["rule_id"]: c for c in changes}
    items = file_actions(report, ops, rules, artifacts, observations)
    unresolved = unresolved_items(report, rules, 0)
    relations = list(relations)
    affected = {i["rule_id"]: i for i in report.get("affected_rules") or []}
    edges = {e["edge_id"]: e for e in relations if e.get("edge_id")}
    holds = server_holds(changes, rules, relations, proposed_dependencies or [], report.get("counterevidence_rule_ids") or [])
    held_by = {}
    for h in holds:
        held_by.setdefault(h["other_rule_id"], h)
    for it in items:
        it["link_kind"], it["about"] = _item_link(it, rules, changes, affected, edges, held_by)
    held_rules = {h["rule_id"] for h in holds}
    for it in items:                                                      # the automatic edits of a held rule wait for the person's decision
        if it["action"] == "AI_EDIT_SUPPORTED" and set(it.get("rule_ids") or []) & held_rules:
            it.update(server_action="AI_EDIT_SUPPORTED", action="MANUAL", reason="server_hold", held_edit=it["proposal_edit"], proposal_edit=None)
    unresolved = unresolved + holds
    outcomes = [_op_outcome(o, by_rule[o["rule_id"]], items, unresolved, rules) for o in ops]
    for oc in outcomes:
        oc["simulation"] = next((s for s in report["simulation"] if s["op_id"] == oc["op_id"]), None)
    rel = relation_updates(relations, ops, rules)
    counter = [{"rule_id": rid, "title": rules[rid]["title"] if rid in rules else rid, "label": "disagrees with a changed rule; kept as read-only evidence; not changed by this draft"}
               for rid in report["counterevidence_rule_ids"]]
    checks = dependency_checks(changes, rules, proposed_dependencies or [])
    deps = [dict(d, label=PROPOSED_LABEL, **(checks.get(d.get("dep_id")) or {"check": None, "text": None})) for d in (proposed_dependencies or []) if d.get("status") == "PROPOSED"]
    open_items = [i for i in items if i["action"] in ("UNRESOLVED", "MANUAL")] + unresolved
    invalid = [c for c in changes if not c["valid"]]
    guidance_state = "not_requested"
    coverage = dict(report["coverage"])
    coverage.update({"file_actions": {a: sum(1 for i in items if i["action"] == a) for a in ACTIONS}, "unresolved_items": len(unresolved) + sum(1 for i in items if i["action"] == "UNRESOLVED"),
                     "operations": len(ops), "operations_with_complete_file_coverage": sum(1 for o in outcomes if o["file_coverage"] == "complete"),
                     "deterministic_complete": bool(report["coverage"]["complete"]), "dependencies_complete": bool((dependency_state or {}).get("complete")),
                     "server_holds": len(holds), "guidance": guidance_state})
    coverage["complete"] = bool(coverage["deterministic_complete"] and coverage["dependencies_complete"] and not open_items and not invalid and all(o["file_coverage"] == "complete" for o in outcomes))
    status = "INVALID" if invalid else ("READY_FOR_REVIEW" if not open_items else "PARTIAL")
    semantic = {"pin": dict(pin), "operations": ops, "rule_changes": [{"rule_id": c["rule_id"], "before": c["before"], "after": c["after"]} for c in changes], "relation_updates": rel}
    draft = {"draft_version": DRAFT_VERSION, "statement": STATEMENT, "applies_nothing": True, "pin": dict(pin), "intent_id": intent["intent_id"], "request": intent["request"],
             "write_scope": list(report["write_scope"]), "report_hash": report["report_hash"], "status": status, "operations": outcomes, "rule_changes": changes, "relation_updates": rel,
             "file_actions": items, "unresolved": unresolved, "counterevidence": counter, "excluded": list(report["excluded"]), "proposed_dependencies": deps,
             "dependency_state": dict(dependency_state or {"status": "not_requested"}), "measured": list(report["measured"]), "coverage": coverage,
             "semantic_diff_hash": canonical_hash(semantic), "guidance": {"status": guidance_state}}
    draft["draft_hash"] = canonical_hash(draft)
    return draft


# ---- model guidance: text over the server's items, never the items themselves -----------------------------------------------------------------------------

def validate_guidance_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != GUIDE_KEYS:
        raise ValueError("keys")
    if not isinstance(obj["summary"], str) or not isinstance(obj["items"], list) or not isinstance(obj["questions_for_human"], list):
        raise ValueError("types")
    if len(obj["items"]) > limits["max_items"] or len(obj["questions_for_human"]) > limits["max_questions"]:
        raise ValueError("sizes")
    for q in obj["questions_for_human"]:                                       # version 2: {question, needed, ref|null}; a bare string is read as advice
        if isinstance(q, str):
            continue
        if not isinstance(q, dict) or set(q) != GUIDE_QUESTION_KEYS or not isinstance(q["question"], str) or not isinstance(q["needed"], bool) \
                or not (q["ref"] is None or isinstance(q["ref"], str)):
            raise ValueError("questions")
    for it in obj["items"]:
        if not isinstance(it, dict) or set(it) != GUIDE_ITEM_KEYS or not isinstance(it["ref"], str) or not isinstance(it["instruction"], str) \
                or not isinstance(it["hold"], bool) or not isinstance(it["rationale"], str):
            raise ValueError("item_shape")
    return obj


def resolve_guidance(obj: Mapping, draft: Mapping, limits: Mapping) -> dict:
    """Keep only guidance about refs the server numbered, in plain text. A `hold` is recorded; what it does is decided in `merge_guidance` (downgrade only)."""
    refs = {i["ref"]: i for i in list(draft["file_actions"]) + list(draft["unresolved"])}
    accepted, rejected, seen = [], [], set()
    for it in obj["items"]:
        ref = it["ref"]
        if ref not in refs:
            rejected.append({"ref": ref[:40], "reason": "ref_not_in_draft"})
            continue
        if ref in seen:
            rejected.append({"ref": ref, "reason": "duplicate"})
            continue
        text = _plain(it["instruction"], limits["max_instruction_chars"])
        why = _plain(it["rationale"], limits["max_rationale_chars"]) if it["rationale"].strip() else ""
        if text is None or why is None:
            rejected.append({"ref": ref, "reason": "text_not_plain"})
            continue
        seen.add(ref)
        accepted.append({"ref": ref, "instruction": text, "hold": bool(it["hold"]), "rationale": why})
    summary = _plain(obj["summary"], limits["max_summary_chars"]) or ""
    held = {a["ref"] for a in accepted if a["hold"]}
    questions, question_items, advice = [], [], []
    for x in obj["questions_for_human"]:
        text = _plain(x if isinstance(x, str) else x["question"], limits["max_question_chars"])
        if not text:
            continue
        # a question blocks the knowledge decision only when the model says the change needs it answered AND it names an item the server numbered AND the server can
        # see that item is open: its action is MANUAL / UNRESOLVED, or the model holds it in this same guidance. A "needed" question on an item whose edit the model
        # lets proceed is inconsistent; a check the person may want to make ("have you verified ...") is advice. Advice is shown, never a blocker (2026-10-22, JOURNEY-V4-J4).
        ref = x.get("ref") if isinstance(x, dict) else None
        if not isinstance(x, dict) or not x["needed"]:
            why = "not needed to define the change" if isinstance(x, dict) else "no numbered item named"
        elif ref not in refs:
            why = "no numbered item named"
        elif ref in held or refs[ref].get("action") in ("MANUAL", "UNRESOLVED") or "action" not in refs[ref]:
            why = None
        else:
            why = "the item it names is not held or open"
        if why is None:
            questions.append(text)
            question_items.append(ref)
        else:
            advice.append({"text": text, "ref": ref, "why_not_blocking": why})
    return {"summary": summary, "accepted": accepted, "rejected": rejected, "questions": questions, "question_items": question_items, "advice": advice,
            "not_addressed": sorted(set(refs) - seen)}


def merge_guidance(draft: Mapping, guidance: Mapping, *, state: Mapping) -> dict:
    """The draft with the model's text attached. A hold downgrades AI_EDIT_SUPPORTED to MANUAL (the proposal is kept for the reviewer as `held_edit`); nothing else changes.
    Items the model did not mention keep the server's action and are listed."""
    out = json.loads(json.dumps(draft, sort_keys=True, default=str))
    by_ref = {a["ref"]: a for a in guidance.get("accepted") or []}
    for coll in ("file_actions", "unresolved"):
        for it in out[coll]:
            g = by_ref.get(it["ref"])
            if g is None:
                continue
            it["model_instruction"], it["model_rationale"] = g["instruction"], g["rationale"]
            if g["hold"] and it["action"] == "AI_EDIT_SUPPORTED":
                it.update(server_action="AI_EDIT_SUPPORTED", action="MANUAL", reason="model_raised_a_concern", held_edit=it["proposal_edit"], proposal_edit=None)
    out["guidance"] = {**dict(state), "summary": guidance.get("summary") or "", "questions_for_human": list(guidance.get("questions") or []),
                       "question_items": list(guidance.get("question_items") or []), "advice_for_human": list(guidance.get("advice") or []),
                       "rejected": list(guidance.get("rejected") or []), "not_addressed": list(guidance.get("not_addressed") or [])}
    out["coverage"]["guidance"] = state.get("status")
    out["coverage"]["file_actions"] = {a: sum(1 for i in out["file_actions"] if i["action"] == a) for a in ACTIONS}
    open_items = [i for i in out["file_actions"] if i["action"] in ("UNRESOLVED", "MANUAL")] + out["unresolved"]
    out["coverage"]["complete"] = bool(out["coverage"]["complete"] and not open_items and state.get("status") == "completed")
    if out["status"] != "INVALID":
        out["status"] = "READY_FOR_REVIEW" if not open_items and not out["guidance"]["questions_for_human"] else "PARTIAL"
    out.pop("draft_hash", None)
    out["draft_hash"] = canonical_hash(out)
    return out


def build_guidance_prompt(frozen: Mapping, draft: Mapping, *, can_read_evidence: bool, max_chars: int) -> str:
    changes = [f"- {o['op_id']} ({o['kind']}): {o['what']} | outcome: rule {o['rule_change']}, files complete={o['file_coverage']}" for o in draft["operations"]]
    rules = [f"- {c['title']}\n  before: {c['before_text']}\n  after:  {c['after_text']}" + ("" if c["valid"] else f"\n  REFUSED: {c['error']}") for c in draft["rule_changes"]]
    for d in draft.get("proposed_dependencies") or []:                  # the completed dependency step: what a model suggested depends on a changed rule (not confirmed)
        rules.append(f"- related (suggested by a model, not confirmed): {d.get('from_title') or d.get('from_rule_id')} {d.get('relation')} {d.get('to_title') or d.get('to_rule_id')}"
                     + (f" | server check: {d['text']}" if d.get("text") else ""))
    st = draft.get("dependency_state") or {}
    if st.get("status") and st["status"] != "completed":
        rules.append(f"- dependency check: {st['status']} ({st.get('reason') or 'not run'})")
    lines, used = [], 0
    for it in list(draft["file_actions"]) + list(draft["unresolved"]):
        if it["ref"].startswith("F"):
            line = f"- ref={it['ref']} kind={it['kind']} role={it['role']} action={it['action']} reason={it['reason']}" + (f"\n  about: {it['about']}" if it.get("about") else "")
            if can_read_evidence:
                line += f"\n  file: {it['path']}" + (f" at {it['locator']}" if it.get("locator") else "") + (f"\n  stored: {it['stored_text'][:160]}" if it.get("stored_text") else "")
        elif it["kind"] == "server_hold":                              # business-level: rule titles and values, no file or location; a person must decide
            line = f"- ref={it['ref']} action=UNRESOLVED HOLD (a person decides) reason={it['reason']} basis={it.get('basis')}: {it['instruction']}"
        else:
            line = f"- ref={it['ref']} action=UNRESOLVED reason={it['reason']}: {it['gap_text'] if can_read_evidence else it['reason']}"
        if used + len(line) > max_chars:
            lines.append("- (more items not shown)")
            break
        lines.append(line)
        used += len(line)
    return (frozen["prompt_template"].replace("{request}", str(draft["request"])[:1500]).replace("{changes}", "\n".join(changes) or "(none)")
            .replace("{rules}", "\n".join(rules) or "(none)").replace("{items}", "\n".join(lines) or "(none)"))


# ---- clarifications by people, blockers of a knowledge decision, the file-write binding (CHG-04) ------------------------------------------------------------

DISPOSITIONS = ("answered", "outside_this_request", "manual_later", "resolved_by_person")
_DISPOSITION_WORDS = {"answered": "answered by a person", "outside_this_request": "a person says this is not part of this change (labelled, not resolved)",
                      "manual_later": "a person will do this after the knowledge change is committed", "resolved_by_person": "a person states where or what it is (a statement, not an edit)"}


def question_refs(draft: Mapping) -> list:
    """Stable refs of the guidance questions, so a clarification can answer one: Q001.. in the order the guidance lists them."""
    return [f"Q{n + 1:03d}" for n, _ in enumerate((draft.get("guidance") or {}).get("questions_for_human") or [])]


def open_refs(draft: Mapping) -> dict:
    """ref -> what it is, for every item a person may clarify: open file actions, unresolved items, guidance questions."""
    out = {}
    for it in draft["file_actions"]:
        if it["action"] in ("UNRESOLVED", "MANUAL"):
            out[it["ref"]] = {"kind": "file_action", "text": it["instruction"], "rule_ids": it["rule_ids"]}
    for u in draft["unresolved"]:
        out[u["ref"]] = {"kind": u["kind"], "text": u["instruction"], "rule_ids": [u["rule_id"]] if u.get("rule_id") else []}
    for ref, q in zip(question_refs(draft), (draft.get("guidance") or {}).get("questions_for_human") or []):
        out[ref] = {"kind": "question", "text": q, "rule_ids": list(draft["write_scope"])}
    return out


def merge_clarifications(draft: Mapping, clarifications: Iterable[Mapping]) -> dict:
    """The draft with people's clarifications attached to the items they name (latest per ref wins; earlier ones kept in `history`). A clarification labels or answers; it never
    changes an action, removes an item or becomes an edit. It is a revision of the draft (new draft_hash)."""
    out = json.loads(json.dumps(draft, sort_keys=True, default=str))
    rows = [dict(c) for c in clarifications]
    by_ref: dict = {}
    for c in rows:
        by_ref.setdefault(c["ref"], []).append(c)
    refs = open_refs(out)
    attached = 0
    for coll in ("file_actions", "unresolved"):
        for it in out[coll]:
            hist = by_ref.get(it["ref"])
            if hist:
                last = hist[-1]
                it["clarification"] = {"disposition": last["disposition"], "label": _DISPOSITION_WORDS.get(last["disposition"], last["disposition"]), "answer": last.get("answer"),
                                       "by": last["actor_id"], "decision_id": last["decision_id"], "history": len(hist)}
                attached += 1
    questions = []
    for ref, q in zip(question_refs(out), (out.get("guidance") or {}).get("questions_for_human") or []):
        hist = by_ref.get(ref)
        row = {"ref": ref, "question": q}
        if hist:
            last = hist[-1]
            row["clarification"] = {"disposition": last["disposition"], "label": _DISPOSITION_WORDS.get(last["disposition"], last["disposition"]), "answer": last.get("answer"), "by": last["actor_id"],
                                    "decision_id": last["decision_id"], "history": len(hist)}
            attached += 1
        questions.append(row)
    out["questions"] = questions
    out["clarifications"] = {"count": len(rows), "attached": attached, "unknown_refs": sorted({c["ref"] for c in rows} - set(refs) - set(question_refs(out)))}
    out.pop("draft_hash", None)
    out["draft_hash"] = canonical_hash(out)
    return out


def semantic_blockers(draft: Mapping, *, acknowledged_flags: bool) -> list:
    """What stops accepting the knowledge change: an invalid after state, an unacknowledged HIGH flag, an open guidance question, an unmapped changed rule without a
    clarification. File-level manual or unresolved items are pending implementation, not blockers of the knowledge decision."""
    out = []
    for c in draft["rule_changes"]:
        if not c["valid"]:
            out.append({"code": "invalid_after_state", "ref": c["rule_id"], "text": f"“{c['title']}”: the changed rule is not valid ({c['error']})."})
    if not acknowledged_flags:
        for o in draft["operations"]:
            for f in o["flags"]:
                if f["severity"] == "HIGH":
                    out.append({"code": "flag_not_acknowledged", "ref": o["op_id"], "text": f"“{o['title']}”: {f['text']} The approver must acknowledge it."})
    about = (draft.get("guidance") or {}).get("question_items") or []
    for n, q in enumerate(draft.get("questions") or [{"ref": r, "question": t} for r, t in zip(question_refs(draft), (draft.get("guidance") or {}).get("questions_for_human") or [])]):
        if not q.get("clarification"):
            item = about[n] if n < len(about) else None
            out.append({"code": "open_question", "ref": q["ref"], "about": item, "text": f"Open question{f' about {item}' if item else ''}: {q['question']}"})
    for u in draft["unresolved"]:
        if u["kind"] == "no_source_mapping" and u.get("rule_id") in draft["write_scope"] and not u.get("clarification"):
            out.append({"code": "unmapped_changed_rule", "ref": u["ref"], "text": u["instruction"]})
        if u["kind"] == "server_hold" and not u.get("clarification"):
            out.append({"code": "server_hold", "ref": u["ref"], "about": u.get("hold"), "text": u["instruction"]})
    return out


def edits_hash(draft: Mapping) -> Optional[str]:
    """Binding of a file-write approval: the current AI_EDIT_SUPPORTED proposal edits (path, locator, edit). None when there is nothing to approve."""
    edits = [{"ref": i["ref"], "path": i["path"], "locator": i["locator"], "edit": i["proposal_edit"]} for i in draft["file_actions"] if i["action"] == "AI_EDIT_SUPPORTED"]
    return canonical_hash(edits) if edits else None


# ---- what this reader may see --------------------------------------------------------------------------------------------------------------------------

def _clarification_view(c, can_read_evidence: bool):
    if not c:
        return None
    return {"disposition": c["disposition"], "label": c["label"], "by": c["by"], "answer": c.get("answer") if can_read_evidence or c["disposition"] != "resolved_by_person" else None,
            "history": c.get("history", 1)}


def render(draft: Mapping, *, mode: str, can_read_evidence: bool, guidance_written_with_evidence: bool = False) -> dict:
    """Business: titles, before/after in words, outcomes, counts and labels; no ids, no paths. File paths, locators, instructions and code recommendations need `evidence.read`.
    Guidance written under evidence rights is withheld from a reader without them. Technical adds ids, hashes, pin and (with evidence rights) the full items."""
    hide_guidance = guidance_written_with_evidence and not can_read_evidence
    ops = []
    for o in draft["operations"]:
        ops.append({"what": o["what"], "rule": o["title"], "rule_change": o["rule_change"], "error": o["error"], "files": o["files"], "file_coverage": o["file_coverage"],
                    "flags": [f["text"] for f in o["flags"]], "simulation": {"status": o["simulation"]["status"], "reason": o["simulation"]["reason"]} if o.get("simulation") else None})
    files = []
    for it in draft["file_actions"]:
        row = {"ref": it["ref"], "kind": it["kind"], "role": it["role"], "action": it["action"], "reason": it["reason"], "code_recommendation": it["code_recommendation"],
               "clarification": _clarification_view(it.get("clarification"), can_read_evidence)}
        if can_read_evidence:
            row.update(path=it["path"], locator=it["locator"], instruction=it["instruction"], proposal_edit=it["proposal_edit"], resolution=it["resolution"], rebound_by_path=it["rebound_by_path"],
                       stored_text=it["stored_text"], held_edit=it.get("held_edit"))
            if not hide_guidance:
                row.update(model_instruction=it.get("model_instruction"), model_rationale=it.get("model_rationale"))
            else:
                row["model_instruction_withheld"] = bool(it.get("model_instruction"))
        files.append(row)
    unresolved = []
    for u in draft["unresolved"]:
        row = {"ref": u["ref"], "kind": u["kind"], "action": "UNRESOLVED", "clarification": _clarification_view(u.get("clarification"), can_read_evidence)}
        if u["kind"] == "server_hold":
            row.update(hold=u.get("hold"), basis=u.get("basis"), other_rule=u.get("other_rule_id"))
        if can_read_evidence or u["kind"] in ("graph_traversal_limited", "server_hold"):
            row["instruction"] = u["instruction"]
        else:
            row["instruction"] = "A location or relation is unknown; details need evidence rights."
        if can_read_evidence and not hide_guidance:
            row["model_instruction"] = u.get("model_instruction")
        unresolved.append(row)
    g = dict(draft.get("guidance") or {})
    guidance = {"status": g.get("status", "not_requested"), "reason": g.get("reason"), "summary": None if hide_guidance else g.get("summary"), "summary_withheld": hide_guidance and bool(g.get("summary")),
                "questions_for_human": [] if hide_guidance else list(g.get("questions_for_human") or []),
                "advice_for_human": [] if hide_guidance else list(g.get("advice_for_human") or []),           # checks the person may want to make: shown, never blockers
                "not_addressed_count": len(g.get("not_addressed") or []), "rejected_count": len(g.get("rejected") or [])}
    out = {"statement": draft["statement"], "status": draft["status"], "operations": ops,
           "rule_changes": [{"title": c["title"], "before": c["before_text"], "after": c["after_text"], "changes": [d["text"] for d in c["diff"]], "aspects": semantic_aspects(c["diff"]), "valid": c["valid"], "error": c["error"],
                             "flags": [f["text"] for f in c["flags"]]} for c in draft["rule_changes"]],
           "relation_updates": [{"type": r["type"], "action": r["action"], "text": r["text"], "status": r["status"]} for r in draft["relation_updates"]],
           "counterevidence": [{"title": c["title"], "label": c["label"]} for c in draft["counterevidence"]], "excluded": [e["text"] for e in draft["excluded"]],
           "proposed_dependencies": [{"from": d.get("from_title", d.get("from_rule_id")), "to": d.get("to_title", d.get("to_rule_id")), "relation": d["relation"], "status": "PROPOSED", "label": d["label"],
                                      "check": d.get("check"), "check_text": d.get("text")} for d in draft["proposed_dependencies"]],
           "dependency_state": {"status": draft["dependency_state"].get("status"), "reason": draft["dependency_state"].get("reason")},
           "files": files if can_read_evidence else [], "files_withheld": 0 if can_read_evidence else len(files), "file_action_counts": draft["coverage"]["file_actions"],
           "unresolved": unresolved, "measured": draft["measured"], "coverage": draft["coverage"], "guidance": guidance,
           "questions": [{"ref": q["ref"], "question": q["question"], "clarification": _clarification_view(q.get("clarification"), can_read_evidence)}
                         for q in (draft.get("questions") or [{"ref": r, "question": t} for r, t in zip(question_refs(draft), (draft.get("guidance") or {}).get("questions_for_human") or [])])]
                        if not hide_guidance else [],
           "clarifications": dict(draft.get("clarifications") or {"count": 0, "attached": 0, "unknown_refs": []})}
    if mode == "technical":
        out["technical"] = {"pin": draft["pin"], "intent_id": draft["intent_id"], "write_scope": draft["write_scope"], "report_hash": draft["report_hash"], "semantic_diff_hash": draft["semantic_diff_hash"],
                            "draft_hash": draft["draft_hash"], "draft_version": draft["draft_version"],
                            "operations": [{k: o[k] for k in ("op_id", "kind", "rule_id", "rule_change", "file_refs", "file_coverage", "error")} for o in draft["operations"]],
                            "rule_changes": [{"rule_id": c["rule_id"], "diff": c["diff"], "invariants": c["invariants"], "valid": c["valid"], "error": c["error"], "op_ids": c["op_ids"]} for c in draft["rule_changes"]],
                            "relation_updates": draft["relation_updates"], "counterevidence_rule_ids": [c["rule_id"] for c in draft["counterevidence"]],
                            "proposed_dependencies": [{k: d.get(k) for k in ("dep_id", "from_rule_id", "to_rule_id", "relation", "status", "provenance", "changes_write_scope", "check")} for d in draft["proposed_dependencies"]],
                            "guidance": {k: g.get(k) for k in ("job_id", "attempt", "result_hash", "integrity", "rejected", "not_addressed") if k in g}}
        if can_read_evidence:
            out["technical"]["file_actions"] = draft["file_actions"] if not hide_guidance else [{k: v for k, v in i.items() if not k.startswith("model_")} for i in draft["file_actions"]]
            out["technical"]["unresolved"] = draft["unresolved"]
            out["technical"]["rule_changes_full"] = [{"rule_id": c["rule_id"], "before": c["before"], "after": c["after"]} for c in draft["rule_changes"]]
    return out
