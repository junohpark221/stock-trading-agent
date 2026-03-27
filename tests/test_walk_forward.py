"""WalkForwardAnalyzer 단위 테스트."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.backtest.walk_forward import WalkForwardAnalyzer, _empty_metrics
from src.core.enums import BacktestMode, BacktestStatus, StrategyType
from src.core.models import BacktestConfig, BacktestResult, PerformanceMetrics


# ── Helpers ───────────────────────────────────────────────────────────


def _make_config(
    start: date = date(2024, 1, 1),
    end: date = date(2025, 12, 31),
) -> BacktestConfig:
    return BacktestConfig(
        strategy_type=StrategyType.POSITION,
        start_date=start,
        end_date=end,
        initial_capital=Decimal("10000000"),
        symbols=["005930"],
        mode=BacktestMode.TECHNICAL,
    )


def _make_metrics(
    start: date,
    end: date,
    total_return_pct: Decimal = Decimal("5.0"),
    total_trades: int = 10,
    winning: int = 6,
    losing: int = 4,
    sharpe: Decimal | None = Decimal("1.2"),
) -> PerformanceMetrics:
    return PerformanceMetrics(
        period_start=start,
        period_end=end,
        total_return_pct=total_return_pct,
        annualized_return_pct=None,
        sharpe_ratio=sharpe,
        sortino_ratio=None,
        max_drawdown_pct=Decimal("3.0"),
        win_rate_pct=Decimal("60.0"),
        avg_win_pct=Decimal("8.0"),
        avg_loss_pct=Decimal("-4.0"),
        profit_factor=Decimal("2.0"),
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
    )


def _make_result(
    config: BacktestConfig,
    metrics: PerformanceMetrics | None = None,
    total_trades: int = 10,
    status: BacktestStatus = BacktestStatus.COMPLETED,
) -> BacktestResult:
    return BacktestResult(
        run_id=uuid4(),
        config=config,
        status=status,
        metrics=metrics,
        trades=[],
        total_trades=total_trades,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )


def _mock_engine_with_returns(oos_returns: list[Decimal]) -> MagicMock:
    """BacktestEngine mock — IS/OOS 교대로 호출되므로 순서대로 결과 반환."""
    engine = MagicMock()
    engine.closed_positions = []
    engine.snapshots = []

    results = []
    for i, oos_ret in enumerate(oos_returns):
        # IS 결과 (항상 양수)
        is_config = _make_config()
        is_metrics = _make_metrics(date(2024, 1, 1), date(2024, 6, 30), Decimal("10.0"))
        results.append(_make_result(is_config, is_metrics))

        # OOS 결과
        oos_config = _make_config()
        winning = 6 if oos_ret > 0 else 2
        losing = 4 if oos_ret > 0 else 8
        oos_metrics = _make_metrics(
            date(2024, 7, 1), date(2024, 8, 31),
            total_return_pct=oos_ret,
            winning=winning,
            losing=losing,
        )
        results.append(_make_result(oos_config, oos_metrics, total_trades=winning + losing))

    engine.run = AsyncMock(side_effect=results)
    return engine


# ══════════════════════════════════════════════════════════════════════
# generate_windows tests
# ══════════════════════════════════════════════════════════════════════


class TestGenerateWindows:
    def test_basic(self):
        """2024-01 ~ 2025-12, IS 6개월 + OOS 2개월 → 3개 윈도우."""
        windows = WalkForwardAnalyzer.generate_windows(
            date(2024, 1, 1), date(2025, 12, 31), 6, 2,
        )

        assert len(windows) == 3

        # 윈도우 0: IS 2024-01-01~2024-06-30, OOS 2024-07-01~2024-08-31
        assert windows[0] == (
            date(2024, 1, 1), date(2024, 6, 30),
            date(2024, 7, 1), date(2024, 8, 31),
        )

        # 윈도우 1: IS 2024-07-01~2024-12-31, OOS 2025-01-01~2025-02-28
        assert windows[1] == (
            date(2024, 7, 1), date(2024, 12, 31),
            date(2025, 1, 1), date(2025, 2, 28),
        )

        # 윈도우 2: IS 2025-01-01~2025-06-30, OOS 2025-07-01~2025-08-31
        assert windows[2] == (
            date(2025, 1, 1), date(2025, 6, 30),
            date(2025, 7, 1), date(2025, 8, 31),
        )

    def test_short_period(self):
        """기간 < IS+OOS → 빈 리스트."""
        windows = WalkForwardAnalyzer.generate_windows(
            date(2024, 1, 1), date(2024, 6, 30), 6, 2,
        )
        assert windows == []

    def test_exact_fit(self):
        """정확히 1개 윈도우만 들어가는 기간."""
        windows = WalkForwardAnalyzer.generate_windows(
            date(2024, 1, 1), date(2024, 8, 31), 6, 2,
        )
        assert len(windows) == 1
        assert windows[0] == (
            date(2024, 1, 1), date(2024, 6, 30),
            date(2024, 7, 1), date(2024, 8, 31),
        )

    def test_no_oos_overlap(self):
        """OOS 구간이 겹치지 않음을 검증."""
        windows = WalkForwardAnalyzer.generate_windows(
            date(2024, 1, 1), date(2025, 12, 31), 6, 2,
        )
        for i in range(len(windows) - 1):
            _, _, _, oos_end = windows[i]
            next_is_start, _, _, _ = windows[i + 1]
            # 다음 윈도우 IS 시작 = 이전 OOS 시작 (IS 겹침 허용)
            # OOS 끝 < 다음 OOS 시작
            _, _, next_oos_start, _ = windows[i + 1]
            assert oos_end < next_oos_start


# ══════════════════════════════════════════════════════════════════════
# run() tests
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestWalkForwardRun:
    async def test_run_mock_engine(self):
        """BacktestEngine.run() Mock → 3 윈도우 WalkForwardResult."""
        engine = _mock_engine_with_returns([
            Decimal("5.0"), Decimal("3.0"), Decimal("-2.0"),
        ])
        analyzer = WalkForwardAnalyzer(engine)
        config = _make_config()

        result = await analyzer.run(config, window_months=6, oos_months=2)

        assert len(result.windows) == 3
        assert result.config == config
        assert result.window_months == 6
        assert result.oos_months == 2
        # engine.run()이 6번 호출됨 (IS 3 + OOS 3)
        assert engine.run.call_count == 6

    async def test_consistency_ratio_calculation(self):
        """3 윈도우 중 2개 양의 수익 → 0.67."""
        engine = _mock_engine_with_returns([
            Decimal("5.0"), Decimal("3.0"), Decimal("-2.0"),
        ])
        analyzer = WalkForwardAnalyzer(engine)

        result = await analyzer.run(_make_config(), window_months=6, oos_months=2)

        assert result.consistency_ratio == Decimal("0.67")

    async def test_is_robust_true(self):
        """ratio >= 0.6 → True."""
        engine = _mock_engine_with_returns([
            Decimal("5.0"), Decimal("3.0"), Decimal("-2.0"),
        ])
        analyzer = WalkForwardAnalyzer(engine)

        result = await analyzer.run(_make_config(), window_months=6, oos_months=2)

        assert result.is_robust is True
        assert result.consistency_ratio >= Decimal("0.6")

    async def test_is_robust_false(self):
        """모두 음의 수익 → ratio=0.0, False."""
        engine = _mock_engine_with_returns([
            Decimal("-1.0"), Decimal("-3.0"), Decimal("-5.0"),
        ])
        analyzer = WalkForwardAnalyzer(engine)

        result = await analyzer.run(_make_config(), window_months=6, oos_months=2)

        assert result.consistency_ratio == Decimal("0")
        assert result.is_robust is False

    async def test_aggregated_oos_trades(self):
        """3 윈도우 OOS 합산 검증."""
        engine = _mock_engine_with_returns([
            Decimal("5.0"), Decimal("3.0"), Decimal("-2.0"),
        ])
        analyzer = WalkForwardAnalyzer(engine)

        result = await analyzer.run(_make_config(), window_months=6, oos_months=2)

        total_oos_trades = sum(w.oos_trade_count for w in result.windows)
        assert total_oos_trades == result.aggregated_oos_metrics.total_trades
        assert result.aggregated_oos_metrics.total_trades > 0

    async def test_avg_oos_return(self):
        """avg_oos_return_pct 검증."""
        engine = _mock_engine_with_returns([
            Decimal("6.0"), Decimal("3.0"), Decimal("-3.0"),
        ])
        analyzer = WalkForwardAnalyzer(engine)

        result = await analyzer.run(_make_config(), window_months=6, oos_months=2)

        # 평균: (6.0 + 3.0 + -3.0) / 3 = 2.0
        assert result.avg_oos_return_pct == Decimal("2.00")

    async def test_empty_windows(self):
        """윈도우가 없는 짧은 기간 → 빈 결과."""
        engine = MagicMock()
        engine.run = AsyncMock()
        analyzer = WalkForwardAnalyzer(engine)

        config = _make_config(start=date(2024, 1, 1), end=date(2024, 6, 30))
        result = await analyzer.run(config, window_months=6, oos_months=2)

        assert len(result.windows) == 0
        assert result.consistency_ratio == Decimal("0")
        assert result.is_robust is False
        engine.run.assert_not_called()
