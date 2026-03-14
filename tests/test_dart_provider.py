"""Unit tests for DartProvider.

All DART API, DB, and Redis calls are mocked.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import AsyncContextManagerMock, make_settings, mock_aiohttp_response

from src.core.enums import ReportType
from src.core.exceptions import CacheError, DatabaseError, ExternalAPIError
from src.data.cache import RedisCache
from src.data.providers.dart_provider import DartProvider, _parse_financial_items

# -- Helpers ---------------------------------------------------------------


def _mock_session_factory(session=None):
    """Create a mock async session factory (async with pattern)."""
    mock_session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, mock_session


def _make_provider(cache=None, session_factory=None, settings=None):
    """DartProvider with all mocked deps."""
    if session_factory is None:
        sf, _ = _mock_session_factory()
    else:
        sf = session_factory
    return DartProvider(
        cache=cache or AsyncMock(spec=RedisCache),
        session_factory=sf,
        settings=settings or make_settings(DART_API_KEY="test_dart_key"),
    )


def _make_db_result(rowcount=0, scalars_all=None, scalars_first=None):
    """Create a mock DB execute result."""
    result = MagicMock()
    result.rowcount = rowcount
    result.scalars = MagicMock()
    result.scalars.return_value.all.return_value = scalars_all or []
    result.scalars.return_value.first.return_value = scalars_first
    return result


def _make_disclosure_row(**kwargs):
    """Disclosure ORM row mock."""
    row = MagicMock()
    row.corp_code = kwargs.get("corp_code", "00126380")
    row.symbol = kwargs.get("symbol", "005930")
    row.report_name = kwargs.get("report_name", "사업보고서")
    row.receipt_no = kwargs.get("receipt_no", "20260301000001")
    row.receipt_date = kwargs.get("receipt_date", date(2026, 3, 1))
    row.filer_name = kwargs.get("filer_name", "삼성전자")
    return row


def _make_financial_row(**kwargs):
    """FinancialStatement ORM row mock."""
    row = MagicMock()
    row.symbol = kwargs.get("symbol", "005930")
    row.corp_code = kwargs.get("corp_code", "00126380")
    row.report_type = kwargs.get("report_type", "annual")
    row.fiscal_year = kwargs.get("fiscal_year", 2025)
    row.fiscal_quarter = kwargs.get("fiscal_quarter")
    row.revenue = kwargs.get("revenue", Decimal("300000000000"))
    row.operating_income = kwargs.get("operating_income", Decimal("50000000000"))
    row.net_income = kwargs.get("net_income", Decimal("40000000000"))
    row.total_assets = kwargs.get("total_assets", Decimal("400000000000"))
    row.total_equity = kwargs.get("total_equity", Decimal("300000000000"))
    row.total_liabilities = kwargs.get("total_liabilities", Decimal("100000000000"))
    row.per = kwargs.get("per", Decimal("15.50"))
    row.pbr = kwargs.get("pbr", Decimal("1.20"))
    row.roe = kwargs.get("roe", Decimal("13.50"))
    row.eps = kwargs.get("eps", Decimal("5000.00"))
    row.bps = kwargs.get("bps", Decimal("40000.00"))
    return row


def _make_corp_code_zip(corp_code: str = "00126380", stock_code: str = "005930") -> bytes:
    """Create a minimal corpCode.xml ZIP like DART API returns."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<result><list>"
        f"<corp_code>{corp_code}</corp_code>"
        f"<stock_code>{stock_code}</stock_code>"
        "</list></result>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def _setup_provider_with_aiohttp(provider, json_data, status=200):
    """Set up provider's aiohttp session mock for API calls."""
    resp = mock_aiohttp_response(status=status, json_data=json_data)
    mock_http_session = MagicMock()
    mock_http_session.closed = False
    mock_http_session.get = MagicMock(return_value=AsyncContextManagerMock(resp))
    mock_http_session.close = AsyncMock()
    provider._session = mock_http_session
    return mock_http_session


# =========================================================================
# 1. Lifecycle
# =========================================================================


class TestDartProviderLifecycle:
    def test_provider_name(self):
        p = _make_provider()
        assert p.provider_name == "dart"

    @pytest.mark.asyncio
    async def test_initialize_creates_session(self):
        p = _make_provider()
        await p.initialize()
        assert p._session is not None
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_closes_session(self):
        p = _make_provider()
        await p.initialize()
        await p.shutdown()
        assert p._session is None

    @pytest.mark.asyncio
    async def test_health_check_true(self):
        p = _make_provider()
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_no_session(self):
        p = _make_provider()
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_false_closed_session(self):
        p = _make_provider()
        mock_session = MagicMock()
        mock_session.closed = True
        p._session = mock_session
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_false_no_api_key(self):
        p = _make_provider(settings=make_settings(DART_API_KEY=""))
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is False


# =========================================================================
# 2. _resolve_corp_code
# =========================================================================


class TestResolveCorpCode:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")
        p = _make_provider(cache=cache)

        result = await p._resolve_corp_code("005930")
        assert result == "00126380"

    @pytest.mark.asyncio
    async def test_cache_miss_api_call(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        p = _make_provider(cache=cache)
        mock_session = _setup_provider_with_aiohttp(p, {})
        # Override resp.read to return valid corpCode ZIP
        resp = mock_session.get.return_value._resp
        resp.read = AsyncMock(return_value=_make_corp_code_zip())

        result = await p._resolve_corp_code("005930")
        assert result == "00126380"
        cache.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_failure_returns_none(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        p = _make_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"status": "800", "message": "fail"})

        result = await p._resolve_corp_code("005930")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_data_returns_none(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        p = _make_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"status": "013", "message": "no data"})

        result = await p._resolve_corp_code("999999")
        assert result is None


# =========================================================================
# 3. sync_disclosures
# =========================================================================


class TestSyncDisclosures:
    @pytest.mark.asyncio
    async def test_normal_upsert(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")  # corp_code cached
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=2))
        session.commit = AsyncMock()

        p = _make_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "status": "000",
            "list": [
                {
                    "rcept_no": "20260301000001",
                    "rcept_dt": "20260301",
                    "report_nm": "사업보고서",
                    "flr_nm": "삼성전자",
                },
                {
                    "rcept_no": "20260302000002",
                    "rcept_dt": "20260302",
                    "report_nm": "분기보고서",
                    "flr_nm": "삼성전자",
                },
            ],
        })

        result = await p.sync_disclosures("005930")
        assert result == 2
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_response_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")
        sf, session = _mock_session_factory()

        p = _make_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {"status": "013", "list": []})

        result = await p.sync_disclosures("005930")
        assert result == 0
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_corp_code_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        p = _make_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"status": "013", "message": "no data"})

        result = await p.sync_disclosures("999999")
        assert result == 0

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")

        p = _make_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"status": "800", "message": "error"})

        with pytest.raises(ExternalAPIError):
            await p.sync_disclosures("005930")

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "status": "000",
            "list": [{"rcept_no": "20260301000001", "rcept_dt": "20260301", "report_nm": "test"}],
        })

        with pytest.raises(DatabaseError):
            await p.sync_disclosures("005930")


# =========================================================================
# 4. sync_financials
# =========================================================================


class TestSyncFinancials:
    @pytest.mark.asyncio
    async def test_normal_upsert(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        p = _make_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "status": "000",
            "list": [
                {"account_nm": "매출액", "thstrm_amount": "300,000,000,000"},
                {"account_nm": "영업이익", "thstrm_amount": "50,000,000,000"},
            ],
        })

        result = await p.sync_financials("005930", 2025, ReportType.ANNUAL)
        assert result == 1
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_response_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")
        sf, session = _mock_session_factory()

        p = _make_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {"status": "013", "list": []})

        result = await p.sync_financials("005930", 2025, ReportType.ANNUAL)
        assert result == 0

    @pytest.mark.asyncio
    async def test_no_corp_code_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        p = _make_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"status": "013"})

        result = await p.sync_financials("999999", 2025, ReportType.ANNUAL)
        assert result == 0

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")

        p = _make_provider(cache=cache)
        _setup_provider_with_aiohttp(p, {"status": "800", "message": "error"})

        with pytest.raises(ExternalAPIError):
            await p.sync_financials("005930", 2025, ReportType.ANNUAL)

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="00126380")
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "status": "000",
            "list": [{"account_nm": "매출액", "thstrm_amount": "100"}],
        })

        with pytest.raises(DatabaseError):
            await p.sync_financials("005930", 2025, ReportType.ANNUAL)


# =========================================================================
# 5. fetch_disclosures
# =========================================================================


class TestFetchDisclosures:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=[
            {
                "corp_code": "00126380",
                "symbol": "005930",
                "report_name": "사업보고서",
                "receipt_no": "20260301000001",
                "receipt_date": "2026-03-01",
            },
        ])
        sf, session = _mock_session_factory()
        p = _make_provider(cache=cache, session_factory=sf)

        result = await p.fetch_disclosures("005930")
        assert len(result) == 1
        assert result[0].receipt_no == "20260301000001"
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_disclosure_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_disclosures("005930")
        assert len(result) == 1
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_result(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_disclosures("005930")
        assert result == []
        cache.set_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(cache=cache, session_factory=sf)
        with pytest.raises(DatabaseError):
            await p.fetch_disclosures("005930")

    @pytest.mark.asyncio
    async def test_cache_read_error_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_disclosure_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_disclosures("005930")
        assert len(result) == 1


# =========================================================================
# 6. fetch_financial_statement
# =========================================================================


class TestFetchFinancialStatement:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value={
            "symbol": "005930",
            "corp_code": "00126380",
            "report_type": "annual",
            "fiscal_year": 2025,
            "revenue": "300000000000",
        })
        sf, session = _mock_session_factory()
        p = _make_provider(cache=cache, session_factory=sf)

        result = await p.fetch_financial_statement("005930", 2025, ReportType.ANNUAL)
        assert result is not None
        assert result.symbol == "005930"
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_financial_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_first=row))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_financial_statement("005930", 2025, ReportType.ANNUAL)
        assert result is not None
        assert result.fiscal_year == 2025
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_first=None))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_financial_statement("005930", 2025, ReportType.ANNUAL)
        assert result is None

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_provider(cache=cache, session_factory=sf)
        with pytest.raises(DatabaseError):
            await p.fetch_financial_statement("005930", 2025, ReportType.ANNUAL)

    @pytest.mark.asyncio
    async def test_cache_read_error_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_financial_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_first=row))

        p = _make_provider(cache=cache, session_factory=sf)
        result = await p.fetch_financial_statement("005930", 2025, ReportType.ANNUAL)
        assert result is not None


# =========================================================================
# 7. _parse_financial_items helper
# =========================================================================


class TestParseFinancialItems:
    def test_parses_known_accounts(self):
        items = [
            {"account_nm": "매출액", "thstrm_amount": "300,000,000,000"},
            {"account_nm": "영업이익", "thstrm_amount": "50,000,000,000"},
            {"account_nm": "당기순이익", "thstrm_amount": "40,000,000,000"},
            {"account_nm": "자산총계", "thstrm_amount": "400,000,000,000"},
            {"account_nm": "자본총계", "thstrm_amount": "300,000,000,000"},
            {"account_nm": "부채총계", "thstrm_amount": "100,000,000,000"},
        ]
        result = _parse_financial_items(items)
        assert result["revenue"] == 300_000_000_000
        assert result["operating_income"] == 50_000_000_000
        assert result["net_income"] == 40_000_000_000
        assert result["total_assets"] == 400_000_000_000
        assert result["total_equity"] == 300_000_000_000
        assert result["total_liabilities"] == 100_000_000_000

    def test_skips_unknown_accounts(self):
        items = [
            {"account_nm": "기타비용", "thstrm_amount": "1,000"},
        ]
        result = _parse_financial_items(items)
        assert result == {}

    def test_handles_empty_amount(self):
        items = [
            {"account_nm": "매출액", "thstrm_amount": ""},
        ]
        result = _parse_financial_items(items)
        assert "revenue" not in result

    def test_handles_invalid_amount(self):
        items = [
            {"account_nm": "매출액", "thstrm_amount": "N/A"},
        ]
        result = _parse_financial_items(items)
        assert result["revenue"] is None
