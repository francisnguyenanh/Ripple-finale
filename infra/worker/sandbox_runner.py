# -*- coding: utf-8 -*-
"""A resource-limited subprocess runner for FIXED INTERNAL programs.

SECURITY STATEMENT. This module is NOT a security boundary and must never be handed
requester-, tenant- or model-derived code. It runs a separate child process (never an in-process exec) so that a
fixed, repo-authored probe/harness can be timed, size-capped and compared; the callers are `infra/worker/simulation.py`
(constant `_PROBE_SOURCES` / `_HD2_*` programs; user data only reaches them as JSON input files that are parsed, not
executed). `infra/worker/review.py` no longer calls it: a user-derived python proposal is never executed.

What is actually enforced, and how strong it is:

  * wall-clock limit   -- the parent waits with a real timeout and kills the child on expiry (all platforms).
  * CPU-time / memory  -- POSIX: `resource.setrlimit` (RLIMIT_CPU / RLIMIT_AS) in the forked child. Windows: no
                          `resource` module; the memory limit is a sampled, best-effort `psutil` RSS poll (not a hard
                          ceiling, and not enforced at all if `psutil` is missing). `SandboxResult.memory_limit_enforced`
                          reports which applied.
  * output-size cap    -- cooperative cap inside the child plus an external parent-side kill on overflow.
  * scratch directory  -- source and inputs are copied into a disposable per-run directory that is removed afterwards.
  * input immutability -- inputs are chmod'd read-only and their SHA-256 is compared before/after the run
                          (`input_hash_before == input_hash_after`). This detects modification; it does not stop a
                          same-user process from changing permissions.
  * best-effort Python-level guards inside the child -- `socket` is patched to raise and `open()`/`os.open` are
                          patched to deny writes under `inputs/` and access outside the scratch directory. These are
                          interpreter-level monkeypatches: code that is hostile (ctypes, os-level calls, importing a
                          fresh socket implementation, subprocesses, ...) can bypass them. They are advisory
                          tripwires for accidental misbehaviour of our own fixed programs, NOT network or filesystem
                          isolation. There is no seccomp/namespace/container confinement here.

Conclusion: resource-limited and advisory only. Executing untrusted code would require a real OS-level sandbox
(container/gVisor/nsjail) that this module does not provide.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

from infra.worker.sandbox_config import SandboxConfig

#: Terminal statuses this runner ever reports. Never a fabricated "EXECUTED"
#: for a run that was actually killed/denied -- one of these, always.
STATUS_EXECUTED = "EXECUTED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_OOM_KILLED = "OOM_KILLED"
STATUS_OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
STATUS_DENIED = "DENIED"
STATUS_CRASHED = "CRASHED"

_SANDBOX_ROOT_ENV = "RIPPLE_SANDBOX_SCRATCH_ROOT"


@dataclass(frozen=True)
class SandboxResult:
    status: str
    reason: str | None
    stdout_result: dict
    input_hashes_before: dict
    input_hashes_after: dict
    memory_limit_enforced: bool
    duration_seconds: float


def _sha256_files(paths: dict[str, str]) -> dict[str, str]:
    digests = {}
    for name, path in paths.items():
        try:
            with open(path, "rb") as fh:
                digests[name] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            digests[name] = None
    return digests


def _chmod_readonly(path: str) -> None:
    os.chmod(path, stat.S_IREAD)


def _chmod_writable_recursive(root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            try:
                os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
    try:
        os.chmod(root, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


_BOOTSTRAP_TEMPLATE = r"""
import io, json, os, sys

RESULT_PATH = {result_path!r}
INPUTS_DIR = os.path.realpath({inputs_dir!r})
SCRATCH_DIR = os.path.realpath({scratch_dir!r})
OUTPUT_CAP = {output_cap!r}
ALLOW_NETWORK = {allow_network!r}

_denial = {{"reason": None}}


def _deny(reason):
    if _denial["reason"] is None:
        _denial["reason"] = reason
    raise PermissionError(reason)


# --- network deny: patched in THIS child interpreter before candidate import.
if not ALLOW_NETWORK:
    import socket as _socket

    def _blocked(*a, **k):
        _deny("network_denied")

    _socket.socket = _blocked
    _socket.create_connection = _blocked
    _socket.getaddrinfo = _blocked


# --- filesystem deny: read-only inputs, no host access outside scratch dir.
_real_open = open


def _guarded_open(path, mode="r", *a, **k):
    real_path = os.path.realpath(str(path))
    writing = any(c in mode for c in "wax+")
    if not (real_path == SCRATCH_DIR or real_path.startswith(SCRATCH_DIR + os.sep)):
        _deny("host_filesystem_denied")
    if writing and (real_path == INPUTS_DIR or real_path.startswith(INPUTS_DIR + os.sep)):
        _deny("write_denied")
    return _real_open(path, mode, *a, **k)


import builtins
builtins.open = _guarded_open


# --- output cap: cooperative stop once combined stdout+stderr exceeds it.
class _CappedStream(io.TextIOBase):
    def __init__(self, real):
        self._real = real
        self._written = 0

    def write(self, s):
        n = len(s.encode("utf-8", "replace"))
        if self._written + n > OUTPUT_CAP:
            _deny("output_limit_exceeded")
        self._written += n
        return self._real.write(s)

    def flush(self):
        self._real.flush()


sys.stdout = _CappedStream(sys.stdout)
sys.stderr = _CappedStream(sys.stderr)

result = {{"status": "EXECUTED", "reason": None, "outcomes": []}}
try:
    candidate_globals = {{"__name__": "candidate", "INPUTS_DIR": INPUTS_DIR,
                         "SCRATCH_DIR": SCRATCH_DIR}}
    with _real_open(os.path.join(SCRATCH_DIR, "candidate.py"), "r") as fh:
        source = fh.read()
    exec(compile(source, "candidate.py", "exec"), candidate_globals)
    run_probe = candidate_globals.get("run_probe")
    handle = candidate_globals.get("handle")
    if callable(run_probe):
        run_probe()
    elif callable(handle):
        with _real_open(os.path.join(INPUTS_DIR, "corpus.json"), "r") as fh:
            corpus = json.load(fh)
        with _real_open(os.path.join(INPUTS_DIR, "config.json"), "r") as fh:
            config = json.load(fh)
        outcomes = []
        for case in corpus:
            outcomes.append({{"historical_case_id": case.get("historical_case_id"),
                             "outcome": str(handle(case, config))}})
        result["outcomes"] = outcomes
except PermissionError as exc:
    result["status"] = "DENIED"
    result["reason"] = _denial["reason"] or str(exc)
except Exception as exc:  # noqa: BLE001 -- sanitize, never leak a raw traceback
    result["status"] = "CRASHED"
    result["reason"] = type(exc).__name__

if _denial["reason"] and result["status"] == "EXECUTED":
    result["status"] = "DENIED"
    result["reason"] = _denial["reason"]

sys.stdout = sys.stdout._real if isinstance(sys.stdout, _CappedStream) else sys.stdout
with _real_open(RESULT_PATH, "w") as fh:
    json.dump(result, fh)
"""


def _preexec_fn(cfg: SandboxConfig):
    """POSIX-only rlimits, applied in the forked child before exec. Never
    referenced on Windows (no `resource` module there)."""
    import resource

    def _apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cfg.cpu_seconds, cfg.cpu_seconds))
        mem_bytes = cfg.memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass  # some POSIX platforms (notably macOS) reject RLIMIT_AS; CPU/wall/output still hold.

    return _apply


def run_isolated(*, candidate_source: str, input_files: dict[str, bytes],
                 config: SandboxConfig, extra_env: dict | None = None) -> SandboxResult:
    """Run `candidate_source` (a `run_probe()` or `handle(request, config)`-shaped module) in a resource-limited
    child process. `candidate_source` MUST be a fixed, repo-authored program: this is not a security boundary and
    never accepts user-derived code (see the module docstring for what is and is not enforced)."""
    scratch_root = os.environ.get(_SANDBOX_ROOT_ENV) or tempfile.gettempdir()
    scratch_dir = tempfile.mkdtemp(prefix="ripple-sandbox-", dir=scratch_root)
    inputs_dir = os.path.join(scratch_dir, "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    input_paths = {}
    try:
        for name, content in input_files.items():
            if os.path.isabs(name) or ".." in name.replace("\\", "/").split("/"):
                raise ValueError(f"unsafe input file name: {name!r}")
            path = os.path.join(inputs_dir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(content)
            input_paths[name] = path
        hashes_before = _sha256_files(input_paths)
        for path in input_paths.values():
            _chmod_readonly(path)
        _chmod_readonly(inputs_dir)

        candidate_path = os.path.join(scratch_dir, "candidate.py")
        with open(candidate_path, "w") as fh:
            fh.write(candidate_source)

        result_path = os.path.join(scratch_dir, "result.json")
        bootstrap_source = _BOOTSTRAP_TEMPLATE.format(
            result_path=result_path, inputs_dir=inputs_dir, scratch_dir=scratch_dir,
            output_cap=config.output_bytes, allow_network=config.allow_network)
        bootstrap_path = os.path.join(scratch_dir, "_bootstrap.py")
        with open(bootstrap_path, "w") as fh:
            fh.write(bootstrap_source)

        posix = os.name == "posix"
        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=scratch_dir, env={"PATH": os.environ.get("PATH", ""), **(extra_env or {})})
        if posix:
            popen_kwargs["preexec_fn"] = _preexec_fn(config)

        start = time.monotonic()
        proc = subprocess.Popen([sys.executable, bootstrap_path], **popen_kwargs)

        killed_reason = None
        memory_limit_enforced = posix

        if not posix:
            try:
                import psutil
                memory_limit_enforced = True

                def _monitor():
                    nonlocal killed_reason
                    try:
                        ps_proc = psutil.Process(proc.pid)
                    except psutil.Error:
                        return
                    limit_bytes = config.memory_mb * 1024 * 1024
                    while proc.poll() is None:
                        try:
                            if ps_proc.memory_info().rss > limit_bytes:
                                killed_reason = STATUS_OOM_KILLED
                                proc.kill()
                                return
                        except psutil.Error:
                            return
                        time.sleep(0.05)

                monitor_thread = threading.Thread(target=_monitor, daemon=True)
                monitor_thread.start()
            except ImportError:
                memory_limit_enforced = False  # documented floor: timeout + output cap only

        # External output-size cap: read pipes incrementally, kill if the
        # child never cooperates with its own in-process cap.
        captured = {"n": 0}

        def _watch_output(stream):
            nonlocal killed_reason
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    captured["n"] += len(chunk)
                    if captured["n"] > config.output_bytes * 4:  # hard external backstop
                        killed_reason = STATUS_OUTPUT_LIMIT_EXCEEDED
                        proc.kill()
                        return
            except (ValueError, OSError):
                return

        out_thread = threading.Thread(target=_watch_output, args=(proc.stdout,), daemon=True)
        err_thread = threading.Thread(target=_watch_output, args=(proc.stderr,), daemon=True)
        out_thread.start()
        err_thread.start()

        try:
            proc.wait(timeout=config.wall_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            timed_out = True

        out_thread.join(timeout=2)
        err_thread.join(timeout=2)
        duration = time.monotonic() - start

        hashes_after = _sha256_files(input_paths)

        if timed_out:
            return SandboxResult(
                status=STATUS_TIMEOUT, reason="wall_clock_timeout",
                stdout_result={}, input_hashes_before=hashes_before,
                input_hashes_after=hashes_after,
                memory_limit_enforced=memory_limit_enforced, duration_seconds=duration)
        if killed_reason == STATUS_OOM_KILLED:
            return SandboxResult(
                status=STATUS_OOM_KILLED, reason="memory_limit_exceeded",
                stdout_result={}, input_hashes_before=hashes_before,
                input_hashes_after=hashes_after,
                memory_limit_enforced=memory_limit_enforced, duration_seconds=duration)
        if killed_reason == STATUS_OUTPUT_LIMIT_EXCEEDED:
            return SandboxResult(
                status=STATUS_OUTPUT_LIMIT_EXCEEDED, reason="output_limit_exceeded_external",
                stdout_result={}, input_hashes_before=hashes_before,
                input_hashes_after=hashes_after,
                memory_limit_enforced=memory_limit_enforced, duration_seconds=duration)

        try:
            with open(result_path, "r") as fh:
                stdout_result = json.load(fh)
        except (OSError, json.JSONDecodeError):
            stdout_result = {}
            status = STATUS_CRASHED
            reason = "no_result_written"
        else:
            status = stdout_result.get("status", STATUS_CRASHED)
            reason = stdout_result.get("reason")
            if status not in (STATUS_EXECUTED, STATUS_DENIED, STATUS_CRASHED):
                status = STATUS_CRASHED

        return SandboxResult(
            status=status, reason=reason, stdout_result=stdout_result,
            input_hashes_before=hashes_before, input_hashes_after=hashes_after,
            memory_limit_enforced=memory_limit_enforced, duration_seconds=duration)
    finally:
        _chmod_writable_recursive(scratch_dir)
        shutil.rmtree(scratch_dir, ignore_errors=True)


__all__ = ["SandboxResult", "run_isolated", "SandboxConfig",
          "STATUS_EXECUTED", "STATUS_TIMEOUT", "STATUS_OOM_KILLED",
          "STATUS_OUTPUT_LIMIT_EXCEEDED", "STATUS_DENIED", "STATUS_CRASHED"]
