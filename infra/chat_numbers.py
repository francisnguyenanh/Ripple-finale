"""Numbers a person actually wrote, with the words that carried them: digits (1,000,000 / 1.000.000 / 500000), digits with a unit word
(500k, 1.5 million, 1 triệu, 50万), and spelled-out numbers (five hundred thousand, năm trăm nghìn).

The server accepts a model-proposed NEW value only if it equals one of these, so every value can be traced to the person's own
words (`quote`). Nothing is guessed: an unparsable phrase simply produces no number, and the AI has to ask."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_SUFFIX = {"k": 1_000, "thousand": 1_000, "nghìn": 1_000, "ngàn": 1_000, "千": 1_000, "m": 1_000_000, "mil": 1_000_000, "million": 1_000_000,
           "triệu": 1_000_000, "tr": 1_000_000, "万": 10_000, "億": 100_000_000, "billion": 1_000_000_000, "tỷ": 1_000_000_000, "bn": 1_000_000_000}
_DIGITS = re.compile(r"(?<![\w.,])(\d[\d.,_]*)(?:\s*(k|thousand|million|billion|mil|bn|m|nghìn|ngàn|triệu|tr|tỷ|万|千|億)(?![A-Za-z]))?", re.I)
_UNIT = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
         "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
         "một": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5, "sáu": 6, "bảy": 7, "tám": 8, "chín": 9, "mười": 10, "lăm": 5, "mốt": 1}
_SCALE = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000, "trăm": 100, "nghìn": 1_000, "ngàn": 1_000,
          "triệu": 1_000_000, "tỷ": 1_000_000_000}
_WORD = re.compile(r"[A-Za-zÀ-ỹ]+")


def _clean(num: str):
    s = num.replace("_", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):             # 1,000,000 or 1.000.000: thousands separators
        s = re.sub(r"[.,]", "", s)
    elif re.fullmatch(r"\d+,\d{1,2}", s):                   # 1,5: decimal comma
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _norm(d: Decimal):
    return int(d) if d == d.to_integral_value() else d


def _word_runs(text: str):
    toks = [(m.group(0).lower(), m.start(), m.end()) for m in _WORD.finditer(text)]
    i = 0
    while i < len(toks):
        w = toks[i][0]
        if w in _UNIT or w in _SCALE or w == "mươi":
            j, total, cur, seen, unit_seen = i, 0, 0, False, False
            while j < len(toks):
                w = toks[j][0]
                if w in _UNIT:
                    cur += _UNIT[w]
                    seen = unit_seen = True
                elif w == "mươi":
                    cur = (cur or 1) * 10
                elif w in _SCALE:
                    if w == "hundred" or w == "trăm":
                        cur = (cur or 1) * 100
                    else:
                        total += (cur or 1) * _SCALE[w]
                        cur = 0
                    seen = True
                elif w == "and":
                    pass
                else:
                    break
                j += 1
            if seen and unit_seen and total + cur > 0:
                yield total + cur, toks[i][1], toks[j - 1][2]
            i = max(j, i + 1)
        else:
            i += 1


def numbers_with_quotes(text: str) -> list[tuple]:
    """-> [(value, quote)] for every number the text states. Words that are also common function words (Vietnamese 'năm' = five/year,
    'ba', 'hai') only count inside a run that also holds a scale word (trăm, nghìn, triệu, ...) or a digit context."""
    t = text or ""
    out: list = []
    for m in _DIGITS.finditer(t):
        d = _clean(m.group(1))
        if d is None:
            continue
        suf = (m.group(2) or "").lower()
        if suf:
            d = d * _SUFFIX.get(m.group(2), _SUFFIX.get(suf, 1))
        out.append((_norm(d), m.group(0).strip()))
    for val, a, b in _word_runs(t):
        quote = t[a:b]
        words = [w.lower() for w in _WORD.findall(quote)]
        only_unit_word = len(words) == 1 and words[0] in _UNIT and words[0] not in ("zero",) and words[0] in ("hai", "ba", "năm", "tư", "tám", "chín", "một", "bảy", "sáu", "bốn", "mười", "lăm", "mốt")
        has_scale = any(w in _SCALE or w == "mươi" for w in words)
        if only_unit_word or (not has_scale and any(w in ("hai", "ba", "năm", "tư") for w in words)):
            continue                                              # ambiguous Vietnamese function word, no scale word: not a number
        out.append((_norm(Decimal(val)), quote))
    return out


def numbers(text: str) -> set:
    return {v for v, _ in numbers_with_quotes(text)}


def quote_for(text: str, value) -> str | None:
    for v, q in numbers_with_quotes(text):
        if v == value:
            return q
    return None
