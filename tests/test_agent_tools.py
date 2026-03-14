"""Agent tool functions + ToolRegistry + ToolContext 단위 테스트 (~30개).

A. Technical tools (4개)
B. Fundamental tools (4개)
C. Market data tools (3개)
D. News & sentiment tools (3개)
E. Macro tools (2개)
F. ToolRegistry (4개)
G. ToolContext (2개)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.tools.context import ToolContext
from src.agent.tools.fundamental import get_financial_statements, get_fundamental_score
from src.agent.tools.macro import get_macro_indicators
from src.agent.tools.market_data import get_current_price, get_market_data_summary
from src.agent.tools.news import analyze_news_sentiment, get_disclosures, get_recent_news
from src.agent.tools.registry import ToolRegistry
from src.agent.tools.technical import get_technical_indicators, scan_chart_patterns


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
        assert len(tools) == 10  # 2+2+2+3+1
        names = {t.name for t in tools}
        assert "get_technical_indicators" in names
        assert "get_macro_indicators" in names

    def test_get_tools_filtered(self) -> None:
        registry = self._make_registry()
        tools = registry.get_tools(modules=["macro"])
        assert len(tools) == 1
        assert tools[0].name == "get_macro_indicators"

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
