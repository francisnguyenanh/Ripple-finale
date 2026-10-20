# -*- coding: utf-8 -*-
"""Multi-agent orchestration (MA-00), the Review Board join (MA-R1) and the Arbiter table (MA-K1): pure, no I/O, no model.

One coordinator decides the flow from a FIXED manifest; specialists return typed output over an authorized context they did not choose; the server validates every item
(categories, severities, references that exist in the authorized pack, plain text), deduplicates by claim, keeps minority and contested findings, runs at most one bounded
rebuttal round for critical claims a single role raised (review board) or one synthesis round whose input is the validated round-1 analyses (arbiter), and joins
deterministically. Agreement is not evidence and never approval: the report ends in `REVIEW_READY` (a person reads it) or `INCOMPLETE` (a required role did not complete,
the deadline passed, the budget ran out or the pin moved). No specialist may spawn work; only the coordinator dispatches children that exist in the manifest.
Specialists' prose is data, never an instruction.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-ma-run/1"
STATES = ("PLANNED", "RUNNING", "JOINING", "REVIEW_READY", "INCOMPLETE", "FAILED", "CANCELLED")
TERMINAL = frozenset({"REVIEW_READY", "INCOMPLETE", "FAILED", "CANCELLED"})
CATEGORIES = ("financial", "operational", "policy", "fraud", "legal", "other")
SEVERITIES = ("critical", "high", "medium", "low")
SPECIALIST_KEYS = {"status", "findings", "disputes", "notes"}
FINDING_KEYS = {"category", "severity", "statement", "evidence_refs", "assumptions", "counterexample"}
DISPUTE_KEYS = {"claim_id", "reason"}
STATUSES = ("OK", "NONE_FOUND", "UNSURE")
COMPARISONS = ("aligned", "divergent", "unverifiable")
MISMATCH_KEYS = {"status", "rows", "notes"}
REPAIR_KEYS = {"status", "sites", "rationale"}
ROW_KEYS = {"rule_id", "comparison", "policy_says", "code_shows", "policy_refs", "code_refs", "assumptions", "options", "actor_to_decide"}

MANIFESTS = {
    "review_board": {
        "version": 1,
        "roles": {"finance": {"required": True}, "operations": {"required": True}, "adversarial": {"required": True}, "compliance": {"required": False}},
        "rounds": 2, "rebuttal_max_per_role": 1, "synthesis_calls": 0, "max_concurrency": 2, "deadline_minutes": 30,
        "rebuttal_severities": ("critical", "high"),
    },
    # MA-K1: two independent analysts in round 1, one arbiter in round 2 that receives their validated analyses as data and produces a mismatch table
    "arbiter": {
        "version": 1,
        "roles": {"policy_analyst": {"required": True, "round": 1, "kind": "findings"}, "code_auditor": {"required": True, "round": 1, "kind": "findings"},
                  "arbiter": {"required": True, "round": 2, "kind": "drift", "inputs": "round1"}},
        "rounds": 2, "rebuttal_max_per_role": 0, "synthesis_calls": 1, "max_concurrency": 2, "deadline_minutes": 30,
        "rebuttal_severities": (), "requires_evidence_rights": True,
    },
    # MA-C1: the tester proposes TYPED cases before seeing the patch; a deterministic sandbox child renders and runs them next to the protected tests on base and patched clones
    "coder_tester": {
        "version": 1,
        "roles": {"tester": {"required": True, "round": 1, "kind": "tests"}, "sandbox": {"required": True, "round": 2, "kind": "sandbox", "inputs": "round1", "deterministic": True},
                  # AIX-C1 bounded repair: after a failing sandbox round the repairer may only ADD literal sites from the server's own scan (never a new number, never a test);
                  # each accepted proposal becomes a new candidate (new patch hash) and one more sandbox round; at most `repair_max` repairs, attempts never reset
                  "repairer": {"required": False, "round": 3, "kind": "repair", "repeat": True}},
        "rounds": 2, "rebuttal_max_per_role": 0, "synthesis_calls": 0, "max_concurrency": 1, "deadline_minutes": 30, "repair_max": 2,
        "rebuttal_severities": (), "requires_evidence_rights": True, "requires_candidate": True,
    },
    # MA-S1: a scenario designer and an assumption critic in round 1; a deterministic engine child runs baseline and validated scenarios
    "simulation": {
        "version": 1,
        "roles": {"scenario_designer": {"required": True, "round": 1, "kind": "scenarios"}, "assumption_critic": {"required": False, "round": 1, "kind": "findings"},
                  "engine": {"required": True, "round": 2, "kind": "simulation", "inputs": "round1", "deterministic": True, "job_kind": "ma.simulate"},
                  # LLM-S1: a result interpreter reads the engine's KPI table and states trade-offs; every number it states must be one of the metrics it cites
                  "result_interpreter": {"required": False, "round": 3, "kind": "interpretation", "inputs": "round2"}},
        "rounds": 2, "rebuttal_max_per_role": 0, "synthesis_calls": 0, "max_concurrency": 2, "deadline_minutes": 30,
        "rebuttal_severities": (),
    },
}
SCENARIOS_KEYS = {"status", "scenarios", "notes"}
CASE_KEYS = {"name", "module", "function", "args", "expect_op", "expect_value", "rationale"}
TESTS_KEYS = {"status", "cases", "notes"}
EXPECT_OPS = ("eq", "ne", "gt", "gte", "lt", "lte")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,6}$")
LIMITS = {"max_findings": 12, "max_statement_chars": 400, "max_refs": 8, "max_assumptions": 4, "max_disputes": 8, "max_reason_chars": 300, "max_notes": 4, "max_rows": 12, "max_options": 4}
_UNSAFE = re.compile(r"<[^>]+>|https?://|\[[^\]]*\]\([^)]*\)|`|\$\(|\brm\s+-rf\b", re.I)


def manifest(workflow: str) -> dict:
    if workflow not in MANIFESTS:
        raise ValueError(f"unknown_workflow:{workflow}")
    return MANIFESTS[workflow]


def manifest_hash(m: Mapping) -> str:
    return canonical_hash({k: (list(v) if isinstance(v, tuple) else v) for k, v in m.items()})


def role_round(m: Mapping, role: str) -> int:
    return int((m["roles"].get(role) or {}).get("round", 1))


def role_kind(m: Mapping, role: str) -> str:
    return str((m["roles"].get(role) or {}).get("kind", "findings"))


def synthesis_roles(m: Mapping) -> list:
    return sorted(r for r, spec in m["roles"].items() if int(spec.get("round", 1)) == 2 and spec.get("inputs"))


def is_deterministic(m: Mapping, role: str) -> bool:
    return bool((m["roles"].get(role) or {}).get("deterministic"))


def followup_roles(m: Mapping) -> list:
    """Round-3 model roles that read the validated round-2 results (LLM-S1 result interpreter)."""
    return sorted(r for r, spec in m["roles"].items() if int(spec.get("round", 1)) == 3 and spec.get("inputs") == "round2")


def max_children(m: Mapping) -> int:
    """The most PROVIDER calls a run can make (the root reservation): one per round-1 model role, at most `rebuttal_max_per_role` rebuttals per such role, plus one per
    model synthesis role; deterministic children (the sandbox) cost no provider call."""
    first = [r for r in m["roles"] if role_round(m, r) == 1 and not is_deterministic(m, r)]
    repairs = int(m.get("repair_max", 0)) * len([r for r in m["roles"] if role_kind(m, r) == "repair"])
    return (len(first) + len(first) * int(m.get("rebuttal_max_per_role", 0)) + len([r for r in synthesis_roles(m) if not is_deterministic(m, r)]) + repairs
            + len([r for r in followup_roles(m) if not is_deterministic(m, r)]))


def run_id_for(workspace_id: str, workflow: str, binding: Mapping) -> str:
    return "run-" + canonical_hash({"ws": workspace_id, "workflow": workflow, "binding": dict(binding)})[:20]


def child_id_for(run_id: str, role: str, round_no: int, attempt: int = 1) -> str:
    return "mac-" + canonical_hash({"run": run_id, "role": role, "round": round_no, "attempt": attempt})[:16]


def join_job_id(run_id: str, child_id: str) -> str:
    return "majoin-" + canonical_hash({"run": run_id, "after": child_id})[:16]


# ---- validation of one specialist answer --------------------------------------------------------------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def _plain(text, limit: int) -> Optional[str]:
    if not isinstance(text, str):
        return None
    t = " ".join(text.split())
    if not t or len(t) > limit or _UNSAFE.search(t):
        return None
    return t


def claim_id_for(category: str, statement: str, refs: Iterable[str]) -> str:
    return "clm-" + canonical_hash({"c": category, "s": _norm(statement), "r": sorted(set(refs))})[:12]


def validate_specialist_object(obj, allowed_refs: Iterable[str], known_claims: Iterable[str] = (), limits: Mapping = LIMITS) -> dict:
    """Shape and content of one specialist answer. -> {"status", "findings": kept, "rejected": [...], "disputes": kept, "rejected_disputes": [...], "notes": [...]}.
    A finding is kept only with a known category and severity, plain text, at least one reference that exists in the authorized pack and no reference outside it; a legal
    finding is kept but marked `legal_support: unsupported` and capped at severity medium (no versioned legal source is in the pack). Raises ValueError for a wrong shape."""
    if not isinstance(obj, Mapping) or set(obj) != SPECIALIST_KEYS:
        raise ValueError("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["findings"], list) or not isinstance(obj["disputes"], list) or not isinstance(obj["notes"], list):
        raise ValueError("status_or_lists")
    if len(obj["findings"]) > limits["max_findings"] or len(obj["disputes"]) > limits["max_disputes"] or len(obj["notes"]) > limits["max_notes"]:
        raise ValueError("too_many_items")
    allowed, known = set(allowed_refs), set(known_claims)
    kept, rejected = [], []
    for f in obj["findings"]:
        if not isinstance(f, Mapping) or set(f) != FINDING_KEYS:
            raise ValueError("finding_shape")
        statement = _plain(f["statement"], limits["max_statement_chars"])
        refs = [r for r in (f["evidence_refs"] or []) if isinstance(r, str)] if isinstance(f["evidence_refs"], list) else None
        if f["category"] not in CATEGORIES or f["severity"] not in SEVERITIES:
            rejected.append({"reason": "category_or_severity_unknown", "statement": str(f.get("statement"))[:80]})
        elif statement is None:
            rejected.append({"reason": "unsafe_or_empty_statement", "statement": str(f.get("statement"))[:80]})
        elif refs is None or not refs or len(refs) > limits["max_refs"]:
            rejected.append({"reason": "no_evidence", "statement": statement[:80]})
        elif any(r not in allowed for r in refs):
            rejected.append({"reason": "reference_not_authorized", "statement": statement[:80], "refs": sorted(set(refs) - allowed)[:4]})
        else:
            assumptions = [a for a in (f["assumptions"] or []) if isinstance(a, str) and _plain(a, 200)][: limits["max_assumptions"]] if isinstance(f["assumptions"], list) else []
            counter = _plain(f["counterexample"], 300) if isinstance(f["counterexample"], str) else None
            severity = f["severity"]
            item = {"claim_id": claim_id_for(f["category"], statement, refs), "category": f["category"], "severity": severity, "statement": statement,
                    "evidence_refs": sorted(set(refs)), "assumptions": assumptions, "counterexample": counter}
            if f["category"] == "legal":
                item["legal_support"] = "unsupported"
                if SEVERITIES.index(severity) < SEVERITIES.index("medium"):
                    item["severity"], item["severity_capped_from"] = "medium", severity
            kept.append(item)
    disputes, rejected_disputes = [], []
    for d in obj["disputes"]:
        if not isinstance(d, Mapping) or set(d) != DISPUTE_KEYS:
            raise ValueError("dispute_shape")
        reason = _plain(d["reason"], limits["max_reason_chars"])
        if not isinstance(d["claim_id"], str) or d["claim_id"] not in known:
            rejected_disputes.append({"reason": "unknown_claim", "claim_id": str(d.get("claim_id"))[:40]})
        elif reason is None:
            rejected_disputes.append({"reason": "unsafe_or_empty_reason", "claim_id": d["claim_id"]})
        else:
            disputes.append({"claim_id": d["claim_id"], "reason": reason})
    notes = [n for n in (_plain(x, 300) for x in obj["notes"] if isinstance(x, str)) if n]
    return {"status": obj["status"], "findings": kept, "rejected": rejected, "disputes": disputes, "rejected_disputes": rejected_disputes, "notes": notes}


def validate_mismatch_object(obj, allowed_refs: Iterable[str], rule_ids: Iterable[str], limits: Mapping = LIMITS) -> dict:
    """The arbiter's table. A row needs a rule of the pack and plain text; `aligned`/`divergent` need evidence on BOTH sides (policy and code refs inside the pack) or the server
    downgrades the row to `unverifiable` (`downgraded_from`); refs outside the pack are dropped from the row and noted. Raises ValueError for a wrong shape."""
    if not isinstance(obj, Mapping) or set(obj) != MISMATCH_KEYS:
        raise ValueError("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["rows"], list) or not isinstance(obj["notes"], list):
        raise ValueError("status_or_lists")
    if len(obj["rows"]) > limits.get("max_rows", 12) or len(obj["notes"]) > limits["max_notes"]:
        raise ValueError("too_many_items")
    allowed, rules = set(allowed_refs), set(rule_ids)
    rows, rejected = [], []
    for r in obj["rows"]:
        if not isinstance(r, Mapping) or set(r) != ROW_KEYS:
            raise ValueError("row_shape")
        if r["rule_id"] not in rules:
            rejected.append({"reason": "rule_not_in_pack", "rule_id": str(r.get("rule_id"))[:60]})
            continue
        if r["comparison"] not in COMPARISONS:
            rejected.append({"reason": "comparison_unknown", "rule_id": r["rule_id"]})
            continue
        ps, cs, actor = _plain(r["policy_says"], limits["max_statement_chars"]), _plain(r["code_shows"], limits["max_statement_chars"]), _plain(r["actor_to_decide"], 120)
        if ps is None or cs is None or actor is None:
            rejected.append({"reason": "unsafe_or_empty_text", "rule_id": r["rule_id"]})
            continue
        prefs = sorted({x for x in (r["policy_refs"] or []) if isinstance(x, str) and x in allowed}) if isinstance(r["policy_refs"], list) else []
        crefs = sorted({x for x in (r["code_refs"] or []) if isinstance(x, str) and x in allowed}) if isinstance(r["code_refs"], list) else []
        dropped = sorted({x for x in list(r["policy_refs"] or []) + list(r["code_refs"] or []) if isinstance(x, str) and x not in allowed})
        row = {"rule_id": r["rule_id"], "comparison": r["comparison"], "policy_says": ps, "code_shows": cs, "policy_refs": prefs, "code_refs": crefs,
               "assumptions": [a for a in (r["assumptions"] or []) if isinstance(a, str) and _plain(a, 200)][: limits["max_assumptions"]] if isinstance(r["assumptions"], list) else [],
               "options": [o for o in (r["options"] or []) if isinstance(o, str) and _plain(o, 200)][: limits.get("max_options", 4)] if isinstance(r["options"], list) else [],
               "actor_to_decide": actor, "refs_dropped": dropped}
        if row["comparison"] in ("aligned", "divergent") and not (prefs and crefs):
            row["downgraded_from"], row["comparison"] = row["comparison"], "unverifiable"          # a verdict without evidence on both sides is not a verdict
        rows.append(row)
    notes = [n for n in (_plain(x, 300) for x in obj["notes"] if isinstance(x, str)) if n]
    return {"status": obj["status"], "rows": sorted(rows, key=lambda x: x["rule_id"]), "rejected": rejected, "notes": notes, "findings": [], "disputes": [], "rejected_disputes": []}


def validate_tests_object(obj, allowed_modules: Iterable[str], allowed_functions: Iterable[str], limits: Mapping = LIMITS) -> dict:
    """The tester's typed cases. A case is kept only with identifier-shaped name/function, a module shown in the code evidence (allowlisted), a function shown there, at most
    `max_args` JSON scalars, a known operator and a scalar expectation; everything else is rejected with a reason. Raises ValueError for a wrong shape."""
    if not isinstance(obj, Mapping) or set(obj) != TESTS_KEYS:
        raise ValueError("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["cases"], list) or not isinstance(obj["notes"], list):
        raise ValueError("status_or_lists")
    if len(obj["cases"]) > limits.get("max_cases", 12) or len(obj["notes"]) > limits["max_notes"]:
        raise ValueError("too_many_items")
    modules, functions = set(allowed_modules), set(allowed_functions)
    kept, rejected, seen = [], [], set()
    for c in obj["cases"]:
        if not isinstance(c, Mapping) or set(c) != CASE_KEYS:
            raise ValueError("case_shape")
        name = c["name"] if isinstance(c["name"], str) else ""
        if not _IDENT_RE.match(name) or not name.islower() and name != name.lower():
            rejected.append({"reason": "name_not_identifier", "name": str(c.get("name"))[:60]})
            continue
        if not isinstance(c["module"], str) or not _MODULE_RE.match(c["module"]) or c["module"] not in modules:
            rejected.append({"reason": "module_not_in_code_evidence", "name": name, "module": str(c.get("module"))[:80]})
            continue
        if not isinstance(c["function"], str) or not _IDENT_RE.match(c["function"]) or c["function"] not in functions:
            rejected.append({"reason": "function_not_in_code_evidence", "name": name, "function": str(c.get("function"))[:60]})
            continue
        args = c["args"]
        if not isinstance(args, list) or len(args) > limits.get("max_args", 8) or not all(a is None or isinstance(a, (int, float, str, bool)) for a in args):
            rejected.append({"reason": "args_not_scalars", "name": name})
            continue
        if c["expect_op"] not in EXPECT_OPS or not (c["expect_value"] is None or isinstance(c["expect_value"], (int, float, str, bool))):
            rejected.append({"reason": "expectation_invalid", "name": name})
            continue
        if any(isinstance(a, str) and len(a) > 200 for a in args) or (isinstance(c["expect_value"], str) and len(c["expect_value"]) > 200):
            rejected.append({"reason": "scalar_too_long", "name": name})
            continue
        key = (name, c["module"], c["function"], json_dumps_safe(args), c["expect_op"], json_dumps_safe(c["expect_value"]))
        if key in seen:
            rejected.append({"reason": "duplicate_case", "name": name})
            continue
        seen.add(key)
        kept.append({"name": name, "module": c["module"], "function": c["function"], "args": list(args), "expect_op": c["expect_op"], "expect_value": c["expect_value"],
                     "rationale": _plain(c["rationale"], 300) if isinstance(c["rationale"], str) else None})
    notes = [n for n in (_plain(x, 300) for x in obj["notes"] if isinstance(x, str)) if n]
    return {"status": obj["status"], "cases": kept, "rejected": rejected, "notes": notes, "findings": [], "disputes": [], "rejected_disputes": []}


def validate_repair_object(obj, offered_sites: Iterable[Mapping], limits: Mapping = LIMITS) -> dict:
    """The repairer's answer: `status` OK | NONE, `sites` chosen ONLY among the offered `{path, line, literal}` (exact match; the server scanned them from the base checkout for the
    decided rule's current value), `rationale` plain. Unknown or repeated sites are rejected with a reason; a wrong shape raises."""
    if not isinstance(obj, Mapping) or set(obj) != REPAIR_KEYS:
        raise ValueError("keys")
    if obj["status"] not in ("OK", "NONE") or not isinstance(obj["sites"], list) or not isinstance(obj["rationale"], str):
        raise ValueError("status_or_lists")
    if len(obj["sites"]) > limits.get("max_repair_sites", 4):
        raise ValueError("too_many_sites")
    offered = {(s["path"], int(s["line"]), s["literal"]) for s in offered_sites}
    kept, rejected, seen = [], [], set()
    for s in obj["sites"]:
        if not isinstance(s, Mapping) or set(s) != {"path", "line", "literal"} or not isinstance(s["path"], str) or isinstance(s["line"], bool) or not isinstance(s["line"], int) or not isinstance(s["literal"], str):
            raise ValueError("site_shape")
        key = (s["path"], s["line"], s["literal"])
        if key not in offered:
            rejected.append({"reason": "site_not_offered", "path": s["path"][:120], "line": s["line"]})
            continue
        if key in seen:
            rejected.append({"reason": "duplicate_site", "path": s["path"][:120], "line": s["line"]})
            continue
        seen.add(key)
        kept.append({"path": s["path"], "line": s["line"], "literal": s["literal"]})
    return {"status": obj["status"] if kept else "NONE", "sites": kept, "rejected": rejected, "rationale": _plain(obj["rationale"], 300), "notes": [], "findings": [], "disputes": [], "rejected_disputes": []}


INTERPRETATION_KEYS = {"status", "statements", "notes"}
STATEMENT_KINDS = ("trade_off", "risk", "conditional_recommendation", "limitation")
_CERTAINTY = re.compile(r"\b(guarantee[sd]?|certainly|definitely|proves?|forecasts?|will (?:save|reduce|increase|cut|eliminate))\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def metric_table(comparison: Mapping) -> dict:
    """`s<n>.<kpi>.<baseline|scenario|delta>` -> value (n = 1-based row of the engine's comparison): the only numbers an interpretation may state.
    The scenario labels are listed apart (`scenario_labels`), so a metric id never carries free text."""
    out = {}
    for n, row in enumerate((comparison or {}).get("rows") or [], start=1):
        for k, v in row.items():
            if isinstance(v, Mapping):
                for part in ("baseline", "scenario", "delta"):
                    if isinstance(v.get(part), (int, float)):
                        out[f"s{n}.{k}.{part}"] = float(v[part])
    return out


def scenario_labels(comparison: Mapping) -> dict:
    return {f"s{n}": {"label": row.get("label"), "policy": row.get("policy")} for n, row in enumerate((comparison or {}).get("rows") or [], start=1)}


def _number_matches(n: float, values: list) -> bool:
    for v in values:
        for cand in (v, v * 100):                                                      # a share may be stated as a percentage
            if abs(cand - n) <= max(0.005, abs(cand) * 0.005):
                return True
    return False


def validate_interpretation_object(obj, metrics: Mapping, limits: Mapping = LIMITS) -> dict:
    """LLM-S1: keep a statement only when it cites known metrics, every number in it is one of the cited metrics (rounding or percent allowed), it is plain text and it
    does not turn an exploratory result into a certainty or a forecast. Rejected statements keep their reason."""
    if not isinstance(obj, dict) or set(obj) != INTERPRETATION_KEYS or obj.get("status") not in ("OK", "NONE_FOUND", "UNSURE"):
        raise ValueError("keys_or_status")
    if not isinstance(obj["statements"], list) or len(obj["statements"]) > limits["max_findings"] or not isinstance(obj["notes"], list):
        raise ValueError("statements")
    kept, rejected = [], []
    for st in obj["statements"]:
        if not isinstance(st, dict) or set(st) != {"text", "metric_refs", "kind"}:
            raise ValueError("statement_shape")
        text = _plain(st.get("text"), limits["max_statement_chars"])
        refs = st.get("metric_refs") if isinstance(st.get("metric_refs"), list) else []
        if text is None:
            rejected.append({"text": str(st.get("text"))[:80], "reason": "unsafe_or_empty_text"})
        elif st.get("kind") not in STATEMENT_KINDS:
            rejected.append({"text": text[:80], "reason": "kind_unknown"})
        elif not refs or any(r not in metrics for r in refs):
            rejected.append({"text": text[:80], "reason": "metric_not_in_engine_results"})
        elif any(not _number_matches(float(n), [metrics[r] for r in refs]) for n in _NUMBER.findall(text)):
            rejected.append({"text": text[:80], "reason": "number_not_in_cited_metrics"})
        elif _CERTAINTY.search(text):
            rejected.append({"text": text[:80], "reason": "overstates_certainty"})
        else:
            kept.append({"text": text, "metric_refs": sorted(set(refs)), "kind": st["kind"]})
    notes = [n for n in (_plain(x, limits["max_statement_chars"]) for x in obj["notes"][: limits["max_notes"]]) if n]
    return {"status": obj["status"], "statements": kept, "rejected": rejected, "notes": notes}


def _role_ids(capacity: Mapping, known_roles: Iterable[str]) -> dict:
    """A capacity key written as the role's name (`manager`) becomes the one known role id it names exactly (`role-manager`); nothing fuzzy, an unknown key stays as written
    and the engine's validator refuses it (live smoke LHA-SMOKE-20261018-07: every scenario used role names)."""
    known = set(known_roles) | {"manual_review"}
    out = {}
    for key, v in capacity.items():
        k = key if not isinstance(key, str) or key in known else ("role-" + "-".join(key.strip().lower().split()))
        k = k if k in known else key
        if k in out:
            raise ValueError("pairs")
        out[k] = v
    return out


def validate_scenarios_object(obj, known_roles: Iterable[str], limits: Mapping = LIMITS) -> dict:
    """The designer's typed scenarios: each one validated by the engine's own bounds; invalid ones rejected with the reason. Raises ValueError for a wrong shape."""
    from core.change_intelligence.v2 import process_simulation as ps
    if not isinstance(obj, Mapping) or set(obj) != SCENARIOS_KEYS:
        raise ValueError("keys")
    if obj["status"] not in STATUSES or not isinstance(obj["scenarios"], list) or not isinstance(obj["notes"], list):
        raise ValueError("status_or_lists")
    if len(obj["scenarios"]) > limits.get("max_scenarios", 4) or len(obj["notes"]) > limits["max_notes"]:
        raise ValueError("too_many_items")
    kept, rejected = [], []
    from core.change_intelligence.v2.business_mining import pairs_to_map
    for s in obj["scenarios"]:
        try:
            if isinstance(s, Mapping) and isinstance(s.get("capacity"), list):                   # structured output sends the capacity map as {role, per_day} pairs
                s = dict(s, capacity=pairs_to_map(s["capacity"], "role", "per_day"))
            if isinstance(s, Mapping) and isinstance(s.get("capacity"), Mapping):
                s = dict(s, capacity=_role_ids(s["capacity"], known_roles))
            kept.append(ps.validate_scenario(s, known_roles=known_roles))
        except ValueError as exc:
            rejected.append({"reason": f"scenario_invalid:{exc}", "label": str((s or {}).get("label") if isinstance(s, Mapping) else s)[:60]})
    notes = [n for n in (_plain(x, 300) for x in obj["notes"] if isinstance(x, str)) if n]
    return {"status": obj["status"], "scenarios": kept, "rejected": rejected, "notes": notes, "findings": [], "disputes": [], "rejected_disputes": []}


def json_dumps_safe(value) -> str:
    import json
    return json.dumps(value, sort_keys=True)


# ---- coordinator decisions ----------------------------------------------------------------------------------------------------------------------------------------------

def rebuttal_targets(round1: Iterable[Mapping], m: Mapping) -> list:
    """Claims that deserve one bounded rebuttal round: findings of a rebuttal severity raised by exactly ONE role in round 1. Sorted, bounded by the manifest."""
    if int(m.get("rebuttal_max_per_role", 0)) < 1:
        return []
    by_claim: dict = {}
    for child in round1:
        res = child.get("result") or {}
        for f in res.get("findings") or []:
            by_claim.setdefault(f["claim_id"], {"finding": f, "roles": set()})["roles"].add(child["role"])
    targets = [v["finding"] | {"raised_by": sorted(v["roles"])} for v in by_claim.values() if len(v["roles"]) == 1 and v["finding"]["severity"] in m.get("rebuttal_severities", ())]
    return sorted(targets, key=lambda f: (SEVERITIES.index(f["severity"]), f["claim_id"]))[: LIMITS["max_disputes"]]


def rebuttal_roles(targets: Iterable[Mapping], m: Mapping, completed_roles: Iterable[str]) -> dict:
    """Which roles get a rebuttal call and with which claims: every completed role, for the target claims it did NOT raise itself (one call per role, bounded by the manifest).
    -> {role: [claims]}; empty when nothing is contested."""
    targets = list(targets)
    out = {}
    for role in sorted(set(completed_roles)):
        if role not in m["roles"]:
            continue
        mine = [t for t in targets if role not in t["raised_by"]]
        if mine:
            out[role] = mine
    return out


# ---- join ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def join(children: Iterable[Mapping], m: Mapping, *, binding: Mapping, reason_incomplete: Optional[str] = None) -> dict:
    """The deterministic report. `children`: [{child_id, role, round, status: completed|failed|cancelled|pending, result?}]. Same inputs -> same `report_hash`; the order of
    children never matters. Minority and contested findings are kept; a role that failed is named; evidence cited by several roles is one source, counted once."""
    children = sorted(children, key=lambda c: (c["round"], c["role"], c["child_id"]))
    roles = {r: {"required": bool(spec.get("required")), "rounds": {}, "status": "missing"} for r, spec in m["roles"].items()}
    findings: dict = {}
    disputes: list = []
    rejected_total = 0
    mismatch_rows, synthesis_notes = [], []
    tests = None
    simulation = None
    repair_history: list = []
    sandbox_rounds: list = []
    for c in children:
        r = roles.setdefault(c["role"], {"required": False, "rounds": {}, "status": "missing"})
        r["rounds"][str(c["round"])] = c["status"]
        if c["round"] >= max(int(k) for k in r["rounds"]):                               # the role's status is its LATEST round (a repeated role such as the sandbox or the repairer)
            r["status"] = "completed" if c["status"] == "completed" else c["status"]
        res = c.get("result") or {}
        if role_kind(m, c["role"]) == "repair":
            repair_history.append({"round": c["round"], "status": c["status"], "sites": res.get("sites") or [], "rejected_sites": res.get("rejected") or [], "new_candidate_job_id": res.get("new_candidate_job_id"),
                                   "rationale": res.get("rationale"), "failed": res.get("failed")})
        if c["status"] != "completed":
            continue
        rejected_total += len(res.get("rejected") or [])
        if role_kind(m, c["role"]) == "drift":
            mismatch_rows += res.get("rows") or []
            synthesis_notes += res.get("notes") or []
        if role_kind(m, c["role"]) == "sandbox":
            sandbox_rounds.append({"round": c["round"], "candidate_job_id": res.get("candidate_job_id"), "patch_hash": res.get("patch_hash"), "verdict": res.get("verdict"),
                                   "protected": res.get("protected"), "generated": res.get("generated")})
            tests = dict(tests or {}, **{k: res.get(k) for k in ("candidate_job_id", "patch_hash", "base_sha", "test_file", "cases", "protected", "generated", "verdict", "isolation")})
        if role_kind(m, c["role"]) == "tests":
            tests = dict(tests or {}, proposed_cases=len(res.get("cases") or []), rejected_cases=res.get("rejected") or [])
        if role_kind(m, c["role"]) == "simulation":
            simulation = dict(simulation or {}, **{k: res.get(k) for k in ("baseline", "scenarios", "comparison", "approver_roles", "seed", "scenarios_proposed", "convergence", "calibration")})
        if role_kind(m, c["role"]) == "scenarios":
            simulation = dict(simulation or {}, proposed=len(res.get("scenarios") or []), rejected_scenarios=res.get("rejected") or [])
        if role_kind(m, c["role"]) == "interpretation":
            simulation = dict(simulation or {}, interpretation={"status": res.get("status"), "statements": res.get("statements") or [], "rejected": res.get("rejected") or [],
                                                                 "label": "Model interpretation of the engine's KPI table: every number is one of the cited metrics; exploratory, not a forecast"})
        for f in res.get("findings") or []:
            e = findings.setdefault(f["claim_id"], {**{k: f[k] for k in ("claim_id", "category", "severity", "statement", "evidence_refs")}, "raised_by": [], "rounds": [],
                                                   "assumptions": [], "counterexamples": [], "disputes": [], **({"legal_support": f["legal_support"]} if f.get("legal_support") else {})})
            if c["role"] not in e["raised_by"]:
                e["raised_by"].append(c["role"])
            e["rounds"].append(c["round"])
            e["assumptions"] = sorted(set(e["assumptions"]) | set(f.get("assumptions") or []))
            if f.get("counterexample") and f["counterexample"] not in e["counterexamples"]:
                e["counterexamples"].append(f["counterexample"])
        for d in res.get("disputes") or []:
            disputes.append({"claim_id": d["claim_id"], "by": c["role"], "round": c["round"], "reason": d["reason"]})
    if tests is not None:
        tests["attempts"] = len(sandbox_rounds)
        tests["history"] = sandbox_rounds
        tests["repairs"] = repair_history
        last = sandbox_rounds[-1] if sandbox_rounds else None
        if last is not None and last.get("verdict") != "review_ready" and not reason_incomplete:
            reason_incomplete = f"sandbox_verdict:{last.get('verdict')}"
    for d in disputes:
        if d["claim_id"] in findings:
            findings[d["claim_id"]]["disputes"].append({"by": d["by"], "reason": d["reason"]})
    for f in findings.values():
        f["raised_by"].sort()
        if f["disputes"]:
            f["resolution"] = "upheld_by_raiser_disputed_by_others" if 2 in f["rounds"] else "unresolved"
            f["contested"] = True
        else:
            f["contested"], f["resolution"] = False, ("agreed" if len(f["raised_by"]) >= 2 else "single_role")
    ordered = sorted(findings.values(), key=lambda f: (SEVERITIES.index(f["severity"]), f["category"], f["claim_id"]))
    agreements = [f["claim_id"] for f in ordered if len(f["raised_by"]) >= 2]
    unresolved = [f["claim_id"] for f in ordered if f["contested"]]
    missing_required = sorted(r for r, v in roles.items() if v["required"] and v["status"] != "completed")
    evidence_use: dict = {}
    for f in ordered:
        for ref in f["evidence_refs"]:
            evidence_use.setdefault(ref, set()).update(f["raised_by"])
    status = "INCOMPLETE" if (missing_required or reason_incomplete) else "REVIEW_READY"
    report = {"schema": SCHEMA, "workflow_manifest_hash": manifest_hash(m), "binding": dict(binding), "status": status,
              "findings": ordered, "agreements": agreements, "unresolved_disagreements": unresolved, "missing_required_roles": missing_required,
              "mismatch_table": sorted(mismatch_rows, key=lambda r: r["rule_id"]), "synthesis_notes": synthesis_notes, "tests": tests, "simulation": simulation,
              "roles": {r: {"required": v["required"], "status": v["status"], "rounds": v["rounds"]} for r, v in sorted(roles.items())},
              "coverage": {"children": len(children), "completed": sum(1 for c in children if c["status"] == "completed"), "rejected_findings": rejected_total,
                           "evidence_sources": {ref: sorted(rs) for ref, rs in sorted(evidence_use.items())}, "distinct_sources": len(evidence_use)},
              "incomplete_reason": reason_incomplete, "human_action_needed": True,
              "limitations": ["Specialists are the same kind of model with different rubrics: agreement among them is not independent validation.",
                              "A finding is a grounded hypothesis for a person to check; severity follows the rubric and the reviewer, not a model score.",
                              "Legal findings carry no versioned legal source and are marked unsupported.",
                              "A drift row compares the decided policy with observed repository text, never with production behaviour.",
                              "Generated tests are typed cases rendered by the server and run with advisory isolation; passing them is not proof of correctness for every input.",
                              "A repair only adds literal sites the server itself scanned for the decided rule's current value; it never names a new number, never touches a test, and stops at the declared cap with its failed history kept.",
                              "A simulation result is exploratory: declared assumptions, no calibration, no behavioural response, not a forecast.",
                              "The report binds to the reviewed change version; a moved pin makes it stale."]}
    report["report_hash"] = canonical_hash({k: v for k, v in report.items() if k != "report_hash"})
    return report


def budget_invariant(root_reserved_usd, children: Iterable[Mapping]) -> dict:
    """`booked + allocated_unspent + unallocated_reserved <= root`: booked = settled child cost, allocated_unspent = open child reservations, unallocated = the root row's remainder."""
    from decimal import Decimal
    booked = sum((Decimal(str(c.get("cost_usd") or 0)) for c in children if c.get("entry_state") == "SETTLED"), Decimal("0"))
    allocated = sum((Decimal(str(c.get("reserved_usd") or 0)) for c in children if c.get("entry_state") == "RESERVED"), Decimal("0"))
    root = Decimal(str(root_reserved_usd or 0))
    remainder = Decimal(str(next((c.get("root_remainder_usd") for c in children if c.get("root_remainder_usd") is not None), root - booked - allocated)))
    total = booked + allocated + remainder
    return {"root_reserved_usd": str(root), "booked_usd": str(booked), "allocated_unspent_usd": str(allocated), "unallocated_reserved_usd": str(remainder),
            "sum_usd": str(total), "holds": total <= root + Decimal("0.0000001")}
