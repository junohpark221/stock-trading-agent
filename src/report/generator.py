"""Report generator — orchestrates data fetching + metrics calculation.

데이터 조회(ReportDataFetcher) → 지표 계산(PerformanceCalculator) → Pydantic 모델 반환.
Step 5 MessageTemplates에서 메시지 포맷팅, Step 7 Job 함수에서 호출.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import structlog

from src.core.enums import OrderStatus
from src.core.models import DailyReportData, PerformanceMetrics, WeeklyReportData
from src.report.data_fetcher import ReportDataFetcher
from src.report.metrics import PerformanceCalculator

if TYPE_CHECKING:
    from src.config import Settings
    from src.llm.cost_tracker import CostTracker

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_Q2 = Decimal("0.01")


class ReportGenerator:
    """일간/주간/월간/LLM비용 리포트 생성기."""

    def __init__(
        self,
        *,
        data_fetcher: ReportDataFetcher,
        cost_tracker: CostTracker,
        settings: Settings,
    ) -> None:
        self._fetcher = data_fetcher
        self._cost_tracker = cost_tracker
        self._settings = settings

    # ------------------------------------------------------------------
    # Daily Report
    # ------------------------------------------------------------------

    async def generate_daily_report(
        self,
        *,
        report_date: date | None = None,
        account_id: str | None = None,
    ) -> DailyReportData:
        """일간 리포트 데이터 생성.

        snapshot 없는 첫날에도 에러 없이 0값 반환.
        """
        if report_date is None:
            report_date = date.today()

        # 데이터 수집
        snapshot = await self._fetcher.get_latest_snapshot(account_id=account_id)
        all_orders = await self._fetcher.get_todays_orders(account_id=account_id)
        open_positions = await self._fetcher.get_open_positions(account_id=account_id)
        budget_status = await self._cost_tracker.get_budget_status()
        usage_stats = await self._cost_tracker.get_usage_stats(
            start_date=report_date, end_date=report_date,
        )

        # snapshot 기반 필드 (없으면 0)
        if snapshot is not None:
            total_value = snapshot.total_value
            cash = snapshot.cash
            invested = snapshot.invested
            unrealized_pnl = snapshot.unrealized_pnl
            realized_pnl = snapshot.realized_pnl_daily
            positions_count = snapshot.positions_count
            raw_sectors = snapshot.sector_allocations or {}
        else:
            total_value = _ZERO
            cash = _ZERO
            invested = _ZERO
            unrealized_pnl = _ZERO
            realized_pnl = _ZERO
            positions_count = len(open_positions)
            raw_sectors = {}

        # 비율 계산
        realized_pnl_pct = (
            (realized_pnl / total_value * _HUNDRED).quantize(_Q2, rounding=ROUND_HALF_UP)
            if total_value > _ZERO
            else _ZERO
        )
        unrealized_pnl_pct = (
            (unrealized_pnl / invested * _HUNDRED).quantize(_Q2, rounding=ROUND_HALF_UP)
            if invested > _ZERO
            else _ZERO
        )
        cash_pct = (
            (cash / total_value * _HUNDRED).quantize(_Q2, rounding=ROUND_HALF_UP)
            if total_value > _ZERO
            else _ZERO
        )

        # 누적 수익률 (INITIAL_CAPITAL 기준)
        initial_capital = self._settings.INITIAL_CAPITAL
        cumulative_return_pct = (
            ((total_value - initial_capital) / initial_capital * _HUNDRED).quantize(
                _Q2, rounding=ROUND_HALF_UP,
            )
            if initial_capital > _ZERO and total_value > _ZERO
            else _ZERO
        )

        # trades_today — 체결(FILLED) 주문만 포함
        trades_today = [
            {
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.filled_quantity or o.quantity,
                "price": str(o.filled_price or o.price),
                "status": o.status,
                "filled_price": str(o.filled_price) if o.filled_price else None,
                "executed_at": o.executed_at.isoformat() if o.executed_at else None,
            }
            for o in all_orders
            if o.status == OrderStatus.FILLED.value
        ]
        pending_orders_count = sum(
            1 for o in all_orders if o.status == OrderStatus.SUBMITTED.value
        )
        cancelled_orders_count = sum(
            1 for o in all_orders if o.status == OrderStatus.CANCELLED.value
        )

        # sector_allocations (JSONB float → Decimal 변환)
        sector_allocations = {
            k: Decimal(str(v)) for k, v in raw_sectors.items()
        }

        # LLM 비용
        llm_cost_today_usd = sum(
            Decimal(str(s.get("cost_usd", "0"))) for s in usage_stats
        )

        # 경고 생성
        warnings = self._generate_warnings(
            sector_allocations=sector_allocations,
            budget_status=budget_status,
        )

        return DailyReportData(
            report_date=report_date,
            realized_pnl=realized_pnl,
            realized_pnl_pct=realized_pnl_pct,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
            total_value=total_value,
            cash=cash,
            cash_pct=cash_pct,
            positions_count=positions_count,
            trades_today=trades_today,
            pending_orders_count=pending_orders_count,
            cancelled_orders_count=cancelled_orders_count,
            cumulative_return_pct=cumulative_return_pct,
            sector_allocations=sector_allocations,
            warnings=warnings,
            llm_cost_today_usd=llm_cost_today_usd,
            llm_cost_monthly_usd=budget_status.current_month_cost_usd,
            llm_budget_usd=budget_status.budget_usd,
        )

    # ------------------------------------------------------------------
    # Weekly Report
    # ------------------------------------------------------------------

    async def generate_weekly_report(
        self,
        *,
        week_end: date | None = None,
        account_id: str | None = None,
    ) -> WeeklyReportData:
        """주간 리포트 데이터 생성.

        week_end 기본값 = today, week_start = week_end - 6일.
        """
        if week_end is None:
            week_end = date.today()
        week_start = week_end - timedelta(days=6)

        closed_positions = await self._fetcher.get_closed_positions(
            start_date=week_start, end_date=week_end, account_id=account_id,
        )
        snapshots = await self._fetcher.get_portfolio_snapshots(
            start_date=week_start, end_date=week_end, account_id=account_id,
        )

        risk_free = Decimal(str(self._settings.RISK_FREE_RATE_PCT))
        performance = PerformanceCalculator.calculate(
            closed_positions=closed_positions,
            snapshots=snapshots,
            period_start=week_start,
            period_end=week_end,
            risk_free_rate_pct=risk_free,
        )

        best_trade = None
        worst_trade = None
        if closed_positions:
            best = max(closed_positions, key=lambda p: p.realized_pnl or _ZERO)
            worst = min(closed_positions, key=lambda p: p.realized_pnl or _ZERO)
            best_trade = self._position_to_trade_dict(best)
            worst_trade = self._position_to_trade_dict(worst)

        strategy_comparison = PerformanceCalculator.breakdown_by_strategy(
            closed_positions,
        )

        return WeeklyReportData(
            week_start=week_start,
            week_end=week_end,
            performance=performance,
            best_trade=best_trade,
            worst_trade=worst_trade,
            strategy_comparison=strategy_comparison,
        )

    # ------------------------------------------------------------------
    # Monthly Report
    # ------------------------------------------------------------------

    async def generate_monthly_report(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        account_id: str | None = None,
    ) -> tuple[PerformanceMetrics, DailyReportData]:
        """월간 리포트 = PerformanceMetrics(월간) + DailyReportData(현재 상태)."""
        today = date.today()
        if year is None:
            year = today.year
        if month is None:
            month = today.month

        period_start = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        period_end = date(year, month, last_day)

        closed_positions = await self._fetcher.get_closed_positions(
            start_date=period_start, end_date=period_end, account_id=account_id,
        )
        snapshots = await self._fetcher.get_portfolio_snapshots(
            start_date=period_start, end_date=period_end, account_id=account_id,
        )

        risk_free = Decimal(str(self._settings.RISK_FREE_RATE_PCT))
        metrics = PerformanceCalculator.calculate(
            closed_positions=closed_positions,
            snapshots=snapshots,
            period_start=period_start,
            period_end=period_end,
            risk_free_rate_pct=risk_free,
        )

        daily_data = await self.generate_daily_report(account_id=account_id)

        return (metrics, daily_data)

    # ------------------------------------------------------------------
    # LLM Cost Report
    # ------------------------------------------------------------------

    async def generate_llm_cost_report(self) -> dict:
        """LLM 비용 리포트 데이터 생성."""
        today = date.today()

        budget_status = await self._cost_tracker.get_budget_status()
        monthly_summary = await self._cost_tracker.get_monthly_summary(
            year=today.year, month=today.month,
        )
        recent_usage = await self._cost_tracker.get_usage_stats(
            start_date=today - timedelta(days=7), end_date=today,
        )

        return {
            "budget_status": budget_status,
            "monthly_summary": monthly_summary,
            "recent_usage": recent_usage,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _position_to_trade_dict(position: object) -> dict:
        """PositionRecord → best/worst trade dict 변환."""
        entry_price = getattr(position, "entry_price", None) or _ZERO
        exit_price = getattr(position, "exit_price", None) or _ZERO
        pnl_pct = (
            ((exit_price - entry_price) / entry_price * _HUNDRED).quantize(
                _Q2, rounding=ROUND_HALF_UP,
            )
            if entry_price > _ZERO
            else _ZERO
        )
        return {
            "symbol": position.symbol,
            "strategy": position.strategy_type,
            "pnl": position.realized_pnl or _ZERO,
            "pnl_pct": pnl_pct,
            "entry_date": str(position.entry_date),
            "exit_date": str(position.exit_date),
        }

    def _generate_warnings(
        self,
        *,
        sector_allocations: dict[str, Decimal],
        budget_status: object,
    ) -> list[str]:
        """경고 메시지 목록 생성."""
        warnings: list[str] = []

        # 섹터 비중 경고
        warn_pct = Decimal(str(self._settings.MONITOR_SECTOR_WEIGHT_WARN_PCT))
        for sector, weight in sector_allocations.items():
            if weight > warn_pct:
                warnings.append(
                    f"섹터 비중 경고: {sector} {weight}% (임계치 {warn_pct}%)"
                )

        # LLM 예산 경고
        budget_warn_pct = Decimal(str(self._settings.MONITOR_LLM_BUDGET_WARN_PCT))
        usage_pct = getattr(budget_status, "usage_percent", _ZERO)
        if usage_pct >= budget_warn_pct:
            warnings.append(
                f"LLM 예산 경고: 사용률 {usage_pct:.1f}% (임계치 {budget_warn_pct}%)"
            )

        return warnings
