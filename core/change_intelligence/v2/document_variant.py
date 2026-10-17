# -*- coding: utf-8 -*-
"""Audience variants of one generated document (extension AIX-D1): pure, no I/O, no model.

The model only writes paragraphs bound to fact ids over the document AST (`document_ast.build_document`); the server decides what stays: a paragraph is kept only if every
fact id is in the pack, every number in it appears in the referenced facts (or, in a narrative section, in the overview counts), every role word in it belongs to a referenced
fact, and the text is plain (no markup, links or control characters). A mandatory section of the audience without a kept paragraph makes the variant a labelled deterministic
FALLBACK (the base document's own bytes under a label), never a partial narrative saved as if complete. Unknowns, limits and the canonical fact table are rendered by the server
in every variant, so the audience never widens what the base document may show and never hides what the knowledge does not know.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, Mapping

from core.change_intelligence.v2 import document_ast as da
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-doc-variant/1"
AUDIENCES = ("executive", "sop", "audit")
MODEL_KEYS = {"status", "sections", "omitted"}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PLAIN_FORBIDDEN = re.compile(r"[<>`#*_\[\]|]|https?://|www\.|[\x00-\x08\x0b-\x1f\x7f]", re.I)
_WORD = re.compile(r"[a-z][a-z\-]+", re.I)


# ---- fact pack ----------------------------------------------------------------------------------------------------------------------------------------

def _numbers_in(value) -> set:
    out = set()
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        out.add(_norm_number(str(value)))
    elif isinstance(value, str):
        out |= {_norm_number(m) for m in _NUMBER.findall(value)}
    elif isinstance(value, Mapping):
        for v in value.values():
            out |= _numbers_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= _numbers_in(v)
    return out


def _norm_number(text: str) -> str:
    t = text.replace(",", "")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t


def fact_pack(doc: Mapping, *, max_facts: int) -> dict:
    """What the model may write from and what the validator checks against: rule facts (meaning only, no evidence refs), open questions, overview counts."""
    facts = []
    for f in da.facts_view(doc)[:max_facts]:
        actor = da._actor_words(f["actor_ref"])
        words = set(w.lower() for w in _WORD.findall(f["title"])) | set(w.lower() for w in _WORD.findall(actor)) | set(w.lower() for w in _WORD.findall(" ".join(f["exceptions"])))
        cond = da.condition_text(f["condition"]) if f["condition"] is not None else None
        words |= set(w.lower() for w in _WORD.findall(cond or ""))
        facts.append({"id": f["rule_id"], "title": f["title"], "actor": actor or None, "condition": cond, "outcome": str((f["outcome"] or {}).get("kind", "")).replace("_", " ") or None,
                      "exceptions": list(f["exceptions"]), "authority": f["authority"], "status": f["status"], "implementation_status": f["implementation_status"],
                      "unknown_fields": list(f["unknown_fields"]), "numbers": sorted(_numbers_in(f["condition"]) | _numbers_in(f["title"]) | _numbers_in(f["exceptions"])), "words": sorted(words)})
    questions = [{"id": q["gap_id"], "question": q["question"], "status": q["status"], "priority": q["priority"], "rules": list(q["affected_rule_ids"]),
                  "numbers": sorted(_numbers_in(q["question"])), "words": sorted(set(w.lower() for w in _WORD.findall(q["question"])))} for q in doc["questions"]]
    ov = doc["overview"]
    return {"facts": facts, "questions": questions, "overview": dict(ov), "overview_numbers": {_norm_number(str(v)) for v in ov.values() if isinstance(v, int) and not isinstance(v, bool)},
            "truncated": len(da.facts_view(doc)) > max_facts}


def build_prompt(frozen: Mapping, audience: str, pack: Mapping) -> str:
    cfg = frozen["audiences"][audience]
    facts = "\n".join(f"- id={f['id']} | {f['title']} | who acts: {f['actor'] or 'not stated'} | when: {f['condition'] or 'not stated'} | what happens: {f['outcome'] or 'not stated'} | "
                      f"exceptions: {'; '.join(f['exceptions']) or 'none recorded'} | {f['authority']}/{f['status']} | in the code: {f['implementation_status']}"
                      + (f" | not known: {', '.join(f['unknown_fields'])}" if f["unknown_fields"] else "") for f in pack["facts"]) or "(no rule in scope)"
    questions = "\n".join(f"- id={q['id']} [{q['status']}/{q['priority']}] {q['question']} (rules: {', '.join(q['rules'])})" for q in pack["questions"]) or "(none)"
    overview = json.dumps(pack["overview"], sort_keys=True)
    sections = "\n".join(f"- {s}: {cfg['headings'][s]}" for s in cfg["sections"])
    prompt = frozen["prompt_template"].replace("{audience}", audience).replace("{guidance}", cfg["guidance"]).replace("{mandatory}", ", ".join(cfg["mandatory"])) \
        .replace("{sections}", sections).replace("{overview}", overview).replace("{facts}", facts).replace("{questions}", questions).replace("{narrative}", ", ".join(cfg["narrative"]) or "none")
    if len(prompt) > frozen["limits"]["max_prompt_chars"]:
        raise ValueError("context_too_large")
    return prompt


# ---- validation ---------------------------------------------------------------------------------------------------------------------------------------

def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != MODEL_KEYS:
        raise ValueError("object keys")
    if obj["status"] not in ("OK", "UNSUPPORTED"):
        raise ValueError("status")
    if not isinstance(obj["sections"], list) or len(obj["sections"]) > limits["max_sections"]:
        raise ValueError("sections")
    for s in obj["sections"]:
        if not isinstance(s, dict) or set(s) != {"id", "paragraphs"} or not isinstance(s["id"], str) or not isinstance(s["paragraphs"], list) or len(s["paragraphs"]) > limits["max_paragraphs_per_section"]:
            raise ValueError("section shape")
        for p in s["paragraphs"]:
            if not isinstance(p, dict) or set(p) != {"text", "fact_ids"} or not isinstance(p["text"], str) or not isinstance(p["fact_ids"], list) or not all(isinstance(x, str) for x in p["fact_ids"]):
                raise ValueError("paragraph shape")
            if len(p["text"]) > limits["max_paragraph_chars"] or len(p["fact_ids"]) > limits["max_fact_ids_per_paragraph"]:
                raise ValueError("paragraph limits")
    if not isinstance(obj["omitted"], list) or len(obj["omitted"]) > limits["max_omitted"] or not all(isinstance(x, str) and len(x) <= 300 for x in obj["omitted"]):
        raise ValueError("omitted")
    return obj


def check(model: Mapping, pack: Mapping, audience_cfg: Mapping) -> dict:
    """Keep what the facts support; return kept sections, rejected paragraphs with reasons, coverage and whether a mandatory section is missing (-> fallback)."""
    by_id = {f["id"]: f for f in pack["facts"]} | {q["id"]: q for q in pack["questions"]}
    kept, rejected, referenced = {}, [], set()
    seen_sections = set()
    for s in model["sections"]:
        if s["id"] not in audience_cfg["sections"] or s["id"] in seen_sections:
            rejected.append({"section": s["id"], "reason": "unknown_or_duplicate_section", "paragraphs": len(s["paragraphs"])})
            continue
        seen_sections.add(s["id"])
        narrative = s["id"] in audience_cfg["narrative"]
        for p in s["paragraphs"]:
            text = " ".join(p["text"].split())
            reason = _paragraph_reason(text, p["fact_ids"], by_id, pack, narrative)
            if reason:
                rejected.append({"section": s["id"], "reason": reason, "text": text[:160], "fact_ids": list(p["fact_ids"])})
                continue
            kept.setdefault(s["id"], []).append({"text": text, "fact_ids": sorted(set(p["fact_ids"]))})
            referenced |= {x for x in p["fact_ids"] if x in {f["id"] for f in pack["facts"]}}
    missing = [m for m in audience_cfg["mandatory"] if not kept.get(m)]
    total = len(pack["facts"])
    return {"kept": kept, "rejected": rejected, "missing_mandatory": missing, "fallback": bool(missing) or model["status"] != "OK",
            "fact_coverage": {"referenced": len(referenced), "total": total, "complete": total > 0 and len(referenced) == total}, "omitted": list(model["omitted"])}


def _paragraph_reason(text: str, fact_ids: Iterable[str], by_id: Mapping, pack: Mapping, narrative: bool) -> str | None:
    if not text:
        return "empty"
    if _PLAIN_FORBIDDEN.search(text):
        return "not_plain_text"
    unknown = [x for x in fact_ids if x not in by_id]
    if unknown:
        return f"unknown_fact_id:{unknown[0]}"
    if not fact_ids and not narrative:
        return "no_fact_ids"
    refs = [by_id[x] for x in fact_ids]
    allowed_numbers = set().union(*(set(r["numbers"]) for r in refs)) if refs else set()
    if narrative:
        allowed_numbers |= pack["overview_numbers"]
    # an identifier of the pack written into the prose (rule-..., gap-...) is a reference, not a number: its digits are not checked as amounts
    # (live finding 2026-10-17: a paragraph citing gap ids in its text was rejected as "number_not_in_referenced_facts:1")
    scan = text
    for known in sorted(by_id, key=len, reverse=True):
        scan = scan.replace(known, " ")
    for m in _NUMBER.findall(scan):
        if _norm_number(m) not in allowed_numbers:
            return f"number_not_in_referenced_facts:{m}"
    if refs:
        allowed_words = set().union(*(set(r["words"]) for r in refs))
        pack_actors = {f["actor"].lower() for f in pack["facts"] if f.get("actor")}
        low = text.lower()
        for actor in pack_actors:
            if actor in low and not all(w in allowed_words for w in _WORD.findall(actor)):
                return f"actor_not_in_referenced_facts:{actor}"
    else:
        low = text.lower()
        for actor in {f["actor"].lower() for f in pack["facts"] if f.get("actor")}:
            if actor in low:
                return f"actor_without_fact_id:{actor}"
    return None


# ---- rendering -----------------------------------------------------------------------------------------------------------------------------------------

def render(doc: Mapping, audience: str, audience_cfg: Mapping, checked: Mapping, *, base_markdown: str | None, label: Mapping) -> str:
    """Canonical Markdown of the variant: label, model sections (kept paragraphs), then the server's unknowns, limits and fact table. A fallback carries the base bytes."""
    lines = [f"# {da.md_text(audience_cfg['title'])}", "", "> " + da.md_text(_label_text(label, checked)), ""]
    if checked["fallback"]:
        lines += ["## Deterministic fallback", "", "The model's wording was not kept" + (f" (missing: {', '.join(da.md_text(m) for m in checked['missing_mandatory'])})" if checked["missing_mandatory"] else "")
                  + ". The base document of the same knowledge version follows unchanged.", "", "---", "", (base_markdown or "").rstrip("\n"), ""]
    else:
        for sid in audience_cfg["sections"]:
            paras = checked["kept"].get(sid)
            if not paras:
                continue
            lines += [f"## {da.md_text(audience_cfg['headings'][sid])}", ""]
            for p in paras:
                lines.append(da.md_text(p["text"]) + (f" [{', '.join(da.code(x) for x in p['fact_ids'])}]" if audience == "audit" and p["fact_ids"] else ""))
                lines.append("")
    lines += ["## What is not known yet", ""]
    if not doc["questions"]:
        lines += ["No open question.", ""]
    for g in doc["questions"]:
        lines.append(f"- {da.md_text(g['question'])}" + (" (important)" if g["priority"] == "critical" else "") + (" - left open on purpose" if g["status"] == "ACCEPTED_UNKNOWN" else ""))
    if doc["questions"]:
        lines.append("")
    lines += ["## Limits", "", "- This variant is a reading of one knowledge version for one audience; it is not evidence and is never read back as a source.",
              f"- Facts referenced by the kept wording: {checked['fact_coverage']['referenced']} of {checked['fact_coverage']['total']}." + ("" if checked["fact_coverage"]["complete"] else " Rules not mentioned above are still in force; see the table."),
              "- Numbers, roles and thresholds in the wording were checked against the facts; paragraphs that failed the check were left out (count in the label)." if not checked["fallback"] else "- No model wording is included.",
              "- Any statement of compliance is about the policy recorded here, not about law."]
    if checked.get("omitted"):
        lines.append("- The model said it could not cover: " + "; ".join(da.md_text(o) for o in checked["omitted"]))
    lines += ["", "## Facts (canonical)", "", "| Rule | Who acts | When it applies | What happens | Exceptions | Status |", "|---|---|---|---|---|---|"]
    for f in da.facts_view(doc):
        cond = da.condition_text(f["condition"]) if f["condition"] is not None else "not stated"
        lines.append(f"| {da.md_text(f['title'])} ({da.code(f['rule_id'])}) | {da.md_text(da._actor_words(f['actor_ref']) or 'not stated')} | {da.md_text(cond)} | "
                     f"{da.md_text(str((f['outcome'] or {}).get('kind', '')).replace('_', ' ') or 'not stated')} | {da.md_text('; '.join(f['exceptions']) or 'none recorded')} | {da.md_text(f['authority'] + '/' + f['status'])} |")
    return "\n".join(lines).rstrip("\n") + "\n"


def _label_text(label: Mapping, checked: Mapping) -> str:
    status = "DETERMINISTIC FALLBACK (no model wording)" if checked["fallback"] else f"MODEL WORDING checked by the server ({len(checked['rejected'])} paragraph(s) left out)"
    return (f"Audience variant {label['audience']} of document {label['artifact_id']} (knowledge version {label['snapshot_id']}); {status}; role {label['role']} pinned to "
            f"{label['model_pinned']} (config {label['config_sha256'][:12]}); generated {label['generated_at']}.")


def variant_id_for(artifact_id: str, audience: str, locale: str, config_sha: str, model_id: str) -> str:
    return "dv-" + canonical_hash({"a": artifact_id, "u": audience, "l": locale, "c": config_sha, "m": model_id})[:24]
