# -*- coding: utf-8 -*-
"""Code change recommendations for a business change (pure, no I/O, no model). SYN-01.

A recommendation tells a developer WHERE and WHAT to change so the code follows the decided rule; it is never an edit, a patch or something the product applies or runs.
Input: the draft of CHG-03 (`rule_changes` before/after, `file_actions` of kind code with the pinned code observation text) and the operations. Output per code location:
observed logic (the stored function text), desired logic (plain words and a pseudocode sketch derived from the operation), the literal occurrences the server could locate,
assumptions, dependent files, risk, suggested tests and acceptance checks, and a status: `SUPPORTED` (the server located what to change in the observed text), `UNCERTAIN`
(the location exists but the server cannot tie the change to a literal: deeper or dynamic behaviour, a person reads the code), `UNRESOLVED` (the knowledge names a code
location the pinned revision does not have). Everything is bound to the pin, the source revision and the semantic diff of the draft; a recommendation whose knowledge or source
moved is stale.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.business_index import condition_text
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "fkl-code-rec/1"
STATUSES = ("SUPPORTED", "UNCERTAIN", "UNRESOLVED")
STATEMENT = ("Recommendations for a developer. Nothing here is applied, executed or generated as a patch; the product never edits code. A SUPPORTED item names the literal the "
             "server found in the observed code; an UNCERTAIN item needs a person to read the logic; an UNRESOLVED item names a code location the pinned sources do not have.")
_NUM_LITERAL = re.compile(r"(?<![\w.])\d[\d_,]*(?:\.\d+)?(?![\w.])")
_UNSAFE_LINE = re.compile(r"(^|\s)(rm\s+-rf|curl\s|wget\s|sudo\s|chmod\s|powershell|bash\s+-c|os\.system|subprocess)", re.I)


def _num(text) -> Optional[float]:
    try:
        return float(str(text).replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _role_words(ref) -> str:
    return str(ref or "unknown").replace("role-", "").replace("-", " ")


def find_literals(text: str, value) -> list:
    """Line-numbered occurrences of a number in code text (1-based relative lines), matched by numeric value so 500_000, 500,000 and 500000 are the same literal."""
    want = _num(value)
    out = []
    if want is None or not isinstance(text, str):
        return out
    for n, line in enumerate(text.split("\n"), start=1):
        for m in _NUM_LITERAL.finditer(line):
            if _num(m.group(0)) == want:
                out.append({"line": n, "literal": m.group(0), "text": line.strip()[:200]})
    return out


def desired_logic(op: Mapping, rc: Mapping) -> dict:
    """What the code should do after the change, in words and as a pseudocode sketch. Derived from the operation and the after state only; never from a model."""
    k, d, title = op["kind"], op["desired"], rc["title"]
    after = rc["after"]
    if k == "set_value":
        before = op["before"]["value"] if isinstance(op.get("before"), Mapping) else op.get("before")
        val = d["value"] if isinstance(d, Mapping) else d
        return {"text": f"The threshold of “{title}” becomes {val}{(' ' + d['unit']) if isinstance(d, Mapping) and d.get('unit') else ''} (was {before}). Every comparison that implements this rule uses the new value; nothing else changes.",
                "pseudocode": f"# rule: {title}\nTHRESHOLD = {val}   # was {before}\nif amount {condition_text(after.get('condition')).split(' ', 4)[-1] if after.get('condition') else '...'}: apply the rule"}
    if k == "set_actor":
        return {"text": f"“{title}” is decided by {_role_words(d)} instead of {_role_words(op.get('before'))}. Routing, assignment or approval-role lookups for this rule return the new role.",
                "pseudocode": f"# rule: {title}\nAPPROVER = \"{_role_words(d)}\"   # was \"{_role_words(op.get('before'))}\"\nroute(request) -> needs_approval_by(APPROVER)"}
    if k == "add_or_change_condition":
        return {"text": f"“{title}” applies when {condition_text(d)}. The guard that decides whether the rule applies implements exactly this condition.",
                "pseudocode": f"# rule: {title}\nif {condition_text(d)}:\n    apply the rule"}
    if k == "set_exception":
        exc = "; ".join(e["text"] for e in d) if d else "none"
        return {"text": f"The exceptions of “{title}” are: {exc}. Each exception is checked before the rule applies; removed exceptions are no longer honoured.",
                "pseudocode": f"# rule: {title}\nfor exception in [{', '.join(repr(e['text']) for e in d)}]:\n    if exception holds: skip the rule\napply the rule"}
    scope = "; ".join(f"{a}: {', '.join(v) if isinstance(v, list) else v}" for a, v in sorted(d.items()))
    return {"text": f"“{title}” applies to {scope} only. The code paths outside this scope do not apply the rule.", "pseudocode": f"# rule: {title}\nif request in scope ({scope}):\n    apply the rule"}


def build_recommendations(draft: Mapping, operations: Iterable[Mapping], *, head: Mapping) -> dict:
    """One recommendation per code file action of the draft. `head` = the current {source_revision_id, snapshot_id} so staleness is stated, not hidden."""
    ops = {o["op_id"]: dict(o) for o in operations}
    changes = {c["rule_id"]: c for c in draft["rule_changes"]}
    items = []
    for fa in draft["file_actions"]:
        if fa["kind"] != "code" or not fa["op_ids"]:
            continue
        op = ops[fa["op_ids"][0]]
        rc = changes[op["rule_id"]]
        text = fa.get("stored_text") or ""
        lits = find_literals(text, op["before"]["value"]) if op["kind"] == "set_value" and isinstance(op.get("before"), Mapping) else []
        if fa["action"] == "UNRESOLVED":
            status, why = "UNRESOLVED", "the knowledge names this code location but the pinned source revision has no such file"
        elif fa["resolution"] in ("label", "heading") and (lits or op["kind"] != "set_value"):
            status, why = ("SUPPORTED", "the observed function text holds the literal to change") if lits else ("UNCERTAIN", "the change is not a literal replacement; a person reads the function and changes the logic")
        elif fa["resolution"] in ("label", "heading"):
            status, why = "UNCERTAIN", "the observed function text does not hold the current value as a literal: the value may be computed, imported or configured elsewhere"
        else:
            status, why = "UNCERTAIN", "the recorded location was not found in the pinned file; a person finds where this rule is implemented"
        if status == "SUPPORTED" and op["kind"] == "set_value" and isinstance(op.get("desired"), Mapping) and op["desired"].get("unit") and not (op.get("before") or {}).get("unit"):
            # the sources gave the current amount without a unit (live finding 2026-10-17): the literal may be in another unit; a person confirms before anything changes
            status, why = "UNCERTAIN", "the sources give the current value without a unit; a person confirms the unit of the literal before changing it"
        dependents = sorted({x["path"] for x in draft["file_actions"] if x["rule_ids"] == fa["rule_ids"] and x["ref"] != fa["ref"]})
        flags = [f["text"] for o in draft["operations"] if o["op_id"] == op["op_id"] for f in o["flags"]]
        risk = "HIGH" if flags or status != "SUPPORTED" else "MEDIUM"
        want = desired_logic(op, rc)
        tests = [f"A case just below and just at the new value behaves as “{rc['title']}” decides.", "A case that matched the old value no longer does (or still does, if the rule was widened).",
                 "The other rules in the same module are unchanged (regression on their existing tests)."]
        if op["kind"] == "set_exception":
            tests.append("One case per exception, and one case that matches none of them.")
        if op["kind"] == "set_actor":
            tests.append("The approver or route returned for a matching request is the new role.")
        checks = [f"After the change, re-import the file: the observation at {fa['path']}{(' ' + fa['locator']) if fa.get('locator') else ''} must read the new logic (SYN-02 read-back).",
                  "No other observed value of this file changed.", "The target rule’s implementation status moves from pending only through that read-back, never by hand."]
        rec = {"ref": fa["ref"], "path": fa["path"], "locator": fa.get("locator"), "rule_id": op["rule_id"], "title": rc["title"], "op_id": op["op_id"], "kind": op["kind"], "status": status,
               "why": why, "observed_logic": text[:2000] if text else None, "literals": lits[:20], "desired": want, "assumptions": [
                   "The observed function is the only place that implements this rule in this file; other files are listed as dependents.",
                   "The literal found (if any) is the value of this rule and not a different rule that happens to use the same number."] + (["The rule's unit and rounding are unchanged."] if op["kind"] == "set_value" else []),
               "dependent_files": dependents, "risk": risk, "flags": flags, "suggested_tests": tests, "acceptance_checks": checks, "manual_only": True,
               "language": "python" if fa["path"].lower().endswith(".py") else "unknown",
               "bound": {"target_rule_id": op["rule_id"] if op["rule_id"].endswith("-target") else op["rule_id"] + "-target", "business_revision": draft["pin"].get("business_revision"),
                         "source_revision_id": draft["pin"].get("source_revision_id")},
               "binding_status": "current" if (head.get("source_revision_id") == draft["pin"].get("source_revision_id") and head.get("snapshot_id") == draft["pin"].get("snapshot_id")) else "stale"}
        for line in (rec["desired"]["pseudocode"] or "").split("\n"):
            if _UNSAFE_LINE.search(line):
                raise ValueError("unsafe_pseudocode")
        items.append(rec)
    pin = draft["pin"]
    stale = []
    if head.get("source_revision_id") != pin.get("source_revision_id"):
        stale.append("the source files moved after this change was confirmed: the observed logic may be out of date")
    if head.get("snapshot_id") != pin.get("snapshot_id"):
        stale.append("the business knowledge moved after this change was confirmed")
    out = {"schema": SCHEMA, "statement": STATEMENT, "pin": dict(pin), "semantic_diff_hash": draft["semantic_diff_hash"], "draft_hash": draft["draft_hash"],
           "bound_source_revision_id": pin.get("source_revision_id"), "current_source_revision_id": head.get("source_revision_id"), "stale": bool(stale), "stale_reasons": stale,
           "items": items, "counts": {s: sum(1 for i in items if i["status"] == s) for s in STATUSES},
           "checklist": [f"{i['ref']} {i['path']}{(' ' + i['locator']) if i.get('locator') else ''}: {i['status']} — {i['desired']['text']}" for i in items],
           "apply": {"supported": False, "reason": "The product does not edit, patch, run or deploy code. A developer implements the recommendation; SYN-02 reads the changed file back."}}
    bound_only = {k: v for k, v in out.items() if k not in ("current_source_revision_id", "stale", "stale_reasons")}
    bound_only["items"] = [{k: v for k, v in i.items() if k != "binding_status"} for i in items]          # staleness is a label, not part of the binding
    out["recommendation_hash"] = canonical_hash(bound_only)
    return out


def render(rec: Mapping, *, can_read_evidence: bool) -> dict:
    """Code locations, observed text and pseudocode are evidence; a reader without `evidence.read` gets counts, titles and the desired logic in words."""
    if can_read_evidence:
        return dict(rec)
    return {"schema": rec["schema"], "statement": rec["statement"], "stale": rec["stale"], "stale_reasons": rec["stale_reasons"], "counts": rec["counts"], "apply": rec["apply"],
            "items": [{"title": i["title"], "kind": i["kind"], "status": i["status"], "risk": i["risk"], "desired": i["desired"]["text"]} for i in rec["items"]], "withheld": "code locations, observed text, pseudocode and tests need evidence rights"}
