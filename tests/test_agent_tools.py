"""Agent tool functions + ToolRegistry + ToolContext 단위 테스트.

A. Technical tools (4개)
B. Fundamental tools (4개)
C. Market data tools (3개)
D. News & sentiment tools (3개)
E. Macro tools (2개)
E-2. Investor flow tools (PRJ-03 단계 8)
F. ToolRegistry (4개)
G. ToolContext (2개)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.tools.context import ToolContext
from src.agent.tools.fundamental import get_financial_statements, get_fundamental_score
from src.agent.tools.investor_flow import (
    get_investor_flow_summary,
    get_market_investor_flow_summary,
)
from src.agent.tools.macro import get_macro_indicators
from src.agent.tools.market_data import get_current_price, get_market_data_summary
from src.agent.tools.news import analyze_news_sentiment, get_disclosures, get_recent_news
from src.agent.tools.registry import ToolRegistry
from src.agent.tools.technical import get_technical_indicators, scan_chart_patterns
from src.core.models import InvestorFlowRecord, MarketInvestorFlowRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_session_factory(
    rows: list | None = None,
    *,
    scalars_first: object | None = None,
    use_first: bool = False,
) -> MagicMock:
    """Create a mock async_sessionmaker.

    If ``use_first`` is True, ``scalars().first()`` returns *scalars_first*.
    Otherwise ``scalars().all()`` returns *rows*.
    """
    session = AsyncMock()
    result_mock = MagicMock()
    scalars_mock = MagicMock()

    if use_first:
        scalars_mock.first.return_value = scalars_first
    else:
        scalars_mock.all.return_value = rows or []

    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx)
    return factory


def _make_ohlcv_row(
    symbol: str = "005930",
    d: date | None = None,
    open_: float = 70000,
    high: float = 71000,
    low: float = 69000,
    close: float = 70500,
    volume: int = 1000000,
    change_rate: float = 0.5,
) -> MagicMock:
    """Create a mock DailyOHLCV row."""
    row = MagicMock()
    row.symbol = symbol
    row.date = d or date(2026, 3, 14)
    row.open = Decimal(str(open_))
    row.high = Decimal(str(high))
    row.low = Decimal(str(low))
    row.close = Decimal(str(close))
    row.volume = volume
    row.change_rate = Decimal(str(change_rate))
    row.trading_value = None
    return row


def _make_ctx(factory: MagicMock | None = None) -> ToolContext:
    """Create a ToolContext with mock session_factory and settings."""
    if factory is None:
        factory = _make_mock_session_factory()
    settings = MagicMock()
    return ToolContext(session_factory=factory, settings=settings)


# -- Investor flow helpers (PRJ-03 단계 8) ----------------------------------


def _make_multi_result_session_factory(results: list[MagicMock]) -> MagicMock:
    """세션 1개에서 execute를 여러 번 하는 도구용 — side_effect 순차 반환."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=results)
    session.commit = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _scalar_result(value: object | None) -> MagicMock:
    """scalar_one_or_none() 결과 mock (security_group 단건 조회용)."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = value
    return m


def _scalars_all_result(rows: list) -> MagicMock:
    """scalars().all() 결과 mock (플로우 행 조회용)."""
    m = MagicMock()
    m.scalars.return_value.all.return_value = rows
    return m


def _tuples_result(tuples: list[tuple]) -> MagicMock:
    """all() 결과 mock ((date, trading_value) 튜플 조회용)."""
    m = MagicMock()
    m.all.return_value = tuples
    return m


def _flow_days(n: int, start: date = date(2026, 6, 1)) -> list[date]:
    """연속 평일 n개 (test_investor_flow_indicators.py 컨벤션)."""
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _make_flow_records(n: int = 10, symbol: str = "005930") -> list[InvestorFlowRecord]:
    """오름차순 종목 수급 레코드 — model_validate(from_attributes)가 MagicMock의
    auto-attribute에 오염되므로 실인스턴스를 ORM row 대용으로 사용(단계 7 확립)."""
    return [
        InvestorFlowRecord(
            symbol=symbol,
            date=d,
            frgn_net_amt=Decimal(1_000_000) * (i + 1),
            frgn_net_qty=100 * (i + 1),
            orgn_net_amt=Decimal(-500_000),
        )
        for i, d in enumerate(_flow_days(n))
    ]


def _make_market_flow_records(
    n: int = 10, market: str = "kospi"
) -> list[MarketInvestorFlowRecord]:
    """오름차순 시장 수급 레코드 (지수 종가 포함)."""
    return [
        MarketInvestorFlowRecord(
            market=market,
            date=d,
            index_close=Decimal("3200.5") + i,
            frgn_net_amt=Decimal(10_000_000) * (i + 1),
        )
        for i, d in enumerate(_flow_days(n))
    ]


# ===========================================================================
# A. Technical tools
# ===========================================================================


class TestTechnicalTools:
    """get_technical_indicators, scan_chart_patterns 테스트."""

    @pytest.mark.asyncio
    async def test_get_technical_indicators_success(self) -> None:
        """충분한 OHLCV 데이터 → 정상 결과."""
        rows = [
            _make_ohlcv_row(
                d=date(2026, 1, 1 + i) if (1 + i) <= 28 else date(2026, 2, i - 27),
                close=70000 + i * 100,
                high=71000 + i * 100,
                low=69000 + i * 100,
                volume=1000000 + i * 10000,
            )
            for i in range(50)
        ]
        factory = _make_mock_session_factory(rows)
        ctx = _make_ctx(factory)

        result = await get_technical_indicators(ctx, "005930", days=50)
        assert result["symbol"] == "005930"
        assert "latest" in result
        assert "patterns" in result
        assert "trend_summary" in result
        assert result["data_points"] == 50

    @pytest.mark.asyncio
    async def test_get_technical_indicators_no_data(self) -> None:
        factory = _make_mock_session_factory([])
        ctx = _make_ctx(factory)

        result = await get_technical_indicators(ctx, "999999")
        assert "error" in result
        assert result["symbol"] == "999999"

    @pytest.mark.asyncio
    async def test_get_technical_indicators_db_error(self) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=cm)
        ctx = _make_ctx(factory)

        result = await get_technical_indicators(ctx, "005930")
        assert "error" in result
        assert result["tool"] == "get_technical_indicators"

    @pytest.mark.asyncio
    async def test_scan_chart_patterns_success(self) -> None:
        rows = [
            _make_ohlcv_row(
                d=date(2026, 1, 1 + i) if (1 + i) <= 28 else date(2026, 2, i - 27),
                close=70000 + i * 100,
                high=71000 + i * 100,
                low=69000 + i * 100,
            )
            for i in range(50)
        ]
        factory = _make_mock_session_factory(rows)
        ctx = _make_ctx(factory)

        result = await scan_chart_patterns(ctx, "005930", days=50)
        assert result["symbol"] == "005930"
        assert "patterns" in result
        assert "support_levels" in result
        assert "resistance_levels" in result
        assert isinstance(result["pattern_count"], int)


# ===========================================================================
# B. Fundamental tools
# ===========================================================================


class TestFundamentalTools:
    """get_fundamental_score, get_financial_statements 테스트."""

    @pytest.mark.asyncio
    async def test_get_fundamental_score_success(self) -> None:
        from src.core.models import FundamentalScore

        score = FundamentalScore(
            symbol="005930",
            overall_score=Decimal("72.5"),
            valuation_score=Decimal("80"),
            growth_score=Decimal("65"),
            profitability_score=Decimal("70"),
            reasoning="test reasoning",
        )
        ctx = _make_ctx()
        ctx._fundamental_analyzer = MagicMock()
        ctx._fundamental_analyzer.analyze = AsyncMock(return_value=score)

        result = await get_fundamental_score(ctx, "005930")
        assert result["symbol"] == "005930"
        assert float(result["overall_score"]) == 72.5
        assert result["reasoning"] == "test reasoning"

    @pytest.mark.asyncio
    async def test_get_fundamental_score_error(self) -> None:
        ctx = _make_ctx()
        ctx._fundamental_analyzer = MagicMock()
        ctx._fundamental_analyzer.analyze = AsyncMock(
            side_effect=RuntimeError("db error")
        )

        result = await get_fundamental_score(ctx, "005930")
        assert "error" in result
        assert result["tool"] == "get_fundamental_score"

    @pytest.mark.asyncio
    async def test_get_financial_statements_success(self) -> None:
        row = MagicMock()
        row.fiscal_year = 2025
        row.revenue = Decimal("300000000000")
        row.operating_income = Decimal("50000000000")
        row.net_income = Decimal("40000000000")
        row.total_assets = Decimal("500000000000")
        row.total_equity = Decimal("350000000000")
        row.total_liabilities = Decimal("150000000000")
        row.per = Decimal("12.5")
        row.pbr = Decimal("1.2")
        row.roe = Decimal("11.4")
        row.eps = Decimal("5000")
        row.bps = Decimal("43000")

        factory = _make_mock_session_factory([row])
        ctx = _make_ctx(factory)

        result = await get_financial_statements(ctx, "005930")
        assert result["symbol"] == "005930"
        assert result["count"] == 1
        assert result["statements"][0]["fiscal_year"] == 2025
        assert result["statements"][0]["per"] == 12.5

    @pytest.mark.asyncio
    async def test_get_financial_statements_empty(self) -> None:
        factory = _make_mock_session_factory([])
        ctx = _make_ctx(factory)

        result = await get_financial_statements(ctx, "999999")
        assert result["count"] == 0
        assert result["statements"] == []


# ===========================================================================
# C. Market data tools
# ===========================================================================


class TestMarketDataTools:
    """get_current_price, get_market_data_summary 테스트."""

    @pytest.mark.asyncio
    async def test_get_current_price_success(self) -> None:
        row = _make_ohlcv_row(close=70500, change_rate=0.71)
        factory = _make_mock_session_factory(scalars_first=row, use_first=True)
        ctx = _make_ctx(factory)

        result = await get_current_price(ctx, "005930")
        assert result["symbol"] == "005930"
        assert result["close"] == 70500.0
        assert result["change_rate"] == 0.71

    @pytest.mark.asyncio
    async def test_get_current_price_no_data(self) -> None:
        factory = _make_mock_session_factory(scalars_first=None, use_first=True)
        ctx = _make_ctx(factory)

        result = await get_current_price(ctx, "999999")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_market_data_summary_stats(self) -> None:
        rows = [
            _make_ohlcv_row(
                d=date(2026, 3, 14 - i) if (14 - i) > 0 else date(2026, 2, 28 + (14 - i)),
                close=70000 + i * 100,
                high=71000 + i * 100,
                low=69000 + i * 100,
                volume=1000000 + i * 5000,
            )
            for i in range(10)
        ]
        factory = _make_mock_session_factory(rows)
        ctx = _make_ctx(factory)

        result = await get_market_data_summary(ctx, "005930", days=10)
        assert result["symbol"] == "005930"
        assert result["period_days"] == 10
        assert result["volatility"] is not None
        assert result["current_close"] is not None
        assert result["volume_trend"] in ("increasing", "decreasing", "stable")


# ===========================================================================
# D. News & sentiment tools
# ===========================================================================


class TestNewsTools:
    """get_recent_news, analyze_news_sentiment, get_disclosures 테스트."""

    @pytest.mark.asyncio
    async def test_get_recent_news_success(self) -> None:
        article = MagicMock()
        article.title = "삼성전자 호실적"
        article.link = "https://example.com/1"
        article.published_at = datetime(2026, 3, 14, tzinfo=timezone.utc)
        article.sentiment_label = "positive"
        article.sentiment_score = Decimal("0.750")

        factory = _make_mock_session_factory([article])
        ctx = _make_ctx(factory)

        result = await get_recent_news(ctx, "005930")
        assert result["count"] == 1
        assert result["articles"][0]["title"] == "삼성전자 호실적"
        assert result["articles"][0]["sentiment_score"] == 0.75

    @pytest.mark.asyncio
    async def test_analyze_sentiment_with_llm_flag(self) -> None:
        from src.core.models import SentimentResult

        sentiment = SentimentResult(
            symbol="005930",
            overall_score=Decimal("0.350"),
            overall_label="neutral",
            method="keyword",
            positive_count=3,
            negative_count=2,
            neutral_count=5,
            total_articles=10,
            key_topics=["실적", "반도체"],
            needs_llm_analysis=True,
            reasoning="애매한 구간",
        )
        ctx = _make_ctx()
        ctx._sentiment_analyzer = MagicMock()
        ctx._sentiment_analyzer.analyze = AsyncMock(return_value=sentiment)

        result = await analyze_news_sentiment(ctx, "005930")
        assert result["symbol"] == "005930"
        assert result["needs_llm_analysis"] is True
        assert float(result["overall_score"]) == pytest.approx(0.35)

    @pytest.mark.asyncio
    async def test_get_disclosures_success(self) -> None:
        disclosure = MagicMock()
        disclosure.report_name = "사업보고서"
        disclosure.receipt_date = date(2026, 3, 10)
        disclosure.filer_name = "삼성전자"

        factory = _make_mock_session_factory([disclosure])
        ctx = _make_ctx(factory)

        result = await get_disclosures(ctx, "005930")
        assert result["count"] == 1
        assert result["disclosures"][0]["report_name"] == "사업보고서"


# ===========================================================================
# E. Macro tools
# ===========================================================================


class TestMacroTools:
    """get_macro_indicators 테스트."""

    @pytest.mark.asyncio
    async def test_get_macro_indicators_all(self) -> None:
        """All indicators found → korean and us dicts populated."""
        call_count = 0

        async def _mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            row = MagicMock()
            row.value = Decimal("3.50")
            row.date = date(2026, 3, 14)

            result_mock = MagicMock()
            scalars_mock = MagicMock()
            scalars_mock.first.return_value = row
            result_mock.scalars.return_value = scalars_mock
            return result_mock

        session = AsyncMock()
        session.execute = _mock_execute
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=cm)
        ctx = _make_ctx(factory)

        result = await get_macro_indicators(ctx)
        assert "korean" in result
        assert "us" in result
        assert "fetched_at" in result
        # 5 ECOS + 5 FRED = 10 queries
        assert call_count == 10

    @pytest.mark.asyncio
    async def test_get_macro_indicators_partial_data(self) -> None:
        """Some indicators missing → partial results, no error."""
        async def _mock_execute(stmt):
            result_mock = MagicMock()
            scalars_mock = MagicMock()
            scalars_mock.first.return_value = None
            result_mock.scalars.return_value = scalars_mock
            return result_mock

        session = AsyncMock()
        session.execute = _mock_execute
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=cm)
        ctx = _make_ctx(factory)

        result = await get_macro_indicators(ctx)
        assert result["korean"] == {}
        assert result["us"] == {}
        assert "error" not in result


# ===========================================================================
# E-2. Investor flow tools (PRJ-03 단계 8)
# ===========================================================================


class TestInvestorFlowTools:
    """get_investor_flow_summary, get_market_investor_flow_summary 테스트."""

    @staticmethod
    def _stock_ctx(
        security_group: str | None,
        flow_rows: list | None = None,
        trading_values: list[tuple] | None = None,
    ) -> ToolContext:
        """종목 도구 3쿼리(그룹→플로우→거래대금) 순서의 ctx 구성."""
        results: list[MagicMock] = [_scalar_result(security_group)]
        if flow_rows is not None:
            results.append(_scalars_all_result(flow_rows))
        if trading_values is not None:
            results.append(_tuples_result(trading_values))
        return _make_ctx(_make_multi_result_session_factory(results))

    @pytest.mark.asyncio
    async def test_summary_success(self) -> None:
        recs = _make_flow_records(10)
        tv = [(r.date, Decimal(50_000_000)) for r in recs]
        # DB는 최신순 반환 — 도구가 reverse로 오름차순 복원
        ctx = self._stock_ctx("ST", list(reversed(recs)), tv)

        result = await get_investor_flow_summary(ctx, "005930")
        assert "error" not in result
        assert result["symbol"] == "005930"
        assert result["days_available"] == 10
        axes = {a["axis"]: a for a in result["axes"]}
        assert set(axes) == {"frgn", "orgn", "prsn", "scrt"}
        # scrt caveat 분리 표기 + frgn 지속 순매수 스트릭 + intensity 산출
        assert axes["scrt"]["caveat"]
        assert axes["frgn"]["streak"] == 10
        frgn_w5 = next(w for w in axes["frgn"]["windows"] if w["window"] == 5)
        assert frgn_w5["intensity"] is not None

    @pytest.mark.asyncio
    async def test_summary_rt_allowed(self) -> None:
        """리츠(RT)는 소비 허용 (07-24 확정: ST+RT)."""
        recs = _make_flow_records(5, symbol="395400")
        ctx = self._stock_ctx("RT", list(reversed(recs)), [])

        result = await get_investor_flow_summary(ctx, "395400")
        assert "excluded" not in result
        assert result["days_available"] == 5

    @pytest.mark.asyncio
    async def test_summary_excluded_etf(self) -> None:
        ctx = self._stock_ctx("EF")
        result = await get_investor_flow_summary(ctx, "069500")
        assert result["excluded"] is True
        assert result["security_group"] == "EF"
        assert result["tool"] == "get_investor_flow_summary"

    @pytest.mark.asyncio
    async def test_summary_excluded_null_group(self) -> None:
        """NULL(동기화 전/마스터 부재)은 보수적 제외."""
        ctx = self._stock_ctx(None)
        result = await get_investor_flow_summary(ctx, "999999")
        assert result["excluded"] is True
        assert result["security_group"] is None
        assert "미상" in result["reason"]

    @pytest.mark.asyncio
    async def test_summary_no_data(self) -> None:
        ctx = self._stock_ctx("ST", [])
        result = await get_investor_flow_summary(ctx, "005930")
        assert "error" in result
        assert result["symbol"] == "005930"

    @pytest.mark.asyncio
    async def test_summary_db_error(self) -> None:
        session_factory = _make_multi_result_session_factory([])
        session_factory.return_value.__aenter__.side_effect = RuntimeError("db down")
        ctx = _make_ctx(session_factory)

        result = await get_investor_flow_summary(ctx, "005930")
        assert "error" in result
        assert result["tool"] == "get_investor_flow_summary"

    @pytest.mark.asyncio
    async def test_summary_days_clamped(self) -> None:
        """days 과대 입력은 120으로 클램프 — limit 절 인자로 검증."""
        recs = _make_flow_records(5)
        ctx = self._stock_ctx("ST", list(reversed(recs)), [])

        await get_investor_flow_summary(ctx, "005930", days=5000)
        session = ctx.session_factory.return_value.__aenter__.return_value
        flow_stmt = session.execute.await_args_list[1].args[0]
        assert flow_stmt._limit_clause.value == 120

    @pytest.mark.asyncio
    async def test_market_summary_both_markets(self) -> None:
        kospi = _make_market_flow_records(10, "kospi")
        kosdaq = _make_market_flow_records(10, "kosdaq")
        factory = _make_multi_result_session_factory(
            [
                _scalars_all_result(list(reversed(kospi))),
                _scalars_all_result(list(reversed(kosdaq))),
            ]
        )
        ctx = _make_ctx(factory)

        result = await get_market_investor_flow_summary(ctx)
        assert set(result["markets"]) == {"kospi", "kosdaq"}
        summary = result["markets"]["kospi"]
        assert summary["market"] == "kospi"
        assert summary["days_available"] == 10
        assert summary["index_close"] is not None
        assert "index_window_returns" in summary

    @pytest.mark.asyncio
    async def test_market_summary_single_market(self) -> None:
        kospi = _make_market_flow_records(5, "kospi")
        factory = _make_multi_result_session_factory(
            [_scalars_all_result(list(reversed(kospi)))]
        )
        ctx = _make_ctx(factory)

        result = await get_market_investor_flow_summary(ctx, market="kospi")
        assert list(result["markets"]) == ["kospi"]

    @pytest.mark.asyncio
    async def test_market_summary_invalid_market(self) -> None:
        ctx = _make_ctx(_make_multi_result_session_factory([]))
        result = await get_market_investor_flow_summary(ctx, market="nasdaq")
        assert "error" in result
        assert "Invalid market" in result["error"]

    @pytest.mark.asyncio
    async def test_market_summary_partial_success(self) -> None:
        """한 시장 결손은 해당 키에 error, 나머지는 정상 제공."""
        kospi = _make_market_flow_records(5, "kospi")
        factory = _make_multi_result_session_factory(
            [_scalars_all_result(list(reversed(kospi))), _scalars_all_result([])]
        )
        ctx = _make_ctx(factory)

        result = await get_market_investor_flow_summary(ctx)
        assert result["markets"]["kospi"]["days_available"] == 5
        assert "error" in result["markets"]["kosdaq"]

    @pytest.mark.asyncio
    async def test_market_summary_all_empty(self) -> None:
        factory = _make_multi_result_session_factory(
            [_scalars_all_result([]), _scalars_all_result([])]
        )
        ctx = _make_ctx(factory)

        result = await get_market_investor_flow_summary(ctx)
        assert "error" in result
        assert "markets" not in result

    @pytest.mark.asyncio
    async def test_market_summary_db_error(self) -> None:
        factory = _make_multi_result_session_factory([])
        factory.return_value.__aenter__.side_effect = RuntimeError("db down")
        ctx = _make_ctx(factory)

        result = await get_market_investor_flow_summary(ctx)
        assert "error" in result
        assert result["tool"] == "get_market_investor_flow_summary"


# ===========================================================================
# F. ToolRegistry
# ===========================================================================


class TestToolRegistry:
    """ToolRegistry 테스트."""

    def _make_registry(self) -> ToolRegistry:
        ctx = _make_ctx()
        return ToolRegistry(ctx)

    def test_get_tools_all(self) -> None:
        registry = self._make_registry()
        tools = registry.get_tools()
        assert len(tools) == 12  # 2+2+2+3+1+2
        names = {t.name for t in tools}
        assert "get_technical_indicators" in names
        assert "get_macro_indicators" in names
        assert "get_investor_flow_summary" in names

    def test_get_tools_filtered(self) -> None:
        registry = self._make_registry()
        tools = registry.get_tools(modules=["macro"])
        assert len(tools) == 1
        assert tools[0].name == "get_macro_indicators"

    def test_get_tools_investor_flow_module(self) -> None:
        registry = self._make_registry()
        tools = registry.get_tools(modules=["investor_flow"])
        assert {t.name for t in tools} == {
            "get_investor_flow_summary",
            "get_market_investor_flow_summary",
        }

    def test_get_tools_multiple_modules(self) -> None:
        registry = self._make_registry()
        tools = registry.get_tools(modules=["technical", "market_data"])
        assert len(tools) == 4  # 2 technical + 2 market_data

    @pytest.mark.asyncio
    async def test_execute_unknown(self) -> None:
        registry = self._make_registry()
        result = await registry.execute("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_known(self) -> None:
        """Execute a known tool — should call the function (will error since mock DB)."""
        ctx = _make_ctx()
        # Set up the factory to return an empty result for market_data
        factory = _make_mock_session_factory(scalars_first=None, use_first=True)
        ctx = _make_ctx(factory)
        registry = ToolRegistry(ctx)

        result = await registry.execute("get_current_price", {"symbol": "005930"})
        # Will return error dict since no data, but should not crash
        assert result["symbol"] == "005930"


# ===========================================================================
# G. ToolContext
# ===========================================================================


class TestToolContext:
    """ToolContext lazy property 테스트."""

    def test_lazy_analyzer_creation(self) -> None:
        ctx = _make_ctx()
        # First access creates analyzer
        analyzer1 = ctx.fundamental_analyzer
        assert analyzer1 is not None
        # Second access returns same instance
        analyzer2 = ctx.fundamental_analyzer
        assert analyzer1 is analyzer2

    def test_lazy_sentiment_analyzer_creation(self) -> None:
        ctx = _make_ctx()
        analyzer1 = ctx.sentiment_analyzer
        assert analyzer1 is not None
        analyzer2 = ctx.sentiment_analyzer
        assert analyzer1 is analyzer2

    def test_none_provider_handling(self) -> None:
        ctx = _make_ctx()
        assert ctx.dart_provider is None
        assert ctx.naver_provider is None
        assert ctx.ecos_provider is None
        assert ctx.fred_provider is None
        assert ctx.kis_provider is None
