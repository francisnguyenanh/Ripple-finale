"""Offline browser fixture, never imported by the deployed service.

Run: python -m tests.browser_preview
Open http://localhost:8765 and use the invitation code printed by this runner.
The provider is a test double; do not cite this as a live Gemini demonstration.
"""
import os
from unittest.mock import patch

import uvicorn
from fastapi.testclient import TestClient
from starlette.responses import Response

from app import main
from infra import judge
from tests.intent_support import model_result


if __name__ == "__main__":
    code = "offline-review-invitation-2026"
    os.environ["RIPPLE_JUDGE_ACCESS_CODE"] = code
    os.environ["RIPPLE_JUDGE_COOKIE_KEY"] = "offline-fixture-cookie-key-only-2026"
    os.environ["RIPPLE_JUDGE_ORIGIN"] = "http://localhost:8765"
    client = TestClient(main.app)
    def forward(body):
        response = client.post("/rpc", json=body)
        return response.status_code, response.json()
    original_cookie = Response.set_cookie
    def local_cookie(self, *args, **kwargs):
        # This runner is explicitly HTTP-only and never ships as the BFF.
        kwargs["secure"] = False
        return original_cookie(self, *args, **kwargs)
    print(f"OFFLINE TEST FIXTURE - invitation: {code}", flush=True)
    with patch.object(judge, "_forward", forward), patch.object(Response, "set_cookie", local_cookie), patch(
        "infra.adapters.cloud_adapters.generate_intent_from_prompt", return_value=model_result()):
        uvicorn.run(judge.app, host="127.0.0.1", port=8765)
