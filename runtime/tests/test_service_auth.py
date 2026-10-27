# -*- coding: utf-8 -*-
"""Tests for whole-Runtime service authentication."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from workmate_runtime.service_auth import RuntimeServiceAuthMiddleware


def _app(token: str = "service-secret") -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/agent/")
    async def protected():
        return {"agents": []}

    app.add_middleware(RuntimeServiceAuthMiddleware, token=token)
    return app


async def test_health_is_public_but_stock_routes_require_service_token():
    async with AsyncClient(
        transport=ASGITransport(app=_app()),
        base_url="http://test",
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/agent/")).status_code == 403
        assert (
            await client.get(
                "/agent/",
                headers={"X-Workmate-Internal-Token": "wrong"},
            )
        ).status_code == 403
        assert (
            await client.get(
                "/agent/",
                headers={"X-Workmate-Internal-Token": "service-secret"},
            )
        ).status_code == 200


async def test_empty_server_token_fails_closed():
    async with AsyncClient(
        transport=ASGITransport(app=_app(token="")),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/agent/",
            headers={"X-Workmate-Internal-Token": ""},
        )

    assert response.status_code == 403


async def test_diagnostic_health_routes_require_service_token():
    app = _app()

    @app.get("/health/reme")
    async def reme_health():
        return {"last_error": "sensitive internal detail"}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/health/reme")).status_code == 403
        authorized = await client.get(
            "/health/reme",
            headers={"X-Workmate-Internal-Token": "service-secret"},
        )
        assert authorized.status_code == 200
