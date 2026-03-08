"""펀더멘털 분석기 — DART 재무제표 기반 종합 점수 산출.

FinancialStatement 테이블의 연간 재무데이터를 조회하여
밸류에이션·성장성·수익성 3개 축으로 종합 점수(0~100)를 산출한다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.exceptions import DatabaseError
from src.core.models import FinancialStatementInfo, FundamentalScore
from src.db.models.analysis import FinancialStatement

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Score bracket definitions
# ---------------------------------------------------------------------------

_PER_BRACKETS: list[tuple[float, int]] = [
    # (upper_bound, score) — 순서대로 검사, 첫 매칭 반환
    (0, 20),      # 적자
    (8, 90),
    (12, 80),
    (15, 65),
    (25, 45),
    (40, 25),
]
_PER_DEFAULT = 10  # >40

_PBR_BRACKETS: list[tuple[float, int]] = [
    (0, 10),
    (0.7, 90),
    (1.0, 80),
    (1.5, 65),
    (3.0, 45),
]
_PBR_DEFAULT = 20  # >3.0

_ROE_BRACKETS: list[tuple[float, int]] = [
    (0, 10),
    (5, 35),
    (10, 55),
    (15, 75),
    (25, 90),
]
_ROE_DEFAULT = 95  # >25%

_GROWTH_BRACKETS: list[tuple[float, int]] = [
    (-15, 10),
    (-5, 25),
    (0, 40),
    (5, 55),
    (10, 65),
    (20, 75),
    (30, 85),
]
_GROWTH_DEFAULT = 95  # >30%

_OP_MARGIN_BRACKETS: list[tuple[float, int]] = [
    (0, 10),
    (5, 35),
    (10, 55),
    (15, 70),
    (20, 85),
]
_OP_MARGIN_DEFAULT = 95  # >20%

_NET_MARGIN_BRACKETS: list[tuple[float, int]] = [
    (0, 10),
    (2, 30),
    (5, 50),
    (10, 65),
    (15, 80),
]
_NET_MARGIN_DEFAULT = 95  # >15%

_NEUTRAL = Decimal("50")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


class FundamentalAnalyzer:
    """펀더멘털 분석기 — DART 재무제표 기반 종합 점수 산출.

    [매매 로직에서의 역할]
    - 종목의 내재가치를 정량적으로 평가하여 매수/매도 판단에 반영
    - 기술적 분석과 결합하여 확인(confirmation) 시그널로 활용

    [계산 방식]
    점수 구성 (0~100):
    - valuation_score (40%): PER/PBR/ROE 기반 밸류에이션
    - growth_score (30%): 매출/영업이익 YoY 성장률
    - profitability_score (30%): 영업이익률, 순이익률

    [우리 전략에서의 활용]
    - Stock Analyst 에이전트가 종목 분석 시 기술적 분석과 함께 참고
    - 종합 점수 70+ → 펀더멘털 우량, 50~70 → 보통, 50 미만 → 주의
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # -- Public API ---------------------------------------------------------

    async def analyze(self, symbol: str) -> FundamentalScore:
        """종목의 펀더멘털 종합 점수를 산출한다.

        Args:
            symbol: 종목 코드 (e.g. "005930")

        Returns:
            FundamentalScore with overall/sub-scores and reasoning
        """
        statements = await self._fetch_statements(symbol, limit=3)

        if not statements:
            logger.info("fundamental.no_data", symbol=symbol)
            return FundamentalScore(
                symbol=symbol,
                overall_score=_ZERO,
                valuation_score=_ZERO,
                growth_score=_ZERO,
                profitability_score=_ZERO,
                reasoning="재무제표 데이터 없음",
            )

        latest = statements[0]  # 최신 연도

        val_score, val_reason = self._score_valuation(latest)
        growth_score, growth_reason = self._score_growth(statements)
        prof_score, prof_reason = self._score_profitability(latest)

        overall = (
            val_score * Decimal("0.4")
            + growth_score * Decimal("0.3")
            + prof_score * Decimal("0.3")
        ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

        # 0~100 클램핑
        overall = max(_ZERO, min(_HUNDRED, overall))

        reasoning = (
            f"[밸류에이션 {val_score}점] {val_reason}\n"
            f"[성장성 {growth_score}점] {growth_reason}\n"
            f"[수익성 {prof_score}점] {prof_reason}\n"
            f"→ 종합 점수: {overall}/100"
        )

        logger.info(
            "fundamental.analyzed",
            symbol=symbol,
            overall=float(overall),
            valuation=float(val_score),
            growth=float(growth_score),
            profitability=float(prof_score),
        )

        return FundamentalScore(
            symbol=symbol,
            overall_score=overall,
            valuation_score=val_score,
            growth_score=growth_score,
            profitability_score=prof_score,
            reasoning=reasoning,
        )

    def compare_sector(self, symbol: str, sector: str) -> dict:
        """섹터 비교 — 향후 확장 예정 (스텁).

        Args:
            symbol: 종목 코드
            sector: 섹터명

        Returns:
            스텁 응답 딕셔너리
        """
        return {
            "symbol": symbol,
            "sector": sector,
            "message": "향후 확장 예정",
            "available": False,
        }

    # -- Data fetching ------------------------------------------------------

    async def _fetch_statements(
        self, symbol: str, limit: int = 3
    ) -> list[FinancialStatementInfo]:
        """DB에서 최근 N개년 annual 재무제표를 조회한다."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(FinancialStatement)
                    .where(
                        FinancialStatement.symbol == symbol,
                        FinancialStatement.report_type == "annual",
                    )
                    .order_by(FinancialStatement.fiscal_year.desc())
                    .limit(limit)
                )
                result = await session.execute(stmt)
                rows = result.scalars().all()
                return [FinancialStatementInfo.model_validate(r) for r in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"재무제표 조회 실패: {exc}") from exc

    # -- Scoring: Valuation -------------------------------------------------

    def _score_valuation(
        self, fs: FinancialStatementInfo
    ) -> tuple[Decimal, str]:
        """PER/PBR/ROE 기반 밸류에이션 점수 (0~100).

        [계산 방식]
        - PER 35% + PBR 30% + ROE 35% 가중 평균
        - 각 지표가 NULL이면 중립(50) 부여
        - ROE가 NULL이지만 net_income/total_equity가 있으면 직접 계산
        """
        parts: list[str] = []

        # PER
        if fs.per is not None:
            per_val = float(fs.per)
            per_score = self._bracket_score(per_val, _PER_BRACKETS, _PER_DEFAULT)
            parts.append(f"PER {fs.per} ({self._label(per_score)})")
        else:
            per_score = 50
            parts.append("PER N/A (중립)")

        # PBR
        if fs.pbr is not None:
            pbr_val = float(fs.pbr)
            pbr_score = self._bracket_score(pbr_val, _PBR_BRACKETS, _PBR_DEFAULT)
            parts.append(f"PBR {fs.pbr} ({self._label(pbr_score)})")
        else:
            pbr_score = 50
            parts.append("PBR N/A (중립)")

        # ROE — NULL이면 net_income/total_equity로 파생 시도
        roe_value = fs.roe
        if roe_value is None and fs.net_income is not None and fs.total_equity:
            roe_value = (fs.net_income / fs.total_equity * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        if roe_value is not None:
            roe_val = float(roe_value)
            roe_score = self._bracket_score(roe_val, _ROE_BRACKETS, _ROE_DEFAULT)
            parts.append(f"ROE {roe_value}% ({self._label(roe_score)})")
        else:
            roe_score = 50
            parts.append("ROE N/A (중립)")

        total = Decimal(str(
            per_score * 0.35 + pbr_score * 0.30 + roe_score * 0.35
        )).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        return total, ", ".join(parts)

    # -- Scoring: Growth ----------------------------------------------------

    def _score_growth(
        self, statements: list[FinancialStatementInfo]
    ) -> tuple[Decimal, str]:
        """매출/영업이익 YoY 성장률 기반 성장성 점수 (0~100).

        [계산 방식]
        - 1개년만 → 중립(50)
        - 2개년: YoY 매출 성장률 50% + 영업이익 성장률 50%
        - 3개년: (최근 YoY × 0.7 + 2년 CAGR × 0.3) 블렌딩
        """
        if len(statements) < 2:
            return _NEUTRAL, "성장률 데이터 부족"

        latest = statements[0]
        prev = statements[1]

        # 최근 YoY 성장률 계산
        rev_yoy = self._calc_growth_rate(latest.revenue, prev.revenue)
        oi_yoy = self._calc_growth_rate(latest.operating_income, prev.operating_income)

        scores: list[float] = []
        parts: list[str] = []

        if rev_yoy is not None:
            rev_score = self._bracket_score(rev_yoy, _GROWTH_BRACKETS, _GROWTH_DEFAULT)
            parts.append(f"매출 YoY {rev_yoy:+.1f}%")
            scores.append(rev_score)

        if oi_yoy is not None:
            oi_score = self._bracket_score(oi_yoy, _GROWTH_BRACKETS, _GROWTH_DEFAULT)
            parts.append(f"영업이익 YoY {oi_yoy:+.1f}%")
            scores.append(oi_score)

        if not scores:
            return _NEUTRAL, "성장률 계산 불가 (이전 연도 데이터 부족)"

        yoy_avg = sum(scores) / len(scores)

        # 3개년 CAGR 블렌딩
        if len(statements) >= 3:
            oldest = statements[2]
            rev_cagr = self._calc_cagr(latest.revenue, oldest.revenue, 2)
            oi_cagr = self._calc_cagr(latest.operating_income, oldest.operating_income, 2)

            cagr_scores: list[float] = []
            if rev_cagr is not None:
                cagr_scores.append(
                    self._bracket_score(rev_cagr, _GROWTH_BRACKETS, _GROWTH_DEFAULT)
                )
            if oi_cagr is not None:
                cagr_scores.append(
                    self._bracket_score(oi_cagr, _GROWTH_BRACKETS, _GROWTH_DEFAULT)
                )

            if cagr_scores:
                cagr_avg = sum(cagr_scores) / len(cagr_scores)
                blended = yoy_avg * 0.7 + cagr_avg * 0.3
                total = Decimal(str(blended)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
                desc = self._growth_description(float(total))
                return total, ", ".join(parts) + f" ({desc})"

        total = Decimal(str(yoy_avg)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        desc = self._growth_description(float(total))
        return total, ", ".join(parts) + f" ({desc})"

    # -- Scoring: Profitability ---------------------------------------------

    def _score_profitability(
        self, fs: FinancialStatementInfo
    ) -> tuple[Decimal, str]:
        """영업이익률/순이익률 기반 수익성 점수 (0~100).

        [계산 방식]
        - 영업이익률 60% + 순이익률 40% 가중 평균
        - revenue가 0/NULL이면 0점
        """
        if not fs.revenue or fs.revenue == 0:
            return _ZERO, "매출액 데이터 없음"

        revenue = float(fs.revenue)
        parts: list[str] = []

        # 영업이익률
        if fs.operating_income is not None:
            op_margin = float(fs.operating_income) / revenue * 100
            op_score = self._bracket_score(op_margin, _OP_MARGIN_BRACKETS, _OP_MARGIN_DEFAULT)
            parts.append(f"영업이익률 {op_margin:.1f}%")
        else:
            op_score = 50
            parts.append("영업이익률 N/A")

        # 순이익률
        if fs.net_income is not None:
            net_margin = float(fs.net_income) / revenue * 100
            net_score = self._bracket_score(net_margin, _NET_MARGIN_BRACKETS, _NET_MARGIN_DEFAULT)
            parts.append(f"순이익률 {net_margin:.1f}%")
        else:
            net_score = 50
            parts.append("순이익률 N/A")

        total = Decimal(str(
            op_score * 0.6 + net_score * 0.4
        )).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        return total, ", ".join(parts)

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _bracket_score(
        value: float,
        brackets: list[tuple[float, int]],
        default: int,
    ) -> int:
        """구간별 점수 룩업.

        brackets는 (상한, 점수) 리스트로, value <= 상한인 첫 구간의 점수를 반환한다.
        어떤 구간에도 해당하지 않으면 default를 반환.
        """
        for upper, score in brackets:
            if value <= upper:
                return score
        return default

    @staticmethod
    def _calc_growth_rate(
        current: Decimal | None, previous: Decimal | None
    ) -> float | None:
        """YoY 성장률(%) 계산. 이전 값이 0/NULL이면 None 반환."""
        if current is None or previous is None or previous == 0:
            return None
        return float((current - previous) / abs(previous) * 100)

    @staticmethod
    def _calc_cagr(
        current: Decimal | None, base: Decimal | None, years: int
    ) -> float | None:
        """연평균 성장률(CAGR, %) 계산."""
        if current is None or base is None or base <= 0 or current <= 0:
            return None
        ratio = float(current / base)
        return (ratio ** (1.0 / years) - 1) * 100

    @staticmethod
    def _label(score: int) -> str:
        """점수에 따른 한글 라벨."""
        if score >= 85:
            return "저평가"
        if score >= 70:
            return "양호"
        if score >= 55:
            return "적정"
        if score >= 40:
            return "보통"
        if score >= 25:
            return "고평가"
        return "주의"

    @staticmethod
    def _growth_description(score: float) -> str:
        """성장성 점수에 따른 한글 설명."""
        if score >= 80:
            return "양호한 성장세"
        if score >= 60:
            return "안정적 성장"
        if score >= 40:
            return "성장 정체"
        return "역성장"
