# -*- coding: utf-8 -*-
"""Carry the accepted business knowledge across a source revision (pure, no I/O, no model). Contract fkl/1 section 13; SYN-02 (and the entry point of F-01).

After a source refresh the OBSERVED rules may be out of date while the TARGET rules (decided by people) must survive. This module:
  * `reobserve_rules`: re-reads, for each OBSERVED rule, the structured scalar its condition is bound to (a YAML key, a cell) from the NEW revision's observations and
    updates the single numeric comparison leaf when the stored value changed (a deterministic read-back; documents and code are not re-read here: they stay as observed
    and are reported `not_reread`);
  * `fingerprint` and `implementation_status_for`: the TARGET rule's implementation status is derived by comparing it with the OBSERVED rule of the same root:
    `verified` (source-aligned: actor, normalized condition, outcome, exceptions and scope all equal; it says nothing about behaviour), `divergent` (the comparable
    condition differs), `partial` (condition equal, another component differs), `unverifiable` (no observed rule of that root); when the observed rule did not change
    since the previous snapshot the previous status is kept (no new information);
  * `rebase`: the accepted snapshot's rules with OBSERVED rules replaced by their new versions (stable rule ids), TARGET rules kept with recomputed status, and a report.
Relations and gaps are re-derived by the caller (`_merge_state`: confirmed relations and answered gaps carried). Nothing here overwrites a target or revives a revoked decision.
"""

from __future__ import annotations

import copy
import json
import hashlib
from typing import Callable, Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_knowledge as bk

STATUSES = ("verified", "partial", "divergent", "unverifiable", "pending")
MISSING_ARTIFACT = object()          # what `read_scalar` returns for a binding whose file is no longer in the source revision (distinct from "not a readable scalar" = None)


def fingerprint(rule: Mapping) -> str:
    cond = bk.normalize_condition(rule["condition"]) if rule.get("condition") is not None else None
    return hashlib.sha256(json.dumps([rule.get("actor_ref"), cond, (rule.get("outcome") or {}).get("kind"), [e["text"] for e in rule.get("exceptions") or []], rule.get("scope")],
                                     sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _components(rule: Mapping) -> dict:
    return {"actor": rule.get("actor_ref"), "condition": bk.normalize_condition(rule["condition"]) if rule.get("condition") is not None else None,
            "outcome": (rule.get("outcome") or {}).get("kind"), "exceptions": [e["text"] for e in rule.get("exceptions") or []], "scope": rule.get("scope")}


def implementation_status_for(target: Mapping, observed: Optional[Mapping], *, previous_observed: Optional[Mapping] = None) -> dict:
    """-> {status, differs: [component...], basis}. `previous_observed` = the observed rule of the same root in the snapshot being rebased; when the observed rule did not
    change, the target keeps its previous status (nothing new was read)."""
    if observed is None:
        return {"status": "unverifiable", "differs": [], "basis": "no observed rule of this root in the new sources"}
    if previous_observed is not None and fingerprint(previous_observed) == fingerprint(observed):
        return {"status": target.get("implementation_status") or "pending", "differs": [], "basis": "the observed rule did not change; the previous status is kept"}
    t, o = _components(target), _components(observed)
    differs = [k for k in ("actor", "condition", "outcome", "exceptions", "scope") if t[k] != o[k]]
    if not differs:
        return {"status": "verified", "differs": [], "basis": "the sources now state the decided rule (source-aligned; behaviour is not tested)"}
    if "condition" in differs:
        return {"status": "divergent", "differs": differs, "basis": "the sources state a different condition than the decided rule"}
    return {"status": "partial", "differs": differs, "basis": "the condition matches the decided rule; " + ", ".join(differs) + " differ(s)"}


def _numeric_leaves(cond) -> list:
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        v = cond.get("value")
        return [cond] if cond.get("op") in ("gt", "gte", "lt", "lte", "eq", "ne") and isinstance(v, (int, float, str)) and not isinstance(v, bool) and _num(v) is not None else []
    out = []
    for key in ("all", "any"):
        for c in cond.get(key, []):
            out += _numeric_leaves(c)
    if "not" in cond:
        out += _numeric_leaves(cond["not"])
    return out


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        return None


def _set_leaf(cond, old_leaf, value):
    if cond is old_leaf:
        out = dict(cond)
        out["value"] = value
        return out
    if not isinstance(cond, Mapping) or "field" in cond:
        return cond
    out = dict(cond)
    for key in ("all", "any"):
        if key in cond:
            out[key] = [_set_leaf(c, old_leaf, value) for c in cond[key]]
    if "not" in cond:
        out["not"] = _set_leaf(cond["not"], old_leaf, value)
    return out


def reobserve_rules(rules: Iterable[Mapping], read_scalar: Callable[[Mapping, Mapping], Optional[object]], *, new_revision_id: str) -> tuple[list, list]:
    """-> (rules, report). `read_scalar(rule, binding)` returns the scalar now stored at a structured binding (int/float/str) or None when the location is not a readable
    scalar or does not exist. Only an OBSERVED rule whose condition has exactly one numeric comparison and one readable scalar binding is updated; a changed value replaces
    that leaf's value (int when integral). Bindings that resolved get `revision_id = new_revision_id`. TARGET rules pass through untouched."""
    out, report = [], []
    for r in rules:
        rule = copy.deepcopy(dict(r))
        if rule.get("authority") != "OBSERVED" or rule.get("status") in ("SUPERSEDED", "REJECTED"):
            out.append(rule)
            continue
        leaves = _numeric_leaves(rule.get("condition"))
        readings, missing = [], []
        for b in rule.get("source_bindings") or []:
            value = read_scalar(rule, b)
            if value is MISSING_ARTIFACT:
                missing.append(b["evidence_ref"])
                continue
            if value is not None:
                readings.append((b, value))
                b["revision_id"] = new_revision_id
        entry = {"rule_id": rule["rule_id"], "readable_bindings": len(readings), "bindings": len(rule.get("source_bindings") or []), "changed": False, "status": "not_reread",
                 "missing_bindings": missing, "mapping_lost": bool(missing) and not readings}
        if len(leaves) == 1 and readings:
            values = {_num(v) for _, v in readings}
            if len(values) == 1:
                new = readings[0][1]
                if _num(new) is not None and _num(new) != _num(leaves[0]["value"]):
                    stored = int(new) if isinstance(new, (int, float)) and float(new).is_integer() else new
                    rule["condition"] = _set_leaf(rule["condition"], leaves[0], stored)
                    entry.update(changed=True, status="reobserved", before=leaves[0]["value"], after=stored)
                else:
                    entry["status"] = "unchanged"
            else:
                entry.update(status="conflicting_readings", values=sorted(str(v) for _, v in readings))
        elif readings:
            entry["status"] = "not_comparable"
        bk.validate_business_rule(rule)
        out.append(rule)
        report.append(entry)
    return out, report


def root_of(rule_id: str) -> str:
    return rule_id[:-7] if rule_id.endswith("-target") else rule_id


def revalidate(relations: Iterable[Mapping], gaps: Iterable[Mapping], *, changed_observed: Iterable[str], missing_observed: Iterable[str], changed_refs: Iterable[str],
               lost_mapping_targets: Iterable[Mapping]) -> dict:
    """Dependencies that moved under a confirmed relation or an answered question are re-opened for a person, never silently kept or dropped (history stays):
      * a CONFIRMED relation whose end is an observed rule that changed or disappeared -> PENDING_HUMAN (provenance kept), listed with its reason;
      * a RESOLVED / ACCEPTED_UNKNOWN gap whose evidence (a source location, not a decision) is a location whose read-back changed -> REOPENED, listed;
      * a TARGET rule whose root lost its mapping -> an `uncertain_link` question (stable id) so the person decides where it now lives.
    -> {relations, gaps, relations_recheck, gaps_reopened, uncertain_links}."""
    changed, missing, refs_changed = set(changed_observed), set(missing_observed), set(changed_refs)
    rel_out, rel_report = [], []
    for e in relations:
        e = dict(e)
        ends = {e["from"], e["to"]}
        if e["status"] == "CONFIRMED" and (ends & (changed | missing)):
            reason = "endpoint_missing" if ends & missing else "endpoint_changed"
            e["status"] = "PENDING_HUMAN"
            rel_report.append({"edge_id": e["edge_id"], "reason": reason, "ends": sorted(ends & (changed | missing))})
        rel_out.append(e)
    gap_out, gap_report = [], []
    for g in gaps:
        g = dict(g)
        if g["status"] in ("RESOLVED", "ACCEPTED_UNKNOWN"):
            refs = [str(r) for r in g.get("evidence_refs") or [] if not str(r).startswith("decision-")]
            hit = sorted(set(refs) & refs_changed)
            if hit:
                g["status"] = "REOPENED"
                gap_report.append({"gap_id": g["gap_id"], "reason": "evidence_changed", "evidence_refs": hit})
        gap_out.append(g)
    links = []
    existing = {g["gap_id"] for g in gap_out}
    for t in lost_mapping_targets:
        gid = bk.gap_id_for("uncertain_link", [t["rule_id"]], "mapping_lost")
        if gid in existing:
            continue
        gap = {"gap_id": gid, "kind": "uncertain_link", "status": "OPEN", "affected_rule_ids": [t["rule_id"]],
               "question": f"The source location that “{t['title']}” was read from is no longer in the sources. Where does this rule live now, or is it no longer implemented?",
               "evidence_refs": [b["evidence_ref"] for b in t.get("source_bindings") or []][:5], "requires_permission": "knowledge.confirm", "priority": "normal"}
        bk.validate_gap(gap)
        gap_out.append(gap)
        links.append({"gap_id": gid, "target_rule_id": t["rule_id"]})
    return {"relations": rel_out, "gaps": gap_out, "relations_recheck": rel_report, "gaps_reopened": gap_report, "uncertain_links": links}


def rebase(accepted_rules: Iterable[Mapping], new_observed: Iterable[Mapping], *, previous_observed: Optional[Iterable[Mapping]] = None) -> dict:
    """The rules of the carried-over snapshot: OBSERVED rules replaced by `new_observed` (by stable rule id; an observed rule that disappeared from the new sources is kept and
    marked), TARGET rules kept with `implementation_status` recomputed against the observed rule of the same root. -> {rules, carried_targets, statuses, missing_observed}."""
    new_by_id = {r["rule_id"]: copy.deepcopy(dict(r)) for r in new_observed}
    prev_by_id = {r["rule_id"]: r for r in (previous_observed or [])}
    rules, statuses, carried, missing = {}, [], [], []
    for r in accepted_rules:
        rid = r["rule_id"]
        if r.get("authority") == "TARGET":
            carried.append(rid)
            continue
        if rid in new_by_id:
            rules[rid] = new_by_id.pop(rid)
        else:
            rules[rid] = copy.deepcopy(dict(r))
            missing.append(rid)
    for rid, r in new_by_id.items():                                                     # observed rules the new sources added
        rules[rid] = r
    changed = sorted(rid for rid, r in rules.items() if r.get("authority") == "OBSERVED" and rid in prev_by_id and fingerprint(prev_by_id[rid]) != fingerprint(r))
    for r in accepted_rules:
        if r.get("authority") != "TARGET":
            continue
        t = copy.deepcopy(dict(r))
        root = root_of(t["rule_id"])
        observed = rules.get(root)
        verdict = implementation_status_for(t, observed, previous_observed=prev_by_id.get(root))
        t["implementation_status"] = verdict["status"]
        rules[t["rule_id"]] = t
        statuses.append({"target_rule_id": t["rule_id"], "root": root, **verdict})
    return {"rules": [rules[k] for k in sorted(rules)], "carried_targets": sorted(carried), "statuses": statuses, "missing_observed": sorted(missing), "changed_observed": changed}
