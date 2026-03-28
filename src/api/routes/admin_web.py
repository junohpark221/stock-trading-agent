"""Backoffice web UI routes."""

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import login_handler, logout_handler, require_admin
from src.api.templates import templates
from src.db.models.account import Account
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin-web"])


# ── Login / Logout ───────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """GET /admin/login — 로그인 폼 렌더링."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(request: Request):
    """POST /admin/login — 비밀번호 검증 후 쿠키 설정."""
    return await login_handler(request)


@router.get("/logout")
async def logout(request: Request):
    """GET /admin/logout — 쿠키 삭제 후 리다이렉트."""
    return await logout_handler(request)


# ── Dashboard ────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def dashboard(
    request: Request,
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

    # 4) 계좌 목록 + 최신 스냅샷
    result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts_orm = list(result.scalars().all())

    fetcher = ReportDataFetcher(get_session_factory())
    account_cards = []
    for acct in accounts_orm:
        snapshot = await fetcher.get_latest_snapshot(account_id=acct.id)
        account_cards.append({
            "id": acct.id,
            "nickname": acct.nickname,
            "strategy_type": acct.strategy_type,
            "is_paper": acct.kis_is_paper,
            "total_value": snapshot.total_value if snapshot else None,
            "unrealized_pnl": snapshot.unrealized_pnl if snapshot else None,
            "realized_pnl_daily": snapshot.realized_pnl_daily if snapshot else None,
            "positions_count": snapshot.positions_count if snapshot else 0,
            "snapshot_date": snapshot.snapshot_date if snapshot else None,
        })

    # 5) 오늘 주문
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
    })
