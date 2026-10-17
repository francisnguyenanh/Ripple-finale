# -*- coding: utf-8 -*-
"""Loader cho config/accounts.json -- danh sach tai khoan/project GCP + AI Studio."""

import json
import os

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_ACCOUNTS_PATH = os.path.join(_MODULE_DIR, "config", "accounts.json")
_AISTUDIO_KEYS_PATH = os.path.join(_MODULE_DIR, "credentials", "aistudio", "api_keys.json")


def load_accounts(path: str = _ACCOUNTS_PATH) -> list:
    """Doc toan bo danh sach account tu config/accounts.json."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("accounts", [])


def get_account(account_id: str, path: str = _ACCOUNTS_PATH) -> dict:
    """Lay 1 account theo id; raise neu khong tim thay."""
    for acc in load_accounts(path):
        if acc.get("id") == account_id:
            return acc
    raise ValueError(f"Khong tim thay account_id='{account_id}' trong {path}")


def resolve_project_id(account: dict) -> str:
    """Use the deployment's GCP project, with a local config fallback."""
    return os.environ.get("GCP_PROJECT", "").strip() or (account.get("project_id") or "").strip()


def resolve_service_account_path(account: dict) -> str:
    """Tra ve duong dan tuyet doi toi service-account JSON (neu account dung auth='service_account')."""
    key_file = (account.get("service_account_json") or "").strip()
    if not key_file:
        return ""
    if not os.path.isabs(key_file):
        key_file = os.path.join(os.path.dirname(_MODULE_DIR), key_file)
    return key_file


def resolve_aistudio_api_key(account: dict, path: str = _AISTUDIO_KEYS_PATH) -> str:
    """Lay AI Studio API key cho 1 account.

    Uu tien doc tu credentials/aistudio/api_keys.json (key ten = account['id']
    hoac account['aistudio_api_key_env']), fallback sang bien moi truong
    account['aistudio_api_key_env'] neu file khong co gia tri tuong ung -- de
    tuong thich nguoc voi setup cu dung .env.
    """
    env_var = account.get("aistudio_api_key_env", "GEMINI_API_KEY")

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            keys = json.load(f)
        key = keys.get(account.get("id", "")) or keys.get(env_var)
        if key:
            return key

    return os.environ.get(env_var, "")
