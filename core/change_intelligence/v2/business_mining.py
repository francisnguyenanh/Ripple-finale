# -*- coding: utf-8 -*-
"""Semantic conflict mining over one knowledge version (extension AIX-K1, extends KNO-06): pure, no I/O, no model.

Grouping is deterministic: live rules that share at least one condition field form a component (union-find), components are split into groups of at most `max_group_rules`
rules, at most `max_groups` groups are compared (the rest is reported as skipped with the reason); a rule without a condition or alone in its component is skipped and
listed. The model compares the rules of ONE group and returns typed hypotheses with a concrete case; the server keeps a hypothesis only if every cited rule id was shown in
that group, the text is plain, the pair is not already asked or decided, and the case makes every cited rule apply under the deterministic policy evaluator (a case one rule
does not match is rejected; a case the evaluator cannot decide downgrades the hypothesis to `missing_context` with the missing facts as unknowns). A `potential_conflict`
whose rules agree on actor and outcome is not a conflict (rejected). Hypotheses are findings, never knowledge: a person dismisses them or raises them as questions through
the existing controls. The coverage report says what was and was not compared; NONE_FOUND is a coverage statement, never proof.

LHA-K1 (2026-10-18): (1) semantic candidates: one model call over the offered pack (live rules with a condition, capped and reported) may propose rule pairs that share a
business concept without sharing a field or scope value; the server keeps a pair only if both ids were offered, the pair is not planned already, it is not a duplicate and
the cap is not reached; a kept pair becomes a two-rule `semantic` group compared like any other group. A candidate pair is never a relation or a finding. (2) compatibility
rubric for a verified potential_conflict: two obligations that can both be met (two approvals by different people, a review and a report) are not a conflict
(`compatible_obligations`); a terminal outcome (rejected, prohibited) against an obligation is `incompatible` and stays a potential conflict; an outcome the rubric does not
know downgrades the hypothesis to `ambiguity` with compatibility `unknown` (a question for a person, neither a conflict nor safe). The model's kind and the server's
disposition are both kept.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

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


def plan_groups(rules: Iterable[Mapping], limits: Mapping, semantic_pairs: Iterable[Mapping] = ()) -> dict:
    """-> {groups: [{group_id, rule_ids, fields}], skipped_rules: [{rule_id, reason}], skipped_groups: [{group_id, rule_ids, reason}], pairs_total, pairs_planned}.
    `semantic_pairs`: pairs kept from the semantic candidate job ({rule_ids, link}); each live pair not planned already becomes a two-rule `semantic` group."""
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
    planned_pairs = in_group_pairs | {frozenset(b["rule_ids"]) for b in bridges}
    semantic = []
    for sp in semantic_pairs:
        a, b = sorted(sp["rule_ids"])
        if a in by_rule and b in by_rule and frozenset((a, b)) not in planned_pairs:
            planned_pairs.add(frozenset((a, b)))
            semantic.append({"group_id": "grp-s-" + canonical_hash([a, b])[:12], "rule_ids": [a, b], "fields": sorted(fields[a] | fields[b]), "kind": "semantic", "link": sp.get("link", ""),
                             "groups": sorted({group_of.get(a, "single"), group_of.get(b, "single")})})
    bridges += semantic
    skipped = [s for s in skipped if s["reason"] != "no_rule_shares_a_field" or not any(s["rule_id"] in b["rule_ids"] for b in bridges)]
    for g in groups:
        g.setdefault("kind", "component")
    pairs_total = len(with_cond) * (len(with_cond) - 1) // 2
    pairs_planned = sum(len(g["rule_ids"]) * (len(g["rule_ids"]) - 1) // 2 for g in groups) + len(bridges)
    unrelated = pairs_total - len(in_group_pairs) - len(scored) - len(semantic)
    return {"groups": groups + bridges, "skipped_rules": sorted(skipped, key=lambda s: s["rule_id"]), "skipped_groups": skipped_groups + bridge_skipped, "rules_live": len(rules),
            "pairs_total": pairs_total, "pairs_planned": pairs_planned,
            "cross_group_pairs": {"planned": len(bridges) - len(semantic), "skipped_by_cap": len(bridge_skipped), "unrelated": max(0, unrelated), "semantic": len(semantic)}}


# ---- semantic candidates (LHA-K1) -------------------------------------------------------------------------------------------------------------------------

CANDIDATE_KEYS = {"status", "pairs", "notes"}
CANDIDATE_LINKS = ("same_concept", "overlapping_case", "dependency", "equivalent")


def candidate_offer(rules: Iterable[Mapping], limits: Mapping) -> dict:
    """The offered pack of the candidate call: live rules with a condition, by id, capped; the rest is reported as not offered."""
    live = [r for r in live_rules(rules) if r.get("condition") is not None]
    cap = int(limits.get("max_candidate_rules", 30))
    return {"rules": live[:cap], "not_offered": [r["rule_id"] for r in live[cap:]], "rules_live_with_condition": len(live)}


def candidate_job_id(snapshot_id: str, attempt: int, binding: Optional[str] = None) -> str:
    return "mcand-" + canonical_hash({"snap": snapshot_id, "attempt": attempt, "stage": "candidates", **({"binding": binding} if binding else {})})[:24]


def structural_pairs(plan: Mapping) -> list:
    """Rule pairs the deterministic plan already compares (groups and bridges, not semantic groups)."""
    return [list(p) for g in plan["groups"] if g.get("kind") != "semantic" for p in _pairs(g["rule_ids"])]


def build_candidate_prompt(frozen: Mapping, rules: Iterable[Mapping], planned: Iterable[Iterable[str]]) -> str:
    from core.change_intelligence.v2.business_index import condition_text
    lines = [f"- id={r['rule_id']} | {r.get('title')} | who acts: {r.get('actor_ref') or 'not stated'} | when: {condition_text(r['condition'])} | "
             f"what happens: {(r.get('outcome') or {}).get('kind', 'unknown')}" for r in rules]
    pairs = "\n".join(f"- {', '.join(p)}" for p in sorted(sorted(p) for p in planned)) or "(none)"
    prompt = frozen["candidate_prompt_template"].replace("{rules}", "\n".join(lines) or "(none)").replace("{planned}", pairs)
    if len(prompt) > frozen["limits"]["max_prompt_chars"]:
        raise ValueError("context_too_large")
    return prompt


def validate_candidate_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != CANDIDATE_KEYS or obj["status"] not in ("OK", "NONE_FOUND"):
        raise ValueError("object keys or status")
    if not isinstance(obj["pairs"], list) or len(obj["pairs"]) > limits["max_candidate_proposals"]:
        raise ValueError("pairs")
    for c in obj["pairs"]:
        if not isinstance(c, dict) or set(c) != {"rule_ids", "relation", "link"} or not isinstance(c["rule_ids"], list) or len(c["rule_ids"]) != 2 \
                or not all(isinstance(x, str) for x in c["rule_ids"]) or c["relation"] not in CANDIDATE_LINKS or not isinstance(c["link"], str) or len(c["link"]) > limits["max_reason_chars"]:
            raise ValueError("pair shape")
    if not isinstance(obj["notes"], list) or len(obj["notes"]) > limits["max_notes"] or not all(isinstance(n, str) and len(n) <= 300 for n in obj["notes"]):
        raise ValueError("notes")
    return obj


def link_checks(a: Mapping, b: Mapping) -> dict:
    """SUR-01: what the server can check about a proposed link of two rules: the units their conditions compare, whether their scopes can meet, the evidence of each side."""
    def units(r):
        out, stack = set(), [r.get("condition")]
        while stack:
            c = stack.pop()
            if isinstance(c, Mapping):
                if "field" in c and c.get("unit"):
                    out.add(str(c["unit"]).upper())
                stack += list(c.get("all") or []) + list(c.get("any") or []) + ([c["not"]] if "not" in c else [])
        return out
    ua, ub = units(a), units(b)
    sa, sb = a.get("scope") or {}, b.get("scope") or {}
    common = [k for k in sa if k in sb and isinstance(sa[k], list) and isinstance(sb[k], list)]
    scope = "not_stated" if not common else ("disjoint" if any(not set(map(str, sa[k])) & set(map(str, sb[k])) for k in common) else "overlapping")
    from core.change_intelligence.v2 import business_graph
    return {"units": "not_compared" if not (ua and ub) else ("same" if ua == ub else "different"), "scope": scope,
            "evidence_refs": {a["rule_id"]: business_graph._evidence_refs(a)[:4], b["rule_id"]: business_graph._evidence_refs(b)[:4]}}


def resolve_candidates(model: Mapping, offered_ids: Iterable[str], planned: Iterable[Iterable[str]], limits: Mapping, rules_by_id: Optional[Mapping] = None) -> dict:
    """Keep proposed pairs of offered rules that are new to the plan, unique and within the cap; every refused pair keeps its reason. With `rules_by_id` each kept pair
    carries the server's link checks (SUR-01); an `equivalent` claim over different units or disjoint scopes is downgraded to `same_concept` (never a merge)."""
    offered, planned = set(offered_ids), {frozenset(p) for p in planned}
    kept, rejected, seen = [], [], set()
    for c in model["pairs"]:
        a, b = sorted(c["rule_ids"])
        reason = None
        if a not in offered or b not in offered:
            reason = "rule_not_offered"
        elif a == b:
            reason = "same_rule"
        elif _UNSAFE.search(c["link"]) or not c["link"].strip():
            reason = "unsafe_or_empty_text"
        elif frozenset((a, b)) in planned:
            reason = "already_planned"
        elif (a, b) in seen:
            reason = "duplicate"
        elif len(kept) >= int(limits.get("max_semantic_pairs", 4)):
            reason = "semantic_cap"
        if reason:
            rejected.append({"rule_ids": [a, b], "reason": reason})
            continue
        seen.add((a, b))
        pair = {"rule_ids": [a, b], "relation": c["relation"], "relation_proposed": c["relation"], "link": " ".join(c["link"].split()), "origin": "model_candidate", "status": "PROPOSED"}
        if rules_by_id is not None:
            pair["checks"] = link_checks(rules_by_id[a], rules_by_id[b])
            if c["relation"] == "equivalent" and (pair["checks"]["units"] == "different" or pair["checks"]["scope"] == "disjoint"):
                pair["relation"] = "same_concept"
        kept.append(pair)
    return {"pairs": kept, "rejected": rejected, "notes": [" ".join(n.split()) for n in model["notes"] if not _UNSAFE.search(n)]}


def compatibility(rules: Iterable[Mapping], rubric: Mapping) -> str:
    """`compatible` (every outcome can be met together), `incompatible` (a terminal outcome against an obligation) or `unknown` (an outcome the rubric does not list)."""
    def cls(kind):
        if kind in rubric.get("cumulative", []):
            return "cumulative"
        if kind in rubric.get("terminal", []) or any(kind.startswith(p) for p in rubric.get("terminal_prefixes", [])):
            return "terminal"
        return None
    classes = [cls(str((r.get("outcome") or {}).get("kind") or "")) for r in rules]
    if None in classes:
        return "unknown"
    return "incompatible" if len(set(classes)) > 1 else "compatible"


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


def group_job_id(snapshot_id: str, group_id: str, attempt: int, binding: Optional[str] = None) -> str:
    """`binding` = the prompt/schema/model fingerprint of the mining role (LHA-00): a changed configuration is a new group job, never a reuse of an old result."""
    return "mine-" + canonical_hash({"snap": snapshot_id, "group": group_id, "attempt": attempt, **({"binding": binding} if binding else {})})[:24]


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


def pairs_to_map(value, key: str, val: str, *, numbers: bool = False):
    """A provider list of {key, value} pairs -> a map (structured output cannot return an open object: live smoke 2026-10-18); a map passes unchanged. Duplicate or
    malformed pairs raise ValueError. With `numbers`, a value written as a plain number becomes a number."""
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, list):
        raise ValueError("pairs")
    out = {}
    for p in value:
        if not isinstance(p, Mapping) or set(p) != {key, val} or not isinstance(p[key], str) or p[key] in out:
            raise ValueError("pairs")
        v = p[val]
        if numbers and isinstance(v, str) and re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?", v.strip()):
            n = float(v.strip().replace(",", ""))
            v = int(n) if n.is_integer() else n
        out[p[key]] = v
    return out


def validate_model_object(obj, limits: Mapping) -> dict:
    if isinstance(obj, dict) and isinstance(obj.get("hypotheses"), list):
        for h in obj["hypotheses"]:
            if isinstance(h, dict) and isinstance(h.get("case"), list):
                h["case"] = pairs_to_map(h["case"], "field", "value", numbers=True)
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


def resolve(model: Mapping, group_rules: Mapping[str, Mapping], asked_pairs: Iterable[Iterable[str]], *, snapshot_id: str, group_id: str, rubric: Optional[Mapping] = None) -> dict:
    """Keep what the group's rules support. `asked_pairs`: rule-id sets of gaps already asked or decided (any status) -> `already_asked`. `rubric`: outcome compatibility
    classes (config `compatibility`); without it a verified conflict is only checked for actor/outcome agreement."""
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
        kind, unknowns, verified, compat, disposition = h["kind"], list(h["unknowns"]), None, None, "as_proposed"
        if kind in ("potential_conflict", "exception_overlap"):
            if not h["case"]:
                kind, verified = "missing_context", False
                unknowns.append("no concrete case was given; the overlap could not be checked")
            else:
                verdicts = {rid: _applies(group_rules[rid], h["case"]) for rid in rids}
                if any(v[0] is False for v in verdicts.values()):
                    rejected.append({"kind": h["kind"], "rule_ids": rids, "reason": "case_not_shared:" + ",".join(r for r, v in sorted(verdicts.items()) if v[0] is False),
                                     "case": {str(k)[:60]: str(v)[:60] for k, v in list(h["case"].items())[:8]}})    # kept for diagnosis: which case the model proposed
                    continue
                if any(v[0] is None for v in verdicts.values()):
                    kind, verified = "missing_context", False
                    unknowns += sorted({w for v in verdicts.values() for w in v[1]})[:4]
                else:
                    verified = True
                    if kind == "potential_conflict" and len({(group_rules[r].get("actor_ref"), (group_rules[r].get("outcome") or {}).get("kind")) for r in rids}) == 1:
                        rejected.append({"kind": h["kind"], "rule_ids": rids, "reason": "no_disagreement"})
                        continue
                    if kind == "potential_conflict" and rubric is not None:
                        compat = compatibility([group_rules[r] for r in rids], rubric)
                        if compat == "compatible":                       # two approvals (or a review and a report) can both be required: not a conflict
                            rejected.append({"kind": h["kind"], "rule_ids": rids, "reason": "compatible_obligations", "case": {k: v for k, v in sorted(h["case"].items())}})
                            continue
                        if compat == "unknown":
                            kind, disposition = "ambiguity", "downgraded:compatibility_unknown"
                            unknowns.append("whether these outcomes can both be met is not in the supported rubric")
        if kind != h["kind"] and disposition == "as_proposed":
            disposition = "downgraded:" + ("case_undecided" if h["case"] else "no_case")
        key = (kind, tuple(rids))
        if key in seen:
            rejected.append({"kind": kind, "rule_ids": rids, "reason": "duplicate"})
            continue
        seen.add(key)
        kept.append({"hypothesis_id": hypothesis_id(snapshot_id, kind, rids, h["case"]), "kind": kind, "kind_proposed": h["kind"], "rule_ids": rids, "titles": [group_rules[r].get("title") for r in rids],
                     "reason": " ".join(h["reason"].split()), "case": {k: v for k, v in sorted(h["case"].items())}, "case_verified": verified, "question": " ".join(h["question"].split()),
                     "unknowns": unknowns[:6], "group_id": group_id, "origin": ORIGIN, "status": "PROPOSED", "disposition": disposition, "compatibility": compat})
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
