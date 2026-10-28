from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.schemas.money import NonNegativeMoney


class CategoryCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    monthly_budget: NonNegativeMoney | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name must not be blank")
        return trimmed


class CategoryResponse(BaseModel):
    id: str
    name: str
    monthly_budget: NonNegativeMoney | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CategoryListItem(BaseModel):
    id: str
    name: str
    monthly_budget: NonNegativeMoney | None
    created_at: datetime
    spend: NonNegativeMoney
    percent_used: float | None
