"""Live diagnostic SUR04-V1-20261022 (closure C-12): an operator-change finding has to name the comparator; the plain-text guard refused `>=` / `>` as if they were markup.
Comparators pass; tags, backticks, pipes, links and URLs are still refused. Pure."""
from __future__ import annotations

from core.change_intelligence.v2 import semantic_drift as sd


def test_comparators_are_plain_text_and_markup_is_not():
    for ok in ("The manager check changed from >= to >, so exactly 1000000 JPY no longer reaches the manager.", "amount <1000000 is now automatic", "a < b and c <= d", "route -> manager"):
        assert not sd._NOT_PLAIN.search(ok), ok
    for bad in ("see <b>here</b>", "</script>", "<!-- x -->", "use `code`", "a | b", "[x](http://y)", "https://example.com", "**bold**"):
        assert sd._NOT_PLAIN.search(bad), bad
