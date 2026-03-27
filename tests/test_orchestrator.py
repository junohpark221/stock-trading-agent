"""PipelineOrchestrator 단위 테스트 (9개).

4개 에이전트를 mock으로 주입하여 오케스트레이션 로직을 검증한다.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.orchestrator import PipelineOrchestrator
from src.core.enums import DecisionAction
from src.core.models import (
    MarketCondition,
    RiskAssessment,
    StockAnalysis,
    TradeDecision,
)

# ── 샘플 데이터 ─────────────────────────────────────────────

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
        name="삼성전자",
        action=action,
        confidence=Decimal("0.8"),
        reasoning="good signals",
    )


def _ra(symbol: str = "005930", approved: bool = True) -> RiskAssessment:
    return RiskAssessment(
        symbol=symbol,
        approved=approved,
        risk_level="medium",
        confidence=Decimal("0.85"),
        reasoning="risk ok",
    )


def _td(symbol: str = "005930") -> TradeDecision:
    return TradeDecision(
        symbol=symbol,
        action=DecisionAction.BUY,
        confidence=Decimal("0.82"),
        quantity=10,
        price=Decimal("78000"),
        reasoning="execute buy",
    )


def _decision_log_mock(cost: Decimal | None = Decimal("0.005")) -> MagicMock:
    """DecisionLog-like mock with llm_cost_usd."""
    m = MagicMock()
    m.llm_cost_usd = cost
    return m


# ── 픽스처 ──────────────────────────────────────────────────

@pytest.fixture
def market_decision_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def stock_decision_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def risk_decision_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def trade_decision_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def mock_market_analyst(market_decision_id: uuid.UUID) -> MagicMock:
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=(_mc(), market_decision_id))
    return agent


@pytest.fixture
def mock_stock_analyst(stock_decision_id: uuid.UUID) -> MagicMock:
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=(_sa(), stock_decision_id))
    return agent


@pytest.fixture
def mock_risk_manager(risk_decision_id: uuid.UUID) -> MagicMock:
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=(_ra(), risk_decision_id))
    return agent


@pytest.fixture
def mock_trader(trade_decision_id: uuid.UUID) -> MagicMock:
    agent = MagicMock()
    agent.analyze = AsyncMock(return_value=(_td(), trade_decision_id))
    return agent


@pytest.fixture
def mock_recorder() -> MagicMock:
    recorder = MagicMock()
    recorder.get_session_decisions = AsyncMock(
        return_value=[
            _decision_log_mock(Decimal("0.005")),
            _decision_log_mock(Decimal("0.003")),
            _decision_log_mock(Decimal("0.010")),
            _decision_log_mock(Decimal("0.002")),
        ]
    )
    return recorder


@pytest.fixture
def orchestrator(
    mock_market_analyst: MagicMock,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
    mock_recorder: MagicMock,
) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        market_analyst=mock_market_analyst,
        stock_analyst=mock_stock_analyst,
        risk_manager=mock_risk_manager,
        trader=mock_trader,
        recorder=mock_recorder,
        max_concurrency=5,
    )


# ── 테스트 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_single_symbol(
    orchestrator: PipelineOrchestrator,
    mock_market_analyst: MagicMock,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
) -> None:
    """1 종목 전체 파이프라인 정상 실행."""
    result = await orchestrator.execute(["005930"])

    assert result.success is True
    assert result.market_condition is not None
    assert len(result.stock_analyses) == 1
    assert len(result.risk_assessments) == 1
    assert len(result.trade_decisions) == 1
    assert result.symbols_analyzed == ["005930"]
    assert result.symbols_skipped == []
    assert result.completed_at is not None

    mock_market_analyst.analyze.assert_awaited_once()
    mock_stock_analyst.analyze.assert_awaited_once()
    mock_risk_manager.analyze.assert_awaited_once()
    mock_trader.analyze.assert_awaited_once()


@pytest.mark.asyncio
async def test_multiple_symbols(
    orchestrator: PipelineOrchestrator,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
    stock_decision_id: uuid.UUID,
    risk_decision_id: uuid.UUID,
    trade_decision_id: uuid.UUID,
) -> None:
    """3개 종목 병렬 실행."""
    symbols = ["005930", "000660", "035420"]
    # 각 종목별 다른 결과 반환
    mock_stock_analyst.analyze = AsyncMock(
        side_effect=[
            (_sa("005930"), stock_decision_id),
            (_sa("000660"), stock_decision_id),
            (_sa("035420"), stock_decision_id),
        ]
    )
    mock_risk_manager.analyze = AsyncMock(
        side_effect=[
            (_ra("005930"), risk_decision_id),
            (_ra("000660"), risk_decision_id),
            (_ra("035420"), risk_decision_id),
        ]
    )
    mock_trader.analyze = AsyncMock(
        side_effect=[
            (_td("005930"), trade_decision_id),
            (_td("000660"), trade_decision_id),
            (_td("035420"), trade_decision_id),
        ]
    )

    result = await orchestrator.execute(symbols)

    assert result.success is True
    assert len(result.stock_analyses) == 3
    assert len(result.risk_assessments) == 3
    assert len(result.trade_decisions) == 3
    assert sorted(result.symbols_analyzed) == sorted(symbols)


@pytest.mark.asyncio
async def test_market_analyst_failure(
    orchestrator: PipelineOrchestrator,
    mock_market_analyst: MagicMock,
    mock_stock_analyst: MagicMock,
) -> None:
    """MarketAnalyst 실패 → 전체 중단, success=False."""
    mock_market_analyst.analyze = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    result = await orchestrator.execute(["005930"])

    assert result.success is False
    assert result.market_condition is None
    assert len(result.stock_analyses) == 0
    assert len(result.errors) == 1
    assert "Market analysis failed" in result.errors[0]
    mock_stock_analyst.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_individual_symbol_failure(
    orchestrator: PipelineOrchestrator,
    mock_stock_analyst: MagicMock,
    stock_decision_id: uuid.UUID,
) -> None:
    """2개 중 1개 종목 실패 → 성공 1개 + 실패 1개 skip."""
    mock_stock_analyst.analyze = AsyncMock(
        side_effect=[
            (_sa("005930"), stock_decision_id),
            RuntimeError("analysis error"),
        ]
    )

    result = await orchestrator.execute(["005930", "000660"])

    assert result.success is True
    assert "005930" in result.symbols_analyzed
    assert "000660" in result.symbols_skipped
    assert len(result.stock_analyses) == 1
    assert any("000660" in e for e in result.errors)


@pytest.mark.asyncio
async def test_hold_skips_risk_and_trade(
    orchestrator: PipelineOrchestrator,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
    stock_decision_id: uuid.UUID,
) -> None:
    """action=HOLD → RiskManager, Trader 미호출."""
    mock_stock_analyst.analyze = AsyncMock(
        return_value=(_sa(action=DecisionAction.HOLD), stock_decision_id)
    )

    result = await orchestrator.execute(["005930"])

    assert result.success is True
    assert result.symbols_analyzed == ["005930"]
    assert len(result.stock_analyses) == 1
    assert result.stock_analyses[0].action == DecisionAction.HOLD
    assert len(result.risk_assessments) == 0
    assert len(result.trade_decisions) == 0
    mock_risk_manager.analyze.assert_not_awaited()
    mock_trader.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_risk_rejected_skips_trade(
    orchestrator: PipelineOrchestrator,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
    risk_decision_id: uuid.UUID,
) -> None:
    """approved=False → Trader 미호출."""
    mock_risk_manager.analyze = AsyncMock(
        return_value=(_ra(approved=False), risk_decision_id)
    )

    result = await orchestrator.execute(["005930"])

    assert result.success is True
    assert len(result.stock_analyses) == 1
    assert len(result.risk_assessments) == 1
    assert result.risk_assessments[0].approved is False
    assert len(result.trade_decisions) == 0
    mock_trader.analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_cost_aggregation(
    orchestrator: PipelineOrchestrator,
    mock_recorder: MagicMock,
) -> None:
    """비용 집계: total_llm_cost_usd, total_llm_calls 정확성."""
    result = await orchestrator.execute(["005930"])

    assert result.total_llm_cost_usd == Decimal("0.020")
    assert result.total_llm_calls == 4
    mock_recorder.get_session_decisions.assert_awaited_once()


@pytest.mark.asyncio
async def test_parent_id_chaining(
    orchestrator: PipelineOrchestrator,
    mock_market_analyst: MagicMock,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
    market_decision_id: uuid.UUID,
    stock_decision_id: uuid.UUID,
    risk_decision_id: uuid.UUID,
) -> None:
    """각 단계 analyze() 호출 시 올바른 parent_id 전달."""
    result = await orchestrator.execute(["005930"])
    assert result.success is True

    # MarketAnalyst: parent_id=None
    ma_call = mock_market_analyst.analyze.call_args
    assert ma_call.kwargs["parent_id"] is None

    # StockAnalyst: parent_id=market_decision_id
    sa_call = mock_stock_analyst.analyze.call_args
    assert sa_call.kwargs["parent_id"] == market_decision_id

    # RiskManager: parent_id=stock_decision_id
    rm_call = mock_risk_manager.analyze.call_args
    assert rm_call.kwargs["parent_id"] == stock_decision_id

    # Trader: parent_id=risk_decision_id
    tr_call = mock_trader.analyze.call_args
    assert tr_call.kwargs["parent_id"] == risk_decision_id


@pytest.mark.asyncio
async def test_session_id_auto_generation(
    orchestrator: PipelineOrchestrator,
) -> None:
    """session_id None → UUID 자동 생성, 명시 시 그대로 사용."""
    # 자동 생성
    result_auto = await orchestrator.execute(["005930"])
    assert result_auto.session_id is not None

    # 명시적 전달
    explicit_id = uuid.uuid4()
    result_explicit = await orchestrator.execute(["005930"], session_id=explicit_id)
    assert result_explicit.session_id == explicit_id

    # 둘이 다름
    assert result_auto.session_id != result_explicit.session_id


# ── 투자 철학 프롬프트 / account_id 전파 테스트 ────────────────


@pytest.mark.asyncio
async def test_investment_prompt_passed_to_symbol_agents(
    orchestrator: PipelineOrchestrator,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
) -> None:
    """execute(investment_prompt=...) → Stock/Risk/Trader에 전달."""
    prompt = "모멘텀 투자 전략으로 운용"
    await orchestrator.execute(["005930"], investment_prompt=prompt)

    for agent in [mock_stock_analyst, mock_risk_manager, mock_trader]:
        call_kwargs = agent.analyze.call_args.kwargs
        assert call_kwargs["investment_prompt"] == prompt


@pytest.mark.asyncio
async def test_market_analyst_no_investment_prompt(
    orchestrator: PipelineOrchestrator,
    mock_market_analyst: MagicMock,
) -> None:
    """MarketAnalyst에는 investment_prompt 미전달."""
    await orchestrator.execute(["005930"], investment_prompt="가치투자")

    call_kwargs = mock_market_analyst.analyze.call_args.kwargs
    assert "investment_prompt" not in call_kwargs


@pytest.mark.asyncio
async def test_account_id_passed_to_all_agents(
    orchestrator: PipelineOrchestrator,
    mock_market_analyst: MagicMock,
    mock_stock_analyst: MagicMock,
    mock_risk_manager: MagicMock,
    mock_trader: MagicMock,
) -> None:
    """execute(account_id=...) → 4개 에이전트 모두 account_id 전달."""
    await orchestrator.execute(["005930"], account_id="acct-1")

    for agent in [mock_market_analyst, mock_stock_analyst, mock_risk_manager, mock_trader]:
        call_kwargs = agent.analyze.call_args.kwargs
        assert call_kwargs["account_id"] == "acct-1"


@pytest.mark.asyncio
async def test_account_id_set_on_pipeline_result(
    orchestrator: PipelineOrchestrator,
) -> None:
    """PipelineResult.account_id가 설정된다."""
    result = await orchestrator.execute(["005930"], account_id="acct-2")
    assert result.account_id == "acct-2"


@pytest.mark.asyncio
async def test_default_params_backward_compatible(
    orchestrator: PipelineOrchestrator,
    mock_market_analyst: MagicMock,
    mock_stock_analyst: MagicMock,
) -> None:
    """새 파라미터 없이 호출 시 기존 동작 동일."""
    result = await orchestrator.execute(["005930"])

    assert result.success is True
    assert result.account_id == "default"
    # MarketAnalyst는 investment_prompt 미전달
    ma_kwargs = mock_market_analyst.analyze.call_args.kwargs
    assert "investment_prompt" not in ma_kwargs
    assert ma_kwargs["account_id"] == "default"
