# -*- coding: utf-8 -*-
"""Canonical hash for v2 domain objects.

Builds on `core/kernel/serialize.py::canonical_dumps` (ADR-005 §2.7: sorted
keys in codepoint order, stable separators) rather than duplicating that
logic. This module adds only what `canonical_dumps` does not already do:
turning a dataclass instance (and any `Decimal` fields it carries) into a
plain JSON-able structure first, then hashing the canonical text.

`Decimal('1')`
and the string `'1'` would otherwise serialize to the identical JSON text `"1"`,
so `canonical_hash` would collide across materially different domain-value
types. Avoided by tagging `Decimal` with a stable, documented type marker
before hashing: `Decimal(x)` becomes `{"__type__": "decimal", "value":
str(x)}`, a JSON *object*, which can never collide with the bare JSON
*string* a plain `str` value produces. This is deterministic (the same
`Decimal` always produces the same tagged object) and key-order
independent (`canonical_dumps` still sorts keys), so every existing
hash-determinism test continues to pass; only the *cross-type* collision
is avoided.

A tag shape of `{"__type__": "decimal", "value": str(x)}`, is an ORDINARY JSON
object -- a genuine user-supplied dict of that exact shape (e.g. inside an
open `Any`/mapping field like `RuleCandidate.value` or `unknown_fields`)
hashes IDENTICALLY to a real `Decimal`
(`canonical_hash(Decimal('1')) ==
canonical_hash({'__type__':'decimal','value':'1'})` would be `True`). Avoided by:

1. Reserving an UNAMBIGUOUS tag key, `_DECIMAL_TAG_KEY`, that embeds NUL
   control characters (`\x00`) around a long, module-private literal. A
   Python `dict` CAN technically contain a key with NUL bytes, but no
   ordinary JSON/YAML/user-authored mapping literal can produce one
   through normal parsing (NUL is not typable in JSON string literals
   without an explicit `\u0000` escape, and no legitimate domain field name
   in this codebase does that) -- an "astronomically unlikely"
   namespace-prefix scheme.
2. Additionally ESCAPING any plain user dict that happens to already
   contain `_DECIMAL_TAG_KEY` as one of its own keys (belt-and-braces): such a dict is wrapped as
   `{_ESCAPE_KEY: <dict, recursively converted>}` before further
   processing, so even a deliberately-adversarial user dict built to
   mimic the reserved tag shape round-trips to a JSON object with a
   DIFFERENT top-level key than the real Decimal tag, and can never
   collide with it.

Property guaranteed and directly tested
(`core/tests/test_ai1_t01_schema.py::
test_canonical_hash_decimal_and_string_same_spelling_differ` plus the
lookalike-dict regression): `canonical_hash(Decimal('1'))` is
never equal to `canonical_hash(any plain str)` or to
`canonical_hash(any plain dict)`, including one deliberately shaped to
look like the tag. The hash remains otherwise deterministic and
key-order-independent (`canonical_dumps` still sorts keys).
"""

from __future__ import annotations

import hashlib
from dataclasses import fields, is_dataclass
from decimal import Decimal
from typing import Any

from core.kernel.serialize import canonical_dumps

#: Reserved, unambiguous type-tag key for a canonically-hashed `Decimal`.
#: Embeds NUL control characters so no ordinary JSON/YAML-authored dict
#: literal can produce this exact key by accident.
_DECIMAL_TAG_KEY = "\x00__ripple_v2_decimal_type_tag__\x00"

#: Reserved escape-wrapper key used ONLY when a genuine user dict already
#: contains `_DECIMAL_TAG_KEY` as one of its own keys (an adversarial
#: lookalike). Distinct literal from `_DECIMAL_TAG_KEY` so the two can
#: never be confused with one another.
_ESCAPE_KEY = "\x00__ripple_v2_escaped_user_dict__\x00"


def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Decimal):
        # Tagged with a reserved, NUL-embedding key: `Decimal('1')` must
        # never hash identically to the plain string `'1'`, NOR to any
        # plain user dict -- including one deliberately shaped to look
        # like `{"__type__": "decimal", "value": "1"}` (an earlier tag shape,
        # which was an ordinary, collidable JSON object) or like the
        # reserved key (see the escape branch below).
        return {_DECIMAL_TAG_KEY: str(obj)}
    if isinstance(obj, dict):
        if _DECIMAL_TAG_KEY in obj or _ESCAPE_KEY in obj:
            # Adversarial or coincidental lookalike: a genuine user dict
            # that happens to carry one of this module's reserved keys.
            # Escape it under a DIFFERENT reserved key so it can never be
            # mistaken for a real Decimal tag (or a real escape wrapper)
            # by this function's own output.
            return {_ESCAPE_KEY: {str(k): _to_jsonable(v) for k, v in obj.items()}}
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def canonical_hash(obj: Any) -> str:
    """Deterministic, key-order-independent hash of `obj`'s content.

    Two independently constructed objects with identical logical content
    hash identically; changing any field's content changes the hash.
    `canonical_dumps` already sorts dict keys, so insertion order never
    affects the result.
    """
    jsonable = _to_jsonable(obj)
    text = canonical_dumps(jsonable)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
