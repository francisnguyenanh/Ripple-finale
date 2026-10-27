"""Deterministic generator of the synthetic ROI replay sets (HK-ROI). SYNTHETIC: no record describes a real company, person or purchase.

    python tests/fixtures/roi_synthetic/generate.py [out_dir]      # default: this folder

Writes `scenarios.json` (the policies as the product stores them + declared assumptions), `dev.json` / `acceptance.json` (records + case labels), `expected_dev.json` /
`expected_acceptance.json` (computed by `oracle.py`, which never imports the engine) and `MANIFEST.json` (seeds, counts, sha256 of every file). Same seeds, same bytes.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oracle  # noqa: E402

ROOT = HERE.parents[2]
SCHEMA = "ripple-roi-synthetic/1"
WINDOW = ("2026-04-01T00:00:00Z", "2026-10-01T00:00:00Z")
SEEDS = {"dev": 20261027, "acceptance": 74151}
SIZES = {"dev": 300, "acceptance": 2000}
WITH_COST = {"minutes_per_approval": 20, "minutes_per_approval_range": [10, 30], "hourly_cost": 6000}
NO_COST = {"minutes_per_approval": 15, "minutes_per_approval_range": [12, 25]}
ASSUMPTIONS = {"S1": WITH_COST, "S2": WITH_COST, "S3": NO_COST}
CATEGORIES = (("OFFICE", 40), ("OPEX", 25), ("CAPEX", 20), ("IT", 15))
PROCESSES = (("STANDARD", 85), ("EMERGENCY", 12), ("URGENT", 3))
RISKS = (("LOW", 70), ("MEDIUM", 20), ("HIGH", 10))
BANDS = ((0, 999_999, 35), (1_000_000, 1_999_999, 35), (2_000_000, 4_999_999, 20), (5_000_000, 20_000_000, 10))


def scenarios():
    rules = json.loads((ROOT / "tests" / "fixtures" / "final_knowledge_loop" / "expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"]
    manager = next(r for r in rules if r["rule_id"] == "rule-standard-manager-review")
    target = copy.deepcopy(manager)
    target.update(rule_id="rule-standard-manager-review-target", authority="TARGET", status="CONFIRMED", condition=dict(manager["condition"], value=1500000))
    target_exc = copy.deepcopy(target)
    target_exc["exceptions"] = [{"text": "Office supplies below 2,000,000 JPY do not need the manager.",
                                 "condition": {"all": [{"field": "request.category", "op": "eq", "value": "OFFICE"},
                                                       {"field": "request.net_amount_jpy", "op": "lt", "value": 2000000, "unit": "JPY"}]}}]
    vendor = {"rule_id": "rule-vendor-country-review", "title": "Foreign vendors need a compliance review", "authority": "TARGET", "status": "CONFIRMED",
              "scope": {"process": ["STANDARD"]}, "condition": {"field": "request.vendor_country", "op": "neq", "value": "JP"},
              "exceptions": [], "outcome": {"kind": "review_required"}, "actor_ref": "role-compliance"}
    gaps = [{"gap_id": "gap-capex-config-threshold", "status": "OPEN", "affected_rule_ids": ["rule-capex-finance-config"]},
            {"gap_id": "gap-emergency-routing", "status": "OPEN", "affected_rule_ids": ["rule-emergency-routing"]}]
    return {"schema": SCHEMA, "window": {"from": WINDOW[0], "to": WINDOW[1]},
            "baseline": {"label": "SYNTHETIC revision-1 (Harbor observed rules)", "rules": rules, "gaps": []},
            "targets": {"S1": {"label": "SYNTHETIC S1 manager threshold 1,500,000 JPY", "rules": rules + [target], "gaps": []},
                        "S2": {"label": "SYNTHETIC S2 S1 + office exception + vendor-country rule", "rules": rules + [target_exc, vendor], "gaps": []},
                        "S3": {"label": "SYNTHETIC S3 S1 with open questions", "rules": rules + [target], "gaps": gaps}},
            "assumptions": ASSUMPTIONS}


def _pick(rng, weighted):
    return rng.choices([w[0] for w in weighted], [w[-1] for w in weighted])[0]


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def edge_cases(split):
    """Hand-listed cases per class; the acceptance list uses other ids, times, categories and companions than the development list."""
    p = "D" if split == "dev" else "A"
    day = "2026-05-12" if split == "dev" else "2026-08-19"
    cat_a, cat_b = ("OFFICE", "CAPEX") if split == "dev" else ("IT", "CAPEX")
    out = []

    def rec(n, amount, category=cat_a, process="STANDARD", risk="LOW", time=f"{day}T10:00:00Z", currency="JPY", **over):
        r = {"id": f"{p}-E{n:03d}", "time": time, "amount_jpy": amount, "currency": currency, "category": category, "process": process, "supplier_risk": risk}
        r.update(over)
        return r

    boundary = [999_999, 1_000_000, 1_499_999, 1_500_000, 1_999_999, 2_000_000, 4_999_999, 5_000_000, 0]
    for i, a in enumerate(boundary):
        out.append(("boundary", rec(i, a, category=cat_b if i % 2 else cat_a, time=f"{day}T1{i % 10}:00:0{i % 10}Z")))
    out += [("boundary", rec(20, "1,700,000")), ("boundary", rec(21, "1499999.99", category=cat_b)), ("boundary", rec(22, 1500000.0)),
            ("boundary", rec(23, 1_200_000, time=WINDOW[0])),                                             # first instant of the window: inside
            ("boundary", rec(24, 1_200_000, time="2026-10-01T08:59:59+09:00")),                         # 2026-09-30T23:59:59Z: inside
            ("boundary", rec(25, 1_200_000, time=WINDOW[1])),                                           # end instant: outside (half-open window)
            ("boundary", rec(26, 1_200_000, time="2026-04-01T08:59:59+09:00")),                         # 2026-03-31T23:59:59Z: outside
            ("missing", {k: v for k, v in rec(30, 1_200_000).items() if k != "id"}), ("missing", rec(31, 1_200_000, id="")),
            ("missing", rec(32, 1_200_000, category=None)), ("missing", rec(33, 1_200_000, supplier_risk="")),
            ("missing", {k: v for k, v in rec(34, 1_200_000).items() if k != "time"}),
            ("contradictory", rec(40, 1_300_000)), ("contradictory", rec(40, 3_300_000, category="CAPEX")),   # same id, other content: the first stands
            ("contradictory", rec(41, 800_000)), ("contradictory", rec(41, 800_000)),                         # exact repeat
            ("exception", rec(50, 1_600_000, category="OFFICE")), ("exception", rec(51, 2_000_000, category="OFFICE")),
            ("exception", rec(52, 1_800_000, process="URGENT")), ("exception", rec(53, 1_100_000, risk="MEDIUM")),
            ("exception", rec(54, 6_000_000, category="CAPEX", process="EMERGENCY", risk="HIGH")), ("exception", rec(55, 300_000, risk="HIGH", process="EMERGENCY")),
            ("invalid", rec(60, -1)), ("invalid", rec(61, "abc")), ("invalid", rec(62, True)), ("invalid", rec(63, "NaN")), ("invalid", rec(64, "Infinity")),
            ("invalid", rec(65, 1_200_000, currency="USD")), ("invalid", rec(66, 1_200_000, currency="EUR")),
            ("invalid", rec(67, 1_200_000, time="2026-02-30T00:00:00Z")), ("invalid", rec(68, 1_200_000, time="yesterday")), ("invalid", rec(69, 1_200_000, time=20260601))]
    return out


def generated(split, n):
    rng = random.Random(SEEDS[split])
    p = "D" if split == "dev" else "A"
    start = datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc)
    span = int(datetime.timedelta(days=183).total_seconds())
    out, valid_ids = [], []
    for i in range(n):
        lo, hi, _ = BANDS[rng.choices(range(len(BANDS)), [b[2] for b in BANDS])[0]]
        amount = rng.randint(lo, hi)
        if rng.random() < 0.5:
            amount = max(lo, (amount // 1000) * 1000)
        t = start + datetime.timedelta(seconds=rng.randrange(span))
        r = {"id": f"{p}-{i:05d}", "time": _iso(t), "amount_jpy": amount, "currency": "JPY", "category": _pick(rng, CATEGORIES), "process": _pick(rng, PROCESSES),
             "supplier_risk": _pick(rng, RISKS)}
        roll, case = rng.random(), "normal"
        if roll < 0.03:
            r["time"], case = _iso(start - datetime.timedelta(seconds=1 + rng.randrange(86400 * 20))), "outside_window"
        elif roll < 0.04:
            r["currency"], case = rng.choice(["USD", "EUR", "CNY"]), "invalid"
        elif roll < 0.05:
            r["amount_jpy"], case = rng.choice([-rng.randint(1, 10_000), "n/a", "12O0000"]), "invalid"
        elif roll < 0.06:
            r[rng.choice(["category", "process", "supplier_risk"])], case = None, "missing"
        elif roll < 0.07 and valid_ids:
            r["id"], case = rng.choice(valid_ids), "contradictory"
        if case == "normal":
            valid_ids.append(r["id"])
        out.append((case, r))
    return out


def build_split(split):
    pairs = generated(split, SIZES[split]) + edge_cases(split)
    records = [r for _, r in pairs]
    cases = [c for c, _ in pairs]
    return {"schema": SCHEMA, "split": split, "seed": SEEDS[split], "synthetic": True, "window": {"from": WINDOW[0], "to": WINDOW[1]}, "records": records, "cases": cases}


def _dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


def main(out_dir=HERE):
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sc = scenarios()
    _dump(out_dir / "scenarios.json", sc)
    for split in ("dev", "acceptance"):
        data = build_split(split)
        _dump(out_dir / f"{split}.json", data)
        exp = {"schema": SCHEMA, "split": split, "computed_by": "oracle.py (independent of the engine)",
               "scenarios": {s: oracle.expected(data["records"], s, window_from=WINDOW[0], window_to=WINDOW[1], assumptions=ASSUMPTIONS[s]) for s in ("S1", "S2", "S3")}}
        _dump(out_dir / f"expected_{split}.json", exp)
    files = ["scenarios.json", "dev.json", "acceptance.json", "expected_dev.json", "expected_acceptance.json"]
    manifest = {"schema": SCHEMA, "synthetic": True, "seeds": SEEDS, "sizes_generated": SIZES, "window": {"from": WINDOW[0], "to": WINDOW[1]},
                "sha256": {f: hashlib.sha256((out_dir / f).read_bytes()).hexdigest() for f in files},
                "sources": {f: hashlib.sha256((HERE / f).read_bytes()).hexdigest() for f in ("generate.py", "oracle.py")}}
    _dump(out_dir / "MANIFEST.json", manifest)
    return manifest


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else HERE)["sha256"], indent=1))
