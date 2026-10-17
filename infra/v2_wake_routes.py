"""`POST /internal/wake`: the Cloud Scheduler backstop of the on-demand worker. It does NOT start an execution by itself: it asks `wake.kick`, which starts the Cloud Run Job only when there is claimable work and no live
worker (so a tick with nothing to do costs one tiny web request and no worker execution). Authenticated by a Google ID token whose email is the Scheduler service account named in RIPPLE_WAKE_SCHEDULER_SA and whose
audience is RIPPLE_WAKE_AUDIENCE; anything else gets 401 with no detail. It reads no source and calls no provider."""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.worker import wake

router = APIRouter()


def _verify(token: str, audience: str) -> dict:
    from google.auth.transport import requests as grequests
    from google.oauth2 import id_token
    return id_token.verify_oauth2_token(token, grequests.Request(), audience=audience)


@router.post("/internal/wake")
async def internal_wake(request: Request):
    sa, aud = os.environ.get("RIPPLE_WAKE_SCHEDULER_SA"), os.environ.get("RIPPLE_WAKE_AUDIENCE")
    auth = request.headers.get("authorization", "")
    if not (sa and aud and auth.lower().startswith("bearer ")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        claims = _verify(auth[7:].strip(), aud)
    except Exception:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if claims.get("email") != sa or not claims.get("email_verified", False):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from infra.v2_policy_routes import _v2_store
    store = _v2_store()
    if store is None:
        return JSONResponse({"error": "unavailable"}, status_code=503)
    return JSONResponse(wake.kick(store), headers={"Cache-Control": "no-store"})
