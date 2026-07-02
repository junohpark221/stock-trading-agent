"""FastAPI application entry point.

Provides lifespan management (DB + Redis), structlog logging,
and the ``/health`` endpoint for infrastructure monitoring.

Usage::

    uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import text

from src.config import get_settings
from src.core.models import HealthStatus
from src.data.cache import close_cache, init_cache
from src.db.session import close_db, get_db_session, init_db
from src.execution.approval import ApprovalManager
from src.notification.telegram import TelegramBot

_redis_client: Redis | None = None
_telegram_bot: TelegramBot | None = None
_approval_manager: ApprovalManager | None = None
_scheduler_engine: object | None = None  # SchedulerEngine (lazy import)
_broker_registry: object | None = None  # BrokerRegistry (lazy import)
_execution_stream: object | None = None  # ExecutionStreamManager (lazy import)
_stoploss_stream: object | None = None  # StopLossStreamService (lazy import)
_scheduler_runtime: object | None = None  # SchedulerRuntime (lazy import)
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


def get_approval_manager() -> ApprovalManager:
    """현재 ApprovalManager 싱글톤 반환. 초기화 전이면 RuntimeError.

    여러 인스턴스가 생기면 ``TelegramBot._callback_handler``가 서로를 덮어써
    버튼 클릭이 이전 인스턴스의 pending 상태에 도달하지 못한다. 따라서
    전역 싱글톤으로 유지하고 모든 호출부(스케줄러/API/커맨드/백오피스)는
    같은 인스턴스를 공유해야 한다.
    """
    if _approval_manager is None:
        raise RuntimeError(
            "ApprovalManager not initialized. App lifespan not started."
        )
    return _approval_manager


def get_scheduler():
    """현재 SchedulerEngine 싱글톤 반환. 미초기화 시 RuntimeError."""
    if _scheduler_engine is None:
        raise RuntimeError("Scheduler not initialized. App lifespan not started.")
    return _scheduler_engine


def get_broker_registry():
    """현재 BrokerRegistry 싱글톤 반환. 미초기화 시 RuntimeError."""
    if _broker_registry is None:
        raise RuntimeError("BrokerRegistry not initialized. App lifespan not started.")
    return _broker_registry


def get_scheduler_runtime():
    """현재 SchedulerRuntime 반환. 미초기화 시 RuntimeError.

    백오피스 계좌 편집 핸들러가 ``reload_account``를 호출할 때 사용(B-10).
    ``SCHEDULER_ENABLED=False``이거나 lifespan 미시작이면 미초기화 상태다.
    """
    if _scheduler_runtime is None:
        raise RuntimeError("SchedulerRuntime not initialized. App lifespan not started.")
    return _scheduler_runtime


def get_stoploss_stream():
    """현재 StopLossStreamService 반환(없으면 None). 어드민 exec-monitor 관측용.

    스케줄러/WS 미가동(테스트·로컬) 환경에서는 None일 수 있으므로 호출측이
    None을 graceful 처리한다(다른 getter와 달리 RuntimeError를 던지지 않는다).
    """
    return _stoploss_stream


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan: startup/shutdown 리소스 관리."""
    global _redis_client, _telegram_bot, _approval_manager
    global _scheduler_engine, _broker_registry
    global _execution_stream, _stoploss_stream, _scheduler_runtime

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

    # 커맨드 핸들러 DI를 위해 session_factory/cache 준비
    from src.data.cache import get_cache
    from src.db.session import get_session_factory

    session_factory = get_session_factory()
    cache = get_cache()

    # Telegram 봇 싱글톤 — polling 시작하여 콜백 수신 가능
    _telegram_bot = TelegramBot(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
        session_factory=session_factory,
        cache=cache,
        settings=settings,
    )
    await _telegram_bot.start()
    log.info("telegram_bot_initialized")

    # ApprovalManager 싱글톤 — 모든 호출부가 같은 pending 상태를 공유해야
    # 텔레그램 콜백이 올바른 인스턴스로 전달된다. Telegram 봇 기동 직후,
    # 스케줄러 조립 이전에 초기화하여 콜백 핸들러 등록을 1회만 수행.
    from src.agent.decision_recorder import DecisionRecorder

    _approval_manager = ApprovalManager(
        telegram_bot=_telegram_bot,
        recorder=DecisionRecorder(session_factory),
        session_factory=session_factory,
        cache=cache,
        settings=settings,
    )
    await _approval_manager.initialize()
    log.info("approval_manager_initialized")

    # Scheduler — TelegramBot 초기화 후 조립 (job들이 telegram_bot 사용)
    if settings.SCHEDULER_ENABLED:
        from src.scheduler.factory import SchedulerFactory

        (
            _scheduler_engine,
            _broker_registry,
            _execution_stream,
            _stoploss_stream,
            _scheduler_runtime,
        ) = await SchedulerFactory.create_scheduler(
            settings=settings,
            session_factory=session_factory,
            cache=cache,
            telegram_bot=_telegram_bot,
            approval_manager=_approval_manager,
        )
        await _scheduler_engine.start()
        log.info(
            "scheduler_initialized",
            jobs=len(_scheduler_engine.get_status()["jobs"]),
        )

        # KIS 체결통보 WS 시작 — Factory가 등록한 credentials 소모.
        pending_creds = getattr(_execution_stream, "_pending_credentials", [])
        if pending_creds:
            await _execution_stream.start(pending_creds)
            log.info("execution_stream_started", accounts=len(pending_creds))

        # F-05: 실시간 손절 체결가 WS 시작 (STOP_LOSS_WS_ENABLED일 때만 동작).
        sl_creds = getattr(_stoploss_stream, "_pending_credentials", [])
        if _stoploss_stream is not None:
            await _stoploss_stream.start(sl_creds)

    log.info("app_started", env=settings.ENV)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    if _scheduler_runtime is not None and _scheduler_runtime.naver_provider is not None:
        try:
            await _scheduler_runtime.naver_provider.shutdown()
            log.info("naver_provider_stopped")
        except Exception:
            log.exception("naver_provider_stop_failed")
    _scheduler_runtime = None
    if _scheduler_engine is not None:
        await _scheduler_engine.stop()
        _scheduler_engine = None
        log.info("scheduler_stopped")

    if _stoploss_stream is not None:
        try:
            await _stoploss_stream.stop()
            log.info("stoploss_stream_stopped")
        except Exception:
            log.exception("stoploss_stream_stop_failed")
        _stoploss_stream = None

    if _execution_stream is not None:
        try:
            await _execution_stream.stop()
            log.info("execution_stream_stopped")
        except Exception:
            log.exception("execution_stream_stop_failed")
        _execution_stream = None

    if _broker_registry is not None:
        await _broker_registry.disconnect_all()
        _broker_registry = None
        log.info("broker_registry_disconnected")

    if _telegram_bot is not None:
        await _telegram_bot.stop()
        _telegram_bot = None
        log.info("telegram_bot_stopped")

    _approval_manager = None

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
        version="0.9.0",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )


app = create_app()

# ── Static files (admin web UI) ──────────────────────────────────────────
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# ── Router registration ──────────────────────────────────────────────────
from src.api.admin.llm_config import router as admin_llm_router  # noqa: E402
from src.api.routes.accounts import router as accounts_router  # noqa: E402
from src.api.routes.analysis import router as analysis_router  # noqa: E402
from src.api.routes.backtest import router as backtest_router  # noqa: E402
from src.api.routes.control import router as control_router  # noqa: E402
from src.api.routes.data import router as data_router  # noqa: E402
from src.api.routes.decisions import router as decisions_router  # noqa: E402
from src.api.routes.orders import router as orders_router  # noqa: E402
from src.api.routes.pipeline import router as pipeline_router  # noqa: E402
from src.api.routes.portfolio import router as portfolio_router  # noqa: E402
from src.api.routes.strategy import router as strategy_router  # noqa: E402
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
app.include_router(backtest_router)
app.include_router(accounts_router)

# ── Admin web UI (conditional on ADMIN_PASSWORD) ─────────────────────────
if get_settings().ADMIN_PASSWORD:
    from src.api.routes.admin_web import router as admin_web_router  # noqa: E402

    app.include_router(admin_web_router)


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
