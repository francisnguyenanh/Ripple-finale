# -*- coding: utf-8 -*-
"""The three tests the seed planted, found without being told.

`pytest.ini` records that TEST-OLD-001..003 exist and that they are green
today and red once the threshold moves. That is the answer key. This file
checks that the product finds them from the change alone -- no marker read,
no name matched, no list maintained anywhere.

The marker is deliberately NOT used to find them. A detector that read
`@pytest.mark.seed_outdated` would be reading the answer off the back of
the card, and would find nothing at all in a real codebase.
"""

from __future__ import annotations

import pathlib

import pytest

from core.change_intelligence.test_proposal import (
    describe,
    render,
    stale_tests,
)

SEED = pathlib.Path(__file__).resolve().parents[2] / "reference_system"
SUITE = SEED / "tests" / "test_approval_rules.py"
ARTIFACT = "tests/test_approval_rules.py"

CURRENT = 1_000_000
PROPOSED = 500_000


@pytest.fixture(scope="module")
def source():
    return SUITE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def stale(source):
    return stale_tests(source, artifact=ARTIFACT, current_value=CURRENT,
                       proposed_value=PROPOSED)


def test_the_seeded_outdated_tests_are_found(stale):
    names = {t.function for t in stale}
    for expected in ("test_old_001_requests_under_a_million_are_always_automatic",
                     "test_old_002_a_seven_hundred_fifty_thousand_request_needs_no_approval",
                     "test_old_003_managers_see_nothing_below_one_million"):
        assert expected in names, f"{expected} was not detected"


def test_the_detection_does_not_read_the_marker():
    """The marker is the answer key, not the method."""
    import inspect

    from core.change_intelligence import test_proposal

    source = inspect.getsource(test_proposal)
    assert "seed_outdated" not in source, (
        "the detector reads the marker the seed planted; it would find "
        "nothing in a codebase nobody had labelled for it")


def test_the_detection_does_not_match_on_the_function_name():
    import inspect

    from core.change_intelligence import test_proposal

    source = inspect.getsource(test_proposal.stale_tests)
    assert "_old_" not in source, (
        "the detector matches the seed's naming convention rather than "
        "the change itself")


def test_each_finding_names_a_line_and_quotes_it(stale):
    """I4: evidence is the line, not a sentence about the line."""
    lines = SUITE.read_text(encoding="utf-8").splitlines()
    for test in stale:
        assert test.quote
        assert test.quote == lines[test.line - 1].strip(), (
            f"{test.function}: the quote is not the line at {test.line}")


def test_each_finding_says_which_amounts_put_it_in_range(stale):
    for test in stale:
        assert test.values
        assert all(PROPOSED <= v < CURRENT for v in test.values)


def test_a_test_asserting_the_boundary_itself_is_not_flagged():
    """It is about to become correct. Sending somebody to read it is noise."""
    source = (
        "def test_at_the_boundary(config):\n"
        "    assert route(1000000) == MANAGER\n")
    assert stale_tests(source, artifact="t.py", current_value=CURRENT,
                       proposed_value=PROPOSED) == ()


def test_a_test_outside_the_range_is_not_flagged():
    source = (
        "def test_well_above(config):\n"
        "    assert route(9000000) == FINANCE\n")
    assert stale_tests(source, artifact="t.py", current_value=CURRENT,
                       proposed_value=PROPOSED) == ()


def test_a_number_in_a_comment_does_not_flag_a_test():
    """The AST is why. A regex would match the explanation."""
    source = (
        "def test_something(config):\n"
        "    # 750000 used to be automatic, which is why this exists\n"
        "    assert route(9000000) == FINANCE\n")
    assert stale_tests(source, artifact="t.py", current_value=CURRENT,
                       proposed_value=PROPOSED) == ()


def test_a_number_in_a_docstring_does_not_flag_a_test():
    source = (
        "def test_something(config):\n"
        '    """Not about 750000 at all."""\n'
        "    assert route(9000000) == FINANCE\n")
    assert stale_tests(source, artifact="t.py", current_value=CURRENT,
                       proposed_value=PROPOSED) == ()


def test_a_non_test_function_is_not_flagged():
    source = (
        "def helper(config):\n"
        "    return route(750000)\n")
    assert stale_tests(source, artifact="t.py", current_value=CURRENT,
                       proposed_value=PROPOSED) == ()


def test_raising_the_threshold_finds_the_same_range(source):
    """Direction does not matter; the affected range does."""
    down = stale_tests(source, artifact=ARTIFACT, current_value=CURRENT,
                       proposed_value=PROPOSED)
    up = stale_tests(source, artifact=ARTIFACT, current_value=PROPOSED,
                     proposed_value=CURRENT)
    assert {t.function for t in down} == {t.function for t in up}


def test_a_change_that_changes_nothing_finds_nothing(source):
    assert stale_tests(source, artifact=ARTIFACT, current_value=CURRENT,
                       proposed_value=CURRENT) == ()


def test_the_findings_are_ordered_by_line(stale):
    assert [t.line for t in stale] == sorted(t.line for t in stale)


def test_the_detection_is_deterministic(source):
    first = stale_tests(source, artifact=ARTIFACT, current_value=CURRENT,
                        proposed_value=PROPOSED)
    second = stale_tests(source, artifact=ARTIFACT, current_value=CURRENT,
                         proposed_value=PROPOSED)
    assert first == second


def test_the_summary_says_what_range_it_looked_at(stale):
    text = describe(stale, current_value=CURRENT, proposed_value=PROPOSED)
    assert "500,000" in text and "1,000,000" in text


def test_nothing_found_is_reported_as_nothing_found():
    assert "no test asserts" in describe((), current_value=CURRENT,
                                         proposed_value=PROPOSED)


def test_the_rendering_carries_the_quoted_line(stale):
    text = render(stale, current_value=CURRENT, proposed_value=PROPOSED)
    assert stale[0].quote in text


def test_nothing_here_writes_a_test_file():
    """The generation half is out of scope, and must stay out of it."""
    import inspect

    from core.change_intelligence import test_proposal

    source = inspect.getsource(test_proposal)
    for banned in ("write_text", "write_bytes", "open("):
        assert banned not in source
