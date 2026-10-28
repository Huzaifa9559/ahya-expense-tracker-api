from typing import Any, Literal

from pydantic import BaseModel, Field


ALLOWED_CONTENT_TYPES = Literal["image/jpeg", "image/png", "application/pdf"]


class ReceiptUploadUrlRequest(BaseModel):
    content_type: ALLOWED_CONTENT_TYPES


class ReceiptUploadUrlResponse(BaseModel):
    url: str
    fields: dict[str, Any]
    key: str
    expires_in: int = 600
    max_bytes: int = 5_242_880


class ReceiptConfirmRequest(BaseModel):
    key: str = Field(min_length=1, max_length=512)


class ReceiptDownloadResponse(BaseModel):
    url: str
    expires_in: int = 900
