"""Phase 6 Step 1: Enum / Pydantic 모델 / Config / APScheduler 의존성 테스트."""

from datetime import date, datetime
from decimal import Decimal

import pytest


# ---------------------------------------------------------------------------
# Enum 직렬화/역직렬화
# ---------------------------------------------------------------------------


class TestPerformanceReportType:
    def test_values(self):
        from src.core.enums import PerformanceReportType

        assert PerformanceReportType.DAILY == "daily"
        assert PerformanceReportType.WEEKLY == "weekly"
        assert PerformanceReportType.MONTHLY == "monthly"
        assert PerformanceReportType.LLM_COST == "llm_cost"

    def test_from_string(self):
        from src.core.enums import PerformanceReportType

        assert PerformanceReportType("daily") is PerformanceReportType.DAILY

    def test_invalid_value(self):
        from src.core.enums import PerformanceReportType

        with pytest.raises(ValueError):
            PerformanceReportType("invalid")


class TestJobStatus:
    def test_values(self):
        from src.core.enums import JobStatus

        assert JobStatus.RUNNING == "running"
        assert JobStatus.SUCCESS == "success"
        assert JobStatus.FAILED == "failed"
        assert JobStatus.SKIPPED == "skipped"

    def test_from_string(self):
        from src.core.enums import JobStatus

        assert JobStatus("success") is JobStatus.SUCCESS


class TestMonitoringAlertType:
    def test_values(self):
        from src.core.enums import MonitoringAlertType

        assert MonitoringAlertType.STOP_LOSS_PROXIMITY == "stop_loss_proximity"
        assert MonitoringAlertType.SECTOR_CONCENTRATION == "sector_concentration"
        assert MonitoringAlertType.LLM_BUDGET == "llm_budget"
        assert MonitoringAlertType.PORTFOLIO_DRAWDOWN == "portfolio_drawdown"

    def test_member_count(self):
        from src.core.enums import MonitoringAlertType

        assert len(MonitoringAlertType) == 4


# ---------------------------------------------------------------------------
# Pydantic 모델 검증
# ---------------------------------------------------------------------------


def _sample_performance_metrics(**overrides):
    defaults = {
        "period_start": date(2026, 3, 1),
        "period_end": date(2026, 3, 25),
        "total_return_pct": Decimal("5.23"),
        "max_drawdown_pct": Decimal("2.10"),
        "win_rate_pct": Decimal("60.00"),
        "avg_win_pct": Decimal("3.50"),
        "avg_loss_pct": Decimal("1.80"),
        "total_trades": 10,
        "winning_trades": 6,
        "losing_trades": 4,
    }
    defaults.update(overrides)
    return defaults


class TestPerformanceMetrics:
    def test_create_with_required_fields(self):
        from src.core.models import PerformanceMetrics

        m = PerformanceMetrics(**_sample_performance_metrics())
        assert m.total_return_pct == Decimal("5.23")
        assert m.total_trades == 10

    def test_optional_fields_default_none(self):
        from src.core.models import PerformanceMetrics

        m = PerformanceMetrics(**_sample_performance_metrics())
        assert m.annualized_return_pct is None
        assert m.sharpe_ratio is None
        assert m.sortino_ratio is None
        assert m.profit_factor is None

    def test_decimal_precision(self):
        from src.core.models import PerformanceMetrics

        m = PerformanceMetrics(
            **_sample_performance_metrics(
                sharpe_ratio=Decimal("1.234567890"),
            )
        )
        assert m.sharpe_ratio == Decimal("1.234567890")

    def test_from_attributes(self):
        from src.core.models import PerformanceMetrics

        assert PerformanceMetrics.model_config["from_attributes"] is True

    def test_json_roundtrip(self):
        from src.core.models import PerformanceMetrics

        m = PerformanceMetrics(**_sample_performance_metrics())
        json_str = m.model_dump_json()
        m2 = PerformanceMetrics.model_validate_json(json_str)
        assert m == m2


class TestDailyReportData:
    def test_defaults(self):
        from src.core.models import DailyReportData

        d = DailyReportData(
            report_date=date(2026, 3, 25),
            realized_pnl=Decimal("10000"),
            realized_pnl_pct=Decimal("1.5"),
            unrealized_pnl=Decimal("-5000"),
            unrealized_pnl_pct=Decimal("-0.8"),
            total_value=Decimal("10000000"),
            cash=Decimal("5000000"),
            cash_pct=Decimal("50.0"),
            positions_count=3,
            cumulative_return_pct=Decimal("5.2"),
        )
        assert d.trades_today == []
        assert d.sector_allocations == {}
        assert d.warnings == []
        assert d.llm_cost_today_usd == Decimal(0)
        assert d.llm_cost_monthly_usd == Decimal(0)
        assert d.llm_budget_usd == Decimal(0)

    def test_with_trades(self):
        from src.core.models import DailyReportData

        d = DailyReportData(
            report_date=date(2026, 3, 25),
            realized_pnl=Decimal("10000"),
            realized_pnl_pct=Decimal("1.5"),
            unrealized_pnl=Decimal("0"),
            unrealized_pnl_pct=Decimal("0"),
            total_value=Decimal("10000000"),
            cash=Decimal("5000000"),
            cash_pct=Decimal("50.0"),
            positions_count=1,
            cumulative_return_pct=Decimal("2.0"),
            trades_today=[{"symbol": "005930", "side": "buy", "pnl": "10000"}],
            llm_cost_today_usd=Decimal("1.50"),
        )
        assert len(d.trades_today) == 1
        assert d.llm_cost_today_usd == Decimal("1.50")


class TestWeeklyReportData:
    def test_nested_performance(self):
        from src.core.models import PerformanceMetrics, WeeklyReportData

        perf = PerformanceMetrics(**_sample_performance_metrics())
        w = WeeklyReportData(
            week_start=date(2026, 3, 17),
            week_end=date(2026, 3, 21),
            performance=perf,
        )
        assert w.performance.total_return_pct == Decimal("5.23")
        assert w.best_trade is None
        assert w.worst_trade is None
        assert w.strategy_comparison == {}

    def test_with_trades(self):
        from src.core.models import PerformanceMetrics, WeeklyReportData

        perf = PerformanceMetrics(**_sample_performance_metrics())
        w = WeeklyReportData(
            week_start=date(2026, 3, 17),
            week_end=date(2026, 3, 21),
            performance=perf,
            best_trade={"symbol": "005930", "pnl_pct": "5.0"},
            worst_trade={"symbol": "000660", "pnl_pct": "-2.0"},
        )
        assert w.best_trade["symbol"] == "005930"


class TestJobExecutionRecord:
    def test_create(self):
        from src.core.enums import JobStatus
        from src.core.models import JobExecutionRecord

        r = JobExecutionRecord(
            id=1,
            job_name="daily_report",
            status=JobStatus.SUCCESS,
            started_at=datetime(2026, 3, 25, 20, 0, 0),
            finished_at=datetime(2026, 3, 25, 20, 0, 5),
            duration_sec=Decimal("5.123"),
            result_summary="report sent",
        )
        assert r.status == JobStatus.SUCCESS
        assert r.error_message == ""

    def test_failed_with_error(self):
        from src.core.enums import JobStatus
        from src.core.models import JobExecutionRecord

        r = JobExecutionRecord(
            id=2,
            job_name="stop_loss_check",
            status=JobStatus.FAILED,
            started_at=datetime(2026, 3, 25, 10, 0, 0),
            error_message="DB connection timeout",
        )
        assert r.status == JobStatus.FAILED
        assert r.finished_at is None
        assert r.error_message == "DB connection timeout"


class TestMonitoringAlert:
    @pytest.mark.parametrize(
        "alert_type",
        [
            "stop_loss_proximity",
            "sector_concentration",
            "llm_budget",
            "portfolio_drawdown",
        ],
    )
    def test_all_alert_types(self, alert_type):
        from src.core.enums import MonitoringAlertType
        from src.core.models import MonitoringAlert

        a = MonitoringAlert(
            alert_type=MonitoringAlertType(alert_type),
            message=f"test {alert_type}",
            current_value=Decimal("10.0"),
            threshold_value=Decimal("5.0"),
            timestamp=datetime(2026, 3, 25, 12, 0, 0),
        )
        assert a.alert_type == alert_type

    def test_with_symbol(self):
        from src.core.enums import MonitoringAlertType
        from src.core.models import MonitoringAlert

        a = MonitoringAlert(
            alert_type=MonitoringAlertType.STOP_LOSS_PROXIMITY,
            symbol="005930",
            message="삼성전자 손절가 근접",
            current_value=Decimal("71000"),
            threshold_value=Decimal("70000"),
            timestamp=datetime(2026, 3, 25, 14, 30, 0),
        )
        assert a.symbol == "005930"

    def test_symbol_optional(self):
        from src.core.enums import MonitoringAlertType
        from src.core.models import MonitoringAlert

        a = MonitoringAlert(
            alert_type=MonitoringAlertType.PORTFOLIO_DRAWDOWN,
            message="포트폴리오 낙폭 경고",
            current_value=Decimal("8.5"),
            threshold_value=Decimal("10.0"),
            timestamp=datetime(2026, 3, 25, 14, 30, 0),
        )
        assert a.symbol is None


# ---------------------------------------------------------------------------
# Config 기본값 검증
# ---------------------------------------------------------------------------


class TestPhase6Config:
    def test_scheduler_defaults(self):
        from src.config import Settings

        s = Settings(
            DATABASE_URL="postgresql+asyncpg://x:x@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert s.SCHEDULER_ENABLED is True

    def test_job_schedule_defaults(self):
        from src.config import Settings

        s = Settings(
            DATABASE_URL="postgresql+asyncpg://x:x@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert s.MARKET_DATA_COLLECTION_TIME == "15:40"
        # 배치 재구성: 결정/실행 분리 + 게이트 (2026-06-29)
        assert s.PRE_OPEN_PREP_TIME == "08:00"
        assert s.DECISION_TIME == "08:30"
        assert s.POSITION_ANALYSIS_DAYS == "tue,fri"  # 휴장 토 → 평일 정정
        assert s.EXECUTION_DRAIN_INTERVAL_MIN == 5
        assert s.EXECUTION_GAP_GUARD_PCT == 3.0
        assert s.DATA_FRESHNESS_MIN_COVERAGE_PCT == 95.0
        assert s.STOP_LOSS_CHECK_INTERVAL_MIN == 5
        assert s.DAILY_REPORT_TIME == "20:00"
        assert s.WEEKLY_REPORT_DAY == "sat"
        assert s.WEEKLY_REPORT_TIME == "10:00"
        assert s.MONTHLY_REPORT_DAY == 1
        assert s.MONTHLY_REPORT_TIME == "10:00"
        assert s.TOKEN_REFRESH_TIME == "08:00"  # 개장 전 갱신으로 이동
        assert s.LLM_COST_REPORT_DAY == "mon"
        assert s.LLM_COST_REPORT_TIME == "09:00"

    def test_monitoring_threshold_defaults(self):
        from src.config import Settings

        s = Settings(
            DATABASE_URL="postgresql+asyncpg://x:x@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert s.MONITOR_STOP_LOSS_PROXIMITY_PCT == 2.0
        assert s.MONITOR_SECTOR_WEIGHT_WARN_PCT == 25.0
        assert s.MONITOR_LLM_BUDGET_WARN_PCT == 80.0

    def test_risk_free_rate_default(self):
        from src.config import Settings

        s = Settings(
            DATABASE_URL="postgresql+asyncpg://x:x@localhost/test",
            REDIS_URL="redis://localhost:6379/0",
        )
        assert s.RISK_FREE_RATE_PCT == 3.5


# ---------------------------------------------------------------------------
# APScheduler 의존성 확인
# ---------------------------------------------------------------------------


class TestAPSchedulerImport:
    def test_import_apscheduler(self):
        import apscheduler

        assert hasattr(apscheduler, "__version__")

    def test_import_asyncio_scheduler(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        assert AsyncIOScheduler is not None
