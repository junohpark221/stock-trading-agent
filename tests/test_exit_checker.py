"""Phase 4 Step 8: ExitConditionChecker 단위 테스트.

각 청산 조건별 트리거/미트리거/경계값 + drawdown 포트폴리오 레벨 = 25 테스트.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.enums import DecisionAction, ExitReason
from src.core.models import PortfolioState
from src.strategy.exit_checker import ExitConditionChecker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(**overrides: object) -> MagicMock:
    """PositionRecord mock 생성."""
    pos = MagicMock()
    pos.symbol = "005930"
    pos.entry_price = Decimal("70000")
    pos.stop_loss_price = Decimal("67000")
    pos.take_profit_price = Decimal("77000")
    pos.trailing_stop_pct = Decimal("5.0")
    pos.max_holding_days = 60
    pos.entry_date = date(2026, 3, 1)
    for k, v in overrides.items():
        setattr(pos, k, v)
    return pos


def _make_portfolio_state(**overrides: object) -> PortfolioState:
    """PortfolioState 생성."""
    defaults: dict = {
        "total_value": Decimal("100000000"),
        "cash": Decimal("50000000"),
        "invested": Decimal("50000000"),
        "unrealized_pnl": Decimal("1000000"),
        "daily_pnl": Decimal("200000"),
        "daily_pnl_pct": Decimal("0.20"),
        "drawdown_pct": Decimal("2.0"),
        "peak_value": Decimal("102000000"),
        "positions": [],
        "sector_allocations": {},
        "daily_trade_count": 0,
        "timestamp": datetime(2026, 3, 22, 9, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return PortfolioState(**defaults)  # type: ignore[arg-type]


def _make_checker(max_drawdown_pct: Decimal = Decimal("10.0")) -> ExitConditionChecker:
    return ExitConditionChecker(max_drawdown_pct=max_drawdown_pct)


# ===========================================================================
# check_stop_loss
# ===========================================================================


class TestCheckStopLoss:
    """손절 체크 테스트."""

    def test_trigger_below_stop_loss(self) -> None:
        """현재가 < 손절가 → 즉시 청산 시그널."""
        checker = _make_checker()
        pos = _make_position(stop_loss_price=Decimal("67000"))
        signal = checker.check_stop_loss(pos, Decimal("66000"), Decimal("-5.71"))

        assert signal is not None
        assert signal.reason == ExitReason.STOP_LOSS
        assert signal.urgency == "immediate"
        assert signal.recommended_action == DecisionAction.STOP_LOSS
        assert signal.symbol == "005930"
        assert signal.trigger_price == Decimal("67000")
        assert signal.current_price == Decimal("66000")

    def test_trigger_at_exact_stop_loss(self) -> None:
        """현재가 == 손절가 (경계값) → 청산 시그널."""
        checker = _make_checker()
        pos = _make_position(stop_loss_price=Decimal("67000"))
        signal = checker.check_stop_loss(pos, Decimal("67000"), Decimal("-4.29"))

        assert signal is not None
        assert signal.reason == ExitReason.STOP_LOSS

    def test_no_trigger_above_stop_loss(self) -> None:
        """현재가 > 손절가 → None."""
        checker = _make_checker()
        pos = _make_position(stop_loss_price=Decimal("67000"))
        signal = checker.check_stop_loss(pos, Decimal("68000"), Decimal("-2.86"))

        assert signal is None

    def test_reasoning_contains_prices(self) -> None:
        """reasoning에 현재가, 손절가, 손실률 포함."""
        checker = _make_checker()
        pos = _make_position(stop_loss_price=Decimal("67000"))
        signal = checker.check_stop_loss(pos, Decimal("66000"), Decimal("-5.71"))

        assert signal is not None
        assert "66,000" in signal.reasoning or "66000" in signal.reasoning
        assert "67,000" in signal.reasoning or "67000" in signal.reasoning


# ===========================================================================
# check_take_profit
# ===========================================================================


class TestCheckTakeProfit:
    """익절 체크 테스트."""

    def test_trigger_above_take_profit(self) -> None:
        """현재가 > 익절가 → 장 마감 청산 시그널."""
        checker = _make_checker()
        pos = _make_position(take_profit_price=Decimal("77000"))
        signal = checker.check_take_profit(pos, Decimal("78000"), Decimal("11.43"))

        assert signal is not None
        assert signal.reason == ExitReason.TAKE_PROFIT
        assert signal.urgency == "end_of_day"
        assert signal.recommended_action == DecisionAction.TAKE_PROFIT
        assert signal.trigger_price == Decimal("77000")

    def test_trigger_at_exact_take_profit(self) -> None:
        """현재가 == 익절가 (경계값) → 청산 시그널."""
        checker = _make_checker()
        pos = _make_position(take_profit_price=Decimal("77000"))
        signal = checker.check_take_profit(pos, Decimal("77000"), Decimal("10.0"))

        assert signal is not None
        assert signal.reason == ExitReason.TAKE_PROFIT

    def test_no_trigger_below_take_profit(self) -> None:
        """현재가 < 익절가 → None."""
        checker = _make_checker()
        pos = _make_position(take_profit_price=Decimal("77000"))
        signal = checker.check_take_profit(pos, Decimal("75000"), Decimal("7.14"))

        assert signal is None

    def test_no_trigger_when_take_profit_is_none(self) -> None:
        """익절가 미설정(None) → None."""
        checker = _make_checker()
        pos = _make_position(take_profit_price=None)
        signal = checker.check_take_profit(pos, Decimal("100000"), Decimal("42.86"))

        assert signal is None


# ===========================================================================
# check_trailing_stop
# ===========================================================================


class TestCheckTrailingStop:
    """트레일링 스톱 체크 테스트."""

    def test_trigger_below_trailing_stop(self) -> None:
        """현재가 < 트레일링가 → 즉시 청산."""
        checker = _make_checker()
        pos = _make_position()
        trailing_price = Decimal("74000")
        signal = checker.check_trailing_stop(
            pos, Decimal("73000"), Decimal("4.29"), trailing_price
        )

        assert signal is not None
        assert signal.reason == ExitReason.TRAILING_STOP
        assert signal.urgency == "immediate"
        assert signal.recommended_action == DecisionAction.SELL
        assert signal.trigger_price == Decimal("74000")

    def test_trigger_at_exact_trailing_stop(self) -> None:
        """현재가 == 트레일링가 (경계값) → 청산."""
        checker = _make_checker()
        pos = _make_position()
        trailing_price = Decimal("74000")
        signal = checker.check_trailing_stop(
            pos, Decimal("74000"), Decimal("5.71"), trailing_price
        )

        assert signal is not None
        assert signal.reason == ExitReason.TRAILING_STOP

    def test_no_trigger_above_trailing_stop(self) -> None:
        """현재가 > 트레일링가 → None."""
        checker = _make_checker()
        pos = _make_position()
        trailing_price = Decimal("74000")
        signal = checker.check_trailing_stop(
            pos, Decimal("76000"), Decimal("8.57"), trailing_price
        )

        assert signal is None


# ===========================================================================
# check_time_based
# ===========================================================================


class TestCheckTimeBased:
    """시간 기반 청산 테스트."""

    def test_trigger_exceeds_max_holding(self) -> None:
        """보유 기간 > max_holding_days → 청산."""
        checker = _make_checker()
        pos = _make_position(entry_date=date(2026, 1, 1), max_holding_days=60)
        today = date(2026, 3, 26)  # 정확히 60거래일 경과 (달력 84일, 주말 제외)
        signal = checker.check_time_based(
            pos, Decimal("72000"), Decimal("2.86"), today
        )

        assert signal is not None
        assert signal.reason == ExitReason.TIME_BASED
        assert signal.urgency == "end_of_day"
        assert signal.recommended_action == DecisionAction.SELL

    def test_trigger_at_exact_max_holding(self) -> None:
        """보유 기간 == max_holding_days (경계값) → 청산."""
        checker = _make_checker()
        pos = _make_position(entry_date=date(2026, 3, 2), max_holding_days=10)
        today = date(2026, 3, 16)  # 월~금 2주 = 정확히 10거래일
        signal = checker.check_time_based(
            pos, Decimal("72000"), Decimal("2.86"), today
        )

        assert signal is not None
        assert signal.reason == ExitReason.TIME_BASED

    def test_no_trigger_within_holding_period(self) -> None:
        """보유 기간 < max_holding_days → None."""
        checker = _make_checker()
        pos = _make_position(entry_date=date(2026, 3, 1), max_holding_days=60)
        today = date(2026, 3, 15)  # 14일
        signal = checker.check_time_based(
            pos, Decimal("72000"), Decimal("2.86"), today
        )

        assert signal is None

    def test_no_trigger_when_max_holding_is_none(self) -> None:
        """max_holding_days 미설정(None) → None."""
        checker = _make_checker()
        pos = _make_position(max_holding_days=None)
        today = date(2026, 12, 31)  # 아무리 오래 보유해도
        signal = checker.check_time_based(
            pos, Decimal("72000"), Decimal("2.86"), today
        )

        assert signal is None

    def test_custom_urgency_next_session(self) -> None:
        """time_urgency 커스텀 파라미터 — Position 전략용 'next_session'."""
        checker = _make_checker()
        pos = _make_position(entry_date=date(2026, 1, 1), max_holding_days=60)
        today = date(2026, 3, 26)  # 정확히 60거래일 경과
        signal = checker.check_time_based(
            pos, Decimal("72000"), Decimal("2.86"), today,
            time_urgency="next_session",
        )

        assert signal is not None
        assert signal.urgency == "next_session"

    def test_reasoning_contains_days(self) -> None:
        """reasoning에 보유일수(거래일), 한도일수 포함."""
        checker = _make_checker()
        pos = _make_position(entry_date=date(2026, 3, 2), max_holding_days=10)
        today = date(2026, 3, 20)  # 달력 18일, 거래일 14일 (주말 4일 제외)
        signal = checker.check_time_based(
            pos, Decimal("72000"), Decimal("2.86"), today
        )

        assert signal is not None
        assert "14일" in signal.reasoning
        assert "10일" in signal.reasoning


# ===========================================================================
# check_drawdown_exit
# ===========================================================================


class TestCheckDrawdownExit:
    """포트폴리오 드로다운 체크 테스트."""

    def test_drawdown_exceeds_threshold(self) -> None:
        """drawdown > max_drawdown_pct → True."""
        checker = _make_checker(max_drawdown_pct=Decimal("10.0"))
        state = _make_portfolio_state(drawdown_pct=Decimal("12.0"))

        assert checker.check_drawdown_exit(state) is True

    def test_drawdown_within_threshold(self) -> None:
        """drawdown < max_drawdown_pct → False."""
        checker = _make_checker(max_drawdown_pct=Decimal("10.0"))
        state = _make_portfolio_state(drawdown_pct=Decimal("5.0"))

        assert checker.check_drawdown_exit(state) is False

    def test_drawdown_at_exact_threshold(self) -> None:
        """drawdown == max_drawdown_pct (경계값) → False (> 조건, >= 아님)."""
        checker = _make_checker(max_drawdown_pct=Decimal("10.0"))
        state = _make_portfolio_state(drawdown_pct=Decimal("10.0"))

        assert checker.check_drawdown_exit(state) is False

    def test_zero_drawdown(self) -> None:
        """drawdown 0% → False."""
        checker = _make_checker(max_drawdown_pct=Decimal("10.0"))
        state = _make_portfolio_state(drawdown_pct=Decimal("0"))

        assert checker.check_drawdown_exit(state) is False
