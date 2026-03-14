"""Unit tests for Analysis API endpoints (/api/analysis/* and /api/data/financials|news|disclosures).

Uses httpx AsyncClient with FastAPI test transport.
DB session is dependency-overridden with mocks.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


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


def _mock_financial_row(**kwargs):
    """FinancialStatement ORM row mock."""
    row = MagicMock()
    row.symbol = kwargs.get("symbol", "005930")
    row.corp_code = kwargs.get("corp_code", "00126380")
    row.report_type = kwargs.get("report_type", "annual")
    row.fiscal_year = kwargs.get("fiscal_year", 2025)
    row.fiscal_quarter = kwargs.get("fiscal_quarter", None)
    row.revenue = kwargs.get("revenue", Decimal("300000000000"))
    row.operating_income = kwargs.get("operating_income", Decimal("50000000000"))
    row.net_income = kwargs.get("net_income", Decimal("40000000000"))
    row.total_assets = kwargs.get("total_assets", Decimal("500000000000"))
    row.total_equity = kwargs.get("total_equity", Decimal("350000000000"))
    row.total_liabilities = kwargs.get("total_liabilities", Decimal("150000000000"))
    row.per = kwargs.get("per", Decimal("12.50"))
    row.pbr = kwargs.get("pbr", Decimal("1.20"))
    row.roe = kwargs.get("roe", Decimal("15.00"))
    row.eps = kwargs.get("eps", Decimal("6000"))
    row.bps = kwargs.get("bps", Decimal("50000"))
    return row


def _mock_news_row(**kwargs):
    """NewsArticle ORM row mock."""
    row = MagicMock()
    row.source = kwargs.get("source", "naver")
    row.symbol = kwargs.get("symbol", "005930")
    row.title = kwargs.get("title", "삼성전자 실적 발표")
    row.description = kwargs.get("description", "삼성전자가 실적을 발표했습니다.")
    row.link = kwargs.get("link", "https://example.com/news/1")
    row.published_at = kwargs.get(
        "published_at", datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    )
    row.sentiment_score = kwargs.get("sentiment_score", None)
    row.sentiment_label = kwargs.get("sentiment_label", None)
    return row


def _mock_disclosure_row(**kwargs):
    """Disclosure ORM row mock."""
    row = MagicMock()
    row.corp_code = kwargs.get("corp_code", "00126380")
    row.symbol = kwargs.get("symbol", "005930")
    row.report_name = kwargs.get("report_name", "사업보고서 (2025.12)")
    row.receipt_no = kwargs.get("receipt_no", "20260301000001")
    row.receipt_date = kwargs.get("receipt_date", date(2026, 3, 1))
    row.filer_name = kwargs.get("filer_name", "삼성전자")
    return row


def _mock_macro_row(**kwargs):
    """EconomicIndicator ORM row mock."""
    row = MagicMock()
    row.source = kwargs.get("source", "ecos")
    row.indicator_code = kwargs.get("indicator_code", "722Y001")
    row.indicator_name = kwargs.get("indicator_name", "기준금리")
    row.date = kwargs.get("date", date(2026, 3, 1))
    row.value = kwargs.get("value", Decimal("3.50"))
    row.unit = kwargs.get("unit", "%")
    return row


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
# Technical Analysis: GET /api/analysis/technical/{symbol}
# ═══════════════════════════════════════════════════════════════════════


class TestTechnicalAnalysis:
    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.scan_patterns")
    @patch("src.api.routes.analysis.compute_all_indicators")
    async def test_success(self, mock_compute, mock_scan):
        from src.core.models import TechnicalIndicators

        mock_compute.return_value = TechnicalIndicators(symbol="005930")
        mock_scan.return_value = []

        session = AsyncMock()
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [
            _mock_ohlcv_row(date=date(2026, 3, i)) for i in range(1, 21)
        ]
        session.execute = AsyncMock(return_value=rows_result)
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/technical/005930")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert "indicators" in data
        assert "patterns" in data
        assert data["data_points"] == 20

    @pytest.mark.asyncio
    async def test_404_no_data(self):
        session = AsyncMock()
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=rows_result)
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/technical/999999")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.scan_patterns")
    @patch("src.api.routes.analysis.compute_all_indicators")
    async def test_custom_days(self, mock_compute, mock_scan):
        from src.core.models import TechnicalIndicators

        mock_compute.return_value = TechnicalIndicators(symbol="005930")
        mock_scan.return_value = []

        session = AsyncMock()
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [
            _mock_ohlcv_row() for _ in range(50)
        ]
        session.execute = AsyncMock(return_value=rows_result)
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/technical/005930?days=50")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_days_422(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp_low = await ac.get("/api/analysis/technical/005930?days=5")
            resp_high = await ac.get("/api/analysis/technical/005930?days=2000")

        assert resp_low.status_code == 422
        assert resp_high.status_code == 422

    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.scan_patterns")
    @patch("src.api.routes.analysis.compute_all_indicators")
    async def test_response_schema(self, mock_compute, mock_scan):
        from src.core.models import PatternSignal, TechnicalIndicators

        mock_compute.return_value = TechnicalIndicators(symbol="005930", latest_rsi=45.0)
        mock_scan.return_value = [
            PatternSignal(
                pattern_name="golden_cross",
                signal_type="bullish",
                confidence=Decimal("0.75"),
                description="test",
            )
        ]

        session = AsyncMock()
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [
            _mock_ohlcv_row() for _ in range(20)
        ]
        session.execute = AsyncMock(return_value=rows_result)
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/technical/005930")

        data = resp.json()
        assert data["indicators"]["latest_rsi"] == 45.0
        assert len(data["patterns"]) == 1
        assert data["patterns"][0]["pattern_name"] == "golden_cross"

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/technical/005930")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# Fundamental Analysis: GET /api/analysis/fundamental/{symbol}
# ═══════════════════════════════════════════════════════════════════════


class TestFundamentalAnalysis:
    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.get_session_factory")
    @patch("src.api.routes.analysis.FundamentalAnalyzer")
    async def test_success(self, mock_cls, mock_factory):
        from src.core.models import FundamentalScore

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze.return_value = FundamentalScore(
            symbol="005930",
            overall_score=Decimal("72.5"),
            valuation_score=Decimal("80"),
            growth_score=Decimal("65"),
            profitability_score=Decimal("70"),
            reasoning="테스트 분석",
        )
        mock_cls.return_value = mock_analyzer

        async with _client() as ac:
            resp = await ac.get("/api/analysis/fundamental/005930")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert float(data["overall_score"]) == 72.5

    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.get_session_factory")
    @patch("src.api.routes.analysis.FundamentalAnalyzer")
    async def test_no_data_result(self, mock_cls, mock_factory):
        from src.core.models import FundamentalScore

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze.return_value = FundamentalScore(
            symbol="999999",
            overall_score=Decimal("0"),
            valuation_score=Decimal("0"),
            growth_score=Decimal("0"),
            profitability_score=Decimal("0"),
            reasoning="재무제표 데이터 없음",
        )
        mock_cls.return_value = mock_analyzer

        async with _client() as ac:
            resp = await ac.get("/api/analysis/fundamental/999999")

        assert resp.status_code == 200
        data = resp.json()
        assert float(data["overall_score"]) == 0
        assert "재무제표 데이터 없음" in data["reasoning"]

    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.get_session_factory")
    @patch("src.api.routes.analysis.FundamentalAnalyzer")
    async def test_response_schema(self, mock_cls, mock_factory):
        from src.core.models import FundamentalScore

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze.return_value = FundamentalScore(
            symbol="005930",
            overall_score=Decimal("65"),
            valuation_score=Decimal("70"),
            growth_score=Decimal("60"),
            profitability_score=Decimal("65"),
            reasoning="분석 결과",
        )
        mock_cls.return_value = mock_analyzer

        async with _client() as ac:
            resp = await ac.get("/api/analysis/fundamental/005930")

        data = resp.json()
        assert all(
            k in data
            for k in [
                "symbol",
                "overall_score",
                "valuation_score",
                "growth_score",
                "profitability_score",
                "reasoning",
            ]
        )

    @pytest.mark.asyncio
    @patch("src.api.routes.analysis.get_session_factory")
    @patch("src.api.routes.analysis.FundamentalAnalyzer")
    async def test_analyzer_error_500(self, mock_cls, mock_factory):
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze.side_effect = Exception("analyzer error")
        mock_cls.return_value = mock_analyzer

        async with _client() as ac:
            resp = await ac.get("/api/analysis/fundamental/005930")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# Macro Indicators: GET /api/analysis/macro
# ═══════════════════════════════════════════════════════════════════════


class TestMacroIndicators:
    @pytest.mark.asyncio
    async def test_default(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_macro_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_source_filter(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_macro_row(source="fred")]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro?source=fred")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "fred"

    @pytest.mark.asyncio
    async def test_indicator_code_filter(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_macro_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro?indicator_code=722Y001")

        assert resp.status_code == 200
        data = resp.json()
        assert data["indicator_code"] == "722Y001"

    @pytest.mark.asyncio
    async def test_date_range(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_macro_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/analysis/macro?start_date=2026-01-01&end_date=2026-03-01"
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_source_422(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro?source=invalid")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_start_after_end_400(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/analysis/macro?start_date=2026-03-01&end_date=2026-01-01"
            )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pagination(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=50)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_macro_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro?limit=10&offset=5")

        data = resp.json()
        assert data["limit"] == 10
        assert data["offset"] == 5

    @pytest.mark.asyncio
    async def test_empty(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro")

        assert resp.status_code == 200
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# Financials: GET /api/data/financials/{symbol}
# ═══════════════════════════════════════════════════════════════════════


class TestFinancials:
    @pytest.mark.asyncio
    async def test_success(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_financial_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert len(data["items"]) == 1
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_empty(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930")

        assert resp.status_code == 200
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_fiscal_year_filter(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [
            _mock_financial_row(fiscal_year=2024)
        ]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930?fiscal_year=2024")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_report_type_filter(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [
            _mock_financial_row(report_type="quarterly")
        ]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930?report_type=quarterly")

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_report_type_422(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930?report_type=invalid")

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_pagination(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=20)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_financial_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930?limit=5&offset=10")

        data = resp.json()
        assert data["limit"] == 5
        assert data["offset"] == 10

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# News: GET /api/data/news/{symbol}
# ═══════════════════════════════════════════════════════════════════════


class TestNews:
    @pytest.mark.asyncio
    async def test_success(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_news_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/news/005930")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_empty(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/news/005930")

        assert resp.status_code == 200
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_date_range(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_news_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/data/news/005930?start_date=2026-01-01&end_date=2026-03-01"
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_start_after_end_400(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/data/news/005930?start_date=2026-03-01&end_date=2026-01-01"
            )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pagination(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=50)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_news_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/news/005930?limit=10&offset=5")

        data = resp.json()
        assert data["limit"] == 10
        assert data["offset"] == 5

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/news/005930")

        assert resp.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# Disclosures: GET /api/data/disclosures/{symbol}
# ═══════════════════════════════════════════════════════════════════════


class TestDisclosures:
    @pytest.mark.asyncio
    async def test_success(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_disclosure_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/disclosures/005930")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_empty(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=0)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/disclosures/005930")

        assert resp.status_code == 200
        assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_date_range(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_disclosure_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/data/disclosures/005930?start_date=2026-01-01&end_date=2026-03-01"
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_start_after_end_400(self):
        session = AsyncMock()
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get(
                "/api/data/disclosures/005930?start_date=2026-03-01&end_date=2026-01-01"
            )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pagination(self):
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=50)
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [_mock_disclosure_row()]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/disclosures/005930?limit=10&offset=5")

        data = resp.json()
        assert data["limit"] == 10
        assert data["offset"] == 5

    @pytest.mark.asyncio
    async def test_db_error_500(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=Exception("db error"))
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/disclosures/005930")

        assert resp.status_code == 500
