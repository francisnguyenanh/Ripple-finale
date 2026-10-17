"""Container runtime for the declared validation / verification / sandbox commands (extension FEX-04 / AIX-C1 sandbox contract, local build).

Selected per declared repository: `"runtime": {"kind": "container", "image": "python:3.12-slim", "memory": "512m", "cpus": "1", "pids": 128, "tmp_mb": 64}`; without it the
commands keep running as timed subprocesses (the advisory default). Each command runs in its own container: no network (`--network none`), read-only root filesystem, the
checkout mounted READ-ONLY at /work, a small tmpfs at /tmp, memory/CPU/PID limits, an unprivileged numeric user, all capabilities dropped, `no-new-privileges`, no host
environment (only a fixed allowlist), no Docker socket and no other mount except a read-only directory of vendored test tooling (pytest and its pure-Python dependencies, copied
from the worker's own installation: nothing is downloaded). A command that overruns its time is killed by container name. A missing engine or image is an infrastructure
failure, never a product failure.

What this is NOT: a verified isolation boundary for hostile code. A container shares the host kernel; the plan's card asks for a VM-class boundary (and its verification) before
arbitrary repositories run. That decision and review stay with the owner.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import uuid

DEFAULTS = {"image": "python:3.12-slim", "memory": "512m", "cpus": "1", "pids": 128, "tmp_mb": 64, "user": "65534:65534"}
VENDORED = ("pytest", "_pytest", "pluggy", "iniconfig", "packaging", "py")
ENV_ALLOW = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": "/deps", "HOME": "/tmp", "TMPDIR": "/tmp", "PYTEST_ADDOPTS": "-p no:cacheprovider"}
ISOLATION_CONTAINER = ("container per command (no network, read-only root and checkout, tmpfs /tmp, memory/CPU/PID limits, unprivileged user, capabilities dropped, "
                       "no-new-privileges, no host environment); shares the host kernel: not a verified boundary for hostile code")


def settings(repo: dict) -> dict | None:
    rt = repo.get("runtime") or {}
    if rt.get("kind") != "container":
        return None
    out = dict(DEFAULTS)
    out.update({k: rt[k] for k in DEFAULTS if k in rt})
    return out


def _deps_dir() -> str:
    """A read-only directory holding pytest and its pure-Python dependencies copied from this interpreter's installation (cached by content of the package versions)."""
    sources = []
    for name in VENDORED:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        path = pathlib.Path(mod.__file__)
        sources.append(path.parent if path.name == "__init__.py" else path)
    key = hashlib.sha256("|".join(f"{p}:{p.stat().st_mtime_ns}" for p in sources).encode("utf-8")).hexdigest()[:16]
    target = pathlib.Path(tempfile.gettempdir(), f"ripple-sandbox-deps-{key}")
    if not target.is_dir():
        staging = pathlib.Path(tempfile.mkdtemp(prefix="ripple-sandbox-deps-"))
        for p in sources:
            if p.is_dir():
                shutil.copytree(p, staging / p.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(p, staging / p.name)
        try:
            staging.rename(target)
        except OSError:                                                                 # another worker created it first
            shutil.rmtree(staging, ignore_errors=True)
    return str(target)


def available(image: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"container engine unavailable: {type(exc).__name__}"
    if r.returncode != 0:
        return False, f"image {image} not present locally (nothing is pulled)"
    return True, r.stdout.strip()


def run(cwd: str, commands: list, *, timeout_seconds: int, cfg: dict, max_output: int) -> dict:
    ok, info = available(cfg["image"])
    if not ok:
        return {"status": "INFRASTRUCTURE_FAILED", "commands": [{"argv": a, "exit": None, "seconds": 0, "output_tail": info} for a in commands], "runtime": {"kind": "container", "image": cfg["image"]}}
    deps = _deps_dir()
    rows, status = [], "PASSED"
    for argv in commands:
        argv = ["python" if a == "{python}" else a for a in argv]
        name = "ripple-sbx-" + uuid.uuid4().hex[:12]
        docker = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only", "--tmpfs", f"/tmp:rw,size={int(cfg['tmp_mb'])}m",
                  "--pids-limit", str(int(cfg["pids"])), "--memory", str(cfg["memory"]), "--memory-swap", str(cfg["memory"]), "--cpus", str(cfg["cpus"]),
                  "--user", str(cfg["user"]), "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                  "--mount", f"type=bind,source={os.path.abspath(cwd)},target=/work,readonly", "--mount", f"type=bind,source={deps},target=/deps,readonly", "-w", "/work"]
        for k, v in ENV_ALLOW.items():
            docker += ["-e", f"{k}={v}"]
        docker += [cfg["image"], *argv]
        t0 = time.monotonic()
        try:
            r = subprocess.run(docker, capture_output=True, text=True, timeout=timeout_seconds, encoding="utf-8", errors="replace", env={**os.environ, "MSYS_NO_PATHCONV": "1"})
            out = (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-max_output:]
            if r.returncode == 125:                                                     # the engine refused to start the container: infrastructure, not the product
                rows.append({"argv": argv, "exit": None, "seconds": round(time.monotonic() - t0, 2), "output_tail": "could not start: " + out[-500:]})
                status = "INFRASTRUCTURE_FAILED"
                continue
            rows.append({"argv": argv, "exit": r.returncode, "seconds": round(time.monotonic() - t0, 2), "output_tail": out})
            if r.returncode != 0 and status == "PASSED":
                status = "FAILED"
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True, text=True, timeout=60)
            rows.append({"argv": argv, "exit": None, "seconds": timeout_seconds, "output_tail": "timeout (container killed)"})
            status = "FAILED" if status == "PASSED" else status
        except OSError as exc:
            rows.append({"argv": argv, "exit": None, "seconds": 0, "output_tail": f"could not start: {exc}"})
            status = "INFRASTRUCTURE_FAILED"
    return {"status": status, "commands": rows, "runtime": {"kind": "container", "image": cfg["image"], "image_id": info, "limits": {k: cfg[k] for k in ("memory", "cpus", "pids", "tmp_mb", "user")}}}
