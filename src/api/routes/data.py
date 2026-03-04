"""Data API routes — read-only endpoints for querying collected market data.

Endpoints:
    GET /api/data/stocks   — 종목 마스터 목록 (페이지네이션 + 마켓 필터)
    GET /api/data/ohlcv/{symbol} — 일별 OHLCV (날짜 범위 + 페이지네이션)
    GET /api/data/stats    — 수집 통계 (종목 수, OHLCV 행 수, 날짜 범위)
"""

from datetime import date
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import MarketType
from src.core.models import OHLCV, StockInfo
from src.db.models.market_data import DailyOHLCV, StockMaster
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])


# ── Response Models ──────────────────────────────────────────────────────


class StockListResponse(BaseModel):
    items: list[StockInfo]
    total: int
    limit: int
    offset: int
    market: str | None


class OHLCVListResponse(BaseModel):
    symbol: str
    items: list[OHLCV]
    total: int
    start_date: date | None
    end_date: date | None


class DataStatsResponse(BaseModel):
    stock_master_total: int
    stock_master_active: int
    ohlcv_total_rows: int
    ohlcv_symbols: int
    ohlcv_date_min: date | None
    ohlcv_date_max: date | None
    kospi_count: int
    kosdaq_count: int


# ── GET /api/data/stocks ─────────────────────────────────────────────────


@router.get("/stocks", response_model=StockListResponse)
async def list_stocks(
    market: str | None = Query(None, pattern="^(kospi|kosdaq)$"),
    active_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """종목 마스터 목록 조회. 마켓/활성 필터 + 페이지네이션."""
    try:
        stmt = select(StockMaster)
        count_stmt = select(func.count()).select_from(StockMaster)

        if market:
            stmt = stmt.where(StockMaster.market_type == market)
            count_stmt = count_stmt.where(StockMaster.market_type == market)
        if active_only:
            stmt = stmt.where(StockMaster.is_active.is_(True))
            count_stmt = count_stmt.where(StockMaster.is_active.is_(True))

        total = (await session.execute(count_stmt)).scalar_one()

        stmt = stmt.order_by(StockMaster.symbol).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

        items = [
            StockInfo(
                symbol=row.symbol,
                name=row.name,
                market_type=MarketType(row.market_type),
                sector=row.sector or "",
                listed_shares=row.listed_shares or 0,
                market_cap_krw=Decimal(row.market_cap_krw or 0),
            )
            for row in rows
        ]

        body = StockListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            market=market,
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("list_stocks_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/data/ohlcv/{symbol} ─────────────────────────────────────────


@router.get("/ohlcv/{symbol}", response_model=OHLCVListResponse)
async def get_ohlcv(
    symbol: str,
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """종목별 일별 OHLCV 조회. 날짜 범위 필터 + 페이지네이션."""
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must be <= end_date"
        )

    has_date_filter = start_date is not None or end_date is not None

    try:
        stmt = select(DailyOHLCV).where(DailyOHLCV.symbol == symbol)
        count_stmt = (
            select(func.count())
            .select_from(DailyOHLCV)
            .where(DailyOHLCV.symbol == symbol)
        )

        if start_date:
            stmt = stmt.where(DailyOHLCV.date >= start_date)
            count_stmt = count_stmt.where(DailyOHLCV.date >= start_date)
        if end_date:
            stmt = stmt.where(DailyOHLCV.date <= end_date)
            count_stmt = count_stmt.where(DailyOHLCV.date <= end_date)

        total = (await session.execute(count_stmt)).scalar_one()

        if total == 0 and not has_date_filter:
            raise HTTPException(
                status_code=404,
                detail=f"No OHLCV data for symbol '{symbol}'",
            )

        stmt = stmt.order_by(DailyOHLCV.date.desc()).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

        items = [
            OHLCV(
                symbol=row.symbol,
                date=row.date,
                open=Decimal(row.open),
                high=Decimal(row.high),
                low=Decimal(row.low),
                close=Decimal(row.close),
                volume=row.volume,
                value=Decimal(row.trading_value or 0),
            )
            for row in rows
        ]

        body = OHLCVListResponse(
            symbol=symbol,
            items=items,
            total=total,
            start_date=start_date,
            end_date=end_date,
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except HTTPException:
        raise
    except Exception:
        logger.exception("get_ohlcv_failed", symbol=symbol)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/data/stats ──────────────────────────────────────────────────


@router.get("/stats", response_model=DataStatsResponse)
async def get_data_stats(
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """수집된 데이터 통계 조회."""
    try:
        # stock_master aggregates
        stock_result = await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(StockMaster.is_active.is_(True)).label("active"),
                func.count()
                .filter(StockMaster.market_type == "kospi")
                .label("kospi"),
                func.count()
                .filter(StockMaster.market_type == "kosdaq")
                .label("kosdaq"),
            ).select_from(StockMaster)
        )
        sr = stock_result.one()

        # daily_ohlcv aggregates
        ohlcv_result = await session.execute(
            select(
                func.count().label("total_rows"),
                func.count(distinct(DailyOHLCV.symbol)).label("symbols"),
                func.min(DailyOHLCV.date).label("date_min"),
                func.max(DailyOHLCV.date).label("date_max"),
            ).select_from(DailyOHLCV)
        )
        ohlcv = ohlcv_result.one()

        body = DataStatsResponse(
            stock_master_total=sr.total,
            stock_master_active=sr.active,
            ohlcv_total_rows=ohlcv.total_rows,
            ohlcv_symbols=ohlcv.symbols,
            ohlcv_date_min=ohlcv.date_min,
            ohlcv_date_max=ohlcv.date_max,
            kospi_count=sr.kospi,
            kosdaq_count=sr.kosdaq,
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("get_data_stats_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None
