"""Backoffice web UI routes."""

import math
from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import login_handler, logout_handler, require_admin
from src.api.templates import templates
from src.db.models.account import Account
from src.db.models.strategy import PositionRecord
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


# ── Account Detail ───────────────────────────────────────────────────────


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def account_detail(
    request: Request,
    account_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/accounts/{account_id} — 계좌 상세: 정보 + 잔고 + 포지션."""
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    fetcher = ReportDataFetcher(get_session_factory())
    snapshot = await fetcher.get_latest_snapshot(account_id=account_id)
    positions = await fetcher.get_open_positions(account_id=account_id)

    return templates.TemplateResponse("account_detail.html", {
        "request": request,
        "account": account,
        "snapshot": snapshot,
        "positions": positions,
    })


# ── Trades History ───────────────────────────────────────────────────────


@router.get("/trades", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def trades_history(
    request: Request,
    account_id: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    symbol: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/trades — 매매 이력: 청산 포지션 필터 + 페이지네이션."""
    # 기본 쿼리 (trades.py 패턴 재사용)
    stmt = (
        select(PositionRecord)
        .where(PositionRecord.status == "closed")
        .order_by(PositionRecord.exit_date.desc())
    )
    count_stmt = select(func.count(PositionRecord.id)).where(
        PositionRecord.status == "closed",
    )

    # 필터 적용
    if account_id:
        stmt = stmt.where(PositionRecord.account_id == account_id)
        count_stmt = count_stmt.where(PositionRecord.account_id == account_id)
    if symbol:
        stmt = stmt.where(PositionRecord.symbol == symbol)
        count_stmt = count_stmt.where(PositionRecord.symbol == symbol)
    if from_date:
        stmt = stmt.where(PositionRecord.exit_date >= from_date)
        count_stmt = count_stmt.where(PositionRecord.exit_date >= from_date)
    if to_date:
        stmt = stmt.where(PositionRecord.exit_date <= to_date)
        count_stmt = count_stmt.where(PositionRecord.exit_date <= to_date)

    # 카운트 + 페이지네이션
    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)

    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)
    trades = list((await session.execute(stmt)).scalars().all())

    # 계좌 목록 (필터 드롭다운용)
    acct_result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts = list(acct_result.scalars().all())

    context = {
        "request": request,
        "trades": trades,
        "accounts": accounts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "account_id": account_id,
        "from_date": from_date,
        "to_date": to_date,
        "symbol": symbol,
    }

    # HTMX 요청이면 partial만 반환
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/trade_rows.html", context)
    return templates.TemplateResponse("trades.html", context)
