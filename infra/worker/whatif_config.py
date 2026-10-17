"""Risk/blocker rule configuration for the what-if preview executor
(`infra/worker/whatif.py`).

There are no other risk/blocker rule config keys in this repo. Follows the
same shape as `infra/worker/sandbox_config.py`: every key is
OPTIONAL, read from the environment, and defaults to the SAFE/strict value
-- absence never relaxes a blocking rule, it only ever tightens or keeps the
default strict posture. This mirrors the contract's explicit instruction
that the executor must never self-mint a readiness/safety flag: the
deterministic rule set below is the only thing that can ever produce a
non-blocked readiness verdict, and its defaults are conservative.

Keys (all optional):

  RIPPLE_WHATIF_BLOCKING_SEVERITIES   comma-separated list of
                                       `core.change_intelligence` finding
                                       severities (subset of the schema's
                                       real enum, "HIGH"/"MEDIUM"/"LOW")
                                       that block readiness when found OPEN
                                       on the consumed impact report.
                                       Default "HIGH,MEDIUM" -- this repo's
                                       finding schema has no literal
                                       "CRITICAL" level (see
                                       `infra/worker/findings.py`'s
                                       `_severity_and_authority`); a
                                       "critical GAP" is
                                       therefore mapped onto this schema's
                                       real highest severities, not a
                                       fabricated new level.
  RIPPLE_WHATIF_BLOCK_ON_PENDING_MANUAL_ACTION
                                       "1"/"0". Default "1" -- an OPEN
                                       finding whose `authority_required` is
                                       set (a human decision is still
                                       pending)
                                       blocks readiness. Never "0" by
                                       default; an operator must explicitly
                                       opt out, and this file records that
                                       choice is auditable (env var, not a
                                       per-request payload field -- the
                                       payload's own `override_readiness`
                                       is NEVER honoured, see whatif.py).
  RIPPLE_WHATIF_BLOCK_ON_PARTIAL_COVERAGE
                                       "1"/"0". Default "1" -- any non-zero
                                       excluded/unknown/malformed/duplicate
                                       count in the consumed simulation
                                       report's denominator, or an
                                       incomplete impact-report coverage
                                       flag, blocks readiness rather than
                                       being silently rounded up to
                                       "complete".

All booleans are parsed strictly ("1" or "0" only); any other value raises
`WhatifConfigError` at load time rather than silently falling back to a
default that could be mistaken for the operator's real intent (same
discipline as `sandbox_config.py`'s `_positive_int`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: This repo's real finding-severity enum (see
#: `infra/worker/findings.py::_severity_and_authority`) -- no "CRITICAL"
#: level exists; HIGH is this schema's actual highest severity.
_KNOWN_SEVERITIES = frozenset({"HIGH", "MEDIUM", "LOW"})

_DEFAULT_BLOCKING_SEVERITIES = ("HIGH", "MEDIUM")


class WhatifConfigError(ValueError):
    """A whatif risk/blocker config env var is present but not a valid value."""


def _strict_bool(env_key: str, default: bool) -> bool:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    if raw == "1":
        return True
    if raw == "0":
        return False
    raise WhatifConfigError(f"{env_key}={raw!r} must be \"1\" or \"0\"")


def _severity_set(env_key: str, default: tuple) -> frozenset:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        values = default
    else:
        values = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    unknown = frozenset(values) - _KNOWN_SEVERITIES
    if unknown:
        raise WhatifConfigError(
            f"{env_key}={raw!r} names unknown severities {sorted(unknown)}; "
            f"known severities are {sorted(_KNOWN_SEVERITIES)}")
    return frozenset(values)


@dataclass(frozen=True)
class WhatifRiskConfig:
    blocking_severities: frozenset
    block_on_pending_manual_action: bool
    block_on_partial_coverage: bool


def load_whatif_risk_config() -> WhatifRiskConfig:
    """Real config loader -- see module docstring for keys/defaults."""
    return WhatifRiskConfig(
        blocking_severities=_severity_set(
            "RIPPLE_WHATIF_BLOCKING_SEVERITIES", _DEFAULT_BLOCKING_SEVERITIES),
        block_on_pending_manual_action=_strict_bool(
            "RIPPLE_WHATIF_BLOCK_ON_PENDING_MANUAL_ACTION", True),
        block_on_partial_coverage=_strict_bool(
            "RIPPLE_WHATIF_BLOCK_ON_PARTIAL_COVERAGE", True),
    )


__all__ = ["WhatifConfigError", "WhatifRiskConfig", "load_whatif_risk_config"]
