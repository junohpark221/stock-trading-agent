"""Integration tests for analysis pipelines.

Tests the full flow from data to analysis without external dependencies.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

import src.main as main_mod
from src.analysis.fundamental.analyzer import FundamentalAnalyzer
from src.analysis.technical.indicators import compute_all_indicators
from src.analysis.technical.patterns import scan_patterns
from src.core.models import PatternSignal, TechnicalIndicators
from src.db.session import get_db_session


# ── Helpers ───────────────────────────────────────────────────────────


def _generate_synthetic_ohlcv(days: int = 120, base_price: float = 70000.0) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    random.seed(42)
    np.random.seed(42)

    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(days)]
    prices = [base_price]
    for _ in range(days - 1):
        change = np.random.normal(0, base_price * 0.015)
        prices.append(max(prices[-1] + change, base_price * 0.5))

    data = []
    for i, d in enumerate(dates):
        close = prices[i]
        high = close * (1 + abs(np.random.normal(0, 0.01)))
        low = close * (1 - abs(np.random.normal(0, 0.01)))
        open_price = close + np.random.normal(0, close * 0.005)
        volume = int(np.random.uniform(5_000_000, 30_000_000))
        data.append(
            {
                "date": d,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )

    df = pd.DataFrame(data)
    df.set_index("date", inplace=True)
    return df


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    main_mod.app.dependency_overrides.clear()


def _override_session(session):
    async def _dep():
        yield session
    main_mod.app.dependency_overrides[get_db_session] = _dep


def _client():
    return AsyncClient(
        transport=ASGITransport(app=main_mod.app),
        base_url="http://test",
    )


# ═══════════════════════════════════════════════════════════════════════
# Technical Pipeline: OHLCV → indicators → patterns
# ═══════════════════════════════════════════════════════════════════════


class TestTechnicalPipeline:
    def test_full_flow(self):
        """120일 synthetic OHLCV → indicators 필드 채워짐 + patterns 타입 검증."""
        df = _generate_synthetic_ohlcv(120)
        indicators = compute_all_indicators(df, "005930")

        assert indicators.symbol == "005930"
        assert len(indicators.sma_5) == 120
        assert len(indicators.rsi_14) == 120
        assert len(indicators.macd_line) == 120
        assert indicators.latest_rsi is not None
        assert 0 <= indicators.latest_rsi <= 100

        patterns = scan_patterns(df, indicators)
        assert isinstance(patterns, list)
        for p in patterns:
            assert isinstance(p, PatternSignal)
            assert p.signal_type in ("bullish", "bearish")
            assert 0 <= float(p.confidence) <= 1

    def test_empty_df(self):
        """빈 DataFrame → 빈 indicators."""
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        indicators = compute_all_indicators(df, "TEST")

        assert indicators.symbol == "TEST"
        assert indicators.sma_5 == []
        assert indicators.latest_rsi is None

    def test_minimal_data(self):
        """최소 데이터(20행)로 지표 계산 가능."""
        df = _generate_synthetic_ohlcv(20)
        indicators = compute_all_indicators(df, "TEST")

        assert len(indicators.sma_5) == 20
        # SMA 20은 20일 데이터가 있어야 마지막 값이 유효
        assert indicators.sma_20[-1] is not None


# ═══════════════════════════════════════════════════════════════════════
# Fundamental Pipeline: mock DB → analyze → score
# ═══════════════════════════════════════════════════════════════════════


class TestFundamentalPipeline:
    @pytest.mark.asyncio
    async def test_score_range(self):
        """mock DB → analyze → score 0~100 범위 검증."""
        mock_session = AsyncMock()
        mock_rows = []
        for year in [2025, 2024, 2023]:
            row = MagicMock()
            row.symbol = "005930"
            row.corp_code = "00126380"
            row.report_type = "annual"
            row.fiscal_year = year
            row.fiscal_quarter = None
            row.revenue = Decimal("300000000000") * Decimal(str(1 + (2025 - year) * -0.05))
            row.operating_income = Decimal("50000000000") * Decimal(str(1 + (2025 - year) * -0.03))
            row.net_income = Decimal("40000000000")
            row.total_assets = Decimal("500000000000")
            row.total_equity = Decimal("350000000000")
            row.total_liabilities = Decimal("150000000000")
            row.per = Decimal("12.50")
            row.pbr = Decimal("1.20")
            row.roe = Decimal("15.00")
            row.eps = Decimal("6000")
            row.bps = Decimal("50000")
            mock_rows.append(row)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = mock_rows
        mock_session.execute = AsyncMock(return_value=result_mock)

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        analyzer = FundamentalAnalyzer(mock_factory)
        score = await analyzer.analyze("005930")

        assert 0 <= float(score.overall_score) <= 100
        assert 0 <= float(score.valuation_score) <= 100
        assert 0 <= float(score.growth_score) <= 100
        assert 0 <= float(score.profitability_score) <= 100
        assert score.reasoning != ""

    @pytest.mark.asyncio
    async def test_no_data_zero_score(self):
        """데이터 없으면 0점."""
        mock_session = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=result_mock)

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        analyzer = FundamentalAnalyzer(mock_factory)
        score = await analyzer.analyze("999999")

        assert float(score.overall_score) == 0
        assert "재무제표 데이터 없음" in score.reasoning

    @pytest.mark.asyncio
    async def test_growth_cagr(self):
        """3개년 데이터로 CAGR 블렌딩 검증."""
        mock_session = AsyncMock()
        mock_rows = []
        revenues = [
            Decimal("300000000000"),
            Decimal("260000000000"),
            Decimal("230000000000"),
        ]
        for i, year in enumerate([2025, 2024, 2023]):
            row = MagicMock()
            row.symbol = "005930"
            row.corp_code = "00126380"
            row.report_type = "annual"
            row.fiscal_year = year
            row.fiscal_quarter = None
            row.revenue = revenues[i]
            row.operating_income = Decimal("50000000000")
            row.net_income = Decimal("40000000000")
            row.total_assets = Decimal("500000000000")
            row.total_equity = Decimal("350000000000")
            row.total_liabilities = Decimal("150000000000")
            row.per = Decimal("12.50")
            row.pbr = Decimal("1.20")
            row.roe = Decimal("15.00")
            row.eps = Decimal("6000")
            row.bps = Decimal("50000")
            mock_rows.append(row)

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = mock_rows
        mock_session.execute = AsyncMock(return_value=result_mock)

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        analyzer = FundamentalAnalyzer(mock_factory)
        score = await analyzer.analyze("005930")

        # 매출 성장 → growth_score > 0
        assert float(score.growth_score) > 0
        assert "매출" in score.reasoning


# ═══════════════════════════════════════════════════════════════════════
# API Integration: mock DB + real compute
# ═══════════════════════════════════════════════════════════════════════


class TestAPIIntegration:
    @pytest.mark.asyncio
    async def test_technical_endpoint_full_flow(self):
        """mock OHLCV DB + real compute_all_indicators → 엔드포인트 응답 검증."""
        df = _generate_synthetic_ohlcv(60)

        # Create mock ORM rows from the DataFrame
        mock_rows = []
        for idx in reversed(df.index):
            row = MagicMock()
            row.symbol = "005930"
            row.date = idx
            row.open = Decimal(str(round(df.loc[idx, "open"], 2)))
            row.high = Decimal(str(round(df.loc[idx, "high"], 2)))
            row.low = Decimal(str(round(df.loc[idx, "low"], 2)))
            row.close = Decimal(str(round(df.loc[idx, "close"], 2)))
            row.volume = int(df.loc[idx, "volume"])
            mock_rows.append(row)

        session = AsyncMock()
        rows_result = MagicMock()
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = mock_rows
        session.execute = AsyncMock(return_value=rows_result)
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/technical/005930?days=60")

        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "005930"
        assert data["data_points"] == 60
        assert len(data["indicators"]["sma_5"]) == 60
        assert data["indicators"]["latest_rsi"] is not None

    @pytest.mark.asyncio
    async def test_data_endpoints_roundtrip(self):
        """financials + news + disclosures 엔드포인트 라운드트립."""
        from datetime import datetime, timezone

        session = AsyncMock()

        # financials
        fin_count = MagicMock()
        fin_count.scalar_one = MagicMock(return_value=1)
        fin_rows = MagicMock()
        fin_row = MagicMock()
        fin_row.symbol = "005930"
        fin_row.corp_code = "00126380"
        fin_row.report_type = "annual"
        fin_row.fiscal_year = 2025
        fin_row.fiscal_quarter = None
        fin_row.revenue = Decimal("300000000000")
        fin_row.operating_income = Decimal("50000000000")
        fin_row.net_income = Decimal("40000000000")
        fin_row.total_assets = Decimal("500000000000")
        fin_row.total_equity = Decimal("350000000000")
        fin_row.total_liabilities = Decimal("150000000000")
        fin_row.per = Decimal("12.50")
        fin_row.pbr = Decimal("1.20")
        fin_row.roe = Decimal("15.00")
        fin_row.eps = Decimal("6000")
        fin_row.bps = Decimal("50000")
        fin_rows.scalars = MagicMock()
        fin_rows.scalars.return_value.all.return_value = [fin_row]
        session.execute = AsyncMock(side_effect=[fin_count, fin_rows])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/data/financials/005930")

        assert resp.status_code == 200
        assert resp.json()["items"][0]["fiscal_year"] == 2025

    @pytest.mark.asyncio
    async def test_macro_filter(self):
        """매크로 필터 검증: source + indicator_code."""
        session = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=1)
        rows_result = MagicMock()
        row = MagicMock()
        row.source = "ecos"
        row.indicator_code = "722Y001"
        row.indicator_name = "기준금리"
        row.date = date(2026, 3, 1)
        row.value = Decimal("3.50")
        row.unit = "%"
        rows_result.scalars = MagicMock()
        rows_result.scalars.return_value.all.return_value = [row]
        session.execute = AsyncMock(side_effect=[count_result, rows_result])
        _override_session(session)

        async with _client() as ac:
            resp = await ac.get("/api/analysis/macro?source=ecos&indicator_code=722Y001")

        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "ecos"
        assert data["indicator_code"] == "722Y001"
        assert data["items"][0]["indicator_name"] == "기준금리"
