"""Pitch D0: deterministic rule extractor (AI-off arm). See DEFINITION.md for the exact rules and limits.

Input: the same persisted observations the production extraction executor reads
(`source_observations`: dicts with artifact_id, locator, raw_value, parser_version).
Output: the same result dict shape `ExtractRulesExecutor.execute` returns ("candidates" with the same
per-candidate keys, "rejected", "model_run_id", ...), built through the same `RuleCandidate` schema
validation, so the same downstream linking/conflict/impact code runs on both arms.
No provider, network, clock or randomness is used: same input -> byte-identical output.
"""
from __future__ import annotations

import hashlib
import json
import re

from core.change_intelligence.v2.schema import EvidenceRef, RuleCandidate, ValidationError
from infra.worker.rule_identity import _parse_value  # stdlib-only product module (tested provider-free)
from pitch.d0.guard import no_provider_calls

D0_VERSION = "d0.3.0"
EXECUTOR_ID = "extraction.d0_deterministic.v1"
_SCHEMA_VERSION = "1.0.0"  # same pin as production extraction

# --- registered vocabularies (frozen with the definition; tokens of the LAST key segment) ---
_MUST_NUMERIC = {"limit", "threshold", "max", "maximum", "min", "minimum", "cap", "ceiling", "floor",
                 "quota", "deadline", "sla", "tolerance", "retention", "timeout", "ttl", "budget",
                 "retries", "required"}
_OBSERVED_NUMERIC = {"amount", "days", "hours", "minutes", "rate", "percent", "pct", "count", "fee",
                     "interval", "period", "cooldown", "duration", "size", "length"}
_NUMERIC_TOKENS = _MUST_NUMERIC | _OBSERVED_NUMERIC
_FLAG_MUST = {"require", "requires", "required", "mandatory", "enforce", "enforced", "forbidden",
              "prohibited", "block", "blocked"}
_FLAG_MAY = {"allow", "allowed", "optional", "permit", "permitted"}
_FLAG_OBSERVED = {"enabled", "enable"}
_FLAG_TOKENS = _FLAG_MUST | _FLAG_MAY | _FLAG_OBSERVED
_OUTCOME_TOKENS = {"approver", "approvers", "reviewer", "escalate", "escalation"}
_EXCEPTION_TOKENS = {"exception", "exceptions", "exempt", "exempted", "exemption", "exemptions",
                     "exclude", "excluded", "except"}
_SCOPE_KEYS = {"currency", "unit", "region", "department", "category", "type", "applies_to", "scope",
               "tier", "level", "grade", "country", "segment", "channel"}
_CURRENCY = {"jpy", "usd", "eur", "vnd", "gbp", "cny", "krw", "sgd", "aud", "thb"}  # key-suffix tokens only
_UNIT_SUFFIX = {"days": "days", "day": "days", "hours": "hours", "hour": "hours", "minutes": "minutes",
                "mins": "minutes", "seconds": "seconds", "secs": "seconds", "pct": "%",
                "percent": "%", "bps": "bps", "count": "count"}


def _tokens(segment: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", segment)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _value_text(value) -> str | None:
    # same canonical text as infra.worker.extraction._value_text (kept local to avoid importing the provider module)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return json.dumps(value)
    return json.dumps(value, sort_keys=True, default=str)


def _label(obs: dict) -> str | None:
    loc = obs.get("locator")
    if not isinstance(loc, dict):
        return None
    if isinstance(loc.get("key_path"), str) and loc["key_path"]:
        return loc["key_path"]
    if isinstance(loc.get("symbol"), str) and loc["symbol"]:
        return loc["symbol"]
    if isinstance(loc.get("sheet"), str) and isinstance(loc.get("cell"), str):
        return f"{loc['sheet']}!{loc['cell']}"
    return None


def _unit_text(kind, unit):
    """Display unit from rule_identity's canonical unit tuple (or None)."""
    if kind == "op":
        unit = unit[1]
    if not unit:
        return None
    if unit[0] == "cur":
        return unit[1]
    if unit[0] == "pct":
        return "%"
    if unit[0] == "dur":
        return unit[1] + "s"
    return unit[1] if isinstance(unit[1], str) else None


def _parse_number(raw):
    """(value, unit_hint, operator) via the product's _parse_value, or None when not a clean number.
    Operator values (">= 500000") keep their original text as value so downstream value_comparison parses them."""
    if isinstance(raw, bool):
        return None
    kind, unit, mag = _parse_value(raw, None)
    if kind not in ("num", "qty", "op"):
        return None
    num = int(mag) if mag == mag.to_integral_value() else float(mag)
    if kind == "op":
        return str(raw).strip(), _unit_text(kind, unit), unit[0]
    return num, _unit_text(kind, unit), None


def _unit_from_key(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Strip a trailing unit/currency token from the predicate tokens."""
    if len(tokens) > 1:
        last = tokens[-1]
        if last in _CURRENCY:
            return last.upper(), tokens[:-1]
        if last in _UNIT_SUFFIX:
            return _UNIT_SUFFIX[last], tokens[:-1]
    return None, tokens


def _evidence(obs: dict, quote: str, source_revision_id: str, artifact_id: str, blob_hash) -> EvidenceRef:
    return EvidenceRef(
        source_revision_id=source_revision_id, artifact_revision_id=artifact_id,
        content_hash=blob_hash or "unknown", locator_type="chunk",
        locator=json.dumps(obs["locator"], sort_keys=True), quote=quote,
        parser_version=obs.get("parser_version") or "unknown")


def _structured_rules(leaves: list[dict]):
    """Yield raw rule dicts from structured (key-path / symbol / cell) leaves."""
    by_path = {_label(o): o for o in leaves}
    for obs in leaves:
        path = _label(obs)
        if "!" in path:  # workbook cells are out of D0 scope
            continue
        raw = obs.get("raw_value")
        parent, _, last = path.rpartition(".")
        tokens = _tokens(last)
        if not tokens:
            continue
        tset = set(tokens)
        modality = value = outcome = operator = None
        unit = None
        rule_tokens = tokens
        why = None
        if tset & _OUTCOME_TOKENS and isinstance(raw, str) and raw.strip():
            modality, value, outcome = "must", raw, raw
            why = "outcome/approver key"
        elif tset & _FLAG_TOKENS and isinstance(raw, bool):
            modality = "must" if tset & _FLAG_MUST else ("may" if tset & _FLAG_MAY else "observed")
            value = raw
            why = "flag key"
        elif tset & _NUMERIC_TOKENS:
            parsed = _parse_number(raw)
            if parsed is None:
                continue
            value, hint, operator = parsed
            modality = "must" if tset & _MUST_NUMERIC else "observed"
            unit, rule_tokens = _unit_from_key(tokens)
            unit = unit or hint
            why = "numeric key"
        else:
            continue
        # siblings: scope keys and exceptions in the same parent mapping (same prefix, one level)
        prefix = parent + "." if parent else ""
        scope = {"section": parent or "<root>"}
        exceptions: list[str] = []
        for other_path, other in sorted(by_path.items()):
            if other_path == path or not other_path.startswith(prefix):
                continue
            rest = other_path[len(prefix):]
            if "." in rest:
                continue
            otokens = _tokens(rest)
            oraw = other.get("raw_value")
            scalar = isinstance(oraw, (str, int, float, bool))
            listed = isinstance(oraw, list) and oraw and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in oraw)
            if rest.lower() in _SCOPE_KEYS and (scalar or listed):
                # the product scope contract (rule_identity.scope_key) accepts scalar, bool and list values
                scope[rest.lower()] = oraw
                if rest.lower() in ("currency", "unit") and unit is None and isinstance(oraw, str):
                    unit = oraw.upper() if rest.lower() == "currency" else oraw
            elif set(otokens) & _EXCEPTION_TOKENS:
                if isinstance(oraw, list):
                    exceptions += [str(x) for x in oraw if isinstance(x, (str, int, float))]
                elif isinstance(oraw, str) and oraw.strip():
                    exceptions.append(oraw)
        quote = f"{path}: {_value_text(raw)}"
        yield {"obs": obs, "quote": quote, "subject": parent or "<root>",
               "predicate": "_".join(rule_tokens), "value": value, "unit": unit, "modality": modality,
               "scope": scope, "exceptions": tuple(exceptions), "outcome": outcome, "applicable": True,
               "rationale": f"D0 {why}: '{last}' matches the registered vocabulary"
                     + (f"; operator {operator}" if operator else "")}


_SENT = re.compile(r"[^.;\n!?]+(?:[.;!?]|$)")
_PROSE = re.compile(
    r"(?P<subj>[^,:]{3,80}?)\s+(?P<modal>must not exceed|must not be (?:less|more) than|must be at least|"
    r"must be at most|must|shall|may)\b(?P<pred>[^\d]{0,60}?)(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>%|[A-Za-z]{2,10})?", re.IGNORECASE)


def _prose_rules(obs: dict):
    text = obs.get("raw_value")
    if not isinstance(text, str):
        return
    for m in _SENT.finditer(text):
        sentence = m.group(0).strip()
        pm = _PROSE.search(sentence)
        if not pm:
            continue
        modal = pm.group("modal").lower()
        num = pm.group("num").replace(",", "")
        value = float(num) if "." in num else int(num)
        pred = re.sub(r"\W+", "_", f"{modal} {pm.group('pred')}".strip().lower()).strip("_")
        yield {"obs": obs, "quote": sentence, "subject": re.sub(r"\s+", " ", pm.group("subj")).strip(),
               "predicate": pred, "value": value, "unit": pm.group("unit"),
               "modality": "may" if modal == "may" else "must", "scope": None, "exceptions": (),
               "outcome": None, "applicable": False,
               "rationale": "D0 prose modal+number pattern; scope not stated"}


def extract_from_observations(observations, *, artifact_id: str, source_revision_id: str,
                              blob_hash: str | None = None, source_role: str = "unknown") -> dict:
    """Pure function of its input. Returns the production-shaped extraction result dict."""
    with no_provider_calls():
        obs_list = sorted((o for o in observations if o.get("artifact_id", artifact_id) == artifact_id
                           and o.get("raw_value") is not None),
                          key=lambda o: str(o.get("observation_id", "")) + json.dumps(o.get("locator"), sort_keys=True))
        digest = hashlib.sha256(json.dumps(
            [artifact_id, source_revision_id, D0_VERSION,
             [[o.get("locator"), o.get("raw_value")] for o in obs_list]],
            sort_keys=True, default=str).encode()).hexdigest()[:24]
        run_id = f"d0-{digest}"
        structured = [o for o in obs_list if _label(o) is not None]
        prose = [o for o in obs_list if _label(o) is None]
        raws = list(_structured_rules(structured))
        if source_role in ("normative", "unknown", "test"):
            for o in prose:
                raws.extend(_prose_rules(o))
        buckets: dict[tuple, dict] = {}
        unknown: list[dict] = []
        rejected: list[dict] = []
        for raw in raws:
            try:
                ev = _evidence(raw["obs"], raw["quote"], source_revision_id, artifact_id, blob_hash)
            except ValidationError as exc:
                rejected.append({"reason": f"schema_invalid: {exc}"})
                continue
            if not raw["applicable"]:
                unknown.append({**raw, "evidence": [ev]})
                continue
            key = (raw["subject"], raw["predicate"], json.dumps(raw["scope"], sort_keys=True, default=str))
            b = buckets.setdefault(key, {**raw, "evidence": [], "merged_count": 0})
            if ev.quote not in {e.quote for e in b["evidence"]}:
                b["evidence"].append(ev)
            b["merged_count"] += 1
        built = []
        entries = [(b, "RECOGNIZED") for b in buckets.values()] + [(u, "UNKNOWN") for u in unknown]
        for index, (b, applic) in enumerate(entries):
            kwargs = dict(
                candidate_id=f"cand-{run_id}-{index:04d}", subject=b["subject"], predicate=b["predicate"],
                value=b["value"], applicability=applic, modality=b["modality"],
                source_revision_id=source_revision_id, artifact_revision_id=artifact_id,
                evidence=tuple(b["evidence"]), scope=b["scope"] or {"unresolved": True},
                schema_version=_SCHEMA_VERSION, unit=b["unit"] if isinstance(b["unit"], str) else None,
                exceptions=b["exceptions"], outcome=b["outcome"],
                unknown_fields={"rationale": b["rationale"]}, model_run_id=run_id)
            if applic == "UNKNOWN":
                kwargs["applicability_reason"] = "missing_scope"
            try:
                cand = RuleCandidate(**kwargs)
            except ValidationError as exc:
                rejected.append({"index": index, "reason": f"schema_invalid: {exc}"})
                continue
            built.append({
                "candidate_id": cand.candidate_id, "subject": cand.subject, "predicate": cand.predicate,
                "value": cand.value, "unit": cand.unit, "applicability": cand.applicability,
                "applicability_reason": cand.applicability_reason, "modality": cand.modality,
                "scope": cand.scope, "exceptions": list(cand.exceptions), "outcome": cand.outcome,
                "source_revision_id": cand.source_revision_id,
                "artifact_revision_id": cand.artifact_revision_id, "model_run_id": cand.model_run_id,
                "ancestry": {"merged_count": b.get("merged_count", 1)},
                "rationale": cand.unknown_fields.get("rationale"),
                "evidence": [{"locator": e.locator, "quote": e.quote, "parser_version": e.parser_version}
                             for e in cand.evidence]})
        return {"candidates": built, "rejected": rejected, "model_run_id": run_id,
                "schema_version": _SCHEMA_VERSION, "executor_id": EXECUTOR_ID,
                "context_chunks_used": None, "context_chunks_total": None,
                "live_call_provenance": "d0_deterministic", "d0_version": D0_VERSION,
                "provider_calls": 0}


def extract_from_store(store, tenant_id, workspace_id, source_revision_id, artifact_id,
                       source_role: str = "unknown") -> dict:
    """Read the pinned revision's observations the way production extraction does, then run D0."""
    obs = [o for o in store.source_observations(tenant_id, workspace_id, source_revision_id)
           if o["artifact_id"] == artifact_id]
    view = store.source_revision_view(tenant_id, workspace_id, source_revision_id)
    art = next((a for a in view["artifacts"] if a["artifact_id"] == artifact_id), None)
    return extract_from_observations(obs, artifact_id=artifact_id, source_revision_id=source_revision_id,
                                     blob_hash=(art or {}).get("blob_hash"), source_role=source_role)
