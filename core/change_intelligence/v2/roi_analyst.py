"""SUR-07: an analyst memo over the MON-ROI replay metric pack (SYNTHETIC data only until a data owner grants real data). Pure functions: no I/O, no provider.

A model reads the immutable replay report (counts, value bands, effort proxy, declared assumptions, coverage) and writes: statements that cite metric keys, questions that
challenge a declared assumption, and sensitivity scenarios (another value for a declared assumption). The server keeps a statement only if every number in it is the value of a
metric it cites; realised-ROI, savings, forecast and certainty wording is refused; a scenario is kept only for a declared assumption key with a positive number, and its
results are computed by the deterministic replay engine (never by the model). Everything is labelled as a replay of a SYNTHETIC dataset: no realised ROI, no forecast.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

SCHEMA = "ripple-roi-analyst/1"
MODEL_KEYS = frozenset({"status", "statements", "assumption_questions", "scenarios", "notes"})
STATUSES = ("memo", "insufficient")
SCENARIO_KEYS = frozenset({"assumption", "value", "rationale"})
ASSUMPTION_KEYS = ("minutes_per_approval", "hourly_cost")
LABEL = ("Analyst memo over a replay of a SYNTHETIC dataset: every number is computed by the deterministic replay engine; the model only explains and asks. It is not a "
         "realised ROI, a saving or a forecast, and it says nothing about real data, which has not been granted.")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
_CLAIM = re.compile(r"\b(reali[sz](?:e|ed|es|ing|ation)\b|sav(?:e|es|ed|ing|ings)\b|will cost|return on investment|ROI|forecast|guarantee|certain(?:ly)?|definitely|net benefit|payback)", re.I)
_NOT_PLAIN = re.compile(r"[<>`|\\]|\*\*|^#|\]\(|https?://", re.M)


def _norm(v) -> str | None:
    try:
        d = Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return format(d.normalize(), "f")


def metric_table(report: Mapping) -> dict:
    """Flat {key: normalised number} of the numeric fields of a replay report (depth <= 3, lists by index); text fields are left out."""
    out = {}

    def walk(prefix, node, depth):
        if depth > 3:
            return
        if isinstance(node, Mapping):
            for k, v in node.items():
                if k in ("unsupported_rows", "excluded", "policies", "statement", "report_hash", "dataset", "basis", "pin"):
                    continue
                walk(f"{prefix}.{k}" if prefix else str(k), v, depth + 1)
        elif isinstance(node, list):
            for i, v in enumerate(node[:6]):
                walk(f"{prefix}[{i}]", v, depth + 1)
        elif not isinstance(node, bool) and isinstance(node, (int, float, str)):
            n = _norm(node)
            if n is not None:
                out[prefix] = n
    walk("", report, 0)
    for k in ("records_in", "valid"):
        if isinstance((report.get("dataset") or {}).get(k), (int, float)):
            out[f"dataset.{k}"] = _norm(report["dataset"][k])
    return out


def numbers_in(text: str) -> set:
    out = set()
    for m in _NUMBER.finditer(str(text or "")):
        n = _norm(m.group(0))
        if n is not None:
            out.add(n.lstrip("-"))
    return out


def validate_model_object(obj, limits: Mapping) -> dict:
    if not isinstance(obj, dict) or set(obj) != set(MODEL_KEYS):
        raise ValueError(f"keys must be {sorted(MODEL_KEYS)}")
    if obj["status"] not in STATUSES or not all(isinstance(obj[k], list) for k in ("statements", "assumption_questions", "scenarios", "notes")):
        raise ValueError("status / lists invalid")
    if len(obj["statements"]) > int(limits["max_statements"]) * 3 or len(obj["scenarios"]) > int(limits["max_scenarios"]) * 3:
        raise ValueError("too many items")
    for s in obj["statements"]:
        if not isinstance(s, dict) or set(s) != {"statement", "metric_keys"} or not isinstance(s["statement"], str) or not isinstance(s["metric_keys"], list) \
                or not all(isinstance(k, str) for k in s["metric_keys"]):
            raise ValueError("statement invalid")
    for q in obj["assumption_questions"]:
        if not isinstance(q, dict) or set(q) != {"question", "assumption"} or not all(isinstance(q[k], str) for k in q):
            raise ValueError("question invalid")
    for sc in obj["scenarios"]:
        if not isinstance(sc, dict) or set(sc) != set(SCENARIO_KEYS) or not isinstance(sc["assumption"], str) or not isinstance(sc["rationale"], str) \
                or isinstance(sc["value"], bool) or not isinstance(sc["value"], (int, float, str)):
            raise ValueError("scenario invalid")
    if not all(isinstance(n, str) for n in obj["notes"]):
        raise ValueError("notes must be strings")
    return obj


def check(model: Mapping, table: Mapping, declared: Iterable[str], limits: Mapping) -> dict:
    """-> {kept, rejected, questions, scenarios}. Items are refused whole with their reason, never edited."""
    declared = set(declared)
    max_chars = int(limits["max_text_chars"])
    kept, rejected, questions, scenarios = [], [], [], []
    for i, s in enumerate(model["statements"]):
        text, keys = " ".join(s["statement"].split()), list(dict.fromkeys(s["metric_keys"]))
        reason = None
        if len(kept) >= int(limits["max_statements"]):
            reason = "over_statement_cap"
        elif not text or len(text) > max_chars or _NOT_PLAIN.search(text):
            reason = "not_plain_text"
        elif not keys or any(k not in table for k in keys):
            reason = "metric_not_in_report"
        elif _CLAIM.search(text):
            reason = "roi_saving_or_forecast_claim"
        else:
            stray = sorted(numbers_in(text) - {table[k].lstrip("-") for k in keys})
            if stray:
                reason = "figure_not_a_cited_metric:" + ",".join(stray[:3])
        if reason:
            rejected.append({"kind": "statement", "index": i, "reason": reason})
            continue
        kept.append({"statement": text, "metric_keys": keys, "values": {k: table[k] for k in keys}, "origin": "model"})
    for i, q in enumerate(model["assumption_questions"][: int(limits["max_questions"])]):
        text = " ".join(q["question"].split())
        if q["assumption"] not in declared or not text or len(text) > max_chars or _NOT_PLAIN.search(text) or _CLAIM.search(text):
            rejected.append({"kind": "question", "index": i, "reason": "assumption_not_declared" if q["assumption"] not in declared else "question_not_acceptable"})
            continue
        questions.append({"question": text, "assumption": q["assumption"], "origin": "model"})
    for i, sc in enumerate(model["scenarios"]):
        value = _norm(sc["value"])
        reason = None
        if len(scenarios) >= int(limits["max_scenarios"]):
            reason = "over_scenario_cap"
        elif sc["assumption"] not in declared or sc["assumption"] not in ASSUMPTION_KEYS:
            reason = "assumption_not_declared"
        elif value is None or Decimal(value) <= 0:
            reason = "value_not_positive_number"
        elif any(x["assumption"] == sc["assumption"] and x["value"] == value for x in scenarios):
            reason = "duplicate"
        if reason:
            rejected.append({"kind": "scenario", "index": i, "reason": reason})
            continue
        scenarios.append({"assumption": sc["assumption"], "value": value, "rationale": " ".join(sc["rationale"].split())[:max_chars], "origin": "model"})
    return {"kept": kept, "rejected": rejected, "questions": questions, "scenarios": scenarios}
