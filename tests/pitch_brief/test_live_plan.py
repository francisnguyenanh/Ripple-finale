"""Offline budget plan counted from the real callers, the explicit judge-pair limit, and the per-role canary gate (pure functions: no database, no provider)."""
from __future__ import annotations

import pathlib
from decimal import Decimal

import pytest

from infra.worker.job_quota import load_worker_budget_config
from tools.judge_local import budget_plan as B
from tools.judge_local import canary as K

CFG_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "runbooks" / "pitch_budget_config.yaml"


@pytest.fixture(scope="module")
def cfg():
    import os
    old = os.environ.pop("RIPPLE_BUDGET_CONFIG_PATH", None)
    try:
        return load_worker_budget_config(str(CFG_PATH))
    finally:
        if old is not None:
            os.environ["RIPPLE_BUDGET_CONFIG_PATH"] = old


def test_01_t9c_plus_numbers_are_counted_from_jobs(cfg):
    p = B.plan(cfg, 13)
    assert p["R"] == Decimal("0.25632") and p["E"] == Decimal("0.07584")
    assert p["initial_extraction"] == Decimal("3.33216") and p["wave"] == Decimal("3.33216")
    assert p["rehearsal"] == Decimal("3.48384")                   
    assert p["local_worst"] == Decimal("10.14816") and p["local_expected"] == Decimal("0.869")
    assert p["cloud_canary"] == Decimal("0.58848") and p["cloud_session_worst"] == Decimal("6.81600")
    assert sum(v for k, v in B.PLANNED_REHEARSAL.items() if k != "explain") == 13


def test_02_a_judge_pair_is_a_job_and_pairs_follow_the_number_of_changed_rules(cfg):
    one, two = B.plan(cfg, 13, judge_pairs=12), B.plan(cfg, 13, judge_pairs=24)
    assert one["impact_judge"] == 12 * Decimal("0.25632") and two["impact_judge"] == 24 * Decimal("0.25632")
    assert one["local_worst"] == Decimal("13.22400") and two["local_worst"] == Decimal("16.29984")


def test_03_a_cap_is_a_bounded_trial_with_a_stated_ratio_not_a_guarantee(cfg):
    base, judged = B.plan(cfg, 13), B.plan(cfg, 13, judge_pairs=12)
    assert round(B.completes_within_ratio(base, Decimal("6.00")), 2) == Decimal("4.58")
    assert round(B.completes_within_ratio(judged, Decimal("6.00")), 2) == Decimal("3.15")
    assert B.completes_within_ratio(base, Decimal("3.00")) is None                 # the wave reservation alone does not fit


def _arts():
    return {"a_cfg": {"artifact_id": "a_cfg", "logical_path": "harbor_demo/config/approval.yaml", "source_role": "unknown"},
            "a_doc": {"artifact_id": "a_doc", "logical_path": "harbor_demo/docs/p.md", "source_role": "unknown"},
            "a_code": {"artifact_id": "a_code", "logical_path": "harbor_demo/app/r.py", "source_role": "implementation"},
            "a_csv": {"artifact_id": "a_csv", "logical_path": "harbor_demo/data/x.csv", "source_role": "unknown"},
            "a_obs": {"artifact_id": "a_obs", "logical_path": "harbor_demo/data/y.xlsx", "source_role": "observational"},
            "a_other": {"artifact_id": "a_other", "logical_path": "other_bundle/z.md", "source_role": "unknown"}}


def test_04_judge_pairs_exclude_the_rules_own_artifact_data_files_flagged_ones_and_other_bundles_and_the_limit_is_explicit():
    rule = {"candidate_id": "c1", "artifact_id": "a_cfg"}
    pairs = B.plan_judge_pairs([rule], _arts(), {"c1": {"a_code"}}, max_pairs=5)
    assert pairs == [("c1", "a_doc")]
    assert len(B.plan_judge_pairs([rule], _arts(), {}, max_pairs=5)) == 2
    two = [rule, {"candidate_id": "c2", "artifact_id": "a_doc"}]
    assert len(B.plan_judge_pairs(two, _arts(), {}, max_pairs=10)) == 4          # pairs multiply with the changed rules: N-1 is per rule
    with pytest.raises(ValueError, match="explicit limit"):
        B.plan_judge_pairs(two, _arts(), {}, max_pairs=3)


def test_05_no_product_caller_creates_impact_judge_jobs():
    import re
    root = pathlib.Path(__file__).resolve().parents[2]
    hits = []
    for base in ("infra", "frontend/src", "core"):
        for p in (root / base).rglob("*"):
            if p.suffix in (".py", ".ts", ".tsx") and "node_modules" not in p.parts:
                text = p.read_text(encoding="utf-8", errors="ignore")
                if re.search(r"create_job\([^)]*[\"']impact\.judge[\"']|kind:\s*[\"']impact\.judge[\"']", text):
                    hits.append(str(p.relative_to(root)))
    assert hits == []


# ------------------------------------------------------------------------------------------------------------------ canary
def _row(job, kind, model, **kw):
    base = {"job_id": job, "kind": kind, "outcome": "COMPLETED", "model_id": model, "prompt_hash": "p", "schema_hash": "s", "input_tokens": 1000, "output_tokens": 500,
            "cost_estimate_usd": "0.12816", "cost_usd": "0.02", "recorded_at": job}
    base.update(kw)
    return base


def test_06_each_role_is_released_only_by_its_own_kind(cfg):
    flash = cfg.role_model_ids["extraction"]
    rows = [_row("j1", "extraction", flash)]
    led = {"j1": {"entry_state": "SETTLED", "cost_usd": "0.02"}}
    rep = K.report(rows, led, cfg, ["extraction", "change.draft"])
    assert rep["extraction"]["status"] == "PASS" and rep["change.draft"]["status"] == "NOT_OBSERVED"       # the extraction canary does not release the draft


def test_07_findings_wrong_model_missing_usage_over_ceiling_unsettled(cfg):
    flash, lite = cfg.role_model_ids["extraction"], cfg.default_model_id
    led = {"j": {"entry_state": "SETTLED", "cost_usd": "0.02"}}
    wrong = K.report([_row("j", "change.draft", lite)], led, cfg, ["change.draft"])["change.draft"]
    assert wrong["status"] == "FAIL" and "configured model" in wrong["findings"][0]
    nousage = K.report([_row("j", "gap.answer", flash, input_tokens=None)], led, cfg, ["gap.answer"])["gap.answer"]
    assert nousage["status"] == "FAIL" and any("usage missing" in f for f in nousage["findings"])
    big = K.report([_row("j", "intent.interpret", flash, output_tokens=cfg.max_output_tokens + 1)], led, cfg, ["intent.interpret"])["intent.interpret"]
    assert big["status"] == "FAIL" and any("output tokens" in f for f in big["findings"])
    over = K.report([_row("j", "impact.judge", flash, cost_usd="0.2")], {"j": {"entry_state": "SETTLED", "cost_usd": "0.2"}}, cfg, ["impact.judge"])["impact.judge"]
    assert over["status"] == "FAIL" and any("worst-case estimate" in f for f in over["findings"])
    open_ = K.report([_row("j", "explain", lite)], {"j": {"entry_state": "RESERVED", "cost_usd": "0.07"}}, cfg, ["intent.explain"])["intent.explain"]
    assert open_["status"] == "FAIL" and any("SETTLED" in f for f in open_["findings"])


def test_08_a_retry_is_one_job_whose_ledger_equals_the_sum_of_its_attempts(cfg):
    flash = cfg.role_model_ids["extraction"]
    rows = [_row("j", "extraction", flash, cost_usd="0.02", recorded_at="1"), _row("j", "extraction", flash, cost_usd="0.03", recorded_at="2")]
    ok = K.report(rows, {"j": {"entry_state": "SETTLED", "cost_usd": "0.05"}}, cfg, ["extraction"])["extraction"]
    bad = K.report(rows, {"j": {"entry_state": "SETTLED", "cost_usd": "0.02"}}, cfg, ["extraction"])["extraction"]
    assert ok["status"] == "PASS" and bad["status"] == "FAIL"
