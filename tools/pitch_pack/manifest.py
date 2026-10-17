"""File manifest (sha256, size) for a candidate Pitch repo directory.

  python -m tools.pitch_pack.manifest <dir> [--out MANIFEST.json] [--source-commit SHA]

The manifest file itself is excluded from its own listing. Paths are POSIX, sorted.
Hashes are over raw bytes; an LF-normalised hash is added for text files to survive CRLF checkouts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build(root: Path, source_commit: str = "TBD", exclude: set[str] | None = None) -> dict:
    root = Path(root).resolve()
    exclude = exclude or set()
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        rel = p.relative_to(root).as_posix()
        if rel in exclude:
            continue
        data = p.read_bytes()
        entry = {"path": rel, "size": len(data), "sha256": _sha(data)}
        if b"\x00" not in data[:8192]:
            entry["sha256_lf"] = _sha(data.replace(b"\r\n", b"\n"))
        files.append(entry)
    overall = _sha("\n".join(f"{f['path']}:{f['sha256']}" for f in files).encode())
    return {"source_commit": source_commit, "file_count": len(files), "overall_sha256": overall,
            "image_digest": "TBD", "files": files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir")
    ap.add_argument("--out", default="MANIFEST.json")
    ap.add_argument("--source-commit", default="TBD")
    a = ap.parse_args(argv)
    root = Path(a.dir)
    if not root.is_dir():
        print(f"not a directory: {a.dir}", file=sys.stderr)
        return 2
    out = Path(a.out).resolve()
    exclude = {out.relative_to(root.resolve()).as_posix()} if root.resolve() in out.parents else set()
    m = build(root, a.source_commit, exclude)
    out.write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8")
    print(f"files={m['file_count']} overall_sha256={m['overall_sha256']} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
