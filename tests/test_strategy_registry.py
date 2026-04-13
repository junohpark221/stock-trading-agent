"""Phase 8 Step 5: Strategy Registry / Factory / account_id 전파 테스트."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.core.enums import StrategyType
from src.core.models import PipelineResult, PositionSizing, Signal
from src.strategy.position_trading import PositionTradingStrategy
from src.strategy.registry import (
    StrategyCommonDeps,
    StrategyFactory,
    _STRATEGY_REGISTRY,
    _apply_risk_overrides,
    get_strategy_class,
    register_strategy,
)
from src.strategy.swing_trading import SwingTradingStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.RISK_PER_TRADE_PCT = 2.0
    s.MAX_POSITION_PCT = 10.0
    s.MAX_POSITION_SIZE_KRW = 10_000_000
    s.SECTOR_CONCENTRATION_PCT = 30.0
    s.MAX_DRAWDOWN_PCT = 10.0
    s.DAILY_LOSS_LIMIT_PCT = 3.0
    s.DAILY_LOSS_LIMIT_KRW = 500_000
    s.CORRELATION_THRESHOLD = 0.7
    s.MAX_DAILY_TRADES = 5
    s.MAX_PORTFOLIO_POSITIONS = 10
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_deps(
    *,
    settings: MagicMock | None = None,
    broker: AsyncMock | None = None,
) -> StrategyCommonDeps:
    return StrategyCommonDeps(
        orchestrator=AsyncMock(),
        recorder=AsyncMock(),
        broker=broker or AsyncMock(),
        session_factory=MagicMock(),
        settings=settings or _make_settings(),
        cache=AsyncMock(),
    )


# ===========================================================================
# 1. Registry decorator
# ===========================================================================


class TestRegisterStrategy:
    """@register_strategy 데코레이터 테스트."""

    def test_position_registered(self):
        """PositionTradingStrategy가 POSITION으로 등록되어 있다."""
        assert StrategyType.POSITION in _STRATEGY_REGISTRY
        assert _STRATEGY_REGISTRY[StrategyType.POSITION] is PositionTradingStrategy

    def test_swing_registered(self):
        """SwingTradingStrategy가 SWING으로 등록되어 있다."""
        assert StrategyType.SWING in _STRATEGY_REGISTRY
        assert _STRATEGY_REGISTRY[StrategyType.SWING] is SwingTradingStrategy

    def test_duplicate_registration_raises(self):
        """같은 StrategyType에 중복 등록하면 ValueError."""
        with pytest.raises(ValueError, match="already registered"):

            @register_strategy(StrategyType.POSITION)
            class DummyStrategy:
                pass


# ===========================================================================
# 2. get_strategy_class
# ===========================================================================


class TestGetStrategyClass:
    """get_strategy_class 조회 테스트."""

    def test_valid_position(self):
        assert get_strategy_class(StrategyType.POSITION) is PositionTradingStrategy

    def test_valid_swing(self):
        assert get_strategy_class(StrategyType.SWING) is SwingTradingStrategy

    def test_unknown_raises_key_error(self):
        """등록되지 않은 타입은 KeyError."""
        # StrEnum이므로 존재하지 않는 값을 직접 만들 수 없으므로
        # 레지스트리를 임시로 비워서 테스트
        saved = dict(_STRATEGY_REGISTRY)
        _STRATEGY_REGISTRY.clear()
        try:
            with pytest.raises(KeyError, match="No strategy registered"):
                get_strategy_class(StrategyType.POSITION)
        finally:
            _STRATEGY_REGISTRY.update(saved)


# ===========================================================================
# 3. _apply_risk_overrides
# ===========================================================================


class TestApplyRiskOverrides:
    """risk_overrides 적용 테스트."""

    def test_valid_override(self):
        """허용된 키로 override하면 새 Settings 사본이 반환된다."""
        from src.config import Settings

        base = Settings(
            DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        original_pct = base.MAX_POSITION_PCT
        updated = _apply_risk_overrides(base, {"MAX_POSITION_PCT": 5.0})
        assert updated.MAX_POSITION_PCT == 5.0
        assert base.MAX_POSITION_PCT == original_pct  # 원본 불변

    def test_invalid_key_raises(self):
        """허용되지 않은 키는 ValueError."""
        from src.config import Settings

        base = Settings(
            DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        with pytest.raises(ValueError, match="Invalid risk override keys"):
            _apply_risk_overrides(base, {"DATABASE_URL": "bad"})


# ===========================================================================
# 4. StrategyFactory.create
# ===========================================================================


class TestStrategyFactory:
    """StrategyFactory.create() 테스트."""

    @patch("src.strategy.position_manager.PositionManager")
    @patch("src.strategy.risk_manager.AlgoRiskManager")
    @patch("src.strategy.portfolio_state.PortfolioStateService")
    def test_create_position(self, mock_ps, mock_arm, mock_pm):
        """POSITION 전략 인스턴스 생성."""
        deps = _make_deps()
        strategy = StrategyFactory.create(StrategyType.POSITION, deps)
        assert isinstance(strategy, PositionTradingStrategy)
        assert strategy._account_id == "default"
        assert strategy._investment_prompt == ""

    @patch("src.strategy.position_manager.PositionManager")
    @patch("src.strategy.risk_manager.AlgoRiskManager")
    @patch("src.strategy.portfolio_state.PortfolioStateService")
    def test_create_swing(self, mock_ps, mock_arm, mock_pm):
        """SWING 전략 인스턴스 생성."""
        deps = _make_deps()
        strategy = StrategyFactory.create(StrategyType.SWING, deps)
        assert isinstance(strategy, SwingTradingStrategy)

    @patch("src.strategy.position_manager.PositionManager")
    @patch("src.strategy.risk_manager.AlgoRiskManager")
    @patch("src.strategy.portfolio_state.PortfolioStateService")
    def test_create_with_account_id(self, mock_ps, mock_arm, mock_pm):
        """account_id가 전략 인스턴스에 전파된다."""
        deps = _make_deps()
        strategy = StrategyFactory.create(
            StrategyType.POSITION, deps, account_id="acct-123"
        )
        assert strategy._account_id == "acct-123"

    @patch("src.strategy.position_manager.PositionManager")
    @patch("src.strategy.risk_manager.AlgoRiskManager")
    @patch("src.strategy.portfolio_state.PortfolioStateService")
    def test_create_with_investment_prompt(self, mock_ps, mock_arm, mock_pm):
        """investment_prompt가 전략 인스턴스에 전파된다."""
        deps = _make_deps()
        strategy = StrategyFactory.create(
            StrategyType.SWING,
            deps,
            investment_prompt="공격적 단기 트레이딩",
        )
        assert strategy._investment_prompt == "공격적 단기 트레이딩"

    @patch("src.strategy.position_manager.PositionManager")
    @patch("src.strategy.risk_manager.AlgoRiskManager")
    @patch("src.strategy.portfolio_state.PortfolioStateService")
    def test_create_with_risk_overrides(self, mock_ps, mock_arm, mock_pm):
        """risk_overrides가 적용된 Settings 사본으로 전략이 생성된다."""
        from src.config import Settings

        real_settings = Settings(
            DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        deps = _make_deps(settings=real_settings)
        strategy = StrategyFactory.create(
            StrategyType.POSITION,
            deps,
            risk_overrides={"MAX_POSITION_PCT": 5.0},
        )
        assert strategy._settings.MAX_POSITION_PCT == 5.0
        assert real_settings.MAX_POSITION_PCT != 5.0  # 원본 불변

    @patch("src.strategy.position_manager.PositionManager")
    @patch("src.strategy.risk_manager.AlgoRiskManager")
    @patch("src.strategy.portfolio_state.PortfolioStateService")
    def test_create_portfolio_service_receives_account_id(
        self, mock_ps, mock_arm, mock_pm
    ):
        """PortfolioStateService가 account_id를 받아 생성된다."""
        deps = _make_deps()
        StrategyFactory.create(
            StrategyType.POSITION, deps, account_id="acct-456"
        )
        mock_ps.assert_called_once()
        call_kwargs = mock_ps.call_args
        assert call_kwargs.kwargs.get("account_id") == "acct-456"

    def test_create_unknown_type_raises(self):
        """미등록 전략 타입은 KeyError."""
        saved = dict(_STRATEGY_REGISTRY)
        _STRATEGY_REGISTRY.clear()
        try:
            deps = _make_deps()
            with pytest.raises(KeyError, match="No strategy registered"):
                StrategyFactory.create(StrategyType.POSITION, deps)
        finally:
            _STRATEGY_REGISTRY.update(saved)


# ===========================================================================
# 5. Strategy base class — account_id / investment_prompt 전파
# ===========================================================================


class TestStrategyAccountPropagation:
    """Strategy ABC의 account_id/investment_prompt 전파 테스트."""

    def _make_strategy(
        self,
        account_id: str = "default",
        investment_prompt: str = "",
    ) -> PositionTradingStrategy:
        return PositionTradingStrategy(
            orchestrator=AsyncMock(),
            risk_manager=AsyncMock(),
            portfolio_service=AsyncMock(),
            broker=AsyncMock(),
            recorder=AsyncMock(),
            position_manager=AsyncMock(),
            session_factory=MagicMock(),
            settings=_make_settings(),
            account_id=account_id,
            investment_prompt=investment_prompt,
        )

    @pytest.mark.asyncio
    async def test_analyze_passes_account_id_and_prompt(self):
        """analyze()가 orchestrator.execute()에 account_id/investment_prompt를 전달한다."""
        strategy = self._make_strategy(
            account_id="test-acct",
            investment_prompt="보수적 가치투자",
        )
        strategy._orchestrator.execute.return_value = MagicMock(spec=PipelineResult)

        await strategy.analyze(["005930"])

        strategy._orchestrator.execute.assert_called_once_with(
            ["005930"],
            investment_prompt="보수적 가치투자",
            risk_tolerance="moderate",
            account_id="test-acct",
        )

    @pytest.mark.asyncio
    async def test_analyze_empty_prompt_passes_none(self):
        """investment_prompt가 빈 문자열이면 None으로 전달된다."""
        strategy = self._make_strategy(account_id="acct-1", investment_prompt="")
        strategy._orchestrator.execute.return_value = MagicMock(spec=PipelineResult)

        await strategy.analyze(["005930"])

        call_kwargs = strategy._orchestrator.execute.call_args
        assert call_kwargs.kwargs["investment_prompt"] is None
        assert call_kwargs.kwargs["account_id"] == "acct-1"

    @pytest.mark.asyncio
    async def test_save_position_passes_account_id(self):
        """save_position()이 position_manager.create()에 account_id를 전달한다."""
        strategy = self._make_strategy(account_id="acct-abc")
        strategy._position_manager.create = AsyncMock()

        signal = MagicMock(spec=Signal)
        signal.symbol = "005930"
        sizing = MagicMock(spec=PositionSizing)
        sizing.quantity = 10
        sizing.entry_price = Decimal("50000")
        sizing.stop_loss_price = Decimal("48000")
        sizing.take_profit_price = Decimal("55000")

        await strategy.save_position(
            signal=signal, sizing=sizing, session_id=uuid4()
        )

        call_kwargs = strategy._position_manager.create.call_args
        assert call_kwargs.kwargs["account_id"] == "acct-abc"

    @pytest.mark.asyncio
    async def test_get_open_positions_passes_account_id(self):
        """get_open_positions()이 position_manager.get_open()에 account_id를 전달한다."""
        strategy = self._make_strategy(account_id="acct-xyz")
        strategy._position_manager.get_open = AsyncMock(return_value=[])

        await strategy.get_open_positions(strategy_type=StrategyType.POSITION)

        strategy._position_manager.get_open.assert_called_once_with(
            strategy_type=StrategyType.POSITION,
            account_id="acct-xyz",
        )

    def test_backward_compat_defaults(self):
        """account_id/investment_prompt 없이 생성하면 기본값 사용."""
        strategy = PositionTradingStrategy(
            orchestrator=AsyncMock(),
            risk_manager=AsyncMock(),
            portfolio_service=AsyncMock(),
            broker=AsyncMock(),
            recorder=AsyncMock(),
            position_manager=AsyncMock(),
            session_factory=MagicMock(),
            settings=_make_settings(),
        )
        assert strategy._account_id == "default"
        assert strategy._investment_prompt == ""
