import uuid

from fastapi import APIRouter, status

from app.deps import CurrentUser, DbSession
from app.schemas.categories import CategoryCreate, CategoryListItem, CategoryResponse
from app.services import categories as categories_service

router = APIRouter(prefix="/categories", tags=["categories"])


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    body: CategoryCreate,
    session: DbSession,
    user: CurrentUser,
) -> CategoryResponse:
    return await categories_service.create_category(
        session,
        user,
        name=body.name,
        monthly_budget=body.monthly_budget,
    )


@router.get("", response_model=list[CategoryListItem])
async def list_categories(
    session: DbSession,
    user: CurrentUser,
) -> list[CategoryListItem]:
    return await categories_service.list_categories(session, user)


@router.get("/{category_id}", response_model=CategoryResponse)
async def get_category(
    category_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> CategoryResponse:
    return await categories_service.get_category(session, user, category_id)
