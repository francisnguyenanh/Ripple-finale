# Synthetic ROI replay sets (HK-ROI) — SYNTHETIC DATA, not a real company's records

These sets verify the ROI replay engine (`core/change_intelligence/v2/roi_replay.py`, contract `docs/contracts/EXTENSION_GOVERNANCE.md` "Historical replay") end to end.
**ROI figures computed on them are simulation results on synthetic data. They are not observed or realised enterprise benefits.** No real data and no data owner
are involved. That is the hackathon profile chosen by the owner on 2026-10-27; the real-data completion gate of MON-ROI stays open for a real deployment.

## Files
| File | What |
|---|---|
| `generate.py` | Deterministic generator: fixed seeds, Python `random.Random`, stdlib only. `python tests/fixtures/roi_synthetic/generate.py [out_dir]` rewrites every file below byte for byte. |
| `oracle.py` | Expected results written **independently of the engine**. It imports neither `core` nor `infra` and does not use the rule grammar or the evaluator. The four Harbor policies are plain Python over the dataset columns; the data-quality rules, bands, effort and cost follow the contract text. |
| `scenarios.json` | The policies as the product stores them (baseline = the Harbor observed rules of `tests/fixtures/final_knowledge_loop/expected_knowledge.json`) and three targets, plus the declared assumptions. |
| `dev.json`, `acceptance.json` | Records (`ripple-roi-replay/1` schema: id, time, amount_jpy, currency, category, process, supplier_risk) and one case label per record. |
| `expected_dev.json`, `expected_acceptance.json` | The oracle's ground truth per scenario: denominators, both sides, changes by band, effort, cost, unsupported ids, excluded ids with reasons, coverage. |
| `MANIFEST.json` | Seeds, sizes, analysis window, sha256 of every generated file and of the two sources. |

## Schema, seeds and assumptions
**Window and seeds**
- Analysis window: 2026-04-01T00:00:00Z to 2026-10-01T00:00:00Z, **half-open** (`[from, to)`), the usual reading of a reporting period.
- Seeds: development 20261027 (300 generated records), acceptance 74151 (2,000 generated records). Each split also has a hand-listed edge list with its own ids, times and categories.

**Generated distributions**
- Amount bands are weighted <1M 35 %, 1–2M 35 %, 2–5M 20 %, >=5M 10 % (uniform inside a band; half of the amounts are rounded to 1,000).
- Category: OFFICE 40 / OPEX 25 / CAPEX 20 / IT 15. Process: STANDARD 85 / EMERGENCY 12 / URGENT 3 (URGENT is outside every rule's scope). Supplier risk: LOW 70 / MEDIUM 20 / HIGH 10.
- About 3 % of generated records fall before the window, and 1 % each carry a foreign currency, an invalid amount, a missing field or a repeated id (one defect per record).

**Policies**

| | Rules |
|---|---|
| Baseline | A standard purchase >= 1,000,000 JPY needs the manager. CAPEX >= 2,000,000 (standard or emergency) needs Finance. Standard CAPEX >= 5,000,000 needs Finance (same approver, counted once). A high-risk supplier (standard or emergency) needs the Director. The emergency routing rule has no condition and decides nothing. |
| S1 | Manager threshold 1,500,000 JPY (a TARGET rule replaces the observed one). |
| S2 | S1, plus office supplies below 2,000,000 JPY never need the manager, plus a vendor-country rule on a column the dataset lacks (coverage only). |
| S3 | S1 with an open question on the CAPEX-from-2,000,000 rule. Every standard / emergency row is undecidable on the target side, so it is unsupported and never a saving. An open question on the non-replayed emergency rule changes nothing. |

**Declared assumptions** (S1 / S2: 20 min per approval, range 10–30, hourly cost 6,000 JPY; S3: 15 min, range 12–25, no hourly cost, so cost is `not_estimated`) are declared inputs, not measurements.

## Case classes (each record carries one label)
| Class | Records |
|---|---|
| `normal` | Valid generated records. |
| `boundary` | Amounts at 999,999 / 1,000,000 / 1,499,999 / 1,500,000 / 1,999,999 / 2,000,000 / 4,999,999 / 5,000,000 / 0. Formatted amounts ("1,700,000", "1499999.99", 1500000.0). The window's first and end instants and one second either side, with and without an offset. |
| `missing` | No id, empty id, null category, empty risk, no time. |
| `contradictory` | A repeated id with other content (the first record stands) and an exact repeat. |
| `exception` | The S2 office exception (applies at 1,600,000; not at 2,000,000), the out-of-scope URGENT process, medium risk, emergency CAPEX with a high-risk supplier. |
| `invalid` | Negative, non-numeric, boolean, NaN and Infinity amounts; USD / EUR; an impossible date, a word, a number as the time. |
| `outside_window` | Generated records before the window. |
Every defective record has exactly one defect, so its exclusion reason is unambiguous.

## Leakage rule between development and acceptance
The splits are generated from different seeds, with different id prefixes (`D-` / `A-`). No record content (time, amount, category, process, risk) appears in both,
and the edge lists differ in ids, times and companion values. Engine defects are diagnosed on the development split only. The acceptance files and their expected
results are committed (hashes in `MANIFEST.json`) before the engine is run on them. A disagreement found on acceptance is reported, never "fixed" by editing the
expected values. `tests/final_loop/test_roi_synthetic_acceptance.py` checks reproducibility, leakage, class coverage, the oracle's independence and engine = oracle,
and runs the acceptance split through the real route.
