"""Clean-environment check: fresh copy/clone + fresh venv + install + offline tests.

  python -m tools.pitch_pack.clean_env_check <repo_dir_or_git_url> [--tests PATH ...] [--req requirements.txt] [--keep]

Steps: copy/clone into a temp dir, create a venv, pip install the requirements, run the offline tests
with provider/DB env vars REMOVED from the child environment, print step results.
Exit 0 only if every step passed. No secrets are read or printed.
Defaults to tests/pitch_d0 tests/pitch_eval (offline; DB tests skip without a DSN).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import venv
from pathlib import Path

STRIP_ENV_PREFIXES = ("RIPPLE_", "GOOGLE_", "GEMINI_", "OPENAI_", "ANTHROPIC_", "GCP_", "PG", "DATABASE_", "AWS_")


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_ENV_PREFIXES)}


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=cwd, env=env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source")
    ap.add_argument("--tests", nargs="+", default=["tests/pitch_d0", "tests/pitch_eval"])
    ap.add_argument("--req", default="requirements.txt")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="pitch_clean_"))
    results: list[tuple[str, bool]] = []
    try:
        work = tmp / "repo"
        src = Path(a.source)
        if src.is_dir():
            shutil.copytree(src, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", ".pytest_cache"))
        elif run(["git", "clone", "--quiet", a.source, str(work)], tmp, clean_env()) != 0:
            print("RESULT clone: FAIL")
            return 1
        results.append(("copy/clone", True))
        vdir = tmp / "venv"
        venv.EnvBuilder(with_pip=True).create(vdir)
        py = vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = clean_env()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        req = work / a.req
        ok = req.exists() and run([str(py), "-m", "pip", "install", "-q", "-r", str(req)], work, env) == 0
        results.append((f"pip install -r {a.req}" + ("" if req.exists() else " (file missing)"), ok))
        if ok:
            run([str(py), "-m", "pip", "install", "-q", "pytest"], work, env)
            rc = run([str(py), "-m", "pytest", "-q", *a.tests], work, env)
            results.append(("pytest " + " ".join(a.tests), rc == 0))
        for name, good in results:
            print(f"RESULT {name}: {'PASS' if good else 'FAIL'}")
        return 0 if all(g for _, g in results) else 1
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
