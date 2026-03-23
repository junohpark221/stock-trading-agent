"""Portfolio API routes — portfolio state, positions, snapshots, and risk checks.

Endpoints:
    GET  /api/portfolio/state       — 현재 포트폴리오 상태 (PortfolioState)
    GET  /api/portfolio/positions   — 보유 포지션 목록 (status/strategy_type 필터)
    GET  /api/portfolio/snapshots   — 일별 스냅샷 이력 (from/to 필터)
    POST /api/portfolio/risk-check  — 특정 종목 알고리즘 리스크 체크
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import SignalAction
from src.db.models.strategy import PortfolioSnapshot, PositionRecord
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ── Response Models ──────────────────────────────────────────────────────


class PositionItem(BaseModel):
    """PositionRecord → API 응답 변환."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    strategy_type: str
    quantity: int
    avg_cost: Decimal
    entry_price: Decimal
    entry_date: date
    stop_loss_price: Decimal
    take_profit_price: Decimal | None
    status: str
    exit_price: Decimal | None
    exit_date: date | None
    exit_reason: str | None
    realized_pnl: Decimal | None


class PositionListResponse(BaseModel):
    items: list[PositionItem]
    total: int


class SnapshotItem(BaseModel):
    """PortfolioSnapshot → API 응답 변환."""

    model_config = ConfigDict(from_attributes=True)

    snapshot_date: date
    total_value: Decimal
    cash: Decimal
    invested: Decimal
    unrealized_pnl: Decimal
    peak_value: Decimal
    drawdown_pct: Decimal
    positions_count: int
    trade_count_daily: int


class SnapshotListResponse(BaseModel):
    items: list[SnapshotItem]
    total: int


class RiskCheckRequest(BaseModel):
    """알고리즘 리스크 체크 요청."""

    symbol: str
    action: SignalAction = SignalAction.BUY
    quantity: int = Field(..., gt=0)
    price: Decimal = Field(..., gt=0)
    stop_loss_price: Decimal | None = None
    sector: str = "기타"


# ── Helpers ──────────────────────────────────────────────────────────────


async def _build_portfolio_service():
    """PortfolioStateService 의존성 조립. KISClient를 connect한 상태로 반환."""
    from src.broker.kis.client import KISClient
    from src.config import get_settings
    from src.data.cache import get_cache
    from src.db.session import get_session_factory
    from src.strategy.portfolio_state import PortfolioStateService

    settings = get_settings()
    session_factory = get_session_factory()
    cache = get_cache()

    broker = KISClient(settings=settings, cache=cache)
    await broker.connect()
    return PortfolioStateService(
        broker=broker,
        session_factory=session_factory,
        cache=cache,
    ), broker


async def _build_risk_manager():
    """AlgoRiskManager 의존성 조립. KISClient를 connect한 상태로 반환."""
    from src.config import get_settings
    from src.data.cache import get_cache
    from src.db.session import get_session_factory
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.risk_manager import AlgoRiskManager

    settings = get_settings()
    session_factory = get_session_factory()
    cache = get_cache()

    from src.broker.kis.client import KISClient

    broker = KISClient(settings=settings, cache=cache)
    await broker.connect()
    portfolio_service = PortfolioStateService(
        broker=broker,
        session_factory=session_factory,
        cache=cache,
    )
    return AlgoRiskManager(
        portfolio_service=portfolio_service,
        session_factory=session_factory,
        settings=settings,
    ), broker


# ── GET /api/portfolio/state ──────────────────────────────────────────────


@router.get("/state")
async def get_portfolio_state() -> JSONResponse:
    """현재 포트폴리오 상태 조회 (Broker 잔고 + DB 이력 기반)."""
    broker = None
    try:
        service, broker = await _build_portfolio_service()
        state = await service.get_current_state()
        return JSONResponse(content=state.model_dump(mode="json"))

    except Exception:
        logger.exception("get_portfolio_state_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None
    finally:
        if broker:
            await broker.disconnect()


# ── GET /api/portfolio/positions ──────────────────────────────────────────


@router.get("/positions", response_model=PositionListResponse)
async def list_positions(
    status: str | None = Query(None, description="open 또는 closed"),
    strategy_type: str | None = Query(None, description="position 또는 swing"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """보유 포지션 목록 조회. status/strategy_type 필터 지원."""
    try:
        stmt = select(PositionRecord)
        count_stmt = select(func.count()).select_from(PositionRecord)

        if status:
            stmt = stmt.where(PositionRecord.status == status)
            count_stmt = count_stmt.where(PositionRecord.status == status)
        if strategy_type:
            stmt = stmt.where(PositionRecord.strategy_type == strategy_type)
            count_stmt = count_stmt.where(PositionRecord.strategy_type == strategy_type)

        total = (await session.execute(count_stmt)).scalar_one()

        stmt = stmt.order_by(PositionRecord.entry_date.desc()).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

        items = [PositionItem.model_validate(row) for row in rows]
        body = PositionListResponse(items=items, total=total)
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("list_positions_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/portfolio/snapshots ──────────────────────────────────────────


@router.get("/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    limit: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """일별 포트폴리오 스냅샷 이력 조회."""
    try:
        stmt = select(PortfolioSnapshot)
        count_stmt = select(func.count()).select_from(PortfolioSnapshot)

        if from_date:
            stmt = stmt.where(PortfolioSnapshot.snapshot_date >= from_date)
            count_stmt = count_stmt.where(PortfolioSnapshot.snapshot_date >= from_date)
        if to_date:
            stmt = stmt.where(PortfolioSnapshot.snapshot_date <= to_date)
            count_stmt = count_stmt.where(PortfolioSnapshot.snapshot_date <= to_date)

        total = (await session.execute(count_stmt)).scalar_one()

        stmt = stmt.order_by(PortfolioSnapshot.snapshot_date.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

        items = [SnapshotItem.model_validate(row) for row in rows]
        body = SnapshotListResponse(items=items, total=total)
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("list_snapshots_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── POST /api/portfolio/risk-check ────────────────────────────────────────


@router.post("/risk-check")
async def run_risk_check(req: RiskCheckRequest) -> JSONResponse:
    """특정 종목에 대해 알고리즘 리스크 8규칙 체크를 수행한다."""
    broker = None
    try:
        manager, broker = await _build_risk_manager()
        result = await manager.check(
            symbol=req.symbol,
            action=req.action,
            quantity=req.quantity,
            price=req.price,
            stop_loss_price=req.stop_loss_price,
            sector=req.sector,
        )
        return JSONResponse(content=result.model_dump(mode="json"))

    except Exception:
        logger.exception("run_risk_check_failed", symbol=req.symbol)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    finally:
        if broker:
            await broker.disconnect()
