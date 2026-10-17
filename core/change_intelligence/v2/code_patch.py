# -*- coding: utf-8 -*-
"""Candidate code patch from SYN-01 recommendations (extension contract `ext/1`, FEX-03): pure text work, no I/O, no model.

A SUPPORTED recommendation names a code file, the observed function text and the literals of the current value inside it. The patch replaces exactly those
literals, spelled as they were (`500_000`, `500,000`, `500000`), inside the observed text only, with the decided value. Anything else (UNCERTAIN, UNRESOLVED, a file the
checkout does not hold once, a path outside the allowlist or a protected file, observed text that is not in the file any more) is listed as NOT implemented with its
reason. Nothing is guessed, no other line changes.
"""
from __future__ import annotations

import difflib
import hashlib
import posixpath
import re
from typing import Iterable, Mapping, Optional

PROTECTED_DEFAULT = ("requirements", "pyproject.toml", "setup.py", "setup.cfg", "package.json", "package-lock.json", "Dockerfile", ".github/", ".gitlab-ci", "Makefile", ".env", "secrets", ".pem", ".key")
OPERATIONS = ("replace_literal",)


def normalize_path(path: str) -> Optional[str]:
    """A repository-relative POSIX path without traversal, or None when it is not acceptable."""
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or ":" in path:
        return None
    norm = posixpath.normpath(path)
    if norm.startswith("..") or norm == "." or "/../" in "/" + norm + "/":
        return None
    return norm


def path_allowed(path: str, allowlist: Iterable[str], protected: Iterable[str] = PROTECTED_DEFAULT) -> Optional[str]:
    """None when the path may be edited; else the refusal code."""
    norm = normalize_path(path)
    if norm is None:
        return "path_outside_allowlist"
    low = norm.lower()
    if any(p.lower() in low for p in protected):
        return "protected_file"
    if not any(norm == a.rstrip("/") or norm.startswith(a if a.endswith("/") else a + "/") for a in allowlist):
        return "path_outside_allowlist"
    return None


def resolve_file(rec_path: str, repo_files: Iterable[str]) -> tuple[Optional[str], str]:
    """The one repository file a recommendation path names (equal, or ending in '/<path>'); (None, reason) otherwise."""
    rec_norm = normalize_path(rec_path)
    if rec_norm is None:
        return None, "path_outside_allowlist"
    hits = [f for f in repo_files if f == rec_norm or f.endswith("/" + rec_norm)]
    if len(hits) == 1:
        return hits[0], ""
    return None, ("file_not_in_repository" if not hits else "path_ambiguous")


def render_like(literal: str, value) -> str:
    """The new value spelled like the old literal (underscores, commas, or plain digits; a decimal keeps its decimal form)."""
    s = str(value)
    if "_" in literal:
        return f"{int(float(s)):_}" if s.replace(".", "", 1).isdigit() else s
    if "," in literal:
        return f"{int(float(s)):,}" if s.replace(".", "", 1).isdigit() else s
    return s


def replace_literals(file_text: str, observed_text: str, literals: Iterable[Mapping], new_value) -> tuple[Optional[str], str, int]:
    """(new file text, reason, replacements). Replaces every listed literal inside the first occurrence of `observed_text` only."""
    if not observed_text or observed_text not in file_text:
        return None, "observed_text_not_found", 0
    start = file_text.index(observed_text)
    region = observed_text
    count = 0
    for lit in sorted({l["literal"] for l in literals}, key=len, reverse=True):
        pattern = r"(?<![\w.])" + re.escape(lit) + r"(?![\w.])"
        region, n = re.subn(pattern, lambda m: render_like(lit, new_value), region)
        count += n
    if count == 0:
        return None, "literal_not_found", 0
    return file_text[:start] + region + file_text[start + len(observed_text):], "", count


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile="a/" + path, tofile="b/" + path))


def patch_hash(diff_text: str) -> str:
    return hashlib.sha256(diff_text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _norm_number(literal: str):
    try:
        return int(float(str(literal).replace("_", "").replace(",", "")))
    except ValueError:
        return None


def literal_sites(repo_files: Iterable[str], read_file, *, before_value, allowlist: Iterable[str], protected: Iterable[str] = PROTECTED_DEFAULT, max_sites: int = 12) -> list:
    """Every line of an allowlisted Python file that carries a number literal equal to `before_value` (spelled any way): `[{path, line, literal}]`, bounded. Deterministic
    scan of the checkout; the repair step may only choose among these (it never names a new number)."""
    target = _norm_number(before_value)
    out = []
    for path in sorted(repo_files):
        if not path.endswith(".py") or path_allowed(path, allowlist, protected):
            continue
        try:
            text = read_file(path)
        except Exception:                                                              # noqa: BLE001 - unreadable file: not a site
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for lit in re.findall(r"(?<![\w.])\d[\d_,]*(?:\.\d+)?(?![\w.])", line):
                if _norm_number(lit) == target:
                    out.append({"path": path, "line": n, "literal": lit})
                    if len(out) >= max_sites:
                        return out
    return out


def plan(recommendations: Iterable[Mapping], operations: Mapping[str, Mapping], repo_files: Iterable[str], read_file, *, allowlist: Iterable[str],
         protected: Iterable[str] = PROTECTED_DEFAULT, extra_sites: Iterable[Mapping] = ()) -> dict:
    """Build the candidate: `{files[{path, before_hash, after_hash, replacements}], diff, patch_hash, implemented[{ref, rule_id, path}], not_implemented[{ref, reason}]}`.
    `read_file(path) -> str` reads the checkout; `operations` = op_id -> operation (for the decided value)."""
    repo_files = list(repo_files)
    implemented, skipped, edits = [], [], {}
    for rec in recommendations:
        ref = rec["ref"]
        if rec["status"] != "SUPPORTED":
            skipped.append({"ref": ref, "reason": "recommendation_" + rec["status"].lower(), "detail": rec.get("why")})
            continue
        op = operations.get(rec["op_id"]) or {}
        if op.get("kind") != "set_value" or not isinstance(op.get("desired"), Mapping):
            skipped.append({"ref": ref, "reason": "operation_not_a_literal_change"})
            continue
        path, why = resolve_file(rec["path"], repo_files)
        if path is None:
            skipped.append({"ref": ref, "reason": why, "detail": rec["path"]})
            continue
        refused = path_allowed(path, allowlist, protected)
        if refused:
            skipped.append({"ref": ref, "reason": refused, "detail": path})
            continue
        before = edits.get(path, (read_file(path),))[0] if path in edits else read_file(path)
        current = edits[path][1] if path in edits else before
        after, why, n = replace_literals(current, rec.get("observed_logic") or "", rec.get("literals") or [], op["desired"]["value"])
        if after is None:
            skipped.append({"ref": ref, "reason": why, "detail": path})
            continue
        edits[path] = (before, after, edits.get(path, (None, None, 0))[2] + n)
        implemented.append({"ref": ref, "rule_id": rec["rule_id"], "path": path, "replacements": n})
    desired_values = {o["desired"]["value"] for o in operations.values() if isinstance(o, Mapping) and o.get("kind") == "set_value" and isinstance(o.get("desired"), Mapping)}
    for site in extra_sites:                                                            # AIX-C1 repair: one more literal site per entry, chosen from the server's own scan, never a new number
        ref = f"repair:{site.get('path')}:{site.get('line')}"
        path = normalize_path(str(site.get("path") or ""))
        if path is None or path not in repo_files or path_allowed(path, allowlist, protected):
            skipped.append({"ref": ref, "reason": "site_not_allowed"})
            continue
        if len(desired_values) != 1:
            skipped.append({"ref": ref, "reason": "no_single_decided_value"})
            continue
        new_value = next(iter(desired_values))
        before = edits[path][0] if path in edits else read_file(path)
        current = edits[path][1] if path in edits else before
        lines = current.splitlines(True)
        idx = int(site.get("line") or 0) - 1
        lit = str(site.get("literal") or "")
        pattern = r"(?<![\w.])" + re.escape(lit) + r"(?![\w.])"
        if not lit or not 0 <= idx < len(lines) or not re.search(pattern, lines[idx]):
            skipped.append({"ref": ref, "reason": "site_literal_not_found"})
            continue
        lines[idx] = re.sub(pattern, lambda m: render_like(lit, new_value), lines[idx], count=1)
        after = "".join(lines)
        edits[path] = (before, after, (edits.get(path, (None, None, 0))[2] or 0) + 1)
        implemented.append({"ref": ref, "rule_id": None, "path": path, "replacements": 1})
    files, diff = [], ""
    for path in sorted(edits):
        before, after, n = edits[path]
        files.append({"path": path, "before_hash": hashlib.sha256(before.encode("utf-8")).hexdigest(), "after_hash": hashlib.sha256(after.encode("utf-8")).hexdigest(), "replacements": n})
        diff += unified_diff(path, before, after)
    return {"files": files, "diff": diff, "patch_hash": patch_hash(diff) if diff else None, "implemented": implemented, "not_implemented": skipped}
