"""Public config stays generic while Cloud Run supplies its GCP project."""

import json
from pathlib import Path
from unittest.mock import patch

from ai_module.accounts import get_account, resolve_project_id
from ai_module.router import AIRouter
from providers.vertex import _settings


ROOT = Path(__file__).resolve().parents[2]


def test_public_config_contains_no_account_identity():
    account = get_account("default")
    routing = json.loads((ROOT / "ai_module/config/routing.config.json").read_text(encoding="utf-8"))
    legacy = json.loads((ROOT / "ai_module/config/ai.config.json").read_text(encoding="utf-8"))

    assert "email" not in account
    assert account["project_id"] == ""
    assert routing["text_generation"]["account_id"] == account["id"]
    assert routing["image_generation"]["account_id"] == account["id"]
    assert routing["image_generation"]["gcs_bucket"] == ""
    assert legacy["vertex_ai"]["project_id"] == ""


def test_vertex_project_comes_from_environment(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "sample-cloud-project")
    account = get_account("default")
    assert resolve_project_id(account) == "sample-cloud-project"

    with patch("ai_module.hub.AIHub.build_vertex_client", return_value=object()) as build:
        AIRouter().get_text_client()
    assert build.call_args.args[0]["project_id"] == "sample-cloud-project"
    assert _settings()["project"] == "sample-cloud-project"
