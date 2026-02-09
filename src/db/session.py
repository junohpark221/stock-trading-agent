"""Async database engine and session management.

Provides ``init_db`` / ``close_db`` for FastAPI lifespan and
``get_db_session`` as a FastAPI dependency.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import Settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(settings: Settings) -> None:
    """엔진 + 세션 팩토리 초기화. FastAPI lifespan startup에서 호출."""
    global _engine, _session_factory

    is_dev = settings.ENV == "development"

    _engine = create_async_engine(
        settings.DATABASE_URL,
        echo=is_dev,
        pool_size=5 if is_dev else 10,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
    )


async def close_db() -> None:
    """엔진 dispose. FastAPI lifespan shutdown에서 호출."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_engine() -> AsyncEngine:
    """현재 엔진 반환. Alembic 등 외부 사용."""
    if _engine is None:
        raise RuntimeError("Database engine not initialized. Call init_db() first.")
    return _engine


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends용 세션 제공자. 요청마다 세션 생성/종료."""
    if _session_factory is None:
        raise RuntimeError(
            "Session factory not initialized. Call init_db() first."
        )
    async with _session_factory() as session:
        yield session
