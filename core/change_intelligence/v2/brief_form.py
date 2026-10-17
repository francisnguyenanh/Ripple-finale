"""Pitch change.brief: strict normaliser for the STRUCTURED intent form (no regex parsing of free text).

A confirmed brief intent holds exactly: subject text, scope (e.g. {"city": "Tokyo"}), unit ("per night"),
currency ("JPY"), old value (15000) and new value (20000). Numbers are preserved exactly: a JSON number is kept;
a string is accepted only in the plain forms `15000`, `15,000` (proper thousands groups) or `15000.5`.
Anything else is NOT understood confidently, so the draft stays NEEDS_CLARIFICATION (the intent store's
existing status name for "not confirmable yet"); nothing is guessed or defaulted.

Pure: no I/O, no store import. Used only when the form carries an `old_value` key.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Mapping

#: Currencies this form accepts (an explicit allowlist; the legacy free-text parser stays USD-only).
BRIEF_CURRENCIES = frozenset({"USD", "JPY", "EUR", "GBP", "VND"})

_PLAIN = re.compile(r"^\d+(\.\d+)?$")
_GROUPED = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")


def parse_exact_number(raw):
    """-> int | float | None. Never rounds, never reads a number out of surrounding words."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return raw if raw == raw and raw not in (float("inf"), float("-inf")) else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not (_PLAIN.match(text) or _GROUPED.match(text)):
        return None
    try:
        dec = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return int(dec) if dec == dec.to_integral_value() and "." not in text else float(dec)


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalise_brief_form(form: Mapping) -> dict:
    """-> {"fields": {...} | None, "problems": [str]}. `fields` is present only when EVERY given field is understood.

    Required: subject, currency, old_value, new_value. Optional (checked when given, never defaulted):
    scope (mapping text->text), unit (text), applies_to (list of text), target_candidate_id (text; the human's
    confirmed choice among several matching rules)."""
    problems: list[str] = []
    subject = _text(form.get("subject")) or _text(form.get("target"))
    if subject is None:
        problems.append("subject text required")
    scope_raw = form.get("scope")
    scope: dict = {}
    if scope_raw is not None:
        if isinstance(scope_raw, Mapping):
            for k, v in scope_raw.items():
                if _text(k) is None or _text(v) is None:
                    problems.append("scope must map non-empty text keys to non-empty text values")
                    scope = {}
                    break
                scope[k.strip()] = v.strip()
        else:
            problems.append("scope must be an object")
    unit = None
    if form.get("unit") is not None:
        unit = _text(form.get("unit"))
        if unit is None:
            problems.append("unit must be non-empty text when given")
    applies = form.get("applies_to")
    applies_to: list = []
    if applies is not None:
        if isinstance(applies, list) and all(_text(x) is not None for x in applies):
            applies_to = [x.strip() for x in applies]
        else:
            problems.append("applies_to must be a list of non-empty text")
    target_id = None
    if form.get("target_candidate_id") is not None:
        target_id = _text(form.get("target_candidate_id"))
        if target_id is None:
            problems.append("target_candidate_id must be non-empty text when given")
    cur_raw = form.get("currency")
    currency = cur_raw.strip().upper() if isinstance(cur_raw, str) else None
    if currency not in BRIEF_CURRENCIES:
        problems.append(f"unsupported or missing currency: {cur_raw!r}")
        currency = None
    old = parse_exact_number(form.get("old_value"))
    new = parse_exact_number(form.get("new_value"))
    if old is None:
        problems.append(f"old_value not understood: {form.get('old_value')!r}")
    if new is None:
        problems.append(f"new_value not understood: {form.get('new_value')!r}")
    if old is not None and new is not None and old == new:
        problems.append("old_value equals new_value")
    if problems:
        return {"fields": None, "problems": problems}
    return {"fields": {"subject": subject, "scope": scope, "unit": unit, "currency": currency,
                       "old_value": old, "new_value": new, "applies_to": applies_to,
                       "target_candidate_id": target_id}, "problems": []}


def brief_intent_draft(form: Mapping) -> dict:
    """The `parse_v2_intent` result dict for a brief form (same keys), `status` PARSED only when fully understood."""
    norm = normalise_brief_form(form)
    f = norm["fields"]
    questions = [f"Not understood confidently, nothing was guessed: {p}" for p in norm["problems"]]
    return {"target": f["subject"] if f else (_text(form.get("subject")) or _text(form.get("target"))),
            "value": f["new_value"] if f else None, "currency": f["currency"] if f else None,
            "scope": f["scope"] if f else (form.get("scope") if isinstance(form.get("scope"), Mapping) else None),
            "exclusions": list(form.get("exclusions") or []), "effective_time": form.get("effective_time"),
            "questions": questions, "unsupported_reason": "; ".join(norm["problems"]) or None,
            "injection_detected": False, "status": "PARSED" if f else "NEEDS_CLARIFICATION"}
