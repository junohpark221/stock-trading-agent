"""Unit tests for Control API and Trades API endpoints.

Uses httpx AsyncClient with FastAPI test transport.
All external dependencies (scheduler, DB) are mocked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.db.session import get_db_session


# ── Helpers ──────────────────────────────────────────────────────────


def _mock_scheduler(*, is_running: bool = True, is_paused: bool = False):
    """Mock SchedulerEngine."""
    engine = MagicMock()
    engine.is_running = is_running
    engine.is_paused = is_paused
    engine._job_fns = {
        "token_refresh": AsyncMock(),
        "market_data_collect": AsyncMock(),
        "swing_analysis": AsyncMock(),
        "position_analysis": AsyncMock(),
        "stop_loss_check": AsyncMock(),
        "daily_report": AsyncMock(),
        "weekly_report": AsyncMock(),
        "monthly_report": AsyncMock(),
        "llm_cost_report": AsyncMock(),
    }
    engine.get_status.return_value = {
        "is_running": is_running,
        "is_paused": is_paused,
        "jobs": [
            {"name": "daily_report", "next_run_time": "2026-03-28T06:00:00+00:00", "trigger": "cron"},
            {"name": "stop_loss_check", "next_run_time": "2026-03-27T10:00:00+00:00", "trigger": "interval"},
        ],
    }
    engine.pause_all = MagicMock()
    engine.resume_all = MagicMock()
    engine.run_job_now = AsyncMock()
    return engine


def _mock_position_row(**kwargs) -> MagicMock:
    """Mock PositionRecord ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.symbol = kwargs.get("symbol", "005930")
    row.strategy_type = kwargs.get("strategy_type", "position")
    row.quantity = kwargs.get("quantity", 10)
    row.entry_price = kwargs.get("entry_price", Decimal("78000"))
    row.entry_date = kwargs.get("entry_date", date(2026, 3, 20))
    row.exit_price = kwargs.get("exit_price", Decimal("82000"))
    row.exit_date = kwargs.get("exit_date", date(2026, 3, 25))
    row.exit_reason = kwargs.get("exit_reason", "take_profit")
    row.realized_pnl = kwargs.get("realized_pnl", Decimal("40000"))
    row.status = kwargs.get("status", "closed")
    row.stop_loss_price = kwargs.get("stop_loss_price", Decimal("72000"))
    row.take_profit_price = kwargs.get("take_profit_price", Decimal("84000"))
    return row


def _mock_order_row(**kwargs) -> MagicMock:
    """Mock Order ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 100)
    row.symbol = kwargs.get("symbol", "005930")
    row.side = kwargs.get("side", "buy")
    row.order_type = kwargs.get("order_type", "limit")
    row.quantity = kwargs.get("quantity", 10)
    row.price = kwargs.get("price", Decimal("78000"))
    row.status = kwargs.get("status", "filled")
    row.filled_quantity = kwargs.get("filled_quantity", 10)
    row.filled_price = kwargs.get("filled_price", Decimal("78000"))
    row.executed_at = kwargs.get("executed_at", datetime(2026, 3, 20, 9, 30, tzinfo=UTC))
    row.created_at = kwargs.get("created_at", datetime(2026, 3, 20, 9, 0, tzinfo=UTC))
    row.position_id = kwargs.get("position_id", 1)
    return row


def _mock_execution_row(**kwargs) -> MagicMock:
    """Mock Execution ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 200)
    row.order_id = kwargs.get("order_id", 100)
    row.fill_price = kwargs.get("fill_price", Decimal("78000"))
    row.fill_quantity = kwargs.get("fill_quantity", 10)
    row.commission = kwargs.get("commission", Decimal("390"))
    row.executed_at = kwargs.get("executed_at", datetime(2026, 3, 20, 9, 30, tzinfo=UTC))
    return row


def _mock_job_execution_row(**kwargs) -> MagicMock:
    """Mock JobExecution ORM row."""
    row = MagicMock()
    row.id = kwargs.get("id", 1)
    row.job_name = kwargs.get("job_name", "daily_report")
    row.status = kwargs.get("status", "success")
    row.started_at = kwargs.get("started_at", datetime(2026, 3, 27, 6, 0, tzinfo=UTC))
    row.finished_at = kwargs.get("finished_at", datetime(2026, 3, 27, 6, 0, 5, tzinfo=UTC))
    row.duration_sec = kwargs.get("duration_sec", Decimal("5.123"))
    row.error_message = kwargs.get("error_message", "")
    row.result_summary = kwargs.get("result_summary", "daily report sent")
    return row


class _FakeScalars:
    """Fake scalars() result."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeResult:
    """Fake execute() result."""

    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return _FakeScalars(self._rows)

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_db_session():
    """Mock AsyncSession for DI override."""
    return AsyncMock()


@pytest.fixture
def app(mock_db_session):
    """FastAPI app with dependency overrides."""
    main_mod.app.dependency_overrides[get_db_session] = lambda: mock_db_session
    yield main_mod.app
    main_mod.app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    """httpx AsyncClient bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ══════════════════════════════════════════════════════════════════════
# Control API Tests
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scheduler_status(client, mock_db_session):
    """GET /api/control/scheduler/status → 200 + jobs 목록."""
    engine = _mock_scheduler()
    with (
        patch("src.api.routes.control.get_settings") as mock_settings,
        patch("src.api.routes.control._get_scheduler", return_value=engine),
    ):
        mock_settings.return_value = MagicMock(SCHEDULER_ENABLED=True)
        resp = await client.get("/api/control/scheduler/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["is_running"] is True
    assert data["is_paused"] is False
    assert len(data["jobs"]) == 2
    assert data["jobs"][0]["name"] == "daily_report"


@pytest.mark.asyncio
async def test_scheduler_status_disabled(client):
    """GET /api/control/scheduler/status → disabled when SCHEDULER_ENABLED=False."""
    with patch("src.api.routes.control.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(SCHEDULER_ENABLED=False)
        resp = await client.get("/api/control/scheduler/status")

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


@pytest.mark.asyncio
async def test_scheduler_pause(client):
    """POST /api/control/scheduler/pause → 200 + paused."""
    engine = _mock_scheduler()
    with (
        patch("src.api.routes.control.get_settings") as mock_settings,
        patch("src.api.routes.control._get_scheduler", return_value=engine),
    ):
        mock_settings.return_value = MagicMock(SCHEDULER_ENABLED=True)
        resp = await client.post("/api/control/scheduler/pause")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "paused"
    assert "paused_at" in data
    engine.pause_all.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_resume(client):
    """POST /api/control/scheduler/resume → 200 + resumed."""
    engine = _mock_scheduler()
    with (
        patch("src.api.routes.control.get_settings") as mock_settings,
        patch("src.api.routes.control._get_scheduler", return_value=engine),
    ):
        mock_settings.return_value = MagicMock(SCHEDULER_ENABLED=True)
        resp = await client.post("/api/control/scheduler/resume")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resumed"
    assert "resumed_at" in data
    engine.resume_all.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_run_job(client):
    """POST /api/control/scheduler/run/daily_report → 200 + triggered."""
    engine = _mock_scheduler()
    with (
        patch("src.api.routes.control.get_settings") as mock_settings,
        patch("src.api.routes.control._get_scheduler", return_value=engine),
    ):
        mock_settings.return_value = MagicMock(SCHEDULER_ENABLED=True)
        resp = await client.post("/api/control/scheduler/run/daily_report")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "triggered"
    assert data["job_name"] == "daily_report"
    engine.run_job_now.assert_called_once_with("daily_report")


@pytest.mark.asyncio
async def test_scheduler_run_invalid_job(client):
    """POST /api/control/scheduler/run/invalid → 400 + valid_names."""
    engine = _mock_scheduler()
    engine.run_job_now = AsyncMock(side_effect=ValueError("Unknown job: invalid"))
    with (
        patch("src.api.routes.control.get_settings") as mock_settings,
        patch("src.api.routes.control._get_scheduler", return_value=engine),
    ):
        mock_settings.return_value = MagicMock(SCHEDULER_ENABLED=True)
        resp = await client.post("/api/control/scheduler/run/invalid")

    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "invalid_job_name"
    assert "daily_report" in data["valid_names"]
    assert len(data["valid_names"]) == 9


@pytest.mark.asyncio
async def test_job_history(client, mock_db_session):
    """GET /api/control/jobs/history → 필터링 + 페이지네이션."""
    rows = [
        _mock_job_execution_row(id=1, job_name="daily_report", status="success"),
        _mock_job_execution_row(id=2, job_name="stop_loss_check", status="success"),
    ]

    # First call: count, Second call: items
    mock_db_session.execute = AsyncMock(
        side_effect=[
            _FakeResult(scalar=2),  # count
            _FakeResult(rows=rows),  # items
        ]
    )

    resp = await client.get("/api/control/jobs/history?limit=10&offset=0")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["job_name"] == "daily_report"
    assert data["items"][0]["status"] == "success"
    assert data["items"][0]["duration_sec"] == "5.123"


# ══════════════════════════════════════════════════════════════════════
# Trades API Tests
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_trades(client, mock_db_session):
    """GET /api/trades → 필터링 (symbol, strategy_type, date range)."""
    rows = [
        _mock_position_row(id=1, symbol="005930", realized_pnl=Decimal("40000")),
        _mock_position_row(id=2, symbol="035720", realized_pnl=Decimal("-10000")),
    ]

    mock_db_session.execute = AsyncMock(
        side_effect=[
            _FakeResult(scalar=2),  # count
            _FakeResult(rows=rows),  # items
        ]
    )

    resp = await client.get("/api/trades?symbol=005930&limit=10")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["symbol"] == "005930"
    assert data["items"][0]["status"] == "closed"


@pytest.mark.asyncio
async def test_list_trades_empty(client, mock_db_session):
    """GET /api/trades → 빈 결과 → items=[], total=0."""
    mock_db_session.execute = AsyncMock(
        side_effect=[
            _FakeResult(scalar=0),  # count
            _FakeResult(rows=[]),  # items
        ]
    )

    resp = await client.get("/api/trades")

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_trades_summary(client, mock_db_session):
    """GET /api/trades/summary → PerformanceMetrics 반환."""
    from src.core.models import PerformanceMetrics

    mock_metrics = PerformanceMetrics(
        period_start=date(2026, 2, 25),
        period_end=date(2026, 3, 27),
        total_return_pct=Decimal("5.20"),
        sharpe_ratio=Decimal("1.50"),
        sortino_ratio=Decimal("2.00"),
        max_drawdown_pct=Decimal("3.10"),
        win_rate_pct=Decimal("60.00"),
        avg_win_pct=Decimal("8.00"),
        avg_loss_pct=Decimal("4.00"),
        profit_factor=Decimal("2.40"),
        total_trades=5,
        winning_trades=3,
        losing_trades=2,
    )

    closed_positions = [
        _mock_position_row(id=i, strategy_type="position", realized_pnl=Decimal("10000") if i <= 3 else Decimal("-5000"))
        for i in range(1, 6)
    ]

    with (
        patch("src.api.routes.trades.get_session_factory") as mock_sf,
        patch("src.api.routes.trades.ReportDataFetcher") as MockFetcher,
        patch("src.api.routes.trades.PerformanceCalculator") as MockCalc,
    ):
        fetcher_inst = AsyncMock()
        fetcher_inst.get_closed_positions.return_value = closed_positions
        fetcher_inst.get_portfolio_snapshots.return_value = []
        MockFetcher.return_value = fetcher_inst
        MockCalc.calculate.return_value = mock_metrics

        resp = await client.get("/api/trades/summary?from_date=2026-02-25&to_date=2026-03-27")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 5
    assert data["winning_trades"] == 3
    assert data["win_rate_pct"] == "60.00"
    assert data["sharpe_ratio"] == "1.50"
    assert "strategy_breakdown" in data
    assert data["strategy_breakdown"]["position"]["total"] == 5


@pytest.mark.asyncio
async def test_trades_summary_no_trades(client, mock_db_session):
    """GET /api/trades/summary → 거래 0건 → 기본 지표."""
    from src.core.models import PerformanceMetrics

    mock_metrics = PerformanceMetrics(
        period_start=date(2026, 2, 25),
        period_end=date(2026, 3, 27),
        total_return_pct=Decimal("0"),
        max_drawdown_pct=Decimal("0"),
        win_rate_pct=Decimal("0"),
        avg_win_pct=Decimal("0"),
        avg_loss_pct=Decimal("0"),
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
    )

    with (
        patch("src.api.routes.trades.get_session_factory") as mock_sf,
        patch("src.api.routes.trades.ReportDataFetcher") as MockFetcher,
        patch("src.api.routes.trades.PerformanceCalculator") as MockCalc,
    ):
        fetcher_inst = AsyncMock()
        fetcher_inst.get_closed_positions.return_value = []
        fetcher_inst.get_portfolio_snapshots.return_value = []
        MockFetcher.return_value = fetcher_inst
        MockCalc.calculate.return_value = mock_metrics

        resp = await client.get("/api/trades/summary")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 0
    assert data["total_return_pct"] == "0"
    assert data["strategy_breakdown"] == {}


@pytest.mark.asyncio
async def test_trade_detail(client, mock_db_session):
    """GET /api/trades/{position_id} → 포지션 + 주문 + 체결."""
    position = _mock_position_row(id=1)
    orders = [_mock_order_row(id=100, position_id=1)]
    executions = [_mock_execution_row(id=200, order_id=100)]

    mock_db_session.execute = AsyncMock(
        side_effect=[
            _FakeResult(scalar=position),   # position query
            _FakeResult(rows=orders),        # orders query
            _FakeResult(rows=executions),    # executions query
        ]
    )

    resp = await client.get("/api/trades/1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["position"]["id"] == 1
    assert data["position"]["symbol"] == "005930"
    assert len(data["orders"]) == 1
    assert data["orders"][0]["id"] == 100
    assert len(data["executions"]) == 1
    assert data["executions"][0]["order_id"] == 100
    assert data["executions"][0]["commission"] == "390"


@pytest.mark.asyncio
async def test_trade_detail_not_found(client, mock_db_session):
    """GET /api/trades/999 → 404."""
    mock_db_session.execute = AsyncMock(
        return_value=_FakeResult(scalar=None),
    )

    resp = await client.get("/api/trades/999")

    assert resp.status_code == 404
    assert resp.json()["error"] == "position_not_found"
