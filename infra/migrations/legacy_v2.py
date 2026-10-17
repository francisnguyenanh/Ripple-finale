"""Operator-only migration of persisted v1 sessions into v2 workspace history.

The legacy reader supplies a stored bundle. No API request can supply artifact
bytes to this migrator. Blob upload precedes one SQL transaction containing
the source head, historical KM, legacy history, audit, outbox and replay key.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from psycopg2.extras import Json

from core.change_intelligence.v2.compat import (
    decode_legacy_knowledge_record, decode_legacy_session,
)
from core.change_intelligence.v2.serialize import canonical_hash
from infra.adapters.v2_postgres import (
    BlobIntegrityError, PostgresV2Store, PublishResult, StaleVersion,
    StoreConflict, TenantScopeError,
)


def _blob_hash(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _object(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


@dataclass(frozen=True)
class MigrationPlan:
    legacy_session_id: str
    artifact_count: int
    blob_hashes: dict[str, str]
    knowledge_count: int
    decision_count: int
    lineage_version: int
    content_hash: str
    source_revision_id: str


@dataclass(frozen=True)
class MigrationResult:
    source_revision_id: str
    version: int
    replayed: bool = False


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    issues: tuple[str, ...] = ()


class LegacyV2Migrator:
    """Bridge a read-only legacy reader into one scoped v2 workspace."""

    _OPERATION = "legacy.migrate"

    def __init__(self, store: PostgresV2Store, legacy_reader: object):
        if not callable(getattr(legacy_reader, "load_bundle", None)):
            raise TypeError("legacy reader must provide load_bundle(session_id)")
        self.store = store
        self.legacy_reader = legacy_reader
        sql = (Path(__file__).with_name("ai1_t04_legacy.sql")
               .read_text(encoding="utf-8"))
        with self.store._tx() as cur:
            cur.execute(sql)

    def _load(self, session_id: str) -> tuple[MigrationPlan, dict]:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("legacy session ID required")
        bundle = self.legacy_reader.load_bundle(session_id)
        if not isinstance(bundle, Mapping):
            raise TypeError("legacy reader must return a mapping")
        session_raw = bundle["session"]
        session = decode_legacy_session(session_raw)
        if session.session_id != session_id:
            raise ValueError("legacy reader returned a different session")
        if not isinstance(session.durable_version, int) or session.durable_version < 0:
            raise ValueError("invalid legacy durable version")
        artifacts = bundle["baseline_artifacts"]
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise ValueError("legacy baseline must have artifacts")
        blob_hashes: dict[str, str] = {}
        for artifact_id, content in artifacts.items():
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("legacy artifact ID required")
            if not isinstance(content, bytes):
                raise TypeError("legacy artifact content must be bytes")
            blob_hashes[artifact_id] = _blob_hash(content)
        records = bundle["knowledge_records"]
        if not isinstance(records, list):
            raise TypeError("legacy knowledge records must be a list")
        for record in records:
            decode_legacy_knowledge_record(record)
        decisions = bundle.get("human_decisions", [])
        if not isinstance(decisions, list):
            raise TypeError("legacy human decisions must be a list")
        decision_ids: set[str] = set()
        for decision in decisions:
            if not isinstance(decision, Mapping):
                raise TypeError("legacy human decision must be a mapping")
            for field in ("decision_id", "actor_id", "decision", "reason", "decided_at"):
                if not isinstance(decision.get(field), str) or not decision[field].strip():
                    raise ValueError(f"legacy human decision {field} required")
            if decision["decision_id"] in decision_ids:
                raise ValueError("duplicate legacy human decision ID")
            decision_ids.add(decision["decision_id"])
        lineage = bundle["lineage_entries"]
        if not isinstance(lineage, list):
            raise TypeError("legacy lineage entries must be a list")
        versions = []
        for entry in lineage:
            if not isinstance(entry, Mapping) or type(entry.get("version")) is not int:
                raise ValueError("invalid legacy lineage version")
            if not isinstance(entry.get("snapshot_id"), str) or not entry["snapshot_id"]:
                raise ValueError("legacy lineage snapshot ID required")
            versions.append(entry["version"])
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError("legacy lineage versions must be consecutive from 1")
        content_hash = canonical_hash({
            "session": session_raw,
            "blob_hashes": blob_hashes,
            "knowledge_records": records,
            "human_decisions": decisions,
            "lineage_entries": lineage,
        })
        plan = MigrationPlan(
            session_id, len(blob_hashes), dict(sorted(blob_hashes.items())),
            len(records), len(decisions), versions[-1] if versions else 0, content_hash,
            "legacy-source:" + content_hash,
        )
        return plan, dict(bundle)

    def dry_run(self, tenant_id: str, workspace_id: str,
                session_id: str) -> MigrationPlan:
        """Validate and recount a stored bundle without staging bytes or data."""
        self.store.head(tenant_id, workspace_id)
        return self._load(session_id)[0]

    def commit(self, tenant_id: str, workspace_id: str, session_id: str, *,
               expected_version: int, idempotency_key: str) -> MigrationResult:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency key required")
        plan, bundle = self._load(session_id)
        for artifact_id, raw in bundle["baseline_artifacts"].items():
            if self.store.stage_blob(raw) != plan.blob_hashes[artifact_id]:
                raise BlobIntegrityError("staged legacy blob changed")
        knowledge_id = ("legacy-km:" + plan.content_hash
                        if bundle["knowledge_records"] else None)
        with self.store._tx() as cur:
            head = self.store._lock_head(cur, tenant_id, workspace_id)
            prior = self.store._replay(cur, tenant_id, workspace_id,
                                       self._OPERATION, idempotency_key,
                                       plan.content_hash)
            if prior is not None:
                return MigrationResult(prior.revision_id, prior.version, True)
            if head.version != expected_version or head.source_revision_id is not None:
                raise StaleVersion("legacy import requires an empty pinned head")
            provenance = {
                "kind": "legacy_baseline",
                "legacy_session_id": session_id,
                "legacy_baseline_id": bundle["session"]["baseline_id"],
                "authority": "imported_history",
            }
            cur.execute("""INSERT INTO v2_source_revisions
                (tenant_id, workspace_id, revision_id, parent_revision_id,
                 manifest, provenance) VALUES (%s,%s,%s,NULL,%s,%s)""",
                (tenant_id, workspace_id, plan.source_revision_id,
                 Json(plan.blob_hashes), Json(provenance)))
            for artifact_id, digest in plan.blob_hashes.items():
                cur.execute("""INSERT INTO v2_blob_refs
                    (tenant_id, workspace_id, blob_hash) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""", (tenant_id, workspace_id, digest))
                cur.execute("""INSERT INTO v2_artifact_revisions
                    (tenant_id, workspace_id, source_revision_id, artifact_id, blob_hash)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (tenant_id, workspace_id, plan.source_revision_id,
                     artifact_id, digest))
            if knowledge_id is not None:
                cur.execute("""INSERT INTO v2_knowledge_revisions
                    (tenant_id, workspace_id, revision_id, source_revision_id,
                     decision_epoch, payload) VALUES (%s,%s,%s,%s,0,%s)""",
                    (tenant_id, workspace_id, knowledge_id,
                     plan.source_revision_id, Json({
                         "authority": "legacy_inferred_unconfirmed",
                         "legacy_records": bundle["knowledge_records"],
                     })))
            session = bundle["session"]
            cur.execute("""INSERT INTO v2_legacy_imports
                (tenant_id, workspace_id, legacy_session_id, source_revision_id,
                 knowledge_revision_id, legacy_baseline_id,
                 legacy_durable_version, legacy_session, legacy_work,
                 lineage_entries, human_decisions, content_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, workspace_id, session_id, plan.source_revision_id,
                 knowledge_id, session["baseline_id"], session["durable_version"],
                 Json(session), Json(session["work"]),
                 Json(bundle["lineage_entries"]),
                 Json(bundle.get("human_decisions", [])),
                 plan.content_hash))
            version = head.version + 1
            cur.execute("""UPDATE v2_workspaces SET version=%s,
                source_revision_id=%s, knowledge_revision_id=NULL
                WHERE tenant_id=%s AND workspace_id=%s""",
                (version, plan.source_revision_id, tenant_id, workspace_id))
            cur.execute("""INSERT INTO v2_workspace_rollout
                (tenant_id, workspace_id, enabled, legacy_session_id)
                VALUES (%s,%s,TRUE,%s)""",
                (tenant_id, workspace_id, session_id))
            result = PublishResult(version, plan.source_revision_id)
            self.store._record_commit(
                cur, tenant_id, workspace_id, self._OPERATION,
                idempotency_key, plan.content_hash, result, "LEGACY_IMPORTED",
                {"legacy_session_id": session_id,
                 "source_revision_id": plan.source_revision_id,
                 "artifact_count": plan.artifact_count,
                 "knowledge_count": plan.knowledge_count,
                 "legacy_decision_count": plan.decision_count,
                 "lineage_version": plan.lineage_version})
            return MigrationResult(plan.source_revision_id, version)

    def verify(self, tenant_id: str, workspace_id: str,
               session_id: str) -> VerificationResult:
        """Compare imported metadata and bytes with the persisted v1 reader."""
        plan, bundle = self._load(session_id)
        issues: list[str] = []
        with self.store._tx() as cur:
            cur.execute("""SELECT source_revision_id, knowledge_revision_id,
                legacy_baseline_id, legacy_durable_version, legacy_session,
                legacy_work, lineage_entries, human_decisions,
                content_hash FROM v2_legacy_imports
                WHERE tenant_id=%s AND workspace_id=%s AND legacy_session_id=%s""",
                (tenant_id, workspace_id, session_id))
            row = cur.fetchone()
            if row is None:
                return VerificationResult(False, ("legacy import missing",))
            if row[0] != plan.source_revision_id or row[8] != plan.content_hash:
                issues.append("legacy source content changed")
            if row[2] != bundle["session"]["baseline_id"] or row[3] != bundle["session"]["durable_version"]:
                issues.append("legacy session metadata differs")
            if _object(row[4]) != bundle["session"]:
                issues.append("full legacy session differs")
            if _object(row[5]) != bundle["session"]["work"] or _object(row[6]) != bundle["lineage_entries"]:
                issues.append("legacy history differs")
            if _object(row[7]) != bundle.get("human_decisions", []):
                issues.append("legacy human decisions differ")
            cur.execute("""SELECT manifest, provenance FROM v2_source_revisions
                WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                (tenant_id, workspace_id, row[0]))
            source = cur.fetchone()
            if source is None or _object(source[0]) != plan.blob_hashes or \
                    _object(source[1]).get("legacy_session_id") != session_id:
                issues.append("source manifest or provenance differs")
            if row[1] is not None:
                cur.execute("""SELECT payload FROM v2_knowledge_revisions
                    WHERE tenant_id=%s AND workspace_id=%s AND revision_id=%s""",
                    (tenant_id, workspace_id, row[1]))
                knowledge = cur.fetchone()
                if knowledge is None or _object(knowledge[0]).get("legacy_records") != bundle["knowledge_records"]:
                    issues.append("legacy knowledge differs")
            elif bundle["knowledge_records"]:
                issues.append("legacy knowledge missing")
        for artifact_id, raw in bundle["baseline_artifacts"].items():
            try:
                if self.store.read_blob(tenant_id, workspace_id,
                                        plan.source_revision_id, artifact_id) != raw:
                    issues.append(f"artifact {artifact_id} differs")
            except (BlobIntegrityError, TenantScopeError):
                issues.append(f"artifact {artifact_id} missing or corrupt")
        return VerificationResult(not issues, tuple(issues))

    def read_legacy_history(self, tenant_id: str, workspace_id: str,
                            session_id: str) -> dict:
        with self.store._tx() as cur:
            cur.execute("""SELECT legacy_work, lineage_entries,
                legacy_durable_version, legacy_session,
                human_decisions FROM v2_legacy_imports
                WHERE tenant_id=%s AND workspace_id=%s AND legacy_session_id=%s""",
                (tenant_id, workspace_id, session_id))
            row = cur.fetchone()
        if row is None:
            raise TenantScopeError("legacy history not found")
        return {"legacy_work": _object(row[0]),
                "lineage_entries": _object(row[1]),
                "durable_version": row[2],
                "legacy_session": _object(row[3]),
                "human_decisions": _object(row[4])}

    def read_legacy(self, session_id: str) -> Mapping[str, object]:
        """Backward reader for release rollback; never resolves from v2 rows."""
        return self.legacy_reader.load_bundle(session_id)

    def is_v2_enabled(self, tenant_id: str, workspace_id: str) -> bool:
        self.store.head(tenant_id, workspace_id)
        with self.store._tx() as cur:
            cur.execute("""SELECT enabled FROM v2_workspace_rollout
                WHERE tenant_id=%s AND workspace_id=%s""",
                (tenant_id, workspace_id))
            row = cur.fetchone()
        return bool(row and row[0])

    def disable_v2(self, tenant_id: str, workspace_id: str) -> None:
        """Rollback application routing only; preserve imported and new data."""
        with self.store._tx() as cur:
            cur.execute("""UPDATE v2_workspace_rollout SET enabled=FALSE,
                updated_at=now() WHERE tenant_id=%s AND workspace_id=%s""",
                (tenant_id, workspace_id))
            if cur.rowcount != 1:
                raise TenantScopeError("workspace rollout not found")
            cur.execute("""INSERT INTO v2_audit
                (tenant_id, workspace_id, event_type, actor_id, payload)
                VALUES (%s,%s,'V2_DISABLED','operator',%s)""",
                (tenant_id, workspace_id, Json({"reason": "release_rollback"})))


class JSONBundleReader:
    """Read a persisted operator bundle; artifact bytes use base64 encoding."""

    def __init__(self, path: Path):
        self.path = path

    def load_bundle(self, session_id: str) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("session", {}).get("session_id") != session_id:
            raise KeyError(session_id)
        encoded = raw.pop("baseline_artifacts_base64")
        raw["baseline_artifacts"] = {
            key: base64.b64decode(value, validate=True)
            for key, value in encoded.items()
        }
        return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate a persisted v1 bundle to v2")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if any(arg == "--artifact" or arg.startswith("--artifact=")
           for arg in raw_argv):
        parser.error("unrecognized arguments: --artifact")
    parser.add_argument("mode", choices=("dry-run", "commit", "verify", "disable", "status"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--gcs-bucket")
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args(raw_argv)
    if args.mode in {"commit", "verify"} and not args.gcs_bucket:
        parser.error("commit/verify require --gcs-bucket")
    if args.mode == "commit" and (args.expected_version is None or not args.idempotency_key):
        parser.error("commit requires --expected-version and --idempotency-key")
    if args.gcs_bucket:
        from google.cloud import storage
        from infra.adapters.v2_blobs import ImmutableGCSBlobs
        blobs = ImmutableGCSBlobs(storage.Client().bucket(args.gcs_bucket))
    else:
        class UnavailableBlobs:
            def put_immutable(self, digest, content):
                raise RuntimeError("GCS bucket required")
            def read(self, digest):
                raise RuntimeError("GCS bucket required")
        blobs = UnavailableBlobs()
    store = PostgresV2Store(args.dsn, blobs)
    store.migrate()
    migrator = LegacyV2Migrator(store, JSONBundleReader(args.bundle))
    if args.mode == "dry-run":
        result = asdict(migrator.dry_run(args.tenant, args.workspace, args.session_id))
    elif args.mode == "commit":
        result = asdict(migrator.commit(
            args.tenant, args.workspace, args.session_id,
            expected_version=args.expected_version,
            idempotency_key=args.idempotency_key))
    elif args.mode == "verify":
        result = asdict(migrator.verify(args.tenant, args.workspace, args.session_id))
    elif args.mode == "disable":
        migrator.disable_v2(args.tenant, args.workspace)
        result = {"enabled": False}
    else:
        result = {"enabled": migrator.is_v2_enabled(args.tenant, args.workspace)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
