"""Smoke tests for the /health endpoint."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod


@pytest.fixture
async def healthy_client():
    """DB + Redis 모두 정상인 클라이언트."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    async def _fake_db():
        yield mock_session

    original_redis = main_mod._redis_client
    main_mod._redis_client = mock_redis

    with patch("src.main.get_db_session", _fake_db):
        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app),
            base_url="http://test",
        ) as ac:
            yield ac

    main_mod._redis_client = original_redis


@pytest.fixture
async def client():
    """DB + Redis 모두 장애인 클라이언트."""
    original_redis = main_mod._redis_client
    main_mod._redis_client = None

    with patch("src.main.get_db_session", side_effect=RuntimeError("no db")):
        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app),
            base_url="http://test",
        ) as ac:
            yield ac

    main_mod._redis_client = original_redis


@pytest.fixture
async def db_only_client():
    """DB만 정상, Redis 장애인 클라이언트."""
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()

    async def _fake_db():
        yield mock_session

    original_redis = main_mod._redis_client
    main_mod._redis_client = None

    with patch("src.main.get_db_session", _fake_db):
        async with AsyncClient(
            transport=ASGITransport(app=main_mod.app),
            base_url="http://test",
        ) as ac:
            yield ac

    main_mod._redis_client = original_redis


async def test_health_ok(healthy_client: AsyncClient):
    """DB + Redis 모두 connected → 200 ok."""
    resp = await healthy_client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"
    assert data["redis"] == "connected"
    assert data["environment"] == "development"
    assert "timestamp" in data


async def test_health_degraded_all_down(client: AsyncClient):
    """DB + Redis 모두 disconnected → 503 degraded."""
    resp = await client.get("/health")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "disconnected"
    assert data["redis"] == "disconnected"


async def test_health_degraded_partial(db_only_client: AsyncClient):
    """DB connected + Redis disconnected → 503 degraded."""
    resp = await db_only_client.get("/health")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "connected"
    assert data["redis"] == "disconnected"


async def test_health_response_fields(healthy_client: AsyncClient):
    """응답 JSON 필드 완전성 검증 — 정확히 5개 필드 + ISO timestamp."""
    resp = await healthy_client.get("/health")
    data = resp.json()

    expected_fields = {"status", "environment", "database", "redis", "timestamp"}
    assert set(data.keys()) == expected_fields

    # timestamp가 ISO 형식 문자열인지 검증
    datetime.fromisoformat(data["timestamp"])
