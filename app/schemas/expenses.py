from datetime import date, datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

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


class ExpenseUpdate(BaseModel):
    category_id: UUID | None = None
    amount: PositiveMoney | None = None
    currency: Annotated[str | None, Field(default=None, min_length=3, max_length=3)] = None
    description: Annotated[str | None, Field(default=None, max_length=500)] = None
    spent_on: date | None = None

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().upper()

    @field_validator("description")
    @classmethod
    def empty_description_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> Self:
        # description=None is indistinguishable from "omit" for PATCH; treat only
        # explicitly useful fields — empty {} must 422.
        if (
            self.category_id is None
            and self.amount is None
            and self.currency is None
            and self.spent_on is None
            and "description" not in self.model_fields_set
        ):
            raise ValueError("At least one field must be provided")
        return self


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
