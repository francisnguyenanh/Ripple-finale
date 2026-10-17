# -*- coding: utf-8 -*-
"""Metrics that cannot be quoted without their scope.

The dishonesty this file is built to prevent is not fabrication. It is the
much easier move of measuring something narrow and reporting it as
something broad: a figure from one seed corpus, printed on a slide as
"accuracy". Every defence here is structural rather than editorial -- the
scope lives inside the object, the constructor refuses a free-text scope,
and `rendered()` prints it beside the number.

The negative metrics matter as much as the positive ones. Any system finds
something if nobody counts what it finds that is not there.
"""

from __future__ import annotations

import pytest

from evals.runner import (
    IMPACT_KEY,
    SCOPES,
    SEED_CORPUS,
    SESSION_TESTS,
    Metric,
    Report,
    UnscopedMetric,
    abstention,
    build_report,
    exact_match,
    introduced_vs_inherited,
    precision,
    recall,
    summary,
    unnecessary_change_rate,
)


def _metric(**kwargs):
    base = dict(name="m", numerator=1, denominator=2,
                answer_key="hand-written", scope=SEED_CORPUS,
                question="q")
    base.update(kwargs)
    return Metric(**base)


# --- a metric cannot exist without its four parts ---------------------------


def test_a_metric_without_an_answer_key_is_refused():
    with pytest.raises(UnscopedMetric, match="whatever the system said"):
        _metric(answer_key="")


def test_a_metric_with_a_free_text_scope_is_refused():
    """Free text is how a seed-local figure becomes "system accuracy"."""
    with pytest.raises(UnscopedMetric, match="quoted as universal"):
        _metric(scope="our tests")


def test_every_declared_scope_names_the_dataset():
    for scope in SCOPES:
        assert len(scope.split()) > 2, scope


def test_a_numerator_larger_than_its_denominator_is_refused():
    with pytest.raises(UnscopedMetric, match="more than all of them"):
        _metric(numerator=3, denominator=2)


def test_negative_counts_are_refused():
    with pytest.raises(UnscopedMetric):
        _metric(numerator=-1)


# --- nothing measured is not a perfect score --------------------------------


def test_zero_of_zero_is_not_a_score():
    metric = _metric(numerator=0, denominator=0)
    assert metric.value is None
    assert not metric.measured


def test_an_unmeasured_metric_says_so_rather_than_printing_a_figure():
    text = _metric(numerator=0, denominator=0).rendered()
    assert "not measured" in text
    assert "0.00" not in text and "1.00" not in text


def test_a_report_counts_its_unmeasured_metrics():
    report = Report(metrics=(_metric(numerator=0, denominator=0),
                             _metric()))
    assert len(report.unmeasured) == 1
    assert len(report.measured) == 1


# --- the scope travels with the number --------------------------------------


def test_the_rendering_prints_the_scope_beside_the_number():
    text = _metric().rendered()
    assert "0.50" in text
    assert SEED_CORPUS in text
    assert "key:" in text


def test_the_report_says_the_figures_are_not_universal():
    text = Report(metrics=(_metric(),)).rendered()
    assert "is a claim about systems in general" in text


def test_the_public_summary_carries_a_scope_for_every_metric():
    data = summary()
    assert data["metrics"]
    for entry in data["metrics"]:
        assert entry["scope"] in SCOPES
        assert entry["question"]
    assert "not a claim about systems in general" in data["disclaimer"]


# --- the metric definitions --------------------------------------------------


def test_precision_counts_what_was_reported():
    metric = precision("p", found={"a", "b", "c"}, expected={"a", "b"},
                       answer_key="k", scope=SEED_CORPUS, question="q")
    assert (metric.numerator, metric.denominator) == (2, 3)


def test_recall_counts_what_was_real():
    metric = recall("r", found={"a"}, expected={"a", "b"},
                    answer_key="k", scope=SEED_CORPUS, question="q")
    assert (metric.numerator, metric.denominator) == (1, 2)


def test_finding_nothing_gives_a_recall_of_zero_not_an_error():
    metric = recall("r", found=set(), expected={"a", "b"},
                    answer_key="k", scope=SEED_CORPUS, question="q")
    assert metric.value == 0.0


def test_unnecessary_change_counts_the_systems_mistakes():
    """Low is good, and the numerator is what it got wrong.

    Phrased so a planner cannot improve its score by proposing more.
    """
    metric = unnecessary_change_rate(
        proposed={"a", "b", "c"}, required={"a"},
        answer_key="k", scope=SEED_CORPUS)
    assert (metric.numerator, metric.denominator) == (2, 3)
    assert metric.notes == ("b", "c")


def test_proposing_only_what_is_required_scores_zero():
    metric = unnecessary_change_rate(
        proposed={"a"}, required={"a", "b"}, answer_key="k",
        scope=SEED_CORPUS)
    assert metric.value == 0.0


def test_abstention_rewards_knowing_when_not_to_answer():
    metric = abstention(
        ambiguous_cases={"GAP-1", "GAP-2"}, abstained_on={"GAP-1"},
        answer_key="k", scope=SEED_CORPUS)
    assert (metric.numerator, metric.denominator) == (1, 2)


def test_abstention_is_reported_separately_from_the_numerator():
    """Folding it in would let an abstention inflate recall."""
    metric = abstention(
        ambiguous_cases={"GAP-1"}, abstained_on={"GAP-1", "GAP-9"},
        answer_key="k", scope=SEED_CORPUS)
    assert metric.numerator == 1
    assert metric.abstained == 2


def test_introduced_and_inherited_are_scored_against_a_key():
    metric = introduced_vs_inherited(
        classified={"C1": "introduced", "C2": "introduced"},
        key={"C1": "introduced", "C2": "inherited"},
        answer_key="k", scope=SEED_CORPUS)
    assert (metric.numerator, metric.denominator) == (1, 2)


def test_blaming_the_change_for_an_inherited_problem_costs_a_point():
    metric = introduced_vs_inherited(
        classified={"C1": "introduced"}, key={"C1": "inherited"},
        answer_key="k", scope=SEED_CORPUS)
    assert metric.value == 0.0


def test_a_case_the_system_did_not_classify_counts_against_it():
    metric = introduced_vs_inherited(
        classified={}, key={"C1": "inherited"}, answer_key="k",
        scope=SEED_CORPUS)
    assert metric.value == 0.0


def test_exact_match_compares_case_by_case():
    metric = exact_match(
        "v", observed={"A": "ALIGNED", "B": "DRIFT"},
        expected={"A": "ALIGNED", "B": "CONFLICT"},
        answer_key="k", scope=SEED_CORPUS, question="q")
    assert (metric.numerator, metric.denominator) == (1, 2)


# --- the runner --------------------------------------------------------------


def test_the_runner_fetches_no_ground_truth_of_its_own():
    """It could otherwise fetch a key from the thing it is scoring."""
    import ast
    import inspect

    from evals import runner

    tree = ast.parse(inspect.getsource(runner.build_report))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rendered = ast.unparse(node.func)
            for banned in ("read_text", "load", "analyse", "discover", "run"):
                assert not rendered.endswith(banned), (
                    f"build_report calls {rendered}; a runner that fetches "
                    f"its own key can fetch it from the system under test")


def test_a_report_carries_the_lineage_of_what_it_measured():
    report = build_report(
        {"impact_found": {"a"}}, {"impact_expected": {"a"}},
        baseline_id="BASE-0001", corpus_lineage="seed at commit abc123")
    assert "seed at commit abc123" in report.rendered()


def test_metrics_appear_only_when_there_are_observations():
    report = build_report({}, {})
    assert report.metrics == ()


def test_positive_and_negative_metrics_sit_in_one_table():
    report = build_report(
        {"impact_found": {"a"}, "actions_proposed": {"a", "b"},
         "abstained_on": {"GAP-1"}},
        {"impact_expected": {"a"}, "actions_required": {"a"},
         "ambiguous_cases": {"GAP-1"}})
    names = {m.name for m in report.metrics}
    assert "impact recall" in names
    assert "unnecessary-change rate" in names
    assert "correct abstention" in names


def test_nothing_in_the_runner_asks_a_model_to_score():
    import _srcscan

    from evals import runner

    source = _srcscan.executable_source(runner).lower()
    for banned in ("gemini", "llm", "proposer", "generate("):
        assert banned not in source, (
            f"the evaluator reaches for {banned}; a model may explain a "
            f"finding, it may not decide whether the finding was correct")


def test_the_report_is_deterministic():
    observations = {"impact_found": {"b", "a"}}
    keys = {"impact_expected": {"a", "b"}}
    first = build_report(observations, keys).rendered()
    second = build_report(observations, keys).rendered()
    assert first == second
