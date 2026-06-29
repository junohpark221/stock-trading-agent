"""Backoffice stock-master routes: stats page + async sync + status poll."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.templates import templates
from src.db.models.market_data import StockMaster
from src.db.session import get_db_session, get_session_factory

logger = structlog.get_logger(__name__)

router = APIRouter()


async def _get_stock_master_stats(session: AsyncSession) -> dict:
    """stock_master 테이블 통계 조회."""
    total = (await session.execute(
        select(func.count(StockMaster.symbol))
    )).scalar_one()
    kospi = (await session.execute(
        select(func.count(StockMaster.symbol)).where(
            StockMaster.market_type == "kospi", StockMaster.is_active.is_(True),
        )
    )).scalar_one()
    kosdaq = (await session.execute(
        select(func.count(StockMaster.symbol)).where(
            StockMaster.market_type == "kosdaq", StockMaster.is_active.is_(True),
        )
    )).scalar_one()
    inactive = (await session.execute(
        select(func.count(StockMaster.symbol)).where(StockMaster.is_active.is_(False))
    )).scalar_one()
    last_updated = (await session.execute(
        select(func.max(StockMaster.updated_at))
    )).scalar_one()

    return {
        "total": total,
        "kospi": kospi,
        "kosdaq": kosdaq,
        "inactive": inactive,
        "last_updated": last_updated.strftime("%Y-%m-%d %H:%M") if last_updated else None,
    }


_SYNC_NS = "stock_master_sync"
_SYNC_KEY = "status"
_SYNC_TTL = 300  # 5분


async def _set_sync_status(cache, data: dict) -> None:
    """Redis에 동기화 상태 저장 (실패 시 로그만)."""
    try:
        await cache.set_json(_SYNC_NS, _SYNC_KEY, data, ttl=_SYNC_TTL)
    except Exception:
        logger.warning("stock_master_sync_status_write_failed", data=data)


async def _send_sync_failure_telegram(error_msg: str) -> None:
    """동기화 실패 시 텔레그램 알림 발송 (실패 시 로그만)."""
    try:
        from src.main import get_telegram_bot

        bot = get_telegram_bot()
        await bot.send_message(
            f"<b>종목 마스터 동기화 실패</b>\n에러: {error_msg}"
        )
    except Exception:
        logger.warning("stock_master_sync_telegram_failed", error=error_msg)


async def _sync_stock_master_background(session_factory) -> None:
    """BackgroundTasks에서 실행되는 stock_master 동기화."""
    from src.config import get_settings
    from src.data.cache import get_cache

    cache = get_cache()

    await _set_sync_status(cache, {
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
    })

    try:
        from src.broker.kis.client import KISClient
        from src.data.providers.kis_provider import KISDataProvider

        settings = get_settings()

        # stock_master는 공개 .mst.zip 다운로드라 인증 불필요 — 임시 KISClient 사용
        client = KISClient(settings=settings, cache=cache)
        await client.connect()
        try:
            provider = KISDataProvider(
                client=client,
                cache=cache,
                session_factory=session_factory,
                settings=settings,
            )
            count = await provider.sync_stock_master()
            logger.info("stock_master_sync_manual_done", upserted=count)
            await _set_sync_status(cache, {"status": "completed", "count": count})
            # 텔레그램 성공 알림
            try:
                from src.main import get_telegram_bot
                bot = get_telegram_bot()
                await bot.send_message(
                    f"<b>종목 마스터 동기화 완료</b>\n갱신: {count}건"
                )
            except Exception:
                logger.warning("stock_master_sync_success_telegram_failed", count=count)
        finally:
            await client.disconnect()

    except Exception as exc:
        error = str(exc)
        logger.exception("stock_master_sync_failed", error=error)
        await _set_sync_status(cache, {"status": "failed", "error": error})
        await _send_sync_failure_telegram(error)


@router.get("/stock-master", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def stock_master_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/stock-master — 종목 마스터 관리: 통계 + 최근 업데이트 10건."""
    stats = await _get_stock_master_stats(session)

    result = await session.execute(
        select(StockMaster).order_by(StockMaster.updated_at.desc()).limit(10)
    )
    stocks = list(result.scalars().all())

    return templates.TemplateResponse("stock_master.html", {
        "request": request,
        "stats": stats,
        "stocks": stocks,
    })


@router.post("/stock-master/sync", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def stock_master_sync(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """POST /admin/stock-master/sync — 종목 마스터 동기화 (비동기)."""
    from src.data.cache import get_cache

    cache = get_cache()

    # 이미 실행 중이면 중복 방지
    try:
        existing = await cache.get_json(_SYNC_NS, _SYNC_KEY)
        if existing and existing.get("status") == "running":
            return templates.TemplateResponse("partials/stock_master_sync_status.html", {
                "request": request,
                "sync_status": "running",
                "sync_count": 0,
                "sync_error": None,
            })
    except Exception:
        pass

    session_factory = get_session_factory()
    background_tasks.add_task(_sync_stock_master_background, session_factory)
    return templates.TemplateResponse("partials/stock_master_sync_status.html", {
        "request": request,
        "sync_status": "running",
        "sync_count": 0,
        "sync_error": None,
    })


@router.get("/stock-master/sync/status", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def stock_master_sync_status(request: Request):
    """GET /admin/stock-master/sync/status — 동기화 상태 폴링."""
    from src.data.cache import get_cache

    cache = get_cache()
    status = "idle"
    count = 0
    error = None

    try:
        data = await cache.get_json(_SYNC_NS, _SYNC_KEY)
        if data:
            status = data.get("status", "idle")
            count = data.get("count", 0)
            error = data.get("error")
    except Exception:
        pass

    return templates.TemplateResponse("partials/stock_master_sync_status.html", {
        "request": request,
        "sync_status": status,
        "sync_count": count,
        "sync_error": error,
    })
