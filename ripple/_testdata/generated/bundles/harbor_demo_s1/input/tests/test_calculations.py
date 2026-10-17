"""Tests for core financial calculations, tax, freight, and due date logic."""

import pytest
from app.calculations import calculate_due_date, calculate_freight, calculate_tax, validate_integer_money


def test_freight_schedule_thresholds():
    # Exactly at 500,000 JPY threshold -> 0 JPY freight
    assert calculate_freight(500_000) == 0
    assert calculate_freight(1_000_000) == 0

    # Below 500,000 JPY -> 2,000 JPY freight
    assert calculate_freight(499_999) == 2_000
    assert calculate_freight(100_000) == 2_000
    assert calculate_freight(1) == 2_000


def test_tax_calculation_floor_integer():
    assert calculate_tax(100) == 10
    assert calculate_tax(109) == 10
    assert calculate_tax(400_000) == 40_000
    assert calculate_tax(1_000_000) == 100_000


def test_due_date_calculation():
    assert calculate_due_date("2026-09-01") == "2026-10-01"
    assert calculate_due_date("2026-01-15") == "2026-02-14"


def test_money_validation_rejects_invalid_types():
    # Booleans must not pass as integers
    with pytest.raises(TypeError):
        validate_integer_money(True, "amount")
    with pytest.raises(TypeError):
        validate_integer_money(False, "amount")

    # Floats must not pass
    with pytest.raises(TypeError):
        validate_integer_money(500.5, "amount")

    # Non-positive values
    with pytest.raises(ValueError):
        validate_integer_money(0, "amount", allow_zero=False)
    with pytest.raises(ValueError):
        validate_integer_money(-100, "amount", allow_zero=False)

    # Zero allowed when explicitly specified
    validate_integer_money(0, "freight", allow_zero=True)
