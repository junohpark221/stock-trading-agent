"""FastAPI application entry point.

Provides lifespan management (DB + Redis), structlog logging,
and the ``/health`` endpoint for infrastructure monitoring.

Usage::

    uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from src.config import get_settings
from src.core.models import HealthStatus
from src.data.cache import close_cache, init_cache
from src.db.session import close_db, get_db_session, init_db
from src.notification.telegram import TelegramBot

_redis_client: Redis | None = None
_telegram_bot: TelegramBot | None = None
_scheduler_engine: object | None = None  # SchedulerEngine (lazy import)
_scheduler_broker: object | None = None  # BrokerInterface (lazy import)
logger = structlog.get_logger(__name__)


def setup_logging(*, is_dev: bool) -> None:
    """structlog 프로세서 체인 설정.

    - dev: 컬러 터미널 출력 (ConsoleRenderer)
    - prod: JSON 포맷 (CloudWatch 연동)
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if is_dev:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_redis() -> Redis:
    """현재 Redis 클라이언트 반환. 초기화 전이면 RuntimeError."""
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized. App lifespan not started.")
    return _redis_client


def get_telegram_bot() -> TelegramBot:
    """현재 TelegramBot 싱글톤 반환. 초기화 전이면 RuntimeError."""
    if _telegram_bot is None:
        raise RuntimeError("Telegram bot not initialized. App lifespan not started.")
    return _telegram_bot


def get_scheduler():
    """현재 SchedulerEngine 싱글톤 반환. 미초기화 시 RuntimeError."""
    if _scheduler_engine is None:
        raise RuntimeError("Scheduler not initialized. App lifespan not started.")
    return _scheduler_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan: startup/shutdown 리소스 관리."""
    global _redis_client, _telegram_bot, _scheduler_engine, _scheduler_broker

    settings = get_settings()
    is_dev = settings.ENV == "development"

    # ── Startup ──────────────────────────────────────────────────────
    setup_logging(is_dev=is_dev)
    log = structlog.get_logger()

    await init_db(settings)
    log.info("database_initialized")

    _redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    log.info("redis_initialized")

    init_cache(_redis_client)
    log.info("cache_initialized")

    # Telegram 봇 싱글톤 — polling 시작하여 콜백 수신 가능
    _telegram_bot = TelegramBot(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
    )
    await _telegram_bot.start()
    log.info("telegram_bot_initialized")

    # Scheduler — TelegramBot 초기화 후 조립 (job들이 telegram_bot 사용)
    if settings.SCHEDULER_ENABLED:
        from src.data.cache import get_cache
        from src.db.session import get_session_factory
        from src.scheduler.factory import SchedulerFactory

        _scheduler_engine, _scheduler_broker = await SchedulerFactory.create_scheduler(
            settings=settings,
            session_factory=get_session_factory(),
            cache=get_cache(),
            telegram_bot=_telegram_bot,
        )
        await _scheduler_engine.start()
        log.info(
            "scheduler_initialized",
            jobs=len(_scheduler_engine.get_status()["jobs"]),
        )

    log.info("app_started", env=settings.ENV)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    if _scheduler_engine is not None:
        await _scheduler_engine.stop()
        _scheduler_engine = None
        log.info("scheduler_stopped")

    if _scheduler_broker is not None:
        await _scheduler_broker.disconnect()
        _scheduler_broker = None
        log.info("scheduler_broker_disconnected")

    if _telegram_bot is not None:
        await _telegram_bot.stop()
        _telegram_bot = None
        log.info("telegram_bot_stopped")

    close_cache()

    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
        log.info("redis_closed")

    await close_db()
    log.info("database_closed")
    log.info("app_stopped")


def create_app() -> FastAPI:
    """FastAPI 앱 팩토리. production에서는 docs 비활성화."""
    settings = get_settings()
    is_prod = settings.ENV == "production"

    return FastAPI(
        title="Stock Trading Agent",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )


app = create_app()

# ── Router registration ──────────────────────────────────────────────────
from src.api.routes.data import router as data_router  # noqa: E402
from src.api.routes.analysis import router as analysis_router  # noqa: E402
from src.api.routes.pipeline import router as pipeline_router  # noqa: E402
from src.api.routes.decisions import router as decisions_router  # noqa: E402
from src.api.admin.llm_config import router as admin_llm_router  # noqa: E402
from src.api.routes.strategy import router as strategy_router  # noqa: E402
from src.api.routes.portfolio import router as portfolio_router  # noqa: E402
from src.api.routes.orders import router as orders_router  # noqa: E402
from src.api.routes.control import router as control_router  # noqa: E402
from src.api.routes.trades import router as trades_router  # noqa: E402

app.include_router(data_router)
app.include_router(analysis_router)
app.include_router(pipeline_router)
app.include_router(decisions_router)
app.include_router(admin_llm_router)
app.include_router(strategy_router)
app.include_router(portfolio_router)
app.include_router(orders_router)
app.include_router(control_router)
app.include_router(trades_router)


@app.get("/health", response_model=HealthStatus)
async def health_check() -> JSONResponse:
    """DB/Redis 연결 확인 → HealthStatus 반환.

    둘 다 connected → 200 "ok", 하나라도 실패 → 503 "degraded".
    """
    settings = get_settings()

    # ── DB 확인 ──────────────────────────────────────────────────────
    db_status = "disconnected"
    try:
        async for session in get_db_session():
            await session.execute(text("SELECT 1"))
            db_status = "connected"
            break
    except Exception:
        logger.warning("health_check_db_failed", exc_info=True)
        db_status = "disconnected"

    # ── Redis 확인 ───────────────────────────────────────────────────
    redis_status = "disconnected"
    try:
        await get_redis().ping()
        redis_status = "connected"
    except Exception:
        logger.warning("health_check_redis_failed", exc_info=True)
        redis_status = "disconnected"

    # ── 응답 구성 ────────────────────────────────────────────────────
    is_healthy = db_status == "connected" and redis_status == "connected"

    body = HealthStatus(
        status="ok" if is_healthy else "degraded",
        environment=settings.ENV,
        database=db_status,
        redis=redis_status,
        timestamp=datetime.now(UTC),
    )

    return JSONResponse(
        content=body.model_dump(mode="json"),
        status_code=200 if is_healthy else 503,
    )
