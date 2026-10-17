"""`workflow` adapter — declarative approval routing.

The artifact family that produces the strongest cross-artifact conflicts,
which is why it is built first: a workflow config states a business rule in
a form a business analyst reads directly, so when it disagrees with a
spreadsheet the disagreement needs no translation to be understood.

The one requirement that matters more than the rest: a threshold is
extracted as a VALUE WITH A UNIT, never as a string.

    condition: {field: amount, op: ">=", value: 1000000, currency: JPY}
      -> RealityFact{kind: threshold, field: amount, op: ">=",
                     value: 1000000, unit: JPY, provenance: {...}}

Without that normalisation, the identity layer cannot tell that `1000000`, `¥1,000,000`
and "one million yen" are one rule, and the whole identity layer collapses
into string matching.

Imports nothing from `core.kernel` -- same discipline as every other
adapter in this tree.
"""

from __future__ import annotations

import glob
import hashlib
import io
import os
from typing import Any, Iterator

import yaml

from core.adapters.base import FileRef, IRRecord, Provenance, SourceAdapter

#: Fields whose value is money in this domain. A threshold on `amount` is a
#: currency amount; a threshold on `customer_tier` is not, and tagging the
#: latter with a currency would make two unrelated rules look comparable.
_MONETARY_FIELDS = {"amount", "value", "total", "limit", "threshold"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_of(lines: list[str], needle: str, start: int = 0) -> int:
    """First line at or after `start` containing `needle`, 1-indexed.

    Locations have to be addressable for evidence to be verifiable (I4), and
    PyYAML discards them. Searching the raw text is the honest way to recover
    a line number; the alternative -- emitting a made-up location -- would
    produce evidence that cannot be checked, which is worse than none.
    """
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    return 1


class WorkflowAdapter(SourceAdapter):
    name = "workflow"
    languages = ["yaml"]

    def discover(self, paths: list[str], options: dict) -> Iterator[FileRef]:
        patterns = options.get("patterns") or ["**/*.yaml", "**/*.yml"]
        seen: set[str] = set()
        for root in paths:
            for pattern in patterns:
                for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
                    if not os.path.isfile(path) or path in seen:
                        continue
                    seen.add(path)
                    yield FileRef(path=path, size=os.path.getsize(path))

    # --- parsing ------------------------------------------------------------

    def parse(self, file_ref: FileRef, **kwargs: Any) -> Iterator[IRRecord]:
        raw = kwargs.get("content")
        if raw is None:
            with open(file_ref.path, "rb") as f:
                raw = f.read()
        yield from self.parse_bytes(raw, file_ref.path, kwargs.get("snapshot_id", ""))

    def parse_bytes(self, raw: bytes, path: str, snapshot_id: str) -> Iterator[IRRecord]:
        """Parse from bytes.

        Bytes rather than a path, because the content may come from a session
        overlay that was never written to disk (ADR-003). Every adapter this
        product adds takes the same shape for the same reason.
        """
        text = raw.decode("utf-8")
        lines = text.splitlines()
        doc = yaml.safe_load(io.BytesIO(raw)) or {}
        currency = doc.get("currency", "")

        def prov(line: int) -> Provenance:
            return Provenance(file=path, line_start=line, line_end=line,
                              method="parser", snapshot_id=snapshot_id or None)

        for index, route in enumerate(doc.get("approval_routes", []) or []):
            route_id = route.get("id", f"route_{index}")
            rule_id = route.get("rule_id", "")
            line = _line_of(lines, f"id: {route_id}")

            yield IRRecord(
                kind="entity", id=f"workflow:{path}:route:{route_id}",
                payload={
                    "type": "approval_route",
                    "route_id": route_id,
                    "rule_id": rule_id,
                    "approver_role": route.get("approver_role", ""),
                    "order": index,
                    "description": route.get("description", ""),
                },
                provenance=(prov(line),),
            )

            condition = route.get("condition") or {}
            if not condition:
                continue

            field = condition.get("field", "")
            value = condition.get("value")
            unit = condition.get("currency") or (
                currency if field in _MONETARY_FIELDS and isinstance(value, (int, float)) else "")

            condition_line = _line_of(lines, "condition:", start=line - 1)
            yield IRRecord(
                kind="entity", id=f"workflow:{path}:condition:{route_id}",
                payload={
                    "type": "threshold" if isinstance(value, (int, float)) else "predicate",
                    "route_id": route_id,
                    "rule_id": rule_id,
                    "field": field,
                    "op": condition.get("op", ""),
                    "value": value,
                    "unit": unit,
                    #: What the route DOES when the condition matches. For a
                    #: numeric threshold this is context; for a non-numeric
                    #: predicate it is the rule itself -- "strategic customers
                    #: are exempt" IS `approver_role: NONE`.
                    "approver_role": route.get("approver_role", ""),
                    "description": route.get("description", ""),
                    "quote": lines[condition_line - 1].strip() if condition_line <= len(lines) else "",
                },
                provenance=(prov(condition_line),),
            )

            yield IRRecord(
                kind="relation", id=f"workflow:{path}:configures:{route_id}",
                payload={
                    "src": f"workflow:{path}:condition:{route_id}",
                    "type": "CONFIGURES",
                    "dst": f"rule:{rule_id}" if rule_id else f"route:{route_id}",
                    "reason": f"route {route_id} implements the decision for {rule_id or route_id}",
                },
                provenance=(prov(condition_line),),
            )

        for index, exception in enumerate(doc.get("exceptions", []) or []):
            exception_id = exception.get("id", f"exception_{index}")
            rule_id = exception.get("rule_id", "")
            line = _line_of(lines, f"id: {exception_id}")

            yield IRRecord(
                kind="entity", id=f"workflow:{path}:exception:{exception_id}",
                payload={
                    "type": "exception",
                    "exception_id": exception_id,
                    "rule_id": rule_id,
                    "handler": exception.get("handler", ""),
                    "description": (exception.get("description") or "").strip(),
                    #: An exception that names a handler but states no value
                    #: has delegated the rule to code. That is a FACT about
                    #: the config, and the thing that makes UN-002 findable.
                    "states_value": any(k in exception for k in ("value", "threshold")),
                },
                provenance=(prov(line),),
            )

            if rule_id:
                yield IRRecord(
                    kind="relation", id=f"workflow:{path}:exempts:{exception_id}",
                    payload={
                        "src": f"workflow:{path}:exception:{exception_id}",
                        "type": "EXEMPTS",
                        "dst": f"rule:{rule_id}",
                        "reason": f"exception {exception_id} carves {rule_id} out to a handler",
                    },
                    provenance=(prov(line),),
                )


ADAPTER = WorkflowAdapter()
