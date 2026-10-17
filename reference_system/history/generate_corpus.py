# -*- coding: utf-8 -*-
"""Generate the 220-record historical transaction corpus.

Deterministic: a fixed seed, so the same corpus comes back every run and
`transactions.jsonl` can be committed and diffed.

The corpus is DESIGNED; the headline numbers are COMPUTED from it. That
distinction is the whole point of it. Designing a distribution so that a
meaningful number falls out is honest work. Writing the number down and
building a corpus to match it afterwards is not, and a single invented
number would devalue every other number in the submission.

Distribution, and why it is shaped this way:

    170  outside the gap            behaviour cannot change
     47  in the gap, standard path  auto_approved -> manager_approval
      3  in the gap, EMERGENCY path behaviour does NOT change

So 50 records sit between 500,000 and 1,000,000, and exactly 47 of them
change. The three that do not are the hardcoded ceiling in
`emergency_purchase.py` becoming visible AS BEHAVIOUR -- a person reviewing
the change would say "50 requests are affected", and the system can say
"47 change and 3 do not, and here is why".
"""

from __future__ import annotations

import json
import pathlib
import random
import sys
from datetime import datetime, timedelta, timezone

SEED = 20260919
TOTAL = 220
IN_GAP_STANDARD = 47
IN_GAP_EMERGENCY = 3

SUPPLIERS = [f"SUP-{n:03d}" for n in range(1, 25)]
BUYERS = ["buyer.tanaka", "buyer.ito", "buyer.nakamura", "buyer.sato", "buyer.kimura"]

#: Twelve months back from the baseline date, so "approvals per month" is a
#: real series rather than an average dressed up as one.
END = datetime(2026, 9, 1, tzinfo=timezone.utc)
START = END - timedelta(days=365)


def _timestamp(rng: random.Random) -> str:
    return (START + timedelta(seconds=rng.randint(0, int((END - START).total_seconds())))).isoformat()


def _record(rng: random.Random, n: int, amount: int, process: str, tier: str,
            risk: str, category: str) -> dict:
    return {
        "historical_case_id": f"HIST-{n:04d}",
        "timestamp": _timestamp(rng),
        "amount": amount,
        "supplier_id": rng.choice(SUPPLIERS),
        "customer_tier": tier,
        "process": process,
        "category": category,
        "supplier_risk": risk,
        "requested_by": rng.choice(BUYERS),
    }


def build() -> list[dict]:
    rng = random.Random(SEED)
    records: list[dict] = []
    n = 0

    # --- 47 in the gap, standard path: these change ------------------------
    for _ in range(IN_GAP_STANDARD):
        n += 1
        records.append(_record(rng, n, rng.randrange(500_000, 1_000_000, 1_000),
                               "standard_po", "standard", "normal", "opex"))

    # --- 3 in the gap, emergency path: these do NOT change -----------------
    for _ in range(IN_GAP_EMERGENCY):
        n += 1
        records.append(_record(rng, n, rng.randrange(500_000, 1_000_000, 1_000),
                               "emergency_purchase", "standard", "normal", "opex"))

    # --- the rest, outside the gap -----------------------------------------
    #
    # Deliberately varied: strategic customers, high-risk suppliers, capex,
    # blanket orders. Their behaviour is unaffected by the BR-001 threshold,
    # which is what makes them useful -- an impact analysis that flags them
    # is over-reporting, and the negative evals need real negatives.
    remaining = TOTAL - len(records)
    for _ in range(remaining):
        n += 1
        roll = rng.random()
        if roll < 0.55:
            amount = rng.randrange(20_000, 500_000, 1_000)
            process, tier, risk, category = "standard_po", "standard", "normal", "opex"
        elif roll < 0.72:
            amount = rng.randrange(1_000_000, 5_000_000, 10_000)
            process, tier, risk, category = "standard_po", "standard", "normal", "opex"
        elif roll < 0.82:
            amount = rng.randrange(5_000_000, 20_000_000, 100_000)
            process, tier, risk, category = "standard_po", "standard", "normal", "capex"
        elif roll < 0.90:
            amount = rng.randrange(100_000, 3_000_000, 10_000)
            process, tier, risk, category = "blanket_order", "strategic", "normal", "opex"
        elif roll < 0.96:
            amount = rng.randrange(100_000, 2_000_000, 10_000)
            process, tier, risk, category = "standard_po", "standard", "high", "opex"
        else:
            amount = rng.randrange(20_000, 490_000, 1_000)
            process, tier, risk, category = "emergency_purchase", "standard", "normal", "opex"
        records.append(_record(rng, n, amount, process, tier, risk, category))

    records.sort(key=lambda r: r["timestamp"])
    # Re-number in chronological order so HIST-0001 really is the oldest.
    for i, record in enumerate(records, 1):
        record["historical_case_id"] = f"HIST-{i:04d}"
    return records


def main(out: str) -> None:
    records = build()
    path = pathlib.Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} records to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "reference_system/history/transactions.jsonl")
