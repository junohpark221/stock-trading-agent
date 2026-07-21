"""Unit tests for collection orchestration functions (Phase 2 Step 9).

All providers are AsyncMock — no real API calls.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.core.enums import ReportType
from src.data.flow_crosscheck import CrosscheckStats, FlowSyncResult
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


# ── PRJ-03: collect_investor_flow / market / short_interest ─────────


class TestCollectInvestorFlow:
    """collect_investor_flow 오케스트레이션 테스트 (페이싱 0으로 패치)."""

    @pytest.fixture()
    def provider(self):
        p = AsyncMock()
        p.provider_name = "kis"
        p.sync_investor_flow = AsyncMock(return_value=FlowSyncResult(30))
        return p

    @pytest.mark.asyncio()
    async def test_all_success(self, provider):
        from src.data.collector import collect_investor_flow

        with patch("src.data.collector._FLOW_CALL_PACE_SEC", 0):
            summary = await collect_investor_flow(provider, ["A", "B", "C"])

        assert summary.total_symbols == 3
        assert summary.succeeded == 3
        assert summary.failed == 0
        assert summary.total_rows == 90
        assert summary.revision_rows == 0
        assert summary.cross_source_rows == 0

    @pytest.mark.asyncio()
    async def test_middle_failure_isolated(self, provider):
        from src.data.collector import collect_investor_flow

        provider.sync_investor_flow = AsyncMock(
            side_effect=[
                FlowSyncResult(30),
                RuntimeError("kis down"),
                FlowSyncResult(30),
            ]
        )
        with patch("src.data.collector._FLOW_CALL_PACE_SEC", 0):
            summary = await collect_investor_flow(provider, ["A", "B", "C"])

        assert summary.succeeded == 2
        assert summary.failed == 1
        assert summary.failed_symbols == ["B"]
        assert summary.total_rows == 60

    @pytest.mark.asyncio()
    async def test_crosscheck_stats_aggregated(self, provider):
        """단계 5: sync별 리비전/경계 카운트가 summary로 합산된다."""
        from src.data.collector import collect_investor_flow

        s1 = CrosscheckStats(
            compared_rows=29, revision_rows=1, mismatched_cells=2
        )
        s2 = CrosscheckStats(
            compared_rows=29, cross_source_rows=3, mismatched_cells=5
        )
        provider.sync_investor_flow = AsyncMock(
            side_effect=[FlowSyncResult(30, s1), FlowSyncResult(30, s2)]
        )
        with patch("src.data.collector._FLOW_CALL_PACE_SEC", 0):
            summary = await collect_investor_flow(provider, ["A", "B"])

        assert summary.total_rows == 60
        assert summary.revision_rows == 1
        assert summary.cross_source_rows == 3
        assert summary.mismatched_cells == 7


class TestCollectMarketInvestorFlow:
    @pytest.mark.asyncio()
    async def test_default_two_markets(self):
        from src.data.collector import collect_market_investor_flow

        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_market_investor_flow = AsyncMock(
            return_value=FlowSyncResult(300)
        )

        summary = await collect_market_investor_flow(provider)

        assert summary.total_symbols == 2
        assert summary.succeeded == 2
        assert summary.total_rows == 600
        provider.sync_market_investor_flow.assert_any_await("kospi")
        provider.sync_market_investor_flow.assert_any_await("kosdaq")

    @pytest.mark.asyncio()
    async def test_one_market_failure(self):
        from src.data.collector import collect_market_investor_flow

        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_market_investor_flow = AsyncMock(
            side_effect=[FlowSyncResult(300), RuntimeError("fail")]
        )

        summary = await collect_market_investor_flow(provider)

        assert summary.succeeded == 1
        assert summary.failed == 1
        assert summary.failed_symbols == ["kosdaq"]


class TestCollectShortInterest:
    @pytest.mark.asyncio()
    async def test_window_and_both_trs_called(self):
        from src.data.collector import collect_short_interest

        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_short_sale = AsyncMock(return_value=10)
        provider.sync_loan_trans = AsyncMock(return_value=10)

        fixed_today = date(2026, 7, 16)
        with (
            patch("src.data.collector.today_kst", return_value=fixed_today),
            patch("src.data.collector._FLOW_CALL_PACE_SEC", 0),
        ):
            summary = await collect_short_interest(provider, ["A"], window_days=14)

        expected_start = fixed_today - timedelta(days=14)
        provider.sync_short_sale.assert_awaited_once_with(
            "A", start_date=expected_start, end_date=fixed_today
        )
        provider.sync_loan_trans.assert_awaited_once_with(
            "A", start_date=expected_start, end_date=fixed_today
        )
        assert summary.succeeded == 1
        assert summary.total_rows == 20

    @pytest.mark.asyncio()
    async def test_loan_failure_marks_symbol_failed(self):
        from src.data.collector import collect_short_interest

        provider = AsyncMock()
        provider.provider_name = "kis"
        provider.sync_short_sale = AsyncMock(return_value=10)
        provider.sync_loan_trans = AsyncMock(side_effect=RuntimeError("loan fail"))

        with patch("src.data.collector._FLOW_CALL_PACE_SEC", 0):
            summary = await collect_short_interest(provider, ["A", "B"])

        assert summary.succeeded == 0
        assert summary.failed == 2
        assert summary.failed_symbols == ["A", "B"]
