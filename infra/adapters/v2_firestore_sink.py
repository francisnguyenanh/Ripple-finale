"""Firestore sink for the realtime projection (extension FS-R1 hints, FS-C1 conversation documents): the same port as `v2_projection.MemorySink` — `put(scope, key, doc) -> bool`
with a conditional MONOTONIC update (an older or equal revision is ignored) — over the Firestore REST API.

This build talks ONLY to a local Firestore EMULATOR: `FIRESTORE_EMULATOR_HOST` must be a loopback host and the project id must start with `demo-` (Firebase guarantees a demo
project never reaches a cloud resource). No credential is read, no Google client library is used, and there is no fallback: an unreachable emulator is `SinkUnavailable`, never
a cloud call. A real Firestore database (credentials, rules deploy, region) is NOT activated here.

Layout: `feeds/{feed_id}/items/{key}`; `feed_id` is the projector's scope with "/" replaced by "~" (`ws~<hash>`, `conv~redacted~<hash>`, `conv~analyst~<hash>`). Clients read a feed
only when their token lists the feed id (rules: `config/firestore.rules`); every write comes from the backend.
"""
from __future__ import annotations

import os
import urllib.parse

import requests

from infra.adapters.v2_projection import SinkUnavailable, feed_id

LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
_ADMIN = {"Authorization": "Bearer owner"}           # the emulator's admin bypass token (what the Admin SDK does against an emulator); meaningless to a real Firestore


class SinkRefused(ValueError):
    """The configuration points somewhere this build must never write to (non-loopback host, non-demo project)."""


def _encode(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: _encode(v) for k, v in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_encode(v) for v in value]}}
    raise TypeError(f"unsupported field type {type(value).__name__}")


def _decode(value: dict):
    (kind, v), = value.items()
    if kind == "integerValue":
        return int(v)
    if kind == "mapValue":
        return {k: _decode(x) for k, x in (v.get("fields") or {}).items()}
    if kind == "arrayValue":
        return [_decode(x) for x in v.get("values") or []]
    if kind == "nullValue":
        return None
    return v


class FirestoreEmulatorSink:
    kind = "firestore-emulator"

    def __init__(self, host: str, project_id: str, *, timeout: float = 5.0, max_races: int = 3):
        bare = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.startswith("[") else host
        if bare not in LOOPBACK:
            raise SinkRefused("the Firestore emulator host must be a loopback address")
        if not project_id.startswith("demo-"):
            raise SinkRefused("the Firestore project id must be a demo- project in this build")
        self.project_id = project_id
        self.base = f"http://{host}/v1/projects/{project_id}/databases/(default)/documents"
        self.timeout = timeout
        self.max_races = max_races
        self.puts = 0
        self.ignored_older = 0
        self._http = requests.Session()
        self._http.trust_env = False                 # never a proxy or netrc from the environment

    @classmethod
    def from_env(cls) -> "FirestoreEmulatorSink":
        host = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
        project = os.environ.get("RIPPLE_FIRESTORE_PROJECT", "")
        if not host or not project:
            raise SinkRefused("FIRESTORE_EMULATOR_HOST and RIPPLE_FIRESTORE_PROJECT are required for the firestore-emulator sink")
        return cls(host, project)

    def _name(self, scope: str, key: str) -> str:
        return f"projects/{self.project_id}/databases/(default)/documents/feeds/{feed_id(scope)}/items/{key}"

    def _call(self, method: str, url: str, **kw):
        try:
            return self._http.request(method, url, headers=_ADMIN, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise SinkUnavailable(f"firestore emulator unreachable: {type(exc).__name__}") from None

    def get(self, scope: str, key: str):
        r = self._call("GET", f"{self.base}/feeds/{urllib.parse.quote(feed_id(scope), safe='~')}/items/{urllib.parse.quote(key)}")
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise SinkUnavailable(f"firestore get HTTP {r.status_code}")
        body = r.json()
        return {k: _decode(v) for k, v in (body.get("fields") or {}).items()}, body.get("updateTime")

    def _get_path(self, rel: str):
        r = self._call("GET", f"{self.base}/{rel}")
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise SinkUnavailable(f"firestore get HTTP {r.status_code}")
        body = r.json()
        return {k: _decode(v) for k, v in (body.get("fields") or {}).items()}, body.get("updateTime")

    def _monotonic_write(self, rel: str, doc: dict, field: str) -> bool:
        """Write `doc` at `rel` only when its `field` is newer than the stored one, with an optimistic precondition on the document we compared against."""
        for _ in range(self.max_races):
            current, update_time = self._get_path(rel)
            if current is not None and int(current.get(field, -1)) >= int(doc[field]):
                self.ignored_older += 1
                return False
            write = {"update": {"name": f"projects/{self.project_id}/databases/(default)/documents/{rel}", "fields": {k: _encode(v) for k, v in doc.items()}},
                     "currentDocument": {"updateTime": update_time} if update_time else {"exists": False}}
            r = self._call("POST", f"{self.base}:commit", json={"writes": [write]})
            if r.status_code == 200:
                return True
            if r.status_code not in (400, 409) or "FAILED_PRECONDITION" not in r.text and "ALREADY_EXISTS" not in r.text and "NOT_FOUND" not in r.text:
                raise SinkUnavailable(f"firestore commit HTTP {r.status_code}")
        raise SinkUnavailable("firestore commit kept racing with another writer")

    def put(self, scope: str, key: str, doc: dict) -> bool:
        self.puts += 1
        return self._monotonic_write(f"feeds/{urllib.parse.quote(feed_id(scope), safe='~')}/items/{urllib.parse.quote(key)}", doc, "revision")

    def put_policy(self, feed: str, policy_version: int) -> bool:
        """`feed_policies/{ws feed}`: the workspace's membership policy version; the rules accept a listener token only while it carries this version."""
        return self._monotonic_write(f"feed_policies/{urllib.parse.quote(feed, safe='~')}", {"schema": "ripple-feed-policy/1", "policy_version": int(policy_version)}, "policy_version")
