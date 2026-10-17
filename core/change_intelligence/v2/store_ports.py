"""Storage contracts; domain code does not import a database or GCS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class WorkspaceHead:
    tenant_id: str
    workspace_id: str
    version: int
    source_revision_id: str | None
    knowledge_revision_id: str | None
    decision_epoch: int
    policy_version: str


@dataclass(frozen=True)
class PublishResult:
    version: int
    revision_id: str
    replayed: bool = False


@dataclass(frozen=True)
class OutboxClaim:
    id: int
    tenant_id: str
    workspace_id: str
    kind: str
    payload: Mapping[str, object]
    fencing_token: int
    #: The row's attempt count AFTER this claim (i.e. including
    #: the current attempt). Lets a worker decide retry-vs-exhausted without a
    #: second query; defaults to 1 so any pre-existing caller that constructs
    #: an `OutboxClaim` positionally without this field still works.
    attempts: int = 1


class ImmutableBlobPort(Protocol):
    """External immutable object storage, e.g. GCS; no SQL blob bytes."""

    def put_immutable(self, digest: str, content: bytes) -> None: ...
    def read(self, digest: str) -> bytes: ...


class VersionedStorePort(Protocol):
    def head(self, tenant_id: str, workspace_id: str) -> WorkspaceHead: ...

    def publish_source(self, tenant_id: str, workspace_id: str, revision_id: str,
                       manifest: Mapping[str, str], *, expected_version: int,
                       operation: str, idempotency_key: str) -> PublishResult: ...

    def publish_knowledge(self, tenant_id: str, workspace_id: str, revision_id: str,
                          source_revision_id: str, decision_epoch: int, *,
                          expected_version: int, idempotency_key: str) -> PublishResult: ...

    def record_decision(self, tenant_id: str, workspace_id: str, decision_id: str,
                        actor_id: str, payload: Mapping[str, object], *,
                        expected_version: int, idempotency_key: str) -> PublishResult: ...

    def create_job(self, tenant_id: str, workspace_id: str, job_id: str,
                   kind: str, payload: Mapping[str, object], *,
                   expected_version: int, idempotency_key: str) -> PublishResult: ...

    def claim_outbox(self, tenant_id: str, workspace_id: str, worker_id: str,
                     *, lease_seconds: int = 30) -> OutboxClaim | None: ...

    def heartbeat_outbox(self, tenant_id: str, workspace_id: str,
                         event_id: int, worker_id: str,
                         token: int, *, lease_seconds: int = 30) -> None: ...

    def ack_outbox(self, tenant_id: str, workspace_id: str,
                   event_id: int, worker_id: str, token: int) -> None: ...
