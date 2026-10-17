"""Pure financial calculations and date logic for Harbor Procurement Mini."""

from datetime import date, timedelta
from typing import Any


def validate_integer_money(value: Any, name: str, allow_zero: bool = False) -> None:
    """Validates that a financial field is strictly a Python int and respects non-negativity."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__} ({value!r})")
    if allow_zero:
        if value < 0:
            raise ValueError(f"{name} cannot be negative: {value}")
    else:
        if value <= 0:
            raise ValueError(f"{name} must be strictly positive (> 0): {value}")


def calculate_freight(goods_net_jpy: int) -> int:
    """Calculates freight charges according to standard shipping policy.
    
    Freight is 0 JPY for goods net amount >= 500,000 JPY; otherwise 2,000 JPY.
    """
    validate_integer_money(goods_net_jpy, "goods_net_jpy", allow_zero=False)
    if goods_net_jpy >= 500_000:
        return 0
    return 2_000


def calculate_tax(goods_net_jpy: int) -> int:
    """Calculates synthetic 10% tax using floor integer arithmetic."""
    validate_integer_money(goods_net_jpy, "goods_net_jpy", allow_zero=False)
    return (goods_net_jpy * 10) // 100


def calculate_due_date(received_date: str) -> str:
    """Calculates invoice due date as received_date + 30 calendar days."""
    dt = date.fromisoformat(received_date)
    due = dt + timedelta(days=30)
    return due.isoformat()
