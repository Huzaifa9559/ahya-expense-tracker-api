from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, PlainSerializer


def parse_decimal(value: Any) -> Decimal:
    """Accept JSON number or string; always via str() so float binary noise is avoided."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("invalid decimal")
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid decimal") from exc
    raise ValueError("invalid decimal")


def money_str(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


# Request amounts: > 0, at most 2 decimal places. Responses serialize as strings.
PositiveMoney = Annotated[
    Decimal,
    BeforeValidator(parse_decimal),
    Field(gt=0, max_digits=14, decimal_places=2),
    PlainSerializer(money_str, return_type=str, when_used="json"),
]

# Optional budgets: >= 0, 2 decimal places.
NonNegativeMoney = Annotated[
    Decimal,
    BeforeValidator(parse_decimal),
    Field(ge=0, max_digits=14, decimal_places=2),
    PlainSerializer(money_str, return_type=str, when_used="json"),
]
