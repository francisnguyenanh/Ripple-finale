"""Resource-limit configuration for the isolated simulation runner
(`infra/worker/sandbox_runner.py`).

All keys below are
read from the environment; every one is OPTIONAL and defaults to a
safe/locked-down value -- absence never means "unbounded".

Keys (all optional, all read as plain env vars, no separate config file):

  RIPPLE_SANDBOX_CPU_SECONDS    int, POSIX RLIMIT_CPU seconds for the
                                 sandboxed subprocess (POSIX only; see
                                 sandbox_runner's WINDOWS LIMITATION note for
                                 the practical floor on this platform).
                                 Default 5.
  RIPPLE_SANDBOX_WALL_SECONDS   int, wall-clock timeout in seconds, enforced
                                 on EVERY platform via the parent's own
                                 process-wait loop (never a POSIX-only
                                 mechanism -- this is the real cross-platform
                                 floor). Default 10.
  RIPPLE_SANDBOX_MEMORY_MB      int, POSIX RLIMIT_AS (address space) in MB.
                                 On Windows, enforced by a best-effort
                                 psutil-based poll-and-kill monitor thread
                                 (soft, sampled, not a hard OS ceiling); see
                                 sandbox_runner.py's WINDOWS LIMITATION note.
                                 Default 256.
  RIPPLE_SANDBOX_OUTPUT_BYTES   int, combined stdout+stderr byte cap,
                                 enforced on every platform by the runner
                                 reading the subprocess's pipes incrementally
                                 and killing it once the cap is exceeded.
                                 Default 65536 (64 KiB).
  RIPPLE_SANDBOX_ALLOW_NETWORK  "1"/"0". Default "0" -- every sandboxed
                                 subprocess has its `socket` module patched
                                 to deny connection attempts before the
                                 candidate code runs, unless this is
                                 explicitly "1" (never set in production; a
                                 test-only escape hatch, mirrored by every
                                 other AI-Closed-Loop env-gated override).

All ints are validated positive; an invalid (non-integer or <= 0) value
raises `SandboxConfigError` at load time rather than silently falling back
to a default that could be mistaken for the operator's real intent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class SandboxConfigError(ValueError):
    """A sandbox config env var is present but not a valid positive value."""


_DEFAULT_CPU_SECONDS = 5
_DEFAULT_WALL_SECONDS = 10
_DEFAULT_MEMORY_MB = 256
_DEFAULT_OUTPUT_BYTES = 65536


def _positive_int(env_key: str, default: int) -> int:
    raw = os.environ.get(env_key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SandboxConfigError(f"{env_key}={raw!r} is not an integer")
    if value <= 0:
        raise SandboxConfigError(f"{env_key}={raw!r} must be a positive integer")
    return value


@dataclass(frozen=True)
class SandboxConfig:
    cpu_seconds: int
    wall_seconds: int
    memory_mb: int
    output_bytes: int
    allow_network: bool


def load_sandbox_config() -> SandboxConfig:
    """Real config loader -- see module docstring for keys/units/defaults."""
    return SandboxConfig(
        cpu_seconds=_positive_int("RIPPLE_SANDBOX_CPU_SECONDS", _DEFAULT_CPU_SECONDS),
        wall_seconds=_positive_int("RIPPLE_SANDBOX_WALL_SECONDS", _DEFAULT_WALL_SECONDS),
        memory_mb=_positive_int("RIPPLE_SANDBOX_MEMORY_MB", _DEFAULT_MEMORY_MB),
        output_bytes=_positive_int("RIPPLE_SANDBOX_OUTPUT_BYTES", _DEFAULT_OUTPUT_BYTES),
        allow_network=os.environ.get("RIPPLE_SANDBOX_ALLOW_NETWORK", "0") == "1",
    )
