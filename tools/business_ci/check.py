"""Business CI drift check — ADVISORY command line (MON-CI local half). Reads a repository at two commits with `git show` (never runs its code), compares the YAML scalars
the accepted policy is bound to, and writes a versioned JSON report plus a one-line summary. Exit code 0 = aligned, 2 = drift, 3 = incomplete (unsupported/unmapped/moved
policy/wrong repository), 4 = usage error. A required-check integration on a real repository is NOT part of this tool.

  python -m tools.business_ci.check --repo <path> --repo-id <id> --base <sha> --head <sha> --policy policy.json --mapping mapping.json --out report.json
  python -m tools.business_ci.check verify --report report.json [--repo <path>]      # hash + stale-head check of a stored report

policy.json: {"snapshot_id", "business_revision", "rules": [...]} (the accepted rules as the product stores them; e.g. the `knowledge.json` of an audit package).
mapping.json: {"profile": "yaml-scalar", "repo_id", "policy_hash", "files": {"approval.yaml": "config/approval.yaml"}} — kept under protected review, never editable by the candidate change.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

import yaml

from core.change_intelligence.v2 import business_ci as ci


def _git(repo: str, *args) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {r.stderr.strip()[:200]}")
    return r.stdout


def _reader(repo: str, sha: str):
    cache: dict = {}

    def read(path: str, key: str):
        if path not in cache:
            try:
                cache[path] = yaml.safe_load(_git(repo, "show", f"{sha}:{path}"))
            except (RuntimeError, yaml.YAMLError):
                cache[path] = ci.UNREADABLE
        node = cache[path]
        if node is ci.UNREADABLE:
            return ci.UNREADABLE
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return ci.UNREADABLE
            node = node[part]
        return node if isinstance(node, (int, float, str)) and not isinstance(node, bool) else ci.UNREADABLE
    return read


def check(a) -> int:
    policy = json.loads(pathlib.Path(a.policy).read_text(encoding="utf-8"))
    mapping = json.loads(pathlib.Path(a.mapping).read_text(encoding="utf-8"))
    base_sha, head_sha = _git(a.repo, "rev-parse", a.base).strip(), _git(a.repo, "rev-parse", a.head).strip()
    changed = [l for l in _git(a.repo, "diff", "--name-only", base_sha, head_sha).splitlines() if l]
    report = ci.run(policy, mapping, repo_id=a.repo_id, base_sha=base_sha, head_sha=head_sha, read_base=_reader(a.repo, base_sha), read_head=_reader(a.repo, head_sha), changed_paths=changed)
    pathlib.Path(a.out).write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    s = report["summary"]
    print(f"business-ci ADVISORY {report['conclusion']}: aligned={s.get('aligned', 0)} no_impact={s.get('no_impact', 0)} drift={s.get('drift', 0)} unsupported={s.get('unsupported', 0)} "
          f"unmapped={len(report['coverage'].get('unmapped', []))} head={head_sha[:12]} policy={report['policy']['policy_hash'][:12]} report={report['report_hash'][:12]}"
          + (" incomplete: " + "; ".join(report["incomplete_reasons"]) if report["incomplete_reasons"] else ""))
    return {"aligned": 0, "drift": 2, "incomplete": 3}[report["conclusion"]]


def verify(a) -> int:
    report = json.loads(pathlib.Path(a.report).read_text(encoding="utf-8"))
    head = _git(a.repo, "rev-parse", "HEAD").strip() if a.repo else None
    problems = ci.verify(report, current_head_sha=head)
    print("business-ci report " + ("VALID" if not problems else "INVALID: " + "; ".join(problems)))
    return 0 if not problems else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    for name in ("--repo", "--repo-id", "--base", "--head", "--policy", "--mapping", "--out"):
        c.add_argument(name, required=True)
    c.set_defaults(fn=check)
    v = sub.add_parser("verify")
    v.add_argument("--report", required=True)
    v.add_argument("--repo", default="")
    v.set_defaults(fn=verify)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("check", "verify"):
        argv = ["check", *argv]
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 4
    try:
        return args.fn(args)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"business-ci error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
