"""Backoffice 성과·거래 (merged performance + trades) routes.

Trades history and performance analysis share the same source — closed
positions — so they live on one screen with ``?tab=metrics|trades``. The old
``/admin/trades`` path redirects here to preserve bookmarks.
"""

from datetime import date, timedelta

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import active_accounts, paginate, render
from src.core.time import today_kst
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher
from src.report.metrics import PerformanceCalculator

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/trades", dependencies=[Depends(require_admin)])
async def trades_redirect(request: Request):
    """GET /admin/trades — 성과·거래 화면의 거래 탭으로 리다이렉트 (북마크 보존)."""
    qs = request.url.query
    target = "/admin/performance?tab=trades"
    if qs:
        target = f"{target}&{qs}"
    return RedirectResponse(target, status_code=303)


@router.get("/performance", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def performance_analysis(
    request: Request,
    tab: str = Query("metrics"),
    account_id: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    symbol: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/performance — 성과 지표(metrics) + 거래 이력(trades) 탭."""
    _to = date.fromisoformat(to_date) if to_date else today_kst()
    _from = date.fromisoformat(from_date) if from_date else _to - timedelta(days=30)

    accounts = await active_accounts(session)

    base_ctx = {
        "request": request,
        "tab": tab,
        "accounts": accounts,
        "account_id": account_id,
        "from_date": from_date,
        "to_date": to_date,
        "symbol": symbol,
        "per_page": per_page,
    }

    if tab == "trades":
        stmt = (
            select(PositionRecord)
            .where(PositionRecord.status == "closed")
            .order_by(PositionRecord.exit_date.desc())
        )
        count_stmt = select(func.count(PositionRecord.id)).where(
            PositionRecord.status == "closed",
        )
        if account_id:
            stmt = stmt.where(PositionRecord.account_id == account_id)
            count_stmt = count_stmt.where(PositionRecord.account_id == account_id)
        if symbol:
            stmt = stmt.where(PositionRecord.symbol == symbol)
            count_stmt = count_stmt.where(PositionRecord.symbol == symbol)
        if from_date:
            stmt = stmt.where(PositionRecord.exit_date >= _from)
            count_stmt = count_stmt.where(PositionRecord.exit_date >= _from)
        if to_date:
            stmt = stmt.where(PositionRecord.exit_date <= _to)
            count_stmt = count_stmt.where(PositionRecord.exit_date <= _to)

        trades, page, total, total_pages = await paginate(
            session, stmt, count_stmt, page, per_page,
        )
        context = {
            **base_ctx,
            "trades": trades,
            "total": total,
            "page": page,
            "total_pages": total_pages,
        }
        return render(request, "performance.html", "partials/trades_table.html", context)

    # metrics 탭
    fetcher = ReportDataFetcher(get_session_factory())
    closed_positions = await fetcher.get_closed_positions(
        start_date=_from, end_date=_to, account_id=account_id,
    )
    snapshots = await fetcher.get_portfolio_snapshots(
        start_date=_from, end_date=_to, account_id=account_id,
    )
    metrics = PerformanceCalculator.calculate(
        closed_positions=closed_positions,
        snapshots=snapshots,
        period_start=_from,
        period_end=_to,
    )
    strategy_breakdown = PerformanceCalculator.breakdown_by_strategy(closed_positions)
    monthly_breakdown = PerformanceCalculator.breakdown_by_month(closed_positions)

    context = {
        **base_ctx,
        "metrics": metrics,
        "strategy_breakdown": strategy_breakdown,
        "monthly_breakdown": monthly_breakdown,
    }
    return render(request, "performance.html", "partials/performance_content.html", context)
