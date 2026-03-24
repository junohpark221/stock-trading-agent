"""Unit tests for Strategy API and Portfolio API endpoints.

Uses httpx AsyncClient with FastAPI test transport.
All external dependencies (LLM, broker, DB) are mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.core.enums import DecisionAction, ExitReason, SignalAction, StrategyType
from src.core.models import (
    ExitSignal,
    PipelineResult,
    PortfolioState,
    Position,
    RiskCheckResult,
    Signal,
)
from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


def _pipeline_result(symbols: list[str] | None = None) -> PipelineResult:
    """Sample PipelineResult for strategy tests."""
    from src.core.models import MarketCondition, RiskAssessment, StockAnalysis, TradeDecision

    syms = symbols or ["005930"]
    return PipelineResult(
        session_id=uuid.uuid4(),
        started_at=datetime(2026, 3, 23, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 3, 23, 9, 1, tzinfo=UTC),
        market_condition=MarketCondition(
            condition="bullish",
            confidence=Decimal("0.75"),
            kospi_trend="상승",
            kosdaq_trend="횡보",
            market_risk_level="medium",
            reasoning="macro stable",
        ),
        stock_analyses=[
            StockAnalysis(
                symbol=s, name="테스트", action=DecisionAction.BUY,
                confidence=Decimal("0.80"), reasoning="good",
            )
            for s in syms
        ],
        risk_assessments=[],
        trade_decisions=[
            TradeDecision(
                symbol=s, action=DecisionAction.BUY, confidence=Decimal("0.82"),
                quantity=10, price=Decimal("78000"), reasoning="buy",
            )
            for s in syms
        ],
        symbols_requested=list(syms),
        symbols_analyzed=list(syms),
        success=True,
    )


def _signal(symbol: str = "005930") -> Signal:
    """Sample Signal."""
    from src.core.enums import AgentType

    return Signal(
        symbol=symbol,
        action=SignalAction.BUY,
        confidence=Decimal("0.80"),
        target_price=Decimal("84000"),
        stop_loss_price=Decimal("72000"),
        quantity=10,
        position_value_krw=Decimal("780000"),
        reasoning="position entry",
        source_agent=AgentType.TRADER,
        timestamp=datetime(2026, 3, 23, 9, 0),
    )


def _exit_signal(symbol: str = "005930") -> ExitSignal:
    """Sample ExitSignal."""
    return ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        urgency="immediate",
        current_price=Decimal("71000"),
        trigger_price=Decimal("72000"),
        unrealized_pnl_pct=Decimal("-8.97"),
        recommended_action=DecisionAction.SELL,
        reasoning="stop-loss triggered",
    )


def _mock_position_row(**kwargs) -> MagicMock:
    """Mock PositionRecord ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.symbol = kwargs.get("symbol", "005930")
    row.strategy_type = kwargs.get("strategy_type", "position")
    row.quantity = kwargs.get("quantity", 10)
    row.avg_cost = kwargs.get("avg_cost", Decimal("78000"))
    row.entry_price = kwargs.get("entry_price", Decimal("78000"))
    row.entry_date = kwargs.get("entry_date", date(2026, 3, 20))
    row.stop_loss_price = kwargs.get("stop_loss_price", Decimal("72000"))
    row.take_profit_price = kwargs.get("take_profit_price", Decimal("84000"))
    row.status = kwargs.get("status", "open")
    row.exit_price = kwargs.get("exit_price", None)
    row.exit_date = kwargs.get("exit_date", None)
    row.exit_reason = kwargs.get("exit_reason", None)
    row.realized_pnl = kwargs.get("realized_pnl", None)
    return row


def _mock_snapshot_row(**kwargs) -> MagicMock:
    """Mock PortfolioSnapshot ORM row."""
    row = MagicMock()
    row.snapshot_date = kwargs.get("snapshot_date", date(2026, 3, 22))
    row.total_value = kwargs.get("total_value", Decimal("10000000"))
    row.cash = kwargs.get("cash", Decimal("5000000"))
    row.invested = kwargs.get("invested", Decimal("5000000"))
    row.unrealized_pnl = kwargs.get("unrealized_pnl", Decimal("120000"))
    row.peak_value = kwargs.get("peak_value", Decimal("10200000"))
    row.drawdown_pct = kwargs.get("drawdown_pct", Decimal("1.96"))
    row.positions_count = kwargs.get("positions_count", 3)
    row.trade_count_daily = kwargs.get("trade_count_daily", 2)
    return row


def _portfolio_state() -> PortfolioState:
    """Sample PortfolioState."""
    return PortfolioState(
        total_value=Decimal("10000000"),
        cash=Decimal("5000000"),
        invested=Decimal("5000000"),
        unrealized_pnl=Decimal("120000"),
        daily_pnl=Decimal("50000"),
        daily_pnl_pct=Decimal("0.5"),
        drawdown_pct=Decimal("1.96"),
        peak_value=Decimal("10200000"),
        positions=[],
        sector_allocations={},
        daily_trade_count=2,
        timestamp=datetime(2026, 3, 23, 15, 30, tzinfo=UTC),
    )


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_session():
    """Mock AsyncSession for DI override."""
    return AsyncMock()


@pytest.fixture
def app(mock_db_session):
    """FastAPI app with dependency overrides."""
    main_mod.app.dependency_overrides[get_db_session] = lambda: mock_db_session
    yield main_mod.app
    main_mod.app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """httpx AsyncClient bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════
# Strategy API Tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("src.api.routes.strategy._build_strategy")
async def test_strategy_run_success(mock_build, client):
    """POST /api/strategy/run — 정상 실행."""
    mock_strategy = MagicMock()
    mock_strategy.strategy_type = StrategyType.POSITION
    mock_strategy._position_manager = MagicMock()
    mock_strategy._position_manager.create = AsyncMock(return_value=MagicMock())
    mock_strategy.analyze = AsyncMock(return_value=_pipeline_result(["005930"]))
    mock_strategy.generate_signals = AsyncMock(return_value=[_signal("005930")])
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_strategy, mock_broker)

    resp = await client.post(
        "/api/strategy/run",
        json={"strategy_type": "position", "symbols": ["005930"]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy_type"] == "position"
    assert data["symbols_analyzed"] == ["005930"]
    assert len(data["signals"]) == 1
    assert data["positions_created"] == 1


@pytest.mark.asyncio
async def test_strategy_run_empty_symbols(client):
    """POST /api/strategy/run — 빈 symbols → 422."""
    resp = await client.post(
        "/api/strategy/run",
        json={"strategy_type": "position", "symbols": []},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_strategy_run_invalid_type(client):
    """POST /api/strategy/run — 잘못된 strategy_type → 422."""
    resp = await client.post(
        "/api/strategy/run",
        json={"strategy_type": "invalid", "symbols": ["005930"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@patch("src.api.routes.strategy._build_strategy")
async def test_strategy_run_internal_error(mock_build, client):
    """POST /api/strategy/run — 내부 에러 → 500."""
    mock_build.side_effect = RuntimeError("boom")

    resp = await client.post(
        "/api/strategy/run",
        json={"strategy_type": "position", "symbols": ["005930"]},
    )
    assert resp.status_code == 500


@pytest.mark.asyncio
@patch("src.api.routes.strategy._build_strategy")
async def test_strategy_run_no_signals(mock_build, client):
    """POST /api/strategy/run — 시그널 없음 (조건 미충족)."""
    mock_strategy = MagicMock()
    mock_strategy.strategy_type = StrategyType.SWING
    mock_strategy.analyze = AsyncMock(return_value=_pipeline_result(["005930"]))
    mock_strategy.generate_signals = AsyncMock(return_value=[])
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_strategy, mock_broker)

    resp = await client.post(
        "/api/strategy/run",
        json={"strategy_type": "swing", "symbols": ["005930"]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["signals"] == []
    assert data["positions_created"] == 0


@pytest.mark.asyncio
async def test_signals_default(mock_db_session, client):
    """GET /api/strategy/signals — 기본 조회."""
    rows = [_mock_position_row(), _mock_position_row(id=2, symbol="000660")]

    # count query → total, then select query → rows
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 2

    mock_rows_result = MagicMock()
    mock_rows_result.scalars.return_value.all.return_value = rows

    mock_db_session.execute = AsyncMock(
        side_effect=[mock_count_result, mock_rows_result]
    )

    resp = await client.get("/api/strategy/signals")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_signals_with_filter(mock_db_session, client):
    """GET /api/strategy/signals — strategy_type 필터."""
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 1

    mock_rows_result = MagicMock()
    mock_rows_result.scalars.return_value.all.return_value = [_mock_position_row()]

    mock_db_session.execute = AsyncMock(
        side_effect=[mock_count_result, mock_rows_result]
    )

    resp = await client.get("/api/strategy/signals?strategy_type=position&symbol=005930")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1


@pytest.mark.asyncio
@patch("src.api.routes.strategy._build_strategy")
async def test_exit_check_with_signals(mock_build, client):
    """POST /api/strategy/exit-check — 청산 조건 있음."""
    mock_strategy = MagicMock()
    mock_strategy.check_all_exit_conditions = AsyncMock(
        return_value=[_exit_signal("005930")]
    )
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_strategy, mock_broker)

    resp = await client.post(
        "/api/strategy/exit-check",
        json={"strategy_type": "position"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["exit_signals"][0]["symbol"] == "005930"
    assert data["exit_signals"][0]["reason"] == "stop_loss"


@pytest.mark.asyncio
@patch("src.api.routes.strategy._build_strategy")
async def test_exit_check_no_signals(mock_build, client):
    """POST /api/strategy/exit-check — 청산 조건 없음."""
    mock_strategy = MagicMock()
    mock_strategy.check_all_exit_conditions = AsyncMock(return_value=[])
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_strategy, mock_broker)

    resp = await client.post("/api/strategy/exit-check", json={})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["exit_signals"] == []


@pytest.mark.asyncio
@patch("src.api.routes.strategy._build_strategy")
async def test_exit_check_all_strategies(mock_build, client):
    """POST /api/strategy/exit-check — 전체 전략 체크 (strategy_type=None)."""
    mock_strategy = MagicMock()
    mock_strategy.check_all_exit_conditions = AsyncMock(return_value=[])
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_strategy, mock_broker)

    resp = await client.post("/api/strategy/exit-check", json={})

    assert resp.status_code == 200
    # _build_strategy called twice (POSITION + SWING)
    assert mock_build.call_count == 2


# ═══════════════════════════════════════════════════════════════════════
# Portfolio API Tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("src.api.routes.portfolio._build_portfolio_service")
async def test_portfolio_state_success(mock_build, client):
    """GET /api/portfolio/state — 정상 반환."""
    mock_service = MagicMock()
    mock_service.get_current_state = AsyncMock(return_value=_portfolio_state())
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_service, mock_broker)

    resp = await client.get("/api/portfolio/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_value"] == "10000000"
    assert data["cash"] == "5000000"
    assert data["drawdown_pct"] == "1.96"


@pytest.mark.asyncio
@patch("src.api.routes.portfolio._build_portfolio_service")
async def test_portfolio_state_error(mock_build, client):
    """GET /api/portfolio/state — 에러 시 500."""
    mock_build.side_effect = RuntimeError("broker connection failed")

    resp = await client.get("/api/portfolio/state")
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_positions_default(mock_db_session, client):
    """GET /api/portfolio/positions — 기본 조회."""
    rows = [_mock_position_row(), _mock_position_row(id=2, symbol="000660")]

    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 2

    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = rows

    mock_db_session.execute = AsyncMock(side_effect=[mock_count, mock_rows])

    resp = await client.get("/api/portfolio/positions")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["symbol"] == "005930"


@pytest.mark.asyncio
async def test_positions_status_filter(mock_db_session, client):
    """GET /api/portfolio/positions — status 필터."""
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 1

    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = [
        _mock_position_row(status="closed", exit_price=Decimal("85000"), exit_reason="take_profit")
    ]

    mock_db_session.execute = AsyncMock(side_effect=[mock_count, mock_rows])

    resp = await client.get("/api/portfolio/positions?status=closed")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["status"] == "closed"


@pytest.mark.asyncio
async def test_positions_strategy_filter(mock_db_session, client):
    """GET /api/portfolio/positions — strategy_type 필터."""
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 1

    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = [
        _mock_position_row(strategy_type="swing")
    ]

    mock_db_session.execute = AsyncMock(side_effect=[mock_count, mock_rows])

    resp = await client.get("/api/portfolio/positions?strategy_type=swing")

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"][0]["strategy_type"] == "swing"


@pytest.mark.asyncio
async def test_snapshots_default(mock_db_session, client):
    """GET /api/portfolio/snapshots — 기본 조회."""
    rows = [_mock_snapshot_row(), _mock_snapshot_row(snapshot_date=date(2026, 3, 21))]

    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 2

    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = rows

    mock_db_session.execute = AsyncMock(side_effect=[mock_count, mock_rows])

    resp = await client.get("/api/portfolio/snapshots")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_snapshots_date_filter(mock_db_session, client):
    """GET /api/portfolio/snapshots — 날짜 필터."""
    mock_count = MagicMock()
    mock_count.scalar_one.return_value = 1

    mock_rows = MagicMock()
    mock_rows.scalars.return_value.all.return_value = [_mock_snapshot_row()]

    mock_db_session.execute = AsyncMock(side_effect=[mock_count, mock_rows])

    resp = await client.get(
        "/api/portfolio/snapshots?from_date=2026-03-20&to_date=2026-03-23"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1


@pytest.mark.asyncio
@patch("src.api.routes.portfolio._build_risk_manager")
async def test_risk_check_pass(mock_build, client):
    """POST /api/portfolio/risk-check — 통과."""
    mock_mgr = MagicMock()
    mock_mgr.check = AsyncMock(
        return_value=RiskCheckResult(
            passed=True,
            symbol="005930",
            violations=[],
            warnings=[],
            adjusted_quantity=10,
            adjusted_amount_krw=Decimal("780000"),
            max_allowed_quantity=10,
            reasoning="모든 리스크 규칙 통과",
        )
    )
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_mgr, mock_broker)

    resp = await client.post(
        "/api/portfolio/risk-check",
        json={
            "symbol": "005930",
            "quantity": 10,
            "price": "78000",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is True
    assert data["adjusted_quantity"] == 10
    assert data["violations"] == []


@pytest.mark.asyncio
@patch("src.api.routes.portfolio._build_risk_manager")
async def test_risk_check_violation(mock_build, client):
    """POST /api/portfolio/risk-check — 위반."""
    mock_mgr = MagicMock()
    mock_mgr.check = AsyncMock(
        return_value=RiskCheckResult(
            passed=False,
            symbol="005930",
            violations=["MAX_DRAWDOWN_PCT"],
            warnings=[],
            adjusted_quantity=0,
            adjusted_amount_krw=Decimal("0"),
            max_allowed_quantity=0,
            reasoning="최대 드로다운 초과로 전체 매매 중단",
        )
    )
    mock_broker = AsyncMock()
    mock_build.return_value = (mock_mgr, mock_broker)

    resp = await client.post(
        "/api/portfolio/risk-check",
        json={
            "symbol": "005930",
            "quantity": 10,
            "price": "78000",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["passed"] is False
    assert "MAX_DRAWDOWN_PCT" in data["violations"]
    assert data["adjusted_quantity"] == 0
