# -*- coding: utf-8 -*-
"""Triage of unbound source values (survey item SUR-02, extends `business_graph.unbound_values` and AIX-K1): pure, no I/O, no model.

`unbound_values` lists numbers of the structured sources (a YAML key, a sheet cell with its header and row label) that no rule's binding or provenance points at. One model call
reads the offered values in their row/header context together with the rules of the version and classifies each as `policy_candidate` (it looks like a business threshold
or limit nobody extracted), `metadata` (an id, a code, a count, a date part, a layout number) or `ambiguous`. The server keeps an item only if its reference was offered, the
class is known, the text is plain, its numbers are the value itself or numbers of the cited rules, and the cited rules exist; a `policy_candidate` without a question for a
person is downgraded to `ambiguous`. Every offered value the model did not classify is listed as not classified, and values beyond the offer cap as not offered: nothing is
presumed. An item is a proposal for a person (status PROPOSED): never a rule, a gap, a binding or a change of the knowledge.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

CLASSES = ("policy_candidate", "metadata", "ambiguous")
MODEL_KEYS = {"status", "items", "notes"}
ORIGIN = "model_classification"
# markup, links and control characters are refused; underscores, '#' and brackets are allowed because a reason names the key or cell it reads (live smoke
# LHA-SMOKE-20261018-01: every reason that quoted `payment_term_days` was refused). Readers render the text escaped.
_UNSAFE = re.compile(r"[<>`|]|https?://|www\.|[\x00-\x08\x0b-\x1f\x7f]", re.I)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm(text) -> str:
    t = str(text).replace(",", "")
    return t.rstrip("0").rstrip(".") if "." in t else t


def _numbers(*texts) -> set:
    return {_norm(m) for t in texts if t is not None for m in _NUMBER.findall(str(t))}


def offer(unbound: Mapping, limits: Mapping) -> dict:
    """The values sent to the model (first `max_unbound_offered` of the listed ones) and what is not offered (the rest of the list plus everything beyond the list cap)."""
    values = list(unbound.get("values") or [])
    cap = int(limits.get("max_unbound_offered", 30))
    return {"values": values[:cap], "not_offered": max(0, int(unbound.get("count", len(values))) - min(cap, len(values))), "total": int(unbound.get("count", len(values)))}


def build_prompt(frozen: Mapping, values: Iterable[Mapping], rules: Iterable[Mapping]) -> str:
    from core.change_intelligence.v2.business_index import condition_text
    vlines = "\n".join(f"- ref={v['evidence_ref']} | value: {v['value']} | file: {v.get('file')} | location: {v.get('location')}"
                       + (f" | column header: {v['header']}" if v.get("header") else "") + (f" | row label: {v['row_label']}" if v.get("row_label") else "") for v in values) or "(none)"
    rlines = "\n".join(f"- id={r['rule_id']} | {r.get('title')} | when: {condition_text(r['condition']) if r.get('condition') is not None else 'not stated'}" for r in rules) or "(none)"
    prompt = frozen["unbound_prompt_template"].replace("{values}", vlines).replace("{rules}", rlines)
    if len(prompt) > frozen["limits"]["max_prompt_chars"]:
        raise ValueError("context_too_large")
    return prompt


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != MODEL_KEYS or obj["status"] not in ("OK", "UNSURE"):
        raise ValueError("object keys or status")
    if not isinstance(obj["items"], list) or len(obj["items"]) > limits["max_unbound_items"]:
        raise ValueError("items")
    for it in obj["items"]:
        if not isinstance(it, dict) or set(it) != {"evidence_ref", "class", "reason", "question", "rule_ids"} or not all(isinstance(it[k], str) for k in ("evidence_ref", "class", "reason", "question")) \
                or not isinstance(it["rule_ids"], list) or not all(isinstance(x, str) for x in it["rule_ids"]) or len(it["rule_ids"]) > 3 \
                or len(it["reason"]) > limits["max_reason_chars"] or len(it["question"]) > limits["max_question_chars"]:
            raise ValueError("item shape")
    if not isinstance(obj["notes"], list) or len(obj["notes"]) > limits["max_notes"] or not all(isinstance(n, str) and len(n) <= 300 for n in obj["notes"]):
        raise ValueError("notes")
    return obj


def resolve(model: Mapping, offered: Iterable[Mapping], rules: Mapping[str, Mapping]) -> dict:
    """-> {items (kept, PROPOSED), rejected (with reasons), not_classified (offered refs the model left out), notes}."""
    by_ref = {v["evidence_ref"]: v for v in offered}
    kept, rejected, seen = [], [], set()
    for it in model["items"]:
        ref, reason = it["evidence_ref"], None
        v = by_ref.get(ref)
        cited = [rules[x] for x in it["rule_ids"] if x in rules]
        allowed = (_numbers(v["value"], v.get("header"), v.get("row_label"), v.get("location")) if v else set()) \
            | _numbers(*[r.get("title") for r in cited], *[_cond_numbers(r.get("condition")) for r in cited])
        text = " ".join((it["reason"] + " " + it["question"]).split())
        for rid in it["rule_ids"]:
            text = text.replace(rid, " ")
        if v is None:
            reason = "value_not_offered"
        elif ref in seen:
            reason = "duplicate"
        elif it["class"] not in CLASSES:
            reason = "unknown_class"
        elif any(x not in rules for x in it["rule_ids"]):
            reason = "rule_not_in_pack"
        elif not it["reason"].strip():
            reason = "empty_reason"
        elif _UNSAFE.search(it["reason"]) or _UNSAFE.search(it["question"]):
            reason = "unsafe_text"
        elif _numbers(text) - allowed:
            reason = "number_not_in_value_or_rules"
        if reason:
            rejected.append({"evidence_ref": ref, "reason": reason})
            continue
        seen.add(ref)
        cls, disposition = it["class"], "as_proposed"
        if cls == "policy_candidate" and not it["question"].strip():
            cls, disposition = "ambiguous", "downgraded:no_question_for_a_person"
        kept.append({"evidence_ref": ref, "value": v["value"], "file": v.get("file"), "location": v.get("location"), "sheet": v.get("sheet"), "cell": v.get("cell"),
                     "header": v.get("header"), "row_label": v.get("row_label"), "class": cls, "class_proposed": it["class"], "disposition": disposition,
                     "reason": " ".join(it["reason"].split()), "question": " ".join(it["question"].split()) or None, "rule_ids": sorted(set(it["rule_ids"])), "origin": ORIGIN, "status": "PROPOSED"})
    not_classified = [r for r in by_ref if r not in seen]
    return {"items": kept, "rejected": rejected, "not_classified": not_classified, "notes": [" ".join(n.split()) for n in model["notes"] if not _UNSAFE.search(n)]}


def _cond_numbers(cond) -> str:
    out, stack = [], [cond]
    while stack:
        c = stack.pop()
        if isinstance(c, Mapping):
            if "field" in c:
                out.append(str(c.get("value")))
            stack += list(c.get("all") or []) + list(c.get("any") or []) + ([c["not"]] if "not" in c else [])
    return " ".join(out)


def job_id(snapshot_id: str, attempt: int, binding=None) -> str:
    from core.change_intelligence.v2.serialize import canonical_hash
    return "munb-" + canonical_hash({"snap": snapshot_id, "attempt": attempt, "stage": "unbound", **({"binding": binding} if binding else {})})[:24]
