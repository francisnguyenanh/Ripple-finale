"""Server-generated correlation (AH-05). Two context variables, read where durable records are written:

* `REQUEST_ID`  the HTTP request ID the guard middleware generates for every request (random; an incoming X-Request-Id is never trusted or echoed);
* `PARENT_JOB`  the job a worker is executing, so a job created by that execution is linked to its parent.

They are metadata only: job identity, idempotency keys, deduplication and authorization never read them. Context variables follow the work into threads
started with anyio / Starlette's thread helpers (the context is copied)."""
from __future__ import annotations

import contextvars

REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("ripple_request_id", default="")
PARENT_JOB: contextvars.ContextVar[str] = contextvars.ContextVar("ripple_parent_job", default="")


def current() -> tuple[str | None, str | None]:
    return (REQUEST_ID.get() or None, PARENT_JOB.get() or None)
