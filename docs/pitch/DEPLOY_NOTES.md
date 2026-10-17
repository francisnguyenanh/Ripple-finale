# Deploy notes (no identifiers, no secrets)
- Container: `Dockerfile` + pinned `requirements.txt`; one uvicorn worker; the UI is served by the same service.
- Budget/config comes from a mounted file (`RIPPLE_BUDGET_CONFIG_PATH`); model ids per role are pinned there (`extraction`, `change.draft` on the Vertex GLOBAL endpoint; the executor refuses an unpinned role or a non-global endpoint).
- A separate worker process runs queued jobs; model calls only run for jobs a second person approved; the ledger books the worst case and settles on usage.
- Release order used: deploy a revision with no traffic, authenticated smoke, one small canary, then the flow.
- Browser origin: set `RIPPLE_JUDGE_ORIGIN` to the exact URL users open.
