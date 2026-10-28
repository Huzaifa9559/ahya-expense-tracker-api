from fastapi import APIRouter, Response, status

from app.deps import AppSettings, CurrentUser, DbSession, RedisClient
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(body: RegisterRequest, session: DbSession) -> UserResponse:
    return await auth_service.register(
        session,
        email=body.email,
        password=body.password,
        base_currency=body.base_currency,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: DbSession,
    redis: RedisClient,
    settings: AppSettings,
) -> TokenResponse:
    return await auth_service.login(
        session,
        redis,
        settings,
        email=body.email,
        password=body.password,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    session: DbSession,
    settings: AppSettings,
) -> TokenResponse:
    return await auth_service.refresh(
        session,
        settings,
        raw_refresh=body.refresh_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, session: DbSession) -> Response:
    await auth_service.logout(session, raw_refresh=body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        email=user.email,
        base_currency=user.base_currency,
        created_at=user.created_at.isoformat(),
    )
