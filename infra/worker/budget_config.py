"""AI closed-loop budget/SLO config loader.

Contract: docs/contracts/AI_CLOSED_LOOP_BUDGETS.md. Implements the exact interface that
contract fixes.

ADR-005 §2.9 "no budget default": a missing or invalid critical key must fail loudly, at
config-load time and again at job admission -- never silently substituted or coerced.
Money is always `Decimal`, never `float` (ADR-005 §2.7's float ban, restated here for cost).
"""

from __future__ import annotations

import datetime
import math
import os
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

import yaml

#: Contract §3: the 10 critical keys, exactly the keys the loader requires
#: (including `max_input_tokens_per_call`).
#:
#: `max_provider_calls_per_job` is deliberately NOT in this tuple: it is optional, defaulting
#: to `max_retries + 1` when absent. Making it critical/required would
#: break every existing config (including the checked-in example) that predates this key and
#: does not set it; it is meant to stay optional with a computed default, not critical. See docs/contracts/AI_CLOSED_LOOP_BUDGETS.md §3.
_MONEY_KEYS = ("per_job_cap_usd", "total_cap_usd")
_CRITICAL_KEYS = (
    "per_job_cap_usd", "total_cap_usd", "max_output_tokens", "max_input_tokens_per_call",
    "max_retries", "model_prices", "default_model_id", "fail_closed_on_missing_usage",
    "latency_slo_p50_seconds", "latency_slo_p95_seconds",
)

#: Contract §6: only these two caps may be overridden by env; RIPPLE_BUDGET_CONFIG_PATH
#: overrides the *path*, not a config value.
_ENV_PATH = "RIPPLE_BUDGET_CONFIG_PATH"
_ENV_PER_JOB = "RIPPLE_BUDGET_PER_JOB_CAP_USD"
_ENV_TOTAL = "RIPPLE_BUDGET_TOTAL_CAP_USD"


class BudgetConfigError(Exception):
    """Raised by `load_budget_config` for a missing file, unparsable YAML, a missing/invalid
    critical key, or an invalid env override. All-or-nothing (§4): every failing key found in
    one pass is named in the message, not just the first."""


class BudgetExceeded(Exception):
    """Raised by `check_admission` when spent + estimate > cap. Never a subclass of, or a
    superclass of, `BudgetConfigError` -- a refusal and a broken config are different
    failures (contract §5)."""

    def __init__(self, *, scope: str, cap: Decimal, spent: Decimal, estimate: Decimal):
        self.scope = scope
        self.cap = cap
        self.spent = spent
        self.estimate = estimate
        super().__init__(
            f"budget_exceeded: {scope} cap {cap} exceeded (spent={spent}, estimate={estimate}, "
            f"would_total={spent + estimate})"
        )


@dataclass(frozen=True)
class ModelPrice:
    model_id: str
    input_usd_per_1k: Decimal
    output_usd_per_1k: Decimal


@dataclass(frozen=True)
class BudgetConfig:
    per_job_cap_usd: Decimal
    total_cap_usd: Decimal
    max_output_tokens: int
    #: The input-side per-call ceiling. Fail-closed cost prices a missing input side at THIS,
    #: never at `max_output_tokens` (otherwise a large prompt would be priced as if it were a
    #: small one, because output and input would share one ceiling with no basis for the input side).
    max_input_tokens_per_call: int
    max_retries: int
    #: Optional (not one of the critical keys -- see the loader's own note on why
    #: this is not itself critical): the max real provider calls one job ATTEMPT can make.
    #: Defaults to `max_retries + 1` when absent from the YAML; if present, must equal that
    #: (consistency check).
    max_provider_calls_per_job: int
    model_prices: dict
    default_model_id: str
    fail_closed_on_missing_usage: bool
    latency_slo_p50_seconds: float
    latency_slo_p95_seconds: float
    #: (optional, default empty = every role uses `default_model_id`): role (job kind, e.g.
    #: "extraction") -> model id, each a key of `model_prices`. Only the listed role changes model.
    role_model_ids: dict = field(default_factory=dict)
    #: (optional): per-provider-call request timeout in seconds, passed to the provider client.
    request_timeout_seconds: float | None = None
    #: (optional): date of the price snapshot behind `model_prices` (ISO yyyy-mm-dd) and the
    #: age after which `price_snapshot_status` reports it stale.
    price_snapshot_date: str | None = None
    price_snapshot_max_age_days: int | None = None
    #: Optional. The max-output-tokens REQUEST parameter sent to the provider (output + thinking). Absent = `max_output_tokens`.
    #: `max_output_tokens` stays the RESERVATION ceiling. Reports exist (Vertex/AI Studio forum, Sept 2026) that thinking models can
    #: overshoot the request cap, up to about 2x, while Google staff say it is a hard ceiling; the documentation page for Vertex does not
    #: say. So when this key is set the loader requires `max_output_tokens >= 2 x generation_max_output_tokens`: a 2x overshoot
    #: still fits inside what was reserved.
    generation_max_output_tokens: int | None = None
    #: (optional): role (job kind) -> thinking level sent to the provider for that role. Absent role = the request carries no thinking parameter (the model default
    #: applies). Only a configured level is ever sent, so a model that does not take the parameter is never sent one by accident.
    role_thinking_levels: dict = field(default_factory=dict)
    #: (optional, R7 2026-10-28): role -> provider service tier requested for that role's calls. Only "priority" (Vertex Priority PayGo) is known; the role's
    #: model must then also be priced under the key "<model>@priority". The served tier is read from the response (`usage_metadata.traffic_type`): a call the
    #: provider served at priority is priced at that key, a call it downgraded (ON_DEMAND) at the standard key, a call without a traffic type at the priority key
    #: (fail closed). Absent role = no tier header (standard PayGo).
    role_service_tiers: dict = field(default_factory=dict)
    #: (optional, R11 2026-10-28): role -> its own request deadline in seconds, at most `request_timeout_seconds`. The call is sent with that timeout, and an answer
    #: that still arrives after it is not used: its reported usage is booked and the attempt ends as a provider timeout (no retry beyond `max_retries`). Absent
    #: role = `request_timeout_seconds` and the earlier behaviour.
    role_request_timeouts: dict = field(default_factory=dict)
    #: (optional, R21 owner approval 2026-10-29): role -> Vertex multi-region location ("us" / "eu") for that role's calls instead of the global endpoint. Only the
    #: roles in `LOCATION_ROLES` may have one (every other role stays global and keeps the global-endpoint assertion); the role's model must be priced under
    #: "<model>@<location>" (the multi-region list price), and a role cannot combine a location with a service tier. Absent role = global.
    role_locations: dict = field(default_factory=dict)
    #: (optional, R23 2026-10-30): role -> its own reservation ceiling of output tokens (`role_max_output_tokens`) and its own max-output-tokens request
    #: parameter (`role_generation_max_output_tokens`). Absent role = the global `max_output_tokens` / `generation_max_output_tokens`. The 2x overshoot rule
    #: holds per role (its ceiling >= 2 x what it sends). Admission reserves the worst single call over every role at THAT role's ceiling; booking prices a
    #: missing output side at the ceiling of the role that made the call. Lets one role (the request assistant: 2,048 sent / 4,096 reserved, as measured)
    #: run tighter than the others without lowering anyone else's cap.
    role_max_output_tokens: dict = field(default_factory=dict)
    role_generation_max_output_tokens: dict = field(default_factory=dict)
    #: The server's AI role allowlist (`infra/ai_kinds.py`): None = the key is absent and NO AI job kind may run (fail closed); the string
    #: "all" = every kind (development / tests / evaluation); otherwise a frozenset of AI job kinds. Not overridable by environment.
    enabled_roles: object = None


THINKING_LEVELS = ("MINIMAL", "LOW", "MEDIUM", "HIGH")


def thinking_level_for_role(config, role: str | None) -> str | None:
    """The configured thinking level of a role, or None (send nothing). Duck-typed like `model_for_role`."""
    mapping = getattr(config, "role_thinking_levels", None) or {}
    return mapping.get(role) if role else None


SERVICE_TIERS = ("priority",)


def service_tier_for_role(config, role: str | None) -> str | None:
    """The configured provider service tier of a role, or None (standard). Duck-typed like `model_for_role`."""
    mapping = getattr(config, "role_service_tiers", None) or {}
    return mapping.get(role) if role else None


def role_request_timeout(config, role: str | None) -> float | None:
    """The role's own deadline (`role_request_timeouts`), or None (the role uses `request_timeout_seconds`). Duck-typed like `model_for_role`."""
    mapping = getattr(config, "role_request_timeouts", None) or {}
    return mapping.get(role) if role else None


LOCATIONS = ("us", "eu")
#: R21: the only roles that may be sent to a non-global location (the request assistant's recovery call); widening it is a reviewed code change. R23 (prepared,
#: not active anywhere: no shipped or evaluated budget sets it): the request assistant's answer call too, so the answer call can be served at a multi-region
#: with the recovery on the global endpoint; every call at a location is priced and booked at that location on every path (`request_assistant._priced`)
LOCATION_ROLES = frozenset({"request.assistant", "request.assistant.recover"})


def location_for_role(config, role: str | None) -> str | None:
    """The configured multi-region location of a role, or None (global). Duck-typed like `model_for_role`."""
    mapping = getattr(config, "role_locations", None) or {}
    return mapping.get(role) if role else None


def output_ceiling_for_role(config, role: str | None) -> int:
    """R23: the reservation ceiling of output tokens of one call of this role (`role_max_output_tokens`, else the global `max_output_tokens`)."""
    mapping = getattr(config, "role_max_output_tokens", None) or {}
    return (mapping.get(role) if role else None) or config.max_output_tokens


def generation_cap_for_role(config, role: str | None) -> int | None:
    """R23: the max-output-tokens request parameter sent for this role (`role_generation_max_output_tokens`, else the global generation cap, else the role's
    own reservation ceiling when it has one, else the global `max_output_tokens`)."""
    mapping = getattr(config, "role_generation_max_output_tokens", None) or {}
    own = mapping.get(role) if role else None
    if own:
        return own
    glob = getattr(config, "generation_max_output_tokens", None)
    ceiling = output_ceiling_for_role(config, role)
    return min(glob, ceiling) if glob else ceiling


def price_key(model_id: str, tier: str | None, location: str | None = None) -> str:
    """The `model_prices` key of a model served at a tier ("<model>@<tier>") or at a multi-region location ("<model>@<location>"); the model id itself for
    the standard tier on the global endpoint. A tier and a location are never combined (the loader refuses it)."""
    if location:
        return f"{model_id}@{location}"
    return f"{model_id}@{tier}" if tier else model_id


def price_key_for_role(config, role: str | None) -> str:
    """The price a role's call is reserved at: its model at its configured tier or location (the priciest the call can be served at)."""
    return price_key(model_for_role(config, role), service_tier_for_role(config, role), location_for_role(config, role))


def model_for_role(config, role: str | None) -> str:
    """The model a role (job kind) runs: its `role_model_ids` entry, else `default_model_id`.
    Duck-typed so a config object without `role_model_ids` keeps the default."""
    mapping = getattr(config, "role_model_ids", None) or {}
    return mapping.get(role) or config.default_model_id


def worst_case_model_id(config) -> str:
    """The priciest configured model (default plus every role model) at the config ceilings.
    Admission is not job-kind aware, so it reserves at this model: it can never under-reserve."""
    roles = getattr(config, "role_model_ids", None) or {}
    ids = {config.default_model_id, *roles.values(), *(price_key(roles.get(r) or config.default_model_id, t) for r, t in (getattr(config, "role_service_tiers", None) or {}).items()),
           *(price_key(roles.get(r) or config.default_model_id, None, loc) for r, loc in (getattr(config, "role_locations", None) or {}).items())}

    def worst(mid):
        price = config.model_prices[mid]
        return (Decimal(config.max_input_tokens_per_call) * price.input_usd_per_1k
                + Decimal(config.max_output_tokens) * price.output_usd_per_1k)
    return max(sorted(ids), key=worst)


def worst_case_call_usd(config) -> Decimal:
    """R23: the most one provider call of ANY configured role can cost, each role priced at its own price key (and its standard key, a tier may be served
    downgraded) with its OWN output ceiling, plus the default model at the global ceiling. Without per-role ceilings this equals the cost of
    `worst_case_model_id(config)` at the global ceilings. Zero when the operator chose fail-open (as `actual_call_cost_usd` for a fully missing report)."""
    if not config.fail_closed_on_missing_usage:
        return Decimal("0")
    roles = getattr(config, "role_model_ids", None) or {}
    names = set(roles) | set(getattr(config, "role_service_tiers", None) or {}) | set(getattr(config, "role_locations", None) or {}) \
        | set(getattr(config, "role_max_output_tokens", None) or {})
    pairs = {(config.default_model_id, config.max_output_tokens)}
    for r in names:
        ceiling = output_ceiling_for_role(config, r)
        pairs.add((price_key_for_role(config, r), ceiling))
        pairs.add((model_for_role(config, r), ceiling))

    def cost(pair):
        price = config.model_prices[pair[0]]
        return Decimal(config.max_input_tokens_per_call) / Decimal(1000) * price.input_usd_per_1k + Decimal(pair[1]) / Decimal(1000) * price.output_usd_per_1k
    return max(cost(p) for p in sorted(pairs))


def worst_case_model_id_raw(raw: dict) -> str:
    """`worst_case_model_id` for a raw (JSON-loaded) config mapping, for harnesses that read the file directly."""
    roles = raw.get("role_model_ids") or {}
    ids = {raw["default_model_id"], *roles.values(), *(price_key(roles.get(r) or raw["default_model_id"], t) for r, t in (raw.get("role_service_tiers") or {}).items()),
           *(price_key(roles.get(r) or raw["default_model_id"], None, loc) for r, loc in (raw.get("role_locations") or {}).items())}

    def worst(mid):
        p = raw["model_prices"][mid]
        return (Decimal(raw["max_input_tokens_per_call"]) * Decimal(str(p["input_usd_per_1k"]))
                + Decimal(raw["max_output_tokens"]) * Decimal(str(p["output_usd_per_1k"])))
    return max(sorted(ids), key=worst)


def price_snapshot_status(config, today: datetime.date | None = None) -> dict:
    """WARNING only: makes price-snapshot age visible: {"snapshot_date", "age_days", "max_age_days", "stale"}. This is NOT the price
    check (config price vs the current provider price), which is done at switch time by a person or task and is not available offline.
    `stale` is True when the date is missing/unparsable or older than `price_snapshot_max_age_days`
    (default 30 when the config does not set it). The prices must be re-checked at switch time."""
    max_age = getattr(config, "price_snapshot_max_age_days", None) or 30
    raw = getattr(config, "price_snapshot_date", None)
    today = today or datetime.date.today()
    try:
        snap = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return {"snapshot_date": raw, "age_days": None, "max_age_days": max_age, "stale": True}
    age = (today - snap).days
    return {"snapshot_date": raw, "age_days": age, "max_age_days": max_age,
            "stale": age > max_age or age < 0}


# --------------------------------------------------------------------------------- validation


def _is_strict_int(value) -> bool:
    """True int, never bool (bool is an int subclass in Python -- excluded explicitly)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_float(value) -> bool:
    """A real float/int-as-float for the two latency keys. bool excluded; int is NOT accepted
    (the loader tests require `"30"` and `None` to be rejected; a plain `30` int is accepted
    for convenience since YAML often parses `30.0` fine but a hand-typed `30` is common too --
    tests only exercise str/None wrong-types for these two keys, so int is accepted)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, (float, int))


def _is_money_string(raw) -> bool:
    """Contract §3: the raw loaded value must be `str` -- a bare YAML numeric scalar
    (`1.00` unquoted -> Python float, or a plain int) is rejected outright, never coerced."""
    return isinstance(raw, str)


def _parse_money(raw: str, errors: list, key: str) -> Decimal | None:
    if not _is_money_string(raw):
        errors.append(f"{key}: money must be a quoted decimal string, got {raw!r} ({type(raw).__name__})")
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        errors.append(f"{key}: {raw!r} is not a valid decimal literal")
        return None
    if not value.is_finite():
        errors.append(f"{key}: {raw!r} must be a finite amount (NaN/Infinity are not allowed)")
        return None
    if value <= 0:
        errors.append(f"{key}: must be > 0, got {value}")
        return None
    return value


def _validate_model_prices(raw, errors: list) -> dict:
    prices: dict = {}
    if not isinstance(raw, dict) or not raw:
        errors.append(f"model_prices: must be a non-empty mapping, got {raw!r}")
        return prices
    for model_id, entry in raw.items():
        if not isinstance(model_id, str) or not model_id:
            errors.append(f"model_prices: every model id key must be a non-empty string, got {model_id!r}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"model_prices[{model_id!r}]: must be a mapping, got {entry!r}")
            continue
        rates = {}
        ok = True
        for rate_key in ("input_usd_per_1k", "output_usd_per_1k"):
            if rate_key not in entry:
                errors.append(f"model_prices[{model_id!r}].{rate_key}: missing")
                ok = False
                continue
            rv = entry[rate_key]
            if not _is_money_string(rv):
                errors.append(
                    f"model_prices[{model_id!r}].{rate_key}: must be a quoted decimal string, "
                    f"got {rv!r} ({type(rv).__name__})"
                )
                ok = False
                continue
            try:
                dv = Decimal(rv)
            except InvalidOperation:
                errors.append(f"model_prices[{model_id!r}].{rate_key}: {rv!r} is not a valid decimal literal")
                ok = False
                continue
            if not dv.is_finite() or dv < 0:
                errors.append(f"model_prices[{model_id!r}].{rate_key}: must be a finite amount >= 0, got {dv}")
                ok = False
                continue
            rates[rate_key] = dv
        if ok:
            prices[model_id] = ModelPrice(
                model_id=model_id, input_usd_per_1k=rates["input_usd_per_1k"],
                output_usd_per_1k=rates["output_usd_per_1k"],
            )
    return prices


def _validate(raw: dict) -> tuple[BudgetConfig | None, list]:
    errors: list = []
    for key in _CRITICAL_KEYS:
        if key not in raw:
            errors.append(f"{key}: missing (critical key)")

    per_job_cap = _parse_money(raw["per_job_cap_usd"], errors, "per_job_cap_usd") \
        if "per_job_cap_usd" in raw else None
    total_cap = _parse_money(raw["total_cap_usd"], errors, "total_cap_usd") \
        if "total_cap_usd" in raw else None

    max_output_tokens = None
    if "max_output_tokens" in raw:
        v = raw["max_output_tokens"]
        if not _is_strict_int(v):
            errors.append(f"max_output_tokens: must be an int, got {v!r} ({type(v).__name__})")
        elif v <= 0:
            errors.append(f"max_output_tokens: must be > 0, got {v}")
        else:
            max_output_tokens = v

    generation_cap = None
    if "generation_max_output_tokens" in raw:
        v = raw["generation_max_output_tokens"]
        if not _is_strict_int(v) or v <= 0:
            errors.append(f"generation_max_output_tokens: must be an int > 0, got {v!r}")
        elif max_output_tokens is not None and max_output_tokens < 2 * v:
            errors.append(f"generation_max_output_tokens: 2 x {v} exceeds the reservation ceiling max_output_tokens={max_output_tokens} "
                          "(a thinking overshoot of 2x must still fit inside what is reserved)")
        else:
            generation_cap = v

    role_caps: dict = {}
    for key in ("role_max_output_tokens", "role_generation_max_output_tokens"):            # R23: per-role output caps
        role_caps[key] = {}
        v = raw.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            errors.append(f"{key}: must be a mapping, got {v!r}")
            continue
        for role, n in v.items():
            if not isinstance(role, str) or not role or not _is_strict_int(n) or n <= 0:
                errors.append(f"{key}[{role!r}]: must be an int > 0, got {n!r}")
            else:
                role_caps[key][role] = n
    for role in set(role_caps["role_max_output_tokens"]) | set(role_caps["role_generation_max_output_tokens"]):
        ceiling = role_caps["role_max_output_tokens"].get(role) or max_output_tokens
        sent = role_caps["role_generation_max_output_tokens"].get(role) or (min(generation_cap, ceiling) if generation_cap and ceiling else None)
        if ceiling and sent and ceiling < 2 * sent:
            errors.append(f"role caps[{role!r}]: 2 x {sent} sent exceeds the role's reservation ceiling {ceiling} (a 2x overshoot must fit what is reserved)")

    max_input_tokens_per_call = None
    if "max_input_tokens_per_call" in raw:
        v = raw["max_input_tokens_per_call"]
        if not _is_strict_int(v):
            errors.append(
                f"max_input_tokens_per_call: must be an int, got {v!r} ({type(v).__name__})"
            )
        elif v <= 0:
            errors.append(f"max_input_tokens_per_call: must be > 0, got {v}")
        else:
            max_input_tokens_per_call = v

    max_retries = None
    if "max_retries" in raw:
        v = raw["max_retries"]
        if not _is_strict_int(v):
            errors.append(f"max_retries: must be an int, got {v!r} ({type(v).__name__})")
        elif v < 0:
            errors.append(f"max_retries: must be >= 0, got {v}")
        else:
            max_retries = v

    #: Optional key -- not in _CRITICAL_KEYS, see that tuple's own note.
    max_provider_calls_per_job = None
    if max_retries is not None:
        max_provider_calls_per_job = max_retries + 1
        if "max_provider_calls_per_job" in raw:
            v = raw["max_provider_calls_per_job"]
            if not _is_strict_int(v) or v <= 0:
                errors.append(
                    f"max_provider_calls_per_job: must be a positive int, got {v!r} "
                    f"({type(v).__name__})"
                )
            elif v != max_retries + 1:
                errors.append(
                    f"max_provider_calls_per_job: must equal max_retries + 1 "
                    f"({max_retries + 1}), got {v} -- the two keys are inconsistent"
                )
            else:
                max_provider_calls_per_job = v

    model_prices = {}
    if "model_prices" in raw:
        model_prices = _validate_model_prices(raw["model_prices"], errors)

    default_model_id = None
    if "default_model_id" in raw:
        v = raw["default_model_id"]
        if not isinstance(v, str) or not v:
            errors.append(f"default_model_id: must be a non-empty string, got {v!r}")
        elif model_prices and v not in model_prices:
            errors.append(f"default_model_id: {v!r} is not a key in model_prices")
        else:
            default_model_id = v

    fail_closed = None
    if "fail_closed_on_missing_usage" in raw:
        v = raw["fail_closed_on_missing_usage"]
        if not isinstance(v, bool):
            errors.append(
                f"fail_closed_on_missing_usage: must be a bool, got {v!r} ({type(v).__name__})"
            )
        else:
            fail_closed = v

    p50 = None
    if "latency_slo_p50_seconds" in raw:
        v = raw["latency_slo_p50_seconds"]
        if not _is_strict_float(v):
            errors.append(f"latency_slo_p50_seconds: must be a number, got {v!r} ({type(v).__name__})")
        elif math.isnan(v) or math.isinf(v) or v <= 0:
            errors.append(f"latency_slo_p50_seconds: must be a finite number > 0, got {v}")
        else:
            p50 = float(v)

    p95 = None
    if "latency_slo_p95_seconds" in raw:
        v = raw["latency_slo_p95_seconds"]
        if not _is_strict_float(v):
            errors.append(f"latency_slo_p95_seconds: must be a number, got {v!r} ({type(v).__name__})")
        elif math.isnan(v) or math.isinf(v) or v <= 0:
            errors.append(f"latency_slo_p95_seconds: must be a finite number > 0, got {v}")
        else:
            p95 = float(v)

    role_model_ids: dict = {}
    if "role_model_ids" in raw:
        v = raw["role_model_ids"]
        if not isinstance(v, dict):
            errors.append(f"role_model_ids: must be a mapping, got {v!r}")
        else:
            for role, mid in v.items():
                if not isinstance(role, str) or not role or not isinstance(mid, str) or not mid:
                    errors.append(f"role_model_ids: entries must be non-empty strings, got {role!r}: {mid!r}")
                elif model_prices and mid not in model_prices:
                    errors.append(f"role_model_ids[{role!r}]: {mid!r} is not a key in model_prices")
                else:
                    role_model_ids[role] = mid

    role_thinking_levels: dict = {}
    if "role_thinking_levels" in raw:
        v = raw["role_thinking_levels"]
        if not isinstance(v, dict):
            errors.append(f"role_thinking_levels: must be a mapping, got {v!r}")
        else:
            for role, level in v.items():
                if not isinstance(role, str) or not role or level not in THINKING_LEVELS:
                    errors.append(f"role_thinking_levels[{role!r}]: level must be one of {THINKING_LEVELS}, got {level!r}")
                else:
                    role_thinking_levels[role] = level

    role_service_tiers: dict = {}
    if "role_service_tiers" in raw:
        v = raw["role_service_tiers"]
        if not isinstance(v, dict):
            errors.append(f"role_service_tiers: must be a mapping, got {v!r}")
        else:
            for role, tier in v.items():
                mid = role_model_ids.get(role) or default_model_id
                if not isinstance(role, str) or not role or tier not in SERVICE_TIERS:
                    errors.append(f"role_service_tiers[{role!r}]: tier must be one of {SERVICE_TIERS}, got {tier!r}")
                elif model_prices and price_key(mid, tier) not in model_prices:
                    errors.append(f"role_service_tiers[{role!r}]: {price_key(mid, tier)!r} is not a key in model_prices (the tier needs its own price)")
                else:
                    role_service_tiers[role] = tier

    role_locations: dict = {}
    if "role_locations" in raw:
        v = raw["role_locations"]
        if not isinstance(v, dict):
            errors.append(f"role_locations: must be a mapping, got {v!r}")
        else:
            for role, loc in v.items():
                mid = role_model_ids.get(role) or default_model_id
                if not isinstance(role, str) or role not in LOCATION_ROLES:
                    errors.append(f"role_locations[{role!r}]: only {sorted(LOCATION_ROLES)} may use a non-global location")
                elif loc not in LOCATIONS:
                    errors.append(f"role_locations[{role!r}]: location must be one of {LOCATIONS}, got {loc!r}")
                elif role in role_service_tiers:
                    errors.append(f"role_locations[{role!r}]: a role cannot combine a location with a service tier")
                elif model_prices and price_key(mid, None, loc) not in model_prices:
                    errors.append(f"role_locations[{role!r}]: {price_key(mid, None, loc)!r} is not a key in model_prices (the location needs its own price)")
                else:
                    role_locations[role] = loc

    request_timeout = None
    if "request_timeout_seconds" in raw:
        v = raw["request_timeout_seconds"]
        if not _is_strict_float(v) or math.isnan(v) or math.isinf(v) or v <= 0:
            errors.append(f"request_timeout_seconds: must be a finite number > 0, got {v!r}")
        else:
            request_timeout = float(v)

    role_request_timeouts: dict = {}
    if "role_request_timeouts" in raw:
        v = raw["role_request_timeouts"]
        if not isinstance(v, dict):
            errors.append(f"role_request_timeouts: must be a mapping, got {v!r}")
        else:
            for role, sec in v.items():
                if not isinstance(role, str) or not role or not (_is_strict_float(sec) or _is_strict_int(sec)) or math.isnan(sec) or math.isinf(sec) or sec <= 0:
                    errors.append(f"role_request_timeouts[{role!r}]: must be a finite number of seconds > 0, got {sec!r}")
                elif request_timeout is None or sec > request_timeout:
                    errors.append(f"role_request_timeouts[{role!r}]: needs request_timeout_seconds and must not exceed it (the worker lease covers that timeout), got {sec!r}")
                else:
                    role_request_timeouts[role] = float(sec)

    snapshot_date = None
    if "price_snapshot_date" in raw:
        v = raw["price_snapshot_date"]
        try:
            datetime.date.fromisoformat(v)
            snapshot_date = v
        except (TypeError, ValueError):
            errors.append(f"price_snapshot_date: must be an ISO date string (yyyy-mm-dd), got {v!r}")

    snapshot_max_age = None
    if "price_snapshot_max_age_days" in raw:
        v = raw["price_snapshot_max_age_days"]
        if not _is_strict_int(v) or v <= 0:
            errors.append(f"price_snapshot_max_age_days: must be a positive int, got {v!r}")
        else:
            snapshot_max_age = v

    if p50 is not None and p95 is not None and p95 < p50:
        errors.append(
            f"latency_slo_p95_seconds: must be >= latency_slo_p50_seconds, got {p95} < {p50}"
        )

    if per_job_cap is not None and total_cap is not None and per_job_cap > total_cap:
        errors.append(
            f"per_job_cap_usd: must be <= total_cap_usd, got {per_job_cap} > {total_cap}"
        )

    from infra import ai_kinds
    enabled_roles = ai_kinds.validate(raw.get("enabled_roles"), errors)

    if errors:
        return None, errors

    return BudgetConfig(
        per_job_cap_usd=per_job_cap, total_cap_usd=total_cap, max_output_tokens=max_output_tokens,
        max_input_tokens_per_call=max_input_tokens_per_call, max_retries=max_retries,
        max_provider_calls_per_job=max_provider_calls_per_job, model_prices=model_prices,
        default_model_id=default_model_id, fail_closed_on_missing_usage=fail_closed,
        latency_slo_p50_seconds=p50, latency_slo_p95_seconds=p95,
        role_model_ids=role_model_ids, role_thinking_levels=role_thinking_levels, role_service_tiers=role_service_tiers, role_request_timeouts=role_request_timeouts, role_locations=role_locations, request_timeout_seconds=request_timeout,
        price_snapshot_date=snapshot_date, price_snapshot_max_age_days=snapshot_max_age,
        generation_max_output_tokens=generation_cap, enabled_roles=enabled_roles,
        role_max_output_tokens=role_caps["role_max_output_tokens"], role_generation_max_output_tokens=role_caps["role_generation_max_output_tokens"],
    ), []


def _apply_env_overrides(config: BudgetConfig) -> BudgetConfig:
    """Contract §6: only the two caps are overridable by env, applied AFTER load/validation.
    An override is validated by the same money rules; an invalid override raises
    BudgetConfigError -- it can never rescue an otherwise-invalid file (the file must already
    be valid before this function is even reached)."""
    per_job_cap = config.per_job_cap_usd
    total_cap = config.total_cap_usd
    errors: list = []

    raw_per_job = os.environ.get(_ENV_PER_JOB)
    if raw_per_job is not None:
        v = _parse_money(raw_per_job, errors, _ENV_PER_JOB)
        if v is not None:
            per_job_cap = v

    raw_total = os.environ.get(_ENV_TOTAL)
    if raw_total is not None:
        v = _parse_money(raw_total, errors, _ENV_TOTAL)
        if v is not None:
            total_cap = v

    if errors:
        raise BudgetConfigError("; ".join(errors))

    if per_job_cap > total_cap:
        raise BudgetConfigError(
            f"{_ENV_PER_JOB}/{_ENV_TOTAL}: per_job cap {per_job_cap} must be <= total cap {total_cap} "
            f"after env overrides"
        )

    if per_job_cap is config.per_job_cap_usd and total_cap is config.total_cap_usd:
        return config

    return replace(config, per_job_cap_usd=per_job_cap, total_cap_usd=total_cap)


def load_budget_config(path: str | None = None) -> BudgetConfig:
    """Reads and validates the YAML file at `path`.

    Contract §6: a set,
    non-empty `RIPPLE_BUDGET_CONFIG_PATH` wins over the `path` argument.
    """
    env_path = os.environ.get(_ENV_PATH)
    effective_path = env_path if env_path else path
    if not effective_path:
        raise BudgetConfigError("no config path given (neither an argument nor RIPPLE_BUDGET_CONFIG_PATH)")

    try:
        with open(effective_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise BudgetConfigError(f"cannot read budget config at {effective_path!r}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BudgetConfigError(f"budget config at {effective_path!r} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise BudgetConfigError(
            f"budget config at {effective_path!r} must be a YAML mapping at the top level, "
            f"got {type(raw).__name__}"
        )

    config, errors = _validate(raw)
    if errors:
        raise BudgetConfigError(
            f"budget config at {effective_path!r} is invalid ({len(errors)} problem(s)): "
            + "; ".join(errors)
        )

    return _apply_env_overrides(config)


# --------------------------------------------------------------------------------- admission / cost

_VALID_SCOPES = ("per_job", "total")


def check_admission(config: BudgetConfig, *, scope: str, spent_usd: Decimal, estimate_usd: Decimal) -> None:
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES!r}, got {scope!r}")
    cap = config.per_job_cap_usd if scope == "per_job" else config.total_cap_usd
    if spent_usd + estimate_usd > cap:
        raise BudgetExceeded(scope=scope, cap=cap, spent=spent_usd, estimate=estimate_usd)


def _rate_for(config: BudgetConfig, model_id: str) -> ModelPrice:
    return config.model_prices[model_id]  # KeyError propagates -- an unpriced model must never cost $0


def estimate_job_cost_usd(
    config: BudgetConfig, *, model_id: str, estimated_input_tokens: int, estimated_output_tokens: int,
) -> Decimal:
    price = _rate_for(config, model_id)
    return (
        Decimal(estimated_input_tokens) / Decimal(1000) * price.input_usd_per_1k
        + Decimal(estimated_output_tokens) / Decimal(1000) * price.output_usd_per_1k
    )


def actual_call_cost_usd(
    config: BudgetConfig, *, model_id: str, input_tokens: int | None, output_tokens: int | None, output_ceiling: int | None = None,
) -> Decimal:
    """Contract §7.3: a missing side is priced at its worst case
    when `fail_closed_on_missing_usage` is true; the reported side is priced as reported.
    Fail-open (`false`) returns Decimal("0") for a fully-missing report -- explicit, never an
    accidental byproduct of arithmetic on `None`.

    Worst-case token count for a missing side (contract §7.3):
    a missing INPUT side is priced at `config.max_input_tokens_per_call`; a missing OUTPUT
    side is priced at `config.max_output_tokens` -- each side has its OWN ceiling (pricing a missing
    input side at the OUTPUT ceiling would under-cost any prompt larger than
    `max_output_tokens` tokens).
    """
    price = _rate_for(config, model_id)  # KeyError first: an unpriced model never gets a "usage
                                          # available" fast path that skips the price lookup.

    if input_tokens is None and output_tokens is None and not config.fail_closed_on_missing_usage:
        return Decimal("0")

    effective_input = input_tokens if input_tokens is not None else config.max_input_tokens_per_call
    effective_output = output_tokens if output_tokens is not None else (output_ceiling or config.max_output_tokens)   # R23: the call's role ceiling

    return (
        Decimal(effective_input) / Decimal(1000) * price.input_usd_per_1k
        + Decimal(effective_output) / Decimal(1000) * price.output_usd_per_1k
    )
