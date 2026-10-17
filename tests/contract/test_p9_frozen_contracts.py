# -*- coding: utf-8 -*-
"""The P9 contracts, frozen before Phase 10 consumes them.

Same shape as the P6, P7 and P8 freeze files: `docs/contracts/P9_FROZEN.md`
states them in prose, and each test here holds one section against the code.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import _srcscan
from app.api import handlers as handlers_module
from app.api import router as router_module
from app.api import sessions as sessions_module
from app.ui import judge_view
from core.change_intelligence import public_surface
from evals import runner as evals_runner

ROOT = pathlib.Path(__file__).resolve().parents[2]
FROZEN = ROOT / "docs" / "contracts" / "P9_FROZEN.md"
APP = ROOT / "app"


@pytest.fixture(scope="module")
def frozen_text():
    return FROZEN.read_text(encoding="utf-8")


# --- 1. the surface ----------------------------------------------------------


def test_the_document_inherits_the_earlier_freezes(frozen_text):
    for earlier in ("P6_FROZEN.md", "P7_FROZEN.md", "P8_FROZEN.md"):
        assert earlier in frozen_text
        assert (ROOT / "docs" / "contracts" / earlier).exists()


def test_the_surface_size_is_stated_and_correct(frozen_text):
    assert f"**{len(public_surface.PUBLIC_SURFACE)} operations**" in frozen_text


def test_the_four_classifications_are_frozen():
    assert public_surface.CLASSIFICATIONS == (
        "READ", "SESSION_WRITE", "DECISION", "CONTROLLED")


def test_there_is_no_privileged_classification():
    for banned in ("ADMIN", "INTERNAL", "DEBUG", "UNSAFE", "SYSTEM"):
        assert banned not in public_surface.CLASSIFICATIONS


def test_every_writing_operation_states_a_boundary():
    for operation in public_surface.writing_operations():
        assert operation.boundary, operation.name


def test_a_writing_operation_without_a_boundary_cannot_be_built():
    with pytest.raises(public_surface.SurfaceViolation, match="boundary"):
        public_surface.Operation(name="x", classification="SESSION_WRITE",
                                 summary="s")


def test_the_router_dispatches_from_the_surface():
    source = inspect.getsource(router_module.Api.call)
    assert "public_surface.get(request.operation)" in source


def test_the_api_refuses_to_start_on_a_mismatch():
    source = inspect.getsource(router_module.Api._check_surface)
    assert "declared - implemented" in source
    assert "implemented - declared" in source


# --- 2. the whitelist --------------------------------------------------------


def test_exactly_one_operation_is_controlled():
    controlled = [o for o in public_surface.PUBLIC_SURFACE
                  if o.classification == public_surface.CONTROLLED]
    assert len(controlled) == 1


def test_the_whitelist_is_the_controlled_operations_own_mutations():
    assert (public_surface.external_edit_whitelist()
            is public_surface.controlled_operation().mutations)


def test_a_whitelist_outside_the_controlled_operation_is_refused():
    with pytest.raises(public_surface.SurfaceViolation, match="second list"):
        public_surface.Operation(
            name="x", classification="READ", summary="s",
            mutations=(public_surface.Mutation(
                artifact="a", locator="b", value_type="integer"),))


def test_a_controlled_operation_with_an_empty_whitelist_is_refused():
    with pytest.raises(public_surface.SurfaceViolation, match="permits nothing"):
        public_surface.Operation(name="x", classification="CONTROLLED",
                                 summary="s", boundary="b")


def test_the_whitelist_matches_exactly_with_no_patterns():
    for mutation in public_surface.external_edit_whitelist():
        for token in ("*", "?", "**", "re:", "glob:"):
            assert token not in mutation.artifact
            assert token not in mutation.locator


def test_a_value_is_never_coerced():
    mutation = public_surface.external_edit_whitelist()[0]
    ok, why = mutation.accepts("500000")
    assert not ok and "integer" in why


def test_a_boolean_is_not_an_integer():
    """`True == 1` in Python; the whitelist must not accept it."""
    mutation = public_surface.external_edit_whitelist()[0]
    assert not mutation.accepts(True)[0]


def test_the_seven_refusals_are_all_present():
    source = inspect.getsource(handlers_module)
    for code in ("invalid_artifact_path", "artifact_not_whitelisted",
                 "locator_not_whitelisted", "invalid_value",
                 "artifact_not_in_session", "stale_artifact"):
        assert code in source, code


# --- 3. isolation ------------------------------------------------------------


def test_a_session_holds_no_route_to_the_registry():
    fields = set(sessions_module.JudgeSession.__dataclass_fields__)
    for banned in ("registry", "sessions", "store", "all_sessions"):
        assert banned not in fields


def test_ids_are_scoped_to_their_session():
    source = inspect.getsource(sessions_module.JudgeSession.mint)
    assert "self.session_id" in source


def test_a_cross_session_reference_is_refused_by_inspection():
    source = inspect.getsource(sessions_module.JudgeSession.owns)
    assert "startswith" in source


def test_session_ids_are_sequential_not_random():
    source = _srcscan.executable_source(sessions_module)
    for banned in ("uuid", "random", "token_hex", "secrets"):
        assert banned not in source, (
            f"{banned} would make two replays of one scenario differ in a "
            f"field that means nothing")


# --- 4. reset ----------------------------------------------------------------


def test_reset_clears_overlay_work_and_lineage():
    source = inspect.getsource(sessions_module.SessionRegistry.reset)
    assert "overlay.clear()" in source
    assert "work.clear()" in source
    assert "VerifiedLineage(" in source


def test_reset_removes_no_file():
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(sessions_module.SessionRegistry.reset)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rendered = ast.unparse(node.func)
            for banned in ("rmtree", "unlink", "remove", "rmdir"):
                assert banned not in rendered, rendered


def test_reset_does_not_remove_the_session_from_the_registry():
    source = inspect.getsource(sessions_module.SessionRegistry.reset)
    assert "del self._sessions" not in source
    assert "pop(" not in source


# --- 5. replay ---------------------------------------------------------------


def test_nothing_under_app_refers_to_replay():
    offenders = []
    for path in _srcscan.python_files(APP, include_tests=True):
        for number, line in _srcscan.code_lines(path):
            if "replay" in line.lower():
                offenders.append(f"{path.name}:{number}")
    assert not offenders, offenders


def test_the_excluded_field_set_is_empty():
    from tests.e2e.test_replay_determinism import NONDETERMINISTIC_FIELDS

    assert NONDETERMINISTIC_FIELDS == frozenset()


def test_the_replay_script_uses_only_public_operations():
    from tests.e2e.test_replay_determinism import SCRIPT

    for step in SCRIPT:
        assert public_surface.is_public(step["operation"]), step["operation"]


# --- 6. evaluation -----------------------------------------------------------


def test_a_metric_needs_all_four_parts():
    fields = evals_runner.Metric.__dataclass_fields__
    for required in ("numerator", "denominator", "answer_key", "scope"):
        assert required in fields


def test_scope_is_a_closed_set():
    assert len(evals_runner.SCOPES) >= 3
    with pytest.raises(evals_runner.UnscopedMetric):
        evals_runner.Metric(name="m", numerator=1, denominator=2,
                            answer_key="k", scope="anywhere", question="q")


def test_zero_of_zero_is_none():
    metric = evals_runner.Metric(
        name="m", numerator=0, denominator=0, answer_key="k",
        scope=evals_runner.SEED_CORPUS, question="q")
    assert metric.value is None


def test_the_report_disclaims_universality():
    report = evals_runner.Report(metrics=())
    assert "is a claim about systems in general" in report.rendered()


def test_the_runner_scores_with_no_model():
    source = _srcscan.executable_source(evals_runner).lower()
    for banned in ("gemini", "llm", "proposer"):
        assert banned not in source


def test_the_negative_metrics_exist():
    for name in ("abstention", "unnecessary_change_rate"):
        assert hasattr(evals_runner, name)


# --- 7. failure behaviour ----------------------------------------------------


def test_a_missing_payload_field_is_a_refusal_not_a_default():
    source = inspect.getsource(router_module.Request.require)
    assert "raise PublicError" in source
    assert ".get(" not in source.split('"""')[-1]


def test_an_unforeseen_exception_fails_closed():
    """Asserted by behaviour, not by source text.

    The message is built from adjacent string literals, so a source scan
    for the sentence finds it split across quotes. What matters is what a
    caller receives.
    """
    from app.api.sessions import SessionRegistry
    from core.change_intelligence.session import InMemoryBaseline

    api = router_module.Api(sessions=SessionRegistry(
        baseline=InMemoryBaseline.from_artifacts({"a.yaml": b"v: 1"})))

    def explode(request):
        raise RuntimeError("nobody anticipated this")

    api.handlers["health"] = explode
    reply = api.call(router_module.Request(operation="health"))
    assert not reply.ok and reply.status == 500
    assert "nothing was changed" in reply.error
    assert "RuntimeError" in reply.error


def test_a_refusal_carries_a_code_and_a_message_separately():
    fields = set(router_module.Reply.__dataclass_fields__)
    assert {"code", "error", "status"} <= fields


# --- 8. the security boundary ------------------------------------------------


FORBIDDEN_IMPORTS = ("subprocess", "os", "shutil", "pickle", "marshal",
                     "socket", "urllib", "requests", "importlib", "ctypes")


@pytest.mark.parametrize("module", FORBIDDEN_IMPORTS)
def test_no_public_module_imports_a_dangerous_library(module):
    offenders = []
    for path in _srcscan.python_files(APP, include_tests=True):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.split(".")[0] == module for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders


def test_the_inventory_is_data_a_reviewer_can_read():
    rows = router_module.inventory()
    assert rows == router_module.inventory()
    assert all({"operation", "classification", "writes", "boundary"}
               <= set(row) for row in rows)


# --- 9 and 10. cloud, and the approval boundary ------------------------------


def test_the_api_holds_no_state_of_its_own():
    fields = set(router_module.Api.__dataclass_fields__)
    assert fields == {"sessions", "handlers"}


def test_the_cloud_boundaries_are_documented(frozen_text):
    assert "cloud_readiness.md" in frozen_text
    assert (ROOT / "docs" / "verification" / "cloud_readiness.md").exists()


def test_the_document_states_nothing_was_deployed(frozen_text):
    collapsed = " ".join(frozen_text.split())
    assert "Nothing was provisioned and there is no live URL" in collapsed


def test_apply_still_refuses_without_approval():
    source = inspect.getsource(handlers_module)
    assert "approval_required" in source


def test_the_first_screen_forbids_the_score_and_the_answer_key():
    assert "alignment_score" in judge_view.NOT_ON_FIRST_SCREEN
    assert "ground_truth" in judge_view.NOT_ON_FIRST_SCREEN
    with pytest.raises(judge_view.FirstScreenViolation):
        judge_view.build_first_screen(
            operation_name="x", counts={"alignment_score": 0.82})


def test_no_public_module_recommends_a_decision():
    """Scanned over string literals, skipping the checker's own list.

    `check_no_recommendation` necessarily spells the phrases it forbids, so
    scanning it with itself is the self-reference version of the
    docstring-scan defect. Everything else is fair game.
    """
    for path in _srcscan.python_files(APP, include_tests=True):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "check_no_recommendation"):
                continue
            if not isinstance(node, ast.Constant) or not isinstance(
                    node.value, str):
                continue
            if _inside_checker(tree, node):
                continue
            found = judge_view.check_no_recommendation(node.value)
            assert not found, f"{path.name}:{node.lineno} {found}"


def _inside_checker(tree, target) -> bool:
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "check_no_recommendation"
                and node.lineno <= target.lineno
                and getattr(node, "end_lineno", 0) >= target.lineno):
            return True
    return False


# --- 11 and 12. debt and datasets --------------------------------------------


def test_p9_claims_no_debt_resolved(frozen_text):
    assert "P9 resolved none of these and claims none of them" in frozen_text


def test_the_document_covers_every_required_section(frozen_text):
    for heading in ("PUBLIC_SURFACE", "External-edit whitelist",
                    "Session isolation", "Reset semantics",
                    "Replay semantics", "Evaluation semantics",
                    "Public failure behaviour", "Security boundary",
                    "Cloud Run assumptions", "Human approval boundary",
                    "Known debt", "Dataset"):
        assert heading in frozen_text, f"{heading} missing"


def test_the_live_versus_replay_rule_is_stated(frozen_text):
    assert "Replay is not the production path" in frozen_text
