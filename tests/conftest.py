"""Shared test fixtures, helpers, and environment setup."""

import os

# 모듈 레벨 환경변수 설정 — conftest.py는 테스트 파일보다 먼저 로드되므로,
# src/main.py의 모듈 레벨 create_app() → get_settings() 호출 전에 실행된다.
# setdefault: .env 파일이 있으면 그 값 우선, 없으면 (CI 등) 기본값 사용.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/trading_agent"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENV", "development")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from src.config import Settings, get_settings  # noqa: E402
from src.data.cache import RedisCache  # noqa: E402


# ── Shared Helpers ────────────────────────────────────────────────────


class AsyncContextManagerMock:
    """Mock for ``async with session.get/post(...)`` pattern."""

    def __init__(self, resp: MagicMock) -> None:
        self._resp = resp

    async def __aenter__(self) -> MagicMock:
        return self._resp

    async def __aexit__(self, *args: object) -> None:
        pass


def make_settings(**overrides: object) -> Settings:
    """Create a Settings with sensible defaults + overrides."""
    defaults: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://localhost/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "KIS_APP_KEY": "test_app_key",
        "KIS_APP_SECRET": "test_app_secret",
        "KIS_IS_PAPER": True,
        "KIS_BASE_URL": "",
        "KIS_TOKEN_REDIS_TTL": 82800,
        "KIS_ACCOUNT_NO": "1234567801",
        "KIS_RATE_LIMIT_INTERVAL": 0.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def mock_aiohttp_response(
    *, status: int = 200, json_data: dict | None = None, text: str = "", headers: dict | None = None
) -> MagicMock:
    """Create a mock aiohttp response with headers support."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    resp.headers = headers or {}
    return resp


# ── Shared Fixtures ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """매 테스트마다 get_settings() @lru_cache 클리어 → 테스트 간 격리."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_redis():
    """AsyncMock Redis client (SCAN 기본: 빈 결과)."""
    r = AsyncMock()
    r.scan = AsyncMock(return_value=(0, []))
    return r


@pytest.fixture
def cache(mock_redis):
    """RedisCache backed by mock_redis."""
    return RedisCache(mock_redis)
