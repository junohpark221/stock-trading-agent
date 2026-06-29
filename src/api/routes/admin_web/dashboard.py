"""Backoffice dashboard + orphaned-position cleanup routes."""

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.portfolio_live import fetch_portfolio_view
from src.api.templates import templates
from src.db.models.account import Account
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def dashboard(
    request: Request,
    cleanup_msg: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/ — 대시보드: 시스템 상태 + 계좌 현황 + 오늘 매매."""
    # 1) DB 상태
    db_status = "disconnected"
    try:
        await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        logger.warning("dashboard_db_check_failed", exc_info=True)

    # 2) Redis 상태 (lazy import to avoid circular dependency with main)
    from src.main import get_redis

    redis_status = "disconnected"
    try:
        await get_redis().ping()
        redis_status = "connected"
    except Exception:
        logger.warning("dashboard_redis_check_failed", exc_info=True)

    # 3) 스케줄러 상태 (lazy import to avoid circular dependency with main)
    from src.main import get_scheduler

    try:
        scheduler_status = get_scheduler().get_status()
    except RuntimeError:
        scheduler_status = {"is_running": False, "is_paused": False, "jobs": [], "disabled": True}

    # 4) 계좌 목록 + 라이브 잔고 (30초 Redis 캐시, 실패 시 DB 스냅샷 폴백)
    result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts_orm = list(result.scalars().all())

    account_cards = []
    for acct in accounts_orm:
        view = await fetch_portfolio_view(acct.id)
        account_cards.append({
            "id": acct.id,
            "nickname": acct.nickname,
            "strategy_type": acct.strategy_type,
            "is_paper": acct.kis_is_paper,
            "total_value": view.total_value if view else None,
            "unrealized_pnl": view.unrealized_pnl if view else None,
            "realized_pnl_daily": view.realized_pnl_daily if view else None,
            "positions_count": view.positions_count if view else 0,
            "snapshot_date": view.snapshot_date if view else None,
            "is_live": view.is_live if view else False,
        })

    # 5) 오늘 주문
    fetcher = ReportDataFetcher(get_session_factory())
    todays_orders = await fetcher.get_todays_orders(account_id=None)
    recent_orders = todays_orders[-20:][::-1]  # 최근 20건, 최신순

    order_summary = {
        "total": len(todays_orders),
        "buy": sum(1 for o in todays_orders if o.side == "buy"),
        "sell": sum(1 for o in todays_orders if o.side == "sell"),
        "filled": sum(1 for o in todays_orders if o.status == "filled"),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "db_status": db_status,
        "redis_status": redis_status,
        "scheduler": scheduler_status,
        "accounts": account_cards,
        "recent_orders": recent_orders,
        "order_summary": order_summary,
        "cleanup_msg": cleanup_msg,
    })


@router.post("/cleanup-orphaned-positions", dependencies=[Depends(require_admin)])
async def cleanup_orphaned_positions(request: Request):
    """POST /admin/cleanup-orphaned-positions — 브로커-DB 포지션 정합성 검증."""
    from src.execution.reconciler import PositionReconciler
    from src.main import get_broker_registry
    from src.strategy.position_manager import PositionManager

    # B-03: position_manager 주입 → 풀 3-way 보장 (스케줄러와 동일 경로)
    reconciler = PositionReconciler(
        broker_registry=get_broker_registry(),
        session_factory=get_session_factory(),
        position_manager=PositionManager(get_session_factory()),
    )
    result = await reconciler.reconcile()

    msg = (
        f"포지션 {result.closed_count}건 정리, "
        f"주문 {result.order_corrected_count}건 보정 "
        f"(브로커: {result.broker_symbol_count}종목, "
        f"DB: {result.db_open_count}종목)"
    )
    logger.info("admin.cleanup_orphaned_positions", **vars(result))
    return RedirectResponse(f"/admin/?cleanup_msg={msg}", status_code=303)
