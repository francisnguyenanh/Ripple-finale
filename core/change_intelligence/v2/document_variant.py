# -*- coding: utf-8 -*-
"""Audience variants of one generated document (extension AIX-D1): pure, no I/O, no model.

The model only writes paragraphs bound to fact ids over the document AST (`document_ast.build_document`); the server decides what stays: a paragraph is kept only if every
fact id is in the pack, every number in it appears in the referenced facts (or, in a narrative section, in the overview counts), every role word in it belongs to a referenced
fact, and the text is plain (no markup, links or control characters). A mandatory section of the audience without a kept paragraph makes the variant a labelled deterministic
FALLBACK (the base document's own bytes under a label), never a partial narrative saved as if complete. Unknowns, limits and the canonical fact table are rendered by the server
in every variant, so the audience never widens what the base document may show and never hides what the knowledge does not know.

Fidelity (LHA-D1): a paragraph that cites a rule carries one claim row per cited rule; each claim must equal the canonical fact (actor, operator, value, unit, outcome, stated
exceptions) and the prose must agree with the facts sentence by sentence: the comparator next to each threshold, negation and exemption words only where the facts have an
exception or a negative condition, the actor bound to the threshold of its own rule, contrast terms (CAPEX/OPEX, ...) and currencies of the cited facts only, no approved /
implemented / compliant status the facts do not hold, no law or regulation the facts do not name. Exceptions of described rules the wording leaves out, facts cut by the pack cap
and the absence of a process order (SOP) are disclosed by the server. These are clause checks, not entailment: no model reviewer reads the free wording in this version.
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
_PLAIN_FORBIDDEN = re.compile(r"[<>`#*\[\]|]|https?://|www\.|[\x00-\x08\x0b-\x1f\x7f]", re.I)       # `_` allowed: fact values carry it (SEGREGATION_BREACH) and md_text escapes it
_WORD = re.compile(r"[a-z][a-z\-]+", re.I)
CLAIM_KEYS = {"fact_id", "actor", "operator", "value", "unit", "outcome", "exceptions"}
OPERATORS = ("gte", "gt", "lte", "lt", "eq", "ne", "in", "not_in")
CLAUSE_CHECKS = "lha-d1/2"                  # 2: per-section and per-paragraph caps leave paragraphs out; server-stated exceptions section
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
# comparator wording next to a threshold: before the number (end-anchored) or after it and its unit (start-anchored); longer phrases first
_OP_BEFORE = [(re.compile(p + r"\s*$", re.I), op) for p, op in (
    (r"\b(?:not|no) more than", "lte"), (r"\b(?:not|no) less than", "gte"), (r"\bat least", "gte"), (r"\bstarting (?:at|from)", "gte"), (r"\bfrom", "gte"),
    (r"\bmore than", "gt"), (r"\bgreater than", "gt"), (r"\bin excess of", "gt"), (r"\babove", "gt"), (r"\bover", "gt"), (r"\bexceeding", "gt"), (r"\bexceeds", "gt"),
    (r"\bat most", "lte"), (r"\bup to", "lte"), (r"\bless than", "lt"), (r"\bbelow", "lt"), (r"\bunder", "lt"))]
_OP_AFTER = [(re.compile(r"^\s*(?:[a-z]{2,6}\s+)?" + p, re.I), op) for p, op in (
    (r"or (?:more|above|higher|over)\b", "gte"), (r"and (?:above|over|more)\b", "gte"), (r"or (?:less|below|lower|under)\b", "lte"))]
_NEGATION = re.compile(r"\b(?:must not|need not|does not need|do not need|doesn't need|don't need|is not required|are not required|not required|is not needed|are not needed|"
                       r"no approval|never (?:needs?|requires?)|without (?:any )?approval|no longer)\b", re.I)
_EXEMPTION = re.compile(r"\b(?:exempt|exempted|exemption|waived?|excused|except|unless)\b", re.I)
_STATUS_DECIDED = re.compile(r"\b(?:rules?|polic(?:y|ies)|controls?|requirements?|thresholds?)\s+(?:is|are|was|were|has been|have been)\s+(?:formally\s+|officially\s+)?"
                             r"(?:approved|confirmed|decided|ratified|signed off)\b", re.I)
_STATUS_IMPLEMENTED = re.compile(r"\b(?:is|are|was|were|has been|have been)\s+(?:fully\s+|already\s+)?(?:implemented|enforced|automated|verified)\b|\bin place in the (?:code|system)\b", re.I)
_CLAUSE = re.compile(r",\s|;|\s(?:and|while|but|whereas)\s", re.I)          # not the commas inside 1,000,000
_LEGAL_NAME = re.compile(r"\b(?:[A-Z][\w-]+\s+)+Act\b|\bArticle\s+\d+")
_COMPLIANCE = re.compile(r"\bcompl(?:iant|ies with|iance with|y with)\b", re.I)
_CURRENCY_ALIAS = {"yen": "jpy", "dollar": "usd", "dollars": "usd", "euro": "eur", "euros": "eur", "dong": "vnd", "pound": "gbp", "pounds": "gbp", "yuan": "cny"}
_STOP = frozenset("a an and are as at be by for from in is it of on or the to with when who what which that this these those any all must need needs not no".split())


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


def _leaves(cond, negated: bool = False) -> list:
    """The comparison leaves of a condition (field, operator, value, unit); a leaf under `not` is marked negated."""
    if not isinstance(cond, Mapping):
        return []
    if "field" in cond:
        return [{"field": cond["field"], "op": cond["op"], "value": cond["value"], "unit": cond.get("unit"), "negated": negated}]
    if "all" in cond or "any" in cond:
        return [leaf for c in cond.get("all", cond.get("any", [])) for leaf in _leaves(c, negated)]
    return _leaves(cond.get("not"), not negated)


def _words(*texts) -> set:
    return {w.lower() for t in texts for w in _WORD.findall(t or "")}


def fact_pack(doc: Mapping, *, max_facts: int) -> dict:
    """What the model may write from and what the validator checks against: rule facts (meaning only, no evidence refs), open questions, overview counts."""
    facts = []
    view = da.facts_view(doc)
    for f in view[:max_facts]:
        actor = da._actor_words(f["actor_ref"])
        cond = da.condition_text(f["condition"]) if f["condition"] is not None else None
        scope = " ".join(f"{k} {' '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in sorted((f.get("scope") or {}).items()))
        words = _words(f["title"], actor, " ".join(f["exceptions"]), cond, scope)
        leaves = _leaves(f["condition"])
        words |= {str(leaf["unit"]).lower() for leaf in leaves if leaf["unit"]} | _words(" ".join(str(leaf["value"]) for leaf in leaves if isinstance(leaf["value"], str)))
        facts.append({"id": f["rule_id"], "title": f["title"], "actor": actor or None, "condition": cond, "outcome": str((f["outcome"] or {}).get("kind", "")).replace("_", " ") or None,
                      "exceptions": list(f["exceptions"]), "authority": f["authority"], "status": f["status"], "implementation_status": f["implementation_status"],
                      "unknown_fields": list(f["unknown_fields"]), "numbers": sorted(_numbers_in(f["condition"]) | _numbers_in(f["title"]) | _numbers_in(f["exceptions"])), "words": sorted(words),
                      "leaves": [{**leaf, "value": _norm_number(str(leaf["value"])) if isinstance(leaf["value"], (int, float)) and not isinstance(leaf["value"], bool) else leaf["value"]} for leaf in leaves]})
    questions = [{"id": q["gap_id"], "question": q["question"], "status": q["status"], "priority": q["priority"], "rules": list(q["affected_rule_ids"]),
                  "numbers": sorted(_numbers_in(q["question"])), "words": sorted(set(w.lower() for w in _WORD.findall(q["question"])))} for q in doc["questions"]]
    ov = doc["overview"]
    return {"facts": facts, "questions": questions, "overview": dict(ov), "overview_numbers": {_norm_number(str(v)) for v in ov.values() if isinstance(v, int) and not isinstance(v, bool)},
            "truncated": len(view) > max_facts, "facts_in_document": len(view)}


def build_prompt(frozen: Mapping, audience: str, pack: Mapping) -> str:
    cfg = frozen["audiences"][audience]
    facts = "\n".join(f"- id={f['id']} | {f['title']} | who acts: {f['actor'] or 'not stated'} | when: {f['condition'] or 'not stated'} | what happens: {f['outcome'] or 'not stated'} | "
                      f"exceptions: {'; '.join(f['exceptions']) or 'none recorded'} | {f['authority']}/{f['status']} | in the code: {f['implementation_status']}"
                      + (" | claim values: " + "; ".join(f"operator={leaf['op']} value={leaf['value']} unit={leaf['unit'] or ''}" + (" (negated)" if leaf["negated"] else "") for leaf in f.get("leaves", []))
                         if f.get("leaves") else "")
                      + (f" | not known: {', '.join(f['unknown_fields'])}" if f["unknown_fields"] else "") for f in pack["facts"]) or "(no rule in scope)"
    questions = "\n".join(f"- id={q['id']} [{q['status']}/{q['priority']}] {q['question']} (rules: {', '.join(q['rules'])})" for q in pack["questions"]) or "(none)"
    overview = json.dumps(pack["overview"], sort_keys=True)
    sections = "\n".join(f"- {s}: {cfg['headings'][s]}" for s in cfg["sections"])
    prompt = frozen["prompt_template"].replace("{audience}", audience).replace("{guidance}", cfg["guidance"]).replace("{mandatory}", ", ".join(cfg["mandatory"])) \
        .replace("{sections}", sections).replace("{overview}", overview).replace("{facts}", facts).replace("{questions}", questions).replace("{narrative}", ", ".join(cfg["narrative"]) or "none")         .replace("{limits}", f"at most {frozen['limits']['max_sections']} sections, {frozen['limits']['max_paragraphs_per_section']} paragraphs per section, "
                             f"{frozen['limits']['max_paragraph_chars']} characters and {frozen['limits']['max_claims_per_paragraph']} claims per paragraph")
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
        if not isinstance(s, dict) or set(s) != {"id", "paragraphs"} or not isinstance(s["id"], str) or not isinstance(s["paragraphs"], list):
            raise ValueError("section shape: keys " + ",".join(sorted(map(str, s))) if isinstance(s, dict) else "section shape")
        if len(s["paragraphs"]) > 3 * limits["max_paragraphs_per_section"]:                 # beyond the cap check() leaves paragraphs out with a reason; far beyond it the answer is refused
            raise ValueError(f"section paragraphs: {len(s['paragraphs'])} > 3 x {limits['max_paragraphs_per_section']}")     # named, so a live failure is diagnosable without the output
        for p in s["paragraphs"]:
            if not isinstance(p, dict) or set(p) != {"text", "fact_ids", "claims"} or not isinstance(p["text"], str) or not isinstance(p["fact_ids"], list) \
                    or not all(isinstance(x, str) for x in p["fact_ids"]) or not isinstance(p["claims"], list):
                raise ValueError("paragraph shape")
            over = _over_limits(p, limits, factor=3)                                         # beyond the limits check() leaves the paragraph out; far beyond them the answer is refused
            if over:
                raise ValueError(f"paragraph limits: {over} over 3 x the limit")
            for c in p["claims"]:
                if not isinstance(c, dict) or set(c) != CLAIM_KEYS or not all(isinstance(c[k], str) for k in CLAIM_KEYS - {"exceptions"}) \
                        or not isinstance(c["exceptions"], list) or not all(isinstance(x, str) for x in c["exceptions"]):
                    raise ValueError("claim shape")
    if not isinstance(obj["omitted"], list) or len(obj["omitted"]) > limits["max_omitted"] or not all(isinstance(x, str) and len(x) <= 300 for x in obj["omitted"]):
        raise ValueError("omitted")
    return obj


def _over_limits(p: Mapping, limits: Mapping, factor: int = 1) -> str | None:
    for name, size, key in (("chars", len(p["text"]), "max_paragraph_chars"), ("fact_ids", len(p["fact_ids"]), "max_fact_ids_per_paragraph"),
                            ("claims", len(p["claims"]), "max_claims_per_paragraph")):
        if size > factor * limits[key]:
            return name
    return None


def check(model: Mapping, pack: Mapping, audience_cfg: Mapping, fidelity: Mapping | None = None, max_paragraphs: int | None = None, limits: Mapping | None = None) -> dict:
    """Keep what the facts support; return kept sections, rejected paragraphs with reasons, coverage, exceptions the kept wording leaves out and whether a mandatory section is
    missing (-> fallback). `fidelity` is the frozen config's vocabulary (contrast terms, currencies, legal words)."""
    fidelity = fidelity or {}
    by_id = {f["id"]: f for f in pack["facts"]} | {q["id"]: q for q in pack["questions"]}
    rule_ids = {f["id"] for f in pack["facts"]}
    kept, rejected, referenced = {}, [], set()
    seen_sections = set()
    for s in model["sections"]:
        if s["id"] not in audience_cfg["sections"] or s["id"] in seen_sections:
            rejected.append({"section": s["id"], "reason": "unknown_or_duplicate_section", "paragraphs": len(s["paragraphs"])})
            continue
        seen_sections.add(s["id"])
        narrative = s["id"] in audience_cfg["narrative"]
        for n, p in enumerate(s["paragraphs"]):
            text = " ".join(p["text"].split())
            if max_paragraphs is not None and n >= max_paragraphs:                          # over the per-section cap: left out and counted, the rest of the answer is still checked
                rejected.append({"section": s["id"], "reason": "over_paragraph_cap", "text": text[:160], "fact_ids": list(p["fact_ids"])})
                continue
            over = _over_limits(p, limits) if limits is not None else None                  # one paragraph over its size limits (live smoke LHA-SMOKE-20261018-09) is left out, not the answer
            if over:
                rejected.append({"section": s["id"], "reason": f"paragraph_over_limit:{over}", "text": text[:160], "fact_ids": list(p["fact_ids"])[:8]})
                continue
            claims = p.get("claims", [])
            # claim rows restate thresholds / actors / outcomes: a narrative paragraph that cites the rules it summarises but states no number and carries no claim has
            # nothing to restate (contract version 3 asks it to cite them; EV-VARIANT-V4 refused such summaries for missing rows). Its role words are still checked
            # against the cited facts and its sentences by the clause checks; with a number or any claim row, the rows must match as for every paragraph.
            claims_exempt = narrative and not claims and not _NUMBER.findall(text)
            reason = _paragraph_reason(text, p["fact_ids"], by_id, pack, narrative) \
                or (None if claims_exempt else _claims_reason(text, p["fact_ids"], claims, by_id, rule_ids)) \
                or _clause_reason(text, [by_id[x] for x in p["fact_ids"] if x in rule_ids], pack, fidelity)
            if reason:
                rejected.append({"section": s["id"], "reason": reason, "text": text[:160], "fact_ids": list(p["fact_ids"])})
                continue
            kept.setdefault(s["id"], []).append({"text": text, "fact_ids": sorted(set(p["fact_ids"])), "claims": [dict(c) for c in claims]})
            referenced |= {x for x in p["fact_ids"] if x in rule_ids}
    server_noted = _server_notes(audience_cfg, pack)
    missing = [m for m in audience_cfg["mandatory"] if not kept.get(m) and m not in server_noted]
    total = len(pack["facts"])
    omitted_exceptions = _omitted_exceptions(kept, by_id, referenced)
    kept_count = sum(len(v) for v in kept.values())
    coverage = {"referenced": len(referenced), "total": total, "complete": total > 0 and len(referenced) == total and not pack.get("truncated"),
                "validation": {"clause_checks": CLAUSE_CHECKS, "model_review": "not_performed", "kept": kept_count, "rejected": len(rejected), "omitted_exceptions": len(omitted_exceptions),
                               "facts_offered": total, "facts_in_document": pack.get("facts_in_document", total)}}
    return {"kept": kept, "rejected": rejected, "missing_mandatory": missing, "fallback": bool(missing) or model["status"] != "OK", "fact_coverage": coverage,
            "omitted": list(model["omitted"]), "omitted_exceptions": omitted_exceptions, "server_notes": server_noted}


def _server_notes(audience_cfg: Mapping, pack: Mapping) -> dict:
    """Sections the server writes itself because the facts leave nothing to word (live smoke LHA-SMOKE-20261018-05: an SOP of a version without any exception could never
    pass its mandatory exceptions section). Only when NO offered rule records an exception; the model's wording of such a section is still checked and kept if valid."""
    notes = audience_cfg.get("server_note_when_no_exceptions") or {}
    if not notes or any(f["exceptions"] for f in pack["facts"]):
        return {}
    return dict(notes)


def _norm_text(text: str) -> str:
    return " ".join(str(text).lower().replace("_", " ").replace("-", " ").split()).removeprefix("the ")


def _claims_reason(text: str, fact_ids: Iterable[str], claims: list, by_id: Mapping, rule_ids: set) -> str | None:
    """One claim row per cited rule, each equal to the canonical fact; a claimed threshold must be written in the text."""
    cited = [x for x in dict.fromkeys(fact_ids) if x in rule_ids]
    claimed = [c["fact_id"] for c in claims]
    if sorted(claimed) != sorted(cited):
        return "claims_do_not_match_cited_rules"
    numbers_in_text = {_norm_number(m) for m in _NUMBER.findall(text)}
    for c in claims:
        f = by_id[c["fact_id"]]
        if c["actor"] and _norm_text(c["actor"]) != _norm_text(f["actor"] or ""):
            return f"claim_mismatch:actor:{c['fact_id']}"
        if c["outcome"] and _norm_text(c["outcome"]) != _norm_text(f["outcome"] or ""):
            return f"claim_mismatch:outcome:{c['fact_id']}"
        if c["operator"] or c["value"] or c["unit"]:
            value = _norm_number(c["value"]) if _NUMBER.fullmatch(c["value"].strip() or "x") else c["value"].strip().lower()
            match = [leaf for leaf in f["leaves"] if leaf["op"] == c["operator"] and str(leaf["value"]).lower() == value
                     and (not c["unit"] or _norm_text(c["unit"]) == _norm_text(leaf["unit"] or ""))]
            if c["operator"] not in OPERATORS or not match:
                return f"claim_mismatch:condition:{c['fact_id']}"
            if _NUMBER.fullmatch(value) and value not in numbers_in_text:
                return f"claim_not_in_text:{c['value']}"
        elif any(str(leaf["value"]) in numbers_in_text for leaf in f["leaves"]):
            return f"claim_missing_threshold:{c['fact_id']}"
        known = {_norm_text(e) for e in f["exceptions"]}
        if any(_norm_text(e) not in known for e in c["exceptions"]):
            return f"claim_mismatch:exception:{c['fact_id']}"
    return None


def _clause_reason(text: str, refs: list, pack: Mapping, fidelity: Mapping) -> str | None:
    """Sentence-level agreement of the prose with the cited facts (no model): comparators, negation, actor-threshold binding, contrast terms, currencies, status, law."""
    for known in sorted([f["id"] for f in pack["facts"]] + [q["id"] for q in pack["questions"]], key=len, reverse=True):
        text = text.replace(known, " ")
    ref_words = set().union(*(set(r["words"]) for r in refs)) if refs else set()
    tokens = [w.lower() for w in re.findall(r"[a-z][a-z\-]*", text, re.I)]
    pack_units = {str(leaf["unit"]).lower() for f in pack["facts"] for leaf in f.get("leaves", []) if leaf["unit"]}
    allowed_units = (ref_words | {str(leaf["unit"]).lower() for r in refs for leaf in r["leaves"] if leaf["unit"]}) if refs else pack_units
    currencies = set(fidelity.get("currencies", []))
    for t in tokens:
        if t in currencies and _CURRENCY_ALIAS.get(t, t) not in {_CURRENCY_ALIAS.get(u, u) for u in allowed_units}:
            return f"unit_not_in_facts:{t}"
    for pair in fidelity.get("contrast_terms", []):
        for term in pair:
            if term in tokens and term not in ref_words and any(o in ref_words for o in pair if o != term):
                return f"contrast_term_not_in_facts:{term}"
    for t in tokens:
        if t in set(fidelity.get("legal_words", [])) and t not in ref_words:
            return f"legal_claim_not_in_facts:{t}"
    legal = _LEGAL_NAME.search(text)
    if legal and not _words(legal.group()) <= ref_words:
        return f"legal_claim_not_in_facts:{legal.group()}"
    if _COMPLIANCE.search(text) and "compliance" not in ref_words and "compliant" not in ref_words:
        return "compliance_claim_not_in_facts"
    if _STATUS_DECIDED.search(text) and not (refs and all(r["status"] == "CONFIRMED" for r in refs)):
        return "status_claim_not_in_facts:decided"
    if _STATUS_IMPLEMENTED.search(text) and not (refs and all(r["implementation_status"] == "verified" for r in refs)):
        return "status_claim_not_in_facts:implemented"
    if not refs:
        return None
    actors = sorted({r["actor"].lower() for r in pack["facts"] if r.get("actor")}, key=len, reverse=True)
    for sentence in _SENTENCE.split(text):
        low = sentence.lower()
        if _NEGATION.search(sentence) or _EXEMPTION.search(sentence):
            negative_leaf = any(leaf["negated"] or leaf["op"] in ("ne", "not_in") for r in refs for leaf in r["leaves"])
            exception_words = {w for r in refs for e in r["exceptions"] for w in _words(e) if len(w) > 3 and w not in _STOP}
            if not negative_leaf and not (exception_words & set(_words(sentence))):
                return "negation_not_in_facts"
        in_sentence = [a for a in actors if re.search(r"\b" + re.escape(a) + r"\b", low)]
        for clause in _CLAUSE.split(sentence):
            mentioned = [a for a in actors if re.search(r"\b" + re.escape(a) + r"\b", clause.lower())] or in_sentence     # a clause without a role is bound by the sentence
            for m in _NUMBER.finditer(clause):
                num = _norm_number(m.group())
                holders = [r for r in refs if any(str(leaf["value"]) == num for leaf in r["leaves"])]
                if not holders:
                    continue
                ops = {leaf["op"] for r in holders for leaf in r["leaves"] if str(leaf["value"]) == num}
                said = _comparator(clause[:m.start()], clause[m.end():])
                if said and said not in ops:
                    return f"comparator_mismatch:{m.group()}"
                if mentioned and any(r["actor"] for r in holders) and not any(r["actor"] and r["actor"].lower() in mentioned for r in holders):
                    return f"actor_number_binding:{mentioned[0]}"
    return None


def _comparator(before: str, after: str) -> str | None:
    for rx, op in _OP_AFTER:
        if rx.search(after):
            return op
    for rx, op in _OP_BEFORE:
        if rx.search(before):
            return op
    return None


def _omitted_exceptions(kept: Mapping, by_id: Mapping, referenced: set) -> list:
    """Exceptions of rules the kept wording describes that no kept paragraph citing the rule states in its text (most of the exception's words); a claim row alone does not count."""
    out = []
    for rid in sorted(referenced):
        paras = [p for ps in kept.values() for p in ps if rid in p["fact_ids"]]
        for exc in by_id[rid]["exceptions"]:
            words = {w for w in _words(exc) if w not in _STOP}
            stated = any(words and len(words & _words(p["text"])) >= max(1, round(0.6 * len(words))) for p in paras)
            if not stated:
                out.append({"fact_id": rid, "exception": exc})
    return out


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
            if not paras and sid in (checked.get("server_notes") or {}):
                lines += [f"## {da.md_text(audience_cfg['headings'][sid])}", "", da.md_text(checked["server_notes"][sid]) + " (stated by the server)", ""]
                continue
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
    validation = checked["fact_coverage"].get("validation") or {}
    if not checked["fallback"]:
        lines.append("- The server checked each kept paragraph clause by clause (comparators, negation, roles bound to their thresholds, categories, currencies, status and legal words); "
                     "no reviewer, human or model, has checked the meaning of the free wording.")
        if audience == "sop":
            lines.append("- The knowledge records no process order: the order of the steps above is a reading aid, not a verified order of execution.")
        if checked.get("omitted_exceptions"):
            lines.append("- Exceptions of the rules described above that the wording does not state (they still apply): "
                         + "; ".join(f"{da.md_text(o['exception'])} ({da.code(o['fact_id'])})" for o in checked["omitted_exceptions"]) + ".")
    if validation.get("facts_in_document", 0) > validation.get("facts_offered", 0):
        lines.append(f"- Only {validation['facts_offered']} of the {validation['facts_in_document']} rules of the document were given to the model; the others are in the table below and "
                     "are not covered by the wording.")
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


def variant_id_for(artifact_id: str, audience: str, locale: str, config_sha: str, model_id: str, attempt: int = 1) -> str:
    """The variant key: base, audience, locale, frozen config, pinned model, the server's clause-check version (a changed validator is a new variant, live smoke
    LHA-SMOKE-20261018-10 replayed a variant refused under the previous checks) and, from the second attempt on, the attempt (a failed variant can be asked again)."""
    key = {"a": artifact_id, "u": audience, "l": locale, "c": config_sha, "m": model_id, "v": CLAUSE_CHECKS}
    if attempt > 1:
        key["t"] = attempt
    return "dv-" + canonical_hash(key)[:24]
