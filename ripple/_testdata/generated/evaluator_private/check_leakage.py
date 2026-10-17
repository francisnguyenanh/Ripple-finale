"""Scanner to ensure zero private IDs or oracle metadata leak into public input."""

import re
import sys
from pathlib import Path


LEAK_PATTERNS = [
    r"\bR0[1-9]\b",
    r"\bR1[0-2]\b",
    r"\bC0[1-2]\b",
    r"\bG0[1-2]\b",
    r"\bS[0-5]\b",
    r"\bevaluator_private\b",
    r"\bFROZEN_SPEC\b",
    r"\bauthoring/contracts\.json\b",
]


def scan_directory(input_dir: Path) -> list[dict[str, str]]:
    findings = []
    regexes = [re.compile(pat) for pat in LEAK_PATTERNS]

    for file_path in input_dir.rglob("*"):
        if not file_path.is_file():
            continue
        # Skip binary files (.xlsx)
        if file_path.suffix in (".xlsx", ".pyc", ".png", ".jpg"):
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for line_num, line in enumerate(content.splitlines(), start=1):
            for reg in regexes:
                m = reg.search(line)
                if m:
                    clean_snippet = line.strip()[:100].encode("ascii", "replace").decode("ascii")
                    findings.append({
                        "file": str(file_path.relative_to(input_dir)),
                        "line": line_num,
                        "match": m.group(0),
                        "snippet": clean_snippet,
                    })
    return findings


if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent
    target_bundle = base / "bundles" / "harbor_demo" / "input"
    findings = scan_directory(target_bundle)

    if findings:
        print(f"FAILED: Found {len(findings)} private ID leakage instances in public bundle:")
        for f in findings:
            print(f"  [{f['file']}:{f['line']}] Matched '{f['match']}': {f['snippet']}")
        sys.exit(1)
    else:
        print("SUCCESS: 0 private ID leaks found in public bundle. Clean separation verified!")
        sys.exit(0)
