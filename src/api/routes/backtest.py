"""Backtest API routes — 백테스트 실행/조회/비교.

Endpoints:
    POST /api/backtest/run                  — 백테스트 실행 시작 (BackgroundTasks)
    GET  /api/backtest/runs                 — 실행 목록 (필터/페이지네이션)
    GET  /api/backtest/runs/{run_id}        — 단일 결과 상세 (metrics + trades)
    GET  /api/backtest/compare              — 복수 결과 비교
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backtest.data_loader import HistoricalDataLoader
from src.backtest.engine import BacktestEngine
from src.backtest.simulator import SimulatedBroker
from src.config import get_settings
from src.core.enums import BacktestMode, BacktestStatus, StrategyType
from src.core.models import BacktestConfig
from src.db.models.backtest import BacktestRun, BacktestTrade
from src.db.session import get_db_session, get_session_factory

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ── Request / Response Models ─────────────────────────────────────────


class RunBacktestRequest(BaseModel):
    """POST /run 요청."""

    strategy_type: StrategyType
    start_date: date
    end_date: date
    initial_capital: Decimal = Decimal("10000000")
    symbols: list[str] = Field(default_factory=list)
    slippage_bps: int = 10
    mode: BacktestMode = BacktestMode.TECHNICAL
    parameters: dict = Field(default_factory=dict)
    llm_model_filter: str | None = None
    benchmark_symbol: str = "KOSPI"


class RunBacktestResponse(BaseModel):
    run_id: UUID
    status: str


class BacktestRunItem(BaseModel):
    """백테스트 실행 요약."""

    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    strategy_type: str
    mode: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    slippage_bps: int
    symbols: list | None = None
    parameters: dict | None = None
    status: str
    result_metrics: dict | None = None
    total_trades: int
    error_message: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class BacktestTradeItem(BaseModel):
    """거래 내역."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    side: str
    quantity: int
    price: Decimal
    commission: Decimal
    slippage: Decimal
    trade_date: date
    pnl: Decimal | None = None
    exit_reason: str | None = None


class BacktestRunDetail(BaseModel):
    run: BacktestRunItem
    trades: list[BacktestTradeItem] | None = None


class BacktestRunListResponse(BaseModel):
    items: list[BacktestRunItem]
    total: int


class CompareRunItem(BaseModel):
    run_id: UUID
    strategy_type: str
    status: str
    result_metrics: dict | None = None


class CompareResponse(BaseModel):
    runs: list[CompareRunItem]
    best_sharpe_run_id: UUID | None = None
    best_return_run_id: UUID | None = None
    lowest_mdd_run_id: UUID | None = None
    warnings: list[str] = Field(default_factory=list)


# ── Background Task ───────────────────────────────────────────────────


async def _execute_backtest(run_id: UUID, config: BacktestConfig) -> None:
    """백테스트 실제 실행 (BackgroundTasks에서 호출).

    get_session_factory()로 별도 세션 획득 — Depends 사용 불가.
    """
    session_factory = get_session_factory()
    settings = get_settings()

    try:
        # status → running
        async with session_factory() as session:
            stmt = (
                select(BacktestRun)
                .where(BacktestRun.run_id == run_id)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                row.status = BacktestStatus.RUNNING.value
                await session.commit()

        # 엔진 조립 + 실행
        loader = HistoricalDataLoader(session_factory)
        await loader.load(
            symbols=config.symbols,
            start_date=config.start_date,
            end_date=config.end_date,
        )

        broker = SimulatedBroker(
            data_loader=loader,
            initial_capital=config.initial_capital,
        )
        engine = BacktestEngine(
            data_loader=loader,
            broker=broker,
            settings=settings,
            session_factory=session_factory,
        )

        result = await engine.run(config)

        # DB 갱신 — API run_id 사용 (engine 내부 run_id 무시)
        async with session_factory() as session:
            stmt = (
                select(BacktestRun)
                .where(BacktestRun.run_id == run_id)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                row.status = result.status.value
                row.result_metrics = (
                    result.metrics.model_dump(mode="json")
                    if result.metrics
                    else None
                )
                row.total_trades = result.total_trades
                row.completed_at = result.completed_at
                row.error_message = result.error_message

                # 거래 내역 삽입
                for t in result.trades:
                    session.add(BacktestTrade(
                        run_id=run_id,
                        symbol=t.symbol,
                        side=t.side.value if hasattr(t.side, "value") else str(t.side),
                        quantity=t.quantity,
                        price=t.price,
                        commission=t.commission,
                        slippage=t.slippage,
                        trade_date=t.trade_date,
                        pnl=t.pnl,
                        exit_reason=(
                            t.exit_reason.value
                            if hasattr(t.exit_reason, "value")
                            else t.exit_reason
                        ),
                    ))

                await session.commit()

        logger.info(
            "backtest_background_completed",
            run_id=str(run_id),
            status=result.status.value,
            total_trades=result.total_trades,
        )

    except Exception as exc:
        logger.exception("backtest_background_failed", run_id=str(run_id))
        try:
            async with session_factory() as session:
                stmt = (
                    select(BacktestRun)
                    .where(BacktestRun.run_id == run_id)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row:
                    row.status = BacktestStatus.FAILED.value
                    row.error_message = str(exc)
                    row.completed_at = datetime.now(UTC)
                    await session.commit()
        except Exception:
            logger.exception("backtest_status_update_failed", run_id=str(run_id))


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post("/run")
async def run_backtest(
    req: RunBacktestRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """백테스트 실행 시작 — 즉시 run_id 반환, 백그라운드로 실행."""
    if req.start_date > req.end_date:
        raise HTTPException(
            status_code=422,
            detail="start_date must be <= end_date",
        )

    run_id = uuid4()
    now = datetime.now(UTC)

    db_run = BacktestRun(
        run_id=run_id,
        strategy_type=req.strategy_type.value,
        mode=req.mode.value,
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
        slippage_bps=req.slippage_bps,
        symbols=req.symbols or None,
        parameters=req.parameters or None,
        status=BacktestStatus.PENDING.value,
        total_trades=0,
        started_at=now,
    )
    session.add(db_run)
    await session.commit()

    config = BacktestConfig(
        strategy_type=req.strategy_type,
        start_date=req.start_date,
        end_date=req.end_date,
        initial_capital=req.initial_capital,
        symbols=req.symbols,
        slippage_bps=req.slippage_bps,
        mode=req.mode,
        parameters=req.parameters,
        llm_model_filter=req.llm_model_filter,
        benchmark_symbol=req.benchmark_symbol,
    )
    background_tasks.add_task(_execute_backtest, run_id, config)

    logger.info("backtest_queued", run_id=str(run_id))

    return JSONResponse(
        content=RunBacktestResponse(
            run_id=run_id,
            status="pending",
        ).model_dump(mode="json"),
    )


@router.get("/runs")
async def list_runs(
    strategy_type: str | None = Query(None),
    status: str | None = Query(None),
    from_date: date | None = Query(None, description="start_date >= from_date"),
    to_date: date | None = Query(None, description="end_date <= to_date"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """백테스트 실행 목록 조회."""
    base = select(BacktestRun)
    count_stmt = select(func.count()).select_from(BacktestRun)

    if strategy_type:
        base = base.where(BacktestRun.strategy_type == strategy_type)
        count_stmt = count_stmt.where(BacktestRun.strategy_type == strategy_type)
    if status:
        base = base.where(BacktestRun.status == status)
        count_stmt = count_stmt.where(BacktestRun.status == status)
    if from_date:
        base = base.where(BacktestRun.start_date >= from_date)
        count_stmt = count_stmt.where(BacktestRun.start_date >= from_date)
    if to_date:
        base = base.where(BacktestRun.end_date <= to_date)
        count_stmt = count_stmt.where(BacktestRun.end_date <= to_date)

    total = (await session.execute(count_stmt)).scalar_one()

    stmt = base.order_by(BacktestRun.started_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()

    items = [
        BacktestRunItem.model_validate(r).model_dump(mode="json") for r in rows
    ]

    return JSONResponse(content={"items": items, "total": total})


@router.get("/runs/{run_id}")
async def get_run(
    run_id: UUID,
    include_trades: bool = Query(True),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """단일 백테스트 결과 상세."""
    stmt = select(BacktestRun).where(BacktestRun.run_id == run_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    run_item = BacktestRunItem.model_validate(row).model_dump(mode="json")

    trades = None
    if include_trades:
        trade_stmt = (
            select(BacktestTrade)
            .where(BacktestTrade.run_id == run_id)
            .order_by(BacktestTrade.trade_date)
        )
        trade_rows = (await session.execute(trade_stmt)).scalars().all()
        trades = [
            BacktestTradeItem.model_validate(t).model_dump(mode="json")
            for t in trade_rows
        ]

    return JSONResponse(content={"run": run_item, "trades": trades})


@router.get("/compare")
async def compare_runs(
    run_ids: str = Query(..., description="Comma-separated run_ids (max 10)"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """복수 백테스트 결과 비교."""
    # 파싱
    raw_ids = [s.strip() for s in run_ids.split(",") if s.strip()]
    try:
        parsed_ids = [UUID(rid) for rid in raw_ids]
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid UUID format in run_ids",
        ) from None

    if len(parsed_ids) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 run_ids allowed")
    if len(parsed_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 run_ids required")

    stmt = select(BacktestRun).where(BacktestRun.run_id.in_(parsed_ids))
    rows = (await session.execute(stmt)).scalars().all()
    found_map: dict[UUID, BacktestRun] = {r.run_id: r for r in rows}

    warnings: list[str] = []
    for rid in parsed_ids:
        if rid not in found_map:
            warnings.append(f"Run {rid} not found, skipped")

    runs: list[dict] = []
    best_sharpe: tuple[Decimal | None, UUID | None] = (None, None)
    best_return: tuple[Decimal | None, UUID | None] = (None, None)
    lowest_mdd: tuple[Decimal | None, UUID | None] = (None, None)

    for row in rows:
        runs.append(CompareRunItem(
            run_id=row.run_id,
            strategy_type=row.strategy_type,
            status=row.status,
            result_metrics=row.result_metrics,
        ).model_dump(mode="json"))

        m = row.result_metrics
        if not m:
            continue

        sharpe = m.get("sharpe_ratio")
        ret = m.get("total_return_pct")
        mdd = m.get("max_drawdown_pct")

        if sharpe is not None:
            val = Decimal(str(sharpe))
            if best_sharpe[0] is None or val > best_sharpe[0]:
                best_sharpe = (val, row.run_id)

        if ret is not None:
            val = Decimal(str(ret))
            if best_return[0] is None or val > best_return[0]:
                best_return = (val, row.run_id)

        if mdd is not None:
            val = Decimal(str(mdd))
            # MDD는 음수(혹은 절대값), 낮을수록 좋음 — 절대값이 작은 게 best
            if lowest_mdd[0] is None or abs(val) < abs(lowest_mdd[0]):
                lowest_mdd = (val, row.run_id)

    resp = CompareResponse(
        runs=runs,
        best_sharpe_run_id=best_sharpe[1],
        best_return_run_id=best_return[1],
        lowest_mdd_run_id=lowest_mdd[1],
        warnings=warnings,
    )
    return JSONResponse(content=resp.model_dump(mode="json"))
