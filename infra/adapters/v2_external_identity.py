"""External identity exchange (extension FB-A1, local build): a verified external ID token -> an ordinary Ripple session for the account a person linked to that subject.

Trust boundary: the external provider is an identity provider only. Authorization is always the current SQL membership (`v2_memberships`), never a claim of the token; claims are
not read for roles at all. A subject is linked to one account by a person holding `membership.manage` (invitation/admin mapping; no auto-join from an e-mail or domain); a link is
revoked the same way and the revocation does not touch existing memberships or decisions. The verifier is a port: `MockVerifier` only when the deployment is explicitly in
`local-mock` mode (tests, emulator-like local runs); `AdminSdkVerifier` is the production adapter that uses the official Admin SDK when it is installed and configured. Nothing here
prints or stores a token, refresh token or key; audit rows carry reason codes only.
"""
from __future__ import annotations

import json
import os
import time

from psycopg2.extras import Json

from infra.adapters.v2_identity import IdentityDenied

SCHEMA = "ripple-external-identity/1"
MODES = ("off", "local-mock", "admin-sdk")
ISSUER_PREFIX = "https://securetoken.google.com/"


class VerifierError(Exception):
    """The token could not be verified (bad signature, expired, wrong shape). Message is a reason code, never token content."""


class VerifierUnavailable(Exception):
    """The configured verifier cannot run here (library or configuration missing)."""


class ExchangeDenied(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def settings() -> dict:
    mode = os.environ.get("RIPPLE_FIREBASE_MODE", "off")
    if mode not in MODES:
        mode = "off"
    return {"mode": mode, "project_id": os.environ.get("RIPPLE_FIREBASE_PROJECT_ID", ""), "identity_tenant": os.environ.get("RIPPLE_FIREBASE_TENANT", ""),
            "max_auth_age_seconds": int(os.environ.get("RIPPLE_FIREBASE_MAX_AUTH_AGE_SECONDS", "300") or 300),
            "session_lifetime_seconds": int(os.environ.get("RIPPLE_FIREBASE_SESSION_SECONDS", "3600") or 3600)}


class MockVerifier:
    """Local-mock mode only: the "token" is `mock:` + a JSON object with the claims the real verifier would return. Never valid outside `RIPPLE_FIREBASE_MODE=local-mock`."""

    def verify(self, id_token: str) -> dict:
        if not isinstance(id_token, str) or not id_token.startswith("mock:"):
            raise VerifierError("token_shape")
        try:
            claims = json.loads(id_token[5:])
        except ValueError:
            raise VerifierError("token_shape") from None
        if not isinstance(claims, dict) or not all(isinstance(claims.get(k), str) and claims.get(k) for k in ("iss", "aud", "sub")):
            raise VerifierError("claims_missing")
        return {"issuer": claims["iss"], "audience": claims["aud"], "subject": claims["sub"], "identity_tenant": claims.get("tenant") or "", "email_verified": bool(claims.get("email_verified", True)),
                "expires_at": int(claims.get("exp", time.time() + 60)), "auth_time": int(claims.get("auth_time", time.time()))}


class AdminSdkVerifier:
    """Production adapter: Firebase Admin SDK `verify_id_token(check_revoked=True)`. Imported lazily; absent library or app -> VerifierUnavailable (503), never a fallback to mock."""

    def __init__(self, project_id: str):
        self.project_id = project_id

    def verify(self, id_token: str) -> dict:
        try:
            import firebase_admin                                                   # noqa: F401  (optional dependency, never required for the local profile)
            from firebase_admin import auth as fb_auth
        except ImportError:
            raise VerifierUnavailable("firebase_admin_not_installed") from None
        try:
            claims = fb_auth.verify_id_token(id_token, check_revoked=True)
        except Exception as exc:                                                     # noqa: BLE001 - every SDK error is one reason code; no token content
            raise VerifierError(type(exc).__name__) from None
        firebase = claims.get("firebase") or {}
        return {"issuer": claims.get("iss", ""), "audience": claims.get("aud", ""), "subject": claims.get("sub", ""), "identity_tenant": firebase.get("tenant") or "",
                "email_verified": bool(claims.get("email_verified", False)), "expires_at": int(claims.get("exp", 0)), "auth_time": int(claims.get("auth_time", 0))}


def verifier_for(cfg: dict):
    if cfg["mode"] == "local-mock":
        return MockVerifier()
    if cfg["mode"] == "admin-sdk":
        if not cfg["project_id"]:
            raise VerifierUnavailable("project_id_missing")
        return AdminSdkVerifier(cfg["project_id"])
    raise VerifierUnavailable("mode_off")


# ---- links (invitation / admin mapping) ----------------------------------------------------------------------------------------------------------------

def _audit(cur, tenant_id, workspace_id, event, actor_id, payload):
    cur.execute("INSERT INTO v2_audit (tenant_id, workspace_id, event_type, actor_id, payload) VALUES (%s,%s,%s,%s,%s)", (tenant_id, workspace_id, event, actor_id, Json(payload)))


def link(store, tenant_id, workspace_id, *, issuer: str, project_id: str, subject: str, identity_tenant: str = "", account_id: str, actor_id: str) -> dict:
    for name, value in (("issuer", issuer), ("project_id", project_id), ("subject", subject), ("account_id", account_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError(f"{name} required")
    if not isinstance(identity_tenant, str) or len(identity_tenant) > 128:
        raise ValueError("identity_tenant invalid")
    with store._tx() as cur:
        cur.execute("SELECT disabled_at FROM v2_accounts WHERE account_id=%s", (account_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError("unknown account")
        cur.execute("SELECT account_id, revoked_at FROM v2_external_identities WHERE issuer=%s AND project_id=%s AND identity_tenant=%s AND subject=%s", (issuer, project_id, identity_tenant, subject))
        existing = cur.fetchone()
        if existing is not None and existing[1] is None:
            if existing[0] == account_id:
                return {"linked": False, "account_id": account_id, "reason": "already_linked"}
            raise ValueError("subject already linked to another account; revoke that link first")
        if existing is not None:
            cur.execute("UPDATE v2_external_identities SET account_id=%s, tenant_id=%s, workspace_id=%s, linked_by=%s, linked_at=now(), revoked_at=NULL, revoked_by=NULL WHERE issuer=%s AND project_id=%s AND identity_tenant=%s AND subject=%s",
                        (account_id, tenant_id, workspace_id, actor_id, issuer, project_id, identity_tenant, subject))
        else:
            cur.execute("INSERT INTO v2_external_identities (issuer, project_id, subject, identity_tenant, account_id, tenant_id, workspace_id, linked_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (issuer, project_id, subject, identity_tenant, account_id, tenant_id, workspace_id, actor_id))
        _audit(cur, tenant_id, workspace_id, "external_identity.linked", actor_id, {"issuer": issuer, "project_id": project_id, "identity_tenant": identity_tenant, "subject_hash": _h(subject), "account_id": account_id})
    return {"linked": True, "account_id": account_id}


def revoke_link(store, tenant_id, workspace_id, *, issuer: str, project_id: str, subject: str, identity_tenant: str = "", actor_id: str) -> dict:
    with store._tx() as cur:
        cur.execute("UPDATE v2_external_identities SET revoked_at=now(), revoked_by=%s WHERE issuer=%s AND project_id=%s AND identity_tenant=%s AND subject=%s AND revoked_at IS NULL AND tenant_id=%s RETURNING account_id",
                    (actor_id, issuer, project_id, identity_tenant, subject, tenant_id))
        row = cur.fetchone()
        if row is not None:
            _audit(cur, tenant_id, workspace_id, "external_identity.revoked", actor_id, {"issuer": issuer, "project_id": project_id, "subject_hash": _h(subject), "account_id": row[0]})
    return {"revoked": row is not None, "account_id": row[0] if row else None, "statement": "Revoking a link stops future exchanges; existing sessions and memberships are untouched (revoke them separately)."}


def list_links(store, tenant_id, workspace_id) -> list:
    with store._tx() as cur:
        cur.execute("SELECT issuer, project_id, identity_tenant, subject, account_id, linked_by, linked_at, revoked_at FROM v2_external_identities WHERE tenant_id=%s AND workspace_id=%s ORDER BY linked_at", (tenant_id, workspace_id))
        return [{"issuer": r[0], "project_id": r[1], "identity_tenant": r[2], "subject_hash": _h(r[3]), "account_id": r[4], "linked_by": r[5], "linked_at": r[6].isoformat(), "revoked_at": r[7].isoformat() if r[7] else None} for r in cur.fetchall()]


def _h(subject: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]


# ---- exchange -------------------------------------------------------------------------------------------------------------------------------------------

def exchange(store, identity, id_token, *, cfg: dict | None = None, verifier=None, now: float | None = None) -> dict:
    """Verify -> check issuer/audience/tenant/expiry/freshness -> active link -> enabled account -> session token. Any failure is ExchangeDenied(code) (generic 401 at the route)."""
    cfg = cfg or settings()
    verifier = verifier or verifier_for(cfg)
    now = now or time.time()
    if not isinstance(id_token, str) or not id_token or len(id_token) > 8192:
        raise ExchangeDenied("token_shape")
    try:
        claims = verifier.verify(id_token)
    except VerifierError as exc:
        raise ExchangeDenied(f"verify:{exc}") from None
    expected_issuer = ISSUER_PREFIX + cfg["project_id"]
    if not cfg["project_id"] or claims["issuer"] != expected_issuer or claims["audience"] != cfg["project_id"]:
        raise ExchangeDenied("wrong_project")
    if cfg["identity_tenant"] and claims["identity_tenant"] != cfg["identity_tenant"]:
        raise ExchangeDenied("wrong_tenant")
    if claims["expires_at"] <= now:
        raise ExchangeDenied("expired")
    if cfg["max_auth_age_seconds"] and now - claims["auth_time"] > cfg["max_auth_age_seconds"]:
        raise ExchangeDenied("stale_auth")                                             # a fresh sign-in is required for the exchange; the ordinary session lifetime applies afterwards
    with store._tx() as cur:
        cur.execute("""SELECT e.account_id, e.tenant_id, e.workspace_id, a.disabled_at FROM v2_external_identities e JOIN v2_accounts a ON a.account_id=e.account_id
            WHERE e.issuer=%s AND e.project_id=%s AND e.identity_tenant=%s AND e.subject=%s AND e.revoked_at IS NULL""", (claims["issuer"], claims["audience"], claims["identity_tenant"], claims["subject"]))
        row = cur.fetchone()
    if row is None:
        raise ExchangeDenied("not_linked")
    account_id, tenant_id, workspace_id, disabled = row
    if disabled is not None:
        _deny_audit(store, tenant_id, workspace_id, account_id, "account_disabled")
        raise ExchangeDenied("account_disabled")
    try:
        token = identity.issue_session(account_id, lifetime_seconds=cfg["session_lifetime_seconds"])
    except IdentityDenied as exc:
        _deny_audit(store, tenant_id, workspace_id, account_id, "session_refused")
        raise ExchangeDenied("session_refused") from exc
    with store._tx() as cur:
        _audit(cur, tenant_id, workspace_id, "external_identity.exchanged", account_id, {"issuer": claims["issuer"], "subject_hash": _h(claims["subject"]), "mode": cfg["mode"]})
    return {"token": token, "account_id": account_id}


def _deny_audit(store, tenant_id, workspace_id, account_id, code):
    with store._tx() as cur:
        _audit(cur, tenant_id, workspace_id, "external_identity.denied", account_id, {"code": code})
