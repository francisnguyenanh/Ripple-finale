# -*- coding: utf-8 -*-
"""Documents are RENDERED from knowledge, never written by a model. I9.

    RIGHT   intent -> Knowledge Model update (evidence, lineage) -> render
    WRONG   intent -> a model writes a paragraph -> write it to the file

The shortcut is genuinely tempting: a language model writes a better
paragraph than a template does, and it takes an afternoon less. What it
costs is the only property that makes the documentation trustworthy -- that
every sentence in it can be traced to a record with evidence behind it.

A rendered document is a VIEW. Ask why a line says what it says and the
answer is a knowledge record and the quote it was promoted from. Ask the
same of a generated paragraph and the answer is that a model produced it,
which is not an answer. Once one paragraph in a file is untraceable, a
reader has to treat every paragraph in it as untraceable, because nothing
on the page says which is which.

So the order is fixed and the direction is one-way:

**The Knowledge Model is updated first**, with a superseding record that
carries evidence and points at what it replaced. The old record is not
deleted -- it becomes SUPERSEDED, so "what did this say before, and why did
it change" stays answerable.

**The document is then rendered deterministically** from the model. The same
model renders the same bytes, which is also what makes the P8 check
possible: re-render and compare, and any difference is a real divergence
rather than a model on a different day.

A model may still help -- suggesting wording for a statement that a person
then approves into the Knowledge Model. That path goes THROUGH the model
with evidence attached, and it is the going-through that matters.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.knowledge import KnowledgeRecord
from core.change_intelligence.states import KnowledgeState


class UntracedSentence(Exception):
    """Something tried to put text into a document with no record behind it."""


@dataclass(frozen=True)
class KnowledgeUpdate:
    """One superseding record, and the one it replaces.

    Both halves, because "what it says now" without "what it said before"
    cannot answer the question a reader of a changed document actually has.
    """

    superseded: KnowledgeRecord
    replacement: KnowledgeRecord
    actor: str
    timestamp: str
    reason: str

    @property
    def semantic_id(self) -> str:
        return self.replacement.semantic_id


@dataclass
class RenderedDocument:
    artifact: str
    text: str
    from_records: tuple = ()
    notes: list = field(default_factory=list)

    @property
    def is_traceable(self) -> bool:
        return bool(self.from_records)


def update_knowledge(model, *, semantic_id: str, new_value, statement: str,
                     evidence, actor: str, timestamp: str,
                     reason: str) -> KnowledgeUpdate:
    """Supersede the record that states the old value. Nothing is deleted.

    Raises if there is no record to supersede: proposing a documentation
    change for a rule the Knowledge Model has never heard of means the
    proposal was built from something other than the model, and that is
    exactly the path this module exists to close.
    """
    if not actor or not timestamp:
        raise UntracedSentence(
            "a knowledge update carries who made it and when (I12)")
    if not evidence:
        raise UntracedSentence(
            f"{semantic_id} would be restated with no evidence; a knowledge "
            f"record without a quotable line is an assertion (I4)")

    existing = [r for r in model.by_semantic_id(semantic_id)
                if r.state is not KnowledgeState.SUPERSEDED]
    if not existing:
        raise UntracedSentence(
            f"the Knowledge Model holds no live record for {semantic_id}; "
            f"a document change must come FROM the model, so there is "
            f"nothing here to render from")

    old = existing[0]
    superseded = KnowledgeRecord(
        id=old.id, knowledge_type=old.knowledge_type,
        semantic_id=old.semantic_id, statement=old.statement,
        evidence=old.evidence, derived_from=old.derived_from,
        state=KnowledgeState.SUPERSEDED, alignment=old.alignment,
        representations=old.representations, gap_id=old.gap_id,
        reason=f"superseded by {actor} at {timestamp}: {reason}",
        km_version=old.km_version)

    replacement = KnowledgeRecord(
        id=f"{old.id}+{old.km_version + 1}",
        knowledge_type=old.knowledge_type,
        semantic_id=semantic_id,
        statement=statement,
        evidence=tuple(evidence),
        #: Lineage: the new record names the one it replaces, so the chain
        #: back to the original evidence survives any number of revisions.
        derived_from=tuple(old.derived_from) + (old.id,),
        state=KnowledgeState.PROPOSED,
        alignment=old.alignment,
        representations=old.representations,
        gap_id=old.gap_id,
        reason=reason,
        km_version=old.km_version + 1)

    _ = new_value
    return KnowledgeUpdate(superseded=superseded, replacement=replacement,
                           actor=actor, timestamp=timestamp, reason=reason)


def commit_update(model, update: KnowledgeUpdate):
    """Put both records into the model. Returns the model, mutated in place.

    The superseded record stays. A model that dropped it would make the
    rendered document's history unrecoverable from the model itself.
    """
    model.records = [r for r in model.records
                     if r.id != update.superseded.id]
    model.records.append(update.superseded)
    model.records.append(update.replacement)
    return model


# --- rendering ---------------------------------------------------------------


def render_section(record: KnowledgeRecord) -> str:
    """One record as documentation. No sentence here has no record behind it."""
    lines = [f"## {record.semantic_id}", "", record.statement, ""]
    if record.evidence:
        lines.append("Source:")
        for ref in record.evidence:
            locator = f":{ref.locator}" if ref.locator else ""
            lines.append(f"- `{ref.artifact}{locator}` — {ref.quote}")
        lines.append("")
    return "\n".join(lines)


def render_document(model, *, artifact: str, title: str,
                    semantic_ids=None) -> RenderedDocument:
    """A document, deterministically, from live knowledge records.

    Ordered by `semantic_id`, not by model order, so two runs over the same
    knowledge produce the same bytes. Without that, the P8 check "re-render
    and compare" reports a difference every time and stops being read.
    """
    live = [r for r in model.records
            if r.state is not KnowledgeState.SUPERSEDED]
    if semantic_ids is not None:
        wanted = set(semantic_ids)
        live = [r for r in live if r.semantic_id in wanted]
    live.sort(key=lambda r: (r.semantic_id, r.id))

    body = [f"# {title}", ""]
    notes: list = []
    for record in live:
        if not record.evidence:
            notes.append(
                f"{record.semantic_id} has no evidence and was not rendered; "
                f"an unsourced line in a document is indistinguishable from "
                f"an invented one")
            continue
        body.append(render_section(record))

    return RenderedDocument(
        artifact=artifact, text="\n".join(body).rstrip() + "\n",
        from_records=tuple(r.id for r in live if r.evidence), notes=notes)


def proposed_document_edit(model, *, artifact: str, title: str,
                           current_text: str, semantic_ids=None):
    """The document change as a before/after, with the records behind it.

    Returned as text rather than a structured edit on purpose: a document
    is prose, and there is no path inside it. What makes this reviewable is
    not a path but the record list -- a reviewer can ask of any line which
    record produced it.
    """
    rendered = render_document(model, artifact=artifact, title=title,
                               semantic_ids=semantic_ids)
    return rendered, (rendered.text != current_text)


def evidence_for_value(artifact: str, locator: str, quote: str,
                       snapshot_id: str = "") -> EvidenceRef:
    return EvidenceRef(artifact=artifact, locator=locator, quote=quote,
                       snapshot_id=snapshot_id)


__all__ = [
    "KnowledgeUpdate",
    "RenderedDocument",
    "UntracedSentence",
    "commit_update",
    "evidence_for_value",
    "proposed_document_edit",
    "render_document",
    "render_section",
    "update_knowledge",
]
