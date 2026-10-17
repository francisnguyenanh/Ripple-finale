"""Rendered-text scans for the new UI (Slice D acceptance).

* Business view: no raw identifier, key path, reason code or JSON as main content.
* Application copy: English only. Verbatim user input and source quotes (blockquote, code, input values, chat turns)
  are removed before the scan: they keep their original language by design.
"""
from __future__ import annotations

import re

# Elements whose text is verbatim data (a source quote, a typed message, a stored value) and keeps its original form.
VERBATIM = "blockquote, code, pre, [data-verbatim], [data-ai-text], [data-testid=chat-turn] > span, [data-testid=evidence-panel], select, textarea, input, option, header select"

JS_TEXT = """(sel) => {
  // Hide the verbatim parts on the LIVE page (so block layout and line breaks are real), read the text, then restore.
  const root = document.querySelector('main') || document.body;
  const hidden = [];
  root.querySelectorAll(sel + ', .sr-only').forEach(n => { hidden.push([n, n.style.display]); n.style.display = 'none'; });
  const text = root.innerText;
  hidden.forEach(([n, d]) => { n.style.display = d; });
  return text;
}"""

IDENTIFIERS = [
    (re.compile(r"\b(?:cand|art|job|src|rev|km|intent|chat|dec|finding|restore-approval|ui)-[A-Za-z0-9_-]{6,}"), "a raw record id"),
    (re.compile(r"\b[0-9a-f]{32}\b"), "a 32-hex id"),
    (re.compile(r"\b[0-9a-f]{64}\b"), "a sha256 digest"),
    (re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b"), "a snake_case key or reason code"),
    (re.compile(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b"), "an UPPER_CASE reason code"),
    (re.compile(r"\{\s*\"[A-Za-z_]+\"\s*:"), "JSON"),
    (re.compile(r"\b[a-z]+(?:_[a-z]+)*\.[a-z]+_[a-z_.]+\b"), "a dotted key path"),
]
NON_ENGLISH = re.compile(r"[぀-ヿ㐀-鿿À-ÿĀ-ỹ]")


def visible_text(page) -> str:
    return page.evaluate(JS_TEXT, VERBATIM)


FILE_NAME = re.compile(r"[A-Za-z0-9_./-]+\.(?:md|yaml|yml|xlsx|py|csv|jsonl|html|json|txt)(?![A-Za-z0-9_])")


def business_leaks(page) -> list[str]:
    text = FILE_NAME.sub(" ", visible_text(page))     # a file name is a name people know, not a key
    out = []
    for rx, what in IDENTIFIERS:
        for m in rx.finditer(text):
            a, b = max(0, m.start() - 30), min(len(text), m.end() + 20)
            out.append(f"{what}: {m.group(0)!r} in ...{text[a:b]!r}")
    return out


def non_english(page) -> list[str]:
    text = visible_text(page)
    return sorted({m.group(0) for m in NON_ENGLISH.finditer(text)})


def assert_business_clean(page, where: str) -> None:
    leaks = business_leaks(page)
    assert not leaks, f"Business view leaks raw data on {where}: {leaks[:40]}"
    foreign = non_english(page)
    assert not foreign, f"non-English application text on {where}: {foreign[:8]}"
