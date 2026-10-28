import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from app.main import create_app


@pytest.mark.asyncio
async def test_unknown_route_returns_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message", "details"}
    assert body["error"]["code"] == "not_found"
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]
    assert body["error"]["details"] == []


@pytest.mark.asyncio
async def test_malformed_body_returns_error_envelope_with_details() -> None:
    """No validated POST exists yet; mount a tiny probe on a fresh app for the envelope."""

    class Probe(BaseModel):
        amount: float = Field(gt=0)

    app = create_app()

    @app.post("/__envelope_probe")
    async def _probe(body: Probe) -> dict:
        return {"amount": body.amount}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post("/__envelope_probe", json={"amount": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"]
    assert isinstance(body["error"]["details"], list)
    assert len(body["error"]["details"]) >= 1
