"""Unit tests for Backtest API endpoints.

Uses httpx AsyncClient with FastAPI test transport.
All external dependencies (DB, backtest engine) are mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.db.session import get_db_session

# ── Helpers ───────────────────────────────────────────────────────────


def _mock_run_row(**kwargs) -> MagicMock:
    """Mock BacktestRun ORM row."""
    row = MagicMock()
    row.run_id = kwargs.get("run_id", uuid.uuid4())
    row.strategy_type = kwargs.get("strategy_type", "position")
    row.mode = kwargs.get("mode", "technical")
    row.start_date = kwargs.get("start_date", date(2025, 1, 1))
    row.end_date = kwargs.get("end_date", date(2025, 6, 30))
    row.initial_capital = kwargs.get("initial_capital", Decimal("10000000"))
    row.slippage_bps = kwargs.get("slippage_bps", 10)
    row.symbols = kwargs.get("symbols", ["005930"])
    row.parameters = kwargs.get("parameters", {})
    row.status = kwargs.get("status", "completed")
    row.result_metrics = kwargs.get("result_metrics", {
        "period_start": "2025-01-01",
        "period_end": "2025-06-30",
        "total_return_pct": "5.23",
        "annualized_return_pct": "10.46",
        "sharpe_ratio": "1.25",
        "sortino_ratio": "1.80",
        "max_drawdown_pct": "-3.50",
        "win_rate_pct": "60.00",
        "avg_win_pct": "3.20",
        "avg_loss_pct": "-1.50",
        "profit_factor": "2.13",
        "total_trades": 15,
        "winning_trades": 9,
        "losing_trades": 6,
    })
    row.total_trades = kwargs.get("total_trades", 15)
    row.error_message = kwargs.get("error_message")
    row.started_at = kwargs.get("started_at", datetime(2026, 3, 25, 9, 0, tzinfo=UTC))
    row.completed_at = kwargs.get(
        "completed_at", datetime(2026, 3, 25, 9, 5, tzinfo=UTC),
    )
    return row


def _mock_trade_row(**kwargs) -> MagicMock:
    """Mock BacktestTrade ORM row."""
    row = MagicMock()
    row.symbol = kwargs.get("symbol", "005930")
    row.side = kwargs.get("side", "buy")
    row.quantity = kwargs.get("quantity", 10)
    row.price = kwargs.get("price", Decimal("78000"))
    row.commission = kwargs.get("commission", Decimal("117"))
    row.slippage = kwargs.get("slippage", Decimal("78"))
    row.trade_date = kwargs.get("trade_date", date(2025, 1, 15))
    row.pnl = kwargs.get("pnl")
    row.exit_reason = kwargs.get("exit_reason")
    return row


def _scalar_one(value):
    """Mock execute().scalar_one() returning a value."""
    result = MagicMock()
    result.scalar_one = MagicMock(return_value=value)
    return result


def _scalar_one_or_none(value):
    """Mock execute().scalar_one_or_none() returning a value."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=value)
    return result


def _scalars_all(values: list):
    """Mock execute().scalars().all() returning a list."""
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=values)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars_mock)
    return result


# ── Fixtures ──────────────────────────────────────────────────────────


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


# ═══════════════════════════════════════════════════════════════════════
# POST /api/backtest/run
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@patch("src.api.routes.backtest._execute_backtest")
async def test_post_run_returns_run_id(mock_exec, client, mock_db_session):
    """POST /api/backtest/run — 정상 요청 → run_id + status=pending."""
    mock_db_session.commit = AsyncMock()

    resp = await client.post(
        "/api/backtest/run",
        json={
            "strategy_type": "position",
            "start_date": "2025-01-01",
            "end_date": "2025-06-30",
            "symbols": ["005930"],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "run_id" in data
    # UUID 형식 검증
    uuid.UUID(data["run_id"])
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_post_run_invalid_dates(client):
    """POST /api/backtest/run — start > end → 422."""
    resp = await client.post(
        "/api/backtest/run",
        json={
            "strategy_type": "position",
            "start_date": "2025-06-30",
            "end_date": "2025-01-01",
            "symbols": ["005930"],
        },
    )

    assert resp.status_code == 422
    assert "start_date" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════
# GET /api/backtest/runs
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_runs_empty(client, mock_db_session):
    """GET /api/backtest/runs — 빈 목록."""
    mock_db_session.execute = AsyncMock(
        side_effect=[
            _scalar_one(0),     # count
            _scalars_all([]),   # rows
        ],
    )

    resp = await client.get("/api/backtest/runs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_runs_filter_strategy(client, mock_db_session):
    """GET /api/backtest/runs?strategy_type=position — 필터 동작."""
    row = _mock_run_row(strategy_type="position")
    mock_db_session.execute = AsyncMock(
        side_effect=[
            _scalar_one(1),       # count
            _scalars_all([row]),  # rows
        ],
    )

    resp = await client.get("/api/backtest/runs?strategy_type=position")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["strategy_type"] == "position"


# ═══════════════════════════════════════════════════════════════════════
# GET /api/backtest/runs/{run_id}
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_run_found(client, mock_db_session):
    """GET /api/backtest/runs/{run_id} — 존재하는 run."""
    rid = uuid.uuid4()
    row = _mock_run_row(run_id=rid, status="completed")
    trade = _mock_trade_row()

    mock_db_session.execute = AsyncMock(
        side_effect=[
            _scalar_one_or_none(row),    # run
            _scalars_all([trade]),       # trades
        ],
    )

    resp = await client.get(f"/api/backtest/runs/{rid}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["run"]["status"] == "completed"
    assert data["run"]["result_metrics"] is not None
    assert len(data["trades"]) == 1


@pytest.mark.asyncio
async def test_get_run_not_found(client, mock_db_session):
    """GET /api/backtest/runs/{uuid} — 없는 UUID → 404."""
    mock_db_session.execute = AsyncMock(
        return_value=_scalar_one_or_none(None),
    )

    resp = await client.get(f"/api/backtest/runs/{uuid.uuid4()}")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_run_include_trades_false(client, mock_db_session):
    """GET /api/backtest/runs/{run_id}?include_trades=false — trades=null."""
    rid = uuid.uuid4()
    row = _mock_run_row(run_id=rid)

    mock_db_session.execute = AsyncMock(
        return_value=_scalar_one_or_none(row),
    )

    resp = await client.get(f"/api/backtest/runs/{rid}?include_trades=false")

    assert resp.status_code == 200
    data = resp.json()
    assert data["trades"] is None


# ═══════════════════════════════════════════════════════════════════════
# GET /api/backtest/compare
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compare_basic(client, mock_db_session):
    """GET /api/backtest/compare — 2개 run 비교."""
    rid1, rid2 = uuid.uuid4(), uuid.uuid4()
    row1 = _mock_run_row(
        run_id=rid1,
        result_metrics={
            "sharpe_ratio": "1.50",
            "total_return_pct": "8.00",
            "max_drawdown_pct": "-5.00",
        },
    )
    row2 = _mock_run_row(
        run_id=rid2,
        result_metrics={
            "sharpe_ratio": "1.20",
            "total_return_pct": "12.00",
            "max_drawdown_pct": "-3.00",
        },
    )

    mock_db_session.execute = AsyncMock(
        return_value=_scalars_all([row1, row2]),
    )

    resp = await client.get(
        f"/api/backtest/compare?run_ids={rid1},{rid2}",
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["runs"]) == 2
    assert data["best_sharpe_run_id"] == str(rid1)
    assert data["best_return_run_id"] == str(rid2)
    assert data["lowest_mdd_run_id"] == str(rid2)  # -3.00 < -5.00 (abs)


@pytest.mark.asyncio
async def test_compare_invalid_id(client, mock_db_session):
    """GET /api/backtest/compare — 없는 run_id → skip + warnings."""
    rid1, rid2 = uuid.uuid4(), uuid.uuid4()
    row1 = _mock_run_row(run_id=rid1)

    mock_db_session.execute = AsyncMock(
        return_value=_scalars_all([row1]),
    )

    resp = await client.get(
        f"/api/backtest/compare?run_ids={rid1},{rid2}",
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["runs"]) == 1
    assert len(data["warnings"]) == 1
    assert str(rid2) in data["warnings"][0]


@pytest.mark.asyncio
async def test_compare_too_many(client, mock_db_session):
    """GET /api/backtest/compare — 11개 → 400."""
    ids = ",".join(str(uuid.uuid4()) for _ in range(11))

    resp = await client.get(f"/api/backtest/compare?run_ids={ids}")

    assert resp.status_code == 400
    assert "Maximum 10" in resp.json()["detail"]
