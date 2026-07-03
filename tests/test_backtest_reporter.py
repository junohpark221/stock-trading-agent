"""BacktestReporter 단위 테스트.

generate_report, IS/OOS 분리, 월별 수익률, compare_runs 검증.
DB 접근 없이 ORM 객체를 직접 생성하여 테스트.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.backtest.reporter import BacktestReporter
from src.core.enums import (
    BacktestMode,
    BacktestStatus,
    ExitReason,
    OrderSide,
    StrategyType,
)
from src.core.models import (
    BacktestConfig,
    BacktestReport,
    BacktestResult,
    BacktestTradeRecord,
    ComparisonReport,
    PerformanceMetrics,
)
from src.db.models.strategy import PortfolioSnapshot, PositionRecord

_ZERO = Decimal("0")
_D = Decimal


# ---------------------------------------------------------------------------
# Test Helpers
# ---------------------------------------------------------------------------


def _make_position(
    *,
    symbol: str = "005930",
    strategy_type: str = "position",
    entry_price: Decimal = _D("50000"),
    exit_price: Decimal = _D("55000"),
    entry_date: date = date(2024, 1, 2),
    exit_date: date = date(2024, 2, 1),
    realized_pnl: Decimal = _D("50000"),
    quantity: int = 10,
) -> PositionRecord:
    return PositionRecord(
        id=1,
        symbol=symbol,
        strategy_type=strategy_type,
        quantity=quantity,
        avg_cost=entry_price,
        entry_price=entry_price,
        entry_date=entry_date,
        stop_loss_price=entry_price * _D("0.95"),
        take_profit_price=exit_price * _D("1.1"),
        status="closed",
        exit_price=exit_price,
        exit_date=exit_date,
        exit_reason="take_profit",
        realized_pnl=realized_pnl,
    )


def _make_snapshot(
    *,
    snapshot_date: date,
    total_value: Decimal,
    cash: Decimal | None = None,
) -> PortfolioSnapshot:
    cash_val = cash if cash is not None else total_value * _D("0.3")
    invested = total_value - cash_val
    return PortfolioSnapshot(
        id=1,
        snapshot_date=snapshot_date,
        total_value=total_value,
        cash=cash_val,
        invested=invested,
        unrealized_pnl=_ZERO,
        realized_pnl_daily=_ZERO,
        peak_value=total_value,
        drawdown_pct=_ZERO,
        positions_count=1,
        trade_count_daily=0,
    )


def _make_snapshots_series(
    start_date: date,
    num_days: int,
    start_value: Decimal,
    daily_return_pct: Decimal,
) -> list[PortfolioSnapshot]:
    """일정한 일별 수익률로 스냅샷 시리즈를 생성."""
    snaps = []
    value = start_value
    d = start_date
    for _ in range(num_days):
        snaps.append(_make_snapshot(snapshot_date=d, total_value=value))
        value = value * (_D("1") + daily_return_pct / _D("100"))
        value = value.quantize(_D("0.01"))
        d += timedelta(days=1)
    return snaps


def _make_metrics(
    *,
    total_return_pct: Decimal = _D("10.00"),
    max_drawdown_pct: Decimal = _D("5.00"),
    sharpe_ratio: Decimal | None = _D("1.50"),
    period_start: date = date(2024, 1, 1),
    period_end: date = date(2024, 6, 30),
) -> PerformanceMetrics:
    return PerformanceMetrics(
        period_start=period_start,
        period_end=period_end,
        total_return_pct=total_return_pct,
        annualized_return_pct=total_return_pct * _D("2"),
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=_D("1.80") if sharpe_ratio else None,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=_D("60.00"),
        avg_win_pct=_D("5.00"),
        avg_loss_pct=_D("3.00"),
        profit_factor=_D("1.50"),
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
    )


def _make_config(
    start_date: date = date(2024, 1, 1),
    end_date: date = date(2024, 6, 30),
) -> BacktestConfig:
    return BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=start_date,
        end_date=end_date,
        initial_capital=_D("10000000"),
        symbols=["005930", "000660"],
    )


def _make_result(
    *,
    run_id: UUID | None = None,
    config: BacktestConfig | None = None,
    metrics: PerformanceMetrics | None = None,
    benchmark_metrics: PerformanceMetrics | None = None,
    excess_return_pct: Decimal | None = None,
) -> BacktestResult:
    return BacktestResult(
        run_id=run_id or uuid4(),
        config=config or _make_config(),
        status=BacktestStatus.COMPLETED,
        metrics=metrics or _make_metrics(),
        benchmark_metrics=benchmark_metrics,
        excess_return_pct=excess_return_pct,
        trades=[],
        total_trades=0,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGenerateReportBasic:
    """generate_report 기본 필드 검증."""

    def test_generate_report_basic(self) -> None:
        """5 positions(3승/2패) + 30 snapshots → BacktestReport 필드 전체 검증."""
        run_id = uuid4()
        config = _make_config()
        metrics = _make_metrics()
        result = _make_result(run_id=run_id, config=config, metrics=metrics)

        # 3 승
        positions = [
            _make_position(
                symbol="005930", strategy_type="position",
                entry_price=_D("50000"), exit_price=_D("55000"),
                entry_date=date(2024, 1, 10), exit_date=date(2024, 2, 1),
                realized_pnl=_D("50000"),
            ),
            _make_position(
                symbol="000660", strategy_type="swing",
                entry_price=_D("80000"), exit_price=_D("88000"),
                entry_date=date(2024, 2, 5), exit_date=date(2024, 2, 20),
                realized_pnl=_D("80000"),
            ),
            _make_position(
                symbol="035720", strategy_type="position",
                entry_price=_D("30000"), exit_price=_D("33000"),
                entry_date=date(2024, 3, 1), exit_date=date(2024, 4, 1),
                realized_pnl=_D("30000"),
            ),
        ]
        # 2 패
        positions.extend([
            _make_position(
                symbol="005930", strategy_type="swing",
                entry_price=_D("55000"), exit_price=_D("52000"),
                entry_date=date(2024, 3, 10), exit_date=date(2024, 3, 20),
                realized_pnl=_D("-30000"),
            ),
            _make_position(
                symbol="000660", strategy_type="position",
                entry_price=_D("90000"), exit_price=_D("85000"),
                entry_date=date(2024, 4, 5), exit_date=date(2024, 5, 1),
                realized_pnl=_D("-50000"),
            ),
        ])

        snapshots = _make_snapshots_series(
            start_date=date(2024, 1, 1),
            num_days=30,
            start_value=_D("10000000"),
            daily_return_pct=_D("0.1"),
        )

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=positions,
            snapshots=snapshots,
        )

        assert isinstance(report, BacktestReport)
        assert report.run_id == run_id
        assert report.trade_count == 5
        assert report.metrics == metrics
        assert len(report.monthly_returns) > 0
        assert "position" in report.strategy_breakdown
        assert "swing" in report.strategy_breakdown
        assert report.generated_at is not None

    def test_empty_trades(self) -> None:
        """0 positions + 10 snapshots → report 정상 생성."""
        result = _make_result()
        snapshots = _make_snapshots_series(
            start_date=date(2024, 1, 1),
            num_days=10,
            start_value=_D("10000000"),
            daily_return_pct=_D("0"),
        )

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=[],
            snapshots=snapshots,
        )

        assert report.trade_count == 0
        assert report.strategy_breakdown == {}


class TestExcessReturn:
    """벤치마크 패스스루 검증."""

    def test_excess_return(self) -> None:
        """result에 benchmark_metrics/excess_return_pct 설정 → report에 패스스루."""
        benchmark = _make_metrics(
            total_return_pct=_D("8.00"),
            max_drawdown_pct=_D("3.00"),
            sharpe_ratio=_D("0.80"),
        )
        result = _make_result(
            benchmark_metrics=benchmark,
            excess_return_pct=_D("5.00"),
        )

        snapshots = _make_snapshots_series(
            start_date=date(2024, 1, 1),
            num_days=10,
            start_value=_D("10000000"),
            daily_return_pct=_D("0.05"),
        )

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=[],
            snapshots=snapshots,
        )

        assert report.benchmark_metrics == benchmark
        assert report.excess_return_pct == _D("5.00")


class TestISOOSSplit:
    """IS/OOS 분리 검증."""

    def test_is_oos_split_70_30(self) -> None:
        """100 snapshots + 10 positions → split_date ≈ 70번째, IS/OOS metrics 유효."""
        start = date(2024, 1, 1)
        snapshots = _make_snapshots_series(
            start_date=start,
            num_days=100,
            start_value=_D("10000000"),
            daily_return_pct=_D("0.05"),
        )

        # 포지션 10개: 5개는 IS 구간, 5개는 OOS 구간
        positions = []
        for i in range(5):
            positions.append(_make_position(
                symbol=f"SYM{i:02d}",
                entry_date=start + timedelta(days=i * 10),
                exit_date=start + timedelta(days=i * 10 + 15),
                entry_price=_D("50000"),
                exit_price=_D("53000"),
                realized_pnl=_D("30000"),
            ))
        for i in range(5):
            positions.append(_make_position(
                symbol=f"SYM{i + 5:02d}",
                entry_date=start + timedelta(days=70 + i * 5),
                exit_date=start + timedelta(days=75 + i * 5),
                entry_price=_D("50000"),
                exit_price=_D("52000"),
                realized_pnl=_D("20000"),
            ))

        config = _make_config(start_date=start, end_date=start + timedelta(days=99))
        result = _make_result(config=config)

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=positions,
            snapshots=snapshots,
            is_ratio=_D("0.7"),
        )

        assert report.oos_split is not None
        assert report.oos_split.is_ratio == _D("0.7")
        # split_date는 70번째 스냅샷 날짜 (index 70)
        expected_split = start + timedelta(days=70)
        assert report.oos_split.split_date == expected_split
        assert report.oos_split.in_sample_metrics is not None
        assert report.oos_split.out_of_sample_metrics is not None

    def test_is_overfit_detection(self) -> None:
        """IS +30%, OOS -8% → is_overfit=True."""
        start = date(2024, 1, 1)

        # IS: 100일, 강한 상승 (일 +0.26% ≈ 총 +30%)
        is_snaps = _make_snapshots_series(
            start_date=start,
            num_days=70,
            start_value=_D("10000000"),
            daily_return_pct=_D("0.38"),
        )
        # OOS: 30일, 하락 (일 -0.28% ≈ 총 -8%)
        oos_start = start + timedelta(days=70)
        oos_start_value = is_snaps[-1].total_value
        oos_snaps = _make_snapshots_series(
            start_date=oos_start,
            num_days=30,
            start_value=oos_start_value,
            daily_return_pct=_D("-0.28"),
        )
        all_snaps = is_snaps + oos_snaps

        # IS 포지션: 수익
        positions = [
            _make_position(
                symbol="A001",
                entry_date=start + timedelta(days=5),
                exit_date=start + timedelta(days=50),
                entry_price=_D("50000"), exit_price=_D("65000"),
                realized_pnl=_D("150000"),
            ),
        ]
        # OOS 포지션: 손실
        positions.append(_make_position(
            symbol="A002",
            entry_date=oos_start + timedelta(days=2),
            exit_date=oos_start + timedelta(days=20),
            entry_price=_D("65000"), exit_price=_D("60000"),
            realized_pnl=_D("-50000"),
        ))

        config = _make_config(start_date=start, end_date=start + timedelta(days=99))
        result = _make_result(config=config)

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=positions,
            snapshots=all_snaps,
            is_ratio=_D("0.7"),
        )

        assert report.oos_split is not None
        assert report.oos_split.is_overfit is True

    def test_is_not_overfit(self) -> None:
        """IS/OOS 모두 양호한 수익 → is_overfit=False."""
        start = date(2024, 1, 1)

        # IS: 상승
        is_snaps = _make_snapshots_series(
            start_date=start,
            num_days=70,
            start_value=_D("10000000"),
            daily_return_pct=_D("0.1"),
        )
        # OOS: IS 대비 50% 이상의 수익률 (일 +0.2%)
        oos_start = start + timedelta(days=70)
        oos_start_value = is_snaps[-1].total_value
        oos_snaps = _make_snapshots_series(
            start_date=oos_start,
            num_days=30,
            start_value=oos_start_value,
            daily_return_pct=_D("0.2"),
        )
        all_snaps = is_snaps + oos_snaps

        positions = [
            _make_position(
                symbol="B001",
                entry_date=start + timedelta(days=5),
                exit_date=start + timedelta(days=40),
                entry_price=_D("50000"), exit_price=_D("55000"),
                realized_pnl=_D("50000"),
            ),
            _make_position(
                symbol="B002",
                entry_date=oos_start + timedelta(days=2),
                exit_date=oos_start + timedelta(days=20),
                entry_price=_D("50000"), exit_price=_D("54000"),
                realized_pnl=_D("40000"),
            ),
        ]

        config = _make_config(start_date=start, end_date=start + timedelta(days=99))
        result = _make_result(config=config)

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=positions,
            snapshots=all_snaps,
            is_ratio=_D("0.7"),
        )

        assert report.oos_split is not None
        assert report.oos_split.is_overfit is False


class TestMonthlyReturns:
    """월별 수익률 검증."""

    def test_monthly_returns(self) -> None:
        """3개월 스냅샷 → 3개 YYYY-MM 키, 예상 수익률 일치."""
        # 1월: 10,000,000 → 10,300,000 (+3%)
        jan_snaps = [
            _make_snapshot(snapshot_date=date(2024, 1, 2), total_value=_D("10000000")),
            _make_snapshot(snapshot_date=date(2024, 1, 15), total_value=_D("10150000")),
            _make_snapshot(snapshot_date=date(2024, 1, 31), total_value=_D("10300000")),
        ]
        # 2월: 10,300,000 → 10,100,000 (-1.94%)
        feb_snaps = [
            _make_snapshot(snapshot_date=date(2024, 2, 1), total_value=_D("10300000")),
            _make_snapshot(snapshot_date=date(2024, 2, 15), total_value=_D("10200000")),
            _make_snapshot(snapshot_date=date(2024, 2, 29), total_value=_D("10100000")),
        ]
        # 3월: 10,100,000 → 10,505,000 (+4.01%)
        mar_snaps = [
            _make_snapshot(snapshot_date=date(2024, 3, 1), total_value=_D("10100000")),
            _make_snapshot(snapshot_date=date(2024, 3, 15), total_value=_D("10300000")),
            _make_snapshot(snapshot_date=date(2024, 3, 29), total_value=_D("10505000")),
        ]
        all_snaps = jan_snaps + feb_snaps + mar_snaps

        result = _make_result(
            config=_make_config(start_date=date(2024, 1, 1), end_date=date(2024, 3, 31)),
        )

        report = BacktestReporter.generate_report(
            result=result,
            closed_positions=[],
            snapshots=all_snaps,
        )

        assert "2024-01" in report.monthly_returns
        assert "2024-02" in report.monthly_returns
        assert "2024-03" in report.monthly_returns

        # 1월: (10300000 - 10000000) / 10000000 * 100 = 3.00%
        assert report.monthly_returns["2024-01"] == _D("3.00")
        # 2월: (10100000 - 10300000) / 10300000 * 100 = -1.94%
        assert report.monthly_returns["2024-02"] == _D("-1.94")
        # 3월: (10505000 - 10100000) / 10100000 * 100 = 4.01%
        assert report.monthly_returns["2024-03"] == _D("4.01")


class TestCompareRuns:
    """복수 런 비교 검증."""

    def test_compare_runs(self) -> None:
        """3개 BacktestResult → best_sharpe/return/mdd 식별."""
        run_a = uuid4()
        run_b = uuid4()
        run_c = uuid4()

        # A: 최고 Sharpe (2.0), 수익률 15%, MDD 8%
        result_a = _make_result(
            run_id=run_a,
            metrics=_make_metrics(
                total_return_pct=_D("15.00"),
                max_drawdown_pct=_D("8.00"),
                sharpe_ratio=_D("2.00"),
            ),
        )
        # B: Sharpe 1.0, 최고 수익률 25%, MDD 12%
        result_b = _make_result(
            run_id=run_b,
            metrics=_make_metrics(
                total_return_pct=_D("25.00"),
                max_drawdown_pct=_D("12.00"),
                sharpe_ratio=_D("1.00"),
            ),
        )
        # C: Sharpe 1.5, 수익률 10%, 최저 MDD 3%
        result_c = _make_result(
            run_id=run_c,
            metrics=_make_metrics(
                total_return_pct=_D("10.00"),
                max_drawdown_pct=_D("3.00"),
                sharpe_ratio=_D("1.50"),
            ),
        )

        # 각 런에 최소한의 스냅샷 제공
        base_snaps = _make_snapshots_series(
            start_date=date(2024, 1, 1),
            num_days=5,
            start_value=_D("10000000"),
            daily_return_pct=_D("0.1"),
        )

        positions_map = {run_a: [], run_b: [], run_c: []}
        snapshots_map = {
            run_a: base_snaps,
            run_b: base_snaps,
            run_c: base_snaps,
        }

        comparison = BacktestReporter.compare_runs(
            results=[result_a, result_b, result_c],
            closed_positions_map=positions_map,
            snapshots_map=snapshots_map,
        )

        assert isinstance(comparison, ComparisonReport)
        assert len(comparison.reports) == 3
        assert comparison.best_sharpe_run_id == run_a
        assert comparison.best_return_run_id == run_b
        assert comparison.lowest_mdd_run_id == run_c
        assert comparison.generated_at is not None


# ---------------------------------------------------------------------------
# breakdown_by_exit_reason (F-13 게이트 측정)
# ---------------------------------------------------------------------------


def _make_trade(
    *,
    exit_reason: ExitReason | None,
    pnl: Decimal | None,
    symbol: str = "005930",
) -> BacktestTradeRecord:
    return BacktestTradeRecord(
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=10,
        price=_D("50000"),
        commission=_ZERO,
        slippage=_ZERO,
        trade_date=date(2026, 1, 5),
        pnl=pnl,
        exit_reason=exit_reason,
    )


class TestBreakdownByExitReason:
    def test_empty(self):
        assert BacktestReporter.breakdown_by_exit_reason([]) == {}

    def test_skips_open_trades(self):
        """exit_reason/pnl 없는(미청산·진입) 거래는 제외."""
        trades = [
            _make_trade(exit_reason=None, pnl=None),
            _make_trade(exit_reason=ExitReason.STOP_LOSS, pnl=_D("-1000")),
        ]
        result = BacktestReporter.breakdown_by_exit_reason(trades)
        assert set(result) == {"stop_loss"}
        assert result["stop_loss"]["trade_count"] == 1

    def test_shares_and_expectancy(self):
        """비중·기대값(평균 pnl)·승률 집계 검증."""
        trades = [
            _make_trade(exit_reason=ExitReason.STOP_LOSS, pnl=_D("-1000")),
            _make_trade(exit_reason=ExitReason.STOP_LOSS, pnl=_D("-2000")),
            _make_trade(exit_reason=ExitReason.TAKE_PROFIT, pnl=_D("3000")),
            _make_trade(exit_reason=ExitReason.TRAILING_STOP, pnl=_D("1000")),
        ]
        result = BacktestReporter.breakdown_by_exit_reason(trades)
        # stop_loss: 2/4 = 50%, 기대값 -1500, 승률 0
        assert result["stop_loss"]["trade_count"] == 2
        assert result["stop_loss"]["share_pct"] == _D("50.00")
        assert result["stop_loss"]["avg_pnl"] == _D("-1500.00")
        assert result["stop_loss"]["win_rate_pct"] == _D("0.00")
        assert result["stop_loss"]["total_pnl"] == _D("-3000")
        # take_profit: 1/4 = 25%, 승률 100%
        assert result["take_profit"]["share_pct"] == _D("25.00")
        assert result["take_profit"]["win_rate_pct"] == _D("100.00")
