"""Listener tokens for the realtime feeds (extension FS-R1 / FS-C1): the server decides every claim, the Firestore rules enforce them.

A token carries: `ripple_feeds` (from `v2_projection.feed_grants` over the member's CURRENT permissions — never from the request), `ripple_ws` (the workspace feed id, also the
policy document id), `ripple_policy` (the workspace's membership policy version at issue) and `ripple_until_ms` (issue time + TTL). The rules (`config/firestore.rules`) accept a
read only for a listed feed, before `ripple_until_ms`, and while `feed_policies/{ripple_ws}.policy_version` equals `ripple_policy`. Every membership grant or revoke bumps the
policy version, and the backend mirrors it into `feed_policies` (on revoke/grant, on each projection run and on every issue), so a revoked member's token stops working at once
and the remaining members simply ask for a new one. The subject is an opaque hash, never the account id.

Issuers: `RIPPLE_FEED_TOKEN_ISSUER` = off (default) | emulator. The emulator issuer signs NOTHING (alg none): only the local Firestore emulator accepts such a token, and it is
available only together with the emulator sink (loopback host, `demo-` project). Minting a real Firebase custom token needs the owner's Firebase project and a server credential;
it is the activation step and is not built.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os

TTL_SECONDS = 900
SCHEMA = "ripple-feed-token/1"


class IssuerRefused(ValueError):
    pass


def subject(tenant_id: str, account_id: str) -> str:
    return "rpl-" + hashlib.sha256(f"{tenant_id}/{account_id}".encode("utf-8")).hexdigest()[:24]


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode("utf-8")).rstrip(b"=").decode("ascii")


class EmulatorIssuer:
    kind = "emulator-unsigned"

    def __init__(self, project_id: str):
        if not project_id.startswith("demo-"):
            raise IssuerRefused("the emulator issuer needs a demo- project")
        self.project_id = project_id

    def issue(self, *, sub: str, feeds: list, ws_feed: str, policy_version: int, now: datetime.datetime | None = None, ttl: int = TTL_SECONDS) -> dict:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        iat = int(now.timestamp())
        until_ms = (iat + ttl) * 1000
        claims = {"iss": f"https://securetoken.google.com/{self.project_id}", "aud": self.project_id, "sub": sub, "user_id": sub, "iat": iat, "exp": iat + ttl, "auth_time": iat,
                  "firebase": {"sign_in_provider": "custom", "identities": {}}, "ripple_feeds": list(feeds), "ripple_ws": ws_feed, "ripple_policy": int(policy_version),
                  "ripple_until_ms": until_ms}
        token = _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(claims) + "."
        return {"schema": SCHEMA, "token": token, "token_kind": self.kind, "expires_at": datetime.datetime.fromtimestamp(iat + ttl, datetime.timezone.utc).isoformat(),
                "feeds": list(feeds), "policy_version": int(policy_version)}


def issuer():
    """The configured issuer or None (off). The emulator issuer exists only with the emulator sink configuration."""
    if os.environ.get("RIPPLE_FEED_TOKEN_ISSUER", "off") != "emulator":
        return None
    if os.environ.get("RIPPLE_PROJECTION_SINK") != "firestore-emulator":
        raise IssuerRefused("the emulator issuer needs RIPPLE_PROJECTION_SINK=firestore-emulator")
    return EmulatorIssuer(os.environ.get("RIPPLE_FIRESTORE_PROJECT", ""))
