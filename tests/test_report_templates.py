"""Tests for report + monitoring message templates — Phase 6 Step 5."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.enums import MonitoringAlertType
from src.core.models import DailyReportData, PerformanceMetrics, WeeklyReportData
from src.notification.templates import MessageTemplates

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def performance_metrics() -> PerformanceMetrics:
    return PerformanceMetrics(
        period_start=date(2026, 3, 23),
        period_end=date(2026, 3, 27),
        total_return_pct=Decimal("2.35"),
        annualized_return_pct=Decimal("63.60"),
        sharpe_ratio=Decimal("1.85"),
        sortino_ratio=Decimal("2.10"),
        max_drawdown_pct=Decimal("-1.20"),
        win_rate_pct=Decimal("65.00"),
        avg_win_pct=Decimal("3.50"),
        avg_loss_pct=Decimal("-1.80"),
        profit_factor=Decimal("2.50"),
        total_trades=20,
        winning_trades=13,
        losing_trades=7,
    )


@pytest.fixture()
def daily_report_data() -> DailyReportData:
    return DailyReportData(
        report_date=date(2026, 3, 27),
        realized_pnl=Decimal("150000"),
        realized_pnl_pct=Decimal("1.25"),
        unrealized_pnl=Decimal("-30000"),
        unrealized_pnl_pct=Decimal("-0.50"),
        total_value=Decimal("20000000"),
        cash=Decimal("8000000"),
        cash_pct=Decimal("40.00"),
        positions_count=5,
        trades_today=[
            {
                "symbol": "005930", "side": "buy",
                "quantity": 10, "price": "72000", "status": "filled",
            },
            {
                "symbol": "373220", "side": "sell",
                "quantity": 5, "price": "580000", "status": "filled",
            },
        ],
        cumulative_return_pct=Decimal("5.30"),
        sector_allocations={"반도체": Decimal("30.50"), "바이오": Decimal("20.00")},
        warnings=["섹터 비중 경고: 반도체 30.50% (임계치 25.00%)"],
        llm_cost_today_usd=Decimal("1.25"),
        llm_cost_monthly_usd=Decimal("18.50"),
        llm_budget_usd=Decimal("50.00"),
    )


@pytest.fixture()
def weekly_report_data(performance_metrics: PerformanceMetrics) -> WeeklyReportData:
    return WeeklyReportData(
        week_start=date(2026, 3, 23),
        week_end=date(2026, 3, 27),
        performance=performance_metrics,
        best_trade={
            "symbol": "005930",
            "strategy": "position",
            "pnl": Decimal("150000"),
            "pnl_pct": Decimal("5.30"),
        },
        worst_trade={
            "symbol": "035720",
            "strategy": "swing",
            "pnl": Decimal("-80000"),
            "pnl_pct": Decimal("-3.20"),
        },
        strategy_comparison={
            "position": {
                "trade_count": 8, "win_rate_pct": Decimal("75.00"),
                "avg_pnl_pct": Decimal("3.20"), "total_pnl": Decimal("500000"),
            },
            "swing": {
                "trade_count": 12, "win_rate_pct": Decimal("58.33"),
                "avg_pnl_pct": Decimal("1.50"), "total_pnl": Decimal("200000"),
            },
        },
    )


@pytest.fixture()
def llm_cost_data() -> dict:
    return {
        "budget_status": SimpleNamespace(
            current_month_cost_usd=Decimal("18.50"),
            budget_usd=Decimal("50.00"),
            usage_percent=Decimal("37.00"),
        ),
        "monthly_summary": {
            "total_cost_usd": "18.50",
            "total_calls": 73,
            "by_provider": {
                "openai": {"cost_usd": "12.30", "call_count": 45},
                "anthropic": {"cost_usd": "6.20", "call_count": 28},
            },
        },
        "recent_usage": [
            SimpleNamespace(date=date(2026, 3, 27), cost_usd=Decimal("2.50")),
            SimpleNamespace(date=date(2026, 3, 26), cost_usd=Decimal("3.10")),
            SimpleNamespace(date=date(2026, 3, 25), cost_usd=Decimal("1.80")),
        ],
    }


# ---------------------------------------------------------------------------
# TestHelpers
# ---------------------------------------------------------------------------


class TestHelpers:
    """New helper method tests."""

    def test_fmt_usd_normal(self) -> None:
        assert MessageTemplates._fmt_usd(Decimal("1234.56")) == "$1,234.56"

    def test_fmt_usd_zero(self) -> None:
        assert MessageTemplates._fmt_usd(Decimal("0")) == "$0.00"

    def test_fmt_usd_small(self) -> None:
        assert MessageTemplates._fmt_usd(Decimal("0.05")) == "$0.05"

    def test_progress_bar_empty(self) -> None:
        result = MessageTemplates._progress_bar(Decimal("0"))
        assert result == "░░░░░░░░░░ 0%"

    def test_progress_bar_half(self) -> None:
        result = MessageTemplates._progress_bar(Decimal("0.5"))
        assert result == "█████░░░░░ 50%"

    def test_progress_bar_full(self) -> None:
        result = MessageTemplates._progress_bar(Decimal("1"))
        assert result == "██████████ 100%"

    def test_progress_bar_overflow_clamped(self) -> None:
        result = MessageTemplates._progress_bar(Decimal("1.5"))
        assert result == "██████████ 100%"

    def test_pnl_sign_positive(self) -> None:
        assert MessageTemplates._pnl_sign(Decimal("100")) == "+"

    def test_pnl_sign_negative(self) -> None:
        assert MessageTemplates._pnl_sign(Decimal("-100")) == ""

    def test_pnl_sign_zero(self) -> None:
        assert MessageTemplates._pnl_sign(Decimal("0")) == ""


# ---------------------------------------------------------------------------
# TestDailyReport
# ---------------------------------------------------------------------------


class TestDailyReport:
    """daily_report() tests."""

    def test_header(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "일간 리포트" in result
        assert "2026-03-27" in result

    def test_pnl_section(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "실현 손익" in result
        assert "150,000원" in result
        assert "미실현 손익" in result
        assert "-30,000원" in result

    def test_pnl_sign_positive(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "+150,000원" in result
        assert "+1.25%" in result

    def test_cumulative_return(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "누적 수익률" in result
        assert "+5.30%" in result

    def test_trades_listed(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "005930" in result
        assert "373220" in result
        assert "2건" in result

    def test_empty_trades(self, daily_report_data: DailyReportData) -> None:
        daily_report_data.trades_today = []
        result = MessageTemplates.daily_report(daily_report_data)
        assert "거래 없음" in result

    def test_portfolio_section(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "포트폴리오 현황" in result
        assert "20,000,000원" in result
        assert "8,000,000원" in result
        assert "5개" in result

    def test_sector_allocations(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "반도체" in result
        assert "30.50%" in result

    def test_empty_sectors(self, daily_report_data: DailyReportData) -> None:
        daily_report_data.sector_allocations = {}
        result = MessageTemplates.daily_report(daily_report_data)
        assert "섹터:" not in result

    def test_warnings(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "주의사항" in result
        assert "섹터 비중 경고" in result

    def test_no_warnings(self, daily_report_data: DailyReportData) -> None:
        daily_report_data.warnings = []
        result = MessageTemplates.daily_report(daily_report_data)
        assert "주의사항" not in result

    def test_llm_cost_section(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert "LLM 비용" in result
        assert "$1.25" in result
        assert "$18.50" in result
        assert "$50.00" in result

    def test_llm_cost_hidden_when_no_budget(self, daily_report_data: DailyReportData) -> None:
        daily_report_data.llm_budget_usd = Decimal(0)
        result = MessageTemplates.daily_report(daily_report_data)
        assert "LLM 비용" not in result

    def test_html_tags_valid(self, daily_report_data: DailyReportData) -> None:
        result = MessageTemplates.daily_report(daily_report_data)
        assert result.count("<b>") == result.count("</b>")


# ---------------------------------------------------------------------------
# TestWeeklyReport
# ---------------------------------------------------------------------------


class TestWeeklyReport:
    """weekly_report() tests."""

    def test_header(self, weekly_report_data: WeeklyReportData) -> None:
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "주간 리포트" in result
        assert "2026-03-23" in result
        assert "2026-03-27" in result

    def test_performance_metrics(self, weekly_report_data: WeeklyReportData) -> None:
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "+2.35%" in result
        assert "-1.20%" in result
        assert "65.00%" in result
        assert "20건" in result

    def test_sharpe_shown(self, weekly_report_data: WeeklyReportData) -> None:
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "Sharpe Ratio: 1.85" in result

    def test_optional_metrics_hidden(self, weekly_report_data: WeeklyReportData) -> None:
        weekly_report_data.performance.sharpe_ratio = None
        weekly_report_data.performance.profit_factor = None
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "Sharpe" not in result
        assert "Profit Factor" not in result

    def test_best_worst_trade(self, weekly_report_data: WeeklyReportData) -> None:
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "005930" in result
        assert "+5.30%" in result
        assert "035720" in result
        assert "-3.20%" in result

    def test_best_worst_none(self, weekly_report_data: WeeklyReportData) -> None:
        weekly_report_data.best_trade = None
        weekly_report_data.worst_trade = None
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "최고" not in result
        assert "최악" not in result

    def test_strategy_comparison(self, weekly_report_data: WeeklyReportData) -> None:
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "전략별 비교" in result
        assert "position" in result
        assert "swing" in result

    def test_empty_strategy_comparison(self, weekly_report_data: WeeklyReportData) -> None:
        weekly_report_data.strategy_comparison = {}
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert "전략별 비교" not in result

    def test_html_tags_valid(self, weekly_report_data: WeeklyReportData) -> None:
        result = MessageTemplates.weekly_report(weekly_report_data)
        assert result.count("<b>") == result.count("</b>")


# ---------------------------------------------------------------------------
# TestMonthlyReport
# ---------------------------------------------------------------------------


class TestMonthlyReport:
    """monthly_report() tests."""

    def test_header(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "월간 리포트" in result
        assert "2026-03-23" in result

    def test_detailed_metrics(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "Sharpe Ratio: 1.85" in result
        assert "Sortino Ratio: 2.10" in result
        assert "Profit Factor: 2.50" in result
        assert "+63.60%" in result

    def test_optional_metrics_hidden(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        performance_metrics.sharpe_ratio = None
        performance_metrics.sortino_ratio = None
        performance_metrics.profit_factor = None
        performance_metrics.annualized_return_pct = None
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "Sharpe" not in result
        assert "Sortino" not in result
        assert "Profit Factor" not in result
        assert "연환산" not in result

    def test_trade_statistics(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "20건" in result
        assert "13승 7패" in result
        assert "+3.50%" in result
        assert "-1.80%" in result

    def test_portfolio_snapshot(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "20,000,000원" in result
        assert "8,000,000원" in result
        assert "5개" in result

    def test_llm_cost_shown(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "$18.50" in result

    def test_llm_cost_hidden_when_no_budget(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        daily_report_data.llm_budget_usd = Decimal(0)
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert "LLM 비용" not in result

    def test_html_tags_valid(
        self,
        performance_metrics: PerformanceMetrics,
        daily_report_data: DailyReportData,
    ) -> None:
        result = MessageTemplates.monthly_report(
            metrics=performance_metrics, summary=daily_report_data,
        )
        assert result.count("<b>") == result.count("</b>")


# ---------------------------------------------------------------------------
# TestLLMCostReport
# ---------------------------------------------------------------------------


class TestLLMCostReport:
    """llm_cost_report() tests."""

    def test_header(self, llm_cost_data: dict) -> None:
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "LLM 비용 리포트" in result

    def test_budget_progress_bar(self, llm_cost_data: dict) -> None:
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "$18.50" in result
        assert "$50.00" in result
        assert "█" in result
        assert "░" in result

    def test_provider_breakdown(self, llm_cost_data: dict) -> None:
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "openai" in result
        assert "$12.30" in result
        assert "45건" in result
        assert "anthropic" in result
        assert "$6.20" in result

    def test_daily_trend(self, llm_cost_data: dict) -> None:
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "최근 7일" in result
        assert "03-27" in result
        assert "$2.50" in result

    def test_zero_budget(self, llm_cost_data: dict) -> None:
        llm_cost_data["budget_status"] = SimpleNamespace(
            current_month_cost_usd=Decimal("5.00"),
            budget_usd=Decimal("0"),
        )
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "예산 미설정" in result

    def test_empty_providers(self, llm_cost_data: dict) -> None:
        llm_cost_data["monthly_summary"]["by_provider"] = {}
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "데이터 없음" in result

    def test_empty_recent_usage(self, llm_cost_data: dict) -> None:
        llm_cost_data["recent_usage"] = []
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert "최근 7일" not in result

    def test_html_tags_valid(self, llm_cost_data: dict) -> None:
        result = MessageTemplates.llm_cost_report(llm_cost_data)
        assert result.count("<b>") == result.count("</b>")


# ---------------------------------------------------------------------------
# TestMonitoringAlert
# ---------------------------------------------------------------------------


class TestMonitoringAlert:
    """monitoring_alert() tests."""

    def test_stop_loss_icon(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="손절가까지 0.5% 남음",
            current_value=Decimal("-4.50"),
            threshold_value=Decimal("-5.00"),
        )
        assert "🔻" in result
        assert "손절 근접 경고" in result

    def test_sector_concentration_icon(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.SECTOR_CONCENTRATION,
            symbol=None,
            message="반도체 섹터 30% 초과",
            current_value=Decimal("30.00"),
            threshold_value=Decimal("25.00"),
        )
        assert "📊" in result
        assert "섹터 비중 경고" in result

    def test_llm_budget_icon(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.LLM_BUDGET,
            symbol=None,
            message="예산 85% 사용",
            current_value=Decimal("85.00"),
            threshold_value=Decimal("80.00"),
        )
        assert "💳" in result
        assert "LLM 예산 경고" in result

    def test_portfolio_drawdown_icon(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.PORTFOLIO_DRAWDOWN,
            symbol=None,
            message="포트폴리오 낙폭 -5.2%",
            current_value=Decimal("-5.20"),
            threshold_value=Decimal("-5.00"),
        )
        assert "📉" in result
        assert "포트폴리오 낙폭 경고" in result

    def test_symbol_shown(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="테스트",
            current_value=Decimal("-4.50"),
            threshold_value=Decimal("-5.00"),
        )
        assert "종목: 005930" in result

    def test_symbol_none(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.LLM_BUDGET,
            symbol=None,
            message="예산 초과",
            current_value=Decimal("85.00"),
            threshold_value=Decimal("80.00"),
        )
        assert "종목" not in result

    def test_message_escaped(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="<script>alert('xss')</script>",
            current_value=Decimal("-4.50"),
            threshold_value=Decimal("-5.00"),
        )
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_values_formatted(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="테스트",
            current_value=Decimal("-4.50"),
            threshold_value=Decimal("-5.00"),
        )
        assert "-4.50%" in result
        assert "-5.00%" in result

    def test_html_tags_valid(self) -> None:
        result = MessageTemplates.monitoring_alert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="테스트",
            current_value=Decimal("-4.50"),
            threshold_value=Decimal("-5.00"),
        )
        assert result.count("<b>") == result.count("</b>")
