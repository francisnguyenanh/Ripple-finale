# -*- coding: utf-8 -*-
"""Are the loop guards real, or do the suites pass for unrelated reasons?

Three mutations, each removing one thing the loop depends on, each
required to turn a suite red:

    1. stop advancing the version only on a pass    -> lineage tests red
    2. stop detecting missing edits                 -> verification red
    3. stop refusing internal event subscriptions   -> loop tests red

The third is the one worth arguing for. Subscribing the loop to an event
produced *inside* a turn creates a cycle, and a cycle does not show up in a
unit test -- it shows up when somebody runs the whole thing. So the guard
against it has to be proven by removing it.

Each mutation restores the file in a `finally`, and a test at the end
asserts nothing was left mutated.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "core" / "change_intelligence"

#: file -> (find, replace, suite that must go red, why it must)
MUTATIONS = {
    "the version advances even when verification failed": (
        PACKAGE / "orchestrator.py",
        "        if result.passed:",
        "        if True:  # MUTATED: advance regardless of the verdict",
        "tests/e2e/test_loop_intent_direction.py",
        "a version that advances on a failed verification says the state "
        "was confirmed when it was not"),
    "a missing edit is treated as applied": (
        PACKAGE / "verification.py",
        "        elif actual == edit.expected_current:",
        "        elif False:  # MUTATED: never report MISSING",
        "tests/contract/test_verification.py",
        "an approved change that did not land would verify as a pass"),
    "internal events may be subscribed to the loop": (
        PACKAGE / "events.py",
        "        if event_type in internal():",
        "        if False:  # MUTATED: allow internal events into the loop",
        "tests/contract/test_loop_events.py",
        "an event produced inside a turn would re-enter the loop"),
}


# EXT ?17 uses this same restore-in-finally harness, not a second runner.
_API = ROOT / "app" / "api" / "handlers.py"
_EXT = "tests/e2e/test_extension_reconciliation.py"
_E2 = "tests/e2e/test_e2_changeset_path.py"
MUTATIONS.update({
    "EXT candidate hash check removed": (_API,
        "if candidate.content_hash != content_hash:", "if False:", _EXT,
        "a different candidate could be confirmed"),
    "EXT invalid candidate self certified": (_API,
        "not reasons, tuple(reasons),", "True, tuple(reasons),", _EXT,
        "ambiguous input could become an accepted intent"),
    "EXT injection scan removed": (_API,
        'findings = scan_for_injection(text.encode("utf-8"), "user:intent_text")',
        'findings = []', _EXT, "the model would see unscanned input"),
    "EXT apply writes no artifacts": (PACKAGE / "apply_gate.py",
        "snapshot = overlay.write_all(staged) if staged else overlay.head",
        "snapshot = overlay.head", _E2, "APPLIED would claim a nonexistent write"),
    "EXT verification falsely passes": (_API,
        "        if result.passed:\n            session.lineage.record_verified(",
        "        result = dataclasses.replace(result, alignment=type(result.alignment).ALIGNED, unresolved=[], blocked_by=[])\n        if result.passed:\n            session.lineage.record_verified(",
        _E2, "missing artifacts would be verified"),
    "EXT approval bypass": (_API,
        "        from core.change_intelligence.structured_diff import StaleDiff, validate_workflow",
        "        from core.change_intelligence.structured_diff import StaleDiff, validate_workflow\n        if request.payload.get('force'):\n            return {'state': 'APPLIED'}",
        "tests/security/test_e2_apply_gate_api.py", "force would bypass named approval"),
    "EXT mutation before whitelist": (_API,
        "        # 1. path traversal, and anything else that is not a plain name",
        "        session.overlay.write(artifact, b'premature mutation')\n        # 1. path traversal, and anything else that is not a plain name",
        "tests/security/test_public_surface.py", "a rejected request would mutate state"),
    "EXT loop call removed": (_API,
        "turn = session.orchestrator.run_reality_direction(",
        "turn = None if True else session.orchestrator.run_reality_direction(", _EXT,
        "an edit would never trigger the loop"),
    "EXT reconcile replaced with clean verdict": (_API,
        '"alignment": outcome.alignment.value, "decision": outcome.decision.value,',
        '"alignment": "ALIGNED", "decision": outcome.decision.value,', _EXT,
        "an external conflict would be hidden"),
    "EXT fresh bus every request": (_API,
        "        if session.orchestrator is None:", "        if True:", _EXT,
        "cross-request bus history would disappear"),
    "EXT apply publishes before validation completes": (PACKAGE / "apply_gate.py",
        "        staged[artifact] = raw", "        staged[artifact] = raw\n        overlay.write(artifact, raw)",
        "tests/contract/test_apply_atomicity.py", "a later artifact failure would expose an earlier write"),
    "EXT durable commit failure swallowed": (ROOT / "app" / "main.py",
        '            code = "persistence_failed"', '            ok = True\n            code = "persistence_failed"',
        "tests/e2e/test_durable_commit.py", "an uncommitted write would report success"),
    "EXT SQL CAS removed": (ROOT / "infra" / "adapters" / "cloud_adapters.py",
        "if expected_version is not None and current != expected_version:", "if False:",
        "tests/contract/test_extension_persistence.py", "a stale session would overwrite current state"),
    "EXT durable event key dropped": (_API,
        'session.put("handled_event", key,', 'session.put("discarded_event", key,', _EXT,
        "event deduplication would vanish after restart"),
})



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

def _run(suite: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", suite, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_removing_a_loop_guard_turns_its_suite_red(name):
    path, find, replace, suite, why = MUTATIONS[name]
    original = path.read_bytes()
    source = original.decode("utf-8").replace("\r\n", "\n")

    assert source.count(find) == 1, (
        f"the mutation for {name!r} no longer matches exactly once; a "
        f"mutation test that silently mutates nothing reports a green suite "
        f"as evidence of safety")

    try:
        path.write_bytes(source.replace(find, replace).encode("utf-8"))
        result = _run(suite)
        assert result.returncode != 0, (
            f"{name}: the suite stayed GREEN, so {why} is not what makes "
            f"those tests pass:\n{result.stdout[-2000:]}")
    finally:
        _restore(path, original)

    assert path.read_bytes() == original


def test_the_suites_are_green_before_any_mutation():
    """The control. A red baseline makes every result above meaningless."""
    for suite in sorted({item[3] for item in MUTATIONS.values()}):
        result = _run(suite)
        assert result.returncode == 0, (
            f"{suite} is already failing:\n{result.stdout[-1500:]}")


def test_nothing_was_left_mutated():
    for path, _, _, _, _ in MUTATIONS.values():
        assert "MUTATED" not in path.read_text(encoding="utf-8"), path
