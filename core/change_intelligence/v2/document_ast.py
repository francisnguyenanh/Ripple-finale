# -*- coding: utf-8 -*-
"""Business and Technical knowledge documents: one fact model, two renderings (pure, no I/O). Contract: docs/contracts/FINAL_KNOWLEDGE_LOOP.md.

`build_document` turns ONE stored snapshot into a document AST; `render_markdown` turns the AST into canonical Markdown with the versioned templates in
`config/document_templates/`. Both modes are built from the same rule facts, so switching mode changes the amount of detail, never the meaning. Nothing here
calls a model, reads a store or writes a file; a generated document is never an input.

Safety: every string that came from a source, a model or a person is escaped before it reaches Markdown; links and HTML cannot be produced from them.
Access: the Technical mode carries source references; without `evidence.read` they are replaced by a stated redaction, never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2 import business_graph
from core.change_intelligence.v2.business_index import condition_text
from core.change_intelligence.v2.business_knowledge import MAX_RULES_PER_ARTIFACT, ContractError
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "fkl-doc/1"
MODES = ("business", "technical")
MAX_FIELD_CHARS = 2000
_WITHDRAWN = frozenset({"SUPERSEDED", "REJECTED"})
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~&])")
_AUTOLINK = re.compile(r"(?i)(https?|ftp)://|www\.")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def md_text(value) -> str:
    """Untrusted text as inert inline Markdown: control characters removed, newlines folded, length bounded, every Markdown/HTML-significant character escaped."""
    text = _CONTROL.sub("", str(value if value is not None else "")).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > MAX_FIELD_CHARS:
        text = text[:MAX_FIELD_CHARS] + "..."
    text = _MD_SPECIAL.sub(r"\\\1", text)
    # a bare address would be turned into a link by many renderers: break the scheme so the source text stays text
    return _AUTOLINK.sub(lambda m: m.group(0).replace(":", "\\:").replace(".", "\\."), text)


def fence(text: str, lang: str = "json") -> str:
    """A fenced block that its own content cannot close."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bar = "`" * max(3, longest + 1)
    return f"{bar}{lang}\n{text}\n{bar}"


def code(text) -> str:
    """Inline code that its own content cannot end; HTML and Markdown inside it stay literal."""
    text = _CONTROL.sub("", str(text)).replace("\n", " ")
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bar = "`" * (longest + 1)
    pad = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return f"{bar}{pad}{text}{pad}{bar}"


def template_from_text(text: str) -> dict:
    """Parse a template file's content (the caller reads the file: this module owns no storage)."""
    return json.loads(text)


# ---- facts ------------------------------------------------------------------------------------------------------------------------

def rule_fact(rule: Mapping) -> dict:
    """The meaning of one rule, the same in both modes."""
    return {"rule_id": rule["rule_id"], "title": rule["title"], "authority": rule["authority"], "status": rule["status"], "actor_ref": rule["actor_ref"],
            "condition": rule["condition"], "outcome": rule["outcome"], "exceptions": [e["text"] for e in rule["exceptions"]], "scope": rule["scope"],
            "implementation_status": rule["implementation_status"], "unknown_fields": sorted(rule.get("unknown_fields", [])),
            "decision_ids": business_graph._decision_refs(rule), "evidence_refs": business_graph._evidence_refs(rule),
            "bindings": [{"artifact_id": b["artifact_id"], "revision_id": b["revision_id"], "evidence_ref": b["evidence_ref"]} for b in rule["source_bindings"]],
            "concept_ids": list(rule["concept_ids"]), "candidate_ids": list(rule.get("candidate_ids", []))}


def build_document(snapshot: Mapping, decisions: Iterable[Mapping], *, mode: str, scope: Optional[Mapping] = None, can_read_evidence: bool = False,
                   locale: str = "en", template_version: str = "1") -> dict:
    """The AST of one snapshot (`snapshot` = a `v2_business.business_view` accepted/pending entry: snapshot, rules, relations, gaps) in one mode."""
    if mode not in MODES:
        raise ContractError("invalid_field", f"mode {mode!r} not in {MODES}")
    scope = dict(scope or {})
    wanted = set(scope.get("concept_ids") or []) | set(scope.get("rule_ids") or [])
    rules = [r for r in snapshot["rules"] if not wanted or r["rule_id"] in wanted or wanted & set(r["concept_ids"])]
    rules = sorted(rules, key=lambda r: r["rule_id"])
    if len(rules) > MAX_RULES_PER_ARTIFACT:
        raise ContractError("limit_exceeded", f"{len(rules)} rules; split with plan_parts() into parts of at most {MAX_RULES_PER_ARTIFACT}")
    ids = {r["rule_id"] for r in rules}
    active = [r for r in rules if r["status"] not in _WITHDRAWN]
    facts = [rule_fact(r) for r in rules]
    relations = sorted((e for e in snapshot["relations"] if e["status"] != "REJECTED" and (not wanted or e["from"] in ids or e["to"] in ids)), key=lambda e: e["edge_id"])
    conflicts = [e for e in relations if e["type"] == "conflicts_with"]
    pairs = {frozenset((e["from"], e["to"])) for e in conflicts}
    for g in sorted(snapshot["gaps"], key=lambda g: g["gap_id"]):            # a disagreement a person settled that no stored relation shows (e.g. suggested by a model)
        rids = sorted(g.get("affected_rule_ids") or [])
        settled = g.get("kind") == "conflicting_facts" and g.get("status") == "RESOLVED" and len(rids) == 2
        if settled and frozenset(rids) not in pairs and (not wanted or set(rids) & ids):
            conflicts.append({"edge_id": g["gap_id"], "from": rids[0], "to": rids[1]})
            pairs.add(frozenset(rids))
    gaps = sorted((g for g in snapshot["gaps"] if not wanted or set(g["affected_rule_ids"]) & ids), key=lambda g: g["gap_id"])
    open_gaps = [g for g in gaps if g["status"] in ("OPEN", "REOPENED")]
    unknown = [g for g in gaps if g["status"] == "ACCEPTED_UNKNOWN"]
    meta = snapshot["snapshot"]
    by_id = {r["rule_id"]: r for r in rules}
    decision_rows = sorted((d for d in decisions if d["kind"] in ("answer_gap", "business_change") and (not wanted or any(d["decision_id"] in business_graph._decision_refs(r) for r in rules))),
                           key=lambda d: d["decision_id"])
    doc = {
        "schema": SCHEMA, "mode": mode, "locale": locale, "template_version": template_version, "scope": scope,
        "snapshot": {"snapshot_id": meta["snapshot_id"], "lifecycle_state": meta["lifecycle_state"], "business_revision": meta["business_revision"],
                     "source_revision_id": meta["source_revision_id"], "km_revision_id": meta["km_revision_id"], "decision_epoch": meta["decision_epoch"],
                     "content_hash": meta["content_hash"]},
        "evidence_access": bool(can_read_evidence),
        "facts": facts,
        "overview": {"rules": len(rules), "active_rules": len(active), "decided_rules": sum(1 for r in active if r["authority"] == "TARGET"),
                     "open_questions": len(open_gaps), "critical_open": sum(1 for g in open_gaps if g["priority"] == "critical"), "accepted_unknown": len(unknown),
                     "partial": bool(open_gaps or unknown), "proposal": meta["lifecycle_state"] != "ACCEPTED"},
        "relations": [{"edge_id": e["edge_id"], "type": e["type"], "from": e["from"], "to": e["to"], "status": e["status"], "provenance_refs": list(e["provenance_refs"])} for e in relations],
        "conflicts": [{"edge_id": e["edge_id"], "rules": [e["from"], e["to"]], "titles": [by_id[x]["title"] if x in by_id else x for x in (e["from"], e["to"])],
                       **({"decided_by": d, "decided_title": by_id[d]["title"]} if (d := _decided_by(active, {e["from"], e["to"]})) else {})} for e in conflicts],
        "questions": [{"gap_id": g["gap_id"], "kind": g["kind"], "question": g["question"], "status": g["status"], "priority": g["priority"],
                       "affected_rule_ids": g["affected_rule_ids"], "evidence_refs": g["evidence_refs"]} for g in open_gaps + unknown],
        "decisions": [{"decision_id": d["decision_id"], "quote": d["quote"], "gap_id": (d.get("body") or {}).get("gap_id"), "summary": (d.get("body") or {}).get("summary", [])}
                      for d in decision_rows],
    }
    validate_document(doc)
    return doc


def _decided_by(active: list, pair: set) -> Optional[str]:
    """The decided rule that settles a disagreement: an active TARGET rule, CONFIRMED by a person's decision, whose root is one side of the pair (the same version link
    the answers use). The sources still disagree; the knowledge no longer does."""
    from core.change_intelligence.v2.business_rebase import root_of
    for r in active:
        if r["authority"] == "TARGET" and r["status"] == "CONFIRMED" and business_graph._decision_refs(r) and root_of(r["rule_id"]) in pair:
            return r["rule_id"]
    return None


def validate_document(doc: Mapping) -> None:
    if doc.get("schema") != SCHEMA:
        raise ContractError("unsupported_contract_version", f"document schema {doc.get('schema')!r}")
    if doc.get("mode") not in MODES:
        raise ContractError("invalid_field", "document mode")
    for key in ("snapshot", "facts", "overview", "relations", "conflicts", "questions", "decisions"):
        if key not in doc:
            raise ContractError("invalid_field", f"document missing {key}")
    seen = set()
    for f in doc["facts"]:
        if f["rule_id"] in seen:
            raise ContractError("invalid_field", f"duplicate rule {f['rule_id']}")
        seen.add(f["rule_id"])


def facts_view(doc: Mapping) -> list:
    """The part of the AST that carries meaning (identical in both modes for the same snapshot and scope)."""
    keys = ("rule_id", "title", "authority", "status", "actor_ref", "condition", "outcome", "exceptions", "scope", "implementation_status", "unknown_fields")
    return [{k: f[k] for k in keys} for f in doc["facts"]]


def plan_parts(rules: Iterable[Mapping], *, max_rules: int = MAX_RULES_PER_ARTIFACT) -> list:
    """Split a large scope by topic (first concept, else `other`) into parts of at most `max_rules`, with a manifest. Never truncates."""
    groups: dict = {}
    for r in sorted(rules, key=lambda r: r["rule_id"]):
        groups.setdefault((r["concept_ids"] or ["other"])[0], []).append(r["rule_id"])
    parts = []
    for topic in sorted(groups):
        ids = groups[topic]
        for i in range(0, len(ids), max_rules):
            chunk = ids[i:i + max_rules]
            parts.append({"part": len(parts) + 1, "topic": topic, "chunk": i // max_rules + 1, "rule_ids": chunk})
    return parts


# ---- rendering --------------------------------------------------------------------------------------------------------------------

def _scope_text(scope: Mapping) -> str:
    return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in sorted(scope.items())) or "-"


def _actor_words(ref) -> str:
    return ref.replace("role-", "").replace("-", " ") if ref else ""


def _sources_business(fact: Mapping) -> str:
    files = sorted({b["evidence_ref"].split("#", 1)[0] for b in fact["bindings"]} | {r.split("#", 1)[0] for r in fact["evidence_refs"]})
    return ", ".join(md_text(f) for f in files)


def _render_rule(fact: Mapping, tpl: Mapping, doc: Mapping, decisions: Mapping) -> list:
    L, W = tpl["labels"], tpl["words"]
    business = doc["mode"] == "business"
    out = [f"### {md_text(fact['title'])}", ""]
    out.append(f"- {L['rule_id']}: `{fact['rule_id']}`")
    if business:
        status = W["status"].get(f"{fact['authority']}/{fact['status']}") or f"{W['authority'][fact['authority']]}; {W['status_other'].get(fact['status'], fact['status'])}"
    else:
        status = f"{fact['authority']} / {fact['status']}"
    out.append(f"- {L['status']}: {md_text(status) if business else '`' + status + '`'}")
    actor = md_text(_actor_words(fact["actor_ref"]).title()) if business else (f"`{fact['actor_ref']}`" if fact["actor_ref"] else W["actor_unknown"])
    out.append(f"- {L['actor']}: {actor if fact['actor_ref'] or not business else W['actor_unknown']}")
    if business:
        out.append(f"- {L['condition']}: {md_text(condition_text(fact['condition'])) if fact['condition'] is not None else W['condition_unknown']}")
        outcome = str(fact["outcome"].get("kind", ""))
        out.append(f"- {L['outcome']}: " + (W["outcome_unknown"] if outcome in ("unknown", "unspecified", "") else md_text(outcome.replace("_", " "))))
        out.append(f"- {L['exceptions']}: " + ("; ".join(md_text(e) for e in fact["exceptions"]) if fact["exceptions"] else W["no_exceptions"]))
    else:
        cond = "null" if fact["condition"] is None else json.dumps(fact["condition"], sort_keys=True, ensure_ascii=False)
        out += [f"- {L['condition']}:", "", fence(cond), ""]
        out.append(f"- {L['outcome']}: {code(json.dumps(fact['outcome'], sort_keys=True, ensure_ascii=False))}")
        out.append(f"- {L['exceptions']}: " + (code(json.dumps(fact["exceptions"], ensure_ascii=False)) if fact["exceptions"] else W["no_exceptions"]))
    out.append(f"- {L['scope']}: {md_text(_scope_text(fact['scope']))}")
    if business:
        decided = [decisions[d] for d in fact["decision_ids"] if d in decisions]
        parts = []
        if decided:
            parts.append("Decided by a person: " + "; ".join(f"“{md_text(d['quote'])}”" for d in decided))
        files = _sources_business(fact)
        if files:
            parts.append(("Related sources: " if fact["authority"] == "TARGET" else "Stated in: ") + files)
        out.append(f"- {L['origin']}: " + (" | ".join(parts) if parts else "Not recorded"))
        out.append(f"- {L['implementation']}: {md_text(W['implementation'].get(fact['implementation_status'], fact['implementation_status']))}")
        if fact["unknown_fields"]:
            out.append("- Not known: " + ", ".join(md_text(u.replace("_ref", "").replace("_", " ")) for u in fact["unknown_fields"]))
    else:
        if doc["evidence_access"]:
            refs = [f"{b['artifact_id']}@{b['revision_id']}#{b['evidence_ref']}" for b in fact["bindings"]] or fact["evidence_refs"]
            out.append(f"- {L['origin']}: decisions {code(json.dumps(fact['decision_ids']))}; sources {code(json.dumps(refs, ensure_ascii=False))}; candidates {code(json.dumps(fact['candidate_ids']))}")
        else:
            out.append(f"- {L['origin']}: decisions {code(json.dumps(fact['decision_ids']))}; sources {W['redacted']}")
        out.append(f"- {L['implementation']}: {code(fact['implementation_status'])}; unknown fields {code(json.dumps(fact['unknown_fields']))}")
    return out + [""]


def render_markdown(doc: Mapping, template: Mapping) -> str:
    """Canonical Markdown: LF line endings, deterministic order, ends with one newline. Same AST and template give the same bytes."""
    validate_document(doc)
    if template["mode"] != doc["mode"] or template["template_version"] != doc["template_version"]:
        raise ContractError("invalid_field", "template does not match the document mode or version")
    H, snap, ov = template["headings"], doc["snapshot"], doc["overview"]
    decisions = {d["decision_id"]: d for d in doc["decisions"]}
    business = doc["mode"] == "business"
    lines = [f"# {md_text(template['title'])}", ""]
    for section in template["sections"]:
        lines += [f"## {H[section]}", ""]
        if section == "overview":
            state = "proposal, not yet accepted by a person" if ov["proposal"] else f"accepted version {snap['business_revision']}"
            if business:
                lines.append(f"This is the business knowledge as an {state}. It holds {ov['active_rules']} rules, {ov['decided_rules']} of them decided by people.")
                lines.append(f"{ov['open_questions']} question(s) are open ({ov['critical_open']} important) and {ov['accepted_unknown']} are left open on purpose."
                             + (" The knowledge is therefore partial." if ov["partial"] else ""))
            else:
                lines += [f"- snapshot: `{snap['snapshot_id']}` ({snap['lifecycle_state']}, business revision {snap['business_revision']})",
                          f"- source revision: `{snap['source_revision_id']}`; knowledge revision: `{snap['km_revision_id']}`; decision epoch: {snap['decision_epoch']}",
                          f"- content hash: `{snap['content_hash']}`", f"- rules: {ov['rules']} (active {ov['active_rules']}, decided {ov['decided_rules']}); "
                          f"open questions {ov['open_questions']} (critical {ov['critical_open']}); accepted unknown {ov['accepted_unknown']}; partial {str(ov['partial']).lower()}",
                          f"- template: `{doc['schema']}` v{doc['template_version']}, locale `{doc['locale']}`, scope `{json.dumps(doc['scope'], sort_keys=True)}`"]
            lines.append("")
        elif section == "rules":
            if not doc["facts"]:
                lines += ["No rule is in this scope.", ""]
            for f in doc["facts"]:
                lines += _render_rule(f, template, doc, decisions)
        elif section == "relations":
            for e in doc["relations"] or []:
                prov = code(json.dumps(e["provenance_refs"])) if doc["evidence_access"] or not e["provenance_refs"] else template["words"]["redacted"]
                lines.append(f"- {code(e['edge_id'])} {md_text(e['type'])}: {code(e['from'])} -> {code(e['to'])} ({md_text(e['status'])}); provenance {prov}")
            lines.append("" if doc["relations"] else "No relation is in this scope.\n")
        elif section == "conflicts":
            if not doc["conflicts"]:
                lines += ["No disagreement between sources is recorded.", ""]
            for c in doc["conflicts"]:
                if business:
                    pair = "- " + " and ".join(f"“{md_text(t)}”" for t in c["titles"]) + " disagree."
                    lines.append(pair + (f" A person decided the business rule: “{md_text(c['decided_title'])}”. The sources themselves are unchanged and still disagree." if c.get("decided_by")
                                        else " A person has to decide which one is the business rule."))
                else:
                    lines.append(f"- {code(c['edge_id'])}: {code(c['rules'][0])} conflicts with {code(c['rules'][1])}" + (f"; decided by {code(c['decided_by'])}" if c.get("decided_by") else ""))
            lines.append("")
        elif section == "unknowns":
            if not doc["questions"]:
                lines += ["No open question." if business else "none", ""]
            for g in doc["questions"]:
                lines.append(f"- {md_text(g['question'])}" + (" (important)" if g["priority"] == "critical" and business else "") + (" - left open on purpose" if g["status"] == "ACCEPTED_UNKNOWN" and business else ""))
                if not business:
                    lines.append(f"  - {code(g['gap_id'])} {md_text(g['kind'])} {md_text(g['status'])} {md_text(g['priority'])} rules {code(json.dumps(g['affected_rule_ids']))}")
            lines.append("")
        elif section == "implementation":
            pending = [f for f in doc["facts"] if f["authority"] == "TARGET" and f["status"] not in _WITHDRAWN]
            if not pending:
                lines += ["No decided rule needs to be compared with the files and code." if business else "none", ""]
            for f in pending:
                words = template["words"]["implementation"].get(f["implementation_status"], f["implementation_status"]) if business else f["implementation_status"]
                lines.append(f"- {md_text(f['title']) if business else code(f['rule_id'])}: {md_text(words) if business else code(words)}")
            lines += ["", "The source files and code are never changed by this knowledge; a person changes them." if business else "source files and code are not modified by the product", ""]
        elif section == "decisions":
            if not doc["decisions"]:
                lines += ["No decision has been recorded." if business else "none", ""]
            for d in doc["decisions"]:
                lines.append(f"- “{md_text(d['quote'])}”" + ("" if business else f" ({code(d['decision_id'])}, " + (f"gap {code(d['gap_id'])})" if d.get("gap_id") else "business change)")))
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def content_hash(markdown: str) -> str:
    return "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
