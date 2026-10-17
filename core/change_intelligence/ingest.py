# -*- coding: utf-8 -*-
"""Six artifact families into one representation with provenance.

Reads a snapshot, never a directory. Every adapter here takes bytes, so
ingestion works identically on the immutable baseline and on a session
overlay whose artifacts were never written to disk (ADR-003) -- which is
what lets P6 re-ingest a shadow world and P8 re-ingest after an apply.

Determinism is a contract, not an aspiration: the same snapshot produces
byte-identical output, because everything is sorted and nothing consults a
clock or a random source.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from core.adapters.doc.policy_text.adapter import ADAPTER as POLICY_TEXT
from core.adapters.source.history.adapter import ADAPTER as HISTORY
from core.adapters.source.pytest_asserts.adapter import ADAPTER as PYTEST_ASSERTS
from core.adapters.source.python.adapter import ADAPTER as PYTHON
from core.adapters.workflow.yaml_routes.adapter import ADAPTER as WORKFLOW
from core.change_intelligence.identity import Candidate

class _Both:
    """Two adapters over one artifact, concatenated in order.

    Exists because the routing table picks exactly one entry, which is the
    right rule for choosing HOW to read a file and the wrong one for a file
    that is legitimately two things at once.
    """

    def __init__(self, *adapters):
        self._adapters = adapters

    @property
    def name(self) -> str:
        return "+".join(a.name for a in self._adapters)

    def parse_bytes(self, raw: bytes, path: str, snapshot_id: str) -> list:
        records = []
        for adapter in self._adapters:
            records.extend(adapter.parse_bytes(raw, path, snapshot_id))
        return records


#: path suffix / prefix -> the adapter that reads it. Explicit, because an
#: ingestion that guesses at file types will one day guess wrong quietly.
_SOURCE_ADAPTERS = [
    (lambda p: p.endswith(".yaml") or p.endswith(".yml"), WORKFLOW, "workflow"),
    # A test file is read TWICE, and the first version of this routed it to
    # the assertion reader alone. That silently cost 105 records: a test
    # file is ordinary Python too, and dropping the generic reader took its
    # CALLS, CONTAINS and IMPLEMENTS edges out of the enterprise graph.
    # Evidence coverage fell from 377 refs to 272 and nothing failed, which
    # is how a coverage loss normally travels.
    #
    # Both readers, then. `PYTHON` supplies the structure, `PYTEST_ASSERTS`
    # supplies what the suite asserts, and neither produces a candidate for
    # a `tests` family -- a test corroborates a rule, it does not state one.
    (lambda p: p.startswith("tests/") or "/tests/" in p,
     _Both(PYTHON, PYTEST_ASSERTS), "tests"),
    (lambda p: p.endswith(".py"), PYTHON, "software"),
    (lambda p: p.endswith("changelog.jsonl"), HISTORY, "history"),
]

FAMILY_OF_PREFIX = {
    "application/": "software", "workflow/": "workflow", "docs/": "docs",
    "data/": "spreadsheet", "tests/": "tests", "history/": "history",
}


def family_of(path: str) -> str:
    for prefix, family in FAMILY_OF_PREFIX.items():
        if path.startswith(prefix):
            return family
    return "unknown"


@dataclass
class Ingestion:
    """Everything one snapshot yielded, keyed for the layers above."""

    snapshot_id: str
    records: list = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def entities(self, of_type: str | None = None) -> list:
        return [
            r for r in self.records
            if r.kind == "entity" and (of_type is None or r.payload.get("type") == of_type)
        ]

    def relations(self, of_type: str | None = None) -> list:
        return [
            r for r in self.records
            if r.kind == "relation" and (of_type is None or r.payload.get("type") == of_type)
        ]


def ingest(snapshot, *, spreadsheet_reader=None, semantic_hints=None) -> Ingestion:
    """Read every artifact in a snapshot through the adapter that fits it.

    `spreadsheet_reader` is injected rather than imported: openpyxl reads a
    workbook from a byte stream, and keeping that at the boundary means this
    module holds no opinion about where bytes come from.

    `semantic_hints` is the project's own vocabulary -- the phrases ITS
    documents use for each rule. It belongs to the project and arrives from
    outside for the same reason: a product that shipped with one
    organisation's wording built in would work on that organisation and
    nowhere else.
    """
    result = Ingestion(snapshot_id=snapshot.snapshot_id)

    for path in sorted(_names(snapshot)):
        raw = snapshot.read(path)
        adapter = _adapter_for(path)

        if adapter is not None:
            result.records.extend(adapter.parse_bytes(raw, path, snapshot.snapshot_id))
        elif path.endswith(".md"):
            result.records.extend(POLICY_TEXT.extract_bytes(
                raw, path, snapshot.snapshot_id, semantic_hints))
        elif path.endswith(".xlsx") and spreadsheet_reader is not None:
            result.records.extend(spreadsheet_reader(raw, path, snapshot.snapshot_id))
        else:
            # Recorded, not ignored. A file nothing read is a hole in
            # coverage, and coverage a reader cannot see is coverage
            # nobody can question.
            result.skipped.append((path, "no adapter for this artifact type"))

    result.candidates = to_candidates(result.records)
    return result


def _names(snapshot) -> list[str]:
    baseline = getattr(snapshot, "_overlay", None)
    if baseline is not None:
        return list(baseline.baseline.names())
    return list(snapshot.names())


def _adapter_for(path: str):
    for matches, adapter, _family in _SOURCE_ADAPTERS:
        if matches(path):
            return adapter
    return None


# --- IR -> identity candidates ------------------------------------------------


def to_candidates(records: list) -> list[Candidate]:
    """Turn the facts that state a rule into identity candidates.

    A function definition or an import is a fact about structure, not a
    statement of a rule, and feeding it to identity resolution would give
    tier 2 a large pile of things to not match.

    The boundary is "states a rule", NOT "states a number". P3 originally
    drew it at numbers, and P4 found what that costs: a route saying
    `customer_tier == strategic -> approver_role: NONE` is a business rule
    that a procurement lead reads directly, and it produced no candidate at
    all, so the exemption nobody documented was invisible to the detector
    built to find exactly that. Non-numeric rules are where undocumented
    behaviour concentrates, because a number invites someone to write it
    down and a routing decision does not.
    """
    candidates: list[Candidate] = []

    for record in records:
        payload = record.payload
        kind = payload.get("type")
        provenance = record.provenance[0] if record.provenance else None
        artifact = (getattr(provenance, "file", None) or getattr(provenance, "doc", None) or "")
        locator = _locator(provenance)

        if kind == "threshold":
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family=family_of(artifact), locator=locator,
                value=payload.get("value"), unit=payload.get("unit", ""),
                field_name=payload.get("field", ""), op=payload.get("op", ""),
                explicit_rule_ids=_ids(payload.get("rule_id")),
                text=payload.get("quote", "")))

        elif kind == "predicate":
            #: A non-numeric route condition. The operative value is what the
            #: route DOES -- the approver role -- not the tier name it tests
            #: for: two routes both matching `== strategic` that send work to
            #: different approvers state different rules.
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family=family_of(artifact),
                locator=locator, value=payload.get("approver_role") or None,
                field_name=payload.get("field", ""), op=payload.get("op", ""),
                explicit_rule_ids=_ids(payload.get("rule_id")),
                text=payload.get("quote", "")))

        elif kind == "exception":
            #: An exception that names a handler and states no value has
            #: DELEGATED the rule rather than stated it. Value None is the
            #: honest reading, and it is what lets P4 classify the result as
            #: an existence_mismatch rather than inventing agreement.
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family=family_of(artifact),
                locator=locator,
                value=payload.get("value") if payload.get("states_value") else None,
                explicit_rule_ids=_ids(payload.get("rule_id")),
                text=payload.get("description", "")))

        elif kind == "rule_scope":
            #: A code site that applies a named rule under a guard. It states
            #: no value -- it reads one from config -- so its contribution is
            #: the SCOPE: who the rule is for at the place it takes effect.
            #: Without this candidate the restriction lives only in the
            #: source and never reaches identity resolution, which is how
            #: `category == "capex"` stayed invisible through all of P3.
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family=family_of(artifact),
                locator=locator, value=None,
                explicit_rule_ids=_ids(payload.get("rule_id")),
                scope=payload.get("scope", ""),
                scope_field=(payload.get("guards") or [{}])[0].get("field", ""),
                defers_to_config=True,
                text=payload.get("quote", "")))

        elif kind == "hardcoded_threshold":
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family=family_of(artifact), locator=locator,
                value=payload.get("value"), unit=payload.get("unit", ""),
                field_name=payload.get("field", ""), op=payload.get("op", ""),
                text=_quote(record)))

        elif kind == "doc_claim":
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family="docs", locator=locator,
                value=payload.get("value"), unit=payload.get("unit", ""),
                explicit_rule_ids=tuple(payload.get("explicit_rule_ids") or ()),
                semantic_hint=payload.get("semantic_hint", ""),
                text=payload.get("sentence", ""),
                observed_at=payload.get("last_reviewed") or None))

        elif kind == "spreadsheet_claim":
            candidates.append(Candidate(
                ref=record.id, artifact=artifact, family="spreadsheet", locator=locator,
                value=payload.get("value"), unit=payload.get("unit", ""),
                field_name=payload.get("field", ""), op=payload.get("op", ""),
                explicit_rule_ids=_ids(payload.get("rule_id")),
                semantic_hint=payload.get("semantic_hint", ""),
                text=_quote(record) or payload.get("rule_name", ""),
                observed_at=payload.get("last_reviewed") or None))

    candidates.sort(key=lambda c: c.ref)
    return _attach_scopes(candidates, records)


def _attach_scopes(candidates: list[Candidate], records: list) -> list[Candidate]:
    """Fold `rule_scope` facts onto the candidates of the same rule.

    A scope is emitted separately because it is found separately -- it lives
    on the enclosing `if`, not on the comparison -- but it belongs to the
    claim, because the question it answers is "when does THIS artifact apply
    the rule".

    Matched on artifact AND rule id: the same file can state two rules, and
    attaching one rule's guard to the other would invent a restriction.
    """
    scopes: dict[tuple[str, str], str] = {}
    for record in records:
        payload = record.payload
        if payload.get("type") != "rule_scope":
            continue
        provenance = record.provenance[0] if record.provenance else None
        artifact = getattr(provenance, "file", None) or ""
        rule_id = payload.get("rule_id", "")
        if artifact and rule_id:
            scopes[(artifact, rule_id)] = payload.get("scope", "")

    if not scopes:
        return candidates

    resolved = []
    for candidate in candidates:
        scope = next((scopes[(candidate.artifact, rule_id)]
                      for rule_id in candidate.explicit_rule_ids
                      if (candidate.artifact, rule_id) in scopes), "")
        resolved.append(replace(candidate, scope=scope) if scope else candidate)
    return resolved


def _ids(rule_id) -> tuple[str, ...]:
    return (rule_id,) if rule_id else ()


def _quote(record) -> str:
    """The artifact's own words, as the adapter read them.

    `Candidate.text` becomes the quote on every EvidenceRef downstream, so
    whatever is put here is what a reader is shown and what verification
    looks for. Two branches used to build a DESCRIPTION instead -- the
    spreadsheet branch passed the rule NAME, and the hardcoded branch passed
    the literal string "<function>: hardcoded literal", which appears in no
    source file anywhere.

    That is the fourth instance of one defect in this project: a sentence
    ABOUT a line offered as though it were the line. It survives review
    every time, because it is true. It fails verification every time,
    because an artifact cannot contain a description of itself.

    The spreadsheet one was the expensive one. It is the 500,000 side of the
    hero conflict, and it verified -- the rule name really is in the row --
    while showing a reader a cell with no number in it. The evidence view
    found it by trying to highlight the value and failing.
    """
    provenance = record.provenance[0] if record.provenance else None
    return (getattr(provenance, "quote", "") or "") if provenance else ""


def _locator(provenance) -> str:
    if provenance is None:
        return ""
    if getattr(provenance, "file", None):
        return f"L{provenance.line_start}"
    if getattr(provenance, "doc", None):
        return f"{provenance.sheet}!{provenance.row}"
    return ""
