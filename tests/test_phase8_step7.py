"""Phase 8 Step 7 테스트: 텔레그램 알림 + 리포트 계좌별 분리.

1. MessageTemplates — account_label 파라미터 (8개 템플릿)
2. ReportDataFetcher — account_id 필터 (6개 쿼리)
3. ReportGenerator — account_id 전파 (3개 생성 메서드)
4. TradingMonitor — account_id/account_label 전파
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.core.enums import (
    ApprovalStatus,
    ExitReason,
    MonitoringAlertType,
    OrderSide,
)
from src.core.models import (
    DailyReportData,
    MonitoringAlert,
    PerformanceMetrics,
    WeeklyReportData,
)
from src.notification.templates import MessageTemplates


# ===========================================================================
# 1. MessageTemplates — account_label
# ===========================================================================


class TestMessageTemplatesAccountLabel:
    """모든 8개 템플릿에 account_label 파라미터가 정상 동작하는지 확인."""

    LABEL = "공격형 (1234)"

    # -- approval_request ---------------------------------------------------

    def _approval_kwargs(self) -> dict:
        return dict(
            symbol="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            quantity=10,
            price=Decimal("70000"),
            position_value_krw=Decimal("700000"),
            portfolio_pct=Decimal("5.0"),
            stop_loss_price=Decimal("65000"),
            take_profit_price=Decimal("80000"),
            risk_reward_ratio=Decimal("2.0"),
            analysis_summary="테스트 분석",
            web_verify_summary="검증 완료",
            session_id=uuid4(),
        )

    def test_approval_request_no_label(self):
        result = MessageTemplates.approval_request(**self._approval_kwargs())
        assert result.startswith("📊")
        assert "[" not in result.split("\n")[0]

    def test_approval_request_with_label(self):
        result = MessageTemplates.approval_request(
            account_label=self.LABEL, **self._approval_kwargs()
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- execution_notification ---------------------------------------------

    def _execution_kwargs(self) -> dict:
        return dict(
            symbol="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            quantity=10,
            fill_price=Decimal("70000"),
            commission=Decimal("350"),
            approval_status=ApprovalStatus.APPROVED,
        )

    def test_execution_notification_no_label(self):
        result = MessageTemplates.execution_notification(**self._execution_kwargs())
        assert result.startswith("✅")

    def test_execution_notification_with_label(self):
        result = MessageTemplates.execution_notification(
            account_label=self.LABEL, **self._execution_kwargs()
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- rejection_notification ---------------------------------------------

    def _rejection_kwargs(self) -> dict:
        return dict(
            symbol="005930",
            name="삼성전자",
            side=OrderSide.BUY,
            reason="리스크 초과",
            stage="risk_blocked",
        )

    def test_rejection_notification_no_label(self):
        result = MessageTemplates.rejection_notification(**self._rejection_kwargs())
        assert "⚠️" in result

    def test_rejection_notification_with_label(self):
        result = MessageTemplates.rejection_notification(
            account_label=self.LABEL, **self._rejection_kwargs()
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- exit_signal_notification -------------------------------------------

    def _exit_signal_kwargs(self) -> dict:
        return dict(
            symbol="005930",
            name="삼성전자",
            reason=ExitReason.STOP_LOSS,
            urgency="immediate",
            current_price=Decimal("65000"),
            unrealized_pnl_pct=Decimal("-7.14"),
            auto_executed=True,
        )

    def test_exit_signal_no_label(self):
        result = MessageTemplates.exit_signal_notification(**self._exit_signal_kwargs())
        assert result.startswith("🔔")

    def test_exit_signal_with_label(self):
        result = MessageTemplates.exit_signal_notification(
            account_label=self.LABEL, **self._exit_signal_kwargs()
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- daily_report -------------------------------------------------------

    def _daily_data(self) -> DailyReportData:
        return DailyReportData(
            report_date=date(2026, 3, 28),
            realized_pnl=Decimal("10000"),
            realized_pnl_pct=Decimal("0.1"),
            unrealized_pnl=Decimal("5000"),
            unrealized_pnl_pct=Decimal("0.5"),
            total_value=Decimal("10000000"),
            cash=Decimal("5000000"),
            cash_pct=Decimal("50"),
            positions_count=3,
            trades_today=[],
            cumulative_return_pct=Decimal("1.5"),
            sector_allocations={},
            warnings=[],
            llm_cost_today_usd=Decimal("0.5"),
            llm_cost_monthly_usd=Decimal("10"),
            llm_budget_usd=Decimal("50"),
        )

    def test_daily_report_no_label(self):
        result = MessageTemplates.daily_report(self._daily_data())
        assert result.startswith("📋")

    def test_daily_report_with_label(self):
        result = MessageTemplates.daily_report(
            self._daily_data(), account_label=self.LABEL
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- weekly_report ------------------------------------------------------

    def _weekly_data(self) -> WeeklyReportData:
        return WeeklyReportData(
            week_start=date(2026, 3, 22),
            week_end=date(2026, 3, 28),
            performance=PerformanceMetrics(
                period_start=date(2026, 3, 22),
                period_end=date(2026, 3, 28),
                total_return_pct=Decimal("1.5"),
                max_drawdown_pct=Decimal("2.0"),
                win_rate_pct=Decimal("60"),
                total_trades=5,
                winning_trades=3,
                losing_trades=2,
                avg_win_pct=Decimal("3.0"),
                avg_loss_pct=Decimal("-1.5"),
            ),
            best_trade=None,
            worst_trade=None,
            strategy_comparison={},
        )

    def test_weekly_report_no_label(self):
        result = MessageTemplates.weekly_report(self._weekly_data())
        assert result.startswith("📋")

    def test_weekly_report_with_label(self):
        result = MessageTemplates.weekly_report(
            self._weekly_data(), account_label=self.LABEL
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- monthly_report -----------------------------------------------------

    def test_monthly_report_no_label(self):
        result = MessageTemplates.monthly_report(
            metrics=self._weekly_data().performance,
            summary=self._daily_data(),
        )
        assert result.startswith("📋")

    def test_monthly_report_with_label(self):
        result = MessageTemplates.monthly_report(
            account_label=self.LABEL,
            metrics=self._weekly_data().performance,
            summary=self._daily_data(),
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- monitoring_alert ---------------------------------------------------

    def _alert_kwargs(self) -> dict:
        return dict(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="손절가 근접",
            current_value=Decimal("3.5"),
            threshold_value=Decimal("5.0"),
        )

    def test_monitoring_alert_no_label(self):
        result = MessageTemplates.monitoring_alert(**self._alert_kwargs())
        assert result.startswith("🔻")

    def test_monitoring_alert_with_label(self):
        result = MessageTemplates.monitoring_alert(
            account_label=self.LABEL, **self._alert_kwargs()
        )
        assert result.startswith("<b>[공격형 (1234)]</b>")

    # -- HTML escaping ------------------------------------------------------

    def test_account_label_html_escape(self):
        result = MessageTemplates.execution_notification(
            account_label="<script>alert(1)</script>",
            **self._execution_kwargs(),
        )
        assert "&lt;script&gt;" in result
        assert "<script>" not in result


# ===========================================================================
# 2. ReportDataFetcher — account_id 필터
# ===========================================================================


class TestReportDataFetcherAccountId:
    """ReportDataFetcher 메서드들이 account_id 파라미터를 받는지 확인.

    실제 DB 없이 시그니처 검증 + 모킹으로 WHERE 조건 전달 확인.
    """

    def test_get_closed_positions_accepts_account_id(self):
        """메서드 시그니처에 account_id가 있는지 확인."""
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_closed_positions)
        assert "account_id" in sig.parameters

    def test_get_portfolio_snapshots_accepts_account_id(self):
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_portfolio_snapshots)
        assert "account_id" in sig.parameters

    def test_get_todays_orders_accepts_account_id(self):
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_todays_orders)
        assert "account_id" in sig.parameters

    def test_get_todays_executions_accepts_account_id(self):
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_todays_executions)
        assert "account_id" in sig.parameters

    def test_get_open_positions_accepts_account_id(self):
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_open_positions)
        assert "account_id" in sig.parameters

    def test_get_latest_snapshot_accepts_account_id(self):
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_latest_snapshot)
        assert "account_id" in sig.parameters

    def test_get_position_with_orders_no_account_id(self):
        """get_position_with_orders에는 account_id가 없어야 한다."""
        import inspect
        from src.report.data_fetcher import ReportDataFetcher
        sig = inspect.signature(ReportDataFetcher.get_position_with_orders)
        assert "account_id" not in sig.parameters


# ===========================================================================
# 3. ReportGenerator — account_id 전파
# ===========================================================================


class TestReportGeneratorAccountId:
    """ReportGenerator가 account_id를 fetcher에 전달하는지 mock으로 검증."""

    @pytest.fixture
    def mock_generator(self):
        """mock 의존성으로 ReportGenerator 인스턴스 생성."""
        from src.report.generator import ReportGenerator

        fetcher = AsyncMock()
        fetcher.get_latest_snapshot.return_value = None
        fetcher.get_todays_orders.return_value = []
        fetcher.get_open_positions.return_value = []
        fetcher.get_closed_positions.return_value = []
        fetcher.get_portfolio_snapshots.return_value = []

        cost_tracker = AsyncMock()
        cost_tracker.get_budget_status.return_value = MagicMock(
            current_month_cost_usd=Decimal("0"),
            budget_usd=Decimal("50"),
            usage_percent=Decimal("0"),
        )
        cost_tracker.get_usage_stats.return_value = []
        cost_tracker.get_monthly_summary.return_value = {}

        settings = MagicMock()
        settings.INITIAL_CAPITAL = Decimal("10000000")
        settings.RISK_FREE_RATE_PCT = Decimal("3.5")
        settings.MONITOR_SECTOR_WEIGHT_WARN_PCT = Decimal("30")
        settings.MONITOR_LLM_BUDGET_WARN_PCT = Decimal("80")

        gen = ReportGenerator(
            data_fetcher=fetcher,
            cost_tracker=cost_tracker,
            settings=settings,
        )
        return gen, fetcher

    @pytest.mark.asyncio
    async def test_daily_report_passes_account_id(self, mock_generator):
        gen, fetcher = mock_generator

        await gen.generate_daily_report(account_id="acct-1")

        fetcher.get_latest_snapshot.assert_called_once_with(account_id="acct-1")
        fetcher.get_todays_orders.assert_called_once_with(account_id="acct-1")
        fetcher.get_open_positions.assert_called_once_with(account_id="acct-1")

    @pytest.mark.asyncio
    async def test_daily_report_none_account_id(self, mock_generator):
        gen, fetcher = mock_generator

        await gen.generate_daily_report()

        fetcher.get_latest_snapshot.assert_called_once_with(account_id=None)
        fetcher.get_todays_orders.assert_called_once_with(account_id=None)
        fetcher.get_open_positions.assert_called_once_with(account_id=None)

    @pytest.mark.asyncio
    async def test_weekly_report_passes_account_id(self, mock_generator):
        gen, fetcher = mock_generator

        await gen.generate_weekly_report(account_id="acct-2")

        fetcher.get_closed_positions.assert_called_once()
        call_kwargs = fetcher.get_closed_positions.call_args.kwargs
        assert call_kwargs["account_id"] == "acct-2"

        fetcher.get_portfolio_snapshots.assert_called_once()
        call_kwargs = fetcher.get_portfolio_snapshots.call_args.kwargs
        assert call_kwargs["account_id"] == "acct-2"

    @pytest.mark.asyncio
    async def test_monthly_report_passes_account_id(self, mock_generator):
        gen, fetcher = mock_generator

        await gen.generate_monthly_report(account_id="acct-3")

        # monthly calls get_closed_positions + get_portfolio_snapshots +
        # internally calls generate_daily_report(account_id="acct-3")
        # which calls get_latest_snapshot, get_todays_orders, get_open_positions

        # get_closed_positions
        closed_call = fetcher.get_closed_positions.call_args.kwargs
        assert closed_call["account_id"] == "acct-3"

        # get_latest_snapshot (from internal daily_report call)
        fetcher.get_latest_snapshot.assert_called_once_with(account_id="acct-3")


# ===========================================================================
# 4. TradingMonitor — account_id + account_label
# ===========================================================================


class TestTradingMonitorAccountId:
    """TradingMonitor가 account_id/account_label을 올바르게 사용하는지 확인."""

    @pytest.fixture
    def mock_monitor(self):
        from src.scheduler.monitor import TradingMonitor

        portfolio = AsyncMock()
        portfolio.get_current_state.return_value = MagicMock(
            sector_allocations={},
            drawdown_pct=Decimal("0"),
        )

        positions = AsyncMock()
        positions.get_open.return_value = []

        cost_tracker = AsyncMock()
        cost_tracker.get_budget_status.return_value = MagicMock(
            usage_percent=Decimal("0"),
        )

        telegram_bot = AsyncMock()
        broker = AsyncMock()
        cache = AsyncMock()
        cache.get.return_value = None

        settings = MagicMock()
        settings.MONITOR_STOP_LOSS_PROXIMITY_PCT = Decimal("5")
        settings.MONITOR_SECTOR_WEIGHT_WARN_PCT = Decimal("30")
        settings.MONITOR_LLM_BUDGET_WARN_PCT = Decimal("80")
        settings.MAX_DRAWDOWN_PCT = Decimal("20")

        monitor = TradingMonitor(
            portfolio_state_service=portfolio,
            position_manager=positions,
            cost_tracker=cost_tracker,
            telegram_bot=telegram_bot,
            broker=broker,
            cache=cache,
            settings=settings,
            account_id="acct-1",
            account_label="공격형 (1234)",
        )
        return monitor, positions, telegram_bot, cache

    @pytest.mark.asyncio
    async def test_stop_loss_passes_account_id(self, mock_monitor):
        monitor, positions, _, _ = mock_monitor

        await monitor.check_stop_loss_proximity()

        positions.get_open.assert_called_once_with(account_id="acct-1")

    @pytest.mark.asyncio
    async def test_send_alert_includes_account_label(self, mock_monitor):
        monitor, _, telegram_bot, _ = mock_monitor

        alert = MonitoringAlert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="테스트",
            current_value=Decimal("3"),
            threshold_value=Decimal("5"),
            timestamp=datetime.now(timezone.utc),
        )

        await monitor._send_alert(alert)

        telegram_bot.send_message.assert_called_once()
        msg = telegram_bot.send_message.call_args[0][0]
        assert "<b>[공격형 (1234)]</b>" in msg

    @pytest.mark.asyncio
    async def test_dedup_key_includes_account_id(self, mock_monitor):
        """sector_concentration dedup 키에 account_id가 포함되는지 확인."""
        monitor, _, _, cache = mock_monitor

        # sector 비중 초과 설정
        monitor._portfolio.get_current_state.return_value = MagicMock(
            sector_allocations={"IT": Decimal("40")},
            drawdown_pct=Decimal("0"),
        )

        await monitor.check_sector_concentration()

        # _is_alert_sent_today가 호출될 때 dedup 키에 account_id 포함
        cache.get.assert_called()
        dedup_key = cache.get.call_args[0][1]
        assert "acct-1" in dedup_key

    @pytest.mark.asyncio
    async def test_default_account_id(self):
        """account_id 미지정 시 기본값 "default" 사용."""
        from src.scheduler.monitor import TradingMonitor

        monitor = TradingMonitor(
            portfolio_state_service=AsyncMock(),
            position_manager=AsyncMock(),
            cost_tracker=AsyncMock(),
            telegram_bot=AsyncMock(),
            broker=AsyncMock(),
            cache=AsyncMock(),
            settings=MagicMock(),
        )
        assert monitor._account_id == "default"
        assert monitor._account_label == ""
