"""Independent expected results for the synthetic ROI replay sets (HK-ROI).

Written from the business description of the Harbor purchase policy and the published report contract (`docs/contracts/EXTENSION_GOVERNANCE.md`, "Historical replay"),
NOT from the engine: no import of `core` / `infra`, no rule grammar, no evaluator. Each policy is plain Python over the four dataset columns; the data-quality rules,
bands, effort and cost formulas follow the contract text. If this oracle and the engine disagree, the disagreement is a finding to explain, never a reason to copy the
engine's answer here.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

REQUIRED = ("id", "time", "amount_jpy", "currency", "category", "process", "supplier_risk")
MANAGER, FINANCE, DIRECTOR = "role-manager", "role-finance", "role-director"


# ---- the policies, in business words ---------------------------------------------------------------------------------------------------------------------------------------
# Baseline (Harbor, observed): a standard purchase of 1,000,000 JPY or more needs the manager; CAPEX of 2,000,000 JPY or more (standard or emergency) needs Finance; standard
# CAPEX of 5,000,000 JPY or more needs Finance (the same approver, counted once); a high-risk supplier (standard or emergency) needs the Director. The emergency routing rule has
# no condition in the knowledge, so it decides nothing in a replay. Any other process value is outside every rule's scope.

def _baseline_approvers(r, manager_from=Decimal(1_000_000), office_exception=False):
    p, a, c, risk = r["process"], r["amount"], r["category"], r["supplier_risk"]
    out = set()
    if p == "STANDARD" and a >= manager_from and not (office_exception and c == "OFFICE" and a < 2_000_000):
        out.add(MANAGER)
    if p in ("STANDARD", "EMERGENCY") and c == "CAPEX" and a >= 2_000_000:
        out.add(FINANCE)
    if p == "STANDARD" and c == "CAPEX" and a >= 5_000_000:
        out.add(FINANCE)
    if p in ("STANDARD", "EMERGENCY") and risk == "HIGH":
        out.add(DIRECTOR)
    return out


POLICIES = {
    "baseline": lambda r: _baseline_approvers(r),
    # S1: the decided change — the manager threshold moves to 1,500,000 JPY.
    "S1": lambda r: _baseline_approvers(r, manager_from=Decimal(1_500_000)),
    # S2: S1 plus an exception: office supplies below 2,000,000 JPY never need the manager. A further target rule reads a column the dataset does not have
    # (vendor country), so it is coverage only and decides nothing.
    "S2": lambda r: _baseline_approvers(r, manager_from=Decimal(1_500_000), office_exception=True),
    # S3: S1 while the CAPEX-from-2,000,000 rule has an open question: every row that rule applies to by scope (standard or emergency) cannot be decided on the target side,
    # so it is unsupported; an open question on the emergency routing rule (no condition, not replayed) changes nothing.
    "S3": lambda r: _baseline_approvers(r, manager_from=Decimal(1_500_000)),
}
UNDECIDABLE = {"S3": lambda r: r["process"] in ("STANDARD", "EMERGENCY")}
EVALUATED = {
    "baseline": ["rule-capex-finance-config", "rule-capex-finance-policy", "rule-high-risk-director", "rule-standard-manager-review"],
    "S1": ["rule-capex-finance-config", "rule-capex-finance-policy", "rule-high-risk-director", "rule-standard-manager-review-target"],
    "S2": ["rule-capex-finance-config", "rule-capex-finance-policy", "rule-high-risk-director", "rule-standard-manager-review-target"],
    "S3": ["rule-capex-finance-config", "rule-capex-finance-policy", "rule-high-risk-director", "rule-standard-manager-review-target"],
}
SUPERSEDED = {"baseline": [], "S1": ["rule-standard-manager-review"], "S2": ["rule-standard-manager-review"], "S3": ["rule-standard-manager-review"]}
NOT_EVALUATED = {
    "baseline": [("rule-emergency-routing", "rule_condition_unknown")],
    "S1": [("rule-emergency-routing", "rule_condition_unknown")],
    "S2": [("rule-emergency-routing", "rule_condition_unknown"), ("rule-vendor-country-review", "fields_not_in_dataset")],
    "S3": [("rule-emergency-routing", "rule_condition_unknown")],
}


# ---- data quality (contract: duplicates, missing fields, bad dates / amounts, unsupported currencies, outside the window; each excluded with a reason) ----------------------

def _time(v):
    if not isinstance(v, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None                     # the sets only carry offset-aware times; a naive time is not generated


def _amount(v):
    if isinstance(v, bool) or v is None or isinstance(v, (list, dict)):
        return None
    try:
        d = Decimal(str(v).replace(",", ""))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def classify(records, window_from, window_to):
    """-> (valid rows, excluded [[id, reason]] in input order; id None for a record without one). Every generated defective record carries exactly one defect,
    so its reason is unambiguous."""
    start, end = _time(window_from), _time(window_to)
    seen, valid, excluded = set(), [], []
    for r in records:
        rid = r.get("id")
        if rid in (None, ""):
            excluded.append([None, "missing_id"])
            continue
        rid = str(rid)
        if rid in seen:
            excluded.append([rid, "duplicate_id"])
            continue
        seen.add(rid)
        if any(r.get(f) in (None, "") for f in REQUIRED):
            excluded.append([rid, "missing_fields"])
            continue
        if r["currency"] != "JPY":
            excluded.append([rid, "unsupported_currency"])
            continue
        t = _time(r["time"])
        if t is None:
            excluded.append([rid, "bad_date"])
            continue
        if t < start or t >= end:                       # the analysis window is half-open: [from, to)
            excluded.append([rid, "outside_window"])
            continue
        a = _amount(r["amount_jpy"])
        if a is None:
            excluded.append([rid, "bad_amount"])
            continue
        valid.append({"id": rid, "amount": a, "category": r["category"], "process": r["process"], "supplier_risk": r["supplier_risk"]})
    return valid, excluded


def band(a):
    return "lt_1m" if a < 1_000_000 else "1m_to_2m" if a < 2_000_000 else "2m_to_5m" if a < 5_000_000 else "gte_5m"


def _hours(n, minutes):
    return (Decimal(n) * Decimal(str(minutes)) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def _signed(n, minutes):
    h = _hours(abs(n), minutes)
    return ("-" if n < 0 else "") + str(h)


def expected(records, scenario, *, window_from, window_to, assumptions):
    valid, excluded = classify(records, window_from, window_to)
    base_f, tgt_f, undecidable = POLICIES["baseline"], POLICIES[scenario], UNDECIDABLE.get(scenario, lambda r: False)
    rows, unsupported = [], []
    for r in valid:
        if undecidable(r):
            unsupported.append(r)
            continue
        b, t = base_f(r), tgt_f(r)
        rows.append({"id": r["id"], "amount": r["amount"], "b": b, "t": t})

    def side(key):
        s = [x[key] for x in rows]
        return {"approvals": sum(len(a) for a in s), "director_required": sum(1 for a in s if DIRECTOR in a), "requires_approval": sum(1 for a in s if a),
                "deny": 0, "allow": sum(1 for a in s if not a)}

    bs, ts = side("b"), side("t")
    changed = [x for x in rows if x["b"] != x["t"]]
    by_band = {}
    for x in changed:
        e = by_band.setdefault(band(x["amount"]), [0, Decimal(0)])
        e[0] += 1
        e[1] += x["amount"]
    delta = ts["approvals"] - bs["approvals"]
    m = assumptions["minutes_per_approval"]
    lo, hi = assumptions.get("minutes_per_approval_range", [m, m])
    out = {
        "denominators": {"records_in": len(valid) + len(excluded), "cohort": len(valid), "replayed": len(rows), "unsupported": len(unsupported)},
        "baseline": bs, "target": ts,
        "changes": {"count": len(changed), "value_jpy": str(sum((x["amount"] for x in changed), Decimal(0))),
                    "by_band": {k: {"count": v[0], "value_jpy": str(v[1])} for k, v in sorted(by_band.items())},
                    "director_delta": ts["director_required"] - bs["director_required"], "approvals_delta": delta},
        "effort": {"baseline_hours": str(_hours(bs["approvals"], m)), "target_hours": str(_hours(ts["approvals"], m)), "delta_hours": _signed(delta, m),
                   "delta_hours_range": [_signed(delta, lo), _signed(delta, hi)]},
        "unsupported_ids": sorted(r["id"] for r in unsupported),
        "unsupported_value_jpy": str(sum((r["amount"] for r in unsupported), Decimal(0))),
        "excluded": excluded,
        "changed_ids": sorted(x["id"] for x in changed),
        "coverage": {"baseline": EVALUATED["baseline"], "target": EVALUATED[scenario], "baseline_superseded": SUPERSEDED["baseline"], "target_superseded": SUPERSEDED[scenario],
                     "baseline_not_evaluated": NOT_EVALUATED["baseline"], "target_not_evaluated": NOT_EVALUATED[scenario]},
    }
    hourly = assumptions.get("hourly_cost")
    if hourly is None:
        out["cost"] = "not_estimated"
    else:
        sign = -1 if delta < 0 else 1
        out["cost"] = {"delta_cost_range": [str((Decimal(abs(delta)) * Decimal(str(x)) / Decimal(60) * Decimal(str(hourly)) * sign).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
                                            for x in (lo, hi)]}
    return out
