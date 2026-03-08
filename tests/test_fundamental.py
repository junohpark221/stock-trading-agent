"""Unit tests for FundamentalAnalyzer.

All DB calls are mocked — no real database required.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.fundamental.analyzer import (
    FundamentalAnalyzer,
    _GROWTH_BRACKETS,
    _GROWTH_DEFAULT,
    _NET_MARGIN_BRACKETS,
    _NET_MARGIN_DEFAULT,
    _OP_MARGIN_BRACKETS,
    _OP_MARGIN_DEFAULT,
    _PBR_BRACKETS,
    _PBR_DEFAULT,
    _PER_BRACKETS,
    _PER_DEFAULT,
    _ROE_BRACKETS,
    _ROE_DEFAULT,
)
from src.core.exceptions import DatabaseError
from src.core.models import FinancialStatementInfo

# -- Helpers ---------------------------------------------------------------


def _mock_session_factory(session=None):
    """Create a mock async session factory (async with pattern)."""
    mock_session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, mock_session


def _make_fs(**kwargs) -> FinancialStatementInfo:
    """Create a FinancialStatementInfo with sensible defaults."""
    defaults = {
        "symbol": "005930",
        "corp_code": "00126380",
        "report_type": "annual",
        "fiscal_year": 2025,
        "fiscal_quarter": None,
        "revenue": Decimal("300000000000"),
        "operating_income": Decimal("50000000000"),
        "net_income": Decimal("40000000000"),
        "total_assets": Decimal("400000000000"),
        "total_equity": Decimal("300000000000"),
        "total_liabilities": Decimal("100000000000"),
        "per": Decimal("15.50"),
        "pbr": Decimal("1.20"),
        "roe": Decimal("13.50"),
        "eps": Decimal("5000.00"),
        "bps": Decimal("40000.00"),
    }
    defaults.update(kwargs)
    return FinancialStatementInfo(**defaults)


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


def _make_analyzer(session=None):
    """Create FundamentalAnalyzer with mocked session factory."""
    sf, mock_session = _mock_session_factory(session)
    return FundamentalAnalyzer(session_factory=sf), mock_session


# =========================================================================
# TestBracketScore
# =========================================================================


class TestBracketScore:
    """_bracket_score 정적 메서드 테스트."""

    def test_first_bracket(self):
        assert FundamentalAnalyzer._bracket_score(-5, _PER_BRACKETS, _PER_DEFAULT) == 20

    def test_middle_bracket(self):
        assert FundamentalAnalyzer._bracket_score(10, _PER_BRACKETS, _PER_DEFAULT) == 80

    def test_default_above_all(self):
        assert FundamentalAnalyzer._bracket_score(50, _PER_BRACKETS, _PER_DEFAULT) == _PER_DEFAULT

    def test_exact_boundary(self):
        # value == upper_bound → 해당 구간 매칭
        assert FundamentalAnalyzer._bracket_score(8, _PER_BRACKETS, _PER_DEFAULT) == 90


# =========================================================================
# TestScoreValuation
# =========================================================================


class TestScoreValuation:
    """_score_valuation 테스트."""

    def setup_method(self):
        self.analyzer, _ = _make_analyzer()

    def test_low_per_high_score(self):
        """PER 5 → 고평가 점수 90."""
        fs = _make_fs(per=Decimal("5.0"), pbr=Decimal("0.5"), roe=Decimal("20.0"))
        score, reason = self.analyzer._score_valuation(fs)
        assert score >= 85  # PER 90 + PBR 90 + ROE 90 → 90

    def test_high_per_low_score(self):
        """PER 45 → 저평가 점수 10."""
        fs = _make_fs(per=Decimal("45.0"), pbr=Decimal("4.0"), roe=Decimal("-5.0"))
        score, reason = self.analyzer._score_valuation(fs)
        assert score <= 20

    def test_negative_per_deficit(self):
        """PER ≤ 0 (적자) → 점수 20."""
        fs = _make_fs(per=Decimal("-10.0"), pbr=Decimal("1.0"), roe=Decimal("10.0"))
        score, reason = self.analyzer._score_valuation(fs)
        # PER 20*0.35=7, PBR 80*0.30=24, ROE 55*0.35=19.25 → 50
        assert 48 <= score <= 52

    def test_all_null_neutral(self):
        """모든 밸류에이션 지표가 NULL → 중립(50)."""
        fs = _make_fs(
            per=None, pbr=None, roe=None,
            net_income=None, total_equity=None,
        )
        score, reason = self.analyzer._score_valuation(fs)
        assert score == 50

    def test_per_null_neutral(self):
        """PER만 NULL → PER는 중립(50), 나머지는 계산."""
        fs = _make_fs(per=None, pbr=Decimal("0.8"), roe=Decimal("14.0"))
        score, reason = self.analyzer._score_valuation(fs)
        assert "PER N/A" in reason

    def test_pbr_negative(self):
        """PBR 음수 → 점수 10."""
        fs = _make_fs(pbr=Decimal("-0.5"))
        score, reason = self.analyzer._score_valuation(fs)
        assert "PBR" in reason

    def test_roe_derived_from_equity(self):
        """ROE=NULL이지만 net_income/total_equity로 파생."""
        fs = _make_fs(
            roe=None,
            net_income=Decimal("40000000000"),
            total_equity=Decimal("300000000000"),
        )
        score, reason = self.analyzer._score_valuation(fs)
        assert "ROE" in reason
        assert "N/A" not in reason.split("ROE")[1].split(",")[0]

    def test_roe_derived_not_possible(self):
        """ROE=NULL, net_income=NULL → 중립."""
        fs = _make_fs(roe=None, net_income=None, total_equity=Decimal("300000000000"))
        score, reason = self.analyzer._score_valuation(fs)
        assert "ROE N/A" in reason

    def test_high_roe(self):
        """ROE > 25% → 점수 95."""
        fs = _make_fs(roe=Decimal("30.0"))
        score, reason = self.analyzer._score_valuation(fs)
        assert "양호" in reason or "저평가" in reason

    def test_per_and_pbr_boundaries(self):
        """PER=12, PBR=1.0 → 경계값 정확 매칭."""
        fs = _make_fs(per=Decimal("12.0"), pbr=Decimal("1.0"), roe=Decimal("10.0"))
        score, reason = self.analyzer._score_valuation(fs)
        # PER 12→80, PBR 1.0→80, ROE 10→55
        expected = Decimal(str(80 * 0.35 + 80 * 0.30 + 55 * 0.35)).quantize(Decimal("1"))
        assert score == expected


# =========================================================================
# TestScoreGrowth
# =========================================================================


class TestScoreGrowth:
    """_score_growth 테스트."""

    def setup_method(self):
        self.analyzer, _ = _make_analyzer()

    def test_single_year_neutral(self):
        """1개년만 → 중립(50)."""
        stmts = [_make_fs(fiscal_year=2025)]
        score, reason = self.analyzer._score_growth(stmts)
        assert score == 50
        assert "부족" in reason

    def test_strong_growth(self):
        """매출/영업이익 모두 35% 성장 → 고득점."""
        stmts = [
            _make_fs(
                fiscal_year=2025,
                revenue=Decimal("400000000000"),
                operating_income=Decimal("70000000000"),
            ),
            _make_fs(
                fiscal_year=2024,
                revenue=Decimal("300000000000"),
                operating_income=Decimal("50000000000"),
            ),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        assert score >= 80

    def test_strong_decline(self):
        """매출/영업이익 모두 -20% → 저득점."""
        stmts = [
            _make_fs(
                fiscal_year=2025,
                revenue=Decimal("240000000000"),
                operating_income=Decimal("40000000000"),
            ),
            _make_fs(
                fiscal_year=2024,
                revenue=Decimal("300000000000"),
                operating_income=Decimal("50000000000"),
            ),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        assert score <= 30

    def test_prev_revenue_zero_skips(self):
        """이전 연도 revenue=0 → 매출 성장률 스킵, 영업이익만 사용."""
        stmts = [
            _make_fs(fiscal_year=2025, revenue=Decimal("300000000000")),
            _make_fs(fiscal_year=2024, revenue=Decimal("0")),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        # 영업이익 성장률만 사용
        assert "영업이익" in reason

    def test_mixed_growth(self):
        """매출↑, 영업이익↓ → 혼합."""
        stmts = [
            _make_fs(
                fiscal_year=2025,
                revenue=Decimal("400000000000"),
                operating_income=Decimal("30000000000"),
            ),
            _make_fs(
                fiscal_year=2024,
                revenue=Decimal("300000000000"),
                operating_income=Decimal("50000000000"),
            ),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        # 매출 +33% (95점), 영업이익 -40% (10점) → 평균 ~52
        assert 40 <= score <= 60

    def test_both_prev_null_neutral(self):
        """이전 연도 revenue=NULL, operating_income=NULL → 계산 불가."""
        stmts = [
            _make_fs(fiscal_year=2025),
            _make_fs(fiscal_year=2024, revenue=None, operating_income=None),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        assert score == 50  # neutral
        assert "계산 불가" in reason

    def test_three_year_cagr_blending(self):
        """3개년 데이터 → CAGR 블렌딩."""
        stmts = [
            _make_fs(
                fiscal_year=2025,
                revenue=Decimal("400000000000"),
                operating_income=Decimal("70000000000"),
            ),
            _make_fs(
                fiscal_year=2024,
                revenue=Decimal("350000000000"),
                operating_income=Decimal("60000000000"),
            ),
            _make_fs(
                fiscal_year=2023,
                revenue=Decimal("300000000000"),
                operating_income=Decimal("50000000000"),
            ),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        assert score >= 60  # consistent growth

    def test_deficit_to_profit_turnaround(self):
        """적자→흑자 전환 (절대값 분모)."""
        stmts = [
            _make_fs(
                fiscal_year=2025,
                operating_income=Decimal("30000000000"),
            ),
            _make_fs(
                fiscal_year=2024,
                operating_income=Decimal("-20000000000"),
            ),
        ]
        score, reason = self.analyzer._score_growth(stmts)
        # OI: 250% → 95, revenue: 0% → 40 (0% hits ≤0 bracket), avg=67.5 → 68
        assert score >= 60


# =========================================================================
# TestScoreProfitability
# =========================================================================


class TestScoreProfitability:
    """_score_profitability 테스트."""

    def setup_method(self):
        self.analyzer, _ = _make_analyzer()

    def test_high_margin(self):
        """영업이익률 25%, 순이익률 20% → 고득점."""
        fs = _make_fs(
            revenue=Decimal("100000000000"),
            operating_income=Decimal("25000000000"),
            net_income=Decimal("20000000000"),
        )
        score, reason = self.analyzer._score_profitability(fs)
        assert score >= 90

    def test_low_margin(self):
        """영업이익률 2%, 순이익률 1% → 저득점."""
        fs = _make_fs(
            revenue=Decimal("100000000000"),
            operating_income=Decimal("2000000000"),
            net_income=Decimal("1000000000"),
        )
        score, reason = self.analyzer._score_profitability(fs)
        assert score <= 40

    def test_negative_margin(self):
        """적자 → 10점."""
        fs = _make_fs(
            revenue=Decimal("100000000000"),
            operating_income=Decimal("-5000000000"),
            net_income=Decimal("-3000000000"),
        )
        score, reason = self.analyzer._score_profitability(fs)
        assert score == 10

    def test_revenue_zero(self):
        """revenue=0 → 0점."""
        fs = _make_fs(revenue=Decimal("0"))
        score, reason = self.analyzer._score_profitability(fs)
        assert score == 0
        assert "매출액 데이터 없음" in reason

    def test_revenue_null(self):
        """revenue=None → 0점."""
        fs = _make_fs(revenue=None)
        score, reason = self.analyzer._score_profitability(fs)
        assert score == 0

    def test_oi_only_no_ni(self):
        """operating_income만 있고 net_income=None → NI 중립(50)."""
        fs = _make_fs(
            revenue=Decimal("100000000000"),
            operating_income=Decimal("15000000000"),
            net_income=None,
        )
        score, reason = self.analyzer._score_profitability(fs)
        # OI margin 15% → 70, NI N/A → 50: 70*0.6+50*0.4=62
        assert score == 62
        assert "순이익률 N/A" in reason


# =========================================================================
# TestAnalyze
# =========================================================================


class TestAnalyze:
    """analyze() 통합 테스트."""

    @pytest.mark.asyncio
    async def test_no_data_returns_zero(self):
        """데이터 없음 → score=0, reasoning='재무제표 데이터 없음'."""
        analyzer, mock_session = _make_analyzer()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")
        assert score.overall_score == 0
        assert "재무제표 데이터 없음" in score.reasoning

    @pytest.mark.asyncio
    async def test_happy_path_three_years(self):
        """3개년 데이터 → 가중 점수 산출."""
        rows = [
            _make_financial_row(
                fiscal_year=2025,
                revenue=Decimal("400000000000"),
                operating_income=Decimal("70000000000"),
                net_income=Decimal("55000000000"),
                per=Decimal("12.0"),
                pbr=Decimal("0.9"),
                roe=Decimal("18.0"),
            ),
            _make_financial_row(
                fiscal_year=2024,
                revenue=Decimal("350000000000"),
                operating_income=Decimal("60000000000"),
            ),
            _make_financial_row(
                fiscal_year=2023,
                revenue=Decimal("300000000000"),
                operating_income=Decimal("50000000000"),
            ),
        ]
        analyzer, mock_session = _make_analyzer()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")
        assert 0 <= score.overall_score <= 100
        assert score.valuation_score > 0
        assert score.growth_score > 0
        assert score.profitability_score > 0
        assert "밸류에이션" in score.reasoning
        assert "성장성" in score.reasoning
        assert "수익성" in score.reasoning

    @pytest.mark.asyncio
    async def test_single_year_growth_neutral(self):
        """1개년만 → growth=50."""
        rows = [_make_financial_row(fiscal_year=2025)]
        analyzer, mock_session = _make_analyzer()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")
        assert score.growth_score == 50

    @pytest.mark.asyncio
    async def test_weight_calculation(self):
        """가중치 40/30/30 산술 검증."""
        analyzer, mock_session = _make_analyzer()
        rows = [_make_financial_row(fiscal_year=2025)]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")

        # Manually recalculate: overall = val*0.4 + growth*0.3 + prof*0.3
        expected = (
            score.valuation_score * Decimal("0.4")
            + score.growth_score * Decimal("0.3")
            + score.profitability_score * Decimal("0.3")
        ).quantize(Decimal("0.1"))
        assert abs(score.overall_score - expected) <= Decimal("0.5")

    @pytest.mark.asyncio
    async def test_score_clamped_0_100(self):
        """점수는 항상 0~100 범위."""
        # 극단적으로 좋은 데이터
        rows = [
            _make_financial_row(
                fiscal_year=2025,
                per=Decimal("3.0"),
                pbr=Decimal("0.3"),
                roe=Decimal("35.0"),
                revenue=Decimal("100000000000"),
                operating_income=Decimal("30000000000"),
                net_income=Decimal("25000000000"),
            ),
        ]
        analyzer, mock_session = _make_analyzer()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")
        assert 0 <= score.overall_score <= 100

    @pytest.mark.asyncio
    async def test_reasoning_format(self):
        """reasoning 포맷 확인."""
        rows = [_make_financial_row(fiscal_year=2025)]
        analyzer, mock_session = _make_analyzer()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")
        assert "[밸류에이션" in score.reasoning
        assert "[성장성" in score.reasoning
        assert "[수익성" in score.reasoning
        assert "→ 종합 점수:" in score.reasoning

    @pytest.mark.asyncio
    async def test_db_error_propagated(self):
        """DB 에러 → DatabaseError 전파."""
        analyzer, mock_session = _make_analyzer()
        mock_session.execute = AsyncMock(side_effect=Exception("connection lost"))

        with pytest.raises(DatabaseError, match="재무제표 조회 실패"):
            await analyzer.analyze("005930")

    @pytest.mark.asyncio
    async def test_db_error_passthrough(self):
        """DatabaseError는 그대로 전파 (이중 래핑 방지)."""
        analyzer, mock_session = _make_analyzer()
        mock_session.execute = AsyncMock(
            side_effect=DatabaseError("original error")
        )

        with pytest.raises(DatabaseError, match="original error"):
            await analyzer.analyze("005930")

    @pytest.mark.asyncio
    async def test_all_extreme_low(self):
        """극단적으로 나쁜 데이터 → 여전히 0~100 범위."""
        rows = [
            _make_financial_row(
                fiscal_year=2025,
                per=Decimal("50.0"),
                pbr=Decimal("5.0"),
                roe=Decimal("-10.0"),
                revenue=Decimal("100000000000"),
                operating_income=Decimal("-5000000000"),
                net_income=Decimal("-3000000000"),
            ),
        ]
        analyzer, mock_session = _make_analyzer()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        mock_session.execute = AsyncMock(return_value=result)

        score = await analyzer.analyze("005930")
        assert 0 <= score.overall_score <= 100
        assert score.overall_score <= 30  # should be low


# =========================================================================
# TestCompareSector
# =========================================================================


class TestCompareSector:
    """compare_sector 스텁 테스트."""

    def test_stub_response(self):
        analyzer, _ = _make_analyzer()
        result = analyzer.compare_sector("005930", "반도체")
        assert result["symbol"] == "005930"
        assert result["sector"] == "반도체"
        assert result["available"] is False
        assert "향후 확장 예정" in result["message"]

    def test_stub_different_inputs(self):
        analyzer, _ = _make_analyzer()
        result = analyzer.compare_sector("035720", "게임")
        assert result["symbol"] == "035720"
        assert result["sector"] == "게임"
