import uuid

from fastapi import APIRouter

from app.deps import CurrentUser, DbSession
from app.schemas.imports import ImportJobStatus
from app.services import imports as imports_service

router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("/{job_id}", response_model=ImportJobStatus)
async def get_import_job(
    job_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ImportJobStatus:
    return await imports_service.get_import_job(session, user, job_id=job_id)
