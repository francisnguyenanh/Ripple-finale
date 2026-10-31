"""AI-01 step 2 (2026-10-31): prompts are rendered in ONE pass. The proof the plan asked for, in three parts: (1) the single-pass rendering is
byte-identical to the historical chain for every input whose values carry no placeholder token of a later substitution (the inputs of every recorded
run: none carried one, which is what the stored prompt hashes of the evaluation runs were made from); (2) the historical chain expanded a placeholder
literal typed inside an earlier value, the single pass does not (the defect and its fix, on the question → knowledge shape of the Q&A builder);
(3) no enabled builder renders its prompt by a chain of replacements any more (a static read of the builders)."""
from __future__ import annotations

import pathlib
import random
import re
import string

from core.change_intelligence.v2 import prompt_render as pr

REPO = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE = "<question>\n{question}\n</question>\n<previous>\n{previous}\n</previous>\n<knowledge>\n{knowledge}\n</knowledge>\n<open>\n{open_questions}\n</open>\nAnswer as JSON."
NAMES = ("question", "previous", "knowledge", "open_questions")
_CHAIN = re.compile(r'\.replace\("\{[a-z_]+\}"[^)]*\)\s*\.replace\("\{[a-z_]+\}"')


def _value(rng, allow_tokens: bool) -> str:
    alphabet = string.ascii_letters + string.digits + " \n<>/{}&\"'-"
    s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
    if not allow_tokens:
        s = pr.PLACEHOLDER.sub("", s)
    return s


def test_single_pass_equals_the_chain_whenever_no_value_carries_a_later_placeholder():
    rng = random.Random(20261031)
    for _ in range(2000):
        values = {n: _value(rng, allow_tokens=False) for n in NAMES}
        pairs = [(n, values[n]) for n in NAMES]
        assert pr.render_once(TEMPLATE, values) == pr.render_sequentially(TEMPLATE, pairs)
    # a template placeholder absent from the values stays as written in both renderings
    assert pr.render_once("a {question} b {missing}", {"question": "q"}) == "a q b {missing}" == pr.render_sequentially("a {question} b {missing}", [("question", "q")])


def test_the_chain_expanded_a_typed_placeholder_and_the_single_pass_does_not():
    typed = "What does {knowledge} say about refunds?"                         # a person typed the two characters and the word
    values = {"question": typed, "previous": "(none)", "knowledge": "K1 rule text", "open_questions": "(none)"}
    chained = pr.render_sequentially(TEMPLATE, [(n, values[n]) for n in NAMES])
    once = pr.render_once(TEMPLATE, values)
    assert "What does K1 rule text say" in chained                             # the defect: the question grew the knowledge section's text inside itself
    assert "What does {knowledge} say" in once and once.count("K1 rule text") == 1
    assert chained != once


def test_no_enabled_builder_chains_placeholder_replacements():
    builders = list((REPO / "core/change_intelligence/v2").glob("*.py")) + list((REPO / "infra/worker").glob("*.py"))
    offenders = []
    for path in builders:
        text = path.read_text(encoding="utf-8")
        if _CHAIN.search(text.replace("\\\n", " ")):
            offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == [], offenders
