# -*- coding: utf-8 -*-
"""The Vertex AI boundary, outside `core/` on purpose.

`core.change_intelligence` imports no provider anywhere. Everything that
needs a model takes one as an argument, so the product runs end to end with
none -- producing a smaller Knowledge Model and an explanation that says no
model was available, rather than a silent fallback to guessing.

This module is the only place that knows Vertex exists. Swapping providers,
or running the whole suite offline, is a matter of what gets passed in.

Authentication is Application Default Credentials. No
service-account key file is needed for this project, and a key file that
does not exist cannot end up in a public submission repository.
"""

from __future__ import annotations

import json
import os
import pathlib
import time

import google.auth
import google.auth.transport.requests as gauth_requests
import requests

_CONFIG = (pathlib.Path(__file__).resolve().parents[1]
           / "ai_module" / "config" / "ai.config.json")

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _settings() -> dict:
    config = json.loads(_CONFIG.read_text(encoding="utf-8"))["vertex_ai"]
    project = os.environ.get("GCP_PROJECT", "").strip() or (config.get("project_id") or "").strip()
    if not project:
        raise RuntimeError("GCP_PROJECT is required for Vertex AI")
    return {
        "project": project,
        #: `global` rather than a regional host. Pinned in configuration
        #: rather than inferred, because a wrong region fails as a 404 on a
        #: model name and sends the reader looking at the model.
        "location": os.environ.get("VERTEX_LOCATION")
                    or (config.get("regions") or "global").split(",")[0].strip(),
        "model": os.environ.get("VERTEX_MODEL") or config["model"],
        "fallback": config.get("fallback_model", ""),
    }


def endpoint(location: str, project: str, model: str) -> str:
    host = ("aiplatform.googleapis.com" if location == "global"
            else f"{location}-aiplatform.googleapis.com")
    return (f"https://{host}/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:generateContent")


class VertexExplainer:
    """Turns a prompt into text, and records what it cost to do so.

    Carries no opinion about the product. It does not know what a conflict
    is, cannot decide one, and returns a string -- every check on that
    string happens in `core.change_intelligence.explain`, where it can be
    tested without a network.
    """

    name = "vertex"

    def __init__(self, *, temperature: float = 0.0, max_output_tokens: int = 3000, timeout: float = 120):
        self._timeout = timeout
        self._settings = _settings()
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._creds = None
        #: model id, region, latency and tokens for every call. The audit
        #: trail has to say which model produced which claim; collecting it
        #: at the boundary is the only place it cannot be forgotten.
        self.calls: list[dict] = []

    def _token(self) -> str:
        if self._creds is None:
            self._creds, _ = google.auth.default(scopes=_SCOPES)
        if not self._creds.valid:
            self._creds.refresh(gauth_requests.Request())
        return self._creds.token

    def explain(self, prompt: str) -> str:
        settings = self._settings
        url = endpoint(settings["location"], settings["project"], settings["model"])

        started = time.time()
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {self._token()}"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                  "generationConfig": {
                      "temperature": self._temperature,
                      "maxOutputTokens": self._max_output_tokens}},
            timeout=self._timeout)
        latency_ms = int((time.time() - started) * 1000)

        if response.status_code != 200:
            raise RuntimeError(
                f"Vertex AI {response.status_code}: {response.text[:300]}")

        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            # A safety block or an empty candidate list is an abstention, not
            # an answer. Returning "" lets the caller record it as one.
            self._record(settings, latency_ms, payload, blocked=True)
            return ""

        parts = candidates[0].get("content", {}).get("parts", [])
        self._record(settings, latency_ms, payload)
        return "".join(p.get("text", "") for p in parts)

    def _record(self, settings: dict, latency_ms: int, payload: dict,
                *, blocked: bool = False) -> None:
        usage = payload.get("usageMetadata", {})
        self.calls.append({
            "model_id": settings["model"],
            "region": settings["location"],
            "latency_ms": latency_ms,
            "tokens": usage.get("totalTokenCount", 0),
            "blocked": blocked,
        })
