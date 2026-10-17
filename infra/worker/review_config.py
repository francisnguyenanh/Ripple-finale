"""Board/role/check configuration for the independent stage-
validation + review board (job kind "review", `infra/worker/review.py`).

Mirrors the EXACT SAME shape `infra/worker/proposal_config.py` and
`infra/worker/whatif_config.py` already established: every key is
OPTIONAL, read from the environment, and any configured value can only
TIGHTEN the conservative default -- never relax a required check/role below
it. "Tighten" here means: an operator may only ADD extra required checks/
roles on top of the hardcoded default set, never remove a default one. There
is no env var that can turn a required check or role optional, because that
is precisely the "reviewer timeout/unavailable must never be silently
approved" / "test failure must never be AI-overridden" guarantee -- an operator cannot configure that
guarantee away.

Keys (all optional):

  RIPPLE_REVIEW_EXTRA_REQUIRED_CHECKS
      comma-separated EXTRA check names (beyond the hardcoded default set
      below) this environment additionally requires before a "review" job
      can ever report `readiness="READY_FOR_HUMAN_REVIEW"`. A name outside
      the known check set raises at load time (fail loud, not silently
      ignored).

  RIPPLE_REVIEW_EXTRA_REQUIRED_ROLES
      comma-separated EXTRA role ids (beyond the hardcoded default
      BUSINESS_REVIEW/EVIDENCE_REVIEW/TECHNICAL_REVIEW) this environment
      additionally requires. A name outside the known role set raises at
      load time.

  RIPPLE_REVIEW_MAX_REPAIR_ROUNDS
      positive integer, default 3. This executor is STATELESS per job (like
      every sibling executor); it does not itself loop or retry a rejected
      proposal. This value is the project-level cap a caller/orchestrator
      driving the "fix proposal -> new version -> re-review" cycle
      (a bounded number of repair rounds) must respect -- recorded here so
      exactly one config source exists for that cap, not reinvented per
      caller. Not enforced inside a single job execution (there is nothing
      to loop over within one execute() call); a future caller-side
      orchestrator reads this value before issuing another "review"/
      "proposal" job pair for the same intent.

Board/model/prompt-version identity (BOARD_ID / ROLE_MODEL_IDS below):
this environment wires NO live external model provider for any job
kind (`ProposalExecutor`/`ImpactAnalysisExecutor`/`SimulationExecutor`/
`WhatifPreviewExecutor` are all `external = False`); `ReviewExecutor`
(`infra/worker/review.py`) follows the identical, real, non-fabricated
posture -- it makes ZERO live provider calls. All three review roles run as
DETERMINISTIC, real, versioned heuristic evaluators inside this single
executor process. Because all three roles are literally the SAME process
running the SAME deterministic code (not three independent model calls),
`model_correlation_disclosure` below states this plainly on every
`ReviewBundle` this executor produces -- this project does NOT claim
independent-model majority proof for a shared-model/shared-process board:
correlation is disclosed rather than called majority proof.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Real checks this executor (`infra/worker/review.py`) actually runs.
_KNOWN_CHECKS = frozenset({
    "apply_dry_run", "syntax_ast", "yaml_schema_roundtrip", "workbook_roundtrip",
    "diff_scope", "acceptance_tests", "simulation_exact_patch",
})

#: Hardcoded, non-overridable minimum required-check set -- see module
#: docstring. Every "review" job is evaluated against all of these; a check
#: not applicable to the candidate's own `edit_kind` is reported
#: `SKIPPED_NOT_APPLICABLE` (never silently omitted from the result), which
#: is NOT the same as passing a required-but-unavailable check (that is
#: `UNAVAILABLE`, and IS a blocker -- see `review.py`).
DEFAULT_REQUIRED_CHECKS: frozenset = frozenset(_KNOWN_CHECKS)

#: Real roles this executor actually evaluates.
_KNOWN_ROLES = frozenset({"BUSINESS_REVIEW", "EVIDENCE_REVIEW", "TECHNICAL_REVIEW"})

DEFAULT_REQUIRED_ROLES: frozenset = frozenset(_KNOWN_ROLES)

#: See module docstring -- deterministic, versioned, non-model board id.
BOARD_ID = "review.board.deterministic.v1"

#: Every role is the SAME deterministic process (see module docstring);
#: named per-role only so a future real provider integration has one place
#: to swap in a distinct model id per role without changing this executor's
#: shape.
ROLE_MODEL_IDS = {
    "BUSINESS_REVIEW": "deterministic-heuristic.v1",
    "EVIDENCE_REVIEW": "deterministic-heuristic.v1",
    "TECHNICAL_REVIEW": "deterministic-heuristic.v1",
}

_DEFAULT_MAX_REPAIR_ROUNDS = 3


class ReviewConfigError(ValueError):
    """A review board config env var is present but invalid."""


def _extra_name_set(env_key: str, known: frozenset, kind_label: str) -> frozenset:
    raw = os.environ.get(env_key)
    if not raw:
        return frozenset()
    values = frozenset(part.strip() for part in raw.split(",") if part.strip())
    unknown = values - known
    if unknown:
        raise ReviewConfigError(
            f"{env_key}={raw!r} names unknown {kind_label} {sorted(unknown)}; "
            f"known {kind_label} are {sorted(known)}")
    return values


def _max_repair_rounds() -> int:
    raw = os.environ.get("RIPPLE_REVIEW_MAX_REPAIR_ROUNDS")
    if not raw:
        return _DEFAULT_MAX_REPAIR_ROUNDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReviewConfigError(
            f"RIPPLE_REVIEW_MAX_REPAIR_ROUNDS={raw!r} is not an integer") from exc
    if value < 1:
        raise ReviewConfigError(
            f"RIPPLE_REVIEW_MAX_REPAIR_ROUNDS={raw!r} must be >= 1")
    return value


@dataclass(frozen=True)
class ReviewBoardConfig:
    required_checks: frozenset
    required_roles: frozenset
    max_repair_rounds: int
    board_id: str
    role_model_ids: dict


def load_review_board_config() -> ReviewBoardConfig:
    """Real config loader -- see module docstring for keys/defaults. The
    returned `required_checks`/`required_roles` are always a SUPERSET of the
    hardcoded defaults (tighten-only -- see module docstring)."""
    extra_checks = _extra_name_set(
        "RIPPLE_REVIEW_EXTRA_REQUIRED_CHECKS", _KNOWN_CHECKS, "checks")
    extra_roles = _extra_name_set(
        "RIPPLE_REVIEW_EXTRA_REQUIRED_ROLES", _KNOWN_ROLES, "roles")
    return ReviewBoardConfig(
        required_checks=DEFAULT_REQUIRED_CHECKS | extra_checks,
        required_roles=DEFAULT_REQUIRED_ROLES | extra_roles,
        max_repair_rounds=_max_repair_rounds(),
        board_id=BOARD_ID,
        role_model_ids=dict(ROLE_MODEL_IDS))


__all__ = [
    "ReviewConfigError", "ReviewBoardConfig", "load_review_board_config",
    "DEFAULT_REQUIRED_CHECKS", "DEFAULT_REQUIRED_ROLES", "BOARD_ID", "ROLE_MODEL_IDS",
]
