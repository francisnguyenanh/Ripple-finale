"""Leaf module: the executor base types shared by `infra.worker.job_worker` and every executor module.

Import-cycle rationale: `job_worker` defined these types, then imported every executor module (to build
`BUILTIN_EXECUTORS`) while each executor module imported these types back from `job_worker`. Whichever worker module
was imported FIRST decided whether the cycle resolved, so `infra.worker.verification` imported first (the lazy import
in `POST /v2/jobs`) raised a circular ImportError. These types now live here, with no `infra.worker` imports, and
`job_worker` re-exports them unchanged so existing `from infra.worker.job_worker import ...` callers keep working.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from infra.adapters.v2_postgres import PostgresV2Store

#: Observable reason (durable in `v2_jobs.execution_reason` / attempt rows); re-exported by `job_worker`.
REASON_CALL_CEILING = "provider_call_ceiling_exceeded"


class ProviderExecutionError(Exception):
    """A RETRYABLE provider/execution failure (timeout, invalid JSON, rate
    limit are the subclasses below)."""


class ProviderTimeoutError(ProviderExecutionError):
    pass


class ProviderInvalidResponseError(ProviderExecutionError):
    pass


class ProviderRateLimitError(ProviderExecutionError):
    pass


def is_timeout_exception(exc: BaseException, code=None) -> bool:
    """A provider timeout is the builtin TimeoutError, an HTTP 408/504 code, an httpx timeout
    (ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout: what the genai SDK raises on its request
    timeout) or a google.api_core DeadlineExceeded. Matched by class, never by message text."""
    if isinstance(exc, TimeoutError) or code in (408, 504):
        return True
    classes = []
    try:
        import httpx
        classes.append(httpx.TimeoutException)
    except ImportError:
        pass
    try:
        from google.api_core import exceptions as gexc
        classes.append(gexc.DeadlineExceeded)
    except ImportError:
        pass
    return bool(classes) and isinstance(exc, tuple(classes))


class NonRetryableExecutionError(Exception):
    """An execution failure a retry cannot fix; ends FAILED immediately."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class ProviderCallCeilingExceeded(NonRetryableExecutionError):
    def __init__(self, limit: int, attempted: int):
        super().__init__(REASON_CALL_CEILING,
                         f"attempted provider call #{attempted} exceeds "
                         f"max_provider_calls_per_job={limit}")


class ProviderCallBudget:
    """The provider-call ceiling enforced AT THE CALL SITE: an executor calls
    `consume()` immediately before each real provider call; the call that
    would exceed `limit` raises before it is made. `used` is the real
    per-attempt count recorded on the attempt row."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def consume(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise ProviderCallCeilingExceeded(self.limit, self.used + n)
        self.used += n


@dataclass(frozen=True)
class ExecutionRequest:
    """What an executor intends to do. `prompt` is the text that WOULD be
    sent to a model (None for a deterministic executor); it is hashed, and
    never persisted raw."""

    prompt: str | None
    schema: Mapping | None
    provider_calls_planned: int
    external: bool


@dataclass(frozen=True)
class ExecutionOutcome:
    result: dict
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None


@dataclass
class ExecutionContext:
    store: PostgresV2Store
    job: dict
    config: object | None
    request_context: dict = field(default_factory=dict)
    # The claim this execution runs under, as
    # `(outbox event id, worker id, fencing token)`; an executor with a
    # durable side effect passes it to the store so the write is fenced.
    lease: tuple | None = None

    @property
    def max_output_tokens(self) -> int | None:
        """Budget contract §7.1: the value a real provider executor must pass
        as the provider's own max-output-tokens request parameter."""
        cap = getattr(self.config, "generation_max_output_tokens", None)
        return cap if cap else getattr(self.config, "max_output_tokens", None)
