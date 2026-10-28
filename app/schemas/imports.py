import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ImportJobAccepted(BaseModel):
    job_id: uuid.UUID
    status: str


class ImportErrorItem(BaseModel):
    row_number: int
    reason: str


class ImportJobStatus(BaseModel):
    job_id: uuid.UUID
    status: str
    total_rows: int
    processed_rows: int
    failed_rows: int
    succeeded_rows: int
    started_at: datetime | None
    finished_at: datetime | None
    errors: list[ImportErrorItem] = Field(default_factory=list)
    errors_truncated: bool = False
