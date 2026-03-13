"""Unit tests for collection orchestration functions (Phase 2 Step 9).

All providers are AsyncMock — no real API calls.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.core.enums import ReportType
from src.data.collector import (
    CollectionSummary,
    OHLCVCollectionSummary,
    collect_financials,
    collect_macro_indicators,
    collect_news,
)


# ── CollectionSummary alias ──────────────────────────────────────────


class TestCollectionSummaryAlias:
    """OHLCVCollectionSummary는 CollectionSummary의 별칭이어야 한다."""

    def test_alias_identity(self):
        assert OHLCVCollectionSummary is CollectionSummary

    def test_alias_instantiation(self):
        s = OHLCVCollectionSummary(total_symbols=3, succeeded=2, failed=1)
        assert isinstance(s, CollectionSummary)
        assert s.total_symbols == 3


# ── collect_financials ───────────────────────────────────────────────


class TestCollectFinancials:
    """collect_financials 오케스트레이션 테스트."""

    @pytest.fixture()
    def dart_provider(self):
        provider = AsyncMock()
        provider.sync_financials = AsyncMock(return_value=5)
        return provider

    @pytest.mark.asyncio()
    async def test_all_success(self, dart_provider):
        result = await collect_financials(dart_provider, ["005930", "000660"], 2025)
        assert result.total_symbols == 2
        assert result.succeeded == 2
        assert result.failed == 0
        # 2 symbols × 3 report types × 5 rows = 30
        assert result.total_rows == 30
        assert result.failed_symbols == []

    @pytest.mark.asyncio()
    async def test_partial_failure(self, dart_provider):
        call_count = 0

        async def _side_effect(symbol, fiscal_year, report_type):
            nonlocal call_count
            call_count += 1
            if symbol == "000660" and report_type == ReportType.ANNUAL:
                raise RuntimeError("DART error")
            return 5

        dart_provider.sync_financials.side_effect = _side_effect
        result = await collect_financials(dart_provider, ["005930", "000660"], 2025)
        assert result.succeeded == 1
        assert result.failed == 1
        assert result.failed_symbols == ["000660"]

    @pytest.mark.asyncio()
    async def test_all_failure(self, dart_provider):
        dart_provider.sync_financials.side_effect = RuntimeError("fail")
        result = await collect_financials(dart_provider, ["005930", "000660"], 2025)
        assert result.succeeded == 0
        assert result.failed == 2
        assert result.total_rows == 0

    @pytest.mark.asyncio()
    async def test_empty_symbols(self, dart_provider):
        result = await collect_financials(dart_provider, [], 2025)
        assert result.total_symbols == 0
        assert result.succeeded == 0
        dart_provider.sync_financials.assert_not_called()

    @pytest.mark.asyncio()
    async def test_three_report_types_called(self, dart_provider):
        await collect_financials(dart_provider, ["005930"], 2025)
        calls = dart_provider.sync_financials.call_args_list
        assert len(calls) == 3
        report_types = {c.args[2] for c in calls}
        assert report_types == {ReportType.ANNUAL, ReportType.SEMI_ANNUAL, ReportType.QUARTERLY}


# ── collect_macro_indicators ─────────────────────────────────────────


class TestCollectMacroIndicators:
    """collect_macro_indicators 오케스트레이션 테스트."""

    @pytest.fixture()
    def ecos_provider(self):
        provider = AsyncMock()
        provider.sync_all = AsyncMock(return_value=10)
        return provider

    @pytest.fixture()
    def fred_provider(self):
        provider = AsyncMock()
        provider.sync_all = AsyncMock(return_value=20)
        return provider

    @pytest.mark.asyncio()
    async def test_default_dates(self, ecos_provider, fred_provider):
        result = await collect_macro_indicators(ecos_provider, fred_provider)
        assert result == {"ecos": 10, "fred": 20}
        # 기본 날짜 확인
        ecos_call = ecos_provider.sync_all.call_args
        assert ecos_call.kwargs["end_date"] == date.today()
        assert ecos_call.kwargs["start_date"] == date.today() - timedelta(days=365)

    @pytest.mark.asyncio()
    async def test_custom_dates(self, ecos_provider, fred_provider):
        start = date(2024, 1, 1)
        end = date(2024, 12, 31)
        result = await collect_macro_indicators(
            ecos_provider, fred_provider, start_date=start, end_date=end
        )
        assert result == {"ecos": 10, "fred": 20}
        ecos_call = ecos_provider.sync_all.call_args
        assert ecos_call.kwargs["start_date"] == start
        assert ecos_call.kwargs["end_date"] == end

    @pytest.mark.asyncio()
    async def test_ecos_failure(self, ecos_provider, fred_provider):
        ecos_provider.sync_all.side_effect = RuntimeError("ECOS down")
        result = await collect_macro_indicators(ecos_provider, fred_provider)
        assert result["ecos"] == 0
        assert result["fred"] == 20

    @pytest.mark.asyncio()
    async def test_fred_failure(self, ecos_provider, fred_provider):
        fred_provider.sync_all.side_effect = RuntimeError("FRED down")
        result = await collect_macro_indicators(ecos_provider, fred_provider)
        assert result["ecos"] == 10
        assert result["fred"] == 0

    @pytest.mark.asyncio()
    async def test_both_failure(self, ecos_provider, fred_provider):
        ecos_provider.sync_all.side_effect = RuntimeError("ECOS down")
        fred_provider.sync_all.side_effect = RuntimeError("FRED down")
        result = await collect_macro_indicators(ecos_provider, fred_provider)
        assert result == {"ecos": 0, "fred": 0}


# ── collect_news ─────────────────────────────────────────────────────


class TestCollectNews:
    """collect_news 오케스트레이션 테스트."""

    @pytest.fixture()
    def naver_provider(self):
        provider = AsyncMock()
        provider.sync_news = AsyncMock(return_value=10)
        return provider

    @pytest.mark.asyncio()
    async def test_all_success(self, naver_provider):
        result = await collect_news(naver_provider, ["005930", "000660"])
        assert result.total_symbols == 2
        assert result.succeeded == 2
        assert result.failed == 0
        assert result.total_rows == 20

    @pytest.mark.asyncio()
    async def test_partial_failure(self, naver_provider):
        async def _side_effect(symbol, query):
            if symbol == "000660":
                raise RuntimeError("Naver error")
            return 10

        naver_provider.sync_news.side_effect = _side_effect
        result = await collect_news(naver_provider, ["005930", "000660"])
        assert result.succeeded == 1
        assert result.failed == 1
        assert result.failed_symbols == ["000660"]

    @pytest.mark.asyncio()
    async def test_query_map(self, naver_provider):
        qmap = {"005930": "삼성전자", "000660": "SK하이닉스"}
        await collect_news(naver_provider, ["005930", "000660"], query_map=qmap)
        calls = naver_provider.sync_news.call_args_list
        assert calls[0].kwargs["query"] == "삼성전자"
        assert calls[1].kwargs["query"] == "SK하이닉스"

    @pytest.mark.asyncio()
    async def test_default_query_is_symbol(self, naver_provider):
        await collect_news(naver_provider, ["005930"])
        call = naver_provider.sync_news.call_args
        assert call.args[0] == "005930"
        assert call.kwargs["query"] == "005930"
