# -*- coding: utf-8 -*-
"""Historical replay of two policy versions over one frozen cohort (MON-ROI, calculation engine; pure, no I/O, no model, no forecast).

The same records are decided under a BASELINE and a TARGET policy with the deterministic evaluator of MON-POL (`policy_decision.decide`), on exactly the same eligibility.
The engine counts what changes (approval routes, director involvement, value exposure by band) and turns counts into **declared-assumption** handling hours; a cost range
exists only when an hourly cost was given. Everything it cannot compute is `not_estimated`, never 0; an INDETERMINATE decision is an `unsupported` row that is reported and
excluded from every saving. Replay assumes historical volume and behaviour stay as they were: no behavioural response, no risk probability, no realised saving is claimed.

Engineering evidence on synthetic data proves the calculation; the package's completion gate (an authorised real sample, a data-quality/cohort review, the data owner's
acceptance) is outside this module.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.policy_decision import decide, effective_rules
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-roi-replay/1"
ENGINE_VERSION = "roi-replay/1"
SUPPORTED_CURRENCIES = ("JPY",)
REQUIRED = ("id", "time", "amount_jpy", "currency", "category", "process", "supplier_risk")
BANDS = ((0, 1_000_000, "lt_1m"), (1_000_000, 2_000_000, "1m_to_2m"), (2_000_000, 5_000_000, "2m_to_5m"), (5_000_000, None, "gte_5m"))
NOT_ESTIMATED = "not_estimated"


# ---- data quality: what enters the cohort, what is excluded and why -------------------------------------------------------------------------------------------------------

def _parse_time(value) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _amount(value) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value).replace(",", ""))
    except (ArithmeticError, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def quality_check(records: Iterable[Mapping], *, window_from: Optional[str] = None, window_to: Optional[str] = None) -> dict:
    """-> {"valid": [normalised records], "excluded": [{id, reason}], "profile": {...}}. Duplicate ids (second and later), missing fields, unparsable dates, negative or
    non-numeric amounts, unsupported currencies and records outside the effective window are excluded with a reason; nothing is silently repaired or converted."""
    seen, valid, excluded = set(), [], []
    start, end = _parse_time(window_from) if window_from else None, _parse_time(window_to) if window_to else None
    currencies: dict = {}
    for i, r in enumerate(records):
        rid = r.get("id") if isinstance(r, Mapping) else None
        key = str(rid) if rid is not None else f"<row {i}>"
        if not isinstance(r, Mapping) or rid in (None, ""):
            excluded.append({"id": key, "reason": "missing_id"})
            continue
        if key in seen:
            excluded.append({"id": key, "reason": "duplicate_id"})
            continue
        seen.add(key)
        missing = [f for f in REQUIRED if r.get(f) in (None, "")]
        if missing:
            excluded.append({"id": key, "reason": "missing_fields", "fields": missing})
            continue
        currencies[str(r["currency"])] = currencies.get(str(r["currency"]), 0) + 1
        if str(r["currency"]).upper() not in SUPPORTED_CURRENCIES:
            excluded.append({"id": key, "reason": "unsupported_currency", "currency": str(r["currency"])})
            continue
        t = _parse_time(r["time"])
        if t is None:
            excluded.append({"id": key, "reason": "bad_date"})
            continue
        if (start and t < start) or (end and t >= end):
            excluded.append({"id": key, "reason": "outside_window"})
            continue
        amount = _amount(r["amount_jpy"])
        if amount is None:
            excluded.append({"id": key, "reason": "bad_amount"})
            continue
        valid.append({"id": key, "time": t.isoformat(), "amount_jpy": amount, "category": str(r["category"]), "process": str(r["process"]), "supplier_risk": str(r["supplier_risk"])})
    by_reason: dict = {}
    for e in excluded:
        by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
    return {"valid": valid, "excluded": excluded, "profile": {"records_in": len(valid) + len(excluded), "valid": len(valid), "excluded_by_reason": by_reason, "currencies": currencies,
                                                               "window": {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None}}}


# ---- replay ------------------------------------------------------------------------------------------------------------------------------------------------------------------

def facts_of(record: Mapping) -> dict:
    return {"process": record["process"], "request.net_amount_jpy": str(record["amount_jpy"]), "request.category": record["category"], "supplier.risk": record["supplier_risk"]}


def route_of(decision: Mapping) -> dict:
    """What the decision asks of people: the sorted approver roles, whether a director is among them, the decision word."""
    actors = sorted({o.get("by") or "unknown" for o in decision.get("obligations") or []})
    return {"decision": decision["decision"], "approvers": actors, "director": any("director" in a.lower() for a in actors), "approvals": len(actors)}


def band_of(amount: Decimal) -> str:
    for lo, hi, name in BANDS:
        if amount >= lo and (hi is None or amount < hi):
            return name
    return "unbanded"


FACT_KEYS = ("process", "request.net_amount_jpy", "request.category", "supplier.risk")


def _fields_of(cond) -> set:
    if not isinstance(cond, Mapping):
        return set()
    if "field" in cond:
        return {str(cond["field"])}
    out: set = set()
    for key in ("all", "any"):
        for c in cond.get(key, []):
            out |= _fields_of(c)
    if "not" in cond:
        out |= _fields_of(cond["not"])
    return out


def evaluable_rules(rules: Iterable[Mapping], fact_keys: Iterable[str] = FACT_KEYS) -> tuple:
    """-> (rules the dataset can decide, [{rule_id, reason}] it cannot). A rule is evaluable when its condition exists and every condition field, every scope key and every
    exception condition field is a column of the dataset; the rest is reported as coverage, never silently treated as "no obligation" nor as "unsupported row"."""
    keys = set(fact_keys)
    kept, out = [], []
    for r in rules:
        if r.get("status") in ("SUPERSEDED", "REJECTED"):
            continue
        if r.get("condition") is None:
            out.append({"rule_id": r["rule_id"], "reason": "rule_condition_unknown"})
            continue
        fields = _fields_of(r["condition"]) | {k for k, v in (r.get("scope") or {}).items() if isinstance(v, list) and v}   # a marker such as {"unresolved": true} is not a field (C-11)
        for e in r.get("exceptions") or []:
            fields |= _fields_of(e.get("condition")) if isinstance(e, Mapping) else set()
        missing = sorted(fields - keys)
        if missing:
            out.append({"rule_id": r["rule_id"], "reason": "fields_not_in_dataset", "fields": missing})
        else:
            kept.append(r)
    return kept, out


def coverage_of(kept: list, not_evaluated: list) -> dict:
    """The rules that decide, as the evaluator applies them: a decided TARGET replaces the OBSERVED rule of the same root, so the replaced rule is listed apart and
    never as evaluated."""
    effective, superseded = effective_rules(kept)
    return {"rules_evaluated": sorted(r["rule_id"] for r in effective), "rules_superseded_by_target": superseded, "rules_not_evaluated": not_evaluated}


def replay(records: Iterable[Mapping], baseline: Mapping, target: Mapping, *, assumptions: Mapping, window_from: Optional[str] = None, window_to: Optional[str] = None,
           dataset_label: str = "") -> dict:
    """The baseline-vs-target report. `baseline` / `target`: {"label", "rules": [...], "gaps": [...]} as the product stores them. `assumptions`:
    {"minutes_per_approval": number, "minutes_per_approval_range": [low, high] (optional), "hourly_cost": number or None (optional)}. Deterministic: same inputs, same report hash."""
    minutes = _amount(assumptions.get("minutes_per_approval"))
    if minutes is None or minutes <= 0:
        raise ValueError("assumptions.minutes_per_approval must be a positive number (a declared assumption, never a default)")
    lo, hi = (assumptions.get("minutes_per_approval_range") or (None, None))[:2] if assumptions.get("minutes_per_approval_range") else (None, None)
    lo_m, hi_m = (_amount(lo) if lo is not None else minutes), (_amount(hi) if hi is not None else minutes)
    hourly = _amount(assumptions.get("hourly_cost")) if assumptions.get("hourly_cost") is not None else None
    q = quality_check(records, window_from=window_from, window_to=window_to)
    rows, unsupported = [], []
    pins = {"baseline": {"label": baseline.get("label")}, "target": {"label": target.get("label")}}
    base_rules, base_out = evaluable_rules(baseline.get("rules") or [])
    target_rules, target_out = evaluable_rules(target.get("rules") or [])
    coverage = {"dataset_fields": list(FACT_KEYS),
                "baseline": coverage_of(base_rules, base_out), "target": coverage_of(target_rules, target_out),
                "note": "only rules whose fields the dataset carries are replayed; the others are listed here and decide nothing in this report"}
    for rec in q["valid"]:
        facts = facts_of(rec)
        b = decide(base_rules, baseline.get("gaps") or [], facts, pin=pins["baseline"])
        t = decide(target_rules, target.get("gaps") or [], facts, pin=pins["target"])
        if b["decision"] == "INDETERMINATE" or t["decision"] == "INDETERMINATE":
            unsupported.append({"id": rec["id"], "amount_jpy": str(rec["amount_jpy"]), "baseline": b["decision"], "target": t["decision"],
                                "reasons": (b["indeterminate_reasons"] + t["indeterminate_reasons"])[:6]})
            continue
        rb, rt = route_of(b), route_of(t)
        rows.append({"id": rec["id"], "amount_jpy": rec["amount_jpy"], "band": band_of(rec["amount_jpy"]), "baseline": rb, "target": rt, "changed": rb != rt})

    def side(name):
        rs = [r[name] for r in rows]
        return {"approvals": sum(r["approvals"] for r in rs), "director_required": sum(1 for r in rs if r["director"]),
                "requires_approval": sum(1 for r in rs if r["decision"] == "REQUIRES_APPROVAL"), "deny": sum(1 for r in rs if r["decision"] == "DENY"),
                "allow": sum(1 for r in rs if r["decision"] == "ALLOW")}

    base_side, target_side = side("baseline"), side("target")
    changed = [r for r in rows if r["changed"]]
    by_band: dict = {}
    for r in changed:
        e = by_band.setdefault(r["band"], {"count": 0, "value_jpy": Decimal("0")})
        e["count"] += 1
        e["value_jpy"] += r["amount_jpy"]
    delta_approvals = target_side["approvals"] - base_side["approvals"]

    def exact_hours(n_approvals, mins):
        return Decimal(n_approvals) * mins / Decimal(60)

    def hours(n_approvals, mins):
        return exact_hours(n_approvals, mins).quantize(Decimal("0.01"))

    effort = {"basis": "count of approvals x declared minutes per approval; a proxy for handling effort, not measured durations",
              "baseline_hours": str(hours(base_side["approvals"], minutes)), "target_hours": str(hours(target_side["approvals"], minutes)),
              "delta_hours": str(hours(delta_approvals, minutes)) if delta_approvals >= 0 else "-" + str(hours(-delta_approvals, minutes)),
              "delta_hours_range": [str(hours(delta_approvals, lo_m)) if delta_approvals >= 0 else "-" + str(hours(-delta_approvals, lo_m)),
                                    str(hours(delta_approvals, hi_m)) if delta_approvals >= 0 else "-" + str(hours(-delta_approvals, hi_m))]}
    if hourly is not None:
        cost = {"basis": "delta hours x declared hourly cost; gross effort only, no implementation/API/operations cost, no horizon: NOT a net ROI",
                "delta_cost_range": [str(((exact_hours(abs(delta_approvals), m) * hourly) * (1 if delta_approvals >= 0 else -1)).quantize(Decimal("0.01"))) for m in (lo_m, hi_m)]}
    else:
        cost = NOT_ESTIMATED
    unsupported_value = sum((Decimal(u["amount_jpy"]) for u in unsupported), Decimal("0"))
    report = {"schema": SCHEMA, "engine_version": ENGINE_VERSION,
              "dataset": {"label": dataset_label, "hash": canonical_hash([dict(r, amount_jpy=str(r["amount_jpy"])) for r in q["valid"]]), **q["profile"]},
              "policies": {"baseline": {"label": baseline.get("label"), "hash": canonical_hash(baseline.get("rules") or [])}, "target": {"label": target.get("label"), "hash": canonical_hash(target.get("rules") or [])}},
              "assumptions": {"minutes_per_approval": str(minutes), "minutes_per_approval_range": [str(lo_m), str(hi_m)], "hourly_cost": str(hourly) if hourly is not None else None,
                              "behaviour": "historical volume and behaviour unchanged; counterfactual responses not modelled", "currency_scope": list(SUPPORTED_CURRENCIES)},
              "denominators": {"records_in": q["profile"]["records_in"], "cohort": len(q["valid"]), "replayed": len(rows), "unsupported": len(unsupported)},
              "coverage": coverage,
              "baseline": base_side, "target": target_side,
              "changes": {"count": len(changed), "value_jpy": str(sum((r["amount_jpy"] for r in changed), Decimal("0"))),
                          "by_band": {k: {"count": v["count"], "value_jpy": str(v["value_jpy"])} for k, v in sorted(by_band.items())},
                          "director_delta": target_side["director_required"] - base_side["director_required"], "approvals_delta": delta_approvals},
              "effort": effort, "cost": cost,
              "unsupported_rows": unsupported, "unsupported_value_jpy": str(unsupported_value), "excluded": q["excluded"],
              "risk": {"payment_delay": NOT_ESTIMATED, "loss": NOT_ESTIMATED, "note": "exposure metrics are defined proxies (counts and values by band), not probabilities or forecasts"},
              "statement": "Two policy versions replayed over the same frozen cohort with the deterministic evaluator, over the rules the dataset's fields can decide (see coverage). "
                           "Counts reconcile to the denominators; unsupported rows are listed and never turned into savings; effort is a declared-assumption proxy; no realised saving, "
                           "risk or net ROI is claimed."}
    report["report_hash"] = canonical_hash({k: v for k, v in report.items() if k != "report_hash"})
    return report
