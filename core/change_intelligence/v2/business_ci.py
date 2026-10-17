# -*- coding: utf-8 -*-
"""Business CI drift check (MON-CI, ADVISORY half; pure, no I/O, no model, no Git).

A versioned report that says, for one repository state (`base_sha` -> `head_sha`), whether each business rule of ONE accepted policy snapshot that is bound to a supported
source location is still **aligned** with the code, has **drifted**, is **not touched** by the diff, or **cannot be checked** (unsupported/incomplete). Supported profile:
**YAML existing scalar** (a rule whose condition has exactly one numeric comparison and a `file.yaml#dotted.key` source binding). Everything else is reported as
`unsupported`, never as compliant; a missing mapping is never "all business rules passed". The checker never executes repository code and never changes the policy, the
mapping or the repository; the policy and the mapping are pinned by hash so a candidate change cannot move the yardstick.

This is the advisory, report-only step: a required-check integration on a real repository is the package's completion gate and is not part of this module.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional

from core.change_intelligence.v2.business_rebase import _num, _numeric_leaves
from core.change_intelligence.v2.policy_decision import effective_rules
from core.change_intelligence.v2.serialize import canonical_hash

SCHEMA = "ripple-business-ci/1"
CHECKER_VERSION = "business-ci/1 (yaml-scalar profile)"
PROFILE = "yaml-scalar"
STATUSES = ("aligned", "drift", "no_impact", "unsupported")
UNREADABLE = object()        # what a reader returns when the location does not exist or is not a scalar


def policy_hash(policy: Mapping) -> str:
    """Hash of the rules a check is measured against (ids, conditions, authority, bindings), so a moved policy is detected."""
    rules = sorted((dict(r) for r in policy.get("rules") or []), key=lambda r: r["rule_id"])
    return canonical_hash({"snapshot_id": policy.get("snapshot_id"), "business_revision": policy.get("business_revision"),
                           "rules": [{k: r.get(k) for k in ("rule_id", "condition", "authority", "status", "source_bindings", "actor_ref", "outcome")} for r in rules]})


def mapping_hash(mapping: Mapping) -> str:
    return canonical_hash({"profile": mapping.get("profile"), "repo_id": mapping.get("repo_id"), "files": mapping.get("files"), "policy_hash": mapping.get("policy_hash")})


def _binding_location(evidence_ref: str) -> Optional[tuple[str, str]]:
    """'approval.yaml#routing_rules.key' -> ('approval.yaml', 'routing_rules.key'); None when the ref is not a YAML key binding."""
    if not isinstance(evidence_ref, str) or "#" not in evidence_ref:
        return None
    name, _, key = evidence_ref.partition("#")
    if not name.lower().endswith((".yaml", ".yml")) or not key or "/" in key or " " in key:
        return None
    return name, key


def plan_checks(policy: Mapping, mapping: Mapping) -> dict:
    """Which rules can be checked under the profile and where. -> {"checks": [...], "unmapped": [...], "unsupported": [...]}.
    A rule is checkable when it is effective (a TARGET replaces its OBSERVED root), its condition has exactly one numeric leaf and at least one YAML key binding whose file
    the mapping maps to a repository path. The expected value is the leaf's value (the decided one for a TARGET rule). `unmapped` lists rules without any YAML binding
    (outside this profile: coverage only); `unsupported` lists YAML-bound rules the profile cannot check (file missing from the mapping, condition shape)."""
    eff, _superseded = effective_rules(policy.get("rules") or [])
    files = mapping.get("files") or {}
    checks, unmapped, unsupported = [], [], []
    for r in sorted(eff, key=lambda x: x["rule_id"]):
        leaves = _numeric_leaves(r.get("condition"))
        locs = [loc for loc in (_binding_location(b.get("evidence_ref")) for b in r.get("source_bindings") or []) if loc]
        if not locs:
            unmapped.append({"rule_id": r["rule_id"], "reason": "no_yaml_binding"})
            continue
        if len(leaves) != 1:
            unsupported.append({"rule_id": r["rule_id"], "reason": "condition_not_single_numeric", "leaves": len(leaves)})
            continue
        for name, key in locs:
            path = files.get(name)
            if not path:
                unsupported.append({"rule_id": r["rule_id"], "reason": "file_not_in_mapping", "file": name})       # a YAML-bound rule the mapping forgot: blocking, never silent
                continue
            checks.append({"rule_id": r["rule_id"], "authority": r.get("authority"), "path": path, "key": key, "expected": leaves[0]["value"], "unit": leaves[0].get("unit"),
                           "field": leaves[0].get("field")})
    return {"checks": checks, "unmapped": unmapped, "unsupported": unsupported}


def compare(check: Mapping, base_value, head_value, *, changed_in_diff: bool) -> dict:
    """One check against the values read at base and head. UNREADABLE at head is `unsupported` (never compliant); equal to the policy is `aligned`; a different readable
    value is `drift` (pre-existing drift is still drift, flagged `introduced_by_diff: False`); a location the diff did not touch that is aligned is `no_impact`."""
    out = {"rule_id": check["rule_id"], "path": check["path"], "key": check["key"], "expected": check["expected"], "unit": check.get("unit"),
           "base": None if base_value is UNREADABLE else base_value, "head": None if head_value is UNREADABLE else head_value, "changed_in_diff": bool(changed_in_diff)}
    if head_value is UNREADABLE:
        out.update(status="unsupported", reason="head_location_unreadable")
        return out
    head_n, exp_n = _num(head_value), _num(check["expected"])
    if head_n is None or exp_n is None:
        out.update(status="unsupported", reason="not_numeric")
        return out
    if head_n == exp_n:
        out.update(status="aligned" if changed_in_diff else "no_impact", reason=None)
    else:
        out.update(status="drift", reason="head_differs_from_policy", introduced_by_diff=bool(changed_in_diff and base_value is not UNREADABLE and _num(base_value) == exp_n))
    return out


def run(policy: Mapping, mapping: Mapping, *, repo_id: str, base_sha: str, head_sha: str, read_base: Callable[[str, str], object], read_head: Callable[[str, str], object],
        changed_paths: Iterable[str]) -> dict:
    """The report. `read_base/read_head(path, key)` return the scalar at that location in that commit or UNREADABLE. A mapping whose pinned `policy_hash` differs from the
    policy given, or whose `repo_id` differs from the repository, yields an `incomplete` report with no rule evaluated (the yardstick moved or the wrong repository)."""
    p_hash, m_hash = policy_hash(policy), mapping_hash(mapping)
    base = {"schema": SCHEMA, "checker_version": CHECKER_VERSION, "profile": PROFILE, "repo_id": repo_id, "base_sha": base_sha, "head_sha": head_sha,
            "policy": {"snapshot_id": policy.get("snapshot_id"), "business_revision": policy.get("business_revision"), "policy_hash": p_hash}, "mapping_hash": m_hash,
            "mode": "advisory", "results": [], "coverage": {}, "summary": {}, "conclusion": None, "would_block": None, "incomplete_reasons": []}
    if mapping.get("policy_hash") != p_hash:
        base["incomplete_reasons"].append("policy_moved: the mapping is pinned to another policy hash")
    if mapping.get("repo_id") != repo_id:
        base["incomplete_reasons"].append("wrong_repository: the mapping is declared for another repository id")
    if mapping.get("profile") != PROFILE:
        base["incomplete_reasons"].append("unsupported_profile")
    if base["incomplete_reasons"]:
        base.update(conclusion="incomplete", would_block=True, statement="Nothing was evaluated: an incomplete check never counts as compliant.")
        base["report_hash"] = _hash(base)
        return base
    plan = plan_checks(policy, mapping)
    changed = set(changed_paths)
    for c in plan["checks"]:
        base["results"].append(compare(c, read_base(c["path"], c["key"]), read_head(c["path"], c["key"]), changed_in_diff=c["path"] in changed))
    counts = {s: sum(1 for r in base["results"] if r["status"] == s) for s in STATUSES}
    rules_total = len(effective_rules(policy.get("rules") or [])[0])
    # `unmapped` = rules this profile cannot see at all (no YAML binding): stated as coverage, never counted as checked; `unsupported_rules` = rules the profile should see but
    # cannot check (forgotten mapping file, condition shape): these block, because silence would masquerade as compliance
    base["coverage"] = {"rules_total": rules_total, "rules_checked": len({r["rule_id"] for r in base["results"]}), "unmapped": plan["unmapped"], "unsupported_rules": plan["unsupported"],
                        "complete": not plan["unsupported"] and counts["unsupported"] == 0}
    base["summary"] = counts
    if counts["drift"]:
        base.update(conclusion="drift", would_block=True)
    elif counts["unsupported"] or not base["coverage"]["complete"]:
        base.update(conclusion="incomplete", would_block=True)
    else:
        base.update(conclusion="aligned", would_block=False)
    base["statement"] = {"drift": "At least one mapped rule differs from the accepted policy at head: a required check would block.",
                         "incomplete": "Some rules could not be checked (unmapped or unsupported locations): a required check would block rather than pass by omission.",
                         "aligned": "Every mapped rule reads the policy value at head; rules without a mapping are listed, not assumed."}[base["conclusion"]]
    base["report_hash"] = _hash(base)
    return base


def _hash(report: Mapping) -> str:
    return canonical_hash({k: v for k, v in report.items() if k != "report_hash"})


def verify(report: Mapping, *, current_head_sha: Optional[str] = None) -> list:
    """Problems with a stored report: a forged/edited body (hash), a head that moved since the check (stale)."""
    problems = []
    if report.get("report_hash") != _hash(report):
        problems.append("report_hash_mismatch")
    if current_head_sha and report.get("head_sha") != current_head_sha:
        problems.append("stale_head: the repository head moved after this check")
    if report.get("schema") != SCHEMA:
        problems.append("unknown_schema")
    return problems
