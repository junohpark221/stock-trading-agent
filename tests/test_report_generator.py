"""ReportDataFetcher + ReportGenerator 단위 테스트 — Phase 6 Step 4."""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.models import DailyReportData, PerformanceMetrics, WeeklyReportData
from src.db.models.execution import Order
from src.db.models.strategy import PortfolioSnapshot, PositionRecord
from src.report.data_fetcher import ReportDataFetcher
from src.report.generator import ReportGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(
    *,
    entry_price: Decimal = Decimal("50000"),
    exit_price: Decimal = Decimal("55000"),
    exit_date: date = date(2026, 3, 20),
    realized_pnl: Decimal = Decimal("50000"),
    strategy_type: str = "swing",
    symbol: str = "005930",
    position_id: int = 1,
) -> PositionRecord:
    return PositionRecord(
        id=position_id,
        symbol=symbol,
        strategy_type=strategy_type,
        quantity=10,
        avg_cost=entry_price,
        entry_price=entry_price,
        entry_date=date(2026, 3, 10),
        stop_loss_price=(entry_price * Decimal("0.97")).quantize(Decimal("0.01")),
        exit_price=exit_price,
        exit_date=exit_date,
        realized_pnl=realized_pnl,
        status="closed",
    )


def _make_snapshot(
    *,
    snapshot_date: date = date(2026, 3, 27),
    total_value: Decimal = Decimal("10500000"),
    cash: Decimal = Decimal("5000000"),
    invested: Decimal = Decimal("5500000"),
    unrealized_pnl: Decimal = Decimal("200000"),
    realized_pnl_daily: Decimal = Decimal("50000"),
    positions_count: int = 3,
    sector_allocations: dict | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_date=snapshot_date,
        total_value=total_value,
        cash=cash,
        invested=invested,
        unrealized_pnl=unrealized_pnl,
        realized_pnl_daily=realized_pnl_daily,
        peak_value=total_value,
        drawdown_pct=Decimal("0"),
        positions_count=positions_count,
        sector_allocations=sector_allocations,
    )


def _make_order(
    *,
    symbol: str = "005930",
    side: str = "buy",
    quantity: int = 10,
    price: Decimal = Decimal("50000"),
    status: str = "filled",
    filled_price: Decimal | None = Decimal("50000"),
    executed_at: datetime | None = None,
    position_id: int | None = None,
) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        order_type="limit",
        quantity=quantity,
        price=price,
        status=status,
        approval_status="auto_approved",
        original_quantity=quantity,
        filled_quantity=quantity if status == "filled" else 0,
        filled_price=filled_price,
        executed_at=executed_at or datetime.now(timezone.utc),
        position_id=position_id,
    )


def _make_settings(**overrides: object) -> MagicMock:
    """테스트용 Settings mock."""
    defaults = {
        "INITIAL_CAPITAL": Decimal("10000000"),
        "RISK_FREE_RATE_PCT": 3.5,
        "MONITOR_SECTOR_WEIGHT_WARN_PCT": 25.0,
        "MONITOR_LLM_BUDGET_WARN_PCT": 80.0,
    }
    defaults.update(overrides)
    settings = MagicMock()
    for k, v in defaults.items():
        setattr(settings, k, v)
    return settings


def _make_budget_status(
    *,
    cost: Decimal = Decimal("30.00"),
    budget: Decimal = Decimal("100.00"),
    usage_pct: Decimal = Decimal("30.00"),
) -> MagicMock:
    bs = MagicMock()
    bs.current_month_cost_usd = cost
    bs.budget_usd = budget
    bs.remaining_usd = budget - cost
    bs.usage_percent = usage_pct
    bs.warning_triggered = False
    bs.budget_exceeded = False
    bs.escalation_disabled = False
    return bs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session():
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def mock_session_factory(mock_session):
    factory = MagicMock(return_value=mock_session)
    return factory


@pytest.fixture
def data_fetcher(mock_session_factory):
    return ReportDataFetcher(mock_session_factory)


@pytest.fixture
def mock_data_fetcher():
    return AsyncMock(spec=ReportDataFetcher)


@pytest.fixture
def mock_cost_tracker():
    tracker = AsyncMock()
    tracker.get_budget_status = AsyncMock(return_value=_make_budget_status())
    tracker.get_usage_stats = AsyncMock(return_value=[])
    tracker.get_monthly_summary = AsyncMock(return_value={
        "year": 2026,
        "month": 3,
        "total_cost_usd": "30.00",
        "total_calls": 150,
        "total_tokens_in": 50000,
        "total_tokens_out": 30000,
        "total_escalations": 5,
        "by_provider": {"openai": {"cost_usd": "25.00", "call_count": 120}},
    })
    return tracker


@pytest.fixture
def settings():
    return _make_settings()


@pytest.fixture
def generator(mock_data_fetcher, mock_cost_tracker, settings):
    return ReportGenerator(
        data_fetcher=mock_data_fetcher,
        cost_tracker=mock_cost_tracker,
        settings=settings,
    )


# ===========================================================================
# ReportDataFetcher Tests
# ===========================================================================


class TestReportDataFetcherClosedPositions:
    """get_closed_positions 테스트."""

    @pytest.mark.asyncio
    async def test_date_range_filter(self, data_fetcher, mock_session):
        """날짜 범위 필터링으로 청산 포지션 조회."""
        pos = _make_position()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [pos]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await data_fetcher.get_closed_positions(
            start_date=date(2026, 3, 1), end_date=date(2026, 3, 31),
        )

        assert len(result) == 1
        assert result[0].symbol == "005930"
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_strategy_filter(self, data_fetcher, mock_session):
        """strategy_type 필터 적용."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await data_fetcher.get_closed_positions(
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31),
            strategy_type="position",
        )

        assert result == []
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_result(self, data_fetcher, mock_session):
        """청산 포지션 없을 때 빈 리스트 반환."""
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await data_fetcher.get_closed_positions(
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )

        assert result == []


class TestReportDataFetcherSnapshots:
    """get_portfolio_snapshots / get_latest_snapshot 테스트."""

    @pytest.mark.asyncio
    async def test_snapshots_date_range(self, data_fetcher, mock_session):
        """날짜 범위 스냅샷 조회."""
        snap = _make_snapshot()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [snap]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await data_fetcher.get_portfolio_snapshots(
            start_date=date(2026, 3, 1), end_date=date(2026, 3, 31),
        )

        assert len(result) == 1
        assert result[0].total_value == Decimal("10500000")

    @pytest.mark.asyncio
    async def test_latest_snapshot_none(self, data_fetcher, mock_session):
        """스냅샷 없을 때 None 반환."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await data_fetcher.get_latest_snapshot()

        assert result is None


class TestReportDataFetcherPositionWithOrders:
    """get_position_with_orders 테스트."""

    @pytest.mark.asyncio
    async def test_found(self, data_fetcher, mock_session):
        """포지션 + 주문 관계 조회 성공."""
        pos = _make_position()
        order = _make_order(position_id=1)

        # 첫 번째 execute: 포지션 조회
        pos_result = MagicMock()
        pos_result.scalar_one_or_none.return_value = pos

        # 두 번째 execute: 주문 조회
        orders_result = MagicMock()
        orders_result.scalars.return_value.all.return_value = [order]

        mock_session.execute = AsyncMock(side_effect=[pos_result, orders_result])

        result = await data_fetcher.get_position_with_orders(position_id=1)

        assert result is not None
        assert result["position"].symbol == "005930"
        assert len(result["orders"]) == 1
        assert result["orders"][0].side == "buy"

    @pytest.mark.asyncio
    async def test_not_found(self, data_fetcher, mock_session):
        """포지션 없을 때 None 반환."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await data_fetcher.get_position_with_orders(position_id=999)

        assert result is None


# ===========================================================================
# ReportGenerator Tests
# ===========================================================================


class TestGenerateDailyReport:
    """generate_daily_report 테스트."""

    @pytest.mark.asyncio
    async def test_normal(self, generator, mock_data_fetcher, mock_cost_tracker):
        """정상 데이터로 일간 리포트 생성."""
        snapshot = _make_snapshot(
            sector_allocations={"IT": 15.5, "금융": 10.0},
        )
        orders = [_make_order(), _make_order(side="sell")]

        mock_data_fetcher.get_latest_snapshot.return_value = snapshot
        mock_data_fetcher.get_todays_orders.return_value = orders
        mock_data_fetcher.get_open_positions.return_value = [
            PositionRecord(
                symbol="005930", strategy_type="swing", quantity=10,
                avg_cost=Decimal("50000"), entry_price=Decimal("50000"),
                entry_date=date(2026, 3, 10),
                stop_loss_price=Decimal("48500"), status="open",
            ),
        ]

        result = await generator.generate_daily_report(report_date=date(2026, 3, 27))

        assert isinstance(result, DailyReportData)
        assert result.report_date == date(2026, 3, 27)
        assert result.total_value == Decimal("10500000")
        assert result.cash == Decimal("5000000")
        assert result.positions_count == 3
        assert len(result.trades_today) == 2
        assert result.realized_pnl == Decimal("50000")
        assert result.unrealized_pnl == Decimal("200000")
        assert result.cumulative_return_pct == Decimal("5.00")
        assert result.llm_cost_monthly_usd == Decimal("30.00")
        assert result.llm_budget_usd == Decimal("100.00")
        # sector_allocations → Decimal 변환 확인
        assert result.sector_allocations["IT"] == Decimal("15.5")

    @pytest.mark.asyncio
    async def test_no_snapshot(self, generator, mock_data_fetcher):
        """스냅샷 없는 첫날 — 0값 반환, 에러 없음."""
        mock_data_fetcher.get_latest_snapshot.return_value = None
        mock_data_fetcher.get_todays_orders.return_value = []
        mock_data_fetcher.get_open_positions.return_value = []

        result = await generator.generate_daily_report()

        assert isinstance(result, DailyReportData)
        assert result.total_value == Decimal("0")
        assert result.cash == Decimal("0")
        assert result.positions_count == 0
        assert result.trades_today == []
        assert result.cumulative_return_pct == Decimal("0")
        assert result.realized_pnl_pct == Decimal("0")
        assert result.unrealized_pnl_pct == Decimal("0")

    @pytest.mark.asyncio
    async def test_warnings_generated(self, generator, mock_data_fetcher, mock_cost_tracker):
        """섹터 비중 초과 + LLM 예산 초과 경고."""
        snapshot = _make_snapshot(
            sector_allocations={"IT": 30.0, "금융": 10.0},  # IT > 25%
        )
        mock_data_fetcher.get_latest_snapshot.return_value = snapshot
        mock_data_fetcher.get_todays_orders.return_value = []
        mock_data_fetcher.get_open_positions.return_value = []

        # LLM 예산 85% 사용 → 경고 (임계치 80%)
        mock_cost_tracker.get_budget_status.return_value = _make_budget_status(
            cost=Decimal("85.00"),
            budget=Decimal("100.00"),
            usage_pct=Decimal("85.00"),
        )

        result = await generator.generate_daily_report()

        assert len(result.warnings) == 2
        assert any("IT" in w for w in result.warnings)
        assert any("LLM" in w for w in result.warnings)


class TestGenerateWeeklyReport:
    """generate_weekly_report 테스트."""

    @pytest.mark.asyncio
    async def test_normal(self, generator, mock_data_fetcher):
        """정상 주간 리포트 — PerformanceMetrics + best/worst trade."""
        positions = [
            _make_position(
                realized_pnl=Decimal("100000"),
                exit_date=date(2026, 3, 22),
                strategy_type="swing",
            ),
            _make_position(
                realized_pnl=Decimal("-30000"),
                exit_price=Decimal("47000"),
                exit_date=date(2026, 3, 23),
                strategy_type="position",
                symbol="000660",
                position_id=2,
            ),
        ]
        snapshots = [
            _make_snapshot(snapshot_date=date(2026, 3, 21), total_value=Decimal("10000000")),
            _make_snapshot(snapshot_date=date(2026, 3, 22), total_value=Decimal("10100000")),
            _make_snapshot(snapshot_date=date(2026, 3, 23), total_value=Decimal("10070000")),
        ]

        mock_data_fetcher.get_closed_positions.return_value = positions
        mock_data_fetcher.get_portfolio_snapshots.return_value = snapshots

        result = await generator.generate_weekly_report(week_end=date(2026, 3, 27))

        assert isinstance(result, WeeklyReportData)
        assert result.week_start == date(2026, 3, 21)
        assert result.week_end == date(2026, 3, 27)
        assert isinstance(result.performance, PerformanceMetrics)
        assert result.performance.total_trades == 2
        assert result.best_trade is not None
        assert result.best_trade["symbol"] == "005930"
        assert result.best_trade["pnl"] == Decimal("100000")
        assert result.worst_trade is not None
        assert result.worst_trade["symbol"] == "000660"
        assert result.worst_trade["pnl"] == Decimal("-30000")
        assert "swing" in result.strategy_comparison
        assert "position" in result.strategy_comparison

    @pytest.mark.asyncio
    async def test_no_trades(self, generator, mock_data_fetcher):
        """거래 0건 — 에러 없이 기본값."""
        mock_data_fetcher.get_closed_positions.return_value = []
        mock_data_fetcher.get_portfolio_snapshots.return_value = []

        result = await generator.generate_weekly_report(week_end=date(2026, 3, 27))

        assert isinstance(result, WeeklyReportData)
        assert result.performance.total_trades == 0
        assert result.best_trade is None
        assert result.worst_trade is None
        assert result.strategy_comparison == {}


class TestGenerateMonthlyReport:
    """generate_monthly_report 테스트."""

    @pytest.mark.asyncio
    async def test_normal(self, generator, mock_data_fetcher):
        """월간 리포트 — (PerformanceMetrics, DailyReportData) 튜플 반환."""
        mock_data_fetcher.get_closed_positions.return_value = []
        mock_data_fetcher.get_portfolio_snapshots.return_value = []
        mock_data_fetcher.get_latest_snapshot.return_value = _make_snapshot()
        mock_data_fetcher.get_todays_orders.return_value = []
        mock_data_fetcher.get_open_positions.return_value = []

        metrics, daily = await generator.generate_monthly_report(year=2026, month=3)

        assert isinstance(metrics, PerformanceMetrics)
        assert metrics.period_start == date(2026, 3, 1)
        assert metrics.period_end == date(2026, 3, 31)
        assert isinstance(daily, DailyReportData)


class TestGenerateLLMCostReport:
    """generate_llm_cost_report 테스트."""

    @pytest.mark.asyncio
    async def test_keys(self, generator, mock_cost_tracker):
        """반환 dict에 필수 키 4개 존재."""
        result = await generator.generate_llm_cost_report()

        assert "budget_status" in result
        assert "monthly_summary" in result
        assert "recent_usage" in result
        assert "generated_at" in result
        assert result["monthly_summary"]["total_calls"] == 150
