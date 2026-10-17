# -*- coding: utf-8 -*-
"""One finding, its whole evidence chain, side by side.

    business claim | implementation | policy doc | spreadsheet/workflow | test | history

Six columns, because a vertical list of nine quotes makes a reader hold the
comparison in their head, and holding a comparison in your head is exactly
the failure the product exists to fix.

Three rules the columns obey, and each of them is the whole reason this is a
module instead of a template:

**An empty cell says why it is empty.** A blank under `test` could mean "no
test asserts this rule", or "no test file was read", or "a test asserts it
and the value did not match". Those are three different facts about the
enterprise, one of them is a finding, one is a bug in this product -- and
rendered as whitespace they are indistinguishable. Every cell that has no
evidence carries `absent_because`.

**`test` and `history` corroborate; they never vote.** A test is evidence
ABOUT intent, not a statement of policy (`discovery.CORROBORATING_FAMILIES`).
If a green test could document a rule then UN-003 -- behaviour a test asserts
and no document describes -- would cease to be a finding the moment the test
column was filled in. The cells are marked `corroborating=True` and nothing
downstream is allowed to read them as claims.

**A highlight that cannot be found is reported, not faked.** Highlighting is
how a reader's eye lands on the number, so a cell that silently highlights
nothing teaches them the highlight means nothing. Where the value is not
literally in the quote -- a qualitative document claim, a config lookup --
the cell says so, and that absence is itself informative: it is the
difference between a document that states the number and one that defers to
wherever the number lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Column order is the reading order, left to right:
#: what the business believes, what runs, what is written down, what the
#: operational artifacts carry, what is asserted, what happened.
COLUMNS = ("business claim", "implementation", "policy doc",
           "spreadsheet/workflow", "test", "history")

#: family -> column. `tests` and `history` are filled from records rather
#: than from claims, so they are absent here on purpose.
_FAMILY_COLUMN = {
    "software": "implementation",
    "docs": "policy doc",
    "spreadsheet": "spreadsheet/workflow",
    "workflow": "spreadsheet/workflow",
}


@dataclass(frozen=True)
class Highlight:
    """Where the value sits inside the quote, or why it does not."""

    start: int = -1
    end: int = -1
    absent_because: str = ""

    @property
    def found(self) -> bool:
        return self.start >= 0


@dataclass(frozen=True)
class EvidenceCell:
    artifact: str
    locator: str
    quote: str
    value: object = None
    last_changed: str | None = None
    highlight: Highlight = field(default_factory=Highlight)
    corroborating: bool = False
    note: str = ""

    @property
    def link(self) -> str:
        """What a reader clicks. `artifact#locator`, so it survives being
        copied into a ticket -- a link that only works inside the app is a
        link nobody can forward."""
        return f"{self.artifact}#{self.locator}" if self.locator else self.artifact


@dataclass(frozen=True)
class EvidenceColumn:
    name: str
    cells: tuple[EvidenceCell, ...] = ()
    absent_because: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.cells


@dataclass(frozen=True)
class EvidenceView:
    finding_id: str
    semantic_id: str
    columns: tuple[EvidenceColumn, ...]

    def column(self, name: str) -> EvidenceColumn:
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    @property
    def empty_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_empty)


# --- highlighting --------------------------------------------------------------


def _forms(value) -> list[str]:
    """Every way one value is written by a human being.

    `1000000`, `1,000,000` and `1_000_000` are the same number to everyone
    except a substring search, and the third one is how it appears in the
    Python that implements the rule. Missing it would leave the
    implementation column -- the column a reviewer trusts most -- as the one
    with no highlight.
    """
    forms = [str(value)]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        whole = int(value)
        forms += [f"{whole:,}", f"{whole:,}".replace(",", "_"), f"{whole:,}".replace(",", " ")]
    return forms


def locate_value(quote: str, value) -> Highlight:
    """The span a renderer should mark, or a reason there is none."""
    if value is None:
        return Highlight(absent_because="this artifact states no value for the rule")
    for form in _forms(value):
        index = quote.find(form)
        if index >= 0:
            return Highlight(start=index, end=index + len(form))
    return Highlight(absent_because=(
        f"the quote does not write {value} out; the artifact states the rule "
        f"without stating the number"))


# --- building ------------------------------------------------------------------


def _business_claim_cell(finding) -> EvidenceCell:
    """The row header: what is being claimed, in the product's own words.

    Carries no artifact, because it is not evidence -- it is the question
    the other five columns answer. Giving it a fake artifact would make it
    citable, and a claim the product made about itself must never be
    quotable as though an artifact said it.
    """
    values = sorted({str(c.value) for c in finding.claims if c.value is not None})
    stated = " / ".join(values) if values else "no value stated anywhere"
    return EvidenceCell(
        artifact="",
        locator="",
        quote=f"{finding.semantic_id}: {stated}",
        note=(f"{finding.kind}"
              + (f" ({finding.conflict_type})" if finding.conflict_type else "")))


def _test_cells(finding, records) -> tuple[tuple[EvidenceCell, ...], str]:
    """Assertions that mention one of the finding's values.

    Matched on VALUE rather than on rule id, because the assertion that
    matters most here does not name a rule: a test written as a bare literal
    is precisely the one that will not notice when the policy moves.
    """
    if records is None:
        return (), ("no test records were supplied; this is missing coverage in "
                    "the reader, not an absence of tests")

    wanted = {c.value for c in finding.claims if c.value is not None}
    cells = []
    for record in records:
        if record.kind != "test_assertion":
            continue
        values = [v for v in record.payload.get("values", []) if v in wanted]
        if not values:
            continue
        provenance = record.provenance[0]
        quote = provenance.quote or ""
        markers = record.payload.get("markers", ())
        note = record.payload["test_name"]
        if record.payload.get("reads_the_value"):
            note += " (reads the value from configuration)"
        if markers:
            note += f" [marked {', '.join(markers)}]"
        cells.append(EvidenceCell(
            artifact=provenance.file or "",
            locator=f"L{provenance.line_start}",
            quote=quote,
            value=values[0],
            highlight=locate_value(quote, values[0]),
            corroborating=True,
            note=note))

    cells.sort(key=lambda c: (c.artifact, c.locator))
    if cells:
        return tuple(cells), ""
    return (), ("no test asserts any of these values -- the behaviour runs "
                "unguarded, so nothing will go red when it changes")


def _history_cells(finding, records) -> tuple[tuple[EvidenceCell, ...], str]:
    """Changes that declared themselves to be about this rule.

    Declared history, not an excavated git log (debt H1, README §21). The
    note on each cell says so, because a reader who assumes these are
    commits will trust the dates more than they deserve.
    """
    if records is None:
        return (), ("no history records were supplied; this is missing "
                    "coverage in the reader, not an absence of history")

    cells = []
    for record in records:
        if record.payload.get("type") != "change_event":
            continue
        if finding.semantic_id not in (record.payload.get("semantic_ids") or []):
            continue
        provenance = record.provenance[0]
        payload = record.payload
        flags = []
        if payload.get("declares_temporary"):
            flags.append("declared temporary")
        if payload.get("declares_unsynchronised"):
            flags.append("declared unsynchronised")
        note = f"{payload['author']} -- {payload['kind']}"
        if flags:
            note += f" [{', '.join(flags)}]"
        if payload.get("note"):
            note += f": {payload['note']}"
        cells.append(EvidenceCell(
            artifact=provenance.file or "history/changelog.jsonl",
            locator=payload["change_id"],
            quote=payload["summary"],
            last_changed=payload["date"],
            corroborating=True,
            note=note))

    cells.sort(key=lambda c: (c.last_changed or "", c.locator))
    if cells:
        return tuple(cells), ""
    return (), "no recorded change declares itself to be about this rule"


def build_evidence_view(finding, *, records=None) -> EvidenceView:
    """Assemble the six columns for one finding.

    `records` is the ingestion's record list, used only for the two
    corroborating columns. Passing None leaves them empty WITH a reason that
    blames the reader rather than the enterprise -- the distinction a blank
    cell destroys.
    """
    by_column: dict[str, list[EvidenceCell]] = {name: [] for name in COLUMNS}
    unmapped: list[str] = []

    for claim in sorted(finding.claims, key=lambda c: (c.artifact, c.locator)):
        column = _FAMILY_COLUMN.get(claim.family)
        if column is None:
            # A family with no column is a hole in this view, and it says so
            # rather than dropping the claim: evidence that exists and is not
            # displayed is worse than evidence that is missing.
            unmapped.append(f"{claim.artifact} ({claim.family})")
            continue
        quote = claim.evidence.quote or ""
        by_column[column].append(EvidenceCell(
            artifact=claim.artifact,
            locator=claim.locator,
            quote=quote,
            value=claim.value,
            last_changed=claim.last_changed,
            highlight=locate_value(quote, claim.value)))

    by_column["business claim"] = [_business_claim_cell(finding)]

    test_cells, test_reason = _test_cells(finding, records)
    history_cells, history_reason = _history_cells(finding, records)

    reasons = {
        "test": test_reason,
        "history": history_reason,
        "implementation": "no source file states this rule",
        "policy doc": "no document states this rule -- it runs undocumented",
        "spreadsheet/workflow": "no spreadsheet or workflow carries this rule",
        "business claim": "",
    }

    columns = []
    for name in COLUMNS:
        cells = tuple(test_cells if name == "test"
                      else history_cells if name == "history"
                      else by_column[name])
        columns.append(EvidenceColumn(
            name=name,
            cells=cells,
            absent_because="" if cells else reasons[name]))

    view = EvidenceView(finding_id=finding.finding_id,
                        semantic_id=finding.semantic_id,
                        columns=tuple(columns))
    if unmapped:
        raise UnrepresentedEvidence(
            f"{finding.finding_id}: no column for " + ", ".join(sorted(unmapped)))
    return view


class UnrepresentedEvidence(Exception):
    """A claim the view has nowhere to put.

    Raised rather than logged. A six-column view that silently drops a
    seventh family shows a reader a complete-looking table that is not
    complete, and there is no worse failure available to this module.
    """


# --- rendering -----------------------------------------------------------------


def render(view: EvidenceView, *, width: int = 78) -> str:
    """A text rendering, so the view is checkable without a browser.

    The UI at P9 renders the same structure. This exists because a table
    whose only renderer is a web page can only be reviewed by looking at it.
    """
    out = [f"{view.finding_id} -- {view.semantic_id}", "=" * len(view.finding_id)]
    for column in view.columns:
        out.append("")
        out.append(f"[{column.name.upper()}]")
        if column.is_empty:
            out.append(f"  (empty) {column.absent_because}")
            continue
        for cell in column.cells:
            head = cell.link or "(the claim under examination)"
            when = f"  last changed {cell.last_changed}" if cell.last_changed else ""
            out.append(f"  {head}{when}")
            for line in _wrap(cell.quote, width - 6):
                out.append(f"      {line}")
            if cell.highlight.absent_because:
                out.append(f"      ^ {cell.highlight.absent_because}")
            elif cell.highlight.found:
                out.append(f"      ^ highlights {cell.quote[cell.highlight.start:cell.highlight.end]}")
            if cell.note:
                out.append(f"      note: {cell.note}")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    lines = []
    for raw in text.splitlines() or [""]:
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            cut = cut if cut > 0 else width
            lines.append(raw[:cut])
            raw = raw[cut:].lstrip()
        lines.append(raw)
    return lines
