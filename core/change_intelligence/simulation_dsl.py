# -*- coding: utf-8 -*-
"""Deterministic policy DSL -- parser/operator allowlist, never
`eval`/`exec`/`compile`.

This is a deterministic-policy expression language for the
"config-only" simulation adapter (`payload.adapter == "deterministic_dsl"`)
that can NEVER execute arbitrary Python -- `parse()` walks the expression's
AST (built by `ast.parse(..., mode="eval")`, which only ever produces a parse
tree, not executable bytecode) and rejects any node shape or identifier
outside a fixed allowlist BEFORE the expression is ever evaluated. `evaluate()`
then walks that same tree with a hand-written recursive interpreter -- it
never calls `eval`/`exec`/`compile` on the source or the tree, so a rejected
node can never reach a real Python execution path.

Allowed grammar: boolean `and`/`or`/`not`, comparisons (`==`, `!=`, `<`,
`<=`, `>`, `>=`), literal constants (str/int/float/bool/None), and bare
names drawn from `ALLOWED_FIELDS` (the corpus record fields the
records use: amount/currency/channel/exception). No function calls,
attribute access, subscripting, imports, or any other node kind is ever
permitted -- `__import__('os').system(...)` is an `ast.Call` node, which is
not in the allowlist, so `parse()` rejects it explicitly, never silently
ignores it.
"""

from __future__ import annotations

import ast

ALLOWED_FIELDS = frozenset({"amount", "currency", "channel", "exception"})

_ALLOWED_NODE_TYPES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Name,
    ast.Load, ast.Constant, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq,
    ast.NotEq, ast.UnaryOp, ast.Not, ast.In, ast.NotIn,
)


MAX_EXPRESSION_CHARS = 2000
MAX_DEPTH = 40


class DslSyntaxError(ValueError):
    """A DSL expression uses a node kind or identifier outside the allowlist,
    or fails to parse as an expression at all. Raised at validation time --
    never silently ignored, never treated as a no-op policy."""


def parse(expression: str) -> ast.Expression:
    """Validate `expression` against the allowlist and return its AST.

    Raises `DslSyntaxError` for anything outside the allowed grammar
    (function calls, attribute access, imports, assignments, comprehensions,
    etc.) -- including the specific `__import__(...)`/`os.system(...)`
    shape, which is an `ast.Call` and is rejected by the node-type check
    alone, no special-casing required.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise DslSyntaxError("policy_expression must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise DslSyntaxError(f"policy_expression is longer than {MAX_EXPRESSION_CHARS} characters")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise DslSyntaxError(f"policy_expression is not valid syntax: {exc}") from exc
    except (RecursionError, MemoryError, ValueError) as exc:      # a hostile shape fails closed with the named error, never a crash
        raise DslSyntaxError("policy_expression is too deeply nested or too large") from exc
    stack = [(tree, 1)]
    while stack:                                                  # iterative depth bound: evaluate() recurses, so its depth is capped here
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise DslSyntaxError(f"policy_expression is nested deeper than {MAX_DEPTH} levels")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODE_TYPES):
            raise DslSyntaxError(
                f"policy_expression uses a disallowed construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in ALLOWED_FIELDS:
            raise DslSyntaxError(f"policy_expression references an unknown field: {node.id!r}")
    return tree


def evaluate(tree: ast.Expression, record: dict) -> bool:
    """Evaluate an already-`parse()`d tree against one corpus record.

    A hand-written recursive walk -- never `eval`/`exec`/`compile`. Only
    called on a tree that has already passed `parse()`'s allowlist check.
    """

    def _ev(node):
        if isinstance(node, ast.Expression):
            return _ev(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return record.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not _ev(node.operand)
        if isinstance(node, ast.BoolOp):
            values = [_ev(v) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = _ev(node.left)
            ok = True
            for op, comparator in zip(node.ops, node.comparators):
                right = _ev(comparator)
                ok = ok and _apply_op(op, left, right)
                left = right
            return ok
        raise DslSyntaxError(f"unsupported node at evaluation: {type(node).__name__}")

    return bool(_ev(tree))


def _apply_op(op: ast.cmpop, left, right) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    raise DslSyntaxError(f"unsupported comparison operator: {type(op).__name__}")


__all__ = ["ALLOWED_FIELDS", "DslSyntaxError", "parse", "evaluate"]
