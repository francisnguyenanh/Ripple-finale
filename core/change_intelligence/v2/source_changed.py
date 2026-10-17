"""The FUTURE `SourceChanged` connector envelope (contract only).

Nothing here is wired: there is no webhook route, no subscription and no caller in
`infra`/`app` (only MANUAL refresh ships, `/v2/sources/refresh/*`). This module is the
pure, I/O-free definition a future Git / object-store connector gateway and Ripple's
ingest will share: what a well-formed envelope is (`validate_source_changed`), how
duplicates are recognised (`dedup_key`) and what to do with a delivery
(`classify_delivery`). The JSON Schema in `docs/contracts/source_changed.v1.schema.json`
is the published mirror of the structural half; the semantic half (duplicate paths,
path traversal, per-change field rules, revision chain) lives only here.

Order and identity rules (see `docs/contracts/SOURCE_CHANGED_ENVELOPE.md`):

* Order is the connector's REVISION CHAIN: an event applies only when its
  `previous_revision` equals the last revision Ripple ingested for that registered
  source. `occurred_at` is informational; it is never order evidence.
* Identity is the authenticated connector session. `tenant_id`, `project_id`,
  `workspace_id` and the ids in the envelope are hints that must MATCH the session;
  the envelope can assert no actor, approval or authority (unknown fields are refused).
* Idempotency key = `(connector_id, event_id)`; redelivery is a `duplicate`.
"""

from __future__ import annotations

import re
import unicodedata

SCHEMA_VERSION = "source_changed.v1"
CONNECTOR_KINDS = ("git", "manual", "object_store")
CHANGE_KINDS = ("added", "modified", "deleted", "renamed")
VERIFIED_BY = ("connector_gateway",)
MAX_MANIFEST_ITEMS = 5000
MAX_ID_LENGTH = 128
MAX_REVISION_LENGTH = 256
MAX_PATH_LENGTH = 1024

_ROOT_FIELDS = ("schema_version", "event_id", "connector", "tenant_id", "project_id",
                "workspace_id", "registered_source_id", "previous_revision",
                "next_revision", "git", "changed_manifest", "occurred_at", "auth_provenance")
_ROOT_REQUIRED = tuple(f for f in _ROOT_FIELDS if f != "git")
_CONNECTOR_FIELDS = ("connector_id", "kind")
_GIT_FIELDS = ("commit", "ref")
_ITEM_FIELDS = ("path", "change", "sha256", "byte_size", "previous_path")
_PROVENANCE_FIELDS = ("method", "key_id", "verified_by", "verified_at")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,9})?Z$")


def _valid_timestamp(value: str) -> bool:
    match = _TIMESTAMP.match(value)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    return 1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59 and second <= 59


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFKC", path).casefold()


def _path_problem(path: str) -> bool:
    """True when `path` could escape the source root or is not a plain relative path."""
    if not path or len(path) > MAX_PATH_LENGTH or "\\" in path or "\x00" in path:
        return True
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return True
    parts = path.split("/")
    return any(p in ("", ".", "..") for p in parts)


def validate_source_changed(envelope) -> dict:
    """Total, deterministic, non-mutating validation. Returns
    `{"ok": bool, "errors": [{"code", "path"}]}`; never raises, whatever the input."""
    errors: list[dict] = []

    def err(code: str, path: str) -> None:
        errors.append({"code": code, "path": path or "$"})

    def text(value, path, *, max_len) -> bool:
        if not isinstance(value, str):
            err("bad_type", path)
            return False
        if not value.strip() or len(value) > max_len:
            err("bad_format", path)
            return False
        return True

    def obj(value, path, allowed, required=()) -> bool:
        if not isinstance(value, dict):
            err("bad_type", path)
            return False
        for name in value:
            if name not in allowed:
                err("unknown_field", f"{path}.{name}" if path != "$" else str(name))
        for name in required:
            if name not in value:
                err("missing_field", f"{path}.{name}" if path != "$" else name)
        return True

    if not obj(envelope, "$", _ROOT_FIELDS, _ROOT_REQUIRED):
        return {"ok": False, "errors": errors}
    env = envelope

    if "schema_version" in env:
        if not isinstance(env["schema_version"], str):
            err("bad_type", "schema_version")
        elif env["schema_version"] != SCHEMA_VERSION:
            err("bad_enum", "schema_version")
    for name in ("event_id", "tenant_id", "project_id", "workspace_id", "registered_source_id"):
        if name in env:
            text(env[name], name, max_len=MAX_ID_LENGTH)

    kind = None
    if "connector" in env and obj(env["connector"], "connector", _CONNECTOR_FIELDS, _CONNECTOR_FIELDS):
        connector = env["connector"]
        if "connector_id" in connector:
            text(connector["connector_id"], "connector.connector_id", max_len=MAX_ID_LENGTH)
        if "kind" in connector:
            if not isinstance(connector["kind"], str):
                err("bad_type", "connector.kind")
            elif connector["kind"] not in CONNECTOR_KINDS:
                err("bad_enum", "connector.kind")
            else:
                kind = connector["kind"]

    previous, nxt = env.get("previous_revision"), env.get("next_revision")
    if "previous_revision" in env and previous is not None:
        text(previous, "previous_revision", max_len=MAX_REVISION_LENGTH)
    if "next_revision" in env:
        text(nxt, "next_revision", max_len=MAX_REVISION_LENGTH)
    if (isinstance(previous, str) and isinstance(nxt, str) and previous.strip()
            and nxt.strip() and previous == nxt):
        err("no_change", "next_revision")

    if kind == "git":
        if "git" not in env:
            err("missing_field", "git")
        elif obj(env["git"], "git", _GIT_FIELDS, _GIT_FIELDS):
            for name in _GIT_FIELDS:
                if name in env["git"]:
                    text(env["git"][name], f"git.{name}", max_len=MAX_REVISION_LENGTH)
            commit = env["git"].get("commit")
            if isinstance(commit, str) and isinstance(nxt, str) and commit != nxt:
                err("revision_mismatch", "git.commit")
    elif "git" in env and kind is not None:
        err("unknown_field", "git")

    if "changed_manifest" in env:
        _validate_manifest(env["changed_manifest"], err, text)

    if "occurred_at" in env:
        if not isinstance(env["occurred_at"], str):
            err("bad_type", "occurred_at")
        elif not _valid_timestamp(env["occurred_at"]):
            err("bad_format", "occurred_at")

    if "auth_provenance" in env and obj(env["auth_provenance"], "auth_provenance",
                                        _PROVENANCE_FIELDS, _PROVENANCE_FIELDS):
        prov = env["auth_provenance"]
        for name in ("method", "key_id"):
            if name in prov:
                text(prov[name], f"auth_provenance.{name}", max_len=MAX_ID_LENGTH)
        if "verified_by" in prov:
            if not isinstance(prov["verified_by"], str):
                err("bad_type", "auth_provenance.verified_by")
            elif prov["verified_by"] not in VERIFIED_BY:
                err("bad_enum", "auth_provenance.verified_by")
        if "verified_at" in prov:
            if not isinstance(prov["verified_at"], str):
                err("bad_type", "auth_provenance.verified_at")
            elif not _valid_timestamp(prov["verified_at"]):
                err("bad_format", "auth_provenance.verified_at")
    return {"ok": not errors, "errors": errors}


def _validate_manifest(manifest, err, text) -> None:
    if not isinstance(manifest, list):
        err("bad_type", "changed_manifest")
        return
    if not manifest:
        err("empty_manifest", "changed_manifest")
        return
    if len(manifest) > MAX_MANIFEST_ITEMS:
        err("bad_format", "changed_manifest")
        return
    seen: dict[str, int] = {}
    for i, item in enumerate(manifest):
        base = f"changed_manifest[{i}]"
        if not isinstance(item, dict):
            err("bad_type", base)
            continue
        for name in item:
            if name not in _ITEM_FIELDS:
                err("unknown_field", f"{base}.{name}")
        for name in ("path", "change"):
            if name not in item:
                err("missing_field", f"{base}.{name}")
        path = item.get("path")
        if "path" in item:
            if not isinstance(path, str):
                err("bad_type", f"{base}.path")
            elif _path_problem(path):
                err("bad_path", f"{base}.path")
            else:
                key = _path_key(path)
                if key in seen:
                    err("duplicate_path", f"{base}.path")
                seen[key] = i
        change = item.get("change")
        change_ok = False
        if "change" in item:
            if not isinstance(change, str):
                err("bad_type", f"{base}.change")
            elif change not in CHANGE_KINDS:
                err("bad_enum", f"{base}.change")
            else:
                change_ok = True
        if "sha256" in item:
            if not isinstance(item["sha256"], str):
                err("bad_type", f"{base}.sha256")
            elif not _SHA256.match(item["sha256"]):
                err("bad_format", f"{base}.sha256")
        if "byte_size" in item:
            size = item["byte_size"]
            if isinstance(size, bool) or not isinstance(size, int):
                err("bad_type", f"{base}.byte_size")
            elif size < 0:
                err("bad_format", f"{base}.byte_size")
        if "previous_path" in item:
            previous = item["previous_path"]
            if not isinstance(previous, str):
                err("bad_type", f"{base}.previous_path")
            elif _path_problem(previous):
                err("bad_path", f"{base}.previous_path")
        if not change_ok:
            continue
        if change in ("added", "modified", "renamed"):
            for name in ("sha256", "byte_size"):
                if name not in item:
                    err("missing_field", f"{base}.{name}")
        if change == "renamed" and "previous_path" not in item:
            err("missing_field", f"{base}.previous_path")
        if change in ("added", "modified") and "previous_path" in item:
            err("bad_field_for_change", f"{base}.previous_path")
        if change == "deleted":
            for name in ("sha256", "byte_size", "previous_path"):
                if name in item:
                    err("bad_field_for_change", f"{base}.{name}")


def dedup_key(envelope) -> tuple:
    """`(connector_id, event_id)`: idempotency is scoped to the connector, never global."""
    connector = envelope.get("connector") if isinstance(envelope, dict) else None
    connector_id = connector.get("connector_id") if isinstance(connector, dict) else None
    event_id = envelope.get("event_id") if isinstance(envelope, dict) else None
    return (connector_id, event_id)


def classify_delivery(envelope, *, current_revision, seen) -> str:
    """One of `accept`, `duplicate`, `out_of_order`, `reject`.

    `current_revision` is the last connector revision Ripple ingested for the
    registered source (None = nothing ingested yet); `seen` is a set of dedup keys.
    Malformed -> `reject`; a seen key -> `duplicate`; `previous_revision` not equal to
    `current_revision` -> `out_of_order` (a replay of an older event, or an event whose
    predecessor has not arrived); otherwise `accept`. `occurred_at` and the ids in the
    envelope are not consulted."""
    if not validate_source_changed(envelope)["ok"]:
        return "reject"
    if dedup_key(envelope) in seen:
        return "duplicate"
    if envelope.get("previous_revision") != current_revision:
        return "out_of_order"
    return "accept"
