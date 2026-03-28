"""Strategy API routes — execute trading strategies and check exit conditions.

Endpoints:
    POST /api/strategy/run         — 전략 실행 (scan → analyze → signals → positions)
    GET  /api/strategy/signals     — 최근 생성된 시그널(포지션) 조회
    POST /api/strategy/exit-check  — 보유 포지션 청산 조건 체크
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.portfolio import PositionItem
from src.core.enums import StrategyType
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


# ── Request/Response Models ──────────────────────────────────────────────


class StrategyRunRequest(BaseModel):
    strategy_type: StrategyType
    symbols: list[str] = Field(
        ..., min_length=1, max_length=50, description="분석 대상 종목 코드"
    )
    account_id: str = Field("default", description="계좌 ID")
    investment_prompt: str = Field("", description="투자 철학 프롬프트")


class ExitCheckRequest(BaseModel):
    strategy_type: StrategyType | None = Field(
        None, description="전략 유형 (미지정 시 전체)"
    )


class StrategyRunResponse(BaseModel):
    strategy_type: str
    symbols_analyzed: list[str]
    signals: list[dict]
    positions_created: int
    session_id: str


# ── Strategy Factory ─────────────────────────────────────────────────────


async def _build_strategy(
    strategy_type: StrategyType,
    *,
    account_id: str = "default",
    investment_prompt: str = "",
    risk_overrides: dict[str, object] | None = None,
):
    """전략 실행에 필요한 전체 의존성 트리를 조립.

    pipeline.py의 _build_orchestrator() 패턴을 확장하여,
    PipelineOrchestrator + StrategyFactory를 통해 전략 인스턴스를 생성한다.
    broker도 함께 반환하여 호출자가 disconnect할 수 있도록 한다.
    """
    from src.agent.agents.market_analyst import MarketAnalyst
    from src.agent.agents.risk_manager import RiskManager
    from src.agent.agents.stock_analyst import StockAnalyst
    from src.agent.agents.trader import Trader
    from src.agent.decision_recorder import DecisionRecorder
    from src.agent.orchestrator import PipelineOrchestrator
    from src.agent.tools.context import ToolContext
    from src.agent.tools.registry import ToolRegistry
    from src.broker.kis.client import KISClient
    from src.config import get_settings
    from src.data.cache import get_cache
    from src.db.session import get_session_factory
    from src.llm.cost_tracker import CostTracker
    from src.llm.router import LLMRouter
    from src.strategy.registry import StrategyCommonDeps, StrategyFactory

    settings = get_settings()
    session_factory = get_session_factory()
    cache = get_cache()

    # Broker
    broker = KISClient(settings=settings, cache=cache)
    await broker.connect()

    # LLM 파이프라인
    cost_tracker = CostTracker(session_factory=session_factory, settings=settings)
    llm_router = LLMRouter(
        session_factory=session_factory,
        settings=settings,
        cost_tracker=cost_tracker,
        cache=cache,
    )
    recorder = DecisionRecorder(session_factory)

    tool_ctx = ToolContext(session_factory=session_factory, settings=settings)
    tool_registry = ToolRegistry(tool_ctx)

    orchestrator = PipelineOrchestrator(
        market_analyst=MarketAnalyst(llm_router, recorder, tool_registry),
        stock_analyst=StockAnalyst(llm_router, recorder, tool_registry),
        risk_manager=RiskManager(llm_router, recorder, tool_registry),
        trader=Trader(llm_router, recorder, tool_registry),
        recorder=recorder,
    )

    # StrategyFactory를 통한 전략 생성
    deps = StrategyCommonDeps(
        orchestrator=orchestrator,
        recorder=recorder,
        broker=broker,
        session_factory=session_factory,
        settings=settings,
        cache=cache,
    )

    try:
        strategy = StrategyFactory.create(
            strategy_type,
            deps,
            account_id=account_id,
            investment_prompt=investment_prompt,
            risk_overrides=risk_overrides,
        )
    except (KeyError, ValueError):
        await broker.disconnect()
        raise

    return strategy, broker


# ── POST /api/strategy/run ───────────────────────────────────────────────


@router.post("/run", response_model=StrategyRunResponse)
async def run_strategy(req: StrategyRunRequest) -> JSONResponse:
    """전략 실행. symbols 리스트를 받아 분석 → 시그널 생성 → 포지션 기록.

    generate_signals() 내부에서 AlgoRiskManager + PositionSizer가 이미 수행되므로
    별도의 리스크 체크/사이징 호출이 불필요하다.
    """
    broker = None
    try:
        strategy, broker = await _build_strategy(
            req.strategy_type,
            account_id=req.account_id,
            investment_prompt=req.investment_prompt,
        )

        # Phase 3 파이프라인에 분석 위임
        pipeline_result = await strategy.analyze(req.symbols)

        # 시그널 생성 (내부: 기술/펀더멘털 필터 + 사이징 + 리스크 체크)
        signals = await strategy.generate_signals(pipeline_result)

        # 통과된 시그널에 대해 포지션 생성 (PositionManager.create 직접 호출)
        positions_created = 0
        session_id = pipeline_result.session_id
        position_manager = strategy._position_manager

        # 전략별 트레일링/보유일 파라미터
        trailing_pct = None
        max_days = None
        if req.strategy_type == StrategyType.POSITION:
            from src.strategy.position_trading import PositionTradingStrategy
            max_days = PositionTradingStrategy.MAX_HOLDING_DAYS
        elif req.strategy_type == StrategyType.SWING:
            from src.strategy.swing_trading import SwingTradingStrategy
            trailing_pct = SwingTradingStrategy.TRAILING_TRAIL_PCT
            max_days = SwingTradingStrategy.MAX_HOLDING_DAYS

        for signal in signals:
            entry_price = signal.position_value_krw / signal.quantity if signal.quantity > 0 else signal.stop_loss_price

            await position_manager.create(
                symbol=signal.symbol,
                strategy_type=strategy.strategy_type.value,
                quantity=signal.quantity,
                entry_price=entry_price,
                stop_loss_price=signal.stop_loss_price or entry_price,
                take_profit_price=signal.target_price,
                trailing_stop_pct=trailing_pct,
                max_holding_days=max_days,
                entry_session_id=session_id,
            )
            positions_created += 1

        body = StrategyRunResponse(
            strategy_type=req.strategy_type.value,
            symbols_analyzed=req.symbols,
            signals=[s.model_dump(mode="json") for s in signals],
            positions_created=positions_created,
            session_id=str(session_id),
        )
        return JSONResponse(content=body.model_dump(mode="json"))

    except Exception:
        logger.exception("run_strategy_failed", strategy_type=req.strategy_type.value)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    finally:
        if broker:
            await broker.disconnect()


# ── GET /api/strategy/signals ────────────────────────────────────────────


@router.get("/signals")
async def list_signals(
    strategy_type: str | None = Query(None, description="position 또는 swing"),
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """최근 생성된 시그널(포지션) 목록 조회."""
    try:
        stmt = select(PositionRecord)
        count_stmt = select(func.count()).select_from(PositionRecord)

        if strategy_type:
            stmt = stmt.where(PositionRecord.strategy_type == strategy_type)
            count_stmt = count_stmt.where(PositionRecord.strategy_type == strategy_type)
        if symbol:
            stmt = stmt.where(PositionRecord.symbol == symbol)
            count_stmt = count_stmt.where(PositionRecord.symbol == symbol)

        total = (await session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(PositionRecord.entry_date.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()

        items = [PositionItem.model_validate(row) for row in rows]
        return JSONResponse(
            content={"items": [i.model_dump(mode="json") for i in items], "total": total}
        )

    except Exception:
        logger.exception("list_signals_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── POST /api/strategy/exit-check ────────────────────────────────────────


@router.post("/exit-check")
async def check_exit_conditions(req: ExitCheckRequest) -> JSONResponse:
    """보유 포지션의 청산 조건을 체크한다.

    strategy_type 지정 시 해당 전략만, 미지정 시 두 전략 모두 체크.
    Strategy.check_all_exit_conditions()는 내부적으로
    get_open_positions → 각 position에 대해 check_exit_conditions 호출.
    """
    brokers = []
    try:
        all_exit_signals = []

        types_to_check = (
            [req.strategy_type]
            if req.strategy_type
            else [StrategyType.POSITION, StrategyType.SWING]
        )
        for st in types_to_check:
            strategy, broker = await _build_strategy(st)
            brokers.append(broker)
            signals = await strategy.check_all_exit_conditions()
            all_exit_signals.extend(signals)

        return JSONResponse(
            content={
                "exit_signals": [s.model_dump(mode="json") for s in all_exit_signals],
                "total": len(all_exit_signals),
            }
        )

    except Exception:
        logger.exception("check_exit_conditions_failed")
        raise HTTPException(status_code=500, detail="Internal server error") from None
    finally:
        for b in brokers:
            await b.disconnect()
