"""Job kind "change.risk_memo" (SUR-06): owner of the frozen config `config/change_risk_memo.json` for the job binding. The executor is
`infra.worker.advisory_memo.RiskMemoExecutor`; it loads its config through `load_config` below, so the binding and the run read the same file."""
from __future__ import annotations

from infra.worker import advisory_memo

KIND = ROLE = advisory_memo.RISK_KIND


def load_config() -> tuple[dict, str]:
    return advisory_memo.load_config(KIND)
