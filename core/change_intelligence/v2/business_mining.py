# -*- coding: utf-8 -*-
"""Semantic conflict mining over one knowledge version (extension AIX-K1, extends KNO-06): pure, no I/O, no model.

Grouping is deterministic: live rules that share at least one condition field form a component (union-find), components are split into groups of at most `max_group_rules`
rules, at most `max_groups` groups are compared (the rest is reported as skipped with the reason); a rule without a condition or alone in its component is skipped and
listed. The model compares the rules of ONE group and returns typed hypotheses with a concrete case; the server keeps a hypothesis only if every cited rule id was shown in
that group, the text is plain, the pair is not already asked or decided, and the case makes every cited rule apply under the deterministic policy evaluator (a case one rule
does not match is rejected; a case the evaluator cannot decide downgrades the hypothesis to `missing_context` with the missing facts as unknowns). A `potential_conflict`
whose rules agree on actor and outcome is not a conflict (rejected). Hypotheses are findings, never knowledge: a person dismisses them or raises them as questions through
the existing controls. The coverage report says what was and was not compared; NONE_FOUND is a coverage statement, never proof.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from core.change_intelligence.v2 import policy_decision as pd
from core.change_intelligence.v2.roi_replay import _fields_of
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-knowledge-mining/1"
MODEL_KEYS = {"status", "hypotheses", "notes"}
KINDS = ("potential_conflict", "exception_overlap", "ambiguity", "missing_context")
ORIGIN = "model_hypothesis"
_UNSAFE = re.compile(r"[<>`#*_\[\]|]|https?://|www\.|[\x00-\x08\x0b-\x1f\x7f]", re.I)
_LIVE_OUT = ("SUPERSEDED", "REJECTED")


# ---- grouping and coverage -------------------------------------------------------------------------------------------------------------------------------

def live_rules(rules: Iterable[Mapping]) -> list:
    effective, _ = pd.effective_rules([r for r in rules if r.get("status") not in _LIVE_OUT])
    return sorted(effective, key=lambda r: r["rule_id"])


def plan_groups(rules: Iterable[Mapping], limits: Mapping) -> dict:
    """-> {groups: [{group_id, rule_ids, fields}], skipped_rules: [{rule_id, reason}], skipped_groups: [{group_id, rule_ids, reason}], pairs_total, pairs_planned}."""
    rules = live_rules(rules)
    fields = {r["rule_id"]: _fields_of(r.get("condition")) | {f for e in r.get("exceptions") or [] for f in _fields_of(e.get("condition") if isinstance(e, Mapping) else None)} for r in rules}
    skipped = [{"rule_id": r["rule_id"], "reason": "no_condition"} for r in rules if not fields[r["rule_id"]]]
    with_cond = [r for r in rules if fields[r["rule_id"]]]
    parent = {r["rule_id"]: r["rule_id"] for r in with_cond}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_field: dict = {}
    for r in with_cond:
        for f in fields[r["rule_id"]]:
            by_field.setdefault(f, []).append(r["rule_id"])
    for ids in by_field.values():
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    components: dict = {}
    for r in with_cond:
        components.setdefault(find(r["rule_id"]), []).append(r["rule_id"])
    groups, skipped_groups = [], []
    n = 0
    for root in sorted(components, key=lambda k: min(components[k])):
        ids = sorted(components[root])
        if len(ids) < 2:
            skipped.append({"rule_id": ids[0], "reason": "no_rule_shares_a_field"})
            continue
        for i in range(0, len(ids), limits["max_group_rules"]):
            chunk = ids[i:i + limits["max_group_rules"]]
            gid = "grp-" + canonical_hash(chunk)[:12]
            entry = {"group_id": gid, "rule_ids": chunk, "fields": sorted(set().union(*(fields[x] for x in chunk)))}
            if len(chunk) < 2:
                skipped_groups.append(dict(entry, reason="single_rule_remainder"))
            elif n >= limits["max_groups"]:
                skipped_groups.append(dict(entry, reason="group_cap"))
            else:
                groups.append(entry)
                n += 1
    # cross-group pairs (bounded): two rules of DIFFERENT groups (or a rule alone in its component) that share a scope value or a concept are compared as a two-rule bridge group;
    # ranked by what they share, capped by max_bridge_pairs, every unplanned cross-group pair is reported, never silently dropped
    by_rule = {r["rule_id"]: r for r in with_cond}
    group_of = {rid: g["group_id"] for g in groups for rid in g["rule_ids"]}
    in_group_pairs = {frozenset(p) for g in groups for p in _pairs(g["rule_ids"])}
    scored = []
    ids = sorted(by_rule)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if frozenset((a, b)) in in_group_pairs:
                continue
            shared = _shared(by_rule[a], by_rule[b])
            if shared:
                scored.append((-len(shared), a, b, sorted(shared)))
    scored.sort()
    cap = int(limits.get("max_bridge_pairs", 4))
    bridges, bridge_skipped = [], []
    for k, (_, a, b, shared) in enumerate(scored):
        entry = {"group_id": "grp-b-" + canonical_hash([a, b])[:12], "rule_ids": [a, b], "fields": sorted(fields[a] | fields[b]), "kind": "bridge", "shared": shared,
                 "groups": sorted({group_of.get(a, "single"), group_of.get(b, "single")})}
        (bridges if k < cap else bridge_skipped).append(entry if k < cap else dict(entry, reason="bridge_cap"))
    skipped = [s for s in skipped if s["reason"] != "no_rule_shares_a_field" or not any(s["rule_id"] in b["rule_ids"] for b in bridges)]
    for g in groups:
        g.setdefault("kind", "component")
    pairs_total = len(with_cond) * (len(with_cond) - 1) // 2
    pairs_planned = sum(len(g["rule_ids"]) * (len(g["rule_ids"]) - 1) // 2 for g in groups) + len(bridges)
    unrelated = pairs_total - len(in_group_pairs) - len(scored)
    return {"groups": groups + bridges, "skipped_rules": sorted(skipped, key=lambda s: s["rule_id"]), "skipped_groups": skipped_groups + bridge_skipped, "rules_live": len(rules),
            "pairs_total": pairs_total, "pairs_planned": pairs_planned, "cross_group_pairs": {"planned": len(bridges), "skipped_by_cap": len(bridge_skipped), "unrelated": max(0, unrelated)}}


def _pairs(ids):
    ids = sorted(ids)
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]


def _shared(a: Mapping, b: Mapping) -> list:
    """What two rules have in common besides a condition field: scope values (`process:STANDARD`) and concept ids."""
    out = set()
    for key, va in (a.get("scope") or {}).items():
        vb = (b.get("scope") or {}).get(key)
        if isinstance(va, list) and isinstance(vb, list):
            out |= {f"{key}:{v}" for v in set(map(str, va)) & set(map(str, vb))}
    out |= set(a.get("concept_ids") or []) & set(b.get("concept_ids") or [])
    return sorted(out)


def group_job_id(snapshot_id: str, group_id: str, attempt: int) -> str:
    return "mine-" + canonical_hash({"snap": snapshot_id, "group": group_id, "attempt": attempt})[:24]


def coverage(plan: Mapping, group_states: Mapping) -> dict:
    """`group_states`: group_id -> completed | failed | running | waiting_for_approval | not_requested. Pairs compared = pairs of completed groups."""
    compared = sum(len(g["rule_ids"]) * (len(g["rule_ids"]) - 1) // 2 for g in plan["groups"] if group_states.get(g["group_id"]) == "completed")
    rules_compared = sorted({x for g in plan["groups"] if group_states.get(g["group_id"]) == "completed" for x in g["rule_ids"]})
    return {"rules_live": plan["rules_live"], "rules_compared": len(rules_compared), "rules_skipped": plan["skipped_rules"], "groups": [dict(g, state=group_states.get(g["group_id"], "not_requested")) for g in plan["groups"]],
            "groups_skipped": plan["skipped_groups"], "pairs_total": plan["pairs_total"], "pairs_planned": plan["pairs_planned"], "pairs_compared": compared, "cross_group_pairs": plan.get("cross_group_pairs"),
            "complete": compared == plan["pairs_total"] and all(group_states.get(g["group_id"]) == "completed" for g in plan["groups"]) and not plan["skipped_groups"],
            "statement": ("Rules that share a condition field were compared in bounded groups; across groups, only pairs that share a scope value or a concept were compared, up to a cap; "
                          "the rest of the pairs and the skipped rules were not looked at. Finding nothing in a group is not proof. "
                          "A hypothesis is a question for a person, never a fact.")}


# ---- prompt and validation ---------------------------------------------------------------------------------------------------------------------------

def build_prompt(frozen: Mapping, rules: Iterable[Mapping], asked: Iterable[str], fields: Mapping) -> str:
    from core.change_intelligence.v2.business_index import condition_text
    lines = []
    for r in rules:
        exc = "; ".join(e.get("text", "") if isinstance(e, Mapping) else str(e) for e in r.get("exceptions") or []) or "none recorded"
        scope = ", ".join(f"{k}: {'/'.join(map(str, v))}" for k, v in sorted((r.get("scope") or {}).items())) or "any"
        lines.append(f"- id={r['rule_id']} | {r.get('title')} | who acts: {r.get('actor_ref') or 'not stated'} | when: {condition_text(r['condition']) if r.get('condition') is not None else 'not stated'} | "
                     f"what happens: {(r.get('outcome') or {}).get('kind', 'unknown')} | exceptions: {exc} | scope: {scope} | {r.get('authority')}/{r.get('status')}")
    field_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(fields.items())) or "(none)"
    prompt = frozen["prompt_template"].replace("{rules}", "\n".join(lines) or "(none)").replace("{asked}", "\n".join(asked) or "(none)").replace("{fields}", field_lines)
    if len(prompt) > frozen["limits"]["max_prompt_chars"]:
        raise ValueError("context_too_large")
    return prompt


def allowed_fields(rules: Iterable[Mapping]) -> dict:
    """field -> description of the values a case may use: the literal values the group's conditions and scopes compare against."""
    out: dict = {}

    def walk(cond):
        if not isinstance(cond, Mapping):
            return
        if "field" in cond:
            vals = cond["value"] if isinstance(cond["value"], list) else [cond["value"]]
            out.setdefault(cond["field"], set()).update(str(v) for v in vals)
        for key in ("all", "any"):
            for c in cond.get(key) or []:
                walk(c)
        if "not" in cond:
            walk(cond["not"])
    for r in rules:
        walk(r.get("condition"))
        for e in r.get("exceptions") or []:
            if isinstance(e, Mapping):
                walk(e.get("condition"))
        for k, v in (r.get("scope") or {}).items():
            if isinstance(v, list):
                out.setdefault(k, set()).update(str(x) for x in v)
    return {k: "a number (for example near the thresholds " + ", ".join(sorted(v)) + ")" if all(_isnum(x) for x in v) else "one of " + ", ".join(sorted(v)) for k, v in out.items()}


def _isnum(x) -> bool:
    try:
        float(str(x).replace(",", ""))
        return True
    except ValueError:
        return False


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != MODEL_KEYS:
        raise ValueError("object keys")
    if obj["status"] not in ("OK", "NONE_FOUND", "UNSURE"):
        raise ValueError("status")
    if not isinstance(obj["hypotheses"], list) or len(obj["hypotheses"]) > limits["max_hypotheses"]:
        raise ValueError("hypotheses")
    for h in obj["hypotheses"]:
        if not isinstance(h, dict) or set(h) != {"kind", "rule_ids", "reason", "case", "question", "unknowns"}:
            raise ValueError("hypothesis keys")
        if h["kind"] not in KINDS or not isinstance(h["rule_ids"], list) or not all(isinstance(x, str) for x in h["rule_ids"]) or not 1 <= len(h["rule_ids"]) <= 3:
            raise ValueError("hypothesis kind or rule_ids")
        if not isinstance(h["reason"], str) or len(h["reason"]) > limits["max_reason_chars"] or not isinstance(h["question"], str) or len(h["question"]) > limits["max_question_chars"]:
            raise ValueError("hypothesis text limits")
        if not isinstance(h["case"], dict) or len(h["case"]) > limits["max_case_fields"] or not all(isinstance(k, str) and isinstance(v, (str, int, float)) and not isinstance(v, bool) for k, v in h["case"].items()):
            raise ValueError("hypothesis case")
        if not isinstance(h["unknowns"], list) or len(h["unknowns"]) > limits["max_unknowns"] or not all(isinstance(u, str) and len(u) <= limits["max_unknown_chars"] for u in h["unknowns"]):
            raise ValueError("hypothesis unknowns")
    if not isinstance(obj["notes"], list) or len(obj["notes"]) > limits["max_notes"] or not all(isinstance(n, str) and len(n) <= 300 for n in obj["notes"]):
        raise ValueError("notes")
    return obj


def hypothesis_id(snapshot_id: str, kind: str, rule_ids: Iterable[str], case: Mapping) -> str:
    return "hyp-" + canonical_hash({"snap": snapshot_id, "kind": kind, "rules": sorted(rule_ids), "case": {k: str(v) for k, v in sorted(case.items())}})[:20]


def resolve(model: Mapping, group_rules: Mapping[str, Mapping], asked_pairs: Iterable[Iterable[str]], *, snapshot_id: str, group_id: str) -> dict:
    """Keep what the group's rules support. `asked_pairs`: rule-id sets of gaps already asked or decided (any status) -> `already_asked`."""
    asked = {frozenset(p) for p in asked_pairs}
    kept, rejected, seen = [], [], set()
    for h in model["hypotheses"]:
        rids = sorted(set(h["rule_ids"]))
        reason = None
        if any(x not in group_rules for x in rids):
            reason = "rule_not_in_group"
        elif len(rids) < 2 and h["kind"] != "missing_context":
            reason = "needs_two_rules"
        elif _UNSAFE.search(h["reason"]) or _UNSAFE.search(h["question"]) or any(_UNSAFE.search(u) for u in h["unknowns"]) or any(_UNSAFE.search(str(v)) for v in h["case"].values()):
            reason = "unsafe_text"
        elif not h["reason"].strip() or not h["question"].strip():
            reason = "empty_text"
        elif frozenset(rids) in asked:
            reason = "already_asked"
        if reason:
            rejected.append({"kind": h["kind"], "rule_ids": rids, "reason": reason})
            continue
        kind, unknowns, verified = h["kind"], list(h["unknowns"]), None
        if kind in ("potential_conflict", "exception_overlap"):
            if not h["case"]:
                kind, verified = "missing_context", False
                unknowns.append("no concrete case was given; the overlap could not be checked")
            else:
                verdicts = {rid: _applies(group_rules[rid], h["case"]) for rid in rids}
                if any(v[0] is False for v in verdicts.values()):
                    rejected.append({"kind": h["kind"], "rule_ids": rids, "reason": "case_not_shared:" + ",".join(r for r, v in sorted(verdicts.items()) if v[0] is False)})
                    continue
                if any(v[0] is None for v in verdicts.values()):
                    kind, verified = "missing_context", False
                    unknowns += sorted({w for v in verdicts.values() for w in v[1]})[:4]
                else:
                    verified = True
                    if kind == "potential_conflict" and len({(group_rules[r].get("actor_ref"), (group_rules[r].get("outcome") or {}).get("kind")) for r in rids}) == 1:
                        rejected.append({"kind": h["kind"], "rule_ids": rids, "reason": "no_disagreement"})
                        continue
        key = (kind, tuple(rids))
        if key in seen:
            rejected.append({"kind": kind, "rule_ids": rids, "reason": "duplicate"})
            continue
        seen.add(key)
        kept.append({"hypothesis_id": hypothesis_id(snapshot_id, kind, rids, h["case"]), "kind": kind, "kind_proposed": h["kind"], "rule_ids": rids, "titles": [group_rules[r].get("title") for r in rids],
                     "reason": " ".join(h["reason"].split()), "case": {k: v for k, v in sorted(h["case"].items())}, "case_verified": verified, "question": " ".join(h["question"].split()),
                     "unknowns": unknowns[:6], "group_id": group_id, "origin": ORIGIN, "status": "PROPOSED"})
    return {"hypotheses": kept, "rejected": rejected, "notes": [" ".join(n.split()) for n in model["notes"] if not _UNSAFE.search(n)]}


def _applies(rule: Mapping, case: Mapping) -> tuple:
    scope_ok, why_scope = pd._scope_applies(rule, case)
    if scope_ok is False:
        return False, []
    cond_ok, why = pd.evaluate_condition(rule.get("condition"), case)
    if cond_ok is False:
        return False, []
    if cond_ok is None or scope_ok is None:
        return None, why + why_scope
    return True, []
