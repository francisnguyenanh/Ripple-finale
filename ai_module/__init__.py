"""
Unified AI module for reusable integration across projects.

Exports:
- AIHub: factory/helper for Vertex AI
- AIRouter: pick backend/account/model-tier via config/routing.config.json
  (multi-GCP-account + multi-model-tier support), see config/accounts.json
  and config/model_catalog.json
"""

from ai_module.hub import AIHub
from ai_module.router import AIRouter
from ai_module.vertex_client import GeminiClient, ChatSession
from ai_module import model_catalog
from ai_module import accounts

__all__ = [
    "AIHub",
    "AIRouter",
    "GeminiClient",
    "ChatSession",
    "model_catalog",
    "accounts",
]
