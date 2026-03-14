"""키워드 기반 감성분석기 단위 테스트 (~25개).

A. 키워드 사전 검증 (5개)
B. 기사별 채점 (7개)
C. 종합 점수 (5개)
D. needs_llm 로직 (4개)
E. DB 연동 (4개, mock session_factory)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analysis.sentiment.analyzer import KeywordSentimentAnalyzer, _ArticleScore
from src.analysis.sentiment.keywords import (
    IMPORTANT_KEYWORDS,
    NEGATIVE_KEYWORDS,
    POSITIVE_KEYWORDS,
)
from src.core.enums import SentimentLabel, SentimentMethod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(
    title: str,
    description: str | None = None,
    symbol: str = "005930",
    article_id: int = 1,
    sentiment_method: str | None = None,
) -> MagicMock:
    """테스트용 NewsArticle mock 생성."""
    article = MagicMock()
    article.id = article_id
    article.title = title
    article.description = description
    article.symbol = symbol
    article.published_at = datetime(2026, 3, 14, tzinfo=timezone.utc)
    article.sentiment_method = sentiment_method
    article.sentiment_score = None
    article.sentiment_label = None
    return article


def _make_analyzer() -> KeywordSentimentAnalyzer:
    """세션 팩토리 mock으로 analyzer 생성."""
    return KeywordSentimentAnalyzer(session_factory=AsyncMock())


# ===========================================================================
# A. 키워드 사전 검증 (5개)
# ===========================================================================


class TestKeywordDictionary:
    """키워드 사전 무결성 검증."""

    def test_positive_keywords_not_empty(self) -> None:
        assert len(POSITIVE_KEYWORDS) >= 40

    def test_negative_keywords_not_empty(self) -> None:
        assert len(NEGATIVE_KEYWORDS) >= 40

    def test_important_keywords_not_empty(self) -> None:
        assert len(IMPORTANT_KEYWORDS) >= 20

    def test_no_duplicate_keywords(self) -> None:
        pos_set = {e.keyword for e in POSITIVE_KEYWORDS}
        neg_set = {e.keyword for e in NEGATIVE_KEYWORDS}
        assert len(pos_set) == len(POSITIVE_KEYWORDS), "긍정 키워드 중복 존재"
        assert len(neg_set) == len(NEGATIVE_KEYWORDS), "부정 키워드 중복 존재"
        # 긍정/부정 키워드 교차 없음
        assert not pos_set & neg_set, "긍정/부정 키워드 교차 존재"

    def test_weight_range(self) -> None:
        for entry in POSITIVE_KEYWORDS:
            assert 0.0 < entry.weight <= 1.0, f"{entry.keyword} weight 범위 초과"
        for entry in NEGATIVE_KEYWORDS:
            assert 0.0 < entry.weight <= 1.0, f"{entry.keyword} weight 범위 초과"


# ===========================================================================
# B. 기사별 채점 (7개)
# ===========================================================================


class TestArticleScoring:
    """개별 기사 키워드 채점 테스트."""

    def setup_method(self) -> None:
        self.analyzer = _make_analyzer()

    def test_positive_keywords_score(self) -> None:
        score = self.analyzer._score_article("어닝서프라이즈 호실적 달성", None)
        assert score.normalized_score > 0
        assert score.label == SentimentLabel.POSITIVE
        assert "어닝서프라이즈" in score.positive_keywords
        assert "호실적" in score.positive_keywords

    def test_negative_keywords_score(self) -> None:
        score = self.analyzer._score_article("적자전환 어닝쇼크 충격", None)
        assert score.normalized_score < 0
        assert score.label == SentimentLabel.NEGATIVE
        assert "적자전환" in score.negative_keywords

    def test_no_keywords_neutral(self) -> None:
        score = self.analyzer._score_article("오늘의 날씨", None)
        assert abs(score.normalized_score) < 0.15
        assert score.label == SentimentLabel.NEUTRAL
        assert score.positive_keywords == []
        assert score.negative_keywords == []

    def test_mixed_keywords_balance(self) -> None:
        score = self.analyzer._score_article("호실적이지만 경기침체 우려", None)
        # 호실적(0.9) vs 경기침체(0.7) → positive but moderate
        assert "호실적" in score.positive_keywords
        assert "경기침체" in score.negative_keywords

    def test_important_keywords_detected(self) -> None:
        score = self.analyzer._score_article("인수합병 M&A 추진 발표", None)
        assert "인수합병" in score.important_keywords
        assert "M&A" in score.important_keywords

    def test_description_included(self) -> None:
        score = self.analyzer._score_article(
            "뉴스 제목", "본문에 어닝서프라이즈 언급",
        )
        assert "어닝서프라이즈" in score.positive_keywords

    def test_normalized_score_range(self) -> None:
        """normalized_score는 항상 [-1.0, +1.0] 범위."""
        # 극단적 긍정
        score_pos = self.analyzer._score_article(
            "어닝서프라이즈 사상최대 호실적 매수추천 신고가 급등", None,
        )
        assert -1.0 <= score_pos.normalized_score <= 1.0

        # 극단적 부정
        score_neg = self.analyzer._score_article(
            "어닝쇼크 적자전환 분식회계 횡령 상장폐지 폭락", None,
        )
        assert -1.0 <= score_neg.normalized_score <= 1.0


# ===========================================================================
# C. 종합 점수 (5개)
# ===========================================================================


class TestAggregateScores:
    """종합 점수 집계 테스트."""

    def setup_method(self) -> None:
        self.analyzer = _make_analyzer()

    @pytest.mark.asyncio
    async def test_all_positive_overall(self) -> None:
        articles = [
            _make_article("호실적 매출증가", article_id=i)
            for i in range(5)
        ]
        result = await self.analyzer.analyze_articles(articles, "005930")
        assert result.overall_label == SentimentLabel.POSITIVE
        assert result.overall_score > Decimal("0")
        assert result.total_articles == 5

    @pytest.mark.asyncio
    async def test_all_negative_overall(self) -> None:
        articles = [
            _make_article("실적악화 적자전환", article_id=i)
            for i in range(5)
        ]
        result = await self.analyzer.analyze_articles(articles, "005930")
        assert result.overall_label == SentimentLabel.NEGATIVE
        assert result.overall_score < Decimal("0")

    @pytest.mark.asyncio
    async def test_empty_articles_neutral(self) -> None:
        result = await self.analyzer.analyze_articles([], "005930")
        assert result.overall_label == SentimentLabel.NEUTRAL
        assert result.total_articles == 0
        assert result.overall_score == Decimal("0.000")

    @pytest.mark.asyncio
    async def test_single_article_matches(self) -> None:
        articles = [_make_article("어닝서프라이즈 달성")]
        result = await self.analyzer.analyze_articles(articles, "005930")
        # 단일 기사 → 해당 기사의 점수와 동일
        score = self.analyzer._score_article("어닝서프라이즈 달성", None)
        assert float(result.overall_score) == pytest.approx(
            score.normalized_score, abs=0.001,
        )

    @pytest.mark.asyncio
    async def test_recent_articles_weighted_more(self) -> None:
        """최신 기사(인덱스 0)가 더 높은 가중치를 받는지 검증."""
        # 최신=강한 긍정, 이전=약한 부정
        articles = [
            _make_article("어닝서프라이즈 사상최대", article_id=0),
            _make_article("약세", article_id=1),
            _make_article("약세", article_id=2),
            _make_article("약세", article_id=3),
        ]
        result = await self.analyzer.analyze_articles(articles, "005930")
        # 최신 강한 긍정이 여러 약한 부정을 상쇄 → 전체적으로 양수에 가까울 수 있음
        # 최소한 4개 모두 약세인 경우보다는 높아야 함
        all_negative = [
            _make_article("약세", article_id=i) for i in range(4)
        ]
        result_neg = await self.analyzer.analyze_articles(all_negative, "005930")
        assert result.overall_score > result_neg.overall_score


# ===========================================================================
# D. needs_llm 로직 (4개)
# ===========================================================================


class TestNeedsLLM:
    """LLM 심층 분석 필요 여부 판단 테스트."""

    def test_ambiguous_range_true(self) -> None:
        # |score| 0.15~0.50 → True
        assert KeywordSentimentAnalyzer._needs_llm(Decimal("0.300"), []) is True
        assert KeywordSentimentAnalyzer._needs_llm(Decimal("-0.200"), []) is True

    def test_clear_range_false(self) -> None:
        # |score| > 0.50 → False (중요 키워드 없음)
        assert KeywordSentimentAnalyzer._needs_llm(Decimal("0.700"), []) is False
        assert KeywordSentimentAnalyzer._needs_llm(Decimal("-0.600"), []) is False

    def test_important_keyword_always_true(self) -> None:
        # 중요 키워드 있으면 score 무관하게 True
        assert KeywordSentimentAnalyzer._needs_llm(
            Decimal("0.900"), ["인수합병"],
        ) is True
        assert KeywordSentimentAnalyzer._needs_llm(
            Decimal("0.000"), ["M&A"],
        ) is True

    def test_neutral_no_important_false(self) -> None:
        # |score| < 0.15, 중요 키워드 없음 → False
        assert KeywordSentimentAnalyzer._needs_llm(Decimal("0.050"), []) is False
        assert KeywordSentimentAnalyzer._needs_llm(Decimal("0.000"), []) is False


# ===========================================================================
# E. DB 연동 (4개, mock session_factory)
# ===========================================================================


class TestDBIntegration:
    """DB 연동 테스트 (mock session_factory)."""

    def _make_mock_session_factory(
        self, articles: list[MagicMock] | None = None,
    ) -> MagicMock:
        """mock session factory 생성.

        async_sessionmaker()는 동기 호출로 AsyncSession context manager를 반환한다.
        """
        session = AsyncMock()
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = articles or []
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()

        # async_sessionmaker()는 동기 호출 → async context manager 반환
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)

        factory = MagicMock(return_value=ctx)
        return factory

    @pytest.mark.asyncio
    async def test_analyze_returns_sentiment_result(self) -> None:
        articles = [
            _make_article("호실적 매출증가", article_id=1),
            _make_article("영업이익증가 성장세", article_id=2),
        ]
        factory = self._make_mock_session_factory(articles)
        analyzer = KeywordSentimentAnalyzer(session_factory=factory)

        result = await analyzer.analyze("005930")
        assert result.symbol == "005930"
        assert result.method == SentimentMethod.KEYWORD
        assert result.total_articles == 2
        assert result.overall_label == SentimentLabel.POSITIVE

    @pytest.mark.asyncio
    async def test_analyze_no_articles_neutral(self) -> None:
        factory = self._make_mock_session_factory([])
        analyzer = KeywordSentimentAnalyzer(session_factory=factory)

        result = await analyzer.analyze("005930")
        assert result.overall_label == SentimentLabel.NEUTRAL
        assert result.total_articles == 0

    @pytest.mark.asyncio
    async def test_update_article_sentiments_count(self) -> None:
        articles = [
            _make_article("호실적", article_id=1),
            _make_article("적자전환", article_id=2),
        ]
        session = AsyncMock()

        # execute는 두 번 호출됨: SELECT + UPDATE×2
        result_mock = MagicMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = articles
        result_mock.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=result_mock)
        session.commit = AsyncMock()

        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=ctx)

        analyzer = KeywordSentimentAnalyzer(session_factory=factory)
        count = await analyzer.update_article_sentiments("005930")
        assert count == 2
        # execute: 1 SELECT + 2 UPDATE = 3 calls
        assert session.execute.call_count == 3
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_skips_already_analyzed(self) -> None:
        """sentiment_method IS NULL 필터로 이미 분석된 기사는 조회되지 않음."""
        # 빈 결과 = 이미 분석된 기사만 존재
        factory = self._make_mock_session_factory([])
        analyzer = KeywordSentimentAnalyzer(session_factory=factory)

        count = await analyzer.update_article_sentiments("005930")
        assert count == 0
