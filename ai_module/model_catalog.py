# -*- coding: utf-8 -*-
"""Loader cho config/model_catalog.json -- bang gia + level (thap->cao) cho model
gen text/suy luan va gen anh. Gia la ESTIMATE de du toan ngan sach, khong phai
bill chinh thuc (xem Cloud Billing console de biet so du that).
"""

import json
import os

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CATALOG_PATH = os.path.join(_MODULE_DIR, "config", "model_catalog.json")


def load_catalog(path: str = _CATALOG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_text_tiers(path: str = _CATALOG_PATH) -> list:
    """Danh sach tier text-gen/reasoning, sap xep level thap (re) -> cao (dat)."""
    tiers = load_catalog(path)["text_generation"]["tiers"]
    return sorted(tiers, key=lambda t: t["level"])


def get_text_tier(tier: str, path: str = _CATALOG_PATH) -> dict:
    for t in list_text_tiers(path):
        if t["tier"] == tier:
            return t
    raise ValueError(f"Khong tim thay text tier='{tier}'. Cac tier hop le: "
                      f"{[t['tier'] for t in list_text_tiers(path)]}")


def list_known_models(path: str = _CATALOG_PATH) -> dict[str, dict]:
    """Danh sach model va metadata tu catalog."""
    return load_catalog(path).get("text_generation", {}).get("models", {})


def get_model_info(model: str, path: str = _CATALOG_PATH) -> dict | None:
    """Lay metadata cua mot model cu the (tier, supports_thinking, providers)."""
    models = list_known_models(path)
    return models.get(model)


def estimate_text_cost(tier: str, input_tokens: int, output_tokens: int,
                        batch: bool = False, path: str = _CATALOG_PATH) -> float:
    t = get_text_tier(tier, path)
    catalog = load_catalog(path)
    discount = catalog["text_generation"]["batch_discount_ratio"] if batch else 1.0
    input_cost = input_tokens / 1_000_000 * t["price_input_per_1m"] * discount
    output_cost = output_tokens / 1_000_000 * t["price_output_per_1m"] * discount
    return input_cost + output_cost


def list_image_tiers(path: str = _CATALOG_PATH) -> list:
    tiers = load_catalog(path)["image_generation"]["tiers"]
    return sorted(tiers, key=lambda t: t["level"])


def get_image_tier(tier: str, path: str = _CATALOG_PATH) -> dict:
    for t in list_image_tiers(path):
        if t["tier"] == tier:
            return t
    raise ValueError(f"Khong tim thay image tier='{tier}'. Cac tier hop le: "
                      f"{[t['tier'] for t in list_image_tiers(path)]}")


def estimate_image_cost(tier: str, prompt_tokens: int = 0, batch: bool = False,
                         path: str = _CATALOG_PATH) -> float:
    t = get_image_tier(tier, path)
    catalog = load_catalog(path)
    image_gen = catalog["image_generation"]
    discount = image_gen["batch_discount_ratio"] if batch else 1.0
    input_price = image_gen["input_text_price_per_1m"].get(t["model"], 0.0)
    input_cost = prompt_tokens / 1_000_000 * input_price * discount
    return t["price_per_image"] * discount + input_cost
