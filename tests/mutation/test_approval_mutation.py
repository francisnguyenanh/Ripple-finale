# -*- coding: utf-8 -*-
"""Is the gate real, or is it an `if` nobody has watched fail?

`tests/security/test_approval_bypass.py` shows that an unapproved change is
refused. That is necessary and it is not sufficient: it would pass equally
well if the refusal came from somewhere incidental, and it says nothing
about whether the suite would NOTICE if the check were removed tomorrow.

So this file removes it. Each mutation deletes one authorisation check from
`apply_gate.py`, runs the security suite against the mutated module, and
asserts the suite goes red. Then it restores the file and asserts that it
did.

The step most projects skip is the fourth one -- checking that the tests
fail. A mutation that produces a green suite means the test was passing for
a reason unrelated to the thing it claims to check, and every gate report
built on it is worth nothing.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "core" / "change_intelligence" / "apply_gate.py"
BYPASS_SUITE = "tests/security/test_approval_bypass.py"

#: Each entry removes ONE authorisation check by replacing the call with a
#: no-op comment. Line-for-line so the file's shape and every line number
#: below it stay put -- a mutation that also shifted the source would make
#: an unrelated failure look like the one being tested for.
MUTATIONS = {
    "no approval check": (
        "    check_approved(changeset)",
        "    pass  # MUTATED: approval check removed"),
    "no version check": (
        "    check_version(changeset)",
        "    pass  # MUTATED: stale-approval check removed"),
    "no snapshot check": (
        "    check_snapshot(changeset, current_snapshot=getattr(",
        "    _ = 0 and check_snapshot(changeset, current_snapshot=getattr("),
}



def _restore(path, original: bytes) -> None:
    """Write the original bytes back, and PROVE it.

    On Windows the restore write can fail with OSError (Errno 22) right after
    the child suite ran: something (a virus scanner, an editor's file watcher)
    briefly holds the just-modified file. A failed restore left mutations in
    product code on 2026-09-25 (`findings = []`, `discarded_event`), which then
    shipped into a later edit. Retry for up to ~10 s; fail loudly otherwise.
    """
    import time
    last = None
    for _ in range(100):
        try:
            path.write_bytes(original)
            if path.read_bytes() == original:
                return
        except OSError as exc:
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"could not restore {path}; run `git checkout -- {path}`") from last

def _run_suite() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", BYPASS_SUITE, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)


@pytest.fixture
def restorable():
    """Hand back the original bytes whatever happens, and prove it."""
    original = GATE.read_bytes()
    try:
        yield original
    finally:
        _restore(GATE, original)
    assert GATE.read_bytes() == original


def test_the_suite_is_green_before_any_mutation():
    """The control. A red baseline would make every result below meaningless."""
    result = _run_suite()
    assert result.returncode == 0, (
        f"the security suite is already failing, so a red result after a "
        f"mutation would prove nothing:\n{result.stdout[-2000:]}")


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_removing_an_authorisation_check_turns_the_suite_red(name, restorable):
    original = restorable
    target, replacement = MUTATIONS[name]
    source = original.decode("utf-8")

    assert source.count(target) == 1, (
        f"the mutation for {name!r} no longer matches the source exactly "
        f"once; a mutation test that silently mutates nothing reports a "
        f"green suite as evidence of safety")

    GATE.write_bytes(source.replace(target, replacement).encode("utf-8"))
    result = _run_suite()

    assert result.returncode != 0, (
        f"removing the {name} left the suite GREEN. The check is not what "
        f"makes those tests pass, so they are not testing it:\n"
        f"{result.stdout[-2000:]}")


def test_the_file_is_restored_after_the_mutations():
    """Run last by name ordering within the file, and asserted explicitly.

    A mutation suite that leaves the repository mutated has done more
    damage than the defect it was looking for.
    """
    source = GATE.read_text(encoding="utf-8")
    assert "MUTATED" not in source
    assert "check_approved(changeset)" in source
    assert "check_version(changeset)" in source


def test_the_mutation_list_covers_every_check_the_gate_makes():
    """A check with no mutation is a check nobody has tested the absence of."""
    import ast

    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "apply_changeset")
    checks = {ast.unparse(node.func) for node in ast.walk(function)
              if isinstance(node, ast.Call)
              and ast.unparse(node.func).startswith("check_")}

    mutated = {target.strip().split("(")[0].replace("_ = 0 and ", "")
               for target, _ in MUTATIONS.values()}
    assert checks == mutated, (
        f"apply_changeset makes {sorted(checks)} but only {sorted(mutated)} "
        f"have a mutation. Adding a gate without adding its mutation means "
        f"nobody has confirmed the new gate does anything.")


# --- mutations 4, 5, 6 (SS17) -- the public HANDLER, not the gate -
#
# Same technique as above (patch source text, run a suite, assert red,
# restore, assert restored) applied to a different file and a different
# suite -- reusing the mechanism rather than building a second one. These
# three are the ones §17 singles out as the batch's central risk: E2 is
# "the one place where doing it badly still LOOKS like it worked", and
# mutation 4 in particular (delete the apply_changeset call, still report
# APPLIED) is the exact shape of the Finding-7 defect this whole plan exists
# to close.

HANDLERS = ROOT / "app" / "api" / "handlers.py"
E2_SUITES = ["tests/e2e/test_e2_changeset_path.py",
             "tests/security/test_e2_apply_gate_api.py"]

HANDLER_MUTATIONS = {
    "apply_changeset call removed, still reports APPLIED": (
        '        try:\n'
        '            applied, result = apply_changeset(',
        '        if True:  # MUTATED: apply_changeset is never called\n'
        '            return {"changeset_id": changeset_id, "state": "APPLIED",\n'
        '                    "written": [], "human_actions": [], "notes": [],\n'
        '                    "snapshot_id": session.snapshot.snapshot_id,\n'
        '                    "parent_snapshot_id": None}\n'
        '        try:\n'
        '            applied, result = apply_changeset('),
    "verify() replaced with a stub that always passes": (
        '        try:\n'
        '            result = verify(\n'
        '                changeset=changeset, snapshot_before=snapshot_before,\n'
        '                snapshot_after=snapshot_after, parse=_parse_for_verification,\n'
        '                read_at=_read_at)',
        '        try:\n'
        '            import types as _t  # MUTATED: verify() is never called\n'
        '            result = _t.SimpleNamespace(\n'
        '                passed=True, alignment=_t.SimpleNamespace(value="ALIGNED"),\n'
        '                reason="MUTATED", unresolved=[], blocked_by=[], dimensions={})'),
    "AUTO_SAFE in the payload skips apply_gate.decide": (
        '        try:\n'
        '            applied, result = apply_changeset(',
        '        if request.payload.get("readiness") == "AUTO_SAFE":\n'
        '            # MUTATED: bypass approval when the caller claims AUTO_SAFE\n'
        '            return {"changeset_id": changeset_id, "state": "APPLIED",\n'
        '                    "written": [], "human_actions": [], "notes": [],\n'
        '                    "snapshot_id": session.snapshot.snapshot_id,\n'
        '                    "parent_snapshot_id": None}\n'
        '        try:\n'
        '            applied, result = apply_changeset('),
    # Mutation 15 -- a public handler that skips a required write:
    # a changeset that names a spreadsheet representation as a required
    # action must actually write it, or "the review screen promised two
    # edits, one landed" is invisible to verification. Removing the
    # spreadsheet branch of `_system_edits_for` reproduces exactly that:
    # apply still reports success (the workflow half still lands), but
    # `tests/e2e/test_e2_changeset_path.py::test_an_approved_artifact_that_
    # was_never_written_cannot_verify_as_aligned` must catch the untouched
    # spreadsheet and turn the suite red.
    "spreadsheet system edit is never generated, only workflow": (
        '            factory = cell_edit if rep.artifact.endswith(".xlsx") else StructuredEdit',
        '            if rep.artifact.endswith(".xlsx"): continue  # MUTATED: spreadsheet edit dropped\n'
        '            factory = cell_edit if rep.artifact.endswith(".xlsx") else StructuredEdit'),
}


def _run_e2_suites() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", *E2_SUITES, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)


@pytest.fixture
def restorable_handlers():
    original = HANDLERS.read_bytes()
    try:
        yield original
    finally:
        _restore(HANDLERS, original)
    assert HANDLERS.read_bytes() == original


def test_the_e2_suites_are_green_before_any_handler_mutation():
    """The control for the handler mutations below."""
    result = _run_e2_suites()
    assert result.returncode == 0, (
        f"the E2 suites are already failing, so a red result after a "
        f"mutation would prove nothing:\n{result.stdout[-2000:]}")


@pytest.mark.parametrize("name", sorted(HANDLER_MUTATIONS))
def test_a_handler_mutation_turns_the_e2_suites_red(name, restorable_handlers):
    original = restorable_handlers
    target, replacement = HANDLER_MUTATIONS[name]
    # handlers.py is CRLF on this checkout; the targets above are written
    # with plain \n for readability. Normalise before matching, and write
    # the mutated file back as \n -- Python does not care, and `restorable_
    # handlers` restores the exact original bytes (CRLF included)
    # regardless of what this test writes in between.
    source = original.decode("utf-8").replace("\r\n", "\n")

    assert source.count(target) == 1, (
        f"the mutation for {name!r} no longer matches handlers.py exactly "
        f"once; a mutation test that silently mutates nothing (or mutates "
        f"more than intended) reports a green suite as evidence of safety")

    HANDLERS.write_bytes(source.replace(target, replacement).encode("utf-8"))
    result = _run_e2_suites()

    assert result.returncode != 0, (
        f"{name} left the E2 suites GREEN. Whatever those tests are "
        f"checking, it is not this:\n{result.stdout[-3000:]}")


def test_handlers_is_restored_after_the_handler_mutations():
    source = HANDLERS.read_text(encoding="utf-8")
    assert "MUTATED" not in source
    assert "apply_changeset(" in source
    assert "result = verify(" in source
