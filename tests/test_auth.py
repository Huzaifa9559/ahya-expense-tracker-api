import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.security import tokens


async def _register(
    client: AsyncClient,
    *,
    email: str = "user@example.com",
    password: str = "password123",
    base_currency: str = "USD",
):
    return await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "base_currency": base_currency,
        },
    )


async def _login(
    client: AsyncClient,
    *,
    email: str = "user@example.com",
    password: str = "password123",
):
    return await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )


@pytest.mark.asyncio
async def test_register_201(client: AsyncClient, rates):
    resp = await _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "user@example.com"
    assert body["base_currency"] == "USD"
    assert "password_hash" not in body
    assert "id" in body


@pytest.mark.asyncio
async def test_register_duplicate_email_409(client: AsyncClient, rates):
    assert (await _register(client)).status_code == 201
    resp = await _register(client, email="USER@example.com")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


@pytest.mark.asyncio
async def test_register_invalid_base_currency_422(client: AsyncClient, rates):
    resp = await _register(client, base_currency="ZZZ")
    assert resp.status_code == 422
    assert "error" in resp.json()


@pytest.mark.asyncio
async def test_register_short_password_422(client: AsyncClient, rates):
    resp = await _register(client, password="short")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_login_200(client: AsyncClient, rates):
    await _register(client)
    resp = await _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


@pytest.mark.asyncio
async def test_login_wrong_password_401(client: AsyncClient, rates):
    await _register(client)
    resp = await _login(client, password="wrongpassword")
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Invalid email or password"


@pytest.mark.asyncio
async def test_login_unknown_email_401_same_message(client: AsyncClient, rates):
    resp = await _login(client, email="nobody@example.com")
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "Invalid email or password"


@pytest.mark.asyncio
async def test_access_token_protects_me(client: AsyncClient, rates):
    await _register(client)
    tokens_body = (await _login(client)).json()
    ok = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens_body['access_token']}"},
    )
    assert ok.status_code == 200
    assert ok.json()["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_missing_token_401(client: AsyncClient, rates):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_garbage_token_401(client: AsyncClient, rates):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")


@pytest.mark.asyncio
async def test_expired_access_token_401(client: AsyncClient, rates, settings: Settings):
    await _register(client)
    login = (await _login(client)).json()
    # Build an expired access JWT with the same secret.
    payload = jwt.decode(
        login["access_token"],
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"verify_exp": False},
    )
    payload["exp"] = datetime.now(UTC) - timedelta(minutes=1)
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rotates_old_token_fails(client: AsyncClient, rates):
    await _register(client)
    login = (await _login(client)).json()
    old = login["refresh_token"]
    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    assert rotated.status_code == 200
    new = rotated.json()["refresh_token"]
    assert new != old

    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": old})
    assert reuse.status_code == 401


@pytest.mark.asyncio
async def test_expired_refresh_401_without_revoke_all(
    client: AsyncClient,
    rates,
    engine: AsyncEngine,
):
    await _register(client)
    first = (await _login(client)).json()
    second = (await _login(client)).json()

    # Expire only the first refresh token row.
    token_hash = tokens.hash_refresh_token(first["refresh_token"])
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                UPDATE refresh_tokens
                SET expires_at = now() - interval '1 hour'
                WHERE token_hash = :h
                """
            ),
            {"h": token_hash},
        )

    expired = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": first["refresh_token"]},
    )
    assert expired.status_code == 401

    # Sibling session must still refresh — proves no revoke-all on expiry.
    still_ok = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": second["refresh_token"]},
    )
    assert still_ok.status_code == 200


@pytest.mark.asyncio
async def test_refresh_token_reuse_revokes_all_tokens(client: AsyncClient, rates):
    await _register(client)
    a = (await _login(client)).json()
    b = (await _login(client)).json()

    rotated = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": a["refresh_token"]},
    )
    assert rotated.status_code == 200
    a2 = rotated.json()["refresh_token"]

    reuse = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": a["refresh_token"]},
    )
    assert reuse.status_code == 401

    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": a2})
    ).status_code == 401
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": b["refresh_token"]})
    ).status_code == 401


@pytest.mark.asyncio
async def test_concurrent_refresh_same_token_exactly_one_success(client: AsyncClient, rates):
    await _register(client)
    login = (await _login(client)).json()
    raw = login["refresh_token"]

    r1, r2 = await asyncio.gather(
        client.post("/api/v1/auth/refresh", json={"refresh_token": raw}),
        client.post("/api/v1/auth/refresh", json={"refresh_token": raw}),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 401]

    winner = r1 if r1.status_code == 200 else r2
    winner_refresh = winner.json()["refresh_token"]
    follow = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": winner_refresh},
    )
    assert follow.status_code == 401


@pytest.mark.asyncio
async def test_logout_204_and_token_no_longer_refreshes(client: AsyncClient, rates):
    await _register(client)
    login = (await _login(client)).json()
    raw = login["refresh_token"]

    out = await client.post("/api/v1/auth/logout", json={"refresh_token": raw})
    assert out.status_code == 204

    again = await client.post("/api/v1/auth/refresh", json={"refresh_token": raw})
    assert again.status_code == 401


@pytest.mark.asyncio
async def test_logout_twice_still_204(client: AsyncClient, rates):
    await _register(client)
    login = (await _login(client)).json()
    raw = login["refresh_token"]

    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": raw})).status_code == 204
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": raw})).status_code == 204


@pytest.mark.asyncio
async def test_sixth_failed_login_returns_429(client: AsyncClient, rates):
    await _register(client)
    for _ in range(5):
        resp = await _login(client, password="wrongpassword")
        assert resp.status_code == 401

    sixth = await _login(client, password="wrongpassword")
    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "rate_limited"
    retry_after = sixth.headers.get("Retry-After")
    assert retry_after is not None
    assert int(retry_after) > 0


@pytest.mark.asyncio
async def test_rate_limited_correct_password_still_returns_429(client: AsyncClient, rates):
    await _register(client)
    for _ in range(5):
        assert (await _login(client, password="wrongpassword")).status_code == 401

    blocked = await _login(client, password="password123")
    assert blocked.status_code == 429
    assert int(blocked.headers.get("Retry-After", "0")) > 0


@pytest.mark.asyncio
async def test_successful_login_resets_rate_limit_counter(client: AsyncClient, rates):
    await _register(client)
    for _ in range(4):
        assert (await _login(client, password="wrongpassword")).status_code == 401

    ok = await _login(client, password="password123")
    assert ok.status_code == 200

    # Counter cleared: four more failures should still be 401, not 429.
    for _ in range(4):
        assert (await _login(client, password="wrongpassword")).status_code == 401
    assert (await _login(client, password="wrongpassword")).status_code == 401
