"""SUR-03 (proposal-only): a model explains the code change a confirmed business change needs and proposes exact edits; the server builds the diff and checks it statically.
Pure functions: no I/O, no provider, nothing executed.

The model proposes `find` / `replace` edits on files the server offered (recommendation paths resolved in the declared repository at a pinned base commit). The server keeps an
edit only if its file was offered, the path is inside the repository allowlist and not protected, it is not a test file (protected tests are never edited by a proposal), its
recommendation ref was offered and its `find` text occurs exactly once in the file at that point. It then builds the unified diff in memory and runs static checks on each
changed file (Python parses, JSON parses, YAML parses through the injected reader). The result is a PROPOSAL: `applied: false`, `executed: false`; it is never written to a
checkout, never run (no VM-class sandbox has been granted for model-written code) and never becomes a candidate patch by itself.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Callable, Iterable, Mapping, Optional

from core.change_intelligence.v2 import code_patch

SCHEMA = "ripple-code-proposal/1"
MODEL_KEYS = frozenset({"status", "explanation", "edits", "suggested_tests", "notes"})
EDIT_KEYS = frozenset({"path", "ref", "find", "replace", "rationale"})
STATUSES = ("proposed", "nothing_to_change", "unsure")
LABEL = ("Proposal only: a model's explanation and proposed edits, checked statically by the server. Nothing was applied to a repository and nothing was run; executing "
         "model-written code needs a VM-class sandbox that has not been granted. A person reviews it; the literal candidate path stays the only way to a patch.")
_TEST_FILE = re.compile(r"(^|/)(tests?|spec)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.spec\.[a-z]+$", re.I)
_CODE_LITERAL = re.compile(r"""(["'])([A-Za-z_][A-Za-z0-9_.\-]{2,80})\1""")       # a quoted code / key literal (a reason code, a config key, an enum value)


def removed_literals(find: str, replace: str, named: str) -> list:
    """Code literals an edit removes (present in the text it replaces, absent from its replacement) that the change and its recommendations never name: a rename or
    removal nobody asked for (SUR-03 v3: a proposal also renamed the reason code NONPOSITIVE_AMOUNT). Adding a new literal is not counted."""
    before = {m.group(2) for m in _CODE_LITERAL.finditer(find)}
    after = {m.group(2) for m in _CODE_LITERAL.finditer(replace)}
    return sorted(lit for lit in before - after if lit not in named)


_NOT_PLAIN = re.compile(r"</?[a-z][a-z0-9]*(?:\s[^<>]*)?/?>|\*\*|^#|\]\(|https?://", re.M | re.I)      # code talk keeps comparators and backticks; markup and links are refused


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE.search(path or ""))


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(MODEL_KEYS):
        raise ValueError(f"keys must be {sorted(MODEL_KEYS)}")
    if obj["status"] not in STATUSES or not isinstance(obj["explanation"], str):
        raise ValueError("status / explanation invalid")
    for key in ("edits", "suggested_tests", "notes"):
        if not isinstance(obj[key], list):
            raise ValueError(f"{key} must be a list")
    if len(obj["edits"]) > int(limits["max_edits"]) * 3:
        raise ValueError("too many edits")
    for e in obj["edits"]:
        if not isinstance(e, dict) or set(e) != set(EDIT_KEYS) or not all(isinstance(e[k], str) for k in EDIT_KEYS):
            raise ValueError("edit keys invalid")
    if not all(isinstance(x, str) for x in obj["suggested_tests"] + obj["notes"]):
        raise ValueError("suggested_tests / notes must be strings")
    return obj


def static_check(path: str, text: str, yaml_reader: Optional[Callable] = None) -> dict:
    low = path.lower()
    try:
        if low.endswith(".py"):
            ast.parse(text)
        elif low.endswith(".json"):
            json.loads(text)
        elif low.endswith((".yaml", ".yml")):
            if yaml_reader is None:
                return {"parses": None, "check": "yaml_reader_unavailable"}
            yaml_reader(text)
        else:
            return {"parses": None, "check": "no_parser_for_type"}
    except Exception as exc:                                                               # noqa: BLE001 - the parser's failure is the finding
        return {"parses": False, "check": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"parses": True, "check": "parsed"}


def check(model: Mapping, offered: Mapping[str, str], refs: Iterable[str], *, allowlist: Iterable[str], protected: Iterable[str], limits: Mapping,
          yaml_reader: Optional[Callable] = None, named: str = "") -> dict:
    """-> {files, diff, patch_hash, kept_edits, rejected, explanation, suggested_tests, notes, applied, executed}. An edit is refused whole with its reason, never edited."""
    refs = set(refs)
    texts = dict(offered)
    kept, rejected = [], []
    max_chars = int(limits["max_text_chars"])
    for i, e in enumerate(model["edits"]):
        path = code_patch.normalize_path(e["path"])
        reason = None
        if len(kept) >= int(limits["max_edits"]):
            reason = "over_edit_cap"
        elif path is None or path not in offered:
            reason = "file_not_offered"
        elif is_test_file(path):
            reason = "test_file_edit"
        elif code_patch.path_allowed(path, allowlist, protected):
            reason = code_patch.path_allowed(path, allowlist, protected)
        elif e["ref"] not in refs:
            reason = "ref_not_offered"
        elif not e["find"]:
            reason = "empty_find"
        elif e["find"] == e["replace"]:
            reason = "no_change"
        elif len(e["replace"]) > int(limits["max_replace_chars"]):
            reason = "replace_too_long"
        elif texts[path].count(e["find"]) != 1:
            reason = "find_not_unique" if texts[path].count(e["find"]) > 1 else "find_not_in_file"
        elif removed_literals(e["find"], e["replace"], named):
            reason = "unrequested_literal_change:" + ",".join(removed_literals(e["find"], e["replace"], named)[:3])
        if reason:
            rejected.append({"index": i, "path": e["path"], "ref": e["ref"], "reason": reason})
            continue
        texts[path] = texts[path].replace(e["find"], e["replace"], 1)
        kept.append({"path": path, "ref": e["ref"], "rationale": " ".join(e["rationale"].split())[:max_chars], "origin": "model"})
    files, parts = [], []
    for path in sorted({k["path"] for k in kept}):
        diff = code_patch.unified_diff(path, offered[path], texts[path])
        if not diff:
            continue
        parts.append(diff)
        files.append({"path": path, "diff": diff, **static_check(path, texts[path], yaml_reader)})
    diff = "".join(parts)
    clean = lambda xs, n: [" ".join(x.split())[:max_chars] for x in xs[:n] if x.strip() and not _NOT_PLAIN.search(x)]     # noqa: E731
    explanation = " ".join(model["explanation"].split())[: int(limits["max_explanation_chars"])]
    return {"files": files, "diff": diff, "patch_hash": code_patch.patch_hash(diff) if diff else None, "kept_edits": kept, "rejected": rejected,
            "explanation": explanation if not _NOT_PLAIN.search(explanation) else "", "suggested_tests": clean(model["suggested_tests"], int(limits["max_tests"])),
            "notes": clean(model["notes"], int(limits["max_notes"])), "static_checks_passed": bool(files) and all(f["parses"] is not False for f in files),
            "applied": False, "executed": False}
