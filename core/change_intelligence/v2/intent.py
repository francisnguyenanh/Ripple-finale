"""Deterministic v2 `intent.parse` extraction.

The goal is to drop the seed vocabulary in v2 while keeping the
negation/value-subject checks. The legacy `core.change_intelligence.
intent_parser.extract`/`core.change_intelligence.intent.validate` pair is
pure in-process Python with no v2 store/route/authority wiring, and its
`target=""` default path falls back to a caller-suppliable seed value with
no grounding check at all -- exactly the "seed vocabulary" behaviour this
module drops. This module is a SEPARATE, v2-native deterministic parser:
it never invents a target when none is grounded (a caller-supplied
`target_hint`, or a `structured_form["target"]`), it adds the "except ..."
negation/exclusion extraction the legacy parser does not have, and it
enforces the supported-currency/date allowlist (unsupported semantics
keep the raw request + a reason, never silently coerced -- never
silently stripped to become supported).

This module writes nothing and imports no store, mirroring the legacy
module's own "writes nothing" contract -- `infra.adapters.v2_postgres.
PostgresV2Store.parse_intent` is the only place a `v2_intents` row is
persisted.
"""

from __future__ import annotations

import re
from typing import Mapping

#: Supported-currency allowlist (supported expressions, currencies and
#: dates). USD only for now; every
#: other currency (including a garbled one such as "XYZ") is unsupported and
#: must be reported with a reason, never coerced.
SUPPORTED_CURRENCIES = frozenset({"USD"})

#: The only effective-date vocabulary this deterministic parser accepts: a
#: real calendar month name. Anything else ("the Nth of Blorktober") is kept
#: as the raw request plus an unsupported-date reason.
_SUPPORTED_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
})

#: Prompt-injection phrasing this deterministic parser flags. Detecting it
#: never changes which target/value is extracted (target/value always come
#: from `target_hint`/`structured_form`/the sentence's own numbers, never
#: from an instruction-like clause) and never mints a confirmed decision --
#: `intent.parse` only ever produces a PARSED/NEEDS_CLARIFICATION draft.
_INJECTION = re.compile(
    r"ignore (all )?(the )?previous instructions"
    r"|disregard (all )?(the )?prior instructions"
    r"|confirm this (intent|change|decision) as\b"
    r"|act as (system|admin|knowledge_owner)\b"
    r"|you are now\b"
    r"|reveal (the )?system prompt",
    re.IGNORECASE)

_EXCLUSION = re.compile(r"\bexcept\b(.*)$", re.IGNORECASE | re.DOTALL)
#: Anchored on the "to <value> <currency>" phrasing the example
#: sentences use. Deliberately NOT a bare `\d+` scan: a bare scan would also
#: match digits embedded in an unrelated identifier (e.g. the "1" inside a
#: `target_hint` such as "cand-1" quoted back inside an injection payload),
#: which would let injected text silently poison the extracted value -- the
#: exact failure this anchor exists to rule out.
_VALUE = re.compile(r"\bto\s+(\d+(?:\.\d+)?)\s*([A-Za-z]{3})?\b", re.IGNORECASE)
_EFFECTIVE = re.compile(r"\beffective\b\s+(.*)$", re.IGNORECASE | re.DOTALL)


def _normalize_exclusion(text: str) -> str:
    text = text.strip(" .,;")
    text = re.sub(r"^(during a|during|when|if)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bdeclared\s+", "declared_", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", "_", text.strip().lower())


def _extract_from_free_text(text: str) -> dict:
    """The free-text (a "budgeted worker" in principle --
    deterministic here, no model call) path. Value/currency come from the
    FIRST numeric+optional-currency token before any "except" clause;
    exclusions come from everything after "except"; effective date comes
    from anything after "effective", accepted ONLY if it names a real
    calendar month -- otherwise kept as an unsupported reason."""
    value = None
    currency = None
    head = _EXCLUSION.split(text, maxsplit=1)[0]
    match = _VALUE.search(head)
    if match:
        raw_value = float(match.group(1))
        value = int(raw_value) if raw_value == int(raw_value) else raw_value
        currency = (match.group(2) or "USD").upper()

    exclusions: list[str] = []
    excl_match = _EXCLUSION.search(text)
    if excl_match:
        clause = excl_match.group(1)
        # Stop the exclusion clause at a following "effective" clause, if any.
        clause = _EFFECTIVE.split(clause, maxsplit=1)[0]
        normalized = _normalize_exclusion(clause)
        if normalized:
            exclusions.append(normalized)

    effective_time = None
    unsupported_reason = None
    eff_match = _EFFECTIVE.search(text)
    if eff_match:
        raw_effective = eff_match.group(1).strip(" .,;")
        words = [w.strip(".,;").lower() for w in raw_effective.split()]
        if any(word in _SUPPORTED_MONTHS for word in words):
            effective_time = raw_effective
        else:
            unsupported_reason = f"unsupported effective date: {raw_effective!r}"

    return {"value": value, "currency": currency, "exclusions": exclusions,
            "effective_time": effective_time, "unsupported_reason": unsupported_reason}


def parse_v2_intent(*, raw_request: str | None = None,
                    structured_form: Mapping | None = None,
                    target_hint: str | None = None) -> dict:
    """Return a plain dict draft: target/value/currency/scope/exclusions/
    effective_time/questions/unsupported_reason/injection_detected/status.

    Exactly one of `raw_request`/`structured_form` is the primary entry
    point ("budgeted worker or structured form"); when both
    a `structured_form` and a `target_hint` are given, `structured_form`'s
    own `target` wins (it is the more specific, caller-typed input).
    `status` is "PARSED" only when a target is grounded, no unsupported
    currency/date was found, and no injection phrasing was detected;
    otherwise "NEEDS_CLARIFICATION" (never silently resolved).
    """
    if not raw_request and not structured_form:
        raise ValueError("raw_request or structured_form required")

    questions: list[str] = []
    injection_detected = bool(raw_request) and bool(_INJECTION.search(raw_request))

    if structured_form is not None and "old_value" in structured_form:
        # Pitch change.brief: strict structured form (exact numbers, JPY etc.); no regex path, no guessing.
        from core.change_intelligence.v2.brief_form import brief_intent_draft
        return brief_intent_draft(structured_form)

    if structured_form is not None:
        target = structured_form.get("target") or target_hint
        value = structured_form.get("value")
        currency = (structured_form.get("currency") or "USD").upper() if value is not None else None
        exclusions = list(structured_form.get("exclusions") or [])
        effective_time = structured_form.get("effective_time")
        scope = structured_form.get("scope")
        unsupported_reason = None
    else:
        target = target_hint
        extracted = _extract_from_free_text(raw_request)
        value = extracted["value"]
        currency = extracted["currency"]
        exclusions = extracted["exclusions"]
        effective_time = extracted["effective_time"]
        unsupported_reason = extracted["unsupported_reason"]
        scope = None

    if currency and currency not in SUPPORTED_CURRENCIES:
        reason = f"unsupported currency: {currency!r}"
        unsupported_reason = (f"{unsupported_reason}; {reason}"
                              if unsupported_reason else reason)

    if not target:
        questions.append("Which rule/target does this change apply to? No "
                         "grounded target was provided or inferable from the "
                         "request -- a brand-new/ambiguous target requires "
                         "knowledge/HITL before readiness.")
    if injection_detected:
        questions.append("The request contains instruction-like text that is "
                         "ignored by the parser; confirm the intended "
                         "target/value explicitly before this intent is "
                         "confirmed.")
    if unsupported_reason:
        questions.append(f"Unsupported semantics kept as raw request: "
                         f"{unsupported_reason}")

    status = ("NEEDS_CLARIFICATION"
             if (not target or injection_detected or unsupported_reason)
             else "PARSED")

    return {
        "target": target, "value": value, "currency": currency, "scope": scope,
        "exclusions": exclusions, "effective_time": effective_time,
        "questions": questions, "unsupported_reason": unsupported_reason,
        "injection_detected": injection_detected, "status": status,
    }


__all__ = ["SUPPORTED_CURRENCIES", "parse_v2_intent"]
