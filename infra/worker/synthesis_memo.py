"""Job kind "ma.synthesis_memo" (SUR-08): owner of the frozen config `config/ma_synthesis_memo.json` for the job binding. The executor is
`infra.worker.advisory_memo.SynthesisMemoExecutor`; it loads its config through `load_config` below, so the binding and the run read the same file."""
from __future__ import annotations

from infra.worker import advisory_memo

KIND = ROLE = advisory_memo.SYNTH_KIND


def load_config() -> tuple[dict, str]:
    return advisory_memo.load_config(KIND)
