"""Walk-forward 분석기.

전체 기간을 [IS window_months | OOS oos_months] 슬라이딩 윈도우로 분할,
각 윈도우별 백테스트 실행 후 OOS 결과를 집계하여 견고성을 판단한다.

Usage::

    analyzer = WalkForwardAnalyzer(engine)
    result = await analyzer.run(config, window_months=6, oos_months=2)
    print(result.consistency_ratio, result.is_robust)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import structlog
from dateutil.relativedelta import relativedelta

from src.core.enums import BacktestStatus
from src.core.models import (
    PerformanceMetrics,
    WalkForwardResult,
    WalkForwardWindow,
)
from src.report.metrics import PerformanceCalculator

if TYPE_CHECKING:
    from src.backtest.engine import BacktestEngine
    from src.core.models import BacktestConfig

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_Q2 = Decimal("0.01")
_ROBUSTNESS_THRESHOLD = Decimal("0.6")


class WalkForwardAnalyzer:
    """Walk-forward 분석기.

    전체 기간을 [IS window_months | OOS oos_months] 윈도우로 분할,
    각 윈도우별 백테스트 실행 후 OOS 결과 집계.
    """

    def __init__(self, engine: BacktestEngine) -> None:
        self._engine = engine

    async def run(
        self,
        config: BacktestConfig,
        *,
        window_months: int = 6,
        oos_months: int = 2,
    ) -> WalkForwardResult:
        """Walk-forward 분석 실행.

        1. 날짜 범위를 윈도우로 분할
        2. 각 윈도우: IS run() → OOS run()
        3. 전체 OOS 결과 집계
        4. consistency_ratio 계산 (양의 수익률 비율)
        5. is_robust = ratio >= 0.6
        """
        windows_dates = self.generate_windows(
            config.start_date, config.end_date, window_months, oos_months,
        )

        logger.info(
            "walk_forward_started",
            total_windows=len(windows_dates),
            window_months=window_months,
            oos_months=oos_months,
            start=str(config.start_date),
            end=str(config.end_date),
        )

        windows: list[WalkForwardWindow] = []
        all_oos_positions: list = []
        all_oos_snapshots: list = []

        for idx, (is_start, is_end, oos_start, oos_end) in enumerate(windows_dates):
            # IS 구간 백테스트
            is_config = config.model_copy(
                update={"start_date": is_start, "end_date": is_end},
            )
            is_result = await self._engine.run(is_config)

            # OOS 구간 백테스트
            oos_config = config.model_copy(
                update={"start_date": oos_start, "end_date": oos_end},
            )
            oos_result = await self._engine.run(oos_config)

            # 메트릭 추출 (실패 시 빈 메트릭)
            is_metrics = is_result.metrics or _empty_metrics(is_start, is_end)
            oos_metrics = oos_result.metrics or _empty_metrics(oos_start, oos_end)

            windows.append(WalkForwardWindow(
                window_index=idx,
                is_start=is_start,
                is_end=is_end,
                oos_start=oos_start,
                oos_end=oos_end,
                is_metrics=is_metrics,
                oos_metrics=oos_metrics,
                oos_trade_count=oos_result.total_trades,
            ))

            # OOS 포지션/스냅샷 수집 (집계 메트릭용)
            if oos_result.status == BacktestStatus.COMPLETED:
                all_oos_positions.extend(self._engine.closed_positions)
                all_oos_snapshots.extend(self._engine.snapshots)

            logger.debug(
                "walk_forward_window_done",
                window=idx,
                is_return=str(is_metrics.total_return_pct),
                oos_return=str(oos_metrics.total_return_pct),
            )

        # ── 집계 ──────────────────────────────────────────────────
        aggregated = _aggregate_oos_metrics(windows, config.start_date, config.end_date)

        # consistency_ratio: 양의 수익률 OOS 비율
        if windows:
            positive_count = sum(
                1 for w in windows if w.oos_metrics.total_return_pct > _ZERO
            )
            consistency_ratio = (
                Decimal(positive_count) / Decimal(len(windows))
            ).quantize(_Q2, rounding=ROUND_HALF_UP)

            avg_oos_return = (
                sum(w.oos_metrics.total_return_pct for w in windows)
                / Decimal(len(windows))
            ).quantize(_Q2, rounding=ROUND_HALF_UP)
        else:
            consistency_ratio = _ZERO
            avg_oos_return = _ZERO

        is_robust = consistency_ratio >= _ROBUSTNESS_THRESHOLD

        logger.info(
            "walk_forward_completed",
            windows=len(windows),
            consistency_ratio=str(consistency_ratio),
            avg_oos_return=str(avg_oos_return),
            is_robust=is_robust,
        )

        return WalkForwardResult(
            config=config,
            window_months=window_months,
            oos_months=oos_months,
            windows=windows,
            aggregated_oos_metrics=aggregated,
            consistency_ratio=consistency_ratio,
            avg_oos_return_pct=avg_oos_return,
            is_robust=is_robust,
            generated_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def generate_windows(
        start_date: date,
        end_date: date,
        window_months: int,
        oos_months: int,
    ) -> list[tuple[date, date, date, date]]:
        """날짜 범위 → (is_start, is_end, oos_start, oos_end) 리스트.

        dateutil.relativedelta로 월 단위 이동.
        마지막 OOS가 end_date 초과 시 해당 윈도우 제외.
        슬라이딩: 다음 IS 시작 = 이전 OOS 시작 (IS 겹침, OOS 비겹침).
        """
        result: list[tuple[date, date, date, date]] = []
        cursor = start_date

        while True:
            is_start = cursor
            is_end = is_start + relativedelta(months=window_months) - relativedelta(days=1)
            oos_start = is_end + relativedelta(days=1)
            oos_end = oos_start + relativedelta(months=oos_months) - relativedelta(days=1)

            # OOS 종료가 전체 기간 초과 시 중단
            if oos_end > end_date:
                break

            result.append((is_start, is_end, oos_start, oos_end))

            # 다음 윈도우: OOS 시작점에서 새로 시작
            cursor = oos_start

        return result


def _empty_metrics(period_start: date, period_end: date) -> PerformanceMetrics:
    """실패/빈 결과용 기본 메트릭."""
    return PerformanceMetrics(
        period_start=period_start,
        period_end=period_end,
        total_return_pct=_ZERO,
        max_drawdown_pct=_ZERO,
        win_rate_pct=_ZERO,
        avg_win_pct=_ZERO,
        avg_loss_pct=_ZERO,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
    )


def _aggregate_oos_metrics(
    windows: list[WalkForwardWindow],
    fallback_start: date,
    fallback_end: date,
) -> PerformanceMetrics:
    """전체 OOS 윈도우 메트릭을 가중평균으로 집계."""
    if not windows:
        return _empty_metrics(fallback_start, fallback_end)

    total_trades = sum(w.oos_metrics.total_trades for w in windows)
    winning = sum(w.oos_metrics.winning_trades for w in windows)
    losing = sum(w.oos_metrics.losing_trades for w in windows)

    n = Decimal(len(windows))

    avg_return = (
        sum(w.oos_metrics.total_return_pct for w in windows) / n
    ).quantize(_Q2, rounding=ROUND_HALF_UP)

    avg_mdd = (
        sum(w.oos_metrics.max_drawdown_pct for w in windows) / n
    ).quantize(_Q2, rounding=ROUND_HALF_UP)

    # Sharpe/Sortino: None이 아닌 값들의 평균
    sharpe_vals = [w.oos_metrics.sharpe_ratio for w in windows if w.oos_metrics.sharpe_ratio is not None]
    avg_sharpe = (
        (sum(sharpe_vals) / Decimal(len(sharpe_vals))).quantize(_Q2, rounding=ROUND_HALF_UP)
        if sharpe_vals else None
    )

    sortino_vals = [w.oos_metrics.sortino_ratio for w in windows if w.oos_metrics.sortino_ratio is not None]
    avg_sortino = (
        (sum(sortino_vals) / Decimal(len(sortino_vals))).quantize(_Q2, rounding=ROUND_HALF_UP)
        if sortino_vals else None
    )

    win_rate = (
        (Decimal(winning) / Decimal(total_trades) * Decimal("100")).quantize(_Q2, rounding=ROUND_HALF_UP)
        if total_trades > 0 else _ZERO
    )

    avg_win = (
        sum(w.oos_metrics.avg_win_pct for w in windows) / n
    ).quantize(_Q2, rounding=ROUND_HALF_UP)

    avg_loss = (
        sum(w.oos_metrics.avg_loss_pct for w in windows) / n
    ).quantize(_Q2, rounding=ROUND_HALF_UP)

    # Profit factor: 총 이익 / 총 손실 근사
    pf_vals = [w.oos_metrics.profit_factor for w in windows if w.oos_metrics.profit_factor is not None]
    avg_pf = (
        (sum(pf_vals) / Decimal(len(pf_vals))).quantize(_Q2, rounding=ROUND_HALF_UP)
        if pf_vals else None
    )

    return PerformanceMetrics(
        period_start=windows[0].oos_start,
        period_end=windows[-1].oos_end,
        total_return_pct=avg_return,
        annualized_return_pct=None,
        sharpe_ratio=avg_sharpe,
        sortino_ratio=avg_sortino,
        max_drawdown_pct=avg_mdd,
        win_rate_pct=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        profit_factor=avg_pf,
        total_trades=total_trades,
        winning_trades=winning,
        losing_trades=losing,
    )
