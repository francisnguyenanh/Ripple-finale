"""REL-02 (audit 2026-10-30): the worker never takes a write boundary of a leased job without its fence. Every call of `finish_execution`,
`begin_provider_attempt`, `record_attempt` and `cancel_execution` in the worker passes `fence=`; the store's write boundaries accept it; the only
fence-less terminal path is the operator tool, which is restricted to QUEUED jobs. Pure file test."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
BOUNDARIES = ("finish_execution", "begin_provider_attempt", "record_attempt", "cancel_execution")


def _calls(text: str, name: str):
    """Every `store.<name>(` call with its argument text (balanced parentheses)."""
    for m in re.finditer(r"store\." + name + r"\(", text):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        yield text[m.end():i - 1]


def test_every_worker_write_boundary_carries_the_fence():
    text = (ROOT / "infra" / "worker" / "job_worker.py").read_text(encoding="utf-8")
    text = re.sub(r'"""[\s\S]*?"""', "", text)                                   # docstrings describe the calls; only code is scanned
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    seen = {}
    for name in BOUNDARIES:
        calls = list(_calls(text, name))
        seen[name] = len(calls)
        for args in calls:
            assert "fence=" in args, f"store.{name}( without fence= in job_worker.py: {args[:120]!r}"
    assert seen["finish_execution"] >= 1 and seen["begin_provider_attempt"] >= 1 and seen["record_attempt"] >= 1 and seen["cancel_execution"] >= 1, seen


def test_store_boundaries_accept_the_fence_and_the_operator_path_is_queued_only():
    pg = (ROOT / "infra" / "adapters" / "v2_postgres.py").read_text(encoding="utf-8")
    for name in BOUNDARIES:
        sig = re.search(r"def " + name + r"\((.*?)\)\s*->", pg, re.S)
        assert sig and "fence" in sig.group(1), name
    assert "_assert_fence(cur, tenant_id, workspace_id, fence, alive=True)" in pg
    assert "_assert_fence(cur, tenant_id, workspace_id, fence, alive=False)" in pg
    ops = (ROOT / "tools" / "ops_jobs.py").read_text(encoding="utf-8")
    assert all("queued_only=True" in args for args in _calls(ops, "cancel_execution")), "the operator cancel must be restricted to QUEUED jobs"
