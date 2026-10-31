"""Satellite routes (extensions FB-R1 projection, FB-N1/FS-I1 notifications and inbox), local build. Every route needs a workspace membership; nothing here decides anything.

GET  /v2/projection/status?workspace_id=                      checkpoint, lag, scope key                                            project.read
GET  /v2/projection/feeds?workspace_id=                       feed ids this member may listen to (the `ripple_feeds` claim)         project.read
POST /v2/projection/feed-token   {workspace_id}              listener token; every claim decided by the server (issuer off -> 503) project.read
POST /v2/projection/run           {workspace_id}              one bounded projector pass into the configured sink (off -> 503)      membership.manage
POST /v2/notifications/tokens     {workspace_id, token, platform}   register this account's device/browser token                    project.read
POST /v2/notifications/tokens/revoke {workspace_id, token? , all?}  revoke one or all of this account's tokens                     project.read
GET  /v2/notifications/inbox?workspace_id=                    my inbox items, unread count                                           project.read
POST /v2/notifications/inbox/{item_id}/read {workspace_id}    read receipt (idempotent; not an approval)                             project.read
POST /v2/notifications/dispatch   {workspace_id}              derive intents, resolve recipients, one dispatch pass (off -> 503)    membership.manage
Sinks/transports: `RIPPLE_PROJECTION_SINK` and `RIPPLE_NOTIFICATION_TRANSPORT` = off (default) | memory (process-local mock for local runs); the projection sink also takes
firestore-emulator (a LOCAL Firestore emulator: loopback `FIRESTORE_EMULATOR_HOST` + a `demo-` project in `RIPPLE_FIRESTORE_PROJECT`; anything else is refused with 503, never
a cloud fallback). A real Firestore database and an FCM adapter are the activation step and need the owner's cloud grant; they implement the same two ports.
"""
from __future__ import annotations

from infra.db_offload import db_route
from infra.v2_policy_routes import _json_body_sync
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.adapters import v2_business, v2_feed_tokens, v2_firestore_sink, v2_notifications, v2_projection
from infra.adapters.v2_identity import IdentityDenied
from infra.adapters.v2_policy import PermissionDenied, permissions_for, require, require_human_actor
from infra.adapters.v2_postgres import StaleVersion, StoreConflict, TenantScopeError
from infra.v2_business_routes import _denied
from infra.v2_policy_routes import _authority, _error, _identity_store, _json_body, _origin_ok, _v2_store

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
_MEMORY_SINK = v2_projection.MemorySink()
_MEMORY_TRANSPORT = v2_notifications.MockTransport()


_FIRESTORE: dict = {}


def sink():
    """The configured projection sink or None (off). Raises `SinkRefused` for a firestore-emulator configuration this build must not write to."""
    mode = os.environ.get("RIPPLE_PROJECTION_SINK", "off")
    if mode == "memory":
        return _MEMORY_SINK
    if mode == "firestore-emulator":
        key = (os.environ.get("FIRESTORE_EMULATOR_HOST", ""), os.environ.get("RIPPLE_FIRESTORE_PROJECT", ""))
        if key not in _FIRESTORE:
            _FIRESTORE[key] = v2_firestore_sink.FirestoreEmulatorSink.from_env()
        return _FIRESTORE[key]
    return None


def _refused(exc):
    return JSONResponse({"error": str(exc), "code": "sink_refused"}, status_code=503, headers=_NO_STORE)


def _status_sink():
    try:
        return sink()
    except v2_firestore_sink.SinkRefused:
        return None


def publish_policy(tenant_id: str, workspace_id: str, policy_version) -> bool:
    """Best effort: mirror the workspace's membership policy version into the sink so the rules refuse listener tokens issued under an older version. Called after a membership
    grant/revoke, on every projection run and on every token issue; when the sink is off or down, tokens still expire within their TTL."""
    try:
        s = sink()
        if s is None or not hasattr(s, "put_policy") or policy_version is None:
            return False
        return s.put_policy(v2_projection.feed_id(v2_projection.scope_for(tenant_id, workspace_id)), int(policy_version))
    except (v2_firestore_sink.SinkRefused, v2_projection.SinkUnavailable, ValueError):
        return False


def transport():
    return _MEMORY_TRANSPORT if os.environ.get("RIPPLE_NOTIFICATION_TRANSPORT", "off") == "memory" else None


def _shell(request, workspace_id, action, *, permission="project.read", mutating=True):
    if mutating and not _origin_ok(request):
        return _error("origin refused", 403)
    identity, store = _identity_store(), _v2_store()
    if identity is None or store is None:
        return _error("v2 unavailable", 503)
    if not (isinstance(workspace_id, str) and workspace_id.strip()):
        return _error("workspace_id required", 400)
    try:
        authority = _authority(identity, request, workspace_id)
        if mutating:
            require_human_actor(authority)
        require(authority, permission)
        out = action(store, authority["tenant_id"], authority)
        return out if isinstance(out, JSONResponse) else JSONResponse(out, headers=_NO_STORE)
    except IdentityDenied as exc:
        return _denied(exc)
    except PermissionDenied as exc:
        return _error(f"permission denied: {exc}", 403)
    except TenantScopeError as exc:
        return _error(str(exc), 404)
    except v2_business.BusinessKnowledgeRefused as exc:
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=409, headers=_NO_STORE)
    except (StaleVersion, StoreConflict) as exc:
        return _error(str(exc), 409)


@router.get("/v2/projection/feeds")
def projection_feeds(request: Request, workspace_id: str):
    return _shell(request, workspace_id, lambda store, tenant, authority: v2_projection.feed_grants(tenant, workspace_id, permissions_for(authority.get("roles"))), mutating=False)


@router.post("/v2/projection/feed-token")
@db_route
def projection_feed_token(request: Request):
    body = _json_body_sync(request)
    if set(body) - {"workspace_id"}:
        return _error("only workspace_id is accepted: feeds and claims are decided by the server", 400)

    def action(store, tenant, authority):
        try:
            iss = v2_feed_tokens.issuer()
            s = sink()
        except (v2_feed_tokens.IssuerRefused, v2_firestore_sink.SinkRefused) as exc:
            return _refused(exc)
        if iss is None or s is None or not hasattr(s, "put_policy"):
            return JSONResponse({"error": "feed token issuer off", "code": "issuer_off"}, status_code=503, headers=_NO_STORE)
        ws_id = body["workspace_id"]
        grants = v2_projection.feed_grants(tenant, ws_id, permissions_for(authority.get("roles")))
        ws_feed = v2_projection.feed_id(v2_projection.scope_for(tenant, ws_id))
        try:
            s.put_policy(ws_feed, int(authority["policy_version"]))
        except v2_projection.SinkUnavailable as exc:
            return JSONResponse({"error": str(exc), "code": "sink_unavailable"}, status_code=503, headers=_NO_STORE)
        return iss.issue(sub=v2_feed_tokens.subject(tenant, authority["account_id"]), feeds=grants["feeds"], ws_feed=ws_feed, policy_version=int(authority["policy_version"]))
    return _shell(request, body.get("workspace_id"), action, permission="project.read")


@router.get("/v2/projection/status")
def projection_status(request: Request, workspace_id: str):
    return _shell(request, workspace_id, lambda store, tenant, authority: v2_projection.status(store, tenant, workspace_id, sink=_status_sink()), mutating=False)


@router.post("/v2/projection/run")
@db_route
def projection_run(request: Request):
    body = _json_body_sync(request)

    def action(store, tenant, authority):
        try:
            s = sink()
        except v2_firestore_sink.SinkRefused as exc:
            return _refused(exc)
        if s is None:
            return JSONResponse({"error": "projection sink off", "code": "sink_off"}, status_code=503, headers=_NO_STORE)
        try:
            out = v2_projection.project_once(store, s, tenant, body["workspace_id"])
            publish_policy(tenant, body["workspace_id"], authority.get("policy_version"))
            return out
        except v2_projection.SinkUnavailable as exc:
            return JSONResponse({"error": str(exc), "code": "sink_unavailable", **v2_projection.pending(store, tenant, body["workspace_id"])}, status_code=503, headers=_NO_STORE)
    return _shell(request, body.get("workspace_id"), action, permission="membership.manage")


@router.post("/v2/notifications/tokens")
@db_route
def register_token(request: Request):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_notifications.register_token(store, tenant, body["workspace_id"], account_id=authority["account_id"], token=body.get("token"), platform=body.get("platform")))


@router.post("/v2/notifications/tokens/revoke")
@db_route
def revoke_token(request: Request):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_notifications.revoke_token(store, tenant, body["workspace_id"], account_id=authority["account_id"], token=body.get("token"), all_tokens=bool(body.get("all"))))


@router.get("/v2/notifications/inbox")
def read_inbox(request: Request, workspace_id: str):
    return _shell(request, workspace_id, lambda store, tenant, authority: v2_notifications.inbox(store, tenant, workspace_id, account_id=authority["account_id"]), mutating=False)


@router.post("/v2/notifications/inbox/{item_id}/read")
@db_route
def mark_read(request: Request, item_id: str):
    body = _json_body_sync(request)
    return _shell(request, body.get("workspace_id"), lambda store, tenant, authority: v2_notifications.mark_read(store, tenant, body["workspace_id"], account_id=authority["account_id"], item_id=item_id))


@router.post("/v2/projection/conversations")
@db_route
def projection_conversations(request: Request):
    body = _json_body_sync(request)
    profile = body.get("profile", "redacted")

    def action(store, tenant, authority):
        try:
            s = sink()
        except v2_firestore_sink.SinkRefused as exc:
            return _refused(exc)
        if s is None:
            return JSONResponse({"error": "projection sink off", "code": "sink_off"}, status_code=503, headers=_NO_STORE)
        if profile not in v2_projection.PROFILES:
            return _error("profile must be redacted or analyst", 400)
        if profile == "analyst":
            require(authority, "knowledge.confirm")                                            # question text is read by knowledge owners; the redacted profile needs the membership only
        try:
            return v2_projection.project_conversations(store, s, tenant, body["workspace_id"], profile=profile)
        except v2_projection.SinkUnavailable as exc:
            return JSONResponse({"error": str(exc), "code": "sink_unavailable"}, status_code=503, headers=_NO_STORE)
    return _shell(request, body.get("workspace_id"), action, permission="membership.manage")


OFFLINE_RULES = ["This is an offline copy of one knowledge version: the current authority was not checked while offline.",
                 "It supports reading only: no approval, decision, apply or policy evaluation may be based on it.",
                 "It is purged on logout, account switch or workspace switch; it may still have been copied or photographed.",
                 "Reconnecting re-checks your permission and the current version before any online action."]


@router.post("/v2/business/documents/{artifact_id}/offline-manifest")
@db_route
def offline_manifest(request: Request, artifact_id: str):
    """FS-O1 (disabled by default): a manifest that lets a trusted device keep a labelled, expiring offline copy of ONE available document. Needs the base document's permission, an
    explicit consent flag and the deployment mode `trusted-device`; the bytes themselves still come from the ordinary content route. SEC-05 (2026-10-30): recording the consent
    writes an audit row, so this is a POST (origin checked, human actor) with `{workspace_id, consent: true}`; a GET never writes."""
    mode = os.environ.get("RIPPLE_OFFLINE_REFERENCE", "off")
    body = _json_body_sync(request)
    workspace_id, consent = (body.get("workspace_id") if isinstance(body, dict) else None), (body.get("consent") if isinstance(body, dict) else None)

    def action(store, tenant, authority):
        if mode != "trusted-device":
            return JSONResponse({"error": "offline reference mode is off", "code": "offline_reference_off"}, status_code=503, headers=_NO_STORE)
        if consent is not True and str(consent).lower() != "true":
            return JSONResponse({"error": "explicit consent required", "code": "consent_required", "rules": OFFLINE_RULES}, status_code=409, headers=_NO_STORE)
        from infra.adapters import v2_business_documents as docs
        from infra.v2_document_routes import _authorized_state
        st = _authorized_state(store, authority, workspace_id, artifact_id)
        if st["freshness"] not in ("available", "stale"):
            return JSONResponse({"error": "document not available", "code": "not_available"}, status_code=409, headers=_NO_STORE)
        import datetime
        from psycopg2.extras import Json
        days = int(os.environ.get("RIPPLE_OFFLINE_REFERENCE_DAYS", "7") or 7)
        now = datetime.datetime.now(datetime.timezone.utc)
        manifest = {"schema": "ripple-offline-reference/1", "artifact_id": artifact_id, "snapshot_id": st["snapshot_id"], "mode": st["mode"], "content_hash": st["content_hash"], "size_bytes": st["size_bytes"],
                    "freshness_at_download": st["freshness"], "downloaded_at": now.isoformat(), "expires_at": (now + datetime.timedelta(days=days)).isoformat(), "content_path": f"/v2/business/documents/{artifact_id}/content",
                    "label": f"offline copy - knowledge version {st['snapshot_id']} - downloaded {now.date().isoformat()} - current authority not checked", "current_authority_checked": False,
                    "purge_on": ["logout", "account_switch", "workspace_switch", "expiry"], "rules": OFFLINE_RULES}
        with store._tx() as cur:
            cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,'OFFLINE_REFERENCE_MANIFEST',%s,%s)",
                        (tenant, workspace_id, authority["account_id"], Json({"artifact_id": artifact_id, "snapshot_id": st["snapshot_id"], "expires_at": manifest["expires_at"]})))
        return manifest
    return _shell(request, workspace_id, action)


@router.get("/v2/business/documents/{artifact_id}/offline-manifest")
def offline_manifest_get(request: Request, artifact_id: str, workspace_id: str = "", consent: str = "false"):
    """A read never records consent (SEC-05): the manifest is issued by the POST above."""
    return JSONResponse({"error": "consent is recorded by POST with {workspace_id, consent: true}", "code": "use_post"}, status_code=405,
                        headers={**_NO_STORE, "Allow": "POST"})


@router.post("/v2/notifications/dispatch")
@db_route
def dispatch(request: Request):
    body = _json_body_sync(request)

    def action(store, tenant, authority):
        t = transport()
        if t is None:
            return JSONResponse({"error": "notification transport off", "code": "transport_off"}, status_code=503, headers=_NO_STORE)
        return v2_notifications.dispatch_once(store, t, tenant, body["workspace_id"])
    return _shell(request, body.get("workspace_id"), action, permission="membership.manage")
