from app.errors import AppError


def format_etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(header: str | None) -> int:
    """
    Require If-Match. Accept `"3"` and `W/"3"`; anything else -> 400 INVALID_IF_MATCH.
    Missing -> 428 PRECONDITION_REQUIRED.
    """
    if header is None or header.strip() == "":
        raise AppError(
            code="PRECONDITION_REQUIRED",
            message="If-Match header is required",
            status_code=428,
        )
    raw = header.strip()
    if raw.upper().startswith("W/"):
        raw = raw[2:].strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    try:
        version = int(raw)
    except ValueError as exc:
        raise AppError(
            code="INVALID_IF_MATCH",
            message="If-Match must be a quoted integer version",
            status_code=400,
        ) from exc
    if version < 0:
        raise AppError(
            code="INVALID_IF_MATCH",
            message="If-Match must be a quoted integer version",
            status_code=400,
        )
    return version
