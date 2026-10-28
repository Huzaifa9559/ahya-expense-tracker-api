from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.money import PositiveMoney


class ExpenseCreate(BaseModel):
    category_id: UUID
    amount: PositiveMoney
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    description: Annotated[str | None, Field(default=None, max_length=500)] = None
    spent_on: date

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("description")
    @classmethod
    def empty_description_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class ExpenseResponse(BaseModel):
    id: str
    category_id: str
    amount: PositiveMoney
    currency: str
    description: str | None
    spent_on: date
    receipt_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ExpenseListResponse(BaseModel):
    items: list[ExpenseResponse]
    next_cursor: str | None
