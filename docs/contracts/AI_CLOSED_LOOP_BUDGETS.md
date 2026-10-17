# AI closed-loop budget/SLO config contract

**Status:** implemented (`infra/worker/budget_config.py`). A config missing a critical budget key fails worker startup and job admission. This file is the single source of truth for the loader interface.
**Inputs:** the owner budget decisions (`docs/product/ai_closed_loop_support.md` §0.3), ADR-005 §2.9 ("no budget default"; the spike worker refuses to start without `--max-output-tokens`), and the observation from the live provider run that `model_id=routed` was recorded with no token usage — the exact gap this contract decides how to handle (§7.2, §7.3).

## 1. Where this lives

- **Loader module:** `infra/worker/budget_config.py` (implemented) — ADR-005's promotion table places worker-owned code (jobs/providers) under `infra/worker/`; this budget loader is worker-owned in the same sense (the worker consults it at startup and at every job admission).
- **Example config:** `config/ai_closed_loop/budgets.example.yaml` (checked in, never containing a real API key or a real per-workspace override — it is the shape reference, not a deployed config).
- **Deployed config path:** not fixed by this contract; the loader takes an explicit path argument (§2) so deployment tooling decides where the real file lives, e.g. a mounted secret or a repo-adjacent `config/` file per environment.

## 2. Function signatures and exception types (exact)

```python
# infra/worker/budget_config.py

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class BudgetConfigError(Exception):
    """Raised by `load_budget_config` when the file is missing, unparsable, or missing/invalid
    on any CRITICAL key (§3). The worker's `main()` catches this at startup and exits 2 (§5),
    exactly like the spike worker's `--max-output-tokens` config-error convention."""


class BudgetExceeded(Exception):
    """Raised by `check_admission` (never by the loader) when spent + estimate > cap. Carries
    the machine-readable fields the API layer turns into the `budget_exceeded` error code
    (contracts §6; docs/product/ai_closed_loop_support.md §3)."""

    def __init__(self, *, scope: str, cap: Decimal, spent: Decimal, estimate: Decimal):
        self.scope = scope          # "per_job" or "total"
        self.cap = cap
        self.spent = spent
        self.estimate = estimate
        super().__init__(
            f"budget_exceeded: {scope} cap {cap} exceeded (spent={spent}, estimate={estimate}, "
            f"would_total={spent + estimate})"
        )


@dataclass(frozen=True)
class ModelPrice:
    """Money is always a Decimal, never a float (ADR-005 §2.7's float ban restated for cost).
    Prices are USD per 1,000 tokens, matching common provider pricing-page granularity."""
    model_id: str
    input_usd_per_1k: Decimal
    output_usd_per_1k: Decimal


@dataclass(frozen=True)
class BudgetConfig:
    """The fully validated, immutable config. Every field here is present and type-checked;
    `load_budget_config` never returns a partially-populated instance -- it raises
    BudgetConfigError instead (§3, §5)."""
    per_job_cap_usd: Decimal
    total_cap_usd: Decimal
    max_output_tokens: int
    max_input_tokens_per_call: int  # the input-side per-call ceiling. Fail-closed cost prices
                                   # a missing input side at THIS, never at max_output_tokens.
    max_retries: int              # must equal core.agent.provider.complete_structured's
                                   # retry_limit convention (spike default 1); a mismatch
                                   # between this value and the actual complete_structured
                                   # call is a startup validation error (§4).
    max_provider_calls_per_job: int  # OPTIONAL in the YAML (not critical): defaults to
                                   # max_retries + 1; if given, must equal max_retries + 1
                                   # (consistency rule, §3).
    model_prices: dict[str, ModelPrice]   # keyed by model_id
    default_model_id: str                  # must be a key in model_prices
    fail_closed_on_missing_usage: bool     # see §7 "cost when usage is unavailable"
    latency_slo_p50_seconds: float
    latency_slo_p95_seconds: float


def load_budget_config(path: str | None = None) -> BudgetConfig:
    """Reads and validates the YAML file at `path`. A
    set, non-empty env `RIPPLE_BUDGET_CONFIG_PATH` WINS OVER the `path` argument -- the
    loader itself prefers the env var, not merely "the env var overrides the config" in
    some looser sense; `path=None` with no env var set is also a BudgetConfigError (no
    implicit default path). Raises BudgetConfigError for: file not found, YAML parse error,
    a missing CRITICAL key (§3), a CRITICAL key of the wrong type (e.g. a YAML float where a
    decimal string is required), a money value that is not a valid Decimal literal, a
    non-positive cap/limit, `default_model_id` not present in `model_prices`, or an env
    override (§6) that itself fails these same checks. Never returns a partially valid
    config -- validation is all-or-nothing per §4."""


def check_admission(
    config: BudgetConfig, *, scope: str, spent_usd: Decimal, estimate_usd: Decimal,
) -> None:
    """Raises BudgetExceeded if `spent_usd + estimate_usd > cap` for the named scope
    (`scope` is "per_job" or "total"; ValueError for any other value). Returns None
    (does not return a value) when admission is granted -- this mirrors
    `core.execution.budget.check_budget`'s existing "return the violation, do not raise"
    style is EXPLICITLY NOT reused here: this function raises, because the worker's
    call site (§5) must abort startup/admission via exception, not via an ignorable
    return value -- ADR-005 §2.9's "no budget default" principle extends to "no budget
    check that can be silently ignored." """


def estimate_job_cost_usd(
    config: BudgetConfig, *, model_id: str, estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> Decimal:
    """Pre-flight estimate, used as `estimate_usd` in `check_admission` before a job is
    claimed. Raises KeyError if `model_id` is not in `config.model_prices` (callers must
    catch this and treat an unpriced model as a configuration error, `budget_exceeded`
    is NOT the right code for an unpriced model -- see §5's exit-2 rule)."""


def actual_call_cost_usd(
    config: BudgetConfig, *, model_id: str, input_tokens: int | None, output_tokens: int | None,
) -> Decimal:
    """Post-call actual cost. `input_tokens`/`output_tokens` are `None` when the provider
    did not report usage (§7 -- the live-run gap: model_id=routed, no usage). Behaviour is
    governed by `config.fail_closed_on_missing_usage` (§7): True -> returns the WORST-CASE
    estimate (config.max_output_tokens at config.model_prices[model_id].output_usd_per_1k,
    plus a fixed input-token estimate the caller supplies via `estimated_input_tokens` --
    see the two-argument overload note in §7); False -> returns Decimal("0") and the caller
    is responsible for surfacing that cost tracking degraded for this call (never silently
    treated as a free call in aggregate reporting)."""
```

## 3. Critical keys (must be present and of the stated type, or the loader raises `BudgetConfigError`)

| Key (YAML path) | Type | Constraint |
|---|---|---|
| `per_job_cap_usd` | decimal string, e.g. `"1.00"` | `> 0`; parses as `Decimal` |
| `total_cap_usd` | decimal string, e.g. `"50.00"` | `> 0`; parses as `Decimal`; **not required to be `>= per_job_cap_usd`** (a demo could in principle run many sub-cap jobs, but a single job cap larger than the total cap is a config error, checked explicitly) |
| `max_output_tokens` | int | `> 0` (ADR-005 §2.9 "no budget default" — this is the same value the spike's `--max-output-tokens` flag required, now sourced from config instead of a CLI flag the caller must remember to pass) |
| `max_input_tokens_per_call` | int | `> 0`. The input-side per-call ceiling, independent of `max_output_tokens`. Used two ways: (a) as the worst-case input-token count for fail-closed cost pricing (§7.3) and the pre-job admission estimate (§5); (b) as the ceiling the worker checks a document's OWN estimated prompt size against, before calling the provider at all (§7.1a) — a document whose estimated prompt exceeds this is refused with `budget_exceeded`, never sent |
| `max_retries` | int | `>= 0`; **must equal** `core.agent.provider.complete_structured`'s `retry_limit` parameter value actually used by the worker's call site — the loader cannot verify this by itself (it does not import the worker), so this is a **startup cross-check** the worker performs once at boot (§5), not a loader-internal check |
| `model_prices` | mapping, `model_id -> {input_usd_per_1k, output_usd_per_1k}` (both decimal strings) | non-empty; every price `>= 0`; every `model_id` string non-empty |
| `default_model_id` | string | must be a key in `model_prices` |
| `fail_closed_on_missing_usage` | bool | required, no implicit default (§7 decision below is `true`, but the config must say so explicitly — a missing key is a `BudgetConfigError`, not a silent `true`) |
| `latency_slo_p50_seconds` | float | `> 0`; matches `docs/evaluation/ai_closed_loop_metrics.md` §3.12's target table per milestone (this file does not duplicate the H/P split — the deployed config picks one milestone's numbers) |
| `latency_slo_p95_seconds` | float | `> 0`; `>= latency_slo_p50_seconds` |

**Money is always a decimal string in the YAML file and a `Decimal` in `BudgetConfig`, never a `float`, at any point in this pipeline.** A YAML value like `1.00` (bare, unquoted) parses to a Python `float` via a standard YAML loader — `load_budget_config` **must** read money keys as strings (e.g. via a custom YAML loader that keeps these specific keys as strings, or by requiring the YAML author to quote them, checked by "is this key's raw loaded value an instance of `str`") and reject a bare numeric YAML scalar for any money key with `BudgetConfigError`, not silently coerce it. This restates ADR-005 §2.7's float ban as an explicit loader validation rule, because unlike most of that rule's other call sites, a config file is hand-authored and the most likely place a float typo (`1.00` instead of `"1.00"`) slips in.

**`max_provider_calls_per_job` is OPTIONAL, not critical:** if absent, it defaults to `max_retries + 1`. If present, it must equal `max_retries + 1` exactly, or the loader raises `BudgetConfigError` naming the inconsistency — it is deliberately not on the critical-key list above, because making it critical would invalidate every config that omits it (including the checked-in example); the default keeps such configs valid while still letting a config be explicit about the number when it wants to be.

### 3a. One run-time meaning of `max_provider_calls_per_job` and `max_retries`

The key has two readings that must not be confused (resolved 2026-10-07; no number changed).
Two readings of the same key existed. The shared loader above (used only by the single-call spike worker) reads it as the TOTAL provider calls of a job and requires `== max_retries + 1`.
The worker contract (`infra/worker/job_quota.py::load_worker_budget_config`, used by every product path: job creation, chat, explain, refresh, claim) reads it as the calls of ONE ATTEMPT:
`ProviderCallBudget(limit)` is created per attempt, a job runs at most `max_retries + 1` attempts, so the most calls a job can make is `max_provider_calls_per_job x (max_retries + 1)`, and the reservation taken at creation is
`worst_call(most expensive configured model) x that total`. **The per-attempt reading is the contract.** The Pitch candidate config (1 call per attempt, 1 retry) therefore means at most 2 calls and reserves 0.25632 USD per job; it is not
expressible in the total reading without removing the retry, so the shared loader's rejection of it is the spike's special case, not a defect of the config.
`infra/worker/budget_invariants.py` states this once (`attempts`, `max_total_provider_calls`, `job_reservation_usd`, `problems`, `attempts_over_estimate`); `tests/pitch_brief/test_budget_semantics.py` proves it against the real routes and worker.
Run-time rules that make the cap hold: (1) a job reserves its whole retry window at creation (user-created AND system-created provider jobs, including the re-extraction wave); (2) a manual retry reserves a fresh window and is refused when it does not fit;
(3) settlement books the cost as reported and is never clamped; a booked cost above the per-call worst case is an overrun that the next admission sees and `attempts_over_estimate` lists (it must be empty at acceptance); (4) a wave that cannot reserve every
job stops, reports the unscheduled artifacts (`budget_deferred_artifact_ids`) and never reads as complete.

## 4. Validation rules (all-or-nothing)

`load_budget_config` validates **every** critical key before returning or raising — it does not stop at the first error. `BudgetConfigError`'s message lists **every** failing key found in one pass (a config with 3 bad keys reports all 3, not just the first), so a human fixing the config does not have to re-run the loader 3 times. This is a deliberate divergence from "fail on first error" specifically because this file is hand-edited, not machine-generated.

## 5. Admission function and startup behaviour

- **Worker startup:** the worker calls `load_budget_config(path)` once, before claiming any job. A `BudgetConfigError` here (missing file, invalid/missing critical key) **exits the worker process with code 2** — identical convention to the spike worker's `--max-output-tokens`/`--provider` configuration-error exit code. The worker also cross-checks `config.max_retries` against the `retry_limit` it actually passes to `complete_structured` at this point; a mismatch is also a startup `BudgetConfigError`-equivalent exit 2 (this cross-check lives in the worker's startup code, not in `budget_config.py` itself, per §3's note).
- **Job admission** (before `jobs.claim()` in worker terms, or before a job is accepted via the API in server terms): the caller computes `estimate_usd = estimate_job_cost_usd(config, model_id=…, estimated_input_tokens=config.max_input_tokens_per_call, estimated_output_tokens=config.max_output_tokens)` (the input side of the estimate is `config.max_input_tokens_per_call`, never `config.max_output_tokens`) and calls `check_admission(config, scope="per_job", spent_usd=Decimal("0"), estimate_usd=estimate_usd)`, then separately `check_admission(config, scope="total", spent_usd=<sum of all actual_call_cost_usd so far this evaluation/demo window>, estimate_usd=estimate_usd)`. Either raising `BudgetExceeded` refuses the job with `budget_exceeded` (contracts §6) **before** the job is claimed/queued — a refused job is never enqueued at all (distinct from a job that queues and then fails; `budget_exceeded` is a pre-admission refusal, matching the owner decision's exact wording "the job is refused with `budget_exceeded`").
- **Post-call accounting:** after each real provider call, the caller records `actual_call_cost_usd(...)` against the running `total` spend (persisted — see §9) so the next job's `total` admission check sees an accurate `spent_usd`.
- **A missing/invalid critical key surfaces identically at both startup and job admission** — i.e., if the config file is edited to become invalid *after* the worker started (a live reload path, if one is ever built), the next job-admission call must re-validate and refuse with the same exit-2-equivalent failure mode (surfaced as a `budget_exceeded`-adjacent hard error at the API layer, since a running worker process cannot literally "exit 2" mid-job-admission without dropping other in-flight work — this is listed in §9).
- **Worker-level pre-flight admission:** after a successful claim and **before any provider call**, the worker computes a per-job worst-case estimate — `estimate_job_cost_usd(config, model_id=config.default_model_id, estimated_input_tokens=config.max_input_tokens_per_call, estimated_output_tokens=config.max_output_tokens) * config.max_provider_calls_per_job` — on the **default** model, deliberately conservative and not necessarily the model the run's actual `--provider` will resolve to. It then calls `check_admission(config, scope="per_job", spent_usd=Decimal("0"), estimate_usd=that_estimate)` (`spent_usd` is always `Decimal("0")` here: no persisted running-spend store exists, §9). When the estimate is refused (`BudgetExceeded`), the job is set to `FAILED` with `error_code="budget_exceeded"` via `jobs.fail(..., retryable=False)` — **not retried**, and **no `provider_call` row is written** (the refusal happens strictly before the provider is ever touched). This is a pre-flight gate on the worst case, not a substitute for the real per-call `total`-scope accounting §9 lists as a limitation. **This ceiling-based estimate alone does not bound any one document's real prompt size** — see §7.1a for the per-document check that closes that gap.

## 6. Env overrides

- `RIPPLE_BUDGET_CONFIG_PATH` — if set and non-empty, **wins over** the `path` argument the caller passed — this is a loader-internal preference, not merely "an override applied somewhere in the caller's own code": `load_budget_config(path)` checks this env var itself, first. Absent (and no argument given) means `load_budget_config(None)` raises `BudgetConfigError` — there is no implicit default path, matching ADR-005 §2.9's "no budget default" principle applied to the config's own location, not just its contents. If the env-resolved path does not exist, that is a `BudgetConfigError` exactly like a missing argument path.
- `RIPPLE_BUDGET_PER_JOB_CAP_USD` / `RIPPLE_BUDGET_TOTAL_CAP_USD` — if set, override the corresponding YAML keys **after** the file is loaded and validated (i.e., the file must still be valid on its own; an env override cannot rescue an otherwise-invalid file, and an override value is itself validated by the same "valid Decimal, `> 0`" rule as §3, raising `BudgetConfigError` if it is not). These two are the only overrides at H, matching the owner decision's two owner-set numbers (§0.3 of the support matrix) being the ones most likely to need a same-day demo-time adjustment without a code change.
- No env override exists for `model_prices`, `max_output_tokens`, `max_input_tokens_per_call`, `max_retries`, `max_provider_calls_per_job`, `fail_closed_on_missing_usage`, or the latency SLO targets — those require a config-file change (and, for `max_retries`, a worker startup restart to re-run the cross-check in §5).

## 7. The three ADR-005 gaps this contract closes, plus the input-ceiling fix

### 7.1 How `max_output_tokens` reaches the provider

**Decision: the contract names the dependency; the loader does not enforce it (it cannot inspect a provider request).** The spike's `--max-output-tokens` flag is a **worker-side configuration gate** (refuses to start without it) — it is not threaded into an actual provider request parameter (e.g. Vertex's `generation_config.max_output_tokens`), because the spike's providers are `replay:`/`fail:` only; no live call path carries it. `BudgetConfig.max_output_tokens` is the **single source of truth** value that:

1. Gates worker startup exactly as the spike flag did (§5), and
2. **Must** be threaded into a real provider adapter's request as the provider's own max-output-tokens parameter when a live (`vertex`/`ai_module`) call path is used. This is an explicit requirement on that adapter, not something assumed to happen because the config value exists.

### 7.1a Bounding one document's real prompt size

**Decision: the worker estimates each prompt's token count itself, before every real call, and refuses (`budget_exceeded`) if the estimate exceeds `max_input_tokens_per_call` — implemented, not deferred.** The pre-job admission estimate (§5) is a worst-case *ceiling* check: it assumes every real call this job makes costs at most `max_input_tokens_per_call` worth of input, which bounds the *job's* worst case but says nothing about whether any *specific* document's actual prompt is anywhere near that size. Without a per-document check, a ~700 KB document would be admitted and sent to the provider, because the admission estimate alone never inspects the document.

**Check, exact:** after fetching the source bytes (and before calling `extract.extract`, i.e. before any provider call), the worker builds the exact prompt it would send (`extract.build_prompt(logical_path, text)`) and estimates its token count with the function below. If `estimate > config.max_input_tokens_per_call`, the job is set `FAILED` / `error_code="budget_exceeded"` via `jobs.fail(..., retryable=False)` — not retried, and no `provider_call` row is written (the refusal is strictly before the provider is touched, identical in shape to the §5 pre-flight refusal, just measured on the real document instead of the ceiling).

**The estimator (stated exactly, so nothing about "conservative" is left to guessing):**

```text
estimated_tokens(prompt_text) = ceil(len(prompt_text.encode("utf-8")) / 2)
```

2 bytes/token is a deliberate over-estimate for ordinary English/ASCII prose (real tokenizers commonly run closer to ~4 bytes/token), chosen so the estimator never UNDER-counts a real prompt — the estimate exists to gate a call before it happens, not to predict provider billing precisely. A future, more accurate estimator (e.g. an actual tokenizer for the configured model) may replace this, provided it remains conservative (never estimates fewer tokens than the real count for any input this project's supported formats can produce) — this is a design constraint on any replacement, not just a description of the current choice.

### 7.2 How the concrete model id and token usage are recorded

**Decision: `provider_call.model_id` must be the concrete, resolvable model identifier the provider actually used — never a router label like `"routed"`** (the string a live spike run logged: "1 provider_call row: model_id=routed"). This is a **product requirement on a live provider adapter**, not something `budget_config.py` can enforce by itself (a config loader cannot inspect a provider's response). The dependency is recorded here so it is not lost: `docs/evaluation/ai_closed_loop_metrics.md` §3.12 reports "provider tokens per model," which is meaningless if `model_id` is a router label with no price in `model_prices` — `estimate_job_cost_usd`/`actual_call_cost_usd` both raise `KeyError` for an unpriced `model_id` (§2), which is the mechanical enforcement: **a job whose actual model resolves to an unpriced/unknown id fails cost accounting loudly (`KeyError`, surfaced by the caller as a worker-side error, not silently priced as `0`)**, rather than silently succeeding with an unaccounted cost. Token usage: when the provider's response does include usage (input/output token counts), the worker must pass them through to `actual_call_cost_usd` unchanged; the "no usage" case is exactly §7.3 below.

### 7.3 Cost when usage is unavailable

**Decision: fail closed (estimate worst-case), not silently free.** `BudgetConfig.fail_closed_on_missing_usage` defaults to no implicit value (§3 — the key is required) but this contract's **recommended and documented value is `true`**: when a provider call's response carries no token-usage metadata (the exact case observed in the live spike run), `actual_call_cost_usd(config, model_id=…, input_tokens=None, output_tokens=None)` returns the **worst-case cost**.

**Exact worst-case formula (a symmetric `max_output_tokens` ceiling on both sides would under-cost a large prompt):** each side now has **its own** ceiling — `config.max_input_tokens_per_call` for a missing input side, `config.max_output_tokens` for a missing output side. `actual_call_cost_usd`'s signature stays exactly as §2 fixes it (`input_tokens: int | None, output_tokens: int | None`, no extra parameter): `effective_input = input_tokens if input_tokens is not None else config.max_input_tokens_per_call` and `effective_output = output_tokens if output_tokens is not None else config.max_output_tokens`; `cost = price(effective_input) + price(effective_output)`. A **present** side is priced exactly as reported, never re-estimated. Partial usage (one side reported, one missing) therefore is never priced below what the reported side alone would cost, and full-missing-usage-with-fail-closed is never below `price(max_input_tokens_per_call) + price(max_output_tokens)`. This over-charges the running total against the true (possibly lower) cost, which is the safe direction for a hard cost ceiling — it can never let real spend silently exceed the tracked total, and — combined with §7.1a's per-document check — it cannot be defeated by a document whose real prompt is larger than `max_output_tokens` tokens.

`fail_closed_on_missing_usage=false` is supported as a config option (for a deployment that would rather under-count than over-refuse): when **both** sides are missing and this flag is `false`, `actual_call_cost_usd` returns `Decimal("0")` exactly (an explicit, documented zero, never an accidental byproduct of arithmetic on `None`). This is **not** the value this contract recommends, and any deployment setting it to `false` accepts that `total_cap_usd` (§0.3's ≤50.00 USD demo ceiling) is no longer a hard guarantee against unmetered provider calls. An **unpriced** `model_id` (e.g. the `routed` label) raises `KeyError` from the price lookup before any of the above applies, with or without usage reported (§7.2) — the fail-closed/fail-open choice only governs what happens once a model is known to be priced.

## 8. Worker CLI wiring

The spike worker takes `--budget-config PATH`, alongside the legacy `--max-output-tokens N` flag:

- **Exactly one** of `--budget-config` or `--max-output-tokens` is required. **Neither, or both**, is a configuration error — exit 2, job untouched (not claimed), identical convention to every other configuration error in this contract. A missing/invalid `--max-output-tokens` alone, with no `--budget-config`, is also exit 2.
- `--budget-config` is **the path forward**; `--max-output-tokens` is kept **only** for spike tests that predate this contract — a caller should prefer `--budget-config` for anything new.
- When `--budget-config` is given, `max_output_tokens` for the run comes from `config.max_output_tokens` (§7.1) — the flag is not read at all in that case.
- The worker also cross-checks `config.max_retries == WORKER_RETRY_LIMIT` (the constant matching `extract.py`'s actual `complete_structured` call, currently `1`) at this same startup point — a mismatch is exit 2, per §3/§5's cross-check note.

## 9. Known limitations

- `max_output_tokens` (§7.1) is not threaded into a live (non-replay) provider request: `--budget-config` supplies the *value*, but no live provider call site receives it yet.
- A live provider adapter that returns a real, priced `model_id` instead of a router label (§7.2) is required for cost accounting on a live provider.
- The running `total` spend is not persisted across jobs/processes: `check_admission` is a pure function over caller-supplied `spent_usd`, and where that running total is stored (a DB row, a file, an in-memory counter that does not survive a worker restart) is not decided here. The worker's own pre-flight admission check (§5) always passes `spent_usd=Decimal("0")` for exactly this reason — it checks the current job's own worst case against the caps, not a running total.
- The "config becomes invalid mid-run" reload behaviour noted at the end of the §5 admission bullets is not implemented.
- Wiring `check_admission`'s `BudgetExceeded` into the `budget_exceeded` HTTP/API error response (contracts §6) is a separate concern: the worker's own CLI path already does this (§5, §8).
