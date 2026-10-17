"""Publication scan for a candidate Pitch repo directory.

  python -m tools.pitch_pack.scan <dir> [--private-terms FILE] [--max-bytes N]

Finds secrets/tokens, emails, private paths, coordination/agent artifacts, private-split
labels, and large binaries. Prints findings with MASKED values only (never the secret).
Exit code 0 = clean, 1 = findings, 2 = usage error.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
import fnmatch
import json
from pathlib import Path

MAX_BYTES_DEFAULT = 1_000_000
TEXT_SCAN_LIMIT = 5_000_000

BAD_DIRS = {".git", ".process_runs", ".claude", "scratchpad", "scratchpads", "ripple_handover",
            "ripple_state", "ripple_dev_check_private", "__pycache__", ".pytest_cache", ".venv", "venv",
            ".idea", ".vscode"}
BAD_FILE_RE = re.compile(r"(^|[\\/])(\.env(\..*)?|.*\.(pem|key|p12|pfx|jks)|id_rsa.*|credentials.*\.json|"
                         r"HANDOVER.*|NEXT_SESSION.*|.*scratchpad.*|.*\.sqlite3?|.*\.db)$", re.I)
PRIVATE_PATH_FILE_RE = re.compile(r"(^|[\\/])(private|check_split|held_?out)[\\/]|"
                                  r"(gold|labels?|aliases)[^\\/]*\.(ya?ml|json|csv|jsonl)$", re.I)

SECRET_PATTERNS = [
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("dsn_with_password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]{3,}@[^\s]+", re.I)),
    ("secret_assignment", re.compile(
        r"(?i)\b[\w\-]*(?:secret|passw(?:or)?d|token|api[_\-]?key|private[_\-]?key|credential)[\w\-]*"
        r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-+/=.]{12,})")),
]
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
EMAIL_ALLOW = re.compile(r"@(example\.(com|org|net)|localhost|users\.noreply\.github\.com)$|^noreply@", re.I)
PRIVATE_PATH_RE = re.compile(
    r"(?i)([A-Z]:[\\/]+Users[\\/]+[^\s\"'\\/]+|/Users/[^\s\"'/]+|/home/[^\s\"'/]+|"
    r"ripple_handover|ripple_state|ripple_dev_check_private|\.process_runs)")
TOKEN_CAND_RE = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
PLACEHOLDER_RE = re.compile(r"(?i)(xxx|your[_\-]|<.*>|\$\{|example|placeholder|changeme|dummy|redacted|\*\*\*)")


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    masked: str
    raw: str = field(default="", compare=False, repr=False)       # the matched text; used only for allow-list matching, never printed

    def render(self) -> str:
        return f"{self.rule}\t{self.path}:{self.line}\t{self.masked}"


def mask(value: str) -> str:
    """Never reveal more than 2 leading chars and the length."""
    v = value.strip()
    return f"{v[:2]}***[len={len(v)}]"


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((s.count(c) / n) * math.log2(s.count(c) / n) for c in set(s))


def scan_text(rel: str, text: str, private_terms: list[str]) -> list[Finding]:
    out: list[Finding] = []
    for ln, line in enumerate(text.splitlines(), 1):
        for rule, rx in SECRET_PATTERNS:
            for m in rx.finditer(line):
                val = m.group(1) if rule == "secret_assignment" else m.group(0)
                if rule == "secret_assignment" and PLACEHOLDER_RE.search(val):
                    continue
                out.append(Finding(rule, rel, ln, mask(val), val))
        for m in EMAIL_RE.finditer(line):
            if not EMAIL_ALLOW.search(m.group(0)):
                out.append(Finding("email", rel, ln, mask(m.group(0)), m.group(0)))
        for m in PRIVATE_PATH_RE.finditer(line):
            out.append(Finding("private_path", rel, ln, mask(m.group(0)), m.group(0)))
        for m in TOKEN_CAND_RE.finditer(line):
            tok = m.group(0)
            if HEX_RE.match(tok) or PLACEHOLDER_RE.search(tok) or tok.count("/") > 2:
                continue  # hex digests are listed by the manifest; path-like strings are not tokens
            if entropy(tok) >= 4.2 and re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok):
                out.append(Finding("high_entropy_token", rel, ln, mask(tok), tok))
        for term in private_terms:
            if term and term in line:
                out.append(Finding("private_split_term", rel, ln, mask(term), term))
    return out


def load_private_terms(path: str | None) -> list[str]:
    if not path:
        return []
    return [t.strip() for t in Path(path).read_text(encoding="utf-8").splitlines()
            if t.strip() and not t.startswith("#")]


def scan_dir(root: Path, private_terms: list[str] | None = None, max_bytes: int = MAX_BYTES_DEFAULT) -> list[Finding]:
    root = Path(root).resolve()
    terms = private_terms or []
    findings: list[Finding] = []
    stack = [root]
    while stack:
        d = stack.pop()
        for p in sorted(d.iterdir()):
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                findings.append(Finding("symlink", rel, 0, "-"))
                continue
            if p.is_dir():
                if p.name in BAD_DIRS:
                    findings.append(Finding("forbidden_dir", rel, 0, p.name))
                    continue  # presence is the finding; do not descend
                stack.append(p)
                continue
            if BAD_FILE_RE.search(rel):
                findings.append(Finding("forbidden_file", rel, 0, p.name))
            if PRIVATE_PATH_FILE_RE.search(rel):
                findings.append(Finding("private_split_or_labels_path", rel, 0, p.name))
            size = p.stat().st_size
            if size > max_bytes:
                findings.append(Finding("large_file", rel, 0, f"{size} bytes"))
                continue
            data = p.read_bytes()[:TEXT_SCAN_LIMIT]
            if b"\x00" in data[:8192]:
                if size > 100_000:
                    findings.append(Finding("large_binary", rel, 0, f"{size} bytes"))
                continue
            findings.extend(scan_text(rel, data.decode("utf-8", "replace"), terms))
    return findings


def load_allowlist(path: str | None) -> list[dict]:
    """Narrow, reasoned exceptions for known false positives. Each entry: {"rule", "path_glob", optional "value_regex", "reason"}. An entry suppresses a finding only when ALL given parts match:
    the rule name, the file path (fnmatch) and, if present, the matched text (re.fullmatch). A rule or path that is not listed, or a value that does not fit the pattern, is still reported."""
    if not path:
        return []
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    for e in entries:
        if not (e.get("rule") and e.get("path_glob") and len((e.get("reason") or "").strip()) >= 15):
            raise SystemExit(f"allow-list entry needs rule, path_glob and a reason of at least 15 characters: {e}")
    return entries


def allowed(f: Finding, entries: list[dict]) -> str | None:
    for e in entries:
        if e["rule"] != f.rule or not fnmatch.fnmatchcase(f.path, e["path_glob"]):
            continue
        rx = e.get("value_regex")
        if rx is not None and not re.fullmatch(rx, f.raw or ""):
            continue
        return e["reason"]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir")
    ap.add_argument("--private-terms", help="file with one literal per line (private-split ids/labels/hashes)")
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT)
    ap.add_argument("--allowlist", help="JSON list of narrow, reasoned exceptions (see load_allowlist); reported separately from findings")
    a = ap.parse_args(argv)
    root = Path(a.dir)
    if not root.is_dir():
        print(f"not a directory: {a.dir}", file=sys.stderr)
        return 2
    allow = load_allowlist(a.allowlist)
    every = scan_dir(root, load_private_terms(a.private_terms), a.max_bytes)
    f, suppressed = [], {}
    for x in every:
        why = allowed(x, allow)
        if why:
            suppressed[(x.rule, why)] = suppressed.get((x.rule, why), 0) + 1
        else:
            f.append(x)
    for x in f:
        print(x.render())
    size_only = [x for x in f if x.rule in ("large_binary", "large_file")]
    print(f"findings={len(f)} (values masked); of which size warnings={len(size_only)}; allow-listed={sum(suppressed.values())}")
    for (rule, why), n in sorted(suppressed.items()):
        print(f"  allow-listed {n} x {rule}: {why}")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
