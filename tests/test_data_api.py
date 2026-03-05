"""Unit tests for Data API endpoints (/api/data/*).

Uses httpx AsyncClient with FastAPI test transport.
DB session is dependency-overridden with mocks.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_stock_row(**kwargs):
    """StockMaster ORM row mock."""
    row = MagicMock()
    row.symbol = kwargs.get("symbol", "005930")
    row.name = kwargs.get("name", "삼성전자")
    row.market_type = kwargs.get("market_type", "kospi")
    row.sector = kwargs.get("sector", None)
    row.listed_shares = kwargs.get("listed_shares", None)
    row.market_cap_krw = kwargs.get("market_cap_krw", None)
    row.is_active = kwargs.get("is_active", True)
    return row


def _mock_ohlcv_row(**kwargs):
    """DailyOHLCV ORM row mock."""
    row = MagicMock()
    row.symbol = kwargs.get("symbol", "005930")
    row.date = kwargs.get("date", date(2026, 3, 1))
    row.open = kwargs.get("open", Decimal("72000"))
    row.high = kwargs.get("high", Decimal("73000"))
    row.low = kwargs.get("low", Decimal("71000"))
    row.close = kwargs.get("close", Decimal("72500"))
    row.volume = kwargs.get("volume", 15000000)
    row.trading_value = kwargs.get("trading_value", Decimal("1000000000"))
    return row


def _make_execute_results(*results):
    """Return a side_effect list of mock execute results."""
    mocks = []
    for r in results:
        m = MagicMock()
        if isinstance(r, int):
            # scalar_one → count
            m.scalar_one = MagicMock(return_value=r)
            m.scalars = MagicMock()
            m.scalars.return_value.all.return_value = []
        elif isinstance(r, list):
            # scalars().all() → row list
            m.scalars = MagicMock()
            m.scalars.return_value.all.return_value = r
        elif isinstance(r, tuple):
            # Named row (stats)
            m.one = MagicMock(return_value=r[0])
        else:
            m = r
        mocks.append(m)
    return mocks


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    """Clean up FastAPI dependency overrides after each test."""
    yield
    main_mod.app.dependency_overrides.clear()


def _override_session(session):
    """Set up DB session dependency override."""
    async def _dep():
        yield session
    main_mod.app.dependency_overrides[get_db_session] = _dep


def _client():
    """Create httpx AsyncClient for testing."""
    return AsyncClient(
        transport=ASGITransport(app=main_mod.app),
        base_url="http://test",
    )


# ═══════════════════════════════════════════════════════════════════════
# 4.1 GET /api/data/stocks
# ═══════════════════════════════════════════════════════════════════════


class TestListStocks:
    @pytest.mark.asyncio
    async def test_default_params(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_stock_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["limit"] == 100
        assert data["offset"] == 0
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_market_filter_kospi(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_stock_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks?market=kospi")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_market_invalid(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks?market=nasdaq")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_active_only_false(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks?active_only=false")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_pagination(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=20)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_stock_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks?limit=5&offset=10")

        data = resp.json()
        assert data["limit"] == 5
        assert data["offset"] == 10

    @pytest.mark.asyncio
    async def test_empty_result(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks")

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_limit_bounds(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp0 = await ac.get("/api/data/stocks?limit=0")
            resp1001 = await ac.get("/api/data/stocks?limit=1001")

        assert resp0.status_code == 422
        assert resp1001.status_code == 422

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stocks")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# 4.2 GET /api/data/ohlcv/{symbol}
# ═══════════════════════════════════════════════════════════════════════


class TestGetOHLCV:
    @pytest.mark.asyncio
    async def test_success(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_ohlcv_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/005930")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_404_no_data_no_filter(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        session.execute = AsyncMock(return_value=count_result)
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/999999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_200_empty_with_date_filter(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/005930?start_date=2026-01-01")

        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_date_range(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_ohlcv_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/data/ohlcv/005930?start_date=2026-01-01&end_date=2026-02-01"
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_start_after_end_400(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/data/ohlcv/005930?start_date=2026-03-01&end_date=2026-01-01"
            )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_start_date_only(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_ohlcv_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/005930?start_date=2026-01-01")

        assert resp.status_code == 200
        data = resp.json()
        assert data["end_date"] is None

    @pytest.mark.asyncio
    async def test_end_date_only(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_ohlcv_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/005930?end_date=2026-03-01")

        assert resp.status_code == 200
        data = resp.json()
        assert data["start_date"] is None

    @pytest.mark.asyncio
    async def test_pagination(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=50)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_ohlcv_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/005930?limit=10&offset=5")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/ohlcv/005930")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# 4.3 GET /api/data/stats
# ═══════════════════════════════════════════════════════════════════════


class TestGetDataStats:
    def _make_stock_stats_row(self, total=100, active=80, kospi=50, kosdaq=30):
        row = MagicMock()
        row.total = total
        row.active = active
        row.kospi = kospi
        row.kosdaq = kosdaq
        return row

    def _make_ohlcv_stats_row(
        self, total_rows=1000, symbols=50, date_min=date(2026, 1, 1), date_max=date(2026, 3, 1)
    ):
        row = MagicMock()
        row.total_rows = total_rows
        row.symbols = symbols
        row.date_min = date_min
        row.date_max = date_max
        return row

    @pytest.mark.asyncio
    async def test_success(self):
        session = AsyncMock()
        stock_result = MagicMock()
        stock_result.one = MagicMock(return_value=self._make_stock_stats_row())
        ohlcv_result = MagicMock()
        ohlcv_result.one = MagicMock(return_value=self._make_ohlcv_stats_row())
        session.execute = AsyncMock(side_effect=[stock_result, ohlcv_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["stock_master_total"] == 100
        assert data["stock_master_active"] == 80
        assert data["ohlcv_total_rows"] == 1000
        assert data["ohlcv_symbols"] == 50

    @pytest.mark.asyncio
    async def test_empty_database(self):
        session = AsyncMock()
        stock_result = MagicMock()
        stock_result.one = MagicMock(
            return_value=self._make_stock_stats_row(total=0, active=0, kospi=0, kosdaq=0)
        )
        ohlcv_result = MagicMock()
        ohlcv_result.one = MagicMock(
            return_value=self._make_ohlcv_stats_row(
                total_rows=0, symbols=0, date_min=None, date_max=None
            )
        )
        session.execute = AsyncMock(side_effect=[stock_result, ohlcv_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["stock_master_total"] == 0
        assert data["ohlcv_date_min"] is None

    @pytest.mark.asyncio
    async def test_kospi_kosdaq_counts(self):
        session = AsyncMock()
        stock_result = MagicMock()
        stock_result.one = MagicMock(
            return_value=self._make_stock_stats_row(kospi=100, kosdaq=50)
        )
        ohlcv_result = MagicMock()
        ohlcv_result.one = MagicMock(return_value=self._make_ohlcv_stats_row())
        session.execute = AsyncMock(side_effect=[stock_result, ohlcv_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stats")

        data = resp.json()
        assert data["kospi_count"] == 100
        assert data["kosdaq_count"] == 50

    @pytest.mark.asyncio
    async def test_date_range(self):
        session = AsyncMock()
        stock_result = MagicMock()
        stock_result.one = MagicMock(return_value=self._make_stock_stats_row())
        ohlcv_result = MagicMock()
        ohlcv_result.one = MagicMock(
            return_value=self._make_ohlcv_stats_row(
                date_min=date(2025, 6, 1), date_max=date(2026, 3, 1)
            )
        )
        session.execute = AsyncMock(side_effect=[stock_result, ohlcv_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stats")

        data = resp.json()
        assert data["ohlcv_date_min"] == "2025-06-01"
        assert data["ohlcv_date_max"] == "2026-03-01"

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/stats")

        assert resp.status_code == 500
