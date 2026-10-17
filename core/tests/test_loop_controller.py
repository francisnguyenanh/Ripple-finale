"""P41 §8.3 -- what is left of the loop tier after the part that never
ran was removed.

`execute_loop1/2/3` and `run_loop_with_convergence_guard` had tests here
and zero production callers, which is the shape this phase exists to
find: the tests exercised the FUNCTION and nothing exercised the PATH to
it, so "implemented" and "runs" looked the same from the outside.

`classify_change` is the one piece `core/kernel/feedback.py` actually
calls, so it is the one piece still tested.
"""

from core.kernel.loop_controller import classify_change


def test_wording_only_change_classified_as_loop1():
    c = classify_change(is_wording_only=True, requires_km_regeneration=False, is_new_ruleset=False)
    assert c.loop == "loop1_inplace"


def test_meaning_change_classified_as_loop2():
    c = classify_change(is_wording_only=False, requires_km_regeneration=True, is_new_ruleset=False)
    assert c.loop == "loop2_regenerate"


def test_ruleset_change_classified_as_loop3():
    c = classify_change(is_wording_only=False, requires_km_regeneration=False, is_new_ruleset=True)
    assert c.loop == "loop3_rule_change"


def test_an_unclassifiable_change_defaults_to_the_safer_path():
    """Neither wording-only nor a declared regeneration: the default is
    loop2, not loop1. Guessing "wording only" would skip re-deriving
    meaning that may well have changed."""
    c = classify_change(is_wording_only=False, requires_km_regeneration=False, is_new_ruleset=False)
    assert c.loop == "loop2_regenerate"
