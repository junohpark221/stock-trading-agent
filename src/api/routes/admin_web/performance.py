"""Backoffice trades history + performance analysis routes."""

import math
from datetime import date, timedelta

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.templates import templates
from src.core.time import today_kst
from src.db.models.account import Account
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher
from src.report.metrics import PerformanceCalculator

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/trades", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def trades_history(
    request: Request,
    account_id: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    symbol: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/trades — 매매 이력: 청산 포지션 필터 + 페이지네이션."""
    _from = date.fromisoformat(from_date) if from_date else None
    _to = date.fromisoformat(to_date) if to_date else None

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
    if _from:
        stmt = stmt.where(PositionRecord.exit_date >= _from)
        count_stmt = count_stmt.where(PositionRecord.exit_date >= _from)
    if _to:
        stmt = stmt.where(PositionRecord.exit_date <= _to)
        count_stmt = count_stmt.where(PositionRecord.exit_date <= _to)

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


@router.get("/performance", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def performance_analysis(
    request: Request,
    account_id: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/performance — 성과 분석: 핵심 지표 + 전략별 + 월별."""
    # 날짜 기본값
    _to = date.fromisoformat(to_date) if to_date else today_kst()
    _from = date.fromisoformat(from_date) if from_date else _to - timedelta(days=30)

    # 계좌 목록 (필터 드롭다운용)
    acct_result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts = list(acct_result.scalars().all())

    # 데이터 조회
    fetcher = ReportDataFetcher(get_session_factory())
    closed_positions = await fetcher.get_closed_positions(
        start_date=_from, end_date=_to, account_id=account_id,
    )
    snapshots = await fetcher.get_portfolio_snapshots(
        start_date=_from, end_date=_to, account_id=account_id,
    )

    # 성과 계산
    metrics = PerformanceCalculator.calculate(
        closed_positions=closed_positions,
        snapshots=snapshots,
        period_start=_from,
        period_end=_to,
    )
    strategy_breakdown = PerformanceCalculator.breakdown_by_strategy(closed_positions)
    monthly_breakdown = PerformanceCalculator.breakdown_by_month(closed_positions)

    context = {
        "request": request,
        "metrics": metrics,
        "strategy_breakdown": strategy_breakdown,
        "monthly_breakdown": monthly_breakdown,
        "accounts": accounts,
        "account_id": account_id,
        "from_date": from_date,
        "to_date": to_date,
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/performance_content.html", context)
    return templates.TemplateResponse("performance.html", context)
