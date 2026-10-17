"""`history` adapter — change history as the sixth source of evidence.

This is what turns a finding from

    approval_routes.yaml disagrees with approval_matrix.xlsx

into

    the spreadsheet was changed in June 2022 and nobody synchronised the
    workflow; the strategic-customer exemption was added in November 2020
    as a temporary measure and there is no revert

The first is a diff. The second is a decision somebody forgot. Half a day
of work for the entire emotional weight of the demo, which is why this
adapter is not cut.

Reads `changelog.jsonl` -- declared history rather than an excavated git
log. See the seed's own HISTORY.md for why, and README §21, because that
is a limitation to state rather than let a judge discover.

Imports nothing from `core.kernel`.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Iterator

from core.adapters.base import FileRef, IRRecord, Provenance, SourceAdapter

#: A change whose note says it was meant to be undone. The single most
#: valuable thing this adapter extracts: an intention that was recorded and
#: then not carried out.
#: "campaign" was in this list and produced a false positive: the note
#: "campaign closed; routing left in place" is the OPPOSITE of a temporary
#: declaration -- it records that something ended and nothing was undone.
#: A marker that fires on the word rather than the claim would put two
#: findings where there is one, and on a screen showing five findings that
#: is a 20% precision loss for nothing.
_TEMPORARY_MARKERS = ("temporary", "revert", "provisional", "interim", "for now")

#: A change that touched one representation of a rule and said, in writing,
#: that the others were somebody else's problem.
_UNSYNCHRONISED_MARKERS = ("separately", "not reviewed", "to be updated",
                           "left as-is", "left in place", "no follow-up")


class HistoryAdapter(SourceAdapter):
    name = "history"
    languages = ["jsonl"]

    def discover(self, paths: list[str], options: dict) -> Iterator[FileRef]:
        patterns = options.get("patterns") or ["**/changelog.jsonl"]
        for root in paths:
            for pattern in patterns:
                for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
                    if os.path.isfile(path):
                        yield FileRef(path=path, size=os.path.getsize(path))

    def parse(self, file_ref: FileRef, **kwargs: Any) -> Iterator[IRRecord]:
        raw = kwargs.get("content")
        if raw is None:
            with open(file_ref.path, "rb") as f:
                raw = f.read()
        yield from self.parse_bytes(raw, file_ref.path, kwargs.get("snapshot_id", ""))

    def parse_bytes(self, raw: bytes, path: str, snapshot_id: str) -> Iterator[IRRecord]:
        lines = raw.decode("utf-8").splitlines()
        changes = []
        for number, line in enumerate(lines, 1):
            if line.strip():
                changes.append((number, json.loads(line)))

        def prov(line: int, quote: str = "") -> Provenance:
            return Provenance(file=path, line_start=line, line_end=line,
                              method="parser", snapshot_id=snapshot_id or None,
                              quote=quote[:200])

        #: semantic_id -> the changes that touched it, so "who last moved
        #: this rule, and did anyone move the rest" is answerable.
        touched: dict[str, list[dict]] = {}

        for line_number, change in changes:
            change_id = change["change_id"]
            note = (change.get("note") or "").lower()
            summary = change.get("summary", "")

            yield IRRecord(
                kind="entity", id=f"history:{change_id}",
                payload={
                    "type": "change_event",
                    "change_id": change_id,
                    "date": change["date"],
                    "author": change["author"],
                    "kind": change.get("kind", ""),
                    "summary": summary,
                    "note": change.get("note", ""),
                    "artifacts": list(change.get("artifacts") or []),
                    "semantic_ids": list(change.get("semantic_ids") or []),
                    #: Both flags are FACTS about the text of the note, not
                    #: inferences about intent. The adapter reports that the
                    #: author wrote "temporary"; deciding what follows is the
                    #: knowledge layer's job (I1, I3).
                    "declares_temporary": any(m in note for m in _TEMPORARY_MARKERS),
                    "declares_unsynchronised": any(m in note for m in _UNSYNCHRONISED_MARKERS),
                    "reverted_by": change.get("reverted_by"),
                },
                provenance=(prov(line_number, summary),))

            for artifact in change.get("artifacts") or []:
                yield IRRecord(
                    kind="relation", id=f"history:{change_id}:changed:{artifact}",
                    payload={"src": f"history:{change_id}", "type": "WRITES",
                             "dst": f"artifact:{artifact}",
                             "date": change["date"],
                             "reason": f"{change['date']} {change['author']}: {summary}"},
                    provenance=(prov(line_number, summary),))

            for semantic_id in change.get("semantic_ids") or []:
                touched.setdefault(semantic_id, []).append(
                    {**change, "_line": line_number})
                yield IRRecord(
                    kind="relation", id=f"history:{change_id}:affects:{semantic_id}",
                    payload={"src": f"history:{change_id}", "type": "SUPERSEDES",
                             "dst": f"rule:{semantic_id}", "date": change["date"],
                             "reason": summary},
                    provenance=(prov(line_number, summary),))

        yield from self._unreverted(touched, prov)

    # --- the records that carry the story --------------------------------------

    def _unreverted(self, touched: dict[str, list[dict]], prov) -> Iterator[IRRecord]:
        """A change declared temporary with no revert, per rule.

        Derived by comparing change records, which is arithmetic over facts,
        not interpretation: "this record says temporary and no record
        reverts it" is checkable. What it MEANS is left to the knowledge
        layer.
        """
        for semantic_id, changes in sorted(touched.items()):
            reverted = {c.get("reverted_by") for c in changes if c.get("reverted_by")}
            for change in changes:
                note = (change.get("note") or "").lower()
                if not any(m in note for m in _TEMPORARY_MARKERS):
                    continue
                if change["change_id"] in reverted:
                    continue
                yield IRRecord(
                    kind="entity",
                    id=f"history:unreverted:{change['change_id']}:{semantic_id}",
                    payload={
                        "type": "unreverted_temporary_change",
                        "change_id": change["change_id"],
                        "date": change["date"],
                        "author": change["author"],
                        "semantic_id": semantic_id,
                        "note": change.get("note", ""),
                        "later_changes": [
                            c["change_id"] for c in changes if c["date"] > change["date"]
                        ],
                    },
                    provenance=(prov(change["_line"], change.get("note", "")),))

    @staticmethod
    def last_change_for(records: list[IRRecord], semantic_id: str) -> dict | None:
        """The most recent change touching a rule.

        This is the recency marker that decides DRIFT against CONFLICT
        (P1-T05). Where history cannot order two representations, the
        product is required to report CONFLICT rather than guess -- so a
        `None` here is a real answer, not a gap in the adapter.
        """
        events = [
            r.payload for r in records
            if r.payload.get("type") == "change_event"
            and semantic_id in r.payload.get("semantic_ids", [])
        ]
        return max(events, key=lambda e: e["date"]) if events else None


ADAPTER = HistoryAdapter()
