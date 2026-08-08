"""Pipeline API routes — execute analysis pipeline and query results.

Endpoints:
    POST /api/pipeline/run                  — 파이프라인 실행
    GET  /api/pipeline/result/{session_id}  — 결과 조회 (decision_log 기반)
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.agent.agents.market_analyst import MarketAnalyst
from src.agent.agents.risk_manager import RiskManager
from src.agent.agents.stock_analyst import StockAnalyst
from src.agent.agents.trader import Trader
from src.agent.decision_recorder import DecisionRecorder
from src.agent.orchestrator import PipelineOrchestrator
from src.agent.tools.context import ToolContext
from src.agent.tools.registry import ToolRegistry
from src.api.routes.decisions import DecisionLogItem
from src.config import get_settings
from src.data.cache import get_cache
from src.db.session import get_session_factory
from src.llm.cost_tracker import CostTracker
from src.llm.router import LLMRouter

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


# ── Request/Response Models ──────────────────────────────────────────────


class PipelineRunRequest(BaseModel):
    symbols: list[str] = Field(
        ..., min_length=1, max_length=20, description="분석 대상 종목 코드"
    )
    session_id: UUID | None = Field(
        None, description="세션 ID (미지정 시 자동 생성)"
    )
    account_id: str = Field("default", description="계좌 ID")
    investment_prompt: str = Field("", description="투자 철학 프롬프트")
    risk_tolerance: str = Field("moderate", description="리스크 허용 수준 (conservative/moderate/aggressive)")


# ── Orchestrator Factory ─────────────────────────────────────────────────


def _build_orchestrator() -> PipelineOrchestrator:
    """파이프라인 실행에 필요한 전체 의존성 트리를 조립."""
    settings = get_settings()
    session_factory = get_session_factory()
    cache = get_cache()

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

    # PRJ-04 §8: 보유 컨텍스트 주입용 (조회 전용, 무상태 래퍼).
    from src.strategy.position_manager import PositionManager

    return PipelineOrchestrator(
        market_analyst=MarketAnalyst(llm_router, recorder, tool_registry),
        stock_analyst=StockAnalyst(llm_router, recorder, tool_registry),
        risk_manager=RiskManager(llm_router, recorder, tool_registry),
        trader=Trader(llm_router, recorder, tool_registry),
        recorder=recorder,
        position_manager=PositionManager(session_factory),
    )


# ── POST /api/pipeline/run ──────────────────────────────────────────────


@router.post("/run")
async def run_pipeline(req: PipelineRunRequest) -> JSONResponse:
    """파이프라인 실행. symbols 리스트를 받아 전체 분석 파이프라인을 수행."""
    try:
        orchestrator = _build_orchestrator()
        result = await orchestrator.execute(
            req.symbols,
            session_id=req.session_id,
            investment_prompt=req.investment_prompt or None,
            risk_tolerance=req.risk_tolerance,
            account_id=req.account_id,
        )
        return JSONResponse(content=result.model_dump(mode="json"))

    except Exception:
        logger.exception("run_pipeline_failed", symbols=req.symbols)
        raise HTTPException(status_code=500, detail="Internal server error") from None


# ── GET /api/pipeline/result/{session_id} ────────────────────────────────


@router.get("/result/{session_id}")
async def get_pipeline_result(session_id: UUID) -> JSONResponse:
    """세션별 파이프라인 결과 조회 (decision_log 기반)."""
    try:
        recorder = DecisionRecorder(get_session_factory())
        decisions = await recorder.get_session_decisions(session_id)

        items = [DecisionLogItem.model_validate(d) for d in decisions]
        return JSONResponse(
            content=[item.model_dump(mode="json") for item in items]
        )

    except Exception:
        logger.exception(
            "get_pipeline_result_failed", session_id=str(session_id)
        )
        raise HTTPException(status_code=500, detail="Internal server error") from None
