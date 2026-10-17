# -*- coding: utf-8 -*-
"""I9 -- there is no path from a model's output to a document.

The temptation is real and the cost is specific. A language model writes a
better paragraph than a template does. What it costs is the property that
makes the documentation worth having: that every sentence traces to a record
with evidence behind it.

Half of this file tests the mechanism. The other half is a source scan,
because the mechanism can be correct and still be bypassed by the next
person who is in a hurry, and the scan is what notices.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.change_intelligence.doc_proposal import (
    UntracedSentence,
    commit_update,
    evidence_for_value,
    proposed_document_edit,
    render_document,
    update_knowledge,
)
from core.change_intelligence.evidence import EvidenceRef
from core.change_intelligence.knowledge import KnowledgeModel, KnowledgeRecord
from core.change_intelligence.states import KnowledgeState

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "core" / "change_intelligence"

SEMANTIC_ID = "approval_threshold"


def _record(value=1000000, km_version=1):
    return KnowledgeRecord(
        id="KR-1", knowledge_type="business_rule", semantic_id=SEMANTIC_ID,
        statement=f"Requests at or above {value} require manager sign-off.",
        evidence=(EvidenceRef(artifact="workflow/approval_routes.yaml",
                              locator="L37", quote=f"value: {value}"),),
        derived_from=("workflow/approval_routes.yaml",),
        state=KnowledgeState.VERIFIED, km_version=km_version)


@pytest.fixture
def model():
    return KnowledgeModel(records=[_record()])


def _update(model, value=500000):
    return update_knowledge(
        model, semantic_id=SEMANTIC_ID, new_value=value,
        statement=f"Requests at or above {value} require manager sign-off.",
        evidence=(evidence_for_value("workflow/approval_routes.yaml", "L37",
                                     f"value: {value}"),),
        actor="ops.lead", timestamp="2026-09-20T10:00:00Z",
        reason="an approved intent lowered the threshold")


# --- the order is fixed ------------------------------------------------------


def test_the_knowledge_model_is_updated_before_anything_is_rendered(model):
    update = _update(model)
    assert update.replacement.statement
    assert update.replacement.km_version == 2


def test_the_old_record_is_superseded_not_deleted(model):
    commit_update(model, _update(model))
    superseded = [r for r in model.records
                  if r.state is KnowledgeState.SUPERSEDED]
    assert len(superseded) == 1
    assert "1000000" in superseded[0].statement, (
        "what it said before is gone; a reader of a changed document asks "
        "exactly that")


def test_the_superseded_record_says_who_superseded_it_and_why(model):
    commit_update(model, _update(model))
    superseded = next(r for r in model.records
                      if r.state is KnowledgeState.SUPERSEDED)
    assert "ops.lead" in superseded.reason
    assert "lowered the threshold" in superseded.reason


def test_the_new_record_names_the_one_it_replaces(model):
    update = _update(model)
    assert "KR-1" in update.replacement.derived_from, (
        "lineage broken: the chain back to the original evidence does not "
        "survive the revision")


def test_a_knowledge_update_without_evidence_is_refused(model):
    with pytest.raises(UntracedSentence, match="without a quotable line"):
        update_knowledge(model, semantic_id=SEMANTIC_ID, new_value=1,
                         statement="s", evidence=(), actor="a",
                         timestamp="t", reason="r")


def test_a_knowledge_update_without_an_actor_is_refused(model):
    with pytest.raises(UntracedSentence, match="who made it"):
        update_knowledge(model, semantic_id=SEMANTIC_ID, new_value=1,
                         statement="s",
                         evidence=(evidence_for_value("a", "b", "c"),),
                         actor="", timestamp="t", reason="r")


def test_a_document_change_for_a_rule_the_model_never_heard_of_is_refused(model):
    """The proposal must come FROM the model. Otherwise it came from elsewhere."""
    with pytest.raises(UntracedSentence, match="must come FROM the model"):
        update_knowledge(model, semantic_id="not_a_rule", new_value=1,
                         statement="s",
                         evidence=(evidence_for_value("a", "b", "c"),),
                         actor="a", timestamp="t", reason="r")


# --- rendering ---------------------------------------------------------------


def test_the_rendered_document_carries_the_new_value(model):
    commit_update(model, _update(model))
    rendered = render_document(model, artifact="docs/policy.md",
                               title="Procurement policy")
    assert "500000" in rendered.text
    assert "1000000" not in rendered.text


def test_every_rendered_line_traces_to_a_record(model):
    rendered = render_document(model, artifact="docs/policy.md", title="P")
    assert rendered.is_traceable
    assert rendered.from_records == ("KR-1",)


def test_the_source_of_each_statement_appears_in_the_document(model):
    rendered = render_document(model, artifact="docs/policy.md", title="P")
    assert "workflow/approval_routes.yaml" in rendered.text
    assert "value: 1000000" in rendered.text


def test_a_record_with_no_evidence_is_not_rendered(model):
    model.records.append(KnowledgeRecord(
        id="KR-2", knowledge_type="business_rule", semantic_id="other",
        statement="Something nobody sourced.", evidence=(),
        derived_from=(), state=KnowledgeState.VERIFIED))
    rendered = render_document(model, artifact="docs/policy.md", title="P")
    assert "nobody sourced" not in rendered.text
    assert rendered.notes and "indistinguishable from" in rendered.notes[0]


def test_rendering_is_byte_identical_across_two_runs(model):
    """What makes the P8 check possible: re-render and compare."""
    first = render_document(model, artifact="d.md", title="P").text
    second = render_document(model, artifact="d.md", title="P").text
    assert first == second


def test_rendering_does_not_depend_on_the_order_records_are_stored_in(model):
    model.records.append(KnowledgeRecord(
        id="KR-3", knowledge_type="business_rule", semantic_id="aaa",
        statement="Another rule.",
        evidence=(EvidenceRef(artifact="a.yaml", locator="L1", quote="q"),),
        derived_from=(), state=KnowledgeState.VERIFIED))
    ordered = render_document(model, artifact="d.md", title="P").text
    model.records.reverse()
    reversed_ = render_document(model, artifact="d.md", title="P").text
    assert ordered == reversed_


def test_a_superseded_record_is_not_rendered(model):
    commit_update(model, _update(model))
    rendered = render_document(model, artifact="d.md", title="P")
    assert rendered.text.count("manager sign-off") == 1


def test_the_edit_reports_whether_the_document_actually_changes(model):
    rendered, changed = proposed_document_edit(
        model, artifact="d.md", title="P", current_text="something else")
    assert changed
    _, unchanged = proposed_document_edit(
        model, artifact="d.md", title="P", current_text=rendered.text)
    assert not unchanged


# --- I9: the source scan -----------------------------------------------------


def _writes_markdown(tree) -> list:
    """Calls that write a .md file, found in the syntax tree."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node)
        if (".write_text(" in rendered or ".write_bytes(" in rendered
                or rendered.startswith("open(")):
            if ".md" in rendered:
                hits.append(rendered[:120])
    return hits


def test_no_module_in_the_package_writes_a_markdown_file():
    offenders = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        hits = _writes_markdown(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        f"{offenders} write markdown directly. A document is rendered from "
        f"the Knowledge Model through the apply path, so that every line in "
        f"it traces to a record (I9)")


def test_the_renderer_never_calls_a_proposer_or_a_model():
    """The renderer takes knowledge and produces text. Nothing else."""
    import inspect

    from core.change_intelligence import doc_proposal

    for function in (doc_proposal.render_document, doc_proposal.render_section):
        source = inspect.getsource(function)
        for banned in ("proposer", "generate", "complete(", "llm", "gemini"):
            assert banned not in source.lower(), (
                f"{function.__name__} reaches for {banned!r}; then a line in "
                f"the document does not trace to a record and a reader has "
                f"to treat every line as untraceable")


def test_update_knowledge_takes_no_free_text_shortcut():
    """A statement arrives WITH evidence or it does not arrive."""
    import inspect

    from core.change_intelligence import doc_proposal

    signature = inspect.signature(doc_proposal.update_knowledge)
    assert "evidence" in signature.parameters
    assert signature.parameters["evidence"].default is inspect.Parameter.empty, (
        "evidence is optional; then a caller in a hurry omits it")
