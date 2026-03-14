"""Analysis API routes — technical, fundamental, and macro analysis endpoints.

Endpoints:
    GET /api/analysis/technical/{symbol} — 기술적 분석 (지표 + 패턴)
    GET /api/analysis/fundamental/{symbol} — 펀더멘털 종합 점수
    GET /api/analysis/macro — 매크로 경제지표 조회
"""

from datetime import date
from decimal import Decimal

import pandas as pd
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.analysis.fundamental.analyzer import FundamentalAnalyzer
from src.analysis.technical.indicators import compute_all_indicators
from src.analysis.technical.patterns import scan_patterns
from src.core.models import (
    EconomicIndicatorInfo,
    FundamentalScore,
    PatternSignal,
    TechnicalIndicators,
)
from src.db.models.analysis import EconomicIndicator
from src.db.models.market_data import DailyOHLCV
from src.db.session import get_db_session, get_session_factory

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


# ── Response Models ──────────────────────────────────────────────────────


class TechnicalAnalysisResponse(BaseModel):
    symbol: str
    indicators: TechnicalIndicators
    patterns: list[PatternSignal]
    data_points: int


class MacroIndicatorResponse(BaseModel):
    items: list[EconomicIndicatorInfo]
    total: int
    limit: int
    offset: int
    source: str | None
    indicator_code: str | None


# ── GET /api/analysis/technical/{symbol} ─────────────────────────────────


@router.get("/technical/{symbol}", response_model=TechnicalAnalysisResponse)
async def get_technical_analysis(
    symbol: str,
    days: int = Query(200, ge=20, le=1000),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """기술적 분석: OHLCV → 기술지표 계산 + 차트 패턴 감지."""
    try:
        stmt = (
            select(DailyOHLCV)
            .where(DailyOHLCV.symbol == symbol)
            .order_by(DailyOHLCV.date.desc())
            .limit(days)
        )
        rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No OHLCV data for symbol '{symbol}'",
            )

        # DataFrame 구성 (ascending order)
        data = [
            {
                "date": row.date,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": int(row.volume),
            }
            for row in reversed(rows)
        ]
        df = pd.DataFrame(data)
        df.set_index("date", inplace=True)

        indicators = compute_all_indicators(df, symbol)
        patterns = scan_patterns(df, indicators)

        body = TechnicalAnalysisResponse(
            symbol=symbol,
            indicators=indicators,
            patterns=patterns,
            data_points=len(df),
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except HTTPException:
        raise
    except Exception:
        logger.exception("get_technical_analysis_failed", symbol=symbol)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/analysis/fundamental/{symbol} ───────────────────────────────


@router.get("/fundamental/{symbol}", response_model=FundamentalScore)
async def get_fundamental_analysis(
    symbol: str,
) -> JSONResponse:
    """펀더멘털 분석: DART 재무제표 기반 종합 점수 산출."""
    try:
        analyzer = FundamentalAnalyzer(get_session_factory())
        score = await analyzer.analyze(symbol)

        return JSONResponse(content=score.model_dump(mode="json"))

    except Exception:
        logger.exception("get_fundamental_analysis_failed", symbol=symbol)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/analysis/macro ──────────────────────────────────────────────


@router.get("/macro", response_model=MacroIndicatorResponse)
async def get_macro_indicators(
    source: str | None = Query(None, pattern="^(ecos|fred)$"),
    indicator_code: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """매크로 경제지표 조회. 소스/지표코드/날짜 범위 필터 + 페이지네이션."""
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400, detail="start_date must be <= end_date"
        )

    try:
        stmt = select(EconomicIndicator)
        count_stmt = select(func.count()).select_from(EconomicIndicator)

        if source:
            stmt = stmt.where(EconomicIndicator.source == source)
            count_stmt = count_stmt.where(EconomicIndicator.source == source)
        if indicator_code:
            stmt = stmt.where(EconomicIndicator.indicator_code == indicator_code)
            count_stmt = count_stmt.where(
                EconomicIndicator.indicator_code == indicator_code
            )
        if start_date:
            stmt = stmt.where(EconomicIndicator.date >= start_date)
            count_stmt = count_stmt.where(EconomicIndicator.date >= start_date)
        if end_date:
            stmt = stmt.where(EconomicIndicator.date <= end_date)
            count_stmt = count_stmt.where(EconomicIndicator.date <= end_date)

        total = (await session.execute(count_stmt)).scalar_one()

        stmt = (
            stmt.order_by(EconomicIndicator.date.desc()).limit(limit).offset(offset)
        )
        rows = (await session.execute(stmt)).scalars().all()

        items = [EconomicIndicatorInfo.model_validate(row) for row in rows]

        body = MacroIndicatorResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            source=source,
            indicator_code=indicator_code,
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except HTTPException:
        raise
    except Exception:
        logger.exception("get_macro_indicators_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None
