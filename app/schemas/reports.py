from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, PlainSerializer

from app.schemas.money import NonNegativeMoney, money_str, parse_decimal

# Change can be negative when only the previous month had spend.
SignedMoney = Annotated[
    Decimal,
    BeforeValidator(parse_decimal),
    Field(max_digits=16, decimal_places=2),
    PlainSerializer(money_str, return_type=str, when_used="json"),
]


class MonthlyReportItem(BaseModel):
    category_id: str
    name: str
    budget: NonNegativeMoney | None
    total: NonNegativeMoney
    previous_total: NonNegativeMoney
    change: SignedMoney
    percent_used: float | None


class MonthlyReportResponse(BaseModel):
    year: int
    month: int
    base_currency: str
    items: list[MonthlyReportItem]
