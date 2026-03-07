"""Unit tests for EcosProvider and FredProvider.

All ECOS/FRED API, DB, and Redis calls are mocked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import AsyncContextManagerMock, make_settings, mock_aiohttp_response

from src.core.enums import DataSourceType
from src.core.exceptions import CacheError, DatabaseError, ExternalAPIError
from src.data.cache import RedisCache
from src.data.providers.ecos_provider import (
    EcosIndicatorConfig,
    EcosProvider,
    _parse_ecos_time,
)
from src.data.providers.fred_provider import FredProvider

# -- Helpers ---------------------------------------------------------------


def _mock_session_factory(session=None):
    """Create a mock async session factory (async with pattern)."""
    mock_session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, mock_session


def _make_ecos_provider(cache=None, session_factory=None, settings=None):
    if session_factory is None:
        sf, _ = _mock_session_factory()
    else:
        sf = session_factory
    return EcosProvider(
        cache=cache or AsyncMock(spec=RedisCache),
        session_factory=sf,
        settings=settings or make_settings(ECOS_API_KEY="test_ecos_key"),
    )


def _make_fred_provider(cache=None, session_factory=None, settings=None):
    if session_factory is None:
        sf, _ = _mock_session_factory()
    else:
        sf = session_factory
    return FredProvider(
        cache=cache or AsyncMock(spec=RedisCache),
        session_factory=sf,
        settings=settings or make_settings(FRED_API_KEY="test_fred_key"),
    )


def _make_db_result(rowcount=0, scalars_all=None):
    result = MagicMock()
    result.rowcount = rowcount
    result.scalars = MagicMock()
    result.scalars.return_value.all.return_value = scalars_all or []
    return result


def _make_indicator_row(**kwargs):
    """EconomicIndicator ORM row mock."""
    row = MagicMock()
    row.source = kwargs.get("source", "ecos")
    row.indicator_code = kwargs.get("indicator_code", "722Y001/0101000")
    row.indicator_name = kwargs.get("indicator_name", "기준금리")
    row.date = kwargs.get("date", date(2026, 3, 1))
    row.value = kwargs.get("value", Decimal("3.50"))
    row.unit = kwargs.get("unit", "%")
    return row


def _setup_provider_with_aiohttp(provider, json_data, status=200):
    resp = mock_aiohttp_response(status=status, json_data=json_data)
    mock_http_session = MagicMock()
    mock_http_session.closed = False
    mock_http_session.get = MagicMock(return_value=AsyncContextManagerMock(resp))
    mock_http_session.close = AsyncMock()
    provider._session = mock_http_session
    return mock_http_session


# =========================================================================
# ECOS Provider Tests
# =========================================================================


class TestEcosProviderLifecycle:
    def test_provider_name(self):
        p = _make_ecos_provider()
        assert p.provider_name == "ecos"

    @pytest.mark.asyncio
    async def test_initialize_creates_session(self):
        p = _make_ecos_provider()
        await p.initialize()
        assert p._session is not None
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_closes_session(self):
        p = _make_ecos_provider()
        await p.initialize()
        await p.shutdown()
        assert p._session is None

    @pytest.mark.asyncio
    async def test_health_check_true(self):
        p = _make_ecos_provider()
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_no_key(self):
        p = _make_ecos_provider(settings=make_settings(ECOS_API_KEY=""))
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_false_no_session(self):
        p = _make_ecos_provider()
        assert await p.health_check() is False


class TestEcosApiGet:
    @pytest.mark.asyncio
    async def test_success(self):
        p = _make_ecos_provider()
        _setup_provider_with_aiohttp(p, {
            "StatisticSearch": {"list_total_count": 1, "row": [{"DATA_VALUE": "3.50"}]}
        })
        result = await p._api_get("some/path")
        assert "StatisticSearch" in result

    @pytest.mark.asyncio
    async def test_error_response(self):
        p = _make_ecos_provider()
        _setup_provider_with_aiohttp(p, {
            "RESULT": {"CODE": "ERROR-001", "MESSAGE": "Invalid parameter"}
        })
        with pytest.raises(ExternalAPIError, match="ERROR-001"):
            await p._api_get("some/path")

    @pytest.mark.asyncio
    async def test_info_200_no_data(self):
        p = _make_ecos_provider()
        _setup_provider_with_aiohttp(p, {
            "RESULT": {"CODE": "INFO-200", "MESSAGE": "no data"}
        })
        result = await p._api_get("some/path")
        assert result["RESULT"]["CODE"] == "INFO-200"


class TestEcosDateParsing:
    def test_daily(self):
        assert _parse_ecos_time("20260301", "D") == date(2026, 3, 1)

    def test_monthly(self):
        assert _parse_ecos_time("202603", "M") == date(2026, 3, 1)

    def test_quarterly_q1(self):
        assert _parse_ecos_time("2026Q1", "Q") == date(2026, 1, 1)

    def test_quarterly_q4(self):
        assert _parse_ecos_time("2026Q4", "Q") == date(2026, 10, 1)

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError, match="Unknown ECOS period"):
            _parse_ecos_time("20260301", "X")


class TestEcosSyncIndicator:
    _config = EcosIndicatorConfig("722Y001", "0101000", "D", "기준금리")

    @pytest.mark.asyncio
    async def test_normal_upsert(self):
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=2))
        session.commit = AsyncMock()

        p = _make_ecos_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "StatisticSearch": {
                "list_total_count": 2,
                "row": [
                    {"TIME": "20260301", "DATA_VALUE": "3.50", "UNIT_NAME": "%"},
                    {"TIME": "20260302", "DATA_VALUE": "3.50", "UNIT_NAME": "%"},
                ],
            }
        })

        result = await p.sync_indicator(
            self._config, date(2026, 3, 1), date(2026, 3, 31)
        )
        assert result == 2
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_response_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()

        p = _make_ecos_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "RESULT": {"CODE": "INFO-200", "MESSAGE": "no data"}
        })

        result = await p.sync_indicator(
            self._config, date(2026, 3, 1), date(2026, 3, 31)
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        p = _make_ecos_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {
            "RESULT": {"CODE": "ERROR-500", "MESSAGE": "server error"}
        })

        with pytest.raises(ExternalAPIError):
            await p.sync_indicator(
                self._config, date(2026, 3, 1), date(2026, 3, 31)
            )

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_ecos_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "StatisticSearch": {
                "list_total_count": 1,
                "row": [{"TIME": "20260301", "DATA_VALUE": "3.50", "UNIT_NAME": "%"}],
            }
        })

        with pytest.raises(DatabaseError):
            await p.sync_indicator(
                self._config, date(2026, 3, 1), date(2026, 3, 31)
            )


class TestEcosFetchIndicators:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {
                "source": "ecos",
                "indicator_code": "722Y001/0101000",
                "indicator_name": "기준금리",
                "date": "2026-03-01",
                "value": "3.50",
                "unit": "%",
            },
        ])
        sf, session = _mock_session_factory()
        p = _make_ecos_provider(cache=cache, session_factory=sf)

        result = await p.fetch_indicators("722Y001/0101000")
        assert len(result) == 1
        assert result[0].indicator_code == "722Y001/0101000"
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_indicator_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_ecos_provider(cache=cache, session_factory=sf)
        result = await p.fetch_indicators("722Y001/0101000")
        assert len(result) == 1
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_result(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[]))

        p = _make_ecos_provider(cache=cache, session_factory=sf)
        result = await p.fetch_indicators("722Y001/0101000")
        assert result == []
        cache.set_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_error_graceful(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_indicator_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_ecos_provider(cache=cache, session_factory=sf)
        result = await p.fetch_indicators("722Y001/0101000")
        assert len(result) == 1


# =========================================================================
# FRED Provider Tests
# =========================================================================


class TestFredProviderLifecycle:
    def test_provider_name(self):
        p = _make_fred_provider()
        assert p.provider_name == "fred"

    @pytest.mark.asyncio
    async def test_initialize_creates_session(self):
        p = _make_fred_provider()
        await p.initialize()
        assert p._session is not None
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_closes_session(self):
        p = _make_fred_provider()
        await p.initialize()
        await p.shutdown()
        assert p._session is None

    @pytest.mark.asyncio
    async def test_health_check_true(self):
        p = _make_fred_provider()
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_no_key(self):
        p = _make_fred_provider(settings=make_settings(FRED_API_KEY=""))
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_false_no_session(self):
        p = _make_fred_provider()
        assert await p.health_check() is False


class TestFredApiGet:
    @pytest.mark.asyncio
    async def test_success(self):
        p = _make_fred_provider()
        _setup_provider_with_aiohttp(p, {
            "observations": [{"date": "2026-03-01", "value": "5.33"}]
        })
        result = await p._api_get("fred/series/observations", {"series_id": "FEDFUNDS"})
        assert "observations" in result

    @pytest.mark.asyncio
    async def test_error_message(self):
        p = _make_fred_provider()
        _setup_provider_with_aiohttp(p, {"error_message": "Bad Request"})
        with pytest.raises(ExternalAPIError, match="Bad Request"):
            await p._api_get("fred/series", {"series_id": "INVALID"})

    @pytest.mark.asyncio
    async def test_http_error(self):
        p = _make_fred_provider()
        _setup_provider_with_aiohttp(p, {}, status=400)
        with pytest.raises(ExternalAPIError, match="HTTP 400"):
            await p._api_get("fred/series", {"series_id": "FEDFUNDS"})


class TestFredFetchSeriesInfo:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(
            return_value={"title": "Federal Funds Rate", "units": "Percent"}
        )
        p = _make_fred_provider(cache=cache)

        title, units = await p._fetch_series_info("FEDFUNDS")
        assert title == "Federal Funds Rate"
        assert units == "Percent"

    @pytest.mark.asyncio
    async def test_cache_miss_api_call(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        p = _make_fred_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {
            "seriess": [{"title": "Federal Funds Rate", "units": "Percent"}]
        })

        title, units = await p._fetch_series_info("FEDFUNDS")
        assert title == "Federal Funds Rate"
        assert units == "Percent"
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_failure_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)

        p = _make_fred_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"error_message": "fail"})

        title, units = await p._fetch_series_info("FEDFUNDS")
        assert title == "FEDFUNDS"
        assert units is None


class TestFredSyncSeries:
    @pytest.mark.asyncio
    async def test_normal_upsert(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(
            return_value={"title": "Fed Funds", "units": "Percent"}
        )
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=3))
        session.commit = AsyncMock()

        p = _make_fred_provider(cache=cache, session_factory=sf)

        # First call: observations API, Second call: series info (cache hit)
        resp_obs = mock_aiohttp_response(json_data={
            "observations": [
                {"date": "2026-03-01", "value": "5.33"},
                {"date": "2026-03-02", "value": "5.33"},
                {"date": "2026-03-03", "value": "5.34"},
            ]
        })
        mock_http = MagicMock()
        mock_http.closed = False
        mock_http.get = MagicMock(return_value=AsyncContextManagerMock(resp_obs))
        mock_http.close = AsyncMock()
        p._session = mock_http

        result = await p.sync_series("FEDFUNDS", date(2026, 3, 1), date(2026, 3, 31))
        assert result == 3
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dot_value_skipped(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(
            return_value={"title": "VIXCLS", "units": "Index"}
        )
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        p = _make_fred_provider(cache=cache, session_factory=sf)

        resp = mock_aiohttp_response(json_data={
            "observations": [
                {"date": "2026-03-01", "value": "."},
                {"date": "2026-03-02", "value": "18.50"},
            ]
        })
        mock_http = MagicMock()
        mock_http.closed = False
        mock_http.get = MagicMock(return_value=AsyncContextManagerMock(resp))
        mock_http.close = AsyncMock()
        p._session = mock_http

        result = await p.sync_series("VIXCLS", date(2026, 3, 1), date(2026, 3, 31))
        assert result == 1

    @pytest.mark.asyncio
    async def test_empty_observations_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        p = _make_fred_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"observations": []})

        result = await p.sync_series("FEDFUNDS", date(2026, 3, 1), date(2026, 3, 31))
        assert result == 0

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        p = _make_fred_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {
            "error_message": "Bad API key"
        })

        with pytest.raises(ExternalAPIError):
            await p.sync_series("FEDFUNDS", date(2026, 3, 1), date(2026, 3, 31))

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(
            return_value={"title": "Fed Funds", "units": "Percent"}
        )
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_fred_provider(cache=cache, session_factory=sf)

        resp = mock_aiohttp_response(json_data={
            "observations": [{"date": "2026-03-01", "value": "5.33"}]
        })
        mock_http = MagicMock()
        mock_http.closed = False
        mock_http.get = MagicMock(return_value=AsyncContextManagerMock(resp))
        mock_http.close = AsyncMock()
        p._session = mock_http

        with pytest.raises(DatabaseError):
            await p.sync_series("FEDFUNDS", date(2026, 3, 1), date(2026, 3, 31))


class TestFredFetchSeries:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {
                "source": "fred",
                "indicator_code": "FEDFUNDS",
                "indicator_name": "Federal Funds Rate",
                "date": "2026-03-01",
                "value": "5.33",
                "unit": "Percent",
            },
        ])
        sf, session = _mock_session_factory()
        p = _make_fred_provider(cache=cache, session_factory=sf)

        result = await p.fetch_series("FEDFUNDS")
        assert len(result) == 1
        assert result[0].source == DataSourceType.FRED
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_indicator_row(
            source="fred",
            indicator_code="FEDFUNDS",
            indicator_name="Federal Funds Rate",
            value=Decimal("5.33"),
            unit="Percent",
        )
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_fred_provider(cache=cache, session_factory=sf)
        result = await p.fetch_series("FEDFUNDS")
        assert len(result) == 1
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_result(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[]))

        p = _make_fred_provider(cache=cache, session_factory=sf)
        result = await p.fetch_series("FEDFUNDS")
        assert result == []
        cache.set_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_error_graceful(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_indicator_row(
            source="fred",
            indicator_code="FEDFUNDS",
            indicator_name="Federal Funds Rate",
            value=Decimal("5.33"),
            unit="Percent",
        )
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_fred_provider(cache=cache, session_factory=sf)
        result = await p.fetch_series("FEDFUNDS")
        assert len(result) == 1
