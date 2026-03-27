"""PerformanceCalculator 단위 테스트 — Phase 6 Step 3."""

from datetime import date
from decimal import Decimal

import pytest

from src.db.models.strategy import PortfolioSnapshot, PositionRecord
from src.report.metrics import PerformanceCalculator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(
    *,
    entry_price: Decimal,
    exit_price: Decimal,
    exit_date: date,
    realized_pnl: Decimal,
    strategy_type: str = "swing",
    symbol: str = "005930",
) -> PositionRecord:
    return PositionRecord(
        symbol=symbol,
        strategy_type=strategy_type,
        quantity=10,
        avg_cost=entry_price,
        entry_price=entry_price,
        entry_date=date(2026, 1, 1),
        stop_loss_price=(entry_price * Decimal("0.97")).quantize(Decimal("0.01")),
        exit_price=exit_price,
        exit_date=exit_date,
        realized_pnl=realized_pnl,
        status="closed",
    )


def _make_snapshot(
    *,
    snapshot_date: date,
    total_value: Decimal,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_date=snapshot_date,
        total_value=total_value,
        cash=Decimal("0"),
        invested=total_value,
        unrealized_pnl=Decimal("0"),
        peak_value=total_value,
        drawdown_pct=Decimal("0"),
        positions_count=1,
    )


# ---------------------------------------------------------------------------
# TestDailyReturns
# ---------------------------------------------------------------------------


class TestDailyReturns:
    """_daily_returns_from_snapshots 테스트."""

    def test_basic_returns(self):
        """3개 스냅샷 → 2개 일별 수익률."""
        snaps = [
            _make_snapshot(snapshot_date=date(2026, 1, 1), total_value=Decimal("100")),
            _make_snapshot(snapshot_date=date(2026, 1, 2), total_value=Decimal("105")),
            _make_snapshot(snapshot_date=date(2026, 1, 3), total_value=Decimal("102")),
        ]
        returns = PerformanceCalculator._daily_returns_from_snapshots(snaps)
        assert len(returns) == 2
        # 100→105: +5%
        assert returns[0] == Decimal("5") / Decimal("1") * Decimal("1")  # 5.0
        assert returns[0] == Decimal("5")
        # 105→102: (102-105)/105 * 100 ≈ -2.857...
        expected = (Decimal("102") - Decimal("105")) / Decimal("105") * Decimal("100")
        assert returns[1] == expected

    def test_empty_snapshots(self):
        assert PerformanceCalculator._daily_returns_from_snapshots([]) == []

    def test_single_snapshot(self):
        snaps = [_make_snapshot(snapshot_date=date(2026, 1, 1), total_value=Decimal("100"))]
        assert PerformanceCalculator._daily_returns_from_snapshots(snaps) == []

    def test_zero_value_skipped(self):
        """prev total_value=0이면 해당 쌍을 스킵."""
        snaps = [
            _make_snapshot(snapshot_date=date(2026, 1, 1), total_value=Decimal("0")),
            _make_snapshot(snapshot_date=date(2026, 1, 2), total_value=Decimal("100")),
            _make_snapshot(snapshot_date=date(2026, 1, 3), total_value=Decimal("110")),
        ]
        returns = PerformanceCalculator._daily_returns_from_snapshots(snaps)
        # 첫 쌍(0→100) 스킵, 두 번째 쌍(100→110) 포함
        assert len(returns) == 1
        assert returns[0] == Decimal("10")

    def test_unsorted_snapshots(self):
        """정렬되지 않은 스냅샷 → 내부에서 정렬."""
        snaps = [
            _make_snapshot(snapshot_date=date(2026, 1, 3), total_value=Decimal("102")),
            _make_snapshot(snapshot_date=date(2026, 1, 1), total_value=Decimal("100")),
            _make_snapshot(snapshot_date=date(2026, 1, 2), total_value=Decimal("105")),
        ]
        returns = PerformanceCalculator._daily_returns_from_snapshots(snaps)
        assert len(returns) == 2
        assert returns[0] == Decimal("5")


# ---------------------------------------------------------------------------
# TestSharpeRatio
# ---------------------------------------------------------------------------


class TestSharpeRatio:
    """calculate_sharpe_ratio 테스트."""

    def test_known_values(self):
        """알려진 일별 수익률로 Sharpe 계산."""
        daily = [Decimal("1"), Decimal("2"), Decimal("-1"), Decimal("3"), Decimal("-0.5")]
        result = PerformanceCalculator.calculate_sharpe_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is not None
        assert isinstance(result, Decimal)
        # mean = (1+2-1+3-0.5)/5 = 0.9
        # rf_daily ≈ 0.01368%
        # std(daily_returns) > 0 → finite Sharpe
        # 양의 평균 수익 → 양의 Sharpe
        assert result > Decimal("0")

    def test_single_return(self):
        result = PerformanceCalculator.calculate_sharpe_ratio(
            [Decimal("1")], risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is None

    def test_empty_returns(self):
        result = PerformanceCalculator.calculate_sharpe_ratio(
            [], risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is None

    def test_zero_std_returns_none(self):
        """모든 수익률 동일 → std=0 → None."""
        daily = [Decimal("1")] * 5
        result = PerformanceCalculator.calculate_sharpe_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is None

    def test_decimal_precision(self):
        """결과가 Decimal이고 소수점 4자리."""
        daily = [Decimal("1"), Decimal("2"), Decimal("-1"), Decimal("3"), Decimal("-0.5")]
        result = PerformanceCalculator.calculate_sharpe_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is not None
        # 소수점 4자리 확인
        assert result == result.quantize(Decimal("0.0001"))

    def test_negative_mean_negative_sharpe(self):
        """평균 수익률 < rf → 음의 Sharpe."""
        daily = [Decimal("-2"), Decimal("-1"), Decimal("-3"), Decimal("0.5"), Decimal("-1")]
        result = PerformanceCalculator.calculate_sharpe_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is not None
        assert result < Decimal("0")


# ---------------------------------------------------------------------------
# TestSortinoRatio
# ---------------------------------------------------------------------------


class TestSortinoRatio:
    """calculate_sortino_ratio 테스트."""

    def test_downside_deviation(self):
        """하방 수익률이 있는 경우 Sortino 계산."""
        daily = [Decimal("1"), Decimal("2"), Decimal("-1"), Decimal("3"), Decimal("-0.5")]
        result = PerformanceCalculator.calculate_sortino_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is not None
        assert isinstance(result, Decimal)
        # 양의 평균 → 양의 Sortino
        assert result > Decimal("0")

    def test_no_downside_returns_none(self):
        """모든 수익률이 rf 이상이면 None."""
        daily = [Decimal("5"), Decimal("10"), Decimal("3")]
        result = PerformanceCalculator.calculate_sortino_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is None

    def test_single_return(self):
        result = PerformanceCalculator.calculate_sortino_ratio(
            [Decimal("1")], risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is None

    def test_empty_returns(self):
        result = PerformanceCalculator.calculate_sortino_ratio(
            [], risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert result is None

    def test_sortino_greater_than_sharpe_for_low_downside(self):
        """하방 변동성 < 전체 변동성이면 Sortino > Sharpe."""
        daily = [Decimal("3"), Decimal("5"), Decimal("-0.5"), Decimal("4"), Decimal("2")]
        sharpe = PerformanceCalculator.calculate_sharpe_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        sortino = PerformanceCalculator.calculate_sortino_ratio(
            daily, risk_free_rate_annual_pct=Decimal("3.5"),
        )
        assert sharpe is not None
        assert sortino is not None
        assert sortino > sharpe


# ---------------------------------------------------------------------------
# TestMaxDrawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    """calculate_max_drawdown 테스트."""

    def test_peak_trough_pattern(self):
        """100 → 80 → 95 → 70 → 110 → MDD = 30%."""
        snaps = [
            _make_snapshot(snapshot_date=date(2026, 1, d), total_value=v)
            for d, v in [
                (1, Decimal("100")),
                (2, Decimal("80")),
                (3, Decimal("95")),
                (4, Decimal("70")),
                (5, Decimal("110")),
            ]
        ]
        mdd = PerformanceCalculator.calculate_max_drawdown(snaps)
        assert mdd == Decimal("30.00")

    def test_monotonic_increase(self):
        """단조 증가 → MDD = 0%."""
        snaps = [
            _make_snapshot(snapshot_date=date(2026, 1, d), total_value=v)
            for d, v in [(1, Decimal("100")), (2, Decimal("110")), (3, Decimal("120"))]
        ]
        mdd = PerformanceCalculator.calculate_max_drawdown(snaps)
        assert mdd == Decimal("0.00")

    def test_empty_snapshots(self):
        assert PerformanceCalculator.calculate_max_drawdown([]) == Decimal("0")

    def test_single_snapshot(self):
        snaps = [_make_snapshot(snapshot_date=date(2026, 1, 1), total_value=Decimal("100"))]
        mdd = PerformanceCalculator.calculate_max_drawdown(snaps)
        assert mdd == Decimal("0.00")


# ---------------------------------------------------------------------------
# TestWinRate
# ---------------------------------------------------------------------------


class TestWinRate:
    """calculate_win_rate 테스트."""

    def test_three_wins_two_losses(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("100")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("120"),
                           exit_date=date(2026, 1, 11), realized_pnl=Decimal("200")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("105"),
                           exit_date=date(2026, 1, 12), realized_pnl=Decimal("50")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 13), realized_pnl=Decimal("-100")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("95"),
                           exit_date=date(2026, 1, 14), realized_pnl=Decimal("-50")),
        ]
        rate, wins, losses = PerformanceCalculator.calculate_win_rate(positions)
        assert rate == Decimal("60.00")
        assert wins == 3
        assert losses == 2

    def test_all_wins(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("100")),
        ]
        rate, wins, losses = PerformanceCalculator.calculate_win_rate(positions)
        assert rate == Decimal("100.00")
        assert wins == 1
        assert losses == 0

    def test_all_losses(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("-100")),
        ]
        rate, wins, losses = PerformanceCalculator.calculate_win_rate(positions)
        assert rate == Decimal("0.00")
        assert wins == 0
        assert losses == 1

    def test_empty(self):
        rate, wins, losses = PerformanceCalculator.calculate_win_rate([])
        assert rate == Decimal("0")
        assert wins == 0
        assert losses == 0

    def test_breakeven_is_loss(self):
        """realized_pnl = 0 → 패배 처리."""
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("100"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("0")),
        ]
        rate, wins, losses = PerformanceCalculator.calculate_win_rate(positions)
        assert losses == 1


# ---------------------------------------------------------------------------
# TestProfitFactor
# ---------------------------------------------------------------------------


class TestProfitFactor:
    """calculate_profit_factor 테스트."""

    def test_basic(self):
        """이익 300, 손실 100 → PF = 3.00."""
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("130"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("300")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 11), realized_pnl=Decimal("-100")),
        ]
        pf = PerformanceCalculator.calculate_profit_factor(positions)
        assert pf == Decimal("3.00")

    def test_no_losses_returns_none(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("100")),
        ]
        assert PerformanceCalculator.calculate_profit_factor(positions) is None

    def test_no_profits_returns_zero(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("-100")),
        ]
        assert PerformanceCalculator.calculate_profit_factor(positions) == Decimal("0")

    def test_empty_returns_none(self):
        assert PerformanceCalculator.calculate_profit_factor([]) is None


# ---------------------------------------------------------------------------
# TestAvgWinLoss
# ---------------------------------------------------------------------------


class TestAvgWinLoss:
    """calculate_avg_win_loss 테스트."""

    def test_basic(self):
        """진입100 → 청산110(+10%) 과 진입100 → 청산95(-5%)."""
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("100")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("95"),
                           exit_date=date(2026, 1, 11), realized_pnl=Decimal("-50")),
        ]
        avg_win, avg_loss = PerformanceCalculator.calculate_avg_win_loss(positions)
        assert avg_win == Decimal("10.00")  # +10%
        assert avg_loss == Decimal("5.00")  # abs(-5%) = 5%

    def test_no_wins(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("-100")),
        ]
        avg_win, avg_loss = PerformanceCalculator.calculate_avg_win_loss(positions)
        assert avg_win == Decimal("0")
        assert avg_loss == Decimal("10.00")

    def test_no_losses(self):
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("115"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("150")),
        ]
        avg_win, avg_loss = PerformanceCalculator.calculate_avg_win_loss(positions)
        assert avg_win == Decimal("15.00")
        assert avg_loss == Decimal("0")

    def test_empty(self):
        avg_win, avg_loss = PerformanceCalculator.calculate_avg_win_loss([])
        assert avg_win == Decimal("0")
        assert avg_loss == Decimal("0")


# ---------------------------------------------------------------------------
# TestBreakdownByStrategy
# ---------------------------------------------------------------------------


class TestBreakdownByStrategy:
    """breakdown_by_strategy 테스트."""

    def test_position_and_swing(self):
        """position 2건 + swing 3건."""
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("100"),
                           strategy_type="position"),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 11), realized_pnl=Decimal("-100"),
                           strategy_type="position"),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("105"),
                           exit_date=date(2026, 1, 12), realized_pnl=Decimal("50"),
                           strategy_type="swing"),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("115"),
                           exit_date=date(2026, 1, 13), realized_pnl=Decimal("150"),
                           strategy_type="swing"),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("97"),
                           exit_date=date(2026, 1, 14), realized_pnl=Decimal("-30"),
                           strategy_type="swing"),
        ]
        result = PerformanceCalculator.breakdown_by_strategy(positions)

        assert "position" in result
        assert "swing" in result
        assert result["position"]["trade_count"] == 2
        assert result["swing"]["trade_count"] == 3

        # position: 1승 1패 → 50%
        assert result["position"]["win_rate_pct"] == Decimal("50.00")
        # swing: 2승 1패 → 66.67%
        assert result["swing"]["win_rate_pct"] == Decimal("66.67")

    def test_empty(self):
        assert PerformanceCalculator.breakdown_by_strategy([]) == {}


# ---------------------------------------------------------------------------
# TestBreakdownByMonth
# ---------------------------------------------------------------------------


class TestBreakdownByMonth:
    """breakdown_by_month 테스트."""

    def test_jan_and_feb(self):
        """1월 2건 + 2월 3건."""
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 15), realized_pnl=Decimal("100")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("90"),
                           exit_date=date(2026, 1, 20), realized_pnl=Decimal("-100")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("105"),
                           exit_date=date(2026, 2, 5), realized_pnl=Decimal("50")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("115"),
                           exit_date=date(2026, 2, 10), realized_pnl=Decimal("150")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("97"),
                           exit_date=date(2026, 2, 15), realized_pnl=Decimal("-30")),
        ]
        result = PerformanceCalculator.breakdown_by_month(positions)

        assert list(result.keys()) == ["2026-01", "2026-02"]
        assert result["2026-01"]["trade_count"] == 2
        assert result["2026-02"]["trade_count"] == 3
        # 1월: 이익100 + 손실-100 = 0
        assert result["2026-01"]["total_pnl"] == Decimal("0")
        # 2월: 50+150-30 = 170
        assert result["2026-02"]["total_pnl"] == Decimal("170")

    def test_empty(self):
        assert PerformanceCalculator.breakdown_by_month([]) == {}


# ---------------------------------------------------------------------------
# TestCalculate (Orchestrator)
# ---------------------------------------------------------------------------


class TestCalculate:
    """calculate() 오케스트레이터 테스트."""

    def test_full_calculation(self):
        """포지션 + 스냅샷 → 전체 PerformanceMetrics."""
        positions = [
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("110"),
                           exit_date=date(2026, 1, 10), realized_pnl=Decimal("100")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("120"),
                           exit_date=date(2026, 1, 15), realized_pnl=Decimal("200")),
            _make_position(entry_price=Decimal("100"), exit_price=Decimal("95"),
                           exit_date=date(2026, 1, 20), realized_pnl=Decimal("-50")),
        ]
        snaps = [
            _make_snapshot(snapshot_date=date(2026, 1, d), total_value=v)
            for d, v in [
                (1, Decimal("1000")),
                (2, Decimal("1010")),
                (3, Decimal("1005")),
                (4, Decimal("1020")),
                (5, Decimal("1015")),
            ]
        ]
        result = PerformanceCalculator.calculate(
            closed_positions=positions,
            snapshots=snaps,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            risk_free_rate_pct=Decimal("3.5"),
        )

        assert result.period_start == date(2026, 1, 1)
        assert result.period_end == date(2026, 1, 31)

        # 총 수익률: (1015-1000)/1000 * 100 = 1.5%
        assert result.total_return_pct == Decimal("1.50")

        # 연환산 수익률 존재
        assert result.annualized_return_pct is not None

        # Sharpe/Sortino 존재 (데이터 충분)
        assert result.sharpe_ratio is not None
        assert isinstance(result.sharpe_ratio, Decimal)

        # MDD > 0 (1020→1015 하락 존재)
        assert result.max_drawdown_pct > Decimal("0")

        # 승률: 2/3 = 66.67%
        assert result.win_rate_pct == Decimal("66.67")
        assert result.winning_trades == 2
        assert result.losing_trades == 1
        assert result.total_trades == 3

        # Profit Factor: 300/50 = 6.00
        assert result.profit_factor == Decimal("6.00")

        # avg_win > 0, avg_loss > 0
        assert result.avg_win_pct > Decimal("0")
        assert result.avg_loss_pct > Decimal("0")

    def test_empty_inputs(self):
        """빈 입력 → 기본값."""
        result = PerformanceCalculator.calculate(
            closed_positions=[],
            snapshots=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )

        assert result.total_return_pct == Decimal("0")
        assert result.annualized_return_pct is None
        assert result.sharpe_ratio is None
        assert result.sortino_ratio is None
        assert result.max_drawdown_pct == Decimal("0")
        assert result.win_rate_pct == Decimal("0")
        assert result.total_trades == 0
        assert result.profit_factor is None

    def test_decimal_precision(self):
        """모든 필드가 Decimal 타입."""
        result = PerformanceCalculator.calculate(
            closed_positions=[],
            snapshots=[],
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        assert isinstance(result.total_return_pct, Decimal)
        assert isinstance(result.max_drawdown_pct, Decimal)
        assert isinstance(result.win_rate_pct, Decimal)
        assert isinstance(result.avg_win_pct, Decimal)
        assert isinstance(result.avg_loss_pct, Decimal)

    def test_single_snapshot_no_annualized(self):
        """스냅샷 1개 → total_return=0, annualized=None."""
        snaps = [_make_snapshot(snapshot_date=date(2026, 1, 1), total_value=Decimal("1000"))]
        result = PerformanceCalculator.calculate(
            closed_positions=[],
            snapshots=snaps,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        assert result.total_return_pct == Decimal("0")
        assert result.annualized_return_pct is None
