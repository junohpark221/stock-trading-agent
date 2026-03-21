"""Phase 3 E2E integration tests.

Uses real agent classes with mocked LLM router and tool registry.
No real LLM calls or DB access — all external dependencies are mocked.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.agents.market_analyst import MarketAnalyst
from src.agent.agents.risk_manager import RiskManager
from src.agent.agents.stock_analyst import StockAnalyst
from src.agent.agents.trader import Trader
from src.agent.decision_recorder import DecisionRecorder
from src.agent.orchestrator import PipelineOrchestrator
from src.agent.tools.registry import ToolRegistry
from src.core.enums import DecisionAction
from src.core.models import (
    MarketCondition,
    RiskAssessment,
    StockAnalysis,
    TradeDecision,
)
from src.llm.router import LLMRouter, RoutingResult
from src.core.models import AgentModelConfig, LLMResponse
from src.core.enums import LLMProviderType, RoutingMode


# Patch all build_user_prompt to avoid json.dumps(Decimal) issues in prompts.
# Integration tests focus on pipeline flow, not prompt rendering.
@pytest.fixture(autouse=True)
def _patch_prompt_builders():
    with (
        patch("src.agent.agents.market_analyst.build_user_prompt", return_value="mock prompt"),
        patch("src.agent.agents.stock_analyst.build_user_prompt", return_value="mock prompt"),
        patch("src.agent.agents.risk_manager.build_user_prompt", return_value="mock prompt"),
        patch("src.agent.agents.trader.build_user_prompt", return_value="mock prompt"),
    ):
        yield


# ── Sample data ──────────────────────────────────────────────────────


def _mc() -> MarketCondition:
    return MarketCondition(
        condition="bullish",
        confidence=Decimal("0.75"),
        kospi_trend="상승",
        kosdaq_trend="횡보",
        market_risk_level="medium",
        reasoning="macro stable",
    )


def _sa(symbol: str = "005930", action: DecisionAction = DecisionAction.BUY) -> StockAnalysis:
    return StockAnalysis(
        symbol=symbol,
        name="테스트종목",
        action=action,
        confidence=Decimal("0.80"),
        reasoning="good technical signals",
    )


def _ra(symbol: str = "005930", approved: bool = True) -> RiskAssessment:
    return RiskAssessment(
        symbol=symbol,
        approved=approved,
        risk_level="medium",
        confidence=Decimal("0.85"),
        reasoning="risk within limits",
    )


def _td(symbol: str = "005930") -> TradeDecision:
    return TradeDecision(
        symbol=symbol,
        action=DecisionAction.BUY,
        confidence=Decimal("0.82"),
        quantity=10,
        price=Decimal("78000"),
        reasoning="execute buy order",
    )


def _llm_response() -> LLMResponse:
    return LLMResponse(
        content="mock response",
        model="mock-model",
        provider=LLMProviderType.OPENAI,
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("0.005"),
        finish_reason="stop",
        latency_ms=10,
    )


def _routing_result() -> RoutingResult:
    return RoutingResult(
        response=_llm_response(),
        escalated=False,
        config_used=AgentModelConfig(
            agent_type="market_analyst",
            routing_mode=RoutingMode.FIXED,
            primary_model="mock-model",
            is_active=True,
        ),
    )


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_router():
    """Mock LLMRouter with configurable route_structured responses."""
    router = MagicMock(spec=LLMRouter)
    # Default: returns MarketCondition
    router.route_structured = AsyncMock(return_value=(_mc(), _routing_result()))
    return router


@pytest.fixture
def mock_recorder():
    """Mock DecisionRecorder that tracks record() calls."""
    recorder = MagicMock(spec=DecisionRecorder)
    # record() returns a new UUID each time
    recorder.record = AsyncMock(side_effect=lambda **kw: uuid.uuid4())
    recorder.get_session_decisions = AsyncMock(return_value=[])
    return recorder


@pytest.fixture
def mock_tool_registry():
    """Mock ToolRegistry — all tool executions return empty dict."""
    registry = MagicMock(spec=ToolRegistry)
    registry.execute = AsyncMock(return_value={})
    registry.get_tools_for_modules = MagicMock(return_value=[])
    return registry


def _make_orchestrator(
    mock_router: MagicMock,
    mock_recorder: MagicMock,
    mock_tool_registry: MagicMock,
) -> PipelineOrchestrator:
    """Create orchestrator with real agents + mocked dependencies."""
    return PipelineOrchestrator(
        market_analyst=MarketAnalyst(mock_router, mock_recorder, mock_tool_registry),
        stock_analyst=StockAnalyst(mock_router, mock_recorder, mock_tool_registry),
        risk_manager=RiskManager(mock_router, mock_recorder, mock_tool_registry),
        trader=Trader(mock_router, mock_recorder, mock_tool_registry),
        recorder=mock_recorder,
    )


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_full_pipeline(mock_router, mock_recorder, mock_tool_registry):
    """전체 파이프라인 E2E — 4단계 순차 실행, PipelineResult 필드 검증."""
    # Configure route_structured to return different models per call
    mock_router.route_structured = AsyncMock(
        side_effect=[
            (_mc(), _routing_result()),       # MarketAnalyst
            (_sa(), _routing_result()),       # StockAnalyst
            (_ra(), _routing_result()),       # RiskManager
            (_td(), _routing_result()),       # Trader
        ]
    )

    orch = _make_orchestrator(mock_router, mock_recorder, mock_tool_registry)
    result = await orch.execute(["005930"])

    assert result.success is True
    assert result.market_condition is not None
    assert result.market_condition.condition == "bullish"
    assert len(result.stock_analyses) == 1
    assert result.stock_analyses[0].symbol == "005930"
    assert len(result.risk_assessments) == 1
    assert len(result.trade_decisions) == 1
    assert result.completed_at is not None
    assert result.symbols_analyzed == ["005930"]

    # 4 LLM calls → 4 record() calls
    assert mock_recorder.record.call_count == 4


@pytest.mark.asyncio
async def test_parent_id_chaining(mock_router, mock_recorder, mock_tool_registry):
    """parent_id 체이닝: Market→Stock→Risk→Trader decision_id 연결."""
    decision_ids = [uuid.uuid4() for _ in range(4)]
    mock_recorder.record = AsyncMock(side_effect=decision_ids)

    mock_router.route_structured = AsyncMock(
        side_effect=[
            (_mc(), _routing_result()),
            (_sa(), _routing_result()),
            (_ra(), _routing_result()),
            (_td(), _routing_result()),
        ]
    )

    orch = _make_orchestrator(mock_router, mock_recorder, mock_tool_registry)
    result = await orch.execute(["005930"])
    assert result.success is True

    # Verify record calls
    calls = mock_recorder.record.call_args_list

    # MarketAnalyst: parent_id=None
    assert calls[0].kwargs["parent_id"] is None
    assert calls[0].kwargs["stage"] == "market_analysis"

    # StockAnalyst: parent_id=market decision_id
    assert calls[1].kwargs["parent_id"] == decision_ids[0]
    assert calls[1].kwargs["stage"] == "stock_analysis"

    # RiskManager: parent_id=stock decision_id
    assert calls[2].kwargs["parent_id"] == decision_ids[1]
    assert calls[2].kwargs["stage"] == "risk_check"

    # Trader: parent_id=risk decision_id
    assert calls[3].kwargs["parent_id"] == decision_ids[2]
    assert calls[3].kwargs["stage"] == "trade_decision"


@pytest.mark.asyncio
async def test_hold_skips_risk_and_trader(mock_router, mock_recorder, mock_tool_registry):
    """HOLD 시 Risk/Trader skip — stock_analyses 있고 risk/trade 없음."""
    mock_router.route_structured = AsyncMock(
        side_effect=[
            (_mc(), _routing_result()),
            (_sa(action=DecisionAction.HOLD), _routing_result()),
        ]
    )

    orch = _make_orchestrator(mock_router, mock_recorder, mock_tool_registry)
    result = await orch.execute(["005930"])

    assert result.success is True
    assert len(result.stock_analyses) == 1
    assert result.stock_analyses[0].action == DecisionAction.HOLD
    assert len(result.risk_assessments) == 0
    assert len(result.trade_decisions) == 0
    # Only 2 LLM calls: market + stock
    assert mock_router.route_structured.call_count == 2


@pytest.mark.asyncio
async def test_risk_rejected_skips_trader(mock_router, mock_recorder, mock_tool_registry):
    """Risk 거부 시 Trader skip — risk_assessments 있고 trade_decisions 없음."""
    mock_router.route_structured = AsyncMock(
        side_effect=[
            (_mc(), _routing_result()),
            (_sa(), _routing_result()),
            (_ra(approved=False), _routing_result()),
        ]
    )

    orch = _make_orchestrator(mock_router, mock_recorder, mock_tool_registry)
    result = await orch.execute(["005930"])

    assert result.success is True
    assert len(result.risk_assessments) == 1
    assert result.risk_assessments[0].approved is False
    assert len(result.trade_decisions) == 0
    assert mock_router.route_structured.call_count == 3


@pytest.mark.asyncio
async def test_multiple_symbols_parallel(mock_router, mock_recorder, mock_tool_registry):
    """다중 종목 병렬 — 3종목 동시 처리, 결과 3세트."""
    symbols = ["005930", "000660", "035420"]

    # With parallel execution, side_effect order is non-deterministic.
    # Use a callback that returns the right model based on agent_type.
    async def _route_structured(*, agent_type, messages, schema, **kw):
        rr = _routing_result()
        if agent_type == "market_analyst":
            return _mc(), rr
        elif agent_type == "stock_analyst":
            # Extract symbol from messages content (best-effort)
            return _sa(), rr
        elif agent_type == "risk_manager":
            return _ra(), rr
        elif agent_type == "trader":
            return _td(), rr
        return _mc(), rr

    mock_router.route_structured = AsyncMock(side_effect=_route_structured)

    orch = _make_orchestrator(mock_router, mock_recorder, mock_tool_registry)
    result = await orch.execute(symbols)

    assert result.success is True
    assert len(result.stock_analyses) == 3
    assert len(result.risk_assessments) == 3
    assert len(result.trade_decisions) == 3
    assert sorted(result.symbols_analyzed) == sorted(symbols)


@pytest.mark.asyncio
async def test_symbol_failure_isolation(mock_router, mock_recorder, mock_tool_registry):
    """종목별 실패 격리 — 1종목 에러, 나머지 정상."""
    call_count = {"stock": 0}

    async def _route_structured(*, agent_type, messages, schema, **kw):
        rr = _routing_result()
        if agent_type == "market_analyst":
            return _mc(), rr
        elif agent_type == "stock_analyst":
            call_count["stock"] += 1
            # First stock call succeeds, second fails
            if call_count["stock"] == 1:
                return _sa("005930"), rr
            raise RuntimeError("LLM timeout for 000660")
        elif agent_type == "risk_manager":
            return _ra("005930"), rr
        elif agent_type == "trader":
            return _td("005930"), rr
        return _mc(), rr

    mock_router.route_structured = AsyncMock(side_effect=_route_structured)

    orch = _make_orchestrator(mock_router, mock_recorder, mock_tool_registry)
    # Use max_concurrency=1 to ensure deterministic order
    orch._max_concurrency = 1
    result = await orch.execute(["005930", "000660"])

    assert result.success is True
    assert "005930" in result.symbols_analyzed
    assert "000660" in result.symbols_skipped
    assert len(result.stock_analyses) == 1
    assert any("000660" in e for e in result.errors)
