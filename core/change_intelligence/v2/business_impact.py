# -*- coding: utf-8 -*-
"""Impact of a confirmed business change intent (pure, no I/O, no model). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md.

Reads only the pinned accepted snapshot (rules, relations) and the confirmed operations. It reports what the stored knowledge shows and says what it could not check:
  * `direct`            the rules the operations change (write scope);
  * `confirmed_link`    a related rule reached only through relations that a person confirmed;
  * `inferred_dependency` a related rule reached through a proposed relation (shared concept or actor) - a lead, not a fact;
  * `counterevidence`   a rule that conflicts with a changed rule; it stays in the read scope and is never changed by this change;
  * `unknown`           a changed or related rule with no source mapping, or a graph part the walk did not reach: never reported as "no impact".
Nothing is executed. No simulation is run here; measured results come only from the numeric flow that already measures them, and this report carries none.
Dependencies that a model might propose are not produced by this module (`ai_dependencies.status` says so).
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_graph as bg
from core.change_intelligence.v2.serialize import canonical_hash

CODE_EXT = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rb", ".cs", ".php", ".sql", ".kt", ".swift", ".rs", ".c", ".cpp"})
CONFIG_EXT = frozenset({".yaml", ".yml", ".json", ".toml", ".ini", ".properties", ".xml", ".env", ".cfg", ".conf"})
DOC_EXT = frozenset({".md", ".txt", ".rst", ".docx", ".pdf", ".html", ".csv", ".xlsx"})
SEVERITY_RUBRIC = {
    "HIGH": "changes who decides or where a rule applies, changes a decided (target/confirmed) rule, or touches a rule that conflicts with a changed rule",
    "MEDIUM": "changes another kind of detail of an observed rule, is linked by a confirmed relation, or involves a code file",
    "LOW": "only an inferred dependency (shared concept or actor); check it, do not assume it changes",
}
_SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
LIMITS = {"max_hops": bg.MAX_HOPS, "max_nodes": bg.MAX_NODES, "max_files": 200}


def classify_file(path: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]+)\Z", path.lower())
    ext = m.group(1) if m else ""
    return "code" if ext in CODE_EXT else "config" if ext in CONFIG_EXT else "document" if ext in DOC_EXT else "other"


def _split_ref(ref: str) -> tuple:
    path, _, locator = str(ref).partition("#")
    return path, locator


def _numbers(condition) -> set:
    out = set()
    if not isinstance(condition, Mapping):
        return out
    if "field" in condition:
        v = condition.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.add(float(v))
        return out
    for key in ("all", "any"):
        for c in condition.get(key, []):
            out |= _numbers(c)
    if "not" in condition:
        out |= _numbers(condition["not"])
    return out


def _before_number(op: Mapping) -> Optional[float]:
    b = op.get("before")
    if isinstance(b, Mapping) and isinstance(b.get("value"), (int, float)) and not isinstance(b.get("value"), bool):
        return float(b["value"])
    return None


def _op_severity(op: Mapping, rule: Mapping) -> str:
    if op["kind"] in ("set_actor", "set_scope") or rule.get("authority") == "TARGET" or rule.get("status") == "CONFIRMED":
        return "HIGH"
    return "MEDIUM"


def _action(op: Mapping, title: str) -> str:
    k = op["kind"]
    if k == "set_value":
        d = op["desired"]
        return f"Change the value of “{title}” from {op['before']['value']} to {d['value']} {d.get('unit') or ''}".rstrip() + "."
    if k == "set_actor":
        return f"Change who acts on “{title}” from {str(op['before']).replace('role-', '')} to {str(op['desired']).replace('role-', '')}."
    if k == "add_or_change_condition":
        return f"Change when “{title}” applies."
    if k == "set_exception":
        return f"Change the exceptions of “{title}”."
    return f"Change where “{title}” applies."


def build_report(operations: Iterable[Mapping], rules: Mapping[str, Mapping], relations: Iterable[Mapping], *, pin: Mapping, known_artifacts: Optional[Iterable[str]] = None) -> dict:
    ops = [dict(o) for o in operations]
    relations = list(relations)
    write_ids = sorted({o["rule_id"] for o in ops})
    for rid in write_ids:
        if rid not in rules:
            raise ValueError(f"unknown_rule:{rid}")
    known = None if known_artifacts is None else set(known_artifacts)
    edge_by_id = {e["edge_id"]: e for e in relations}
    items: dict = {}
    gaps: list = []
    for o in ops:
        r = rules[o["rule_id"]]
        it = items.setdefault(o["rule_id"], {"rule_id": o["rule_id"], "title": r["title"], "link": "direct", "severity": "MEDIUM", "reasons": [], "path": []})
        it["reasons"].append(o["kind"])
        if _SEVERITY_ORDER[_op_severity(o, r)] < _SEVERITY_ORDER[it["severity"]]:
            it["severity"] = _op_severity(o, r)
    counter: dict = {}
    omitted = {"nodes_over_cap": 0, "beyond_hops": 0}
    starts = list(write_ids)
    for o in ops:
        if o["kind"] == "set_actor" and any(e["from"] == o["desired"] or e["to"] == o["desired"] for e in relations):
            starts.append(o["desired"])
    walked, unreached = set(), set()
    for start in dict.fromkeys(starts):
        walk = bg.traverse(relations, start, max_hops=LIMITS["max_hops"], max_nodes=LIMITS["max_nodes"])
        omitted["nodes_over_cap"] += walk["omitted"]["nodes_over_cap"]
        omitted["beyond_hops"] += walk["omitted"]["beyond_hops"]
        walked.update(walk["nodes"])
        unreached.update(walk["unreached"])
        for node in walk["nodes"]:
            if node == start or not node.startswith("rule-") or node in write_ids:
                continue
            if node not in rules:
                gaps.append({"kind": "stale_edge", "ref": node, "text": f"A relation points at “{node}”, which is not in the accepted knowledge; it was skipped."})
                continue
            path = walk["paths"][node]
            edges = [edge_by_id[p] for p in path]
            if any(e["type"] == "conflicts_with" for e in edges):
                counter.setdefault(node, {"rule_id": node, "title": rules[node]["title"], "via": path})
                continue
            link = "confirmed_link" if all(e["status"] == "CONFIRMED" for e in edges) else "inferred_dependency"
            sev = "MEDIUM" if link == "confirmed_link" else "LOW"
            cur = items.get(node)
            if cur is None or (cur["link"] == "inferred_dependency" and link == "confirmed_link"):
                items[node] = {"rule_id": node, "title": rules[node]["title"], "link": link, "severity": sev, "reasons": ["related_by_graph"], "path": path}
    left_rules = sorted(n for n in unreached - walked if n.startswith("rule-") and n in rules)    # only a connected rule the walk never reached makes the report incomplete
    if left_rules:
        gaps.append({"kind": "graph_traversal_limited", "ref": "", "rule_ids": left_rules[:20], "count": len(left_rules), "text": f"The graph walk stopped at "
                     f"{LIMITS['max_hops']} steps / {LIMITS['max_nodes']} items; {len(left_rules)} further connected rule(s) were not checked; they are unknown, not unaffected."})
    for rid in write_ids:                                    # a conflicting rule is read-only evidence and never part of the write scope
        for e in relations:
            if e["type"] == "conflicts_with" and rid in (e["from"], e["to"]):
                other = e["to"] if e["from"] == rid else e["from"]
                if other in rules and other not in write_ids:
                    counter.setdefault(other, {"rule_id": other, "title": rules[other]["title"], "via": [e["edge_id"]]})
    for rid, c in counter.items():
        if rid not in items:
            items[rid] = {"rule_id": rid, "title": c["title"], "link": "counterevidence", "severity": "HIGH", "reasons": ["conflicts_with_a_changed_rule"], "path": c["via"]}
    files: dict = {}
    for rid, it in items.items():
        role = "to_change" if it["link"] == "direct" else "check"
        bindings = rules[rid].get("source_bindings") or []
        if not bindings:
            gaps.append({"kind": "no_source_mapping", "ref": rid, "text": f"“{rules[rid]['title']}” has no mapped source or code location, so its impact on files is unknown (not “none”)."})
            it["unknown"] = True
        for b in bindings:
            path, locator = _split_ref(b["evidence_ref"])
            key = (b["artifact_id"], path)
            f = files.setdefault(key, {"artifact_id": b["artifact_id"], "path": path, "kind": classify_file(path), "role": role, "locators": [], "rules": [], "status": "mapped"})
            if role == "to_change":
                f["role"] = "to_change"
            if locator and locator not in f["locators"]:
                f["locators"].append(locator)
            if rid not in f["rules"]:
                f["rules"].append(rid)
            if known is None or not known:
                f["status"] = "not_checked"
            elif b["artifact_id"] not in known:
                f["status"] = "missing_file"
    for f in files.values():
        if f["status"] == "missing_file":
            gaps.append({"kind": "missing_file", "ref": f["path"], "text": f"“{f['path']}” is named by the knowledge but is not in the pinned source revision."})
    inventory = sorted(files.values(), key=lambda f: (f["role"] != "to_change", f["path"], f["artifact_id"]))[: LIMITS["max_files"]]
    actions = []
    for o in ops:
        r = rules[o["rule_id"]]
        actions.append({"kind": "update_rule", "rule_id": o["rule_id"], "op_id": o["op_id"], "text": _action(o, r["title"])})
        for f in inventory:
            if o["rule_id"] in f["rules"] and f["role"] == "to_change":
                verb = "Review the code at" if f["kind"] == "code" else "Update the setting in" if f["kind"] == "config" else "Update the wording in" if f["kind"] == "document" else "Check"
                actions.append({"kind": "recommend_" + f["kind"], "rule_id": o["rule_id"], "op_id": o["op_id"], "path": f["path"], "locators": f["locators"],
                                "text": f"{verb} {f['path']}: {_action(o, r['title'])} This is a recommendation; nothing is changed or run."})
    excluded = []
    for o in ops:
        n = _before_number(o)
        if o["kind"] == "set_value" and n is not None:
            for rid, r in sorted(rules.items()):
                if rid in items or r.get("status") in ("REJECTED", "SUPERSEDED"):
                    continue
                if n in _numbers(r.get("condition")):
                    excluded.append({"rule_id": rid, "title": r["title"], "reason": "same_number_different_rule", "text": f"“{r['title']}” uses the same number but is a different rule with no link to the changed one; it is not changed."})
    simulation = []
    for o in ops:
        if o["kind"] == "set_value":
            simulation.append({"op_id": o["op_id"], "status": "NOT_SIMULATED", "reason": "not run by this report; a supported numeric change can be measured by the Harbor simulation on request (POST .../impact/simulate), which reports its own counts"})
        else:
            simulation.append({"op_id": o["op_id"], "status": "NOT_SIMULATED", "reason": f"no simulation adapter supports a “{o['kind']}” change"})
    order = sorted(items.values(), key=lambda i: (_SEVERITY_ORDER[i["severity"]], i["link"] != "direct", i["rule_id"]))
    unknown = [g for g in gaps if g["kind"] in ("no_source_mapping", "graph_traversal_limited", "missing_file", "stale_edge")]
    report = {
        "pin": dict(pin), "operations": [o["op_id"] for o in ops], "write_scope": write_ids,
        "affected_rules": order, "counterevidence_rule_ids": sorted(counter), "file_inventory": inventory, "proposed_actions": actions, "excluded": excluded[:50],
        "severity_rubric": SEVERITY_RUBRIC, "gaps": gaps, "simulation": simulation, "measured": [],
        "ai_dependencies": {"status": "not_run", "reason": "dependencies a model might propose are not produced yet; only stored knowledge and its relations were used"},
        "coverage": {"write_rules": len(write_ids), "write_rules_with_source_mapping": sum(1 for r in write_ids if rules[r].get("source_bindings")),
                     "related_rules_found": sum(1 for i in order if i["link"] in ("confirmed_link", "inferred_dependency")), "files_listed": len(inventory),
                     "unknown_items": len(unknown), "complete": not unknown},
        "statement": "This lists what the stored knowledge and its relations show. Items marked unknown were not checked; they are not “no impact”. Nothing was run or changed.",
        "limits": dict(LIMITS),
    }
    report["report_hash"] = canonical_hash(report)
    return report


_LINK_WORDS = {"direct": "changed by this request", "confirmed_link": "linked by a confirmed relation", "inferred_dependency": "possibly related (not confirmed)",
               "counterevidence": "disagrees with a changed rule; kept as evidence, not changed"}


def logical_rule_id(rule_id: str) -> str:
    """The observed rule and its accepted TARGET version (`<id>-target`, business_answers) are two versions of one logical rule."""
    return rule_id[:-7] if rule_id.endswith("-target") else rule_id


def ref_key(rule_id: str) -> str:
    """Opaque, stable key of a LOGICAL rule for typed links between reader views (impact rows, risk-memo statements, assistant facts); carries no id in clear."""
    return "rk-" + canonical_hash({"rule": logical_rule_id(str(rule_id))})[:16]


def _version(i: Mapping) -> dict:
    return {"view": "target" if i["rule_id"].endswith("-target") else "observed", "title": i["title"], "meaning": _LINK_WORDS[i["link"]], "link": i["link"],
            "severity": i["severity"], "unknown": bool(i.get("unknown"))}


def _affected_for_reader(items) -> list:
    """One entry per logical rule. When the walk reached both versions of a rule, they are grouped, never merged by title: each version keeps its view, link
    (counterevidence included) and severity under `versions`; the entry's headline is the first version in the report's order (the most severe)."""
    groups: dict = {}
    for i in items:
        groups.setdefault(logical_rule_id(i["rule_id"]), []).append(i)
    out = []
    for vs in groups.values():
        head = vs[0]
        e = {"title": head["title"], "meaning": _LINK_WORDS[head["link"]], "link": head["link"], "severity": head["severity"], "unknown": any(v.get("unknown") for v in vs),
             "ref_key": ref_key(head["rule_id"])}
        if len(vs) > 1:
            e["versions"] = [_version(v) for v in vs]
        out.append(e)
    return out


def render(report: Mapping, *, mode: str, can_read_evidence: bool) -> dict:
    """What THIS reader may see. Business mode has no ids; file names, locators and code recommendations need evidence rights; Technical adds ids, paths in the graph and the hash.
    The stored report (and its hash) keeps one item per rule id; only this rendering groups the versions of one logical rule."""
    withheld = 0 if can_read_evidence else len(report["file_inventory"])
    out = {"statement": report["statement"], "severity_rubric": report["severity_rubric"], "coverage": report["coverage"],
           "affected_rules": _affected_for_reader(report["affected_rules"]),
           "gaps": [g["text"] for g in report["gaps"] if can_read_evidence or g["kind"] not in ("missing_file",)],
           "excluded": [e["text"] for e in report["excluded"]], "simulation": [{"status": s["status"], "reason": s["reason"]} for s in report["simulation"]],
           "measured": report["measured"], "ai_dependencies": report["ai_dependencies"],
           "proposed_actions": [a["text"] for a in report["proposed_actions"] if can_read_evidence or a["kind"] == "update_rule"],
           "files": [{"path": f["path"], "kind": f["kind"], "role": f["role"], "status": f["status"]} for f in report["file_inventory"]] if can_read_evidence else [],
           "files_withheld": withheld}
    if mode == "technical":
        out["technical"] = {"pin": report["pin"], "report_hash": report["report_hash"], "write_scope": report["write_scope"], "counterevidence_rule_ids": report["counterevidence_rule_ids"],
                            "affected": [{"rule_id": i["rule_id"], "logical_rule_id": logical_rule_id(i["rule_id"]), "link": i["link"], "severity": i["severity"], "reasons": i["reasons"],
                                          "path": i["path"]} for i in report["affected_rules"]],
                            "limits": report["limits"], "gaps": report["gaps"] if can_read_evidence else [g for g in report["gaps"] if g["kind"] != "missing_file"]}
        if can_read_evidence:
            out["technical"]["files"] = [{k: f[k] for k in ("artifact_id", "path", "kind", "role", "locators", "rules", "status")} for f in report["file_inventory"]]
    return out


# ---- dependencies proposed by a model: kept only as PROPOSED leads ------------------------------------------------------------------------------

DEP_KEYS = {"status", "dependencies", "notes"}
DEP_ITEM_KEYS = {"from_rule_id", "to_rule_id", "relation", "rationale", "evidence_refs"}
DEP_RELATIONS = frozenset({"depends_on", "exception_to", "implements"})
DEP_STATUSES = frozenset({"OK", "NONE_FOUND", "UNSURE"})
_UNSAFE_TEXT = re.compile(r"<[^>]*>|\]\(|https?://|www\.|`|javascript:|\x00", re.I)


def _plain(text, limit) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t if t and len(t) <= limit and not _UNSAFE_TEXT.search(t) else None


def validate_dep_object(obj, limits: Mapping) -> dict:
    """Shape only; what the entries mean is decided by `resolve_dependencies`."""
    if not isinstance(obj, dict) or set(obj) != DEP_KEYS or obj["status"] not in DEP_STATUSES:
        raise ValueError("keys_or_status")
    if not isinstance(obj["dependencies"], list) or not isinstance(obj["notes"], list) or not all(isinstance(n, str) for n in obj["notes"]):
        raise ValueError("lists")
    if len(obj["dependencies"]) > limits["max_dependencies"] or len(obj["notes"]) > limits["max_notes"]:
        raise ValueError("sizes")
    for d in obj["dependencies"]:
        if not isinstance(d, dict) or set(d) != DEP_ITEM_KEYS or not all(isinstance(d[k], str) for k in ("from_rule_id", "to_rule_id", "relation", "rationale")):
            raise ValueError("dependency_shape")
        if not isinstance(d["evidence_refs"], list) or not all(isinstance(x, str) for x in d["evidence_refs"]):
            raise ValueError("dependency_refs")
    return obj


def rule_refs(rule: Mapping) -> set:
    """The references of one stored rule that a dependency may cite: its id and the source locations it is bound to or was observed in."""
    refs = {rule["rule_id"]} | {b["evidence_ref"] for b in rule.get("source_bindings") or []}
    refs |= {p["evidence_ref"] for p in rule.get("provenance") or [] if p.get("kind") == "source_evidence"}
    return refs


def resolve_dependencies(obj: Mapping, rules: Mapping[str, Mapping], write_ids: Iterable[str], *, limits: Mapping, can_read_evidence: bool) -> dict:
    write = set(write_ids)
    accepted, rejected, seen = [], [], set()
    for d in obj["dependencies"]:
        a, b, rel = d["from_rule_id"], d["to_rule_id"], d["relation"]

        def no(reason):
            rejected.append({"reason": reason, "from_rule_id": a[:80], "to_rule_id": b[:80], "relation": rel[:40]})

        if a not in rules or b not in rules:
            no("invented_rule_id")
        elif a == b:
            no("same_rule")
        elif a not in write and b not in write:
            no("not_about_a_changed_rule")
        elif rel not in DEP_RELATIONS:
            no("relation_not_allowed")
        else:
            why = _plain(d["rationale"], limits["max_rationale_chars"])
            allowed = rule_refs(rules[a]) | rule_refs(rules[b])
            if not can_read_evidence:
                allowed = {a, b}                                                           # the asker may cite business items, never source locations
            refs = list(dict.fromkeys(d["evidence_refs"]))
            if why is None:
                no("rationale_not_plain_text")
            elif not refs or len(refs) > limits["max_evidence_refs"]:
                no("evidence_refs_missing_or_too_many")
            elif not set(refs) <= allowed:
                no("evidence_ref_not_in_the_pinned_rules")
            elif (a, b, rel) in seen:
                no("duplicate")
            else:
                seen.add((a, b, rel))
                accepted.append({"dep_id": "dep-" + canonical_hash({"f": a, "t": b, "r": rel})[:16], "from_rule_id": a, "to_rule_id": b, "relation": rel, "rationale": why,
                                 "evidence_refs": refs, "status": "PROPOSED", "provenance": "ai_inference", "changes_write_scope": False})
    notes = [t for t in (_plain(n, limits["max_rationale_chars"]) for n in obj["notes"]) if t]
    return {"accepted": accepted, "rejected": rejected, "notes": notes}


def build_dep_prompt(frozen: Mapping, changes: Iterable[str], rules: Iterable[Mapping], found: Iterable[str], *, can_read_evidence: bool, max_chars: int) -> str:
    lines, used = [], 0
    for r in rules:
        line = f"- id={r['rule_id']} actor={r.get('actor_ref')} concepts={','.join(r.get('concept_ids') or [])} status={r.get('status')}\n  title: {r['title']}"
        if r.get("condition") is not None:                       # contract version 2: the condition (thresholds) the dependency may rest on
            from core.change_intelligence.v2.business_index import condition_text
            line += f"\n  when: {condition_text(r['condition'])}"
        if can_read_evidence:
            line += "\n  references: " + "; ".join(sorted(rule_refs(r) - {r["rule_id"]}))
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    parts = {"{changes}": "\n".join(changes), "{found}": "\n".join(found) or "(none)", "{rules}": "\n".join(lines) or "(none)"}
    return re.sub(r"\{changes\}|\{found\}|\{rules\}", lambda m: parts[m.group(0)], frozen["prompt_template"])     # one pass: data never expands a placeholder


def merge_dependencies(report: Mapping, proposed: Mapping, *, rules_not_shown: int = 0) -> dict:
    """Coverage with the model part: complete only when the stored-graph part is complete AND the proposal ran fully (a limited rule list is not complete)."""
    gaps = [] if not rules_not_shown else [{"kind": "ai_rules_not_shown", "ref": "", "text": f"{rules_not_shown} rule(s) were not shown to the model, so their possible dependencies are unknown."}]
    return {"accepted": proposed["accepted"], "rejected": proposed["rejected"], "notes": proposed["notes"], "gaps": gaps,
            "complete": bool(report["coverage"]["complete"]) and not gaps}
