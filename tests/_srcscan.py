# -*- coding: utf-8 -*-
"""Source scanning shared by the architecture and boundary tests.

Both ask questions of the form "does this code mention X?", and both have
to answer about CODE rather than about prose. A rule that fires on a
comment is a rule under which the reasoning for the rule cannot be written
down -- and in this repository the reasoning is the part worth keeping.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re


def code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line number, code) with comments stripped and docstrings skipped."""
    out: list[tuple[int, str]] = []
    in_doc = False
    delim = ""
    text = path.read_text(encoding="utf-8", errors="replace")

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw
        if in_doc:
            if delim in line:
                in_doc = False
                line = line.split(delim, 1)[1]
            else:
                continue
        while True:
            match = re.search(r'("""|\'\'\')', line)
            if not match:
                break
            delim = match.group(1)
            rest = line[match.end():]
            if delim in rest:
                line = line[: match.start()] + rest.split(delim, 1)[1]
                continue
            line = line[: match.start()]
            in_doc = True
            break
        line = re.sub(r"#.*$", "", line)
        if line.strip():
            out.append((number, line))
    return out


def python_files(root: pathlib.Path, *, include_tests: bool = False) -> list[pathlib.Path]:
    return [
        p for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and (include_tests or "tests" not in p.parts)
    ]


def executable_source(module) -> str:
    """A module's code with every docstring removed, via the syntax tree.

    The line-based `code_lines` above answers the same question for a file.
    This one exists because the caller usually has a module object and
    wants one string to search.

    Why either is needed: a scan over raw text flags the docstring that
    FORBIDS the thing it looks for. "no randomness" contains "random",
    "legitimate" contains "git", "similarity" contains "similar". That has
    now been found five times in this repository under five names, so the
    scan belongs in one place and this is it.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))
