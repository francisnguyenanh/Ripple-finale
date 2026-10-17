# -*- coding: utf-8 -*-
"""Which events drive the loop, read from the schemas that declare it.

This lives beside the schemas rather than inside `change_intelligence`
because reading files is infrastructure, and tier 2 reaches storage only
through a contract (P1 §6 Q5). The classification itself is domain
knowledge; *fetching* it is not, and separating the two is what lets the
loop module be tested without a filesystem.

There is still exactly one source of truth: the `x-loop-triggering` flag in
each event's JSON Schema. Nothing here restates the list, and a test in
`tests/contract/test_loop_events.py` fails if any module does.
"""

from __future__ import annotations

import functools
import json
import pathlib

SCHEMA_DIR = pathlib.Path(__file__).resolve().parent / "events"


@functools.lru_cache(maxsize=1)
def _classification() -> dict:
    """`{event_type: is_loop_triggering}`. Cached: the schemas are static."""
    found = {}
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        found[path.stem] = schema.get("x-loop-triggering") is True
    return found


def known() -> tuple:
    """Every declared event type, sorted."""
    return tuple(sorted(_classification()))


def loop_triggering() -> tuple:
    """The ones that drive the reconcile loop."""
    return tuple(sorted(n for n, loop in _classification().items() if loop))


def internal() -> tuple:
    """Produced inside a turn already in progress. Never subscribed."""
    return tuple(sorted(n for n, loop in _classification().items() if not loop))


__all__ = ["SCHEMA_DIR", "internal", "known", "loop_triggering"]
