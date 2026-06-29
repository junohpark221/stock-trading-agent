"""Backoffice backtest routes: list + run + detail."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import paginate, render
from src.api.routes.backtest import _execute_backtest
from src.api.templates import templates
from src.core.enums import BacktestMode, BacktestStatus, StrategyType
from src.core.models import BacktestConfig
from src.db.models.backtest import BacktestRun, BacktestTrade
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/backtest", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def backtest_page(
    request: Request,
    strategy_type: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/backtest — 백테스트 실행 목록 + 새 실행 폼."""
    stmt = select(BacktestRun).order_by(BacktestRun.started_at.desc())
    count_stmt = select(func.count(BacktestRun.id))

    if strategy_type:
        stmt = stmt.where(BacktestRun.strategy_type == strategy_type)
        count_stmt = count_stmt.where(BacktestRun.strategy_type == strategy_type)
    if status:
        stmt = stmt.where(BacktestRun.status == status)
        count_stmt = count_stmt.where(BacktestRun.status == status)

    runs, page, total, total_pages = await paginate(
        session, stmt, count_stmt, page, per_page,
    )

    context = {
        "request": request,
        "runs": runs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "strategy_type": strategy_type,
        "status": status,
        "strategy_types": [e.value for e in StrategyType],
        "backtest_modes": [e.value for e in BacktestMode],
        "backtest_statuses": [e.value for e in BacktestStatus],
    }

    return render(request, "backtest.html", "partials/backtest_runs.html", context)


@router.post("/backtest/run", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def backtest_run(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/backtest/run — 새 백테스트 실행."""
    form = await request.form()

    strategy_type_val = form.get("strategy_type", "position")
    mode_val = form.get("mode", "technical")
    start_date_val = date.fromisoformat(str(form["start_date"]))
    end_date_val = date.fromisoformat(str(form["end_date"]))
    initial_capital_val = Decimal(str(form.get("initial_capital", "10000000")))
    symbols_raw = str(form.get("symbols", "")).strip()
    symbols_list = [s.strip() for s in symbols_raw.split(",") if s.strip()] if symbols_raw else []

    run_id = uuid4()
    now = datetime.now(UTC)

    db_run = BacktestRun(
        run_id=run_id,
        strategy_type=strategy_type_val,
        mode=mode_val,
        start_date=start_date_val,
        end_date=end_date_val,
        initial_capital=initial_capital_val,
        slippage_bps=10,
        symbols=symbols_list or None,
        parameters=None,
        status=BacktestStatus.PENDING.value,
        total_trades=0,
        started_at=now,
    )
    session.add(db_run)
    await session.commit()

    config = BacktestConfig(
        strategy_type=StrategyType(strategy_type_val),
        start_date=start_date_val,
        end_date=end_date_val,
        initial_capital=initial_capital_val,
        symbols=symbols_list,
        mode=BacktestMode(mode_val),
    )
    background_tasks.add_task(_execute_backtest, run_id, config)

    return RedirectResponse(url="/admin/backtest", status_code=303)


@router.get("/backtest/{run_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def backtest_detail(
    request: Request,
    run_id: UUID,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/backtest/{run_id} — 백테스트 상세 (HTMX partial)."""
    stmt = select(BacktestRun).where(BacktestRun.run_id == run_id)
    run = (await session.execute(stmt)).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    trades = []
    if run.status == BacktestStatus.COMPLETED.value:
        trade_stmt = (
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run_id)
            .order_by(BacktestTrade.trade_date)
        )
        trades = list((await session.execute(trade_stmt)).scalars().all())

    return templates.TemplateResponse("partials/backtest_detail.html", {
        "request": request,
        "run": run,
        "trades": trades,
    })
