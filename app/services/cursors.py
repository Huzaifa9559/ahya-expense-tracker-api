import base64
import json
import uuid
from datetime import date

from app.errors import AppError


def encode_expense_cursor(*, spent_on: date, expense_id: uuid.UUID) -> str:
    payload = {"spent_on": spent_on.isoformat(), "id": str(expense_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_expense_cursor(cursor: str) -> tuple[date, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        spent_on = date.fromisoformat(data["spent_on"])
        expense_id = uuid.UUID(data["id"])
        return spent_on, expense_id
    except Exception as exc:
        raise AppError(
            code="INVALID_CURSOR",
            message="Malformed pagination cursor",
            status_code=400,
        ) from exc
