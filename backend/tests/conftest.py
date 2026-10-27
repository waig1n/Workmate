# -*- coding: utf-8 -*-
"""Shared test fixtures.

Native HTTP integration uses an isolated PostgreSQL schema. Pure database
unit tests can use SQLite; catalogs and Skill bytes are in-memory dependencies.
"""

import asyncio
import os
import tempfile

# Must be set before any app import touches get_settings()/engine.
_TMP = tempfile.mkdtemp(prefix="workmate-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["ARTIFACT_STORAGE_DIR"] = f"{_TMP}/artifacts"
os.environ["JWT_SECRET"] = "test-secret"
# Model identity lives in workmate.yaml profiles; env carries the API key
# that profiles' api_key_env points at.
os.environ["CHAT_MODEL_API_KEY"] = "test-key"
# Many tests self-register a second user; the production default
# (bootstrap_only) would reject them. Policy-specific tests override the
# mode per-call (see test_registration_policy.py).
os.environ["REGISTRATION_MODE"] = "open"
# Existing integration fixtures create their own focused Agent/Skill graphs.
# Workspace-assistant behavior has dedicated tests that call its provisioner.
os.environ["WORKSPACE_ASSISTANT_ENABLED"] = "false"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from product_catalog_fakes import CatalogData, SkillCatalog, MCPCatalog
from app.db import get_engine, get_sessionmaker
from workmate_product.persistence.models import Base


@pytest_asyncio.fixture
async def db_session(request, monkeypatch):
    import uuid
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import engine as engine_module
    from product_catalog_fakes import MemoryBlobStore

    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if 'client_and_app' in request.fixturenames and not dsn:
        pytest.skip('native Product HTTP requires WORKMATE_TEST_POSTGRES_DSN')
    schema = 'product_http_' + uuid.uuid4().hex if dsn else None
    if dsn:
        engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
            connect_args={'server_settings': {'search_path': schema}})
    else:
        engine = create_async_engine(os.environ['DATABASE_URL'])
    sessions = async_sessionmaker(engine, expire_on_commit=False,
        info={'skill_blob_store': MemoryBlobStore()})
    monkeypatch.setattr(engine_module, '_engine', engine)
    monkeypatch.setattr(engine_module, '_sessionmaker', sessions)
    try:
        async with engine.begin() as conn:
            if schema:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            else:
                await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            yield session
    finally:
        if schema:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture
async def fake_client():
    return CatalogData()


@pytest_asyncio.fixture
async def client_and_app(db_session, fake_client, monkeypatch):
    """Native Product API without external startup services or fake App execution."""
    from app.main import build_app
    from app.services.bootstrap import ensure_bootstrap
    from app.config import get_settings
    from app.db import get_db

    monkeypatch.setenv("RUNTIME_INTERNAL_TOKEN", "test-internal-token")
    get_settings.cache_clear()
    await ensure_bootstrap(db_session)
    async def database():
        async with get_sessionmaker()() as session:
            yield session
    app = build_app()
    app.dependency_overrides[get_db] = database
    app.state.runtime_client = fake_client
    from app.services.durable_runtime import compose_product_services
    from redis.asyncio import Redis
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    from workmate_product.realtime.composition import create_realtime_log
    from uuid import uuid4
    # Lazy, zero-retry transport: ordinary API tests never open SSE or connect.
    redis_client = Redis(host='127.0.0.1', port=1, decode_responses=True,
                         retry=Retry(NoBackoff(), 0))
    settings = get_settings()
    log = create_realtime_log(redis_client, namespace='api-fixture-' + uuid4().hex,
                              policy=settings.realtime)
    runtime, projection, stream = compose_product_services(get_sessionmaker(), settings,
                                                           realtime_log=log)
    app.state.product_sessions = get_sessionmaker()
    app.state.product_runtime = runtime
    app.state.product_projection = projection
    app.state.product_stream = stream
    app.state.skill_catalogs = {hub: SkillCatalog(fake_client, hub)
                               for hub in ('clawhub', 'skillhub-cn')}
    app.state.mcp_catalogs = {'github': MCPCatalog(fake_client)}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http, app
    finally:
        try:
            await stream.close()
        finally:
            try:
                await stream.reader.close()
            finally:
                await redis_client.aclose()
                get_settings.cache_clear()


@pytest_asyncio.fixture
async def auth_headers(client_and_app):
    http, _ = client_and_app
    resp = await http.post("/api/auth/register", json={
        "email": "u@test.dev", "password": "password123",
        "display_name": "Tester", "organization_name": "Test Organization",
    })
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def seeded_ids(client_and_app, auth_headers):
    """One agent + conversation ready for task tests."""
    http, app = client_and_app
    resp = await http.post("/api/agents", json={
        "slug": "default", "display_name": "Default Agent",
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]

    resp = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "T1",
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    conv_id = resp.json()["id"]
    return agent_id, conv_id
