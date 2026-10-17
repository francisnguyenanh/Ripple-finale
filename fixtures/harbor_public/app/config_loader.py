"""Configuration loader and schema validator for Harbor Procurement Mini."""

from pathlib import Path
from typing import Any, Union
import yaml
from app.enums import RouteType
from app.models import ApprovalConfig


def load_approval_config(config_path: Union[str, Path]) -> ApprovalConfig:
    """Loads and validates approval and matching rules configuration from YAML."""
    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid configuration format in {path}: expected dictionary root")

    version = str(data.get("version", "1.0.0"))
    currency = str(data.get("currency", "JPY"))
    if currency != "JPY":
        raise ValueError(f"Unsupported system currency: {currency}")

    routing = data.get("routing_rules", {})
    matching = data.get("matching_rules", {})

    std_threshold = routing.get("standard_manager_threshold_jpy")
    capex_threshold = routing.get("capex_finance_threshold_jpy")
    high_risk_route_str = routing.get("high_risk_route", "NEEDS_DIRECTOR")

    if not isinstance(std_threshold, int) or std_threshold <= 0:
        raise ValueError("routing_rules.standard_manager_threshold_jpy must be positive integer")
    if not isinstance(capex_threshold, int) or capex_threshold <= 0:
        raise ValueError("routing_rules.capex_finance_threshold_jpy must be positive integer")

    fixed_tol = matching.get("price_tolerance_fixed_jpy", 10_000)
    pct_tol = matching.get("price_tolerance_percent", 2)
    terms = matching.get("payment_term_days", 30)

    return ApprovalConfig(
        version=version,
        currency=currency,
        standard_manager_threshold_jpy=std_threshold,
        capex_finance_threshold_jpy=capex_threshold,
        high_risk_route=RouteType(high_risk_route_str),
        price_tolerance_fixed_jpy=fixed_tol,
        price_tolerance_percent=pct_tol,
        payment_term_days=terms,
    )
