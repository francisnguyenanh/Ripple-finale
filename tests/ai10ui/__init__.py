"""AI10 browser-journey harness.

`contract.py` is the ONE place that knows the shell URL, hash routes, locators and the
user-step helpers. `harness.py` is the hermetic infrastructure (isolated Postgres database,
in-memory blob seam, deterministic provider seam, in-process uvicorn server + worker thread,
Playwright). `fixtures.py` holds the pytest fixtures both test directories import.
"""
