"""Shared test fixtures and environment setup."""

import os

# 모듈 레벨 환경변수 설정 — conftest.py는 테스트 파일보다 먼저 로드되므로,
# src/main.py의 모듈 레벨 create_app() → get_settings() 호출 전에 실행된다.
# setdefault: .env 파일이 있으면 그 값 우선, 없으면 (CI 등) 기본값 사용.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/trading_agent"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENV", "development")

import pytest  # noqa: E402

from src.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """매 테스트마다 get_settings() @lru_cache 클리어 → 테스트 간 격리."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
