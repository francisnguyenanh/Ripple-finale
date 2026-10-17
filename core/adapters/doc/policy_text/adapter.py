"""`policy_text` doc adapter — what a DOCUMENT says.

The distinction this adapter exists to keep, and the root of the product's
ability to find conflicts at all:

    RIGHT   RealityFact{kind: doc_claim, claim_text: "1,000,000 JPY",
                        semantic_hint: "approval threshold", provenance: {...}}
    WRONG   RealityFact{kind: rule, value: 1000000}

The first says "this document states X, here". The second says "X is true",
which the document has no authority to establish. Collapse the two and a
policy that disagrees with the running system becomes indistinguishable
from the system itself -- and the conflict the product sells disappears
into an averaging of opinions (I9).

Markdown and XLSX readers already exist in `core/adapters/doc/`. This adds
the extraction that is specific to POLICY prose: an amount, the sentence it
sits in, and a hint at which rule it is about.

Imports nothing from `core.kernel`.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any, Iterator

from core.adapters.base import DocAdapter, FileRef, IRRecord, Provenance

#: Written amounts, in the forms policy prose actually uses. Ordered longest
#: first so "1,000,000" is not matched as "000".
_AMOUNT = re.compile(
    r"(?P<currency>JPY|¥)?\s*(?P<digits>\d{1,3}(?:,\d{3})+|\d{4,})\s*(?P<trailing>JPY|yen)?",
    re.I)

#: Amounts spelled out. A policy that says "one million yen" states the same
#: rule as one that says 1,000,000, and a reader that only sees digits would
#: report a documented rule as undocumented.
_WORDED = [
    (re.compile(r"\bone million\b", re.I), 1_000_000),
    (re.compile(r"\ba million\b", re.I), 1_000_000),
    (re.compile(r"\bhalf a million\b", re.I), 500_000),
    (re.compile(r"\bfive million\b", re.I), 5_000_000),
]

#: Phrases that say which rule a sentence is about, as {semantic_id: phrases}.
#:
#: EMPTY BY DEFAULT, and that is the point. An earlier version hard-coded
#: this product's own demo vocabulary -- "approval_threshold",
#: "capex_finance_threshold" -- into the adapter, and a boundary test caught
#: it. A reader would have been right to ask whether the system works on
#: anything but its own seed.
#:
#: The vocabulary of a domain belongs to the PROJECT being examined, so it
#: arrives through `hints=`, the way a profile supplies anything else
#: project-specific. With none supplied, claims come back unhinted and
#: identity resolution falls to its later tiers -- a worse answer, honestly
#: arrived at.
HintMap = dict  # semantic_id -> tuple of phrases

_RULE_ID = re.compile_RULE_ID = re.compile(r"\bBR-\d{3}\b")


def _hint_for(sentence: str, hints: HintMap) -> str:
    lowered = sentence.lower()
    for semantic_id, phrases in hints.items():
        if any(phrase.lower() in lowered for phrase in phrases):
            return semantic_id
    return ""


#: Verbs that carry an obligation or a routing decision. A document covers a
#: rule when it STATES the rule, not when it mentions the topic.
_NORMATIVE = re.compile(
    r"\b(must|shall|require[sd]?|is required|referred to|routed to|"
    r"escalat(?:e|es|ed)|needs? (?:approval|sign-off)|sign-?off|"
    r"is exempt|are exempt|goes? to|is approved|are approved|"
    r"approves?|may not|cannot|is not permitted)\b",
    re.IGNORECASE)


def _is_normative(sentence: str) -> bool:
    """Does this line STATE a rule, or merely name its subject?

    The three things it rejects are the three that made the first version of
    the qualitative claim wrong, each in a different way:

        `## 6. Emergency purchases`          a heading names a topic
        `**Q. ...strategic account...?**`    a question asks about one
        `| Emergency | Fast-tracked |`       a table cell labels one

    Counting any of them as documentation would have made the two rules the
    seed marks as UNDOCUMENTED look documented -- and a detector that finds
    nothing because it is satisfied too easily is worse than one that finds
    too much, because nothing on screen invites no scrutiny.
    """
    stripped = sentence.strip()
    if not stripped or stripped.startswith("#") or "?" in stripped:
        return False
    if stripped.startswith("|") and not _NORMATIVE.search(stripped):
        return False
    return bool(_NORMATIVE.search(stripped))


def _sentence_at(lines: list[str], index: int) -> str:
    return lines[index].strip() if 0 <= index < len(lines) else ""


def _hint_context(lines: list[str], index: int) -> str:
    """The sentence, its nearest preceding heading, and the line before it.

    Policy prose does not repeat its subject in every sentence. The FAQ
    answers "How much is small?" with "The figure is one million yen", and
    the manager's guide puts "What lands in your queue" in a heading two
    lines above the amount. Reading one line in isolation reports both as
    amounts about nothing -- and an unhinted claim is one the identity layer
    has to fall back to tier 3 for, which is the tier we most want to avoid.

    Still only a hint: it widens what may be CONSIDERED, never what is
    concluded.
    """
    parts = [_sentence_at(lines, index)]
    for back in range(index - 1, max(-1, index - 4), -1):
        line = _sentence_at(lines, back)
        if line:
            parts.append(line)
            break
    for back in range(index - 1, max(-1, index - 40), -1):
        line = _sentence_at(lines, back)
        if line.startswith("#") or line.startswith("**Q."):
            parts.append(line)
            break
    return " ".join(parts)


class PolicyTextAdapter(DocAdapter):
    name = "policy_text"
    formats = ["md"]

    def discover(self, paths: list[str], options: dict) -> Iterator[FileRef]:
        patterns = (options or {}).get("patterns") or ["**/*.md"]
        for root in paths:
            for pattern in patterns:
                for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
                    if os.path.isfile(path):
                        yield FileRef(path=path, size=os.path.getsize(path))

    def extract(self, file_ref: FileRef, targets: list[str]) -> Iterator[IRRecord]:
        with open(file_ref.path, "rb") as f:
            yield from self.extract_bytes(f.read(), file_ref.path, "")

    def extract_bytes(self, raw: bytes, path: str, snapshot_id: str,
                      hints: HintMap | None = None) -> Iterator[IRRecord]:
        hints = dict(hints or {})
        lines = raw.decode("utf-8").splitlines()
        document_id = f"doc:{path}"
        seen: set[tuple[int, int]] = set()
        #: Rules this document has already been seen to cover qualitatively.
        covered: set[str] = set()

        reviewed = ""
        for line in lines[:20]:
            match = re.search(r"\*\*Last reviewed:\*\*\s*(\S+)", line)
            if match:
                reviewed = match.group(1)
                break

        yield IRRecord(
            kind="entity", id=document_id,
            payload={"type": "document", "path": path, "last_reviewed": reviewed,
                     "line_count": len(lines)},
            provenance=(Provenance(doc=path, sheet="body", row=1, method="parser"),))

        for index, line in enumerate(lines):
            sentence = _sentence_at(lines, index)
            if not sentence or sentence.startswith("<!--"):
                continue

            # The sentence outranks the context. The SOP's capex row sits
            # under a heading called "Approval thresholds", so a hint drawn
            # from the widened context labels a Finance rule as the manager
            # approval rule -- and mis-grouping two rules is the failure R5
            # names as the worst in the phase: every impact downstream of a
            # wrong group is wrong. Context is the fallback, never the
            # override.
            context = _hint_context(lines, index)

            # A document can cover a rule without stating a number. The
            # policy says high-risk suppliers go to the Operations Director
            # and names no amount, which is a complete statement of BR-005.
            # Extracting only amounts made that sentence invisible and the
            # rule it documents look UNDOCUMENTED -- a false positive, and
            # the kind that costs most: precision outranks recall here
            # because a missed group is visible to a reader and a wrong one
            # is not.
            #
            # Deliberately narrow. The hint must be in the SENTENCE, never
            # the surrounding context, and one claim per document per rule
            # is enough: the question a qualitative claim answers is "does
            # this document cover this rule at all", not "what does it say".
            qualitative = _hint_for(sentence, hints)
            if (qualitative and qualitative not in covered
                    and not self._claims_in(sentence)
                    and _is_normative(sentence)):
                covered.add(qualitative)
                explicit = _RULE_ID.findall(sentence)
                yield IRRecord(
                    kind="entity",
                    id=f"{document_id}:claim:L{index + 1}:{qualitative}",
                    payload={
                        "type": "doc_claim",
                        "claim_text": sentence[:200],
                        "value": None,
                        "unit": "",
                        "semantic_hint": qualitative,
                        "explicit_rule_ids": explicit,
                        "sentence": sentence[:300],
                        "document": path,
                        "last_reviewed": reviewed,
                        #: States coverage, not a value. Downstream stages must not read
                        #: this as agreement with anything.
                        "qualitative": True,
                    },
                    provenance=(Provenance(doc=path, sheet="body", row=index + 1,
                                           method="parser", quote=sentence[:200]),))

            # A normative sentence that attaches to NOTHING -- no amount,
            # no hint for any known rule. Until now it produced no record at
            # all, which made an entire class of finding structurally
            # impossible: a rule that exists only in a document.
            #
            # UN-004 is exactly that. The SOP says a request escalates after
            # two working days and nothing implements it, and the detector
            # built to find rules nobody wrote down cannot see a rule nobody
            # built. `detect_undocumented` has said in its own docstring
            # that the two are mirrors; this is the half that
            # was missing.
            #
            # Emitted with NO semantic_id, deliberately. Inventing one would
            # manufacture identity, and two sentences that happen to share
            # words are not thereby the same rule. A person names it.
            if (_is_normative(sentence)
                    and not self._claims_in(sentence)
                    and not _hint_for(sentence, hints)):
                yield IRRecord(
                    kind="entity",
                    id=f"{document_id}:unattached:L{index + 1}",
                    payload={
                        "type": "unattached_rule",
                        "sentence": sentence[:300],
                        "document": path,
                        "last_reviewed": reviewed,
                    },
                    provenance=(Provenance(doc=path, sheet="body", row=index + 1,
                                           method="parser", quote=sentence[:200]),))

            for claim in self._claims_in(sentence):
                value, quote = claim
                if (index, value) in seen:
                    continue
                seen.add((index, value))

                explicit = _RULE_ID.findall(sentence)
                yield IRRecord(
                    kind="entity", id=f"{document_id}:claim:L{index + 1}:{value}",
                    payload={
                        "type": "doc_claim",
                        "claim_text": quote,
                        "value": value,
                        "unit": "JPY",
                        #: A hint, never a decision. Tier 3 of identity
                        #: resolution may use it; tier 1 outranks it.
                        "semantic_hint": _hint_for(sentence, hints) or _hint_for(context, hints),
                        "explicit_rule_ids": explicit,
                        "sentence": sentence[:300],
                        "document": path,
                        "last_reviewed": reviewed,
                    },
                    provenance=(Provenance(doc=path, sheet="body", row=index + 1,
                                           method="parser", quote=sentence[:200]),))

                yield IRRecord(
                    kind="relation", id=f"{document_id}:documents:L{index + 1}:{value}",
                    payload={
                        "src": f"{document_id}:claim:L{index + 1}:{value}",
                        "type": "DOCUMENTS",
                        "dst": f"rule:{explicit[0] if explicit else _hint_for(sentence, hints) or _hint_for(context, hints) or 'unknown'}",
                        "reason": f"{os.path.basename(path)} line {index + 1} states {value:,} JPY",
                    },
                    provenance=(Provenance(doc=path, sheet="body", row=index + 1,
                                           method="parser", quote=sentence[:200]),))

    @staticmethod
    def _claims_in(sentence: str) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []

        for match in _AMOUNT.finditer(sentence):
            digits = match.group("digits")
            if not (match.group("currency") or match.group("trailing")
                    or "," in digits or len(digits) >= 6):
                # A bare short number in prose is a section number, a count
                # of days or a version -- not an amount.
                continue
            value = int(digits.replace(",", ""))
            if value >= 1000:
                found.append((value, match.group(0).strip()))

        for pattern, value in _WORDED:
            match = pattern.search(sentence)
            if match and not any(v == value for v, _ in found):
                found.append((value, match.group(0)))

        return found


ADAPTER = PolicyTextAdapter()
