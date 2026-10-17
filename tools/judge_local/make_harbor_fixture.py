"""Build / verify the VETTED copy of the PUBLIC Harbor package used by the simulation adapter.

  python -m tools.judge_local.make_harbor_fixture build [--input <harbor_demo/input>]   # copy + write MANIFEST.json
  python -m tools.judge_local.make_harbor_fixture verify                                 # recompute hashes, exit 1 on drift

Only public input files are copied (never evaluator_private / authoring / reports); the seed in the test-data tree is not
modified. The manifest pins every file by sha256; `infra/worker/harbor_sim.py` refuses to run if any file differs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "fixtures" / "harbor_public"
INCLUDE = ("app/*.py", "config/approval.yaml", "data/requisitions.jsonl", "data/purchase_orders.jsonl",
           "data/receipts.jsonl", "data/invoices.jsonl", "data/suppliers.csv", "data/employees.csv",
           "run_output/routes.jsonl", "run_output/invoice_decisions.jsonl")


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _default_input() -> pathlib.Path:
    for base in (REPO.parent / "Ripple", REPO):
        p = base / "ripple" / "_testdata" / "generated" / "bundles" / "harbor_demo" / "input"
        if p.is_dir():
            return p
    raise SystemExit("public harbor_demo/input not found; pass --input")


def build(src: pathlib.Path) -> int:
    if src.name != "input" or src.parent.name != "harbor_demo":
        print("refusing: --input must be the public harbor_demo/input directory", file=sys.stderr)
        return 2
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    files = {}
    for pattern in INCLUDE:
        for p in sorted(src.glob(pattern)):
            if p.is_file() and "__pycache__" not in p.parts:
                rel = p.relative_to(src).as_posix()
                dest = FIXTURE / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(p.read_bytes())
                files[rel] = _sha(dest)
    manifest = {"fixture": "harbor_public", "origin": "ripple/_testdata/generated/bundles/harbor_demo/input (PUBLIC input only)",
                "note": "Pinned copy of the public Harbor package, config, transactions and shipped baseline outputs. Only this "
                        "hash-verified copy may be executed by the simulation adapter; never workspace-uploaded code.",
                "files": files}
    (FIXTURE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"copied {len(files)} files to {FIXTURE}")
    return 0


def verify() -> int:
    m = json.loads((FIXTURE / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = []
    for rel, digest in m["files"].items():
        p = FIXTURE / rel
        if not p.is_file() or _sha(p) != digest:
            bad.append(rel)
    extra = [p.relative_to(FIXTURE).as_posix() for p in FIXTURE.rglob("*")
             if p.is_file() and p.name not in ("MANIFEST.json", ".gitattributes") and "__pycache__" not in p.parts
             and p.relative_to(FIXTURE).as_posix() not in m["files"]]
    print("OK" if not bad and not extra else f"DRIFT: {bad} extra: {extra}")
    return 0 if not bad and not extra else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("build", "verify"))
    ap.add_argument("--input")
    a = ap.parse_args()
    raise SystemExit(build(pathlib.Path(a.input) if a.input else _default_input()) if a.cmd == "build" else verify())
