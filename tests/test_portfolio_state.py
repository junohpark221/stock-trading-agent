"""Phase 4 Step 3: PortfolioStateService + Strategy ABC 단위 테스트.

Tests:
- PortfolioStateService: 포트폴리오 상태 집계, 섹터 배분, 스냅샷, peak value, 매매 건수
- Strategy ABC: abstract 제약, 분석 위임, 청산 확인, 포지션 CRUD
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import (
    AgentType,
    DecisionAction,
    ExitReason,
    PositionStatus,
    SignalAction,
    StrategyType,
)
from src.core.models import (
    AccountBalance,
    ExitSignal,
    PipelineResult,
    Position,
    PositionSizing,
    Signal,
)
from src.db.models.strategy import PositionRecord
from src.strategy.base import Strategy
from src.strategy.portfolio_state import PortfolioStateService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_balance(**overrides: object) -> AccountBalance:
    defaults = {
        "total_assets": Decimal("100000000"),
        "cash": Decimal("50000000"),
        "invested": Decimal("50000000"),
        "unrealized_pnl": Decimal("1000000"),
        "realized_pnl": Decimal("500000"),
        "daily_pnl": Decimal("200000"),
        "daily_pnl_pct": Decimal("0.20"),
        "positions_count": 2,
        "timestamp": datetime(2026, 3, 22, 9, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return AccountBalance(**defaults)  # type: ignore[arg-type]


def _make_position(symbol: str = "005930", **overrides: object) -> Position:
    defaults = {
        "symbol": symbol,
        "quantity": 100,
        "average_cost": Decimal("70000"),
        "current_price": Decimal("72000"),
        "market_value": Decimal("7200000"),
        "unrealized_pnl": Decimal("200000"),
        "unrealized_pnl_pct": Decimal("2.86"),
        "status": PositionStatus.OPEN,
        "entry_date": datetime(2026, 3, 20, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Position(**defaults)  # type: ignore[arg-type]


def _make_signal(symbol: str = "005930") -> Signal:
    return Signal(
        symbol=symbol,
        action=SignalAction.BUY,
        confidence=Decimal("0.85"),
        target_price=Decimal("80000"),
        stop_loss_price=Decimal("66000"),
        quantity=10,
        position_value_krw=Decimal("700000"),
        reasoning="테스트 시그널",
        source_agent=AgentType.TRADER,
        timestamp=datetime(2026, 3, 22, 9, 0, tzinfo=UTC),
    )


def _make_sizing(symbol: str = "005930") -> PositionSizing:
    return PositionSizing(
        symbol=symbol,
        entry_price=Decimal("70000"),
        stop_loss_price=Decimal("66000"),
        take_profit_price=Decimal("80000"),
        risk_per_share=Decimal("4000"),
        quantity=10,
        position_value_krw=Decimal("700000"),
        risk_amount_krw=Decimal("40000"),
        risk_pct_of_portfolio=Decimal("0.04"),
        position_pct_of_portfolio=Decimal("0.70"),
        risk_reward_ratio=Decimal("2.50"),
    )


def _mock_session_factory():
    """Mock async session factory returning (factory, session)."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _mock_scalar_result(value):
    """Mock execute() result that returns scalar()."""
    result = MagicMock()
    result.scalar.return_value = value
    return result


def _mock_rows_result(rows):
    """Mock execute() result that returns .all()."""
    result = MagicMock()
    result.all.return_value = rows
    return result


def _mock_scalars_result(items):
    """Mock execute() result for scalars().all()."""
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = items
    result.scalars.return_value = scalars_mock
    return result


def _mock_scalar_one_or_none_result(item):
    """Mock execute() result for scalar_one_or_none()."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = item
    return result


# ---------------------------------------------------------------------------
# _TestStrategy — Strategy ABC 테스트용 구현체
# ---------------------------------------------------------------------------


class _TestStrategy(Strategy):
    """테스트 전용 Strategy 구현체."""

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.POSITION

    async def scan_universe(self) -> list[str]:
        return ["005930", "000660"]

    async def generate_signals(self, pipeline_result: PipelineResult) -> list[Signal]:
        return []

    async def check_exit_conditions(
        self, position: PositionRecord
    ) -> ExitSignal | None:
        # 테스트에서 반환값을 제어하기 위해 _exit_map 사용
        return getattr(self, "_exit_map", {}).get(position.symbol)


def _make_test_strategy(
    orchestrator=None,
    session_factory=None,
    broker=None,
    **kwargs,
):
    """_TestStrategy 인스턴스를 편리하게 생성."""
    factory = session_factory or _mock_session_factory()[0]
    settings = MagicMock()
    settings.MAX_DRAWDOWN_PCT = 10.0
    return _TestStrategy(
        orchestrator=orchestrator or AsyncMock(),
        risk_manager=None,
        portfolio_service=AsyncMock(),
        broker=broker or AsyncMock(),
        recorder=AsyncMock(),
        position_manager=AsyncMock(),
        session_factory=factory,
        settings=settings,
        **kwargs,
    )


# ===========================================================================
# PortfolioStateService Tests
# ===========================================================================


class TestGetCurrentStateEmpty:
    """포지션 0건 — cash만 있는 빈 포트폴리오."""

    @pytest.mark.asyncio
    async def test_empty_portfolio(self):
        balance = _make_balance(
            total_assets=Decimal("10000000"),
            cash=Decimal("10000000"),
            invested=Decimal(0),
            positions_count=0,
        )
        broker = AsyncMock()
        broker.get_balance_and_positions = AsyncMock(return_value=(balance, []))

        factory, session = _mock_session_factory()
        # get_peak_value → no snapshots
        # get_daily_trade_count → 0
        session.execute = AsyncMock(
            side_effect=[
                _mock_scalar_result(None),  # peak_value
                _mock_scalar_result(0),  # daily_trade_count
            ]
        )

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)

        service = PortfolioStateService(broker, factory, cache)
        state = await service.get_current_state()

        assert state.total_value == Decimal("10000000")
        assert state.cash == Decimal("10000000")
        assert state.invested == Decimal(0)
        assert state.positions == []
        assert state.sector_allocations == {}
        assert state.drawdown_pct == Decimal(0)
        assert state.daily_trade_count == 0


class TestGetCurrentStateWithPositions:
    """포지션 2건 — 전체 필드 조립 확인."""

    @pytest.mark.asyncio
    async def test_with_positions(self):
        pos1 = _make_position("005930", market_value=Decimal("7200000"))
        pos2 = _make_position("000660", market_value=Decimal("5000000"))

        broker = AsyncMock()
        broker.get_balance_and_positions = AsyncMock(
            return_value=(_make_balance(), [pos1, pos2])
        )

        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            side_effect=[
                # get_sector_allocations → StockMaster query
                _mock_rows_result([
                    ("005930", "전기전자"),
                    ("000660", "전기전자"),
                ]),
                _mock_scalar_result(Decimal("95000000")),  # peak_value < current
                _mock_scalar_result(1),  # daily_trade_count
            ]
        )

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        service = PortfolioStateService(broker, factory, cache)
        state = await service.get_current_state()

        assert len(state.positions) == 2
        assert state.daily_trade_count == 1
        assert "전기전자" in state.sector_allocations
        # peak should be updated to current (100M > 95M)
        assert state.peak_value == Decimal("100000000")
        assert state.drawdown_pct == Decimal(0)
        # F-18: daily_pnl/daily_pnl_pct는 balance(실현손익 기준)를 그대로 패스스루.
        assert state.daily_pnl == Decimal("200000")
        assert state.daily_pnl_pct == Decimal("0.20")


class TestDrawdownCalculation:
    """고점 대비 낙폭 계산."""

    @pytest.mark.asyncio
    async def test_drawdown_from_peak(self):
        balance = _make_balance(total_assets=Decimal("100000000"))
        broker = AsyncMock()
        broker.get_balance_and_positions = AsyncMock(return_value=(balance, []))

        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            side_effect=[
                _mock_scalar_result(Decimal("110000000")),  # peak > current
                _mock_scalar_result(0),
            ]
        )

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)

        service = PortfolioStateService(broker, factory, cache)
        state = await service.get_current_state()

        # drawdown = (110M - 100M) / 110M * 100 ≈ 9.0909...
        expected = (Decimal("110000000") - Decimal("100000000")) / Decimal("110000000") * Decimal(100)
        assert state.drawdown_pct == expected
        assert state.peak_value == Decimal("110000000")

    @pytest.mark.asyncio
    async def test_new_peak_no_drawdown(self):
        """현재가 신고점이면 drawdown=0."""
        balance = _make_balance(total_assets=Decimal("120000000"))
        broker = AsyncMock()
        broker.get_balance_and_positions = AsyncMock(return_value=(balance, []))

        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            side_effect=[
                _mock_scalar_result(Decimal("110000000")),  # peak < current
                _mock_scalar_result(0),
            ]
        )

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)

        service = PortfolioStateService(broker, factory, cache)
        state = await service.get_current_state()

        assert state.drawdown_pct == Decimal(0)
        assert state.peak_value == Decimal("120000000")


class TestGetSectorAllocations:
    """섹터 배분 계산."""

    @pytest.mark.asyncio
    async def test_empty_positions(self):
        factory, _ = _mock_session_factory()
        cache = AsyncMock()
        service = PortfolioStateService(AsyncMock(), factory, cache)

        result = await service.get_sector_allocations([], Decimal("100000000"))
        assert result == {}

    @pytest.mark.asyncio
    async def test_zero_total_value(self):
        factory, _ = _mock_session_factory()
        cache = AsyncMock()
        service = PortfolioStateService(AsyncMock(), factory, cache)

        result = await service.get_sector_allocations(
            [_make_position()], Decimal(0)
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_with_sectors(self):
        pos1 = _make_position("005930", market_value=Decimal("60000000"))
        pos2 = _make_position("035420", market_value=Decimal("40000000"))

        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            return_value=_mock_rows_result([
                ("005930", "전기전자"),
                ("035420", "서비스업"),
            ])
        )

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        service = PortfolioStateService(AsyncMock(), factory, cache)
        result = await service.get_sector_allocations(
            [pos1, pos2], Decimal("100000000")
        )

        assert result["전기전자"] == Decimal(60)
        assert result["서비스업"] == Decimal(40)

    @pytest.mark.asyncio
    async def test_unknown_sector_grouped_as_etc(self):
        """StockMaster에 없는 종목은 '기타'로 분류."""
        pos = _make_position("999999", market_value=Decimal("10000000"))

        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_rows_result([]))

        cache = AsyncMock()
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        service = PortfolioStateService(AsyncMock(), factory, cache)
        result = await service.get_sector_allocations(
            [pos], Decimal("100000000")
        )

        assert "기타" in result
        assert result["기타"] == Decimal(10)


class TestSaveSnapshot:
    """일별 스냅샷 upsert."""

    @pytest.mark.asyncio
    async def test_save_calls_upsert(self):
        factory, session = _mock_session_factory()
        session.execute = AsyncMock()
        session.commit = AsyncMock()

        cache = AsyncMock()
        service = PortfolioStateService(AsyncMock(), factory, cache)

        from src.core.models import PortfolioState

        state = PortfolioState(
            total_value=Decimal("100000000"),
            cash=Decimal("50000000"),
            invested=Decimal("50000000"),
            unrealized_pnl=Decimal("1000000"),
            daily_pnl=Decimal("200000"),
            daily_pnl_pct=Decimal("0.20"),
            drawdown_pct=Decimal("5.00"),
            peak_value=Decimal("105000000"),
            positions=[],
            sector_allocations={"전기전자": Decimal("60.00")},
            daily_trade_count=2,
            timestamp=datetime(2026, 3, 22, 15, 0, tzinfo=UTC),
        )

        await service.save_snapshot(state)

        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()


class TestGetPeakValue:
    """역대 최고 자산 조회."""

    @pytest.mark.asyncio
    async def test_no_history_returns_zero(self):
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalar_result(None))

        service = PortfolioStateService(AsyncMock(), factory, AsyncMock())
        peak = await service.get_peak_value()

        assert peak == Decimal(0)

    @pytest.mark.asyncio
    async def test_returns_max_value(self):
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            return_value=_mock_scalar_result(Decimal("150000000"))
        )

        service = PortfolioStateService(AsyncMock(), factory, AsyncMock())
        peak = await service.get_peak_value()

        assert peak == Decimal("150000000")


class TestGetDailyTradeCount:
    """당일 매매 건수."""

    @pytest.mark.asyncio
    async def test_count_by_date(self):
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalar_result(3))

        service = PortfolioStateService(AsyncMock(), factory, AsyncMock())
        count = await service.get_daily_trade_count(date(2026, 3, 22))

        assert count == 3


# ===========================================================================
# Strategy ABC Tests
# ===========================================================================


class TestStrategyABC:
    """Strategy 추상 클래스 제약."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            Strategy(
                orchestrator=AsyncMock(),
                risk_manager=None,
                portfolio_service=AsyncMock(),
                broker=AsyncMock(),
                recorder=AsyncMock(),
                session_factory=AsyncMock(),
                settings=MagicMock(),
            )

    def test_concrete_strategy_type(self):
        strategy = _make_test_strategy()
        assert strategy.strategy_type == StrategyType.POSITION


class TestStrategyAnalyze:
    """analyze() → PipelineOrchestrator 위임."""

    @pytest.mark.asyncio
    async def test_delegates_to_orchestrator(self):
        mock_result = PipelineResult(
            session_id=uuid.uuid4(),
            started_at=datetime.now(UTC),
            symbols_requested=["005930"],
        )
        orchestrator = AsyncMock()
        orchestrator.execute = AsyncMock(return_value=mock_result)

        strategy = _make_test_strategy(orchestrator=orchestrator)
        result = await strategy.analyze(["005930"])

        orchestrator.execute.assert_awaited_once_with(
            ["005930"], investment_prompt=None, risk_tolerance="moderate", account_id="default"
        )
        assert result.session_id == mock_result.session_id


class TestCheckAllExitConditions:
    """check_all_exit_conditions() — 전체 청산 확인."""

    @pytest.mark.asyncio
    async def test_no_positions_returns_empty(self):
        strategy = _make_test_strategy()
        strategy._position_manager.get_open = AsyncMock(return_value=[])

        signals = await strategy.check_all_exit_conditions()

        assert signals == []

    @pytest.mark.asyncio
    async def test_collects_exit_signals(self):
        pos1 = PositionRecord(
            symbol="005930", strategy_type="position", quantity=10,
            avg_cost=Decimal("70000"), entry_price=Decimal("70000"),
            entry_date=date(2026, 3, 20), stop_loss_price=Decimal("66000"),
            status="open",
        )
        pos2 = PositionRecord(
            symbol="000660", strategy_type="position", quantity=5,
            avg_cost=Decimal("150000"), entry_price=Decimal("150000"),
            entry_date=date(2026, 3, 21), stop_loss_price=Decimal("140000"),
            status="open",
        )

        exit_signal = ExitSignal(
            symbol="005930",
            reason=ExitReason.STOP_LOSS,
            urgency="immediate",
            current_price=Decimal("65000"),
            unrealized_pnl_pct=Decimal("-7.14"),
            recommended_action=DecisionAction.SELL,
            reasoning="손절선 도달",
        )

        strategy = _make_test_strategy()
        strategy._position_manager.get_open = AsyncMock(
            return_value=[pos1, pos2]
        )
        # 005930만 ExitSignal 반환, 000660은 None
        strategy._exit_map = {"005930": exit_signal}

        signals = await strategy.check_all_exit_conditions()

        assert len(signals) == 1
        assert signals[0].symbol == "005930"
        assert signals[0].reason == ExitReason.STOP_LOSS


class TestSavePosition:
    """save_position() — PositionManager.create 위임."""

    @pytest.mark.asyncio
    async def test_creates_position_record(self):
        expected_record = PositionRecord(
            id=1,
            symbol="005930",
            strategy_type="position",
            quantity=10,
            avg_cost=Decimal("70000"),
            entry_price=Decimal("70000"),
            entry_date=date(2026, 3, 23),
            stop_loss_price=Decimal("66000"),
            take_profit_price=Decimal("80000"),
            status="open",
        )
        sid = uuid.uuid4()
        expected_record.entry_session_id = sid

        pm = AsyncMock()
        pm.create = AsyncMock(return_value=expected_record)

        strategy = _make_test_strategy()
        strategy._position_manager = pm

        signal = _make_signal()
        sizing = _make_sizing()

        record = await strategy.save_position(
            signal=signal, sizing=sizing, session_id=sid
        )

        pm.create.assert_awaited_once()
        assert record.symbol == "005930"
        assert record.strategy_type == "position"
        assert record.quantity == 10
        assert record.avg_cost == Decimal("70000")
        assert record.stop_loss_price == Decimal("66000")
        assert record.take_profit_price == Decimal("80000")
        assert record.status == "open"
        assert record.entry_session_id == sid


class TestClosePosition:
    """close_position() — PositionManager.close 위임."""

    @pytest.mark.asyncio
    async def test_calculates_pnl(self):
        closed_record = PositionRecord(
            id=1, symbol="005930", strategy_type="position",
            quantity=10, avg_cost=Decimal("70000"),
            entry_price=Decimal("70000"), entry_date=date(2026, 3, 20),
            stop_loss_price=Decimal("66000"), status="closed",
            exit_price=Decimal("80000"),
            exit_reason=ExitReason.TAKE_PROFIT.value,
            realized_pnl=Decimal("100000"),
        )
        sid = uuid.uuid4()
        closed_record.exit_session_id = sid

        pm = AsyncMock()
        pm.close = AsyncMock(return_value=closed_record)

        strategy = _make_test_strategy()
        strategy._position_manager = pm

        record = await strategy.close_position(
            1,
            exit_price=Decimal("80000"),
            reason=ExitReason.TAKE_PROFIT,
            session_id=sid,
        )

        pm.close.assert_awaited_once()
        assert record.status == "closed"
        assert record.exit_price == Decimal("80000")
        assert record.exit_reason == ExitReason.TAKE_PROFIT.value
        # (80000 - 70000) * 10 = 100000
        assert record.realized_pnl == Decimal("100000")
        assert record.exit_session_id == sid


class TestGetOpenPositions:
    """get_open_positions() — PositionManager.get_open 위임."""

    @pytest.mark.asyncio
    async def test_with_strategy_filter(self):
        pos = PositionRecord(
            symbol="005930", strategy_type="position", quantity=10,
            avg_cost=Decimal("70000"), entry_price=Decimal("70000"),
            entry_date=date(2026, 3, 20), stop_loss_price=Decimal("66000"),
            status="open",
        )

        pm = AsyncMock()
        pm.get_open = AsyncMock(return_value=[pos])

        strategy = _make_test_strategy()
        strategy._position_manager = pm

        positions = await strategy.get_open_positions(
            strategy_type=StrategyType.POSITION
        )

        assert len(positions) == 1
        assert positions[0].symbol == "005930"
        pm.get_open.assert_awaited_once()
