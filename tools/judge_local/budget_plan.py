"""Offline budget plan for the live-local package, counted from the REAL callers (no database, no network, no provider).

Callers of provider jobs in the product (verified by reading the code):
  * extraction           SourcesPage button / import flow (one job per artifact) and the re-extraction wave after an apply (one job per artifact of the published revision)
  * intent.interpret     `/v2/chat` (`_dispatch`: one job per unanswered person turn; ENFORCED limit `max_ai_turns_per_conversation` = 6 in config/intent_interpret.json)
  * change.draft         the draft stage (one job per draft or regeneration)
  * gap.answer           free-text questions and queue answers (`frontend/src/features/freetext/surfaces.tsx`)
  * explain              `/v2/explanations` (wording of verified facts; flash-lite)
  * impact.judge         NO PRODUCT CALLER. Only the generic `POST /v2/jobs` route and the offline eval harness create it; the UI never does. Judging pairs therefore needs
                         an explicit operator step, and the number of pairs is limited here (`plan_judge_pairs(..., max_pairs)`), not by the product.
Only intent.interpret has a product-enforced count. Every other count below is a PLAN; the enforced bound is the project cap (admission refuses a job whose reservation does not
fit settled + reserved). A job's reservation covers its retry (calls per attempt x attempts), so a retry is never counted again.
"""
from __future__ import annotations

from decimal import Decimal

from infra.worker.budget_config import actual_call_cost_usd
from infra.worker.budget_invariants import max_total_provider_calls

#: planned jobs of ONE operator rehearsal (the same list as the decision package): flash jobs 13, flash-lite jobs 2.
PLANNED_REHEARSAL = {"intent.interpret": 9, "change.draft": 1, "gap.answer": 3, "explain": 2}
OBSERVED_FLASH_CALL = Decimal("0.022")     # average cost of an earlier live flash call (a planning figure, not a guarantee)
OBSERVED_LITE_CALL = Decimal("0.0055")     # derived from the price ratio, never observed


def job_reserve(config, model_id: str) -> Decimal:
    return actual_call_cost_usd(config, model_id=model_id, input_tokens=None, output_tokens=None) * max_total_provider_calls(config)


def plan_judge_pairs(changed_rules: list[dict], artifacts: dict, flagged: dict, *, max_pairs: int) -> list[tuple[str, str]]:
    """The (changed rule, artifact) pairs an operator would judge: every rule-bearing artifact of the rule's bundle, except the rule's own artifact and the ones the deterministic
    rules already flagged (`flagged[candidate_id]` = set of artifact ids). Refuses (ValueError) when the count exceeds `max_pairs`: the limit is explicit, never implied by N-1."""
    from infra.worker import impact
    pairs = []
    for rule in changed_rules:
        own = rule["artifact_id"]
        bundle = impact.bundle_prefix(artifacts[own])
        for aid in sorted(artifacts):
            a = artifacts[aid]
            if aid == own or impact.bundle_prefix(a) != bundle or not impact._is_rule_bearing(a) or aid in flagged.get(rule["candidate_id"], ()):
                continue
            pairs.append((rule["candidate_id"], aid))
    if len(pairs) > max_pairs:
        raise ValueError(f"{len(pairs)} judge pairs exceed the explicit limit {max_pairs}: narrow the changed rules or raise the limit deliberately")
    return pairs


def plan(config, n_artifacts: int, *, judge_pairs: int = 0, flash: str = "gemini-3.8-flash", lite: str = "gemini-3.5-flash-lite") -> dict:
    R, E = job_reserve(config, flash), job_reserve(config, lite)
    n_flash = sum(v for k, v in PLANNED_REHEARSAL.items() if k != "explain")
    n_lite = PLANNED_REHEARSAL["explain"]
    initial, wave = n_artifacts * R, n_artifacts * R
    rehearsal = n_flash * R + n_lite * E
    judge = judge_pairs * R
    exp = lambda calls_flash, calls_lite=0: calls_flash * OBSERVED_FLASH_CALL + calls_lite * OBSERVED_LITE_CALL
    local_worst = initial + rehearsal + judge + wave
    out = {"R": R, "E": E, "n_artifacts": n_artifacts, "judge_pairs": judge_pairs,
           "initial_extraction": initial, "rehearsal": rehearsal, "impact_judge": judge, "wave": wave, "local_worst": local_worst,
           "local_expected": exp(n_artifacts + n_flash + judge_pairs + n_artifacts, n_lite),
           "cloud_canary": 2 * R + E,
           "cloud_session_worst": rehearsal + judge + wave, "cloud_session_expected": exp(n_flash + judge_pairs + n_artifacts, n_lite)}
    # What a cap C must hold: everything SETTLED before the wave plus the wave's reservation made at once.
    calls_before_wave = n_artifacts + n_flash + judge_pairs
    out["wave_reservation"] = wave
    out["calls_before_wave"] = calls_before_wave
    out["_ratio_den"] = calls_before_wave * OBSERVED_FLASH_CALL + n_lite * OBSERVED_LITE_CALL
    return out


def completes_within_ratio(p: dict, cap: Decimal) -> Decimal | None:
    """How many times the observed average call cost the planned path can cost and still complete under `cap` (None when even the wave reservation alone exceeds the cap)."""
    room = Decimal(cap) - p["wave_reservation"]
    return None if room <= 0 else room / p["_ratio_den"]


if __name__ == "__main__":      # python -m tools.judge_local.budget_plan [config.yaml] [N]
    import sys
    from infra.worker.job_quota import load_worker_budget_config
    cfg = load_worker_budget_config(sys.argv[1] if len(sys.argv) > 1 else "docs/runbooks/pitch_budget_config.yaml")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 13
    for pairs in (0, n - 1, 2 * (n - 1)):
        p = plan(cfg, n, judge_pairs=pairs)
        print(f"N={n} judge_pairs={pairs}: " + ", ".join(f"{k}={v:.5f}" for k, v in p.items() if isinstance(v, Decimal) and not k.startswith("_")))
        for cap in ("6.00",):
            r = completes_within_ratio(p, Decimal(cap))
            print(f"   cap {cap}: completes within {r:.2f}x the observed average call cost" if r else f"   cap {cap}: the wave alone does not fit")
