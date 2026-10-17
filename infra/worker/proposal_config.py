"""The allowed-paths/actions/edit-kinds "support matrix" for the
contextual patch generator (job kind "proposal", `infra/worker/proposal.py`).

There is no other support-matrix config key in this repo. This follows the
EXACT SAME shape `infra/worker/whatif_config.py` already
established: every key is OPTIONAL, read from the environment, and any
configured value can only TIGHTEN the default -- never relax a protection
below its conservative default (same discipline `whatif_config.py`'s module
docstring documents; absence never widens what a patch may touch).

Keys (all optional):

  RIPPLE_PROPOSAL_ALLOWED_EDIT_KINDS
      comma-separated subset of the real supported edit kinds this executor
      implements: "python", "markdown", "yaml_typed_edit", "xlsx_typed_edit".
      Default: all four. An operator may narrow this (e.g. disable xlsx
      support in an environment with no openpyxl available); a value naming
      an edit kind this executor does not implement at all raises at load
      time rather than silently being accepted and then failing every job.

  RIPPLE_PROPOSAL_EXTRA_PROTECTED_PATH_SUBSTRINGS
      comma-separated EXTRA substrings (in addition to the hardcoded,
      non-overridable defaults below) that make a `target_artifact_id`
      ineligible as a patch target. This can only ADD protection, never
      remove the hardcoded defaults -- there is no env var that can widen
      what a patch may touch to include auth/audit/dependency/test-runner
      config; that would defeat the whole point of this gate.

The hardcoded, non-negotiable protected-path substrings (never patch
targets, regardless of any config): anything under `infra/adapters/`
carrying policy/identity/auth wiring, `.github/workflows/`, dependency
manifests (`requirements`, `pyproject.toml`, `package.json`,
`Pipfile`), and the test suite itself (`tests/`). These are checked as
case-sensitive substring matches against the caller-supplied
`target_artifact_id`/logical path -- a deliberately broad, conservative
match (a false positive here only means a legitimate patch has to go
through a human-reviewed exception path; a false
negative would let a patch tamper with the very machinery that verifies
patches, which is forbidden outright).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Real edit kinds this executor (`infra/worker/proposal.py`) implements.
_KNOWN_EDIT_KINDS = frozenset({"python", "markdown", "yaml_typed_edit", "xlsx_typed_edit"})

_DEFAULT_ALLOWED_EDIT_KINDS = tuple(sorted(_KNOWN_EDIT_KINDS))

#: Hardcoded, non-overridable -- see module docstring. Substring match
#: against the caller's `target_artifact_id` (a logical path/artifact key).
PROTECTED_PATH_SUBSTRINGS: tuple[str, ...] = (
    "infra/adapters/v2_policy",
    "infra/adapters/v2_identity",
    "infra/v2_policy_routes",
    ".github/workflows/",
    "requirements.txt",
    "requirements-",
    "pyproject.toml",
    "package.json",
    "Pipfile",
    "tests/",
)


class ProposalConfigError(ValueError):
    """A proposal support-matrix config env var is present but invalid."""


def _edit_kind_set(env_key: str, default: tuple) -> frozenset:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        values = default
    else:
        values = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = frozenset(values) - _KNOWN_EDIT_KINDS
    if unknown:
        raise ProposalConfigError(
            f"{env_key}={raw!r} names unimplemented edit kinds {sorted(unknown)}; "
            f"known edit kinds are {sorted(_KNOWN_EDIT_KINDS)}")
    return frozenset(values)


def _extra_protected_substrings(env_key: str) -> tuple[str, ...]:
    raw = os.environ.get(env_key)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class ProposalSupportMatrix:
    allowed_edit_kinds: frozenset
    protected_path_substrings: tuple[str, ...]

    def is_protected_path(self, target_artifact_id: str) -> bool:
        if not isinstance(target_artifact_id, str) or not target_artifact_id:
            return True  # fail closed: no target name at all is never a valid target
        return any(needle in target_artifact_id for needle in self.protected_path_substrings)


def load_proposal_support_matrix() -> ProposalSupportMatrix:
    """Real config loader -- see module docstring for keys/defaults."""
    allowed = _edit_kind_set("RIPPLE_PROPOSAL_ALLOWED_EDIT_KINDS", _DEFAULT_ALLOWED_EDIT_KINDS)
    extra = _extra_protected_substrings("RIPPLE_PROPOSAL_EXTRA_PROTECTED_PATH_SUBSTRINGS")
    return ProposalSupportMatrix(
        allowed_edit_kinds=allowed,
        protected_path_substrings=PROTECTED_PATH_SUBSTRINGS + extra)


__all__ = ["ProposalConfigError", "ProposalSupportMatrix", "load_proposal_support_matrix",
          "PROTECTED_PATH_SUBSTRINGS"]
