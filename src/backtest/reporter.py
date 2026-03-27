"""BacktestReporter — 벤치마크 비교 + IS/OOS 분리 + 과적합 감지.

PerformanceCalculator를 래핑하여 백테스트 결과에 대한
확장 리포트(월별 수익률, 전략별 breakdown, IS/OOS 분석)를 생성한다.
Stateless — 모든 메서드가 @staticmethod.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from src.core.models import (
    BacktestReport,
    BacktestResult,
    ComparisonReport,
    OOSSplitResult,
    PerformanceMetrics,
)
from src.report.metrics import PerformanceCalculator

if TYPE_CHECKING:
    from src.db.models.strategy import PortfolioSnapshot, PositionRecord

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")


class BacktestReporter:
    """백테스트 결과 리포트 생성기.

    PerformanceCalculator를 래핑하여 벤치마크 비교,
    IS/OOS 분리, 월별 수익률 분석을 추가한다.
    Stateless — 모든 메서드가 @staticmethod.
    """

    @staticmethod
    def generate_report(
        *,
        result: BacktestResult,
        closed_positions: list[PositionRecord],
        snapshots: list[PortfolioSnapshot],
        is_ratio: Decimal = Decimal("0.7"),
    ) -> BacktestReport:
        """BacktestResult → BacktestReport 변환.

        1. PerformanceCalculator.calculate()로 전체 성과 지표
        2. 벤치마크 대비 초과수익률 패스스루
        3. IS/OOS 분리 (is_ratio)
        4. 월별 수익률 + 전략별 breakdown
        """
        # 성과 지표: engine이 이미 계산한 것 사용, 없으면 폴백
        metrics = result.metrics
        if metrics is None:
            metrics = PerformanceCalculator.calculate(
                closed_positions=closed_positions,
                snapshots=snapshots,
                period_start=result.config.start_date,
                period_end=result.config.end_date,
            )

        # 월별 수익률 (스냅샷 기반)
        monthly_returns = BacktestReporter._calculate_monthly_returns(snapshots)

        # 전략별 breakdown
        strategy_breakdown = PerformanceCalculator.breakdown_by_strategy(
            closed_positions,
        )

        # IS/OOS 분리
        oos_split = BacktestReporter._split_is_oos(
            closed_positions=closed_positions,
            snapshots=snapshots,
            period_start=result.config.start_date,
            period_end=result.config.end_date,
            is_ratio=is_ratio,
        )

        return BacktestReport(
            run_id=result.run_id,
            config=result.config,
            metrics=metrics,
            benchmark_metrics=result.benchmark_metrics,
            excess_return_pct=result.excess_return_pct,
            monthly_returns=monthly_returns,
            strategy_breakdown=strategy_breakdown,
            oos_split=oos_split,
            trade_count=len(closed_positions),
            generated_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _split_is_oos(
        *,
        closed_positions: list[PositionRecord],
        snapshots: list[PortfolioSnapshot],
        period_start: date,
        period_end: date,
        is_ratio: Decimal,
    ) -> OOSSplitResult | None:
        """IS/OOS 분리. 거래일 기준 is_ratio로 분할.

        과적합 판단: OOS total_return < IS total_return * 0.5 → is_overfit=True.
        양쪽 스냅샷이 2개 미만이면 None 반환.
        """
        sorted_snaps = sorted(snapshots, key=lambda s: s.snapshot_date)
        n = len(sorted_snaps)
        if n < 4:  # 최소 양쪽 2개씩
            return None

        split_index = int(n * float(is_ratio))
        if split_index < 2 or split_index >= n - 1:
            return None

        split_date = sorted_snaps[split_index].snapshot_date

        # 스냅샷 분할
        is_snapshots = sorted_snaps[:split_index]
        oos_snapshots = sorted_snaps[split_index:]

        # 포지션 분할 (exit_date 기준)
        is_positions = [
            p for p in closed_positions
            if p.exit_date is not None and p.exit_date < split_date
        ]
        oos_positions = [
            p for p in closed_positions
            if p.exit_date is not None and p.exit_date >= split_date
        ]

        # IS 성과
        is_metrics = PerformanceCalculator.calculate(
            closed_positions=is_positions,
            snapshots=is_snapshots,
            period_start=period_start,
            period_end=split_date,
        )

        # OOS 성과
        oos_metrics = PerformanceCalculator.calculate(
            closed_positions=oos_positions,
            snapshots=oos_snapshots,
            period_start=split_date,
            period_end=period_end,
        )

        # 과적합 판단
        is_overfit = oos_metrics.total_return_pct < is_metrics.total_return_pct * Decimal("0.5")

        return OOSSplitResult(
            split_date=split_date,
            is_ratio=is_ratio,
            in_sample_metrics=is_metrics,
            out_of_sample_metrics=oos_metrics,
            is_overfit=is_overfit,
        )

    @staticmethod
    def _calculate_monthly_returns(
        snapshots: list[PortfolioSnapshot],
    ) -> dict[str, Decimal]:
        """월별 수익률. {"2024-01": Decimal("3.45"), ...}

        각 월의 첫/마지막 스냅샷 total_value로 수익률 계산.
        """
        if not snapshots:
            return {}

        sorted_snaps = sorted(snapshots, key=lambda s: s.snapshot_date)

        # YYYY-MM 그룹화
        groups: dict[str, list] = defaultdict(list)
        for snap in sorted_snaps:
            key = snap.snapshot_date.strftime("%Y-%m")
            groups[key].append(snap)

        result: dict[str, Decimal] = {}
        for month_key in sorted(groups):
            month_snaps = groups[month_key]
            if len(month_snaps) < 2:
                result[month_key] = _ZERO
                continue

            first_val = month_snaps[0].total_value
            last_val = month_snaps[-1].total_value

            if first_val == _ZERO:
                continue

            ret = ((last_val - first_val) / first_val * _HUNDRED).quantize(
                _Q2, rounding=ROUND_HALF_UP,
            )
            result[month_key] = ret

        return result

    @staticmethod
    def compare_runs(
        results: list[BacktestResult],
        closed_positions_map: dict[UUID, list[PositionRecord]],
        snapshots_map: dict[UUID, list[PortfolioSnapshot]],
    ) -> ComparisonReport:
        """복수 백테스트 결과 비교 리포트."""
        reports: list[BacktestReport] = []
        for res in results:
            positions = closed_positions_map.get(res.run_id, [])
            snaps = snapshots_map.get(res.run_id, [])
            report = BacktestReporter.generate_report(
                result=res,
                closed_positions=positions,
                snapshots=snaps,
            )
            reports.append(report)

        # 최고 Sharpe
        best_sharpe_run_id: UUID | None = None
        best_sharpe = None
        for r in reports:
            if r.metrics.sharpe_ratio is not None:
                if best_sharpe is None or r.metrics.sharpe_ratio > best_sharpe:
                    best_sharpe = r.metrics.sharpe_ratio
                    best_sharpe_run_id = r.run_id

        # 최고 수익률
        best_return_run_id: UUID | None = None
        best_return = None
        for r in reports:
            if best_return is None or r.metrics.total_return_pct > best_return:
                best_return = r.metrics.total_return_pct
                best_return_run_id = r.run_id

        # 최저 MDD
        lowest_mdd_run_id: UUID | None = None
        lowest_mdd = None
        for r in reports:
            if lowest_mdd is None or r.metrics.max_drawdown_pct < lowest_mdd:
                lowest_mdd = r.metrics.max_drawdown_pct
                lowest_mdd_run_id = r.run_id

        return ComparisonReport(
            reports=reports,
            best_sharpe_run_id=best_sharpe_run_id,
            best_return_run_id=best_return_run_id,
            lowest_mdd_run_id=lowest_mdd_run_id,
            generated_at=datetime.now(timezone.utc),
        )
