"""Deterministic seed helpers. Pure functions — no DB I/O."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

# Realistic mid-market-ish USD cross rates (1 unit of currency = X USD).
_BASE_RATES: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08500000"),
    "AED": Decimal("0.27229400"),
    "PKR": Decimal("0.00359000"),
}

SEED_CURRENCIES = tuple(_BASE_RATES.keys())
SEED_LOOKBACK_DAYS = 90


def generate_seed_rates(today: date) -> list[dict[str, Any]]:
    """
    Build exchange-rate rows for the last SEED_LOOKBACK_DAYS calendar days.

    Skips Saturdays and Sundays. USD is always 1. Other currencies wobble
    deterministically around a fixed base (no randomness) so re-runs match.
    """
    rows: list[dict[str, Any]] = []
    for offset in range(SEED_LOOKBACK_DAYS):
        valid_on = today - timedelta(days=offset)
        if valid_on.weekday() >= 5:  # Saturday=5, Sunday=6
            continue
        for currency, base in _BASE_RATES.items():
            if currency == "USD":
                rate = Decimal("1")
            else:
                # Deterministic ± wobble from the calendar ordinal (basis points-ish).
                wobble = Decimal((valid_on.toordinal() % 11) - 5) * Decimal("0.00001000")
                rate = (base + wobble).quantize(Decimal("0.00000001"))
                if rate <= 0:
                    rate = base
            rows.append(
                {
                    "currency": currency,
                    "valid_on": valid_on,
                    "rate_to_usd": rate,
                }
            )
    return rows


def count_seed_weekdays(today: date) -> int:
    """Number of Mon–Fri days in the seed window (for tests)."""
    return sum(
        1
        for offset in range(SEED_LOOKBACK_DAYS)
        if (today - timedelta(days=offset)).weekday() < 5
    )
