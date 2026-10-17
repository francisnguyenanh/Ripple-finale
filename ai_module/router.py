# -*- coding: utf-8 -*-
"""AIRouter -- diem vao duy nhat de lay client text-gen/reasoning hoac image-gen
theo cau hinh thu cong trong config/routing.config.json, tra cuu account trong
config/accounts.json va model/gia trong config/model_catalog.json.

Dung trong bat ky project nao:
    from ai_module.router import AIRouter
    router = AIRouter()

    # Text / reasoning
    client, model = router.get_text_client()          # dung dung config/routing.config.json
    reply = client.ask("Xin chao")

    client, model = router.get_text_client(tier="reasoning", account_id="acc_vti_secondary")

    # Image gen -- tra ve (client, model, image_size) de dung voi generate_images.py
    client, model, image_size = router.get_image_client()

Sua config/routing.config.json de doi mac dinh (khong can dong code); truyen
tham so vao get_text_client()/get_image_client() de override tam thoi cho 1 lan goi.
"""

import json
import os

from ai_module.accounts import get_account, resolve_project_id, resolve_service_account_path, resolve_aistudio_api_key
from ai_module import model_catalog

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_ROUTING_PATH = os.path.join(_MODULE_DIR, "config", "routing.config.json")


def load_routing(path: str = _ROUTING_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class AIRouter:
    def __init__(self, routing_path: str = _ROUTING_PATH):
        self.routing_path = routing_path
        self.routing = load_routing(routing_path)

    # ── Text / reasoning ─────────────────────────────────────────────────

    def get_text_client(self, *, backend: str = None, account_id: str = None,
                         tier: str = None, model: str = None, location: str = None):
        """Tra ve (client, model_name). client co .ask(prompt) / .start_chat() (vertex)
        hoac la mot google.genai.Client tho (aistudio).

        `location` (P26A EndpointLocation.id, e.g. "global" or a region like
        "us-central1") — when supplied — is the region actually used for the
        vertex backend. Without it, routing's GLOBAL/MULTI_REGION/REGIONAL
        decision was computed and recorded for audit but never fed back
        into which endpoint the request actually hit; every call silently
        used the account's `default_region` regardless of what routing
        decided.
        """
        cfg = self.routing["text_generation"]
        backend = backend or cfg["backend"]
        account_id = account_id or cfg["account_id"]
        tier = tier or cfg["tier"]
        model = model or (cfg.get("model_override") or "").strip() or None

        tier_info = None
        try:
            tier_info = model_catalog.get_text_tier(tier) if tier else None
        except Exception:
            tier_info = None
        resolved_model = model or (tier_info["model"] if tier_info else "gemini-3.5-flash-lite")

        if backend == "vertex":
            account = get_account(account_id)
            from ai_module.hub import AIHub
            va_cfg = {
                "project_id": resolve_project_id(account),
                "service_account_json": resolve_service_account_path(account) if account.get("auth") == "service_account" else "",
                "model": resolved_model,
                "fallback_model": resolved_model,
                "regions": location or account.get("default_region", "us-central1"),
            }
            client = AIHub.build_vertex_client(va_cfg)
            return client, resolved_model

        if backend == "aistudio":
            account = get_account(account_id)
            from google import genai
            api_key = resolve_aistudio_api_key(account)
            if not api_key:
                raise SystemExit(
                    f"Khong tim thay AI Studio API key cho account '{account_id}' "
                    f"(kiem tra credentials/aistudio/api_keys.json)"
                )
            client = genai.Client(api_key=api_key)
            return client, resolved_model

        raise ValueError(f"backend khong hop le: {backend} (chi ho tro 'vertex' hoac 'aistudio')")

    # ── Image gen ────────────────────────────────────────────────────────

    def get_image_client(self, *, backend: str = None, account_id: str = None,
                          tier: str = None, model: str = None, image_size: str = None):
        """Tra ve (client, model_name, image_size) de dung truc tiep voi
        generate_images.py's run_online/run_batch."""
        cfg = self.routing["image_generation"]
        backend = backend or cfg["backend"]
        account_id = account_id or cfg["account_id"]
        tier = tier or cfg["tier"]
        model = model or (cfg.get("model_override") or "").strip() or None
        image_size = image_size or (cfg.get("image_size_override") or "").strip() or None

        tier_info = model_catalog.get_image_tier(tier)
        resolved_model = model or tier_info["model"]
        resolved_size = image_size or tier_info["image_size"]
        account = get_account(account_id)

        from google import genai

        if backend == "vertex":
            project_id = resolve_project_id(account)
            if not project_id:
                raise SystemExit("Set GCP_PROJECT before using Vertex AI image generation")
            client = genai.Client(vertexai=True, project=project_id, location=account.get("default_region", "global"))
            return client, resolved_model, resolved_size

        if backend == "aistudio":
            api_key = resolve_aistudio_api_key(account)
            if not api_key:
                raise SystemExit(
                    f"Khong tim thay AI Studio API key cho account '{account_id}' "
                    f"(kiem tra credentials/aistudio/api_keys.json)"
                )
            client = genai.Client(api_key=api_key)
            return client, resolved_model, resolved_size

        raise ValueError(f"backend khong hop le: {backend} (chi ho tro 'vertex' hoac 'aistudio')")
