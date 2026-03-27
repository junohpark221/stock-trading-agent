"""Phase 8 Step 3: 도메인 모델 + 서비스 계층 account_id 전파 테스트.

Pydantic 모델 account_id 필드, PositionManager/PortfolioStateService/
AlgoRiskManager/AgentMemoryManager의 account_id 파라미터/필터 검증.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

from src.core.enums import (
    AgentType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    SignalAction,
    StrategyType,
)
from src.core.models import (
    AccountBalance,
    OrderRequest,
    OrderResult,
    PipelineResult,
    PortfolioState,
    Position,
    Signal,
)
from src.strategy.memory_manager import AgentMemoryManager
from src.strategy.portfolio_state import PortfolioStateService
from src.strategy.position_manager import PositionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_factory():
    """mock async session factory."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _mock_scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    result.scalar.return_value = value
    return result


def _mock_scalars_result(values: list) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    result.scalars.return_value = scalars_mock
    return result


# ===========================================================================
# A. Pydantic 모델 account_id 필드
# ===========================================================================


class TestModelsAccountId:
    """7개 도메인 모델에 account_id: str = 'default' 필드가 추가되었는지 검증."""

    def test_default_values(self):
        """account_id 미지정 시 'default' 기본값."""
        signal = Signal(
            symbol="005930",
            action=SignalAction.BUY,
            confidence=Decimal("0.8"),
            reasoning="test",
            source_agent=AgentType.STOCK_ANALYST,
            timestamp=datetime.now(UTC),
        )
        assert signal.account_id == "default"

        order_req = OrderRequest(
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("70000"),
        )
        assert order_req.account_id == "default"

        order_res = OrderResult(
            order_id="ORD001",
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("70000"),
            status=OrderStatus.FILLED,
            timestamp=datetime.now(UTC),
        )
        assert order_res.account_id == "default"

        position = Position(
            symbol="005930",
            quantity=100,
            average_cost=Decimal("70000"),
            current_price=Decimal("72000"),
            status=PositionStatus.OPEN,
            entry_date=datetime.now(UTC),
        )
        assert position.account_id == "default"

        balance = AccountBalance(
            total_assets=Decimal("100000000"),
            cash=Decimal("50000000"),
            invested=Decimal("50000000"),
            timestamp=datetime.now(UTC),
        )
        assert balance.account_id == "default"

        pipeline = PipelineResult(
            session_id=uuid4(),
            started_at=datetime.now(UTC),
        )
        assert pipeline.account_id == "default"

        state = PortfolioState(
            total_value=Decimal("100000000"),
            cash=Decimal("50000000"),
            invested=Decimal("50000000"),
            unrealized_pnl=Decimal("0"),
            daily_pnl=Decimal("0"),
            daily_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            peak_value=Decimal("100000000"),
            positions=[],
            sector_allocations={},
            daily_trade_count=0,
            timestamp=datetime.now(UTC),
        )
        assert state.account_id == "default"

    def test_custom_account_id(self):
        """account_id를 커스텀 값으로 설정."""
        signal = Signal(
            account_id="acct-1",
            symbol="005930",
            action=SignalAction.BUY,
            confidence=Decimal("0.8"),
            reasoning="test",
            source_agent=AgentType.STOCK_ANALYST,
            timestamp=datetime.now(UTC),
        )
        assert signal.account_id == "acct-1"

        pipeline = PipelineResult(
            account_id="aggressive",
            session_id=uuid4(),
            started_at=datetime.now(UTC),
        )
        assert pipeline.account_id == "aggressive"


# ===========================================================================
# B. PositionManager account_id
# ===========================================================================


class TestPositionManagerAccountId:
    """PositionManager 메서드에 account_id 전파 검증."""

    @pytest.mark.asyncio
    async def test_create_with_account_id(self):
        """create(account_id='acct-1') → PositionRecord에 전달."""
        factory, session = _mock_session_factory()
        mgr = PositionManager(factory)

        with patch(
            "src.strategy.position_manager.PositionRecord"
        ) as MockRecord:
            mock_record = MagicMock(id=1, symbol="005930", quantity=10)
            MockRecord.return_value = mock_record

            await mgr.create(
                symbol="005930",
                strategy_type="position",
                quantity=10,
                entry_price=Decimal("70000"),
                stop_loss_price=Decimal("67000"),
                account_id="acct-1",
            )

            # PositionRecord 생성자에 account_id='acct-1' 전달 확인
            _, kwargs = MockRecord.call_args
            assert kwargs["account_id"] == "acct-1"

    @pytest.mark.asyncio
    async def test_create_default_account_id(self):
        """create() account_id 미지정 시 'default'."""
        factory, session = _mock_session_factory()
        mgr = PositionManager(factory)

        with patch(
            "src.strategy.position_manager.PositionRecord"
        ) as MockRecord:
            mock_record = MagicMock(id=1, symbol="005930", quantity=10)
            MockRecord.return_value = mock_record

            await mgr.create(
                symbol="005930",
                strategy_type="position",
                quantity=10,
                entry_price=Decimal("70000"),
                stop_loss_price=Decimal("67000"),
            )

            _, kwargs = MockRecord.call_args
            assert kwargs["account_id"] == "default"

    @pytest.mark.asyncio
    async def test_get_open_with_account_filter(self):
        """get_open(account_id='acct-1') → 쿼리 실행 (에러 없음)."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalars_result([]))
        mgr = PositionManager(factory)

        result = await mgr.get_open(account_id="acct-1")
        assert result == []
        # execute가 호출됨
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_open_without_account_filter(self):
        """get_open() account_id=None → 전체 계좌 조회."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalars_result([]))
        mgr = PositionManager(factory)

        result = await mgr.get_open()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_by_symbol_with_account_filter(self):
        """get_by_symbol(account_id='acct-1') → 에러 없이 실행."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            return_value=_mock_scalar_result(None)
        )
        mgr = PositionManager(factory)

        result = await mgr.get_by_symbol("005930", account_id="acct-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_daily_entries_with_account_filter(self):
        """get_daily_entries(account_id='acct-1') → 에러 없이 실행."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalar_result(3))
        mgr = PositionManager(factory)

        count = await mgr.get_daily_entries(
            date.today(), account_id="acct-1"
        )
        assert count == 3


# ===========================================================================
# C. PortfolioStateService account_id
# ===========================================================================


class TestPortfolioStateAccountId:
    """PortfolioStateService 인스턴스 바인딩 account_id 검증."""

    def test_stores_account_id(self):
        """생성 시 account_id 저장."""
        broker = AsyncMock()
        factory = MagicMock()
        cache = AsyncMock()

        svc = PortfolioStateService(
            broker, factory, cache, account_id="acct-1"
        )
        assert svc._account_id == "acct-1"

    def test_default_account_id(self):
        """account_id 미지정 시 'default'."""
        broker = AsyncMock()
        factory = MagicMock()
        cache = AsyncMock()

        svc = PortfolioStateService(broker, factory, cache)
        assert svc._account_id == "default"

    @pytest.mark.asyncio
    async def test_get_current_state_includes_account_id(self):
        """get_current_state() 반환에 account_id 포함."""
        broker = AsyncMock()
        broker.get_balance = AsyncMock(
            return_value=AccountBalance(
                total_assets=Decimal("100000000"),
                cash=Decimal("50000000"),
                invested=Decimal("50000000"),
                timestamp=datetime.now(UTC),
            )
        )
        broker.get_positions = AsyncMock(return_value=[])

        factory, session = _mock_session_factory()
        # get_peak_value mock
        session.execute = AsyncMock(
            return_value=_mock_scalar_result(Decimal("100000000"))
        )
        cache = AsyncMock()

        svc = PortfolioStateService(
            broker, factory, cache, account_id="acct-1"
        )

        # get_daily_trade_count도 mock
        with patch.object(
            svc, "get_daily_trade_count", return_value=0
        ):
            state = await svc.get_current_state()

        assert state.account_id == "acct-1"

    @pytest.mark.asyncio
    async def test_save_snapshot_includes_account_id(self):
        """save_snapshot() values에 account_id 포함 + 올바른 constraint."""
        factory, session = _mock_session_factory()
        broker = AsyncMock()
        cache = AsyncMock()

        svc = PortfolioStateService(
            broker, factory, cache, account_id="acct-1"
        )

        state = PortfolioState(
            account_id="acct-1",
            total_value=Decimal("100000000"),
            cash=Decimal("50000000"),
            invested=Decimal("50000000"),
            unrealized_pnl=Decimal("0"),
            daily_pnl=Decimal("0"),
            daily_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            peak_value=Decimal("100000000"),
            positions=[],
            sector_allocations={},
            daily_trade_count=0,
            timestamp=datetime(2026, 3, 27, 9, 0, tzinfo=UTC),
        )

        await svc.save_snapshot(state)

        # session.execute가 호출됨 (upsert 문)
        session.execute.assert_called_once()
        # constraint 이름이 올바른지 stmt 인자에서 확인
        stmt_arg = session.execute.call_args[0][0]
        # compiled SQL에 account_date constraint 참조 확인
        compiled = str(stmt_arg.compile(compile_kwargs={"literal_binds": True}))
        assert "uq_portfolio_snapshots_account_date" in compiled

    @pytest.mark.asyncio
    async def test_get_peak_value_filters_account(self):
        """get_peak_value()에 account_id WHERE 필터."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(
            return_value=_mock_scalar_result(Decimal("120000000"))
        )
        broker = AsyncMock()
        cache = AsyncMock()

        svc = PortfolioStateService(
            broker, factory, cache, account_id="acct-1"
        )
        peak = await svc.get_peak_value()
        assert peak == Decimal("120000000")
        session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_daily_trade_count_filters_account(self):
        """get_daily_trade_count()에 account_id WHERE 필터."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalar_result(5))
        broker = AsyncMock()
        cache = AsyncMock()

        svc = PortfolioStateService(
            broker, factory, cache, account_id="acct-2"
        )
        count = await svc.get_daily_trade_count(date.today())
        assert count == 5


# ===========================================================================
# D. AlgoRiskManager account_id
# ===========================================================================


class TestRiskManagerAccountId:
    """AlgoRiskManager.check() account_id 패스스루."""

    @pytest.mark.asyncio
    async def test_check_with_account_id(self):
        """check(account_id='acct-1') → 정상 실행."""
        from src.strategy.risk_manager import AlgoRiskManager

        # portfolio_service mock
        portfolio_svc = AsyncMock()
        portfolio_svc.get_current_state = AsyncMock(
            return_value=PortfolioState(
                total_value=Decimal("100000000"),
                cash=Decimal("50000000"),
                invested=Decimal("50000000"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("0"),
                daily_pnl_pct=Decimal("0"),
                drawdown_pct=Decimal("0"),
                peak_value=Decimal("100000000"),
                positions=[],
                sector_allocations={},
                daily_trade_count=0,
                timestamp=datetime.now(UTC),
            )
        )

        factory, session = _mock_session_factory()
        settings = MagicMock()
        settings.RISK_MAX_POSITION_PCT = Decimal("10")
        settings.RISK_MAX_SINGLE_POSITION_PCT = Decimal("20")
        settings.RISK_MAX_HOLDING_COUNT = 10
        settings.RISK_MAX_SECTOR_PCT = Decimal("30")
        settings.RISK_MAX_DRAWDOWN_PCT = Decimal("15")
        settings.RISK_DAILY_LOSS_LIMIT_PCT = Decimal("3")
        settings.RISK_DAILY_LOSS_LIMIT_KRW = Decimal("5000000")
        settings.RISK_CORRELATION_THRESHOLD = Decimal("0.7")
        settings.RISK_MAX_DAILY_TRADES = 5

        mgr = AlgoRiskManager(portfolio_svc, factory, settings)

        # SELL은 항상 통과
        result = await mgr.check(
            symbol="005930",
            action=SignalAction.SELL,
            quantity=10,
            price=Decimal("70000"),
            stop_loss_price=None,
            sector="전기전자",
            account_id="acct-1",
        )
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_check_default_account_id(self):
        """check() account_id 미지정 시 기본값으로 동작."""
        from src.strategy.risk_manager import AlgoRiskManager

        portfolio_svc = AsyncMock()
        portfolio_svc.get_current_state = AsyncMock(
            return_value=PortfolioState(
                total_value=Decimal("100000000"),
                cash=Decimal("50000000"),
                invested=Decimal("50000000"),
                unrealized_pnl=Decimal("0"),
                daily_pnl=Decimal("0"),
                daily_pnl_pct=Decimal("0"),
                drawdown_pct=Decimal("0"),
                peak_value=Decimal("100000000"),
                positions=[],
                sector_allocations={},
                daily_trade_count=0,
                timestamp=datetime.now(UTC),
            )
        )

        factory, session = _mock_session_factory()
        settings = MagicMock()
        settings.RISK_MAX_POSITION_PCT = Decimal("10")
        settings.RISK_MAX_SINGLE_POSITION_PCT = Decimal("20")
        settings.RISK_MAX_HOLDING_COUNT = 10
        settings.RISK_MAX_SECTOR_PCT = Decimal("30")
        settings.RISK_MAX_DRAWDOWN_PCT = Decimal("15")
        settings.RISK_DAILY_LOSS_LIMIT_PCT = Decimal("3")
        settings.RISK_DAILY_LOSS_LIMIT_KRW = Decimal("5000000")
        settings.RISK_CORRELATION_THRESHOLD = Decimal("0.7")
        settings.RISK_MAX_DAILY_TRADES = 5

        mgr = AlgoRiskManager(portfolio_svc, factory, settings)

        # account_id 없이 호출 → 기본값 "default"로 정상 동작
        result = await mgr.check(
            symbol="005930",
            action=SignalAction.HOLD,
            quantity=0,
            price=Decimal("70000"),
            stop_loss_price=None,
            sector="전기전자",
        )
        assert result.passed is True


# ===========================================================================
# E. AgentMemoryManager account_id
# ===========================================================================


class TestMemoryManagerAccountId:
    """AgentMemoryManager 메서드 account_id 전파 검증."""

    @pytest.mark.asyncio
    async def test_save_lesson_with_account_id(self):
        """save_lesson(account_id='acct-1') → AgentMemory에 전달."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        with patch(
            "src.strategy.memory_manager.AgentMemory"
        ) as MockMemory:
            mock_record = MagicMock(id=42)
            MockMemory.return_value = mock_record

            result = await mgr.save_lesson(
                agent_type="stock_analyst",
                symbol="005930",
                content="test lesson",
                account_id="acct-1",
            )

            assert result == 42
            _, kwargs = MockMemory.call_args
            assert kwargs["account_id"] == "acct-1"

    @pytest.mark.asyncio
    async def test_save_lesson_default_account_id(self):
        """save_lesson() account_id 미지정 시 'default'."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        with patch(
            "src.strategy.memory_manager.AgentMemory"
        ) as MockMemory:
            mock_record = MagicMock(id=1)
            MockMemory.return_value = mock_record

            await mgr.save_lesson(
                agent_type="stock_analyst",
                symbol=None,
                content="general lesson",
            )

            _, kwargs = MockMemory.call_args
            assert kwargs["account_id"] == "default"

    @pytest.mark.asyncio
    async def test_get_relevant_memories_with_account_filter(self):
        """get_relevant_memories(account_id='acct-1') → 에러 없이 실행."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalars_result([]))
        mgr = AgentMemoryManager(factory)

        result = await mgr.get_relevant_memories(
            agent_type="stock_analyst",
            account_id="acct-1",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_get_relevant_memories_without_account_filter(self):
        """get_relevant_memories() account_id=None → 전체 계좌."""
        factory, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_mock_scalars_result([]))
        mgr = AgentMemoryManager(factory)

        result = await mgr.get_relevant_memories(
            agent_type="stock_analyst",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_record_trade_outcome_with_account_id(self):
        """record_trade_outcome(account_id='acct-1') → save_lesson에 전달."""
        factory, session = _mock_session_factory()
        mgr = AgentMemoryManager(factory)

        # Mock position (closed with loss)
        position = MagicMock()
        position.status = "closed"
        position.realized_pnl = Decimal("-100000")
        position.symbol = "005930"
        position.strategy_type = "position"
        position.avg_cost = Decimal("70000")
        position.quantity = 100
        position.entry_price = Decimal("70000")
        position.exit_price = Decimal("69000")
        position.exit_reason = "stop_loss"
        position.exit_date = date(2026, 3, 27)
        position.entry_date = date(2026, 3, 20)
        position.entry_session_id = None

        # Mock pipeline result
        pipeline = PipelineResult(
            session_id=uuid4(),
            started_at=datetime.now(UTC),
            stock_analyses=[],
        )

        with patch.object(mgr, "save_lesson", new_callable=AsyncMock) as mock_save:
            mock_save.return_value = 99

            result = await mgr.record_trade_outcome(
                position, pipeline, account_id="acct-1"
            )

            assert result == 99
            # save_lesson에 account_id 전달 확인
            _, kwargs = mock_save.call_args
            assert kwargs["account_id"] == "acct-1"
