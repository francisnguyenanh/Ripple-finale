"""SUR-04: semantic drift ADVISORY next to the exact MON-CI scalar check. Pure functions: no I/O, no provider.

A model reads a bounded diff and the accepted rules and proposes findings where the code may now behave differently from a rule although no mapped number changed (a comparator
turned around, an actor or exception removed, a condition or route changed). The server keeps a finding only if its rule was offered, its path is in the diff and its witness
lines are verbatim changed lines of that path; for a comparator claim it also checks that the comparators of the two witness lines differ (`operator_witness`). That proves
the lines exist and differ, not that the meaning changed: every finding stays ADVISORY. The exact scalar result is computed and stored apart and is never changed by this
layer; "no finding" is never a PASS and never compliance.
"""
from __future__ import annotations

import re
from typing import Mapping

SCHEMA = "ripple-semantic-drift/1"
MODEL_KEYS = frozenset({"status", "findings", "notes"})
STATUSES = ("findings", "none_found", "unsure")
KINDS = ("operator_change", "actor_change", "exception_removed", "condition_change", "route_change", "threshold_moved_elsewhere")
CONFIDENCE = ("low", "medium", "high")
FINDING_KEYS = frozenset({"kind", "rule_id", "path", "witness_removed", "witness_added", "statement", "confidence"})
LABEL = ("Semantic drift ADVISORY: a model's reading of the diff, with witness lines the server found in the diff. It is not a compliance result; the exact check above is "
         "unchanged; a person decides.")

_CMP = re.compile(r"(>=|<=|==|!=|>|<|\bgte\b|\blte\b|\bgt\b|\blt\b|\beq\b|\bne\b)")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])(\d{1,3}(?:[,_]\d{3})+|\d+)(?:\.(\d+))?")
# markup is refused (a tag `<b>`, `</x>`, `<!--`, backticks, pipes, markdown links, URLs); a comparison operator is not markup: an operator-change statement has to name it
# (2026-10-22, live diagnostic SUR04-V1: a finding naming ">=" and ">" was refused as not plain text)
_NOT_PLAIN = re.compile(r"<[A-Za-z/!]|[`|\\]|\*\*|^#|\]\(|https?://", re.M)


def index_diff(diff_text: str, *, max_lines_per_file: int = 400) -> dict:
    """Unified diff -> {path: {"added": [stripped lines], "removed": [stripped lines]}} (renames keep the new path, a deleted file its old path so a removed check stays
    witnessable; binary files have no lines)."""
    files: dict = {}
    cur = old = None
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            cur = old = None
            continue
        if raw.startswith("--- ") and cur is None:
            p = raw[4:].strip()
            old = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            cur = old if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            if cur is not None:
                files.setdefault(cur, {"added": [], "removed": []})
            continue
        if raw.startswith("@@") or cur is None:
            continue
        entry = files[cur]
        if raw.startswith("+") and len(entry["added"]) < max_lines_per_file:
            entry["added"].append(raw[1:].strip())
        elif raw.startswith("-") and len(entry["removed"]) < max_lines_per_file:
            entry["removed"].append(raw[1:].strip())
    return files


def numbers_in(text: str) -> set:
    out = set()
    for whole, frac in _NUMBER.findall(str(text or "")):
        n = re.sub(r"[,_]", "", whole)
        if frac and frac.strip("0"):
            n += "." + frac.rstrip("0")
        out.add(n)
    return out


def comparators(line: str) -> list:
    return [m.lower() for m in _CMP.findall(line or "")]


def rule_line(rule: Mapping) -> str:
    def cond(c):
        if not c:
            return "no condition"
        if "all" in c:
            return " and ".join(cond(x) for x in c["all"])
        if "any" in c:
            return " or ".join(cond(x) for x in c["any"])
        return f"{c.get('field')} {c.get('op')} {c.get('value')}{(' ' + c['unit']) if c.get('unit') else ''}"
    exc = "; ".join(e.get("text", "") for e in rule.get("exceptions") or [])
    return (f"id={rule['rule_id']}: {rule['title']} (when {cond(rule.get('condition'))}; actor {rule.get('actor_ref') or 'unknown'}; outcome {(rule.get('outcome') or {}).get('kind')}"
            + (f"; exceptions: {exc}" if exc else "") + ")")


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(MODEL_KEYS):
        raise ValueError(f"keys must be {sorted(MODEL_KEYS)}")
    if obj["status"] not in STATUSES or not isinstance(obj["findings"], list) or not isinstance(obj["notes"], list):
        raise ValueError("status / findings / notes invalid")
    if len(obj["findings"]) > int(limits["max_findings"]) * 3:
        raise ValueError("too many findings")
    for f in obj["findings"]:
        if not isinstance(f, dict) or set(f) != set(FINDING_KEYS) or not all(isinstance(f[k], str) for k in FINDING_KEYS):
            raise ValueError("finding keys invalid")
        if f["kind"] not in KINDS or f["confidence"] not in CONFIDENCE:
            raise ValueError("finding kind or confidence invalid")
    if not all(isinstance(n, str) for n in obj["notes"]):
        raise ValueError("notes must be strings")
    return obj


def check(model: Mapping, diff_index: Mapping, rules: Mapping, limits: Mapping) -> dict:
    """-> {kept, rejected, notes, conclusion}. A finding is refused whole with its reason, never edited."""
    kept, rejected, seen = [], [], set()
    max_chars = int(limits["max_statement_chars"])
    for i, f in enumerate(model["findings"]):
        statement = " ".join(f["statement"].split())
        w_rem, w_add = f["witness_removed"].strip(), f["witness_added"].strip()
        entry = diff_index.get(f["path"])
        reason = None
        if len(kept) >= int(limits["max_findings"]):
            reason = "over_finding_cap"
        elif not statement or len(statement) > max_chars or _NOT_PLAIN.search(statement):
            reason = "not_plain_text"
        elif f["rule_id"] not in rules:
            reason = "rule_not_offered"
        elif entry is None:
            reason = "path_not_in_diff"
        elif not w_rem and not w_add:
            reason = "no_witness"
        elif w_rem and w_rem not in entry["removed"]:
            reason = "witness_removed_not_in_diff"
        elif w_add and w_add not in entry["added"]:
            reason = "witness_added_not_in_diff"
        elif f["kind"] == "exception_removed" and not w_rem:
            reason = "exception_removed_needs_removed_line"
        else:
            allowed = numbers_in(w_rem) | numbers_in(w_add) | numbers_in(rule_line(rules[f["rule_id"]]))
            stray = sorted(numbers_in(statement) - allowed)
            if stray:
                reason = "figure_not_in_witness_or_rule:" + ",".join(stray[:3])
            elif (f["kind"], f["rule_id"], f["path"], w_rem, w_add) in seen:
                reason = "duplicate"
        if reason:
            rejected.append({"index": i, "kind": f["kind"], "rule_id": f["rule_id"], "path": f["path"], "reason": reason})
            continue
        seen.add((f["kind"], f["rule_id"], f["path"], w_rem, w_add))
        item = {"kind": f["kind"], "rule_id": f["rule_id"], "path": f["path"], "witness_removed": w_rem, "witness_added": w_add, "statement": statement,
                "confidence": f["confidence"], "status": "advisory", "server_checked": "witness_lines_exist", "origin": "model"}
        if f["kind"] == "operator_change":
            a, b = comparators(w_rem), comparators(w_add)
            item["operator_witness"] = "confirmed" if a and b and a != b else "not_shown"       # the lines carry different comparators; the meaning stays the model's reading
        kept.append(item)
    notes = [" ".join(n.split())[:max_chars] for n in model["notes"][: int(limits["max_notes"])] if n.strip() and not _NOT_PLAIN.search(n)]
    return {"kept": kept, "rejected": rejected, "notes": notes,
            "conclusion": "advisory_findings" if kept else "no_finding_kept", "never_a_pass": True}
