"""Phase 4 Step 1: Enum, Pydantic model, Config 기반 테스트.

Tests:
- StrategyType, ExitReason enum 직렬화/역직렬화
- ExitSignal, PortfolioState, RiskCheckResult, PositionSizing 모델 생성/검증
- Signal 모델 확장 필드 (quantity, position_value_krw)
- Settings 리스크 관리 기본값 검증
"""

from datetime import datetime
from decimal import Decimal

import pytest

from src.config import Settings
from src.core.enums import (
    DecisionAction,
    ExitReason,
    PositionStatus,
    SignalAction,
    StrategyType,
)
from src.core.models import (
    ExitSignal,
    PortfolioState,
    Position,
    PositionSizing,
    RiskCheckResult,
    Signal,
)
from tests.conftest import make_settings


# ---------------------------------------------------------------------------
# Enum Tests
# ---------------------------------------------------------------------------


class TestStrategyType:
    """StrategyType enum 테스트."""

    def test_values(self):
        assert StrategyType.POSITION == "position"
        assert StrategyType.SWING == "swing"

    def test_from_string(self):
        assert StrategyType("position") is StrategyType.POSITION
        assert StrategyType("swing") is StrategyType.SWING

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            StrategyType("invalid")

    def test_all_members(self):
        assert len(StrategyType) == 3


class TestExitReason:
    """ExitReason enum 테스트."""

    EXPECTED_VALUES = [
        ("STOP_LOSS", "stop_loss"),
        ("TAKE_PROFIT", "take_profit"),
        ("TRAILING_STOP", "trailing_stop"),
        ("TIME_BASED", "time_based"),
        ("FUNDAMENTAL", "fundamental"),
        ("LLM_SIGNAL", "llm_signal"),
        ("DRAWDOWN", "drawdown"),
        ("MANUAL", "manual"),
        ("EXPIRED", "expired"),
    ]

    def test_all_values(self):
        for name, value in self.EXPECTED_VALUES:
            assert getattr(ExitReason, name) == value

    def test_from_string(self):
        for _, value in self.EXPECTED_VALUES:
            assert ExitReason(value).value == value

    def test_member_count(self):
        assert len(ExitReason) == 10

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            ExitReason("unknown")


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 22, 10, 0, 0)


class TestSignalExtension:
    """Signal 모델 확장 필드 테스트."""

    def test_default_values(self):
        sig = Signal(
            symbol="005930",
            action=SignalAction.BUY,
            confidence=Decimal("0.8"),
            reasoning="test",
            source_agent="stock_analyst",
            timestamp=NOW,
        )
        assert sig.quantity == 0
        assert sig.position_value_krw == Decimal(0)

    def test_with_values(self):
        sig = Signal(
            symbol="005930",
            action=SignalAction.BUY,
            confidence=Decimal("0.8"),
            quantity=10,
            position_value_krw=Decimal("700000"),
            reasoning="test",
            source_agent="stock_analyst",
            timestamp=NOW,
        )
        assert sig.quantity == 10
        assert sig.position_value_krw == Decimal("700000")

    def test_json_roundtrip(self):
        sig = Signal(
            symbol="005930",
            action=SignalAction.BUY,
            confidence=Decimal("0.8"),
            quantity=5,
            position_value_krw=Decimal("350000"),
            reasoning="test",
            source_agent="stock_analyst",
            timestamp=NOW,
        )
        data = sig.model_dump(mode="json")
        restored = Signal.model_validate(data)
        assert restored.quantity == 5
        assert Decimal(str(restored.position_value_krw)) == Decimal("350000")


class TestExitSignal:
    """ExitSignal 모델 테스트."""

    def _make(self, **overrides):
        defaults = {
            "symbol": "005930",
            "reason": ExitReason.STOP_LOSS,
            "urgency": "immediate",
            "current_price": Decimal("68000"),
            "unrealized_pnl_pct": Decimal("-3.5"),
            "recommended_action": DecisionAction.SELL,
            "reasoning": "손절 라인 도달",
        }
        defaults.update(overrides)
        return ExitSignal(**defaults)

    def test_create(self):
        es = self._make()
        assert es.symbol == "005930"
        assert es.reason == ExitReason.STOP_LOSS
        assert es.urgency == "immediate"
        assert es.trigger_price is None

    def test_with_trigger_price(self):
        es = self._make(trigger_price=Decimal("67500"))
        assert es.trigger_price == Decimal("67500")

    def test_json_roundtrip(self):
        es = self._make()
        data = es.model_dump(mode="json")
        restored = ExitSignal.model_validate(data)
        assert restored.reason == ExitReason.STOP_LOSS
        assert restored.recommended_action == DecisionAction.SELL

    def test_all_exit_reasons(self):
        for reason in ExitReason:
            es = self._make(reason=reason)
            assert es.reason == reason


class TestPortfolioState:
    """PortfolioState 모델 테스트."""

    def _make_position(self):
        return Position(
            symbol="005930",
            quantity=10,
            average_cost=Decimal("70000"),
            current_price=Decimal("68000"),
            market_value=Decimal("680000"),
            unrealized_pnl=Decimal("-20000"),
            unrealized_pnl_pct=Decimal("-2.86"),
            status=PositionStatus.OPEN,
            entry_date=NOW,
        )

    def _make(self, **overrides):
        defaults = {
            "total_value": Decimal("10000000"),
            "cash": Decimal("9320000"),
            "invested": Decimal("680000"),
            "unrealized_pnl": Decimal("-20000"),
            "daily_pnl": Decimal("-15000"),
            "daily_pnl_pct": Decimal("-0.15"),
            "drawdown_pct": Decimal("1.5"),
            "peak_value": Decimal("10150000"),
            "positions": [self._make_position()],
            "sector_allocations": {"전기전자": Decimal("6.8")},
            "daily_trade_count": 2,
            "timestamp": NOW,
        }
        defaults.update(overrides)
        return PortfolioState(**defaults)

    def test_create(self):
        ps = self._make()
        assert ps.total_value == Decimal("10000000")
        assert ps.drawdown_pct == Decimal("1.5")
        assert len(ps.positions) == 1
        assert ps.sector_allocations["전기전자"] == Decimal("6.8")
        assert ps.daily_trade_count == 2

    def test_empty_positions(self):
        ps = self._make(positions=[], sector_allocations={})
        assert ps.positions == []
        assert ps.sector_allocations == {}

    def test_json_roundtrip(self):
        ps = self._make()
        data = ps.model_dump(mode="json")
        restored = PortfolioState.model_validate(data)
        assert len(restored.positions) == 1
        assert restored.positions[0].symbol == "005930"


class TestRiskCheckResult:
    """RiskCheckResult 모델 테스트."""

    def _make(self, **overrides):
        defaults = {
            "passed": True,
            "symbol": "005930",
            "violations": [],
            "warnings": [],
            "adjusted_quantity": 10,
            "adjusted_amount_krw": Decimal("700000"),
            "max_allowed_quantity": 14,
            "reasoning": "모든 리스크 체크 통과",
        }
        defaults.update(overrides)
        return RiskCheckResult(**defaults)

    def test_passed(self):
        rc = self._make()
        assert rc.passed is True
        assert rc.violations == []

    def test_failed_with_violations(self):
        rc = self._make(
            passed=False,
            violations=["MAX_POSITION_PCT", "DAILY_LOSS_LIMIT"],
            warnings=["SECTOR_CONCENTRATION_PCT 접근"],
            adjusted_quantity=5,
            adjusted_amount_krw=Decimal("350000"),
            reasoning="포지션 비중 초과 + 일일 손실 한도 근접",
        )
        assert rc.passed is False
        assert len(rc.violations) == 2
        assert rc.adjusted_quantity == 5

    def test_json_roundtrip(self):
        rc = self._make(violations=["MAX_DRAWDOWN_PCT"])
        data = rc.model_dump(mode="json")
        restored = RiskCheckResult.model_validate(data)
        assert restored.violations == ["MAX_DRAWDOWN_PCT"]


class TestPositionSizing:
    """PositionSizing 모델 테스트."""

    def _make(self, **overrides):
        defaults = {
            "symbol": "005930",
            "entry_price": Decimal("70000"),
            "stop_loss_price": Decimal("67900"),
            "risk_per_share": Decimal("2100"),
            "quantity": 9,
            "position_value_krw": Decimal("630000"),
            "risk_amount_krw": Decimal("18900"),
            "risk_pct_of_portfolio": Decimal("1.89"),
            "position_pct_of_portfolio": Decimal("6.3"),
        }
        defaults.update(overrides)
        return PositionSizing(**defaults)

    def test_create(self):
        ps = self._make()
        assert ps.entry_price == Decimal("70000")
        assert ps.stop_loss_price == Decimal("67900")
        assert ps.risk_per_share == Decimal("2100")
        assert ps.quantity == 9
        assert ps.take_profit_price is None
        assert ps.risk_reward_ratio is None

    def test_with_take_profit(self):
        ps = self._make(
            take_profit_price=Decimal("75000"),
            risk_reward_ratio=Decimal("2.38"),
        )
        assert ps.take_profit_price == Decimal("75000")
        assert ps.risk_reward_ratio == Decimal("2.38")

    def test_json_roundtrip(self):
        ps = self._make()
        data = ps.model_dump(mode="json")
        restored = PositionSizing.model_validate(data)
        assert restored.quantity == 9
        assert Decimal(str(restored.risk_amount_krw)) == Decimal("18900")

    def test_risk_calculation_consistency(self):
        """risk_per_share × quantity ≈ risk_amount_krw 검증."""
        ps = self._make()
        expected_risk = ps.risk_per_share * ps.quantity
        assert expected_risk == ps.risk_amount_krw


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------


class TestPhase4Config:
    """Phase 4 리스크 관리 설정 기본값 테스트."""

    def test_risk_defaults(self):
        s = make_settings()
        assert s.RISK_CHECK_ENABLED is True
        assert s.RISK_PER_TRADE_PCT == 2.0
        assert s.MAX_POSITION_PCT == 10.0
        assert s.SECTOR_CONCENTRATION_PCT == 30.0
        assert s.MAX_DRAWDOWN_PCT == 10.0
        assert s.DAILY_LOSS_LIMIT_PCT == 3.0
        assert s.CORRELATION_THRESHOLD == 0.7
        assert s.MAX_DAILY_TRADES == 5

    def test_existing_strategy_defaults(self):
        s = make_settings()
        assert s.MAX_POSITION_SIZE_KRW == 1_000_000
        assert s.MAX_PORTFOLIO_POSITIONS == 5
        assert s.STOP_LOSS_PERCENT == 3.0
        assert s.TAKE_PROFIT_PERCENT == 5.0
        assert s.DAILY_LOSS_LIMIT_KRW == 500_000

    def test_override_risk_settings(self):
        s = make_settings(
            RISK_PER_TRADE_PCT=1.5,
            MAX_POSITION_PCT=15.0,
            MAX_DAILY_TRADES=10,
        )
        assert s.RISK_PER_TRADE_PCT == 1.5
        assert s.MAX_POSITION_PCT == 15.0
        assert s.MAX_DAILY_TRADES == 10

    def test_types(self):
        s = make_settings()
        assert isinstance(s.RISK_PER_TRADE_PCT, float)
        assert isinstance(s.MAX_POSITION_PCT, float)
        assert isinstance(s.SECTOR_CONCENTRATION_PCT, float)
        assert isinstance(s.MAX_DRAWDOWN_PCT, float)
        assert isinstance(s.DAILY_LOSS_LIMIT_PCT, float)
        assert isinstance(s.CORRELATION_THRESHOLD, float)
        assert isinstance(s.MAX_DAILY_TRADES, int)
