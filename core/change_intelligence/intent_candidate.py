"""A persisted, unconfirmed proposal. Grounding, never model confidence, binds it."""
from dataclasses import dataclass
import hashlib
import json
import math
import re

GROUNDED_FIELDS = ("target", "semantic_id", "current_value", "desired_value",
                   "unit", "scope", "ambiguity", "intent_type")


def validate_model_output(parsed):
    keys = {"text", "target", "proposed_value", "rationale", "unit", "scope", "ambiguity"}
    if not isinstance(parsed, dict) or set(parsed) != keys:
        return False, ("model output must contain exactly the declared seven keys",)
    reasons = []
    for key in keys - {"proposed_value", "ambiguity"}:
        if not isinstance(parsed[key], str):
            reasons.append(f"{key} must be a string")
    if parsed["ambiguity"] is not None and not isinstance(parsed["ambiguity"], str):
        reasons.append("ambiguity must be a string or null")
    value = parsed["proposed_value"]
    if not (value == "UNKNOWN" or (type(value) in (int, float) and math.isfinite(value))):
        reasons.append("proposed_value must be a finite number or UNKNOWN")
    return not reasons, tuple(reasons)



def _names(text, phrase):
    """Whole-word, case-insensitive: `all` does not occur inside `small`."""
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.I) is not None


#: Clause boundaries. A comma or period with digits on both sides ("500,000") is not one.
_CLAUSE = re.compile(r"[,.](?!\d)|(?<!\d)[,.]|[;:!?、。]|(?<!\w)but(?!\w)", re.I)
#: Exclusion connectives (re-review F2, F3). Everything AFTER one, within its
#: clause, is excluded: "lower the fee instead of the approval threshold".
#: Deliberately broad: a false exclusion refuses and asks again; a missed one
#: lets the user confirm the change they excluded.
_EXCLUDES_WHAT_FOLLOWS = re.compile(
    r"(?<!\w)(?:not|no|never|don't|dont|doesn't|without|except|excluding|exclude|"
    r"other than|instead of|rather than|in place of|as opposed to|apart from|"
    r"aside from|save for|besides|leave|avoid|skip)(?!\w)", re.I)
#: Cues that say a clause's subject is NOT to change, wherever the subject
#: sits ("the threshold stays the same", "keep the threshold"). The whole
#: clause is treated as excluded.
_EXCLUDES_THE_CLAUSE = re.compile(
    r"(?<!\w)(?:unchanged|untouched|alone|as is|as-is|as it is|intact|the same|"
    r"remain|remains|stay|stays|should not|shouldn't|must not|mustn't|will not|won't|"
    r"cannot|can't|keep|keeps|maintain|maintains|preserve|retain|freeze|hold)(?!\w)", re.I)
#: Sentence boundaries, for anchoring a target to the stated new value.
_SENTENCE = re.compile(r"(?<!\d)\.|\.(?!\d)|[;!?。\n]")


def _segments(text):
    """(segment, excluded, clause_index) triples, in order.

    A clause splits at its first exclusion connective: the part before is
    requested, the rest is excluded.
    """
    out = []
    for index, clause in enumerate(_CLAUSE.split(text)):
        if not clause.strip():
            continue
        if _EXCLUDES_THE_CLAUSE.search(clause):
            out.append((clause, True, index))
            continue
        cut = _EXCLUDES_WHAT_FOLLOWS.search(clause)
        if cut is None:
            out.append((clause, False, index))
        else:
            out.append((clause[:cut.start()], False, index))
            out.append((clause[cut.start():], True, index))
    return out


def _mentions(text, phrases):
    """(affirmed, excluded): whether any phrase is requested / excluded."""
    affirmed = excluded = False
    for segment, is_excluded, _ in _segments(text):
        if any(_names(segment, p) for p in phrases):
            if is_excluded:
                excluded = True
            else:
                affirmed = True
    return affirmed, excluded


def _phrases(sid, vocabulary):
    return (*vocabulary[sid], sid.replace("_", " "))


def rules_named(text, vocabulary):
    """Rules the human text asks to change: named in a clause that is not negated."""
    return tuple(sorted(sid for sid in vocabulary
                        if _mentions(text, _phrases(sid, vocabulary))[0]))


#: Verbs that SET a value (see below). The rule a person asks to
#: change is the object of the change verb that carries the number: in
#: "lower the shipping fee to JPY 500000 and review the approval threshold
#: later", the value belongs to the shipping fee, whatever follows.
_CHANGE_VERB = re.compile(
    r"(?<!\w)(?:lower|lowers|lowering|raise|raises|raising|set|sets|setting|change|changes|"
    r"changing|update|updates|updating|increase|increases|increasing|decrease|decreases|"
    r"decreasing|reduce|reduces|reducing|cut|cuts|cutting|adjust|adjusts|adjusting|move|"
    r"moves|moving|make|makes|bump|bumps|drop|drops|lift|lifts|tighten|tightens|loosen|"
    r"loosens|relax|relaxes|modify|modifies|amend|amends|alter|alters|put|puts)(?!\w)", re.I)
#: Where the object of a change verb ends.
_OBJECT_END = re.compile(
    r"(?<!\w)(?:to|at|by|from|for|in|on|with|into|until|below|above|under|over|of)(?!\w)"
    r"|[,;:()]|\d", re.I)
#: A pronoun object refers back to a subject named earlier in the sentence:
#: "For the approval threshold, lower it to 500000".
_PRONOUN = re.compile(r"^\W*(?:it|this|that|them|this one|that one)\W*$", re.I)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _named_in(span, vocabulary):
    return {sid for sid in vocabulary if any(_names(span, p) for p in _phrases(sid, vocabulary))}


def _value_subjects(text, vocabulary):
    """The rule ids the text's new value(s) are attached to.

    For each number: the object of the closest change verb before it, in the
    same sentence. A pronoun object resolves to the rules named earlier in
    that sentence. With no change verb, the sentence up to the number is the
    subject. Rules mentioned anywhere else are not what the value changes.
    """
    bound = set()
    for sentence in _SENTENCE.split(text):
        for number in _NUMBER.finditer(sentence):
            before = sentence[:number.start()]
            verbs = list(_CHANGE_VERB.finditer(before))
            if not verbs:
                bound |= _named_in(before, vocabulary)
                continue
            verb = verbs[-1]
            tail = before[verb.end():]
            end = _OBJECT_END.search(tail)
            obj = tail[:end.start()] if end else tail
            here = _named_in(obj, vocabulary)
            if here:
                bound |= here
            elif _PRONOUN.match(obj):
                bound |= {sid for sid in vocabulary
                          if _mentions(before[:verb.start()], _phrases(sid, vocabulary)) == (True, False)}
    return bound


def ground_target(text, hint, vocabulary):
    """Accept a model's target only when the human text requests that rule alone.

    `vocabulary` maps each known semantic id to the phrases the project uses
    for it (seed_manifest `semantic_hints`). A rule the text negates or
    excludes ("do not change the approval threshold") is not requested, even
    if also mentioned elsewhere. Returns (target, reasons); an empty target
    means the hint is unresolved and must not be confirmed.
    """
    if hint not in vocabulary:
        return "", ()
    affirmed, negated = _mentions(text, _phrases(hint, vocabulary))
    if negated:
        return "", (f"the request excludes or negates the proposed rule {hint}",)
    named = rules_named(text, vocabulary)
    if not affirmed:
        return "", (f"the request does not name the proposed rule {hint}"
                    + (f"; it names {', '.join(named)}" if named else ""),)
    # The change a person asks for is the ACTION that carries the value they
    # give: in "lower the fee to JPY 500000 and review the
    # approval threshold later" the value belongs to the fee. Binding to the
    # action also holds for exclusion words no list above knows.
    if re.search(r"\d", text):
        bound = _value_subjects(text, vocabulary)
        if hint not in bound:
            return "", (f"the request's new value belongs to another subject, not the proposed rule {hint}",)
        requested = bound
    else:
        requested = set(named)
    if len(requested) > 1:
        return "", ("the request's value applies to more than one rule (" + ", ".join(sorted(requested))
                    + "); confirm one change at a time",)
    return hint, ()


def ground_scope(text, scope, vocabulary=None):
    """A scope counts only if the human wrote it, as whole words, and did not narrow it.

    Stricter than a target: "all orders except small orders" affirms
    `all orders` but narrows it in the same clause. An exclusion in that
    clause refuses the scope unless what it excludes is a known rule
    ("..., instead of the emergency ceiling" excludes a rule, not orders).
    """
    scope = scope.strip()
    if not scope or scope == "UNKNOWN":
        return ""
    phrase = (scope,)
    segments = _segments(text)
    if any(ex and any(_names(seg, p) for p in phrase) for seg, ex, _ in segments):
        return ""
    clauses = {i for seg, ex, i in segments if not ex and any(_names(seg, p) for p in phrase)}
    if not clauses:
        return ""
    rules = vocabulary or {}
    for seg, ex, i in segments:
        if ex and i in clauses and not any(
                _names(seg, p) for sid in rules for p in _phrases(sid, rules)):
            return ""
    return scope


@dataclass(frozen=True)
class IntentCandidate:
    candidate_id: str
    session_id: str
    base_snapshot: str
    source_text: str
    source_text_sha256: str
    target: str
    semantic_id: str
    current_value: object
    desired_value: object
    unit: str
    scope: str
    ambiguity: str
    intent_type: str
    evidence: tuple
    questions: tuple
    validation_ok: bool
    validation_reasons: tuple
    injection_findings: tuple = ()
    model: str = ""
    region: str = ""
    latency_ms: int = 0
    created_at: str = ""

    @property
    def content_hash(self):
        content = {key: getattr(self, key) for key in GROUNDED_FIELDS}
        content.update(session_id=self.session_id, base_snapshot=self.base_snapshot,
                       source_text_sha256=self.source_text_sha256)
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()
