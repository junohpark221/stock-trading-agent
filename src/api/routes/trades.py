"""Trade history API — closed positions + performance metrics.

Endpoints:
    GET /api/trades                — 종료된 거래 목록
    GET /api/trades/summary        — 거래 요약 + PerformanceMetrics
    GET /api/trades/{position_id}  — 단일 거래 상세
"""

from __future__ import annotations

from datetime import date, timedelta

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.execution import Execution, Order
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session, get_session_factory
from src.report.data_fetcher import ReportDataFetcher
from src.report.metrics import PerformanceCalculator

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


# ── List Trades ──────────────────────────────────────────────────────


@router.get("")
async def list_trades(
    symbol: str | None = Query(None),
    strategy_type: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """종료된 거래 목록.

    positions 테이블 WHERE status='closed'.
    필터: symbol, strategy_type, exit_date BETWEEN from_date AND to_date.
    정렬: exit_date DESC.
    """
    stmt = (
        select(PositionRecord)
        .where(PositionRecord.status == "closed")
        .order_by(PositionRecord.exit_date.desc())
    )
    count_stmt = select(func.count(PositionRecord.id)).where(
        PositionRecord.status == "closed"
    )

    if symbol is not None:
        stmt = stmt.where(PositionRecord.symbol == symbol)
        count_stmt = count_stmt.where(PositionRecord.symbol == symbol)
    if strategy_type is not None:
        stmt = stmt.where(PositionRecord.strategy_type == strategy_type)
        count_stmt = count_stmt.where(PositionRecord.strategy_type == strategy_type)
    if from_date is not None:
        stmt = stmt.where(PositionRecord.exit_date >= from_date)
        count_stmt = count_stmt.where(PositionRecord.exit_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(PositionRecord.exit_date <= to_date)
        count_stmt = count_stmt.where(PositionRecord.exit_date <= to_date)

    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        {
            "id": row.id,
            "symbol": row.symbol,
            "strategy_type": row.strategy_type,
            "quantity": row.quantity,
            "entry_price": str(row.entry_price),
            "entry_date": row.entry_date.isoformat() if row.entry_date else None,
            "exit_price": str(row.exit_price) if row.exit_price else None,
            "exit_date": row.exit_date.isoformat() if row.exit_date else None,
            "exit_reason": row.exit_reason,
            "realized_pnl": str(row.realized_pnl) if row.realized_pnl is not None else None,
            "status": row.status,
        }
        for row in rows
    ]

    return JSONResponse({"items": items, "total": total})


# ── Trades Summary ───────────────────────────────────────────────────


@router.get("/summary")
async def trades_summary(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    strategy_type: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """거래 요약 + PerformanceMetrics.

    ReportDataFetcher + PerformanceCalculator 사용.
    기본 기간: 최근 30일.
    """
    period_end = to_date or date.today()
    period_start = from_date or (period_end - timedelta(days=30))

    fetcher = ReportDataFetcher(session_factory=get_session_factory())

    closed_positions = await fetcher.get_closed_positions(
        start_date=period_start,
        end_date=period_end,
        strategy_type=strategy_type,
    )
    snapshots = await fetcher.get_portfolio_snapshots(
        start_date=period_start,
        end_date=period_end,
    )

    metrics = PerformanceCalculator.calculate(
        closed_positions=closed_positions,
        snapshots=snapshots,
        period_start=period_start,
        period_end=period_end,
    )

    # 전략별 breakdown
    strategy_breakdown: dict[str, dict] = {}
    for pos in closed_positions:
        st = pos.strategy_type
        if st not in strategy_breakdown:
            strategy_breakdown[st] = {"total": 0, "winning": 0, "losing": 0}
        strategy_breakdown[st]["total"] += 1
        if pos.realized_pnl is not None and pos.realized_pnl > 0:
            strategy_breakdown[st]["winning"] += 1
        else:
            strategy_breakdown[st]["losing"] += 1

    return JSONResponse({
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total_return_pct": str(metrics.total_return_pct),
        "annualized_return_pct": str(metrics.annualized_return_pct) if metrics.annualized_return_pct is not None else None,
        "sharpe_ratio": str(metrics.sharpe_ratio) if metrics.sharpe_ratio is not None else None,
        "sortino_ratio": str(metrics.sortino_ratio) if metrics.sortino_ratio is not None else None,
        "max_drawdown_pct": str(metrics.max_drawdown_pct),
        "win_rate_pct": str(metrics.win_rate_pct),
        "avg_win_pct": str(metrics.avg_win_pct),
        "avg_loss_pct": str(metrics.avg_loss_pct),
        "profit_factor": str(metrics.profit_factor) if metrics.profit_factor is not None else None,
        "total_trades": metrics.total_trades,
        "winning_trades": metrics.winning_trades,
        "losing_trades": metrics.losing_trades,
        "strategy_breakdown": strategy_breakdown,
    })


# ── Trade Detail ─────────────────────────────────────────────────────


@router.get("/{position_id}")
async def get_trade_detail(
    position_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """단일 거래 상세 (진입/청산/PnL + 관련 주문/체결).

    404: 없는 position_id.
    """
    # 포지션 조회
    pos_result = await session.execute(
        select(PositionRecord).where(PositionRecord.id == position_id)
    )
    position = pos_result.scalar_one_or_none()
    if position is None:
        return JSONResponse(status_code=404, content={"error": "position_not_found"})

    # 주문 조회
    orders_result = await session.execute(
        select(Order)
        .where(Order.position_id == position_id)
        .order_by(Order.created_at.asc())
    )
    orders = list(orders_result.scalars().all())

    # 체결 조회 (주문별)
    order_ids = [o.id for o in orders]
    executions: list = []
    if order_ids:
        exec_result = await session.execute(
            select(Execution)
            .where(Execution.order_id.in_(order_ids))
            .order_by(Execution.executed_at.asc())
        )
        executions = list(exec_result.scalars().all())

    return JSONResponse({
        "position": {
            "id": position.id,
            "symbol": position.symbol,
            "strategy_type": position.strategy_type,
            "quantity": position.quantity,
            "entry_price": str(position.entry_price),
            "entry_date": position.entry_date.isoformat() if position.entry_date else None,
            "exit_price": str(position.exit_price) if position.exit_price else None,
            "exit_date": position.exit_date.isoformat() if position.exit_date else None,
            "exit_reason": position.exit_reason,
            "realized_pnl": str(position.realized_pnl) if position.realized_pnl is not None else None,
            "status": position.status,
            "stop_loss_price": str(position.stop_loss_price),
            "take_profit_price": str(position.take_profit_price) if position.take_profit_price else None,
        },
        "orders": [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "order_type": o.order_type,
                "quantity": o.quantity,
                "price": str(o.price),
                "status": o.status,
                "filled_quantity": o.filled_quantity,
                "filled_price": str(o.filled_price) if o.filled_price else None,
                "executed_at": o.executed_at.isoformat() if o.executed_at else None,
            }
            for o in orders
        ],
        "executions": [
            {
                "id": e.id,
                "order_id": e.order_id,
                "fill_price": str(e.fill_price),
                "fill_quantity": e.fill_quantity,
                "commission": str(e.commission),
                "executed_at": e.executed_at.isoformat() if e.executed_at else None,
            }
            for e in executions
        ],
    })
