# -*- coding: utf-8 -*-
"""A sentence becomes a strict record, or it becomes a question.

    "Orders above JPY 500,000 should require manager approval."
      -> {intent_type: business_rule_change, target: approval_threshold,
          desired_value: 500000 JPY, ...}

    "It should be stricter."
      -> desired_value UNKNOWN, and the question that would resolve it

**Abstaining is the correct answer, not a failure.** This is the module
where a model is most tempted to be helpful: asked for a number and given a
sentence with no number in it, it will produce one. So the gate here does
not ask the model how confident it is -- it reads the SENTENCE and refuses
any `desired_value` the sentence does not determine.

Same shape as the grounding check in `explain.py`, for the same reason: a
constraint the model is asked to respect is one the model may ignore, and
neither can be tested offline. A constraint applied to the model's output
can be.

**This module writes nothing and imports no store.** This is checkable with a
grep. An extractor that could persist would make a parsed guess durable
before anybody validated it, and validation is done in intent.

I11: the model arrives as an argument. With no proposer, extraction still
runs -- the script path handles every determinate sentence in the seed, and
the model's job is the sentences a regular expression should not be trusted
with.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

UNKNOWN = "UNKNOWN"

#: The intent types the product acts on. `rule_removal` is separate from a
#: value change on purpose: "drop this rule" and "set it to zero" produce
#: completely different proposals, and a parser that collapsed them would
#: turn a deletion into an edit.
INTENT_TYPES = (
    "business_rule_change",
    "rule_removal",
    "scope_change",
    "unknown",
)

#: Words that ask for a direction without naming a destination. The most
#: common shape of an unactionable request, and the one a model will happily
#: invent a number for.
#:
#: `shorten` and `extend` were added after the split test: "lower the
#: threshold and also shorten the deadline" is TWO requests, and with only
#: the money verbs in this list the second half did not read as an ask, so
#: the two were silently merged into one.
_DIRECTIONAL = re.compile(
    r"\b(stricter|tighter|looser|lower|higher|reduce[d]?|increase[d]?|"
    r"relax(?:ed)?|tighten(?:ed)?|shorten(?:ed)?|lengthen(?:ed)?|"
    r"extend(?:ed)?|shrink|widen|narrow(?:ed)?|raise[d]?|"
    r"more|less|faster|sooner|later)\b", re.IGNORECASE)

_REMOVAL = re.compile(
    r"\b(remove|delete|drop|abolish|retire|scrap|get rid of|no longer apply)\b",
    re.IGNORECASE)

_CURRENCY = r"(?:JPY|USD|EUR|GBP|yen|¥|\$)"
_AMOUNT_WITH_UNIT = re.compile(
    rf"(?:(?P<pre>{_CURRENCY})\s*(?P<n1>[\d][\d,_]*)"
    rf"|(?P<n2>[\d][\d,_]*)\s*(?P<post>{_CURRENCY}))",
    re.IGNORECASE)

_BARE_NUMBER = re.compile(r"\b(?P<n>[\d][\d,_]*)\b")

#: Two requests joined into one sentence. Splitting them is not cosmetic:
#: one may be valid and the other not, and a merged intent forces a person
#: to accept or reject both together -- which is how the invalid half gets
#: approved.
_CONJUNCTION = re.compile(r"\b(?:and also|and the|as well as|;)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Question:
    """What to ask when the sentence does not determine an answer.

    Carried on the result rather than logged, because "the system could not
    tell" is only useful to a person if it arrives with the question that
    would resolve it.
    """

    about: str
    text: str


@dataclass(frozen=True)
class IntentDraft:
    """A parsed request, before validation. Never persisted by this module.

    `desired_value` is `UNKNOWN` far more often than a demo would suggest,
    and that is the point: the field says what the sentence determined, not
    what a model was willing to say.
    """

    intent_type: str
    target: str
    desired_value: object
    unit: str = ""
    current_value: object = None
    scope: str = ""
    rationale: str = ""
    confidence: str = "low"
    #: WHY the confidence is what it is, in words a person can check. A bare
    #: "high" is a claim about the model's feelings.
    confidence_basis: str = ""
    source_text: str = ""
    questions: tuple[Question, ...] = ()

    @property
    def is_actionable(self) -> bool:
        return self.desired_value != UNKNOWN and self.target != UNKNOWN


@dataclass
class Extraction:
    """Everything one sentence produced. Possibly more than one intent."""

    drafts: list[IntentDraft] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


class MalformedProposal(Exception):
    """The model returned something that is not the agreed shape.

    Blocked rather than repaired. A parser that patches up malformed JSON
    produces a shape nobody downstream can rely on.
    """


# --- the script path -------------------------------------------------------------


def _amounts(text: str) -> list[tuple[int, str]]:
    """Amounts that carry a unit. A bare number is NOT an amount here.

    "Change it to 500" is a classic ambiguity case: 500? 500
    thousand? 500,000 JPY? Reading it as 500 is a decision the sentence does
    not support, and it is the kind of decision that reaches a payment
    system.
    """
    found = []
    for match in _AMOUNT_WITH_UNIT.finditer(text):
        digits = match.group("n1") or match.group("n2")
        unit = (match.group("pre") or match.group("post") or "").upper()
        unit = {"YEN": "JPY", "¥": "JPY", "$": "USD"}.get(unit, unit)
        found.append((int(digits.replace(",", "").replace("_", "")), unit))
    return found


def split_requests(text: str) -> list[str]:
    """Two asks in one sentence become two sentences.

    Not cosmetic. One may be valid and the other not, and a merged intent
    forces a person to accept or reject both together.
    """
    parts = [p.strip(" .,;") for p in _CONJUNCTION.split(text) if p and p.strip(" .,;")]
    if len(parts) < 2:
        return [text.strip()]
    # Only a split if BOTH halves ask for something. "The threshold and the
    # deadline are documented in the SOP" is one statement, not two asks.
    asks = [p for p in parts
            if _amounts(p) or _DIRECTIONAL.search(p) or _REMOVAL.search(p)]
    return asks if len(asks) >= 2 else [text.strip()]


def extract(text: str, *, target: str = "", current_value=None,
            proposer=None) -> Extraction:
    """Parse one piece of feedback into one or several drafts."""
    result = Extraction()
    for part in split_requests(text):
        draft, questions = _extract_one(
            part, target=target, current_value=current_value, proposer=proposer)
        result.drafts.append(draft)
        result.questions.extend(questions)
    return result


def _extract_one(text: str, *, target: str, current_value, proposer):
    questions: list[Question] = []
    amounts = _amounts(text)

    # Removal is checked FIRST. "Drop the 1,000,000 threshold" contains a
    # number, and reading the number first turns a deletion into an edit
    # that sets the value to what it already is.
    if _REMOVAL.search(text):
        return IntentDraft(
            intent_type="rule_removal",
            target=target or UNKNOWN,
            desired_value=None,
            current_value=current_value,
            rationale=text,
            confidence="high" if target else "low",
            confidence_basis=("an explicit removal verb, and a target the "
                              "caller supplied" if target else
                              "an explicit removal verb, but no target rule"),
            source_text=text), questions

    if amounts:
        # Several amounts in one clause is ambiguous about which is desired.
        # Asked about rather than ranked.
        if len(amounts) > 1:
            questions.append(Question(
                about="desired_value",
                text=(f"This mentions {len(amounts)} amounts "
                      f"({', '.join(f'{a:,} {u}' for a, u in amounts)}). "
                      f"Which one is the new value?")))
        value, unit = amounts[0]
        return IntentDraft(
            intent_type="business_rule_change",
            target=target or UNKNOWN,
            desired_value=value if len(amounts) == 1 else UNKNOWN,
            unit=unit if len(amounts) == 1 else "",
            current_value=current_value,
            rationale=text,
            confidence="high" if len(amounts) == 1 else "low",
            confidence_basis=("an explicit numeric value with a unit"
                              if len(amounts) == 1 else
                              f"{len(amounts)} amounts and no stated direction"),
            source_text=text,
            questions=tuple(questions)), questions

    # A number with no unit. A classic ambiguity, and the one most worth
    # refusing: 500 could be 500, 500 thousand, or 500,000 JPY.
    bare = _BARE_NUMBER.search(text)
    if bare:
        questions.append(Question(
            about="unit",
            text=(f"'{bare.group('n')}' has no unit. Is this "
                  f"{bare.group('n')} JPY, or thousands, or something else?")))
        return _abstain(text, target, current_value,
                        "a number with no unit", questions), questions

    if _DIRECTIONAL.search(text):
        questions.append(Question(
            about="desired_value",
            text=("This asks for a direction but names no value. What should "
                  "the new value be?")))
        return _abstain(text, target, current_value,
                        "a direction with no destination", questions), questions

    if proposer is None:
        questions.append(Question(
            about="desired_value",
            text="What specifically should change, and to what value?"))
        return _abstain(text, target, current_value,
                        "no value, direction or removal verb found",
                        questions), questions

    return _ask_model(text, target, current_value, proposer, questions)


def _abstain(text, target, current_value, why, questions) -> IntentDraft:
    return IntentDraft(
        intent_type="unknown",
        target=target or UNKNOWN,
        desired_value=UNKNOWN,
        current_value=current_value,
        rationale=text,
        confidence="low",
        confidence_basis=why,
        source_text=text,
        questions=tuple(questions))


# --- the model path ----------------------------------------------------------------


def build_prompt(text: str, *, target: str = "") -> str:
    """Ask for the agreed shape, and say that UNKNOWN is a valid answer.

    The last rule matters more than the schema. A model asked for a number
    and told nothing about abstaining will produce a number.
    """
    return (
        "Extract a business-rule change request from the statement below and "
        "reply with JSON only.\n\n"
        f"Fields: intent_type (one of {', '.join(INTENT_TYPES)}), target, "
        "desired_value, unit, scope, rationale.\n\n"
        "Rules for your answer:\n"
        "- If the statement does not determine a value, set desired_value to "
        '"UNKNOWN". That is a correct answer, not a failure.\n'
        "- Never infer a unit that the statement does not give.\n"
        "- Do not decide whether the change is a good idea.\n\n"
        f"Rule under discussion: {target or 'not specified'}\n\n"
        f"STATEMENT: {text}\n")


def parse_proposal(raw: str) -> dict:
    """Read the model's JSON, or refuse it.

    No repair, no salvage, no best-effort field extraction. A parser that
    patches malformed output produces a shape nobody downstream can rely on.
    """
    if not raw or not raw.strip():
        raise MalformedProposal("the proposer returned nothing")

    body = raw.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\n?", "", body)
        body = re.sub(r"\n?```$", "", body).strip()

    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise MalformedProposal(f"not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise MalformedProposal(f"expected an object, got {type(payload).__name__}")

    intent_type = payload.get("intent_type")
    if intent_type not in INTENT_TYPES:
        raise MalformedProposal(
            f"intent_type {intent_type!r} is not one of {list(INTENT_TYPES)}")

    return payload


def _ask_model(text, target, current_value, proposer, questions):
    try:
        payload = parse_proposal(proposer.explain(build_prompt(text, target=target)))
    except MalformedProposal as exc:
        questions.append(Question(
            about="desired_value",
            text="What specifically should change, and to what value?"))
        return _abstain(text, target, current_value,
                        f"the proposer's answer was rejected: {exc}",
                        questions), questions
    except Exception as exc:  # noqa: BLE001 -- a provider failure is not an answer
        questions.append(Question(
            about="desired_value",
            text="What specifically should change, and to what value?"))
        return _abstain(text, target, current_value,
                        f"the proposer failed: {type(exc).__name__}",
                        questions), questions

    # The model's value is accepted ONLY if the sentence carries it. This is
    # the whole defence: a model asked for a number, given a sentence with
    # none, will supply one, and no amount of prompting reliably stops it.
    proposed = payload.get("desired_value")
    if proposed in (None, "", UNKNOWN) or not _amounts(text):
        questions.append(Question(about="desired_value",
                                  text="What should the new value be?"))
        return _abstain(text, target, current_value,
                        "the statement determines no value, whatever the "
                        "proposer returned", questions), questions

    value, unit = _amounts(text)[0]
    return IntentDraft(
        intent_type=payload.get("intent_type", "business_rule_change"),
        target=payload.get("target") or target or UNKNOWN,
        desired_value=value,
        unit=unit,
        current_value=current_value,
        scope=payload.get("scope", ""),
        rationale=payload.get("rationale") or text,
        confidence="high",
        confidence_basis="the statement carries the value the proposer returned",
        source_text=text,
        questions=tuple(questions)), questions


__all__ = [
    "Extraction",
    "INTENT_TYPES",
    "IntentDraft",
    "MalformedProposal",
    "Question",
    "UNKNOWN",
    "build_prompt",
    "extract",
    "parse_proposal",
    "split_requests",
]
