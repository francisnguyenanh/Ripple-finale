"""MON-ROI calculation engine (local half): a hand-calculable fixture and a reproducible synthetic cohort of 10,000 records replayed through two policy versions with the
deterministic evaluator. Pure: no provider, no DB, no real data (the completion gate — an authorised real sample and the data owner's acceptance — stays OPEN)."""
from __future__ import annotations

import copy
import json
import pathlib
import random
import time
from decimal import Decimal

import pytest

from core.change_intelligence.v2 import roi_replay as roi

ROOT = pathlib.Path(__file__).resolve().parents[2]
RULES = json.loads((ROOT / "tests" / "fixtures" / "final_knowledge_loop" / "expected_knowledge.json").read_text(encoding="utf-8"))["observed_rules"]
BASELINE = {"label": "revision-1", "rules": RULES, "gaps": []}


def _target():
    """The decided change: the manager threshold goes from 1,000,000 to 1,500,000 JPY (a TARGET rule replaces the OBSERVED one)."""
    t = copy.deepcopy(next(r for r in RULES if r["rule_id"] == "rule-standard-manager-review"))
    t.update(rule_id="rule-standard-manager-review-target", authority="TARGET", condition=dict(t["condition"], value=1500000))
    return {"label": "revision-2", "rules": RULES + [t], "gaps": []}


def rec(i, amount, category="OFFICE", process="STANDARD", risk="LOW", time_="2026-03-01T00:00:00Z", currency="JPY"):
    return {"id": f"r{i}", "time": time_, "amount_jpy": amount, "currency": currency, "category": category, "process": process, "supplier_risk": risk}


HAND = [rec(1, 900_000), rec(2, 1_200_000), rec(3, 1_400_000), rec(4, 1_600_000), rec(5, 2_500_000, category="CAPEX"), rec(6, 300_000, risk="HIGH"), rec(7, 1_200_000, process="EMERGENCY"),
        rec(8, 50_000)]
ASSUMPTIONS = {"minutes_per_approval": 20, "minutes_per_approval_range": [10, 30]}


def test_hand_calculable_fixture_counts_reconcile_and_the_effort_is_a_declared_assumption_proxy():
    rep = roi.replay(HAND, BASELINE, _target(), assumptions=ASSUMPTIONS, dataset_label="hand")
    d = rep["denominators"]
    assert d == {"records_in": 8, "cohort": 8, "replayed": d["replayed"], "unsupported": d["unsupported"]} and d["replayed"] + d["unsupported"] == 8
    # r2 (1.2M) and r3 (1.4M) stop needing the manager under the target; r4 (1.6M) still does; r1/r8 never did; r5 CAPEX stays with Finance; r6 high risk stays with the Director
    assert rep["changes"]["count"] == 2 and rep["changes"]["value_jpy"] == "2600000" and rep["changes"]["by_band"] == {"1m_to_2m": {"count": 2, "value_jpy": "2600000"}}
    assert rep["changes"]["approvals_delta"] == -2 and rep["changes"]["director_delta"] == 0
    assert rep["baseline"]["approvals"] - rep["target"]["approvals"] == 2
    assert rep["effort"]["delta_hours"] == "-0.67" and rep["effort"]["delta_hours_range"] == ["-0.33", "-1.00"]              # 2 approvals x 20 / 10 / 30 minutes
    assert rep["cost"] == "not_estimated" and rep["risk"]["payment_delay"] == "not_estimated"                               # no hourly cost declared: never 0
    assert rep["coverage"]["baseline"]["rules_not_evaluated"] == [{"rule_id": "rule-emergency-routing", "reason": "rule_condition_unknown"}]  # a rule without a condition decides nothing here
    assert rep["denominators"]["unsupported"] == 0                                                                           # the EMERGENCY row meets no evaluable rule: ALLOW on both sides
    tc = rep["coverage"]["target"]                                                                                           # the decided TARGET replaces the OBSERVED rule: only it is listed as evaluated
    assert "rule-standard-manager-review-target" in tc["rules_evaluated"] and "rule-standard-manager-review" not in tc["rules_evaluated"]
    assert tc["rules_superseded_by_target"] == ["rule-standard-manager-review"] and rep["coverage"]["baseline"]["rules_superseded_by_target"] == []
    with_cost = roi.replay(HAND, BASELINE, _target(), assumptions={**ASSUMPTIONS, "hourly_cost": 6000}, dataset_label="hand")
    assert with_cost["cost"]["delta_cost_range"] == ["-2000.00", "-6000.00"] and "NOT a net ROI" in with_cost["cost"]["basis"]
    assert all(r["amount_jpy"] for r in rep["unsupported_rows"]) and rep["unsupported_value_jpy"] == str(sum(Decimal(u["amount_jpy"]) for u in rep["unsupported_rows"]))
    again = roi.replay(HAND, BASELINE, _target(), assumptions=ASSUMPTIONS, dataset_label="hand")
    assert again["report_hash"] == rep["report_hash"] and again["dataset"]["hash"] == rep["dataset"]["hash"]                   # same inputs, same report


def test_quality_check_excludes_duplicates_missing_fields_bad_dates_foreign_currency_and_out_of_window_rows_with_reasons():
    rows = [rec(1, 900_000), rec(1, 900_000), {**rec(2, 1_200_000), "category": None}, rec(3, 1_000_000, time_="yesterday"), rec(4, 1_000_000, currency="USD"),
            rec(5, 1_000_000, time_="2025-12-31T23:59:59Z"), rec(6, -5), {"time": "2026-01-01T00:00:00Z"}, rec(7, "1,700,000")]
    q = roi.quality_check(rows, window_from="2026-01-01", window_to="2026-07-01")
    assert [v["id"] for v in q["valid"]] == ["r1", "r7"] and q["valid"][1]["amount_jpy"] == Decimal("1700000")
    assert q["profile"]["excluded_by_reason"] == {"duplicate_id": 1, "missing_fields": 1, "bad_date": 1, "unsupported_currency": 1, "outside_window": 1, "bad_amount": 1, "missing_id": 1}
    assert q["profile"]["currencies"] == {"JPY": 5, "USD": 1} and q["profile"]["records_in"] == 9
    rep = roi.replay(rows, BASELINE, _target(), assumptions=ASSUMPTIONS, window_from="2026-01-01", window_to="2026-07-01")
    assert rep["denominators"]["records_in"] == 9 and rep["denominators"]["cohort"] == 2 and len(rep["excluded"]) == 7


def test_rules_the_dataset_cannot_decide_are_coverage_not_savings_and_an_open_question_makes_a_row_unsupported():
    odd = [rec(1, 1_200_000, process="MYSTERY")]                                                             # a process no rule's scope names: every rule is out of scope -> ALLOW both sides
    rep = roi.replay(odd, BASELINE, _target(), assumptions=ASSUMPTIONS)
    assert rep["denominators"]["replayed"] + rep["denominators"]["unsupported"] == 1
    foreign = copy.deepcopy(BASELINE)
    foreign["rules"] = RULES + [dict(RULES[0], rule_id="rule-currency", title="Requests are in JPY", condition={"field": "request.currency", "op": "eq", "value": "JPY"})]
    rep = roi.replay([rec(1, 1_200_000)], foreign, _target(), assumptions=ASSUMPTIONS)
    assert rep["coverage"]["baseline"]["rules_not_evaluated"][-1] == {"rule_id": "rule-currency", "reason": "fields_not_in_dataset", "fields": ["request.currency"]}
    assert rep["denominators"]["unsupported"] == 0                                                           # a rule the data cannot decide is reported, not turned into unsupported rows
    questioned = copy.deepcopy(BASELINE)
    questioned["gaps"] = [{"gap_id": "gap-x", "kind": "missing_exception", "status": "OPEN", "affected_rule_ids": ["rule-standard-manager-review"]}]
    rep = roi.replay([rec(1, 1_200_000)], questioned, _target(), assumptions=ASSUMPTIONS)
    assert rep["denominators"]["unsupported"] == 1 and rep["changes"]["count"] == 0 and rep["unsupported_rows"][0]["baseline"] == "INDETERMINATE"
    with pytest.raises(ValueError):
        roi.replay(HAND, BASELINE, _target(), assumptions={})


def test_a_synthetic_cohort_of_ten_thousand_records_is_reproducible_and_reconciles(tmp_path):
    rng = random.Random(20261017)
    cats, procs, risks = ["OFFICE", "CAPEX", "SERVICE"], ["STANDARD", "EMERGENCY"], ["LOW", "HIGH"]
    rows = [rec(i, rng.choice([50_000, 400_000, 900_000, 1_100_000, 1_450_000, 1_600_000, 2_200_000, 4_000_000, 6_000_000]), category=rng.choice(cats), process=rng.choice(procs),
                risk=rng.choice(risks), time_=f"2026-{rng.randint(1, 6):02d}-{rng.randint(1, 28):02d}T09:00:00Z") for i in range(10_000)]
    t0 = time.perf_counter()
    rep = roi.replay(rows, BASELINE, _target(), assumptions=ASSUMPTIONS, dataset_label="synthetic-10k")
    elapsed = time.perf_counter() - t0
    d = rep["denominators"]
    assert d["records_in"] == 10_000 and d["cohort"] == 10_000 and d["replayed"] + d["unsupported"] == 10_000
    assert sum(v["count"] for v in rep["changes"]["by_band"].values()) == rep["changes"]["count"]
    assert rep["changes"]["approvals_delta"] == rep["target"]["approvals"] - rep["baseline"]["approvals"] <= 0            # a higher threshold never adds a manager approval
    assert roi.replay(rows, BASELINE, _target(), assumptions=ASSUMPTIONS, dataset_label="synthetic-10k")["report_hash"] == rep["report_hash"]
    (tmp_path / "timing.txt").write_text(f"10000 records replayed twice (baseline+target) in {elapsed:.2f} s", encoding="utf-8")
    assert elapsed < 120
