"""Step 8.10 — final core boundary checks, run as the very last gate
before calling the framework itself done.
"""

import ast
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CORE_DIR = os.path.join(REPO_ROOT, "core")

PROJECT_NAMES = ("fixture_cobol", "fixture_jobscript", "fixture_noop", "paycalc", "taxcalc")
_ABS_PATH_RE = re.compile(r"^[A-Za-z]:\\\\|^/home/|^/Users/")


def _iter_core_py_files(exclude_tests=True):
    for dirpath, _dirs, files in os.walk(CORE_DIR):
        if exclude_tests and (os.sep + "tests" in dirpath + os.sep):
            continue
        for fname in files:
            if fname.endswith(".py"):
                yield os.path.join(dirpath, fname)


def test_no_project_specific_constants_in_core():
    hits = []
    for path in _iter_core_py_files():
        text = open(path, encoding="utf-8").read().lower()
        for name in PROJECT_NAMES:
            if name in text:
                hits.append((path, name))
    assert hits == []


def test_no_llm_in_forbidden_layers():
    forbidden_paths = [
        os.path.join(CORE_DIR, "kernel"),
        os.path.join(CORE_DIR, "adapters"),
        os.path.join(CORE_DIR, "agent", "arbiter.py"),
    ]
    banned_terms = ("openai", "anthropic")  # "llm"/"prompt" appear legitimately in docstrings
                                             # stating the boundary rule itself (documented in
                                             # every prior phase's status doc) — real API vendor
                                             # names would never have a legitimate reason to appear.
    hits = []
    targets = []
    for p in forbidden_paths:
        if os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                for fname in files:
                    if fname.endswith(".py"):
                        targets.append(os.path.join(dirpath, fname))
        elif os.path.isfile(p):
            targets.append(p)

    for path in targets:
        text = open(path, encoding="utf-8").read().lower()
        for term in banned_terms:
            if term in text:
                hits.append((path, term))
    assert hits == []


def test_no_absolute_paths_in_core():
    hits = []
    for path in _iter_core_py_files():
        for lineno, line in enumerate(open(path, encoding="utf-8"), start=1):
            if _ABS_PATH_RE.match(line.strip()):
                hits.append(f"{path}:{lineno}")
    assert hits == []


def test_adapters_only_import_the_interface_not_kernel_internals():
    adapters_dir = os.path.join(CORE_DIR, "adapters")
    allowed_kernel_imports = {
        "core.kernel.serialize",  # shared canonical serialize helper is fine
    }
    hits = []
    for dirpath, _dirs, files in os.walk(adapters_dir):
        if os.sep + "tests" in dirpath + os.sep:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                module = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name
                if module and module.startswith("core.kernel") and module not in allowed_kernel_imports:
                    hits.append(f"{path}: imports {module}")
    assert hits == []
