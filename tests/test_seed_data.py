from datetime import date, timedelta
from decimal import Decimal

from app.seed_data import (
    SEED_CURRENCIES,
    SEED_LOOKBACK_DAYS,
    count_seed_weekdays,
    generate_seed_rates,
)


def test_generate_seed_rates_four_currencies_no_weekends_usd_one() -> None:
    today = date(2026, 10, 28)
    rows = generate_seed_rates(today)

    currencies = {r["currency"] for r in rows}
    assert currencies == set(SEED_CURRENCIES)

    for row in rows:
        assert row["valid_on"].weekday() < 5
        assert row["rate_to_usd"] > 0
        if row["currency"] == "USD":
            assert row["rate_to_usd"] == Decimal("1")

    expected_weekdays = count_seed_weekdays(today)
    assert expected_weekdays == sum(
        1 for i in range(SEED_LOOKBACK_DAYS) if (today - timedelta(days=i)).weekday() < 5
    )
    assert len(rows) == expected_weekdays * len(SEED_CURRENCIES)


def test_generate_seed_rates_is_deterministic() -> None:
    today = date(2026, 6, 15)
    assert generate_seed_rates(today) == generate_seed_rates(today)
