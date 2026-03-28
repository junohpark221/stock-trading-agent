"""Decision Log API routes — query and aggregate agent decisions.

Endpoints:
    GET /api/decisions/stats          — 의사결정 통계 (집계)
    GET /api/decisions/{session_id}   — 세션별 전체 의사결정 체인
    GET /api/decisions                — 조건 검색 (symbol, stage, date range, pagination)
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.llm import DecisionLog
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


# ── Response Models ──────────────────────────────────────────────────────


class DecisionLogItem(BaseModel):
    """API 응답용 DecisionLog 직렬화 (llm_prompt/llm_response/data_snapshot 제외 — 대용량)."""

    model_config = ConfigDict(from_attributes=True)

    decision_id: UUID
    parent_id: UUID | None
    session_id: UUID
    stage: str
    agent_type: str | None
    symbol: str | None
    llm_provider: str | None
    llm_model: str | None
    llm_tokens_in: int | None
    llm_tokens_out: int | None
    llm_cost_usd: Decimal | None
    decision: str
    confidence: Decimal | None
    reasoning: str
    outcome: str | None
    outcome_pnl: Decimal | None
    created_at: datetime


class DecisionListResponse(BaseModel):
    items: list[DecisionLogItem]
    total: int
    limit: int
    offset: int


class DecisionStatsResponse(BaseModel):
    total_decisions: int
    by_stage: dict[str, int]
    by_agent_type: dict[str, int]
    by_symbol: dict[str, int]
    avg_confidence: Decimal | None
    total_llm_cost_usd: Decimal
    total_llm_calls: int
    date_from: str | None
    date_to: str | None


# ── GET /api/decisions/stats ─────────────────────────────────────────────


@router.get("/stats", response_model=DecisionStatsResponse)
async def get_decision_stats(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    symbol: str | None = Query(None),
    account_id: str = Query("default", description="계좌 ID"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """의사결정 통계 조회. 날짜/종목 필터링 지원."""
    try:
        # Base filter
        filters = [DecisionLog.account_id == account_id]
        if from_date:
            filters.append(DecisionLog.created_at >= from_date)
        if to_date:
            filters.append(DecisionLog.created_at <= to_date)
        if symbol:
            filters.append(DecisionLog.symbol == symbol)

        # Total count
        count_stmt = select(func.count()).select_from(DecisionLog)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = (await session.execute(count_stmt)).scalar_one()

        # Average confidence
        avg_stmt = select(func.avg(DecisionLog.confidence)).select_from(DecisionLog)
        for f in filters:
            avg_stmt = avg_stmt.where(f)
        avg_conf = (await session.execute(avg_stmt)).scalar_one()

        # Total LLM cost
        cost_stmt = select(func.coalesce(func.sum(DecisionLog.llm_cost_usd), 0)).select_from(
            DecisionLog
        )
        for f in filters:
            cost_stmt = cost_stmt.where(f)
        total_cost = (await session.execute(cost_stmt)).scalar_one()

        # Total LLM calls (rows with non-null llm_provider)
        calls_stmt = (
            select(func.count())
            .select_from(DecisionLog)
            .where(DecisionLog.llm_provider.is_not(None))
        )
        for f in filters:
            calls_stmt = calls_stmt.where(f)
        total_calls = (await session.execute(calls_stmt)).scalar_one()

        # Group by stage
        stage_stmt = select(
            DecisionLog.stage, func.count().label("cnt")
        ).group_by(DecisionLog.stage)
        for f in filters:
            stage_stmt = stage_stmt.where(f)
        stage_rows = (await session.execute(stage_stmt)).all()
        by_stage = {row.stage: row.cnt for row in stage_rows}

        # Group by agent_type
        agent_stmt = (
            select(DecisionLog.agent_type, func.count().label("cnt"))
            .where(DecisionLog.agent_type.is_not(None))
            .group_by(DecisionLog.agent_type)
        )
        for f in filters:
            agent_stmt = agent_stmt.where(f)
        agent_rows = (await session.execute(agent_stmt)).all()
        by_agent_type = {row.agent_type: row.cnt for row in agent_rows}

        # Group by symbol
        symbol_stmt = (
            select(DecisionLog.symbol, func.count().label("cnt"))
            .where(DecisionLog.symbol.is_not(None))
            .group_by(DecisionLog.symbol)
        )
        for f in filters:
            symbol_stmt = symbol_stmt.where(f)
        symbol_rows = (await session.execute(symbol_stmt)).all()
        by_symbol = {row.symbol: row.cnt for row in symbol_rows}

        body = DecisionStatsResponse(
            total_decisions=total,
            by_stage=by_stage,
            by_agent_type=by_agent_type,
            by_symbol=by_symbol,
            avg_confidence=Decimal(str(avg_conf)) if avg_conf is not None else None,
            total_llm_cost_usd=Decimal(str(total_cost)),
            total_llm_calls=total_calls,
            date_from=str(from_date) if from_date else None,
            date_to=str(to_date) if to_date else None,
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("get_decision_stats_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/decisions/{session_id} ──────────────────────────────────────


@router.get("/{session_id}", response_model=list[DecisionLogItem])
async def get_session_decisions(
    session_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """세션별 전체 의사결정 체인 조회 (created_at 순)."""
    try:
        stmt = (
            select(DecisionLog)
            .where(DecisionLog.session_id == session_id)
            .order_by(DecisionLog.created_at)
        )
        rows = (await session.execute(stmt)).scalars().all()

        items = [DecisionLogItem.model_validate(row) for row in rows]
        return JSONResponse(content=[item.model_dump(mode="json") for item in items])

    except Exception:
        logger.exception("get_session_decisions_failed", session_id=str(session_id))
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/decisions ───────────────────────────────────────────────────


@router.get("", response_model=DecisionListResponse)
async def list_decisions(
    symbol: str | None = Query(None),
    stage: str | None = Query(None),
    agent_type: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    account_id: str = Query("default", description="계좌 ID"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """조건 검색 (symbol, stage, agent_type, date range, pagination)."""
    try:
        stmt = select(DecisionLog)
        count_stmt = select(func.count()).select_from(DecisionLog)

        stmt = stmt.where(DecisionLog.account_id == account_id)
        count_stmt = count_stmt.where(DecisionLog.account_id == account_id)

        if symbol:
            stmt = stmt.where(DecisionLog.symbol == symbol)
            count_stmt = count_stmt.where(DecisionLog.symbol == symbol)
        if stage:
            stmt = stmt.where(DecisionLog.stage == stage)
            count_stmt = count_stmt.where(DecisionLog.stage == stage)
        if agent_type:
            stmt = stmt.where(DecisionLog.agent_type == agent_type)
            count_stmt = count_stmt.where(DecisionLog.agent_type == agent_type)
        if from_date:
            stmt = stmt.where(DecisionLog.created_at >= from_date)
            count_stmt = count_stmt.where(DecisionLog.created_at >= from_date)
        if to_date:
            stmt = stmt.where(DecisionLog.created_at <= to_date)
            count_stmt = count_stmt.where(DecisionLog.created_at <= to_date)

        total = (await session.execute(count_stmt)).scalar_one()

        stmt = stmt.order_by(DecisionLog.created_at.desc()).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

        items = [DecisionLogItem.model_validate(row) for row in rows]

        body = DecisionListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("list_decisions_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None
