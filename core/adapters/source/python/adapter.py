"""`python` source adapter — `ast` from the standard library.

Two extractions here decide whether WOW #2 exists at all:

    numeric literal in a comparison   -> a hardcoded threshold
    attribute read on a `config` arg  -> a threshold read from configuration

Telling those apart is the whole difference between "this module implements
the rule" and "this module has a copy of a number that was once the rule".
A reader of the code sees a plausible constant either way.

No tokenizer, no parser: every construct the product needs already has an
`ast` node type. See `traps.md` for what is deliberately not handled.

Imports nothing from `core.kernel`.
"""

from __future__ import annotations

import ast
import glob
import os
import re
from typing import Any, Iterator

from core.adapters.base import FileRef, IRRecord, Provenance, SourceAdapter

#: Parameter names that mean "the policy was handed to me". ADR-002 C-1
#: requires the reference application to take its config this way, so a read
#: through one of these is a deliberate configuration reference rather than
#: an incidental attribute access.
_CONFIG_PARAMS = {"config", "cfg", "settings", "policy"}

#: Comparisons in which a bare number is a business threshold rather than an
#: index or a loop bound.
_THRESHOLD_OPS = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=",
                  ast.Eq: "==", ast.NotEq: "!="}

#: Below this, a literal is far more likely to be an index, a retry count or
#: a timeout than a monetary threshold. Stated as a constant because it is a
#: judgement call, and a judgement call hidden in a condition is a bug
#: waiting to be argued about.
_MIN_INTERESTING_LITERAL = 1000

#: A rule identifier as organisations write them. Where one appears in the
#: same call as a number, the artifact is DECLARING a rule rather than
#: comparing against one -- and that is tier-1 evidence for identity
#: resolution, the strongest kind there is.
_RULE_ID = re.compile(r"^BR-\d{3}$")


def _flatten_and(test: ast.AST) -> list[ast.AST]:
    """The operands of an `and` chain, or the test itself.

    `a == x and amount >= t` is one test to Python and two claims to this
    product: a scope and a threshold. Reading only the outermost node sees
    a BoolOp and neither.
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        flat: list[ast.AST] = []
        for value in test.values:
            flat.extend(_flatten_and(value))
        return flat
    return [test]


class PythonAdapter(SourceAdapter):
    name = "python"
    languages = ["python"]

    def discover(self, paths: list[str], options: dict) -> Iterator[FileRef]:
        patterns = options.get("patterns") or ["**/*.py"]
        excluded = set(options.get("exclude") or ["__init__.py"])
        seen: set[str] = set()
        for root in paths:
            for pattern in patterns:
                for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
                    if not os.path.isfile(path) or path in seen:
                        continue
                    if os.path.basename(path) in excluded or "__pycache__" in path:
                        continue
                    seen.add(path)
                    yield FileRef(path=path, size=os.path.getsize(path))

    def parse(self, file_ref: FileRef, **kwargs: Any) -> Iterator[IRRecord]:
        raw = kwargs.get("content")
        if raw is None:
            with open(file_ref.path, "rb") as f:
                raw = f.read()
        yield from self.parse_bytes(raw, file_ref.path, kwargs.get("snapshot_id", ""))

    def parse_bytes(self, raw: bytes, path: str, snapshot_id: str) -> Iterator[IRRecord]:
        source = raw.decode("utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=path)

        def prov(node: ast.AST) -> Provenance:
            line = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", line) or line
            return Provenance(
                file=path, line_start=line, line_end=end, method="parser",
                snapshot_id=snapshot_id or None,
                quote=lines[line - 1].strip() if line <= len(lines) else "",
            )

        module_id = f"python:{path}"

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name in self._imported_names(node):
                    yield IRRecord(
                        kind="relation", id=f"{module_id}:depends_on:{name}",
                        payload={"src": module_id, "type": "DEPENDS_ON", "dst": f"module:{name}",
                                 "reason": f"{os.path.basename(path)} imports {name}"},
                        provenance=(prov(node),))

            elif isinstance(node, ast.ClassDef):
                yield IRRecord(
                    kind="entity", id=f"{module_id}:class:{node.name}",
                    payload={"type": "class", "name": node.name,
                             "docstring": (ast.get_docstring(node) or "")[:400]},
                    provenance=(prov(node),))
                yield IRRecord(
                    kind="relation", id=f"{module_id}:contains:class:{node.name}",
                    payload={"src": module_id, "type": "CONTAINS",
                             "dst": f"{module_id}:class:{node.name}",
                             "reason": "module defines this class"},
                    provenance=(prov(node),))

            elif isinstance(node, ast.FunctionDef):
                yield from self._function(node, module_id, path, prov, lines)

            elif isinstance(node, ast.Call):
                yield from self._rule_declaration(node, module_id, path, prov, lines)

    def _rule_declaration(self, node: ast.Call, module_id: str, path: str,
                          prov, lines: list[str]) -> Iterator[IRRecord]:
        """A call that names a rule id and a number in the same breath.

            Rule("BR-001", "high_value_requires_manager", 1_000_000, ...)

        This is where a configuration module STATES the policy, and the
        value sits in a constructor rather than a comparison -- so the
        threshold extraction above cannot see it. Missing it would leave the
        software family absent from the hero rule's representations, and the
        one artifact a change has to be applied to would not appear in its
        own impact analysis.
        """
        literals = [a for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and _RULE_ID.match(a.value)]
        if not literals:
            return
        rule_id = literals[0].value

        numbers = [a.value for a in node.args
                   if isinstance(a, ast.Constant) and isinstance(a.value, int)
                   and not isinstance(a.value, bool) and a.value >= _MIN_INTERESTING_LITERAL]
        names = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)
                 and not _RULE_ID.match(a.value)]

        yield IRRecord(
            kind="entity", id=f"{module_id}:rule_declaration:{rule_id}",
            payload={
                "type": "threshold" if numbers else "rule_declaration",
                "rule_id": rule_id,
                "rule_name": names[0] if names else "",
                "value": numbers[0] if numbers else None,
                "unit": "JPY" if numbers else "",
                "field": "amount" if numbers else "",
                "op": ">=" if numbers else "",
                # The REAL source line, not a description of it. Evidence
                # is a substring that can be found at the location it cites
                # (I4); a sentence about the line is unverifiable, and the
                # whole-KM evidence check catches it -- as it did here.
                "quote": (lines[node.lineno - 1].strip()
                          if 0 < node.lineno <= len(lines) else ""),
            },
            provenance=(prov(node),))

        yield IRRecord(
            kind="relation", id=f"{module_id}:implements:{rule_id}",
            payload={"src": f"{module_id}:rule_declaration:{rule_id}",
                     "type": "IMPLEMENTS", "dst": f"rule:{rule_id}",
                     "reason": f"{os.path.basename(path)} declares {rule_id}"},
            provenance=(prov(node),))

    # --- functions ----------------------------------------------------------

    def _function(self, node: ast.FunctionDef, module_id: str, path: str,
                  prov, lines: list[str]) -> Iterator[IRRecord]:
        fn_id = f"{module_id}:function:{node.name}"
        params = [a.arg for a in node.args.args]
        config_params = {p for p in params if p in _CONFIG_PARAMS}

        # Locals that hold a value read out of the config. Without this,
        #
        #     threshold = config.threshold("BR-001")
        #     if request.amount >= threshold:
        #
        # reads as a comparison against an ordinary variable, and a module
        # that follows the policy correctly looks like one with no policy at
        # all. Every real handler is written this way, so missing it would
        # make the adapter blind to the correct case and loud about the
        # broken one -- the worst possible failure for identity resolution.
        config_aliases = self._config_aliases(node, config_params)
        config_names = config_params | config_aliases

        # Passing the config on is not ignoring it. A dispatcher that hands
        # `config` to the function doing the work has done its job, and
        # flagging it would bury the one module that really does ignore it.
        forwards_config = any(
            isinstance(sub, ast.Call)
            and any(isinstance(a, ast.Name) and a.id in config_params for a in sub.args)
            for sub in ast.walk(node)
        )

        yield IRRecord(
            kind="entity", id=fn_id,
            payload={"type": "function", "name": node.name, "params": params,
                     "accepts_config": bool(config_params),
                     "docstring": (ast.get_docstring(node) or "")[:400]},
            provenance=(prov(node),))
        yield IRRecord(
            kind="relation", id=f"{module_id}:contains:function:{node.name}",
            payload={"src": module_id, "type": "CONTAINS", "dst": fn_id,
                     "reason": "module defines this function"},
            provenance=(prov(node),))

        reads_config = False

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                callee = self._callee_name(sub.func)
                if callee:
                    yield IRRecord(
                        kind="relation", id=f"{fn_id}:calls:{callee}:{getattr(sub, 'lineno', 0)}",
                        payload={"src": fn_id, "type": "CALLS", "dst": f"callable:{callee}",
                                 "reason": f"{node.name} calls {callee}"},
                        provenance=(prov(sub),))
                    if self._is_config_read(sub.func, config_names):
                        reads_config = True

            elif isinstance(sub, ast.Attribute) and self._is_config_read(sub, config_params):
                reads_config = True
                yield IRRecord(
                    kind="relation", id=f"{fn_id}:reads:config:{getattr(sub, 'lineno', 0)}",
                    payload={"src": fn_id, "type": "READS", "dst": "config",
                             "attribute": sub.attr,
                             "reason": f"{node.name} reads {sub.attr} from the injected config"},
                    provenance=(prov(sub),))

            elif isinstance(sub, ast.Compare):
                yield from self._comparison(sub, fn_id, node.name, config_names, prov)

            elif isinstance(sub, ast.If):
                yield from self._rule_scope(sub, fn_id, node.name, prov, lines)

        # The whole point of the adapter, as one record.
        #
        # A function that takes `config` and never reads it is the shape of
        # emergency_purchase.py: same signature as the correct path, same
        # apparent contract, and a policy of its own.
        if config_params:
            if reads_config:
                note = "reads the config it is given"
            elif forwards_config:
                note = "passes the config on to another handler"
            else:
                note = ("ACCEPTS a config and neither reads nor forwards it -- any "
                        "policy value in this function is its own")
            yield IRRecord(
                kind="entity", id=f"{fn_id}:config_usage",
                payload={"type": "config_usage", "function": node.name,
                         "accepts_config": True, "reads_config": reads_config,
                         "forwards_config": forwards_config,
                         "ignores_config": not reads_config and not forwards_config,
                         "note": note},
                provenance=(prov(node),))

    def _rule_scope(self, node: ast.If, fn_id: str, fn_name: str,
                    prov, lines: list[str]) -> Iterator[IRRecord]:
        """A guard that narrows WHEN a rule applies, not WHAT its value is.

        `approval_service` runs BR-004 only when `request.category == "capex"`.
        The workflow states the same 5,000,000 and states no category at all,
        so comparing values alone reports the two as ALIGNED -- and the
        restriction that decides whether a five-million request reaches
        Finance is invisible.

        Scope is a separate axis from value, and it fails separately: two
        artifacts can agree on every number and still disagree about who the
        rule is for. That is a `scope_mismatch`, and the repair is to make
        the scope explicit rather than changing a number.
        """
        guards = []
        for test in _flatten_and(node.test):
            if not isinstance(test, ast.Compare) or len(test.ops) != 1:
                continue
            if not isinstance(test.ops[0], ast.Eq):
                continue
            comparator = test.comparators[0]
            if not isinstance(comparator, ast.Constant) or \
                    not isinstance(comparator.value, str):
                continue
            guards.append({"field": self._name_of(test.left),
                           "op": "==", "value": comparator.value})
        if not guards:
            return

        rule_ids = sorted({
            sub.value for sub in ast.walk(node)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
            and _RULE_ID.match(sub.value)})
        for rule_id in rule_ids:
            yield IRRecord(
                kind="entity",
                id=f"{fn_id}:rule_scope:{rule_id}:{getattr(node, 'lineno', 0)}",
                payload={"type": "rule_scope", "function": fn_name,
                         "rule_id": rule_id, "guards": guards,
                         "scope": "; ".join(
                             f'{g["field"]} == "{g["value"]}"' for g in guards),
                         #: The real line, not a sentence about it. A quote
                         #: that describes a line cannot be verified against
                         #: the artifact, so by I4 it is not evidence -- the
                         #: same mistake that has already had to be corrected once.
                         "quote": (lines[node.lineno - 1].strip()
                                   if 0 < node.lineno <= len(lines) else "")},
                provenance=(prov(node),))

    def _comparison(self, node: ast.Compare, fn_id: str, fn_name: str,
                    config_params: set[str], prov) -> Iterator[IRRecord]:
        for op, comparator in zip(node.ops, node.comparators):
            symbol = _THRESHOLD_OPS.get(type(op))
            if symbol is None:
                continue
            left_is_config = self._is_config_read(node.left, config_params)
            right_is_config = self._is_config_read(comparator, config_params)

            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, int) \
                    and not isinstance(comparator.value, bool) \
                    and comparator.value >= _MIN_INTERESTING_LITERAL:
                yield IRRecord(
                    kind="entity",
                    id=f"{fn_id}:literal_threshold:{getattr(comparator, 'lineno', 0)}",
                    payload={
                        "type": "hardcoded_threshold",
                        "function": fn_name,
                        "field": self._name_of(node.left),
                        "op": symbol,
                        "value": comparator.value,
                        "unit": "",
                        #: The claim, stated plainly so a reader of the IR
                        #: does not have to infer it: this number came from
                        #: the source, not from the policy.
                        "from_config": False,
                    },
                    provenance=(prov(comparator),))

            elif left_is_config or right_is_config:
                attribute = self._attr_of(node.left if left_is_config else comparator)
                yield IRRecord(
                    kind="entity",
                    id=f"{fn_id}:config_threshold:{getattr(node, 'lineno', 0)}",
                    payload={"type": "config_threshold", "function": fn_name,
                             "op": symbol, "attribute": attribute, "from_config": True},
                    provenance=(prov(node),))

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _config_aliases(node: ast.FunctionDef, config_params: set[str]) -> set[str]:
        """Local names bound to something read out of the config.

        One pass, no fixed point: `a = config.x; b = a` is not followed. Two
        levels of indirection on a policy value is rare enough that guessing
        at it would cost more precision than it buys recall -- and `traps.md`
        says so out loud rather than leaving it to be discovered.
        """
        aliases: set[str] = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign) or len(sub.targets) != 1:
                continue
            target = sub.targets[0]
            if isinstance(target, ast.Name) and PythonAdapter._is_config_read(
                    sub.value, config_params):
                aliases.add(target.id)
        return aliases

    @staticmethod
    def _imported_names(node) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        return [node.module] if node.module else []

    @staticmethod
    def _callee_name(func: ast.AST) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    @staticmethod
    def _name_of(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    @staticmethod
    def _attr_of(node: ast.AST) -> str:
        """What the config value was called where it was compared.

        Includes the plain local name, because with alias tracking the thing
        being compared is usually `threshold`, not `config.threshold(...)` --
        and an empty attribute would leave the record unable to say WHICH
        policy value the comparison was about.
        """
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Call):
            return PythonAdapter._callee_name(node.func)
        if isinstance(node, ast.Name):
            return node.id
        return ""

    @staticmethod
    def _is_config_read(node: ast.AST, config_params: set[str]) -> bool:
        """Does this expression reach into the injected config?

        Walks the attribute/call chain to its root so `config.rules["BR-001"]`
        and `config.threshold("BR-001")` both count -- a check that only knew
        `config.x` would call the second one a hardcode.
        """
        current = node
        for _ in range(8):
            if isinstance(current, ast.Name):
                return current.id in config_params
            if isinstance(current, ast.Attribute):
                current = current.value
            elif isinstance(current, ast.Call):
                current = current.func
            elif isinstance(current, ast.Subscript):
                current = current.value
            else:
                return False
        return False


ADAPTER = PythonAdapter()
