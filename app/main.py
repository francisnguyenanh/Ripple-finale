import time
import uuid
import logging
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.responses import JSONResponse
from app.api.router import Api, Request as ApiRequest
from core.change_intelligence import public_surface
from infra.adapters.cloud_adapters import (
    StaleSessionVersion,
    build_cloud_registry,
    judge_max_commands,
    judge_max_model_calls,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ripple")

app = FastAPI(title="Ripple API (P9)")

# Build registry from Cloud SQL and Cloud Storage when available
registry = build_cloud_registry()
api = Api(sessions=registry)

# Successful command responses live in session.work, committed with their effects.
_WRITE_OPERATIONS = frozenset(o.name for o in public_surface.writing_operations())


# A bad quota value fails the revision at startup, not the first judge command.
judge_max_commands(), judge_max_model_calls()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ready")
def ready():
    if registry.backend is not None:
        registry.backend.ping()
    return {"status": "ok"}

@app.post("/rpc")
async def rpc_endpoint(req: FastAPIRequest):
    return handle_rpc(await req.json())


def handle_rpc(body: dict) -> JSONResponse:
    """The /rpc command path, callable in-process by the single-service BFF."""
    start_time = time.time()
    req_id = body.get("request_id") or uuid.uuid4().hex
    operation = body.get("operation", "unknown")

    import hashlib
    import json
    session_id = body.get("session_id", "")
    fingerprint = hashlib.sha256(json.dumps(
        {k: body.get(k) for k in ("operation", "session_id", "payload", "actor")},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if session_id and registry.backend is not None and hasattr(registry.backend, "fetch_session"):
        # One authoritative snapshot per command, retained until its CAS commit.
        registry._sessions.pop(session_id, None)
    if session_id and operation in _WRITE_OPERATIONS:
        try:
            session = registry.get(session_id)
            cached = session.work.get("handled_request", {}).get(req_id)
        except Exception:
            cached = None
        if cached:
            if cached["fingerprint"] != fingerprint:
                return JSONResponse(status_code=409, content={"ok": False, "code": "request_id_reused"})
            return JSONResponse(status_code=cached["status"], content=cached["content"])

    logger.info(f"request_id={req_id} operation={operation} status=STARTED")

    api_req = ApiRequest(
        operation=operation,
        session_id=body.get("session_id", ""),
        payload=body.get("payload", {}),
        actor=body.get("actor", ""),
        timestamp=body.get("timestamp", ""),
        request_id=req_id
    )

    quota_changed = False
    if api_req.actor.startswith("browser-judge:") and api_req.session_id:
        from datetime import datetime, timezone
        try:
            session = registry.get(api_req.session_id)
            created = datetime.fromisoformat(session.created_at.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - created).total_seconds() > 3600:
                return JSONResponse({"ok": False, "error": "session expired", "code": "session_expired"}, status_code=410)
            if operation in _WRITE_OPERATIONS:
                usage = session.work.setdefault("judge_usage", {"commands": 0, "model_calls": 0})
                if usage["commands"] >= judge_max_commands() or (
                        operation == "intent.parse" and usage["model_calls"] >= judge_max_model_calls()):
                    return JSONResponse({"ok": False, "error": "session quota reached", "code": "quota_exceeded"}, status_code=429)
                usage["commands"] += 1
                usage["model_calls"] += int(operation == "intent.parse")
                quota_changed = True
        except Exception:
            return JSONResponse({"ok": False, "error": "session unavailable", "code": "unknown_session"}, status_code=404)

    reply = api.call(api_req)
    latency_ms = int((time.time() - start_time) * 1000)

    content = {"ok": reply.ok, "data": reply.data, "error": reply.error,
               "code": reply.code, "request_id": reply.request_id, "operation": reply.operation}
    if reply.ok and session_id and operation in _WRITE_OPERATIONS:
        registry.get(session_id).put("handled_request", req_id,
            {"fingerprint": fingerprint, "status": reply.status, "content": content})

    # Persist to Cloud SQL if a multi-instance backend is active. Only for
    # operations that actually write -- a read has nothing to durably commit,
    # and persisting the whole session on every read makes
    # every request pay for a write that never happened.
    ok = reply.ok
    error = reply.error
    code = reply.code
    status = reply.status
    if (registry.backend is not None and api_req.session_id and (reply.ok or quota_changed)
            and operation in _WRITE_OPERATIONS):
        try:
            session = registry.get(api_req.session_id)
            new_version = registry.backend.persist_session(
                session, expected_version=session.durable_version)
            session.durable_version = new_version
        except StaleSessionVersion as e:
            # Another instance's write landed on
            # this session between our read and our write. Discarding the
            # in-process copy forces the next request to re-fetch the
            # authoritative row rather than keep building on a fork nobody
            # else can see.
            logger.warning(f"Stale session version for {api_req.session_id}: {e}")
            registry._sessions.pop(api_req.session_id, None)
            ok = False
            error = str(e)
            code = "stale_version"
            status = 409
        except Exception as e:
            logger.warning(f"Could not persist session {api_req.session_id} to Cloud SQL: {e}")
            # A command is not successful until its durable commit is. The
            # in-process cache now holds a mutation nobody outside this
            # instance can see; discard it so the next request re-fetches
            # the last DURABLE state rather than building on a mutation
            # that will vanish the moment this instance restarts.
            registry._sessions.pop(api_req.session_id, None)
            ok = False
            error = "session mutated but the durable commit failed; retry"
            code = "persistence_failed"
            status = 503

    logger.info(f"request_id={req_id} operation={operation} status={'OK' if ok else 'ERROR'} latency_ms={latency_ms} code={code}")

    content = {
        "ok": ok,
        "data": reply.data if ok else None,
        "error": error,
        "code": code,
        "request_id": reply.request_id,
        "operation": reply.operation
    }
    return JSONResponse(status_code=status, content=content)
