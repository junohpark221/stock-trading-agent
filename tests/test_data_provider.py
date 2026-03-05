"""Unit tests for KISDataProvider and collector functions.

All DB, Redis, and KIS API calls are mocked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from src.broker.kis.client import KISClient
from src.core.enums import MarketType
from src.core.exceptions import CacheError, DatabaseError
from src.core.models import OHLCV, StockInfo
from src.data.cache import RedisCache
from src.data.collector import OHLCVCollectionSummary, collect_daily_ohlcv, collect_stock_master
from src.data.providers.kis_provider import KISDataProvider

from conftest import make_settings


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_session_factory(session=None):
    """Create a mock async session factory (async with pattern)."""
    mock_session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, mock_session


def _make_provider(client=None, cache=None, session_factory=None, settings=None):
    """KISDataProvider with all mocked deps."""
    if session_factory is None:
        sf, _ = _mock_session_factory()
    else:
        sf = session_factory
    return KISDataProvider(
        client=client or AsyncMock(spec=KISClient),
        cache=cache or AsyncMock(spec=RedisCache),
        session_factory=sf,
        settings=settings or make_settings(),
    )


def _make_db_result(rowcount=0, scalar_one=0, scalars_all=None):
    """Create a mock DB execute result."""
    result = MagicMock()
    result.rowcount = rowcount
    result.scalar_one = MagicMock(return_value=scalar_one)
    result.scalars = MagicMock()
    result.scalars.return_value.all.return_value = scalars_all or []
    return result


def _make_stock_row(**kwargs):
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


def _make_ohlcv_row(**kwargs):
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


# ═══════════════════════════════════════════════════════════════════════
# 3.1 KISDataProvider Lifecycle
# ═══════════════════════════════════════════════════════════════════════


class TestKISDataProviderLifecycle:
    def test_provider_name(self):
        p = _make_provider()
        assert p.provider_name == "kis"

    @pytest.mark.asyncio
    async def test_initialize(self):
        client = AsyncMock(spec=KISClient)
        p = _make_provider(client=client)
        await p.initialize()
        client.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown(self):
        client = AsyncMock(spec=KISClient)
        p = _make_provider(client=client)
        await p.shutdown()
        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_health_check_true(self):
        client = AsyncMock(spec=KISClient)
        mock_session = MagicMock()
        mock_session.closed = False
        client._session = mock_session
        p = _make_provider(client=client)
        assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false(self):
        client = AsyncMock(spec=KISClient)
        client._session = None
        p = _make_provider(client=client)
        assert await p.health_check() is False


# ═══════════════════════════════════════════════════════════════════════
# 3.2 sync_stock_master
# ═══════════════════════════════════════════════════════════════════════


class TestSyncStockMaster:
    @pytest.mark.asyncio
    async def test_empty_api_returns_zero(self):
        client = AsyncMock(spec=KISClient)
        client.get_stock_master = AsyncMock(return_value=[])
        sf, session = _mock_session_factory()
        p = _make_provider(client=client, session_factory=sf)

        result = await p.sync_stock_master()
        assert result == 0
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upsert_single_batch(self):
        client = AsyncMock(spec=KISClient)
        stocks = [
            StockInfo(symbol=f"{i:06d}", name=f"Stock{i}", market_type=MarketType.KOSPI)
            for i in range(100)
        ]
        client.get_stock_master = AsyncMock(return_value=stocks)

        sf, session = _mock_session_factory()
        upsert_result = _make_db_result(rowcount=100)
        deactivate_result = _make_db_result(rowcount=0)
        session.execute = AsyncMock(side_effect=[upsert_result, deactivate_result])
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        result = await p.sync_stock_master()
        assert result == 100
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upsert_multi_batch(self):
        client = AsyncMock(spec=KISClient)
        stocks = [
            StockInfo(symbol=f"{i:06d}", name=f"Stock{i}", market_type=MarketType.KOSPI)
            for i in range(1200)
        ]
        client.get_stock_master = AsyncMock(return_value=stocks)

        sf, session = _mock_session_factory()
        # 3 upsert batches (500+500+200) + 1 deactivate
        results = [
            _make_db_result(rowcount=500),
            _make_db_result(rowcount=500),
            _make_db_result(rowcount=200),
            _make_db_result(rowcount=0),
        ]
        session.execute = AsyncMock(side_effect=results)
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        result = await p.sync_stock_master()
        assert result == 1200

    @pytest.mark.asyncio
    async def test_deactivate_missing(self):
        client = AsyncMock(spec=KISClient)
        stocks = [StockInfo(symbol="005930", name="삼성전자", market_type=MarketType.KOSPI)]
        client.get_stock_master = AsyncMock(return_value=stocks)

        sf, session = _mock_session_factory()
        upsert_result = _make_db_result(rowcount=1)
        deactivate_result = _make_db_result(rowcount=5)
        session.execute = AsyncMock(side_effect=[upsert_result, deactivate_result])
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        await p.sync_stock_master()
        # Deactivate was called (2nd execute call)
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_invalidated(self):
        client = AsyncMock(spec=KISClient)
        stocks = [StockInfo(symbol="005930", name="삼성전자", market_type=MarketType.KOSPI)]
        client.get_stock_master = AsyncMock(return_value=stocks)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        await p.sync_stock_master()
        cache.clear_namespace.assert_awaited_once_with("kis:master")

    @pytest.mark.asyncio
    async def test_cache_error_logged(self):
        client = AsyncMock(spec=KISClient)
        stocks = [StockInfo(symbol="005930", name="삼성전자", market_type=MarketType.KOSPI)]
        client.get_stock_master = AsyncMock(return_value=stocks)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        cache.clear_namespace = AsyncMock(side_effect=CacheError("redis down"))
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        result = await p.sync_stock_master()
        assert result == 1  # Still returns count despite cache error

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        client = AsyncMock(spec=KISClient)
        stocks = [StockInfo(symbol="005930", name="삼성전자", market_type=MarketType.KOSPI)]
        client.get_stock_master = AsyncMock(return_value=stocks)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(client=client, session_factory=sf)
        with pytest.raises(DatabaseError):
            await p.sync_stock_master()


# ═══════════════════════════════════════════════════════════════════════
# 3.3 sync_daily_ohlcv
# ═══════════════════════════════════════════════════════════════════════


class TestSyncDailyOHLCV:
    @pytest.mark.asyncio
    async def test_empty_bars_returns_zero(self):
        client = AsyncMock(spec=KISClient)
        client.get_daily_ohlcv = AsyncMock(return_value=[])
        sf, session = _mock_session_factory()
        p = _make_provider(client=client, session_factory=sf)

        result = await p.sync_daily_ohlcv("005930")
        assert result == 0

    @pytest.mark.asyncio
    async def test_upsert_single_batch(self):
        client = AsyncMock(spec=KISClient)
        from datetime import timedelta
        base = date(2026, 1, 1)
        bars = [
            OHLCV(symbol="005930", date=base + timedelta(days=i),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
            for i in range(50)
        ]
        client.get_daily_ohlcv = AsyncMock(return_value=bars)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=50))
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        result = await p.sync_daily_ohlcv("005930")
        assert result == 50

    @pytest.mark.asyncio
    async def test_upsert_multi_batch(self):
        client = AsyncMock(spec=KISClient)
        bars = [
            OHLCV(symbol="005930", date=date(2026, 1, 1),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
        ] * 700
        client.get_daily_ohlcv = AsyncMock(return_value=bars)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=[
            _make_db_result(rowcount=500),
            _make_db_result(rowcount=200),
        ])
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        result = await p.sync_daily_ohlcv("005930")
        assert result == 700

    @pytest.mark.asyncio
    async def test_cache_deleted(self):
        client = AsyncMock(spec=KISClient)
        bars = [
            OHLCV(symbol="005930", date=date(2026, 3, 1),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
        ]
        client.get_daily_ohlcv = AsyncMock(return_value=bars)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        await p.sync_daily_ohlcv("005930")
        cache.delete.assert_awaited_once_with("kis:ohlcv", "005930")

    @pytest.mark.asyncio
    async def test_cache_error_logged(self):
        client = AsyncMock(spec=KISClient)
        bars = [
            OHLCV(symbol="005930", date=date(2026, 3, 1),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
        ]
        client.get_daily_ohlcv = AsyncMock(return_value=bars)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        cache = AsyncMock(spec=RedisCache)
        cache.delete = AsyncMock(side_effect=CacheError("redis down"))
        p = _make_provider(client=client, cache=cache, session_factory=sf)

        result = await p.sync_daily_ohlcv("005930")
        assert result == 1

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        client = AsyncMock(spec=KISClient)
        bars = [
            OHLCV(symbol="005930", date=date(2026, 3, 1),
                   open=Decimal("72000"), high=Decimal("73000"),
                   low=Decimal("71000"), close=Decimal("72500"), volume=1000)
        ]
        client.get_daily_ohlcv = AsyncMock(return_value=bars)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(client=client, session_factory=sf)
        with pytest.raises(DatabaseError):
            await p.sync_daily_ohlcv("005930")


# ═══════════════════════════════════════════════════════════════════════
# 3.4 fetch_stock_master
# ═══════════════════════════════════════════════════════════════════════


class TestFetchStockMaster:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {"symbol": "005930", "name": "삼성전자", "market_type": "kospi"},
        ])
        sf, session = _mock_session_factory()
        p = _make_provider(cache=cache, session_factory=sf)

        result = await p.fetch_stock_master()
        assert len(result) == 1
        assert result[0].symbol == "005930"
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_stock_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_stock_master()
        assert len(result) == 1
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_miss_db_empty(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_stock_master()
        assert result == []
        cache.set_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_read_error_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_stock_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_stock_master()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_cache_write_error_logged(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock(side_effect=CacheError("redis down"))

        row = _make_stock_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_stock_master()
        assert len(result) == 1  # Still returns data despite cache write error

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(cache=cache, session_factory=sf)
        with pytest.raises(DatabaseError):
            await p.fetch_stock_master()

    @pytest.mark.asyncio
    async def test_db_row_mapping(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_stock_row(sector=None, listed_shares=None, market_cap_krw=None)
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_stock_master()
        assert result[0].sector == ""
        assert result[0].listed_shares == 0
        assert result[0].market_cap_krw == 0


# ═══════════════════════════════════════════════════════════════════════
# 3.5 fetch_daily_ohlcv
# ═══════════════════════════════════════════════════════════════════════


class TestFetchDailyOHLCV:
    @pytest.mark.asyncio
    async def test_cache_hit_no_filter(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {"symbol": "005930", "date": "2026-03-01",
             "open": "72000", "high": "73000", "low": "71000",
             "close": "72500", "volume": 1000},
        ])
        sf, session = _mock_session_factory()
        p = _make_provider(cache=cache, session_factory=sf)

        result = await p.fetch_daily_ohlcv("005930")
        assert len(result) == 1
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_hit_with_filter(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {"symbol": "005930", "date": "2026-01-01",
             "open": "72000", "high": "73000", "low": "71000",
             "close": "72500", "volume": 1000},
            {"symbol": "005930", "date": "2026-03-01",
             "open": "73000", "high": "74000", "low": "72000",
             "close": "73500", "volume": 2000},
        ])
        sf, session = _mock_session_factory()
        p = _make_provider(cache=cache, session_factory=sf)

        result = await p.fetch_daily_ohlcv(
            "005930", start_date=date(2026, 2, 1), end_date=date(2026, 3, 31)
        )
        assert len(result) == 1
        assert result[0].date == date(2026, 3, 1)

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_ohlcv_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_daily_ohlcv("005930")
        assert len(result) == 1
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_miss_db_empty(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_daily_ohlcv("005930")
        assert result == []
        cache.set_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filter_start_only(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {"symbol": "005930", "date": "2026-01-01",
             "open": "72000", "high": "73000", "low": "71000",
             "close": "72500", "volume": 1000},
            {"symbol": "005930", "date": "2026-03-01",
             "open": "73000", "high": "74000", "low": "72000",
             "close": "73500", "volume": 2000},
        ])
        p = _make_provider(cache=cache)

        result = await p.fetch_daily_ohlcv("005930", start_date=date(2026, 2, 1))
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_filter_end_only(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {"symbol": "005930", "date": "2026-01-01",
             "open": "72000", "high": "73000", "low": "71000",
             "close": "72500", "volume": 1000},
            {"symbol": "005930", "date": "2026-03-01",
             "open": "73000", "high": "74000", "low": "72000",
             "close": "73500", "volume": 2000},
        ])
        p = _make_provider(cache=cache)

        result = await p.fetch_daily_ohlcv("005930", end_date=date(2026, 2, 1))
        assert len(result) == 1
        assert result[0].date == date(2026, 1, 1)

    @pytest.mark.asyncio
    async def test_cache_read_error_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_ohlcv_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_daily_ohlcv("005930")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(cache=cache, session_factory=sf)
        with pytest.raises(DatabaseError):
            await p.fetch_daily_ohlcv("005930")


# ═══════════════════════════════════════════════════════════════════════
# 3.6 collect_stock_master
# ═══════════════════════════════════════════════════════════════════════


class TestCollectStockMaster:
    @pytest.mark.asyncio
    async def test_delegates_to_provider(self):
        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_stock_master = AsyncMock(return_value=100)

        result = await collect_stock_master(provider)
        assert result == 100
        provider.sync_stock_master.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_propagates(self):
        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_stock_master = AsyncMock(side_effect=DatabaseError("fail"))

        with pytest.raises(DatabaseError):
            await collect_stock_master(provider)


# ═══════════════════════════════════════════════════════════════════════
# 3.7 collect_daily_ohlcv
# ═══════════════════════════════════════════════════════════════════════


class TestCollectDailyOHLCV:
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_daily_ohlcv = AsyncMock(return_value=100)

        result = await collect_daily_ohlcv(provider, ["A", "B", "C"])
        assert result.succeeded == 3
        assert result.failed == 0
        assert result.total_rows == 300
        assert result.total_symbols == 3

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_daily_ohlcv = AsyncMock(
            side_effect=[100, DatabaseError("fail"), 100]
        )

        result = await collect_daily_ohlcv(provider, ["A", "B", "C"])
        assert result.succeeded == 2
        assert result.failed == 1
        assert result.failed_symbols == ["B"]

    @pytest.mark.asyncio
    async def test_all_fail(self):
        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_daily_ohlcv = AsyncMock(side_effect=DatabaseError("fail"))

        result = await collect_daily_ohlcv(provider, ["A", "B", "C"])
        assert result.succeeded == 0
        assert result.failed == 3

    @pytest.mark.asyncio
    async def test_empty_symbols(self):
        provider = AsyncMock()
        provider.provider_name = "kis"

        result = await collect_daily_ohlcv(provider, [])
        assert result.total_symbols == 0
        assert result.succeeded == 0

    @pytest.mark.asyncio
    async def test_period_days_forwarded(self):
        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_daily_ohlcv = AsyncMock(return_value=200)

        await collect_daily_ohlcv(provider, ["A"], period_days=200)
        provider.sync_daily_ohlcv.assert_awaited_once_with("A", period_days=200)
