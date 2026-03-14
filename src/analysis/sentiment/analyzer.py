"""키워드 기반 감성분석기 — 하이브리드 감성분석의 1차 단계.

NaverProvider가 수집한 news_article 테이블의 기사를 키워드 매칭으로
1차 분류하고, 애매하거나 중요한 뉴스는 needs_llm_analysis=True 플래그로
Step 8의 Stock Analyst LLM 심층 분석에 위임한다.

비용: $0 (순수 키워드 매칭)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.analysis.sentiment.keywords import (
    IMPORTANT_KEYWORDS,
    NEGATIVE_KEYWORDS,
    POSITIVE_KEYWORDS,
)
from src.core.enums import SentimentLabel, SentimentMethod
from src.core.exceptions import DatabaseError
from src.core.models import SentimentResult
from src.db.models.analysis import NewsArticle

logger = structlog.get_logger(__name__)

_NEUTRAL_THRESHOLD = 0.15
_AMBIGUOUS_UPPER = 0.50


# ---------------------------------------------------------------------------
# Internal data class
# ---------------------------------------------------------------------------


@dataclass
class _ArticleScore:
    """개별 기사의 키워드 채점 결과."""

    positive_keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    important_keywords: list[str] = field(default_factory=list)
    positive_weight_sum: float = 0.0
    negative_weight_sum: float = 0.0
    raw_score: float = 0.0
    normalized_score: float = 0.0
    label: SentimentLabel = SentimentLabel.NEUTRAL


# ---------------------------------------------------------------------------
# KeywordSentimentAnalyzer
# ---------------------------------------------------------------------------


class KeywordSentimentAnalyzer:
    """키워드 기반 감성분석기.

    [매매 로직에서의 역할]
    - 뉴스 기사의 감성을 비용 $0으로 1차 분류
    - 명확한 긍정/부정은 즉시 반영, 애매한 기사는 LLM에 위임
    - Stock Analyst 에이전트가 종목 분석 시 참고

    [채점 방식]
    1. 기사별: 키워드 매칭 → raw_score → tanh 정규화 → [-1, +1]
    2. 종합: 시간 가중 평균 (최신 기사 가중치 높음)
    3. LLM 위임 판단: 애매 구간(0.15~0.50) 또는 중요 키워드 매칭
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # -- Public API ---------------------------------------------------------

    async def analyze(self, symbol: str, limit: int = 50) -> SentimentResult:
        """DB에서 기사 조회 → 키워드 분석 → SentimentResult 반환."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(NewsArticle)
                    .where(NewsArticle.symbol == symbol)
                    .order_by(NewsArticle.published_at.desc())
                    .limit(limit)
                )
                result = await session.execute(stmt)
                articles = list(result.scalars().all())
        except Exception as exc:
            raise DatabaseError(f"뉴스 기사 조회 실패: {exc}") from exc

        return self._analyze_article_list(articles, symbol)

    async def analyze_articles(
        self,
        articles: list[NewsArticle],
        symbol: str = "",
    ) -> SentimentResult:
        """이미 로드된 기사 목록을 직접 분석. Step 8 StockAnalyst에서 사용."""
        if articles and not symbol:
            symbol = articles[0].symbol or ""
        return self._analyze_article_list(articles, symbol)

    async def update_article_sentiments(
        self,
        symbol: str,
        limit: int = 50,
    ) -> int:
        """미분석 기사(sentiment_method IS NULL)를 키워드로 채점 → DB 업데이트 → 건수 반환."""
        try:
            async with self._session_factory() as session:
                # 미분석 기사 조회
                stmt = (
                    select(NewsArticle)
                    .where(
                        NewsArticle.symbol == symbol,
                        NewsArticle.sentiment_method.is_(None),
                    )
                    .order_by(NewsArticle.published_at.desc())
                    .limit(limit)
                )
                result = await session.execute(stmt)
                articles = list(result.scalars().all())

                if not articles:
                    return 0

                # 개별 채점 + DB 업데이트
                updated = 0
                for article in articles:
                    score = self._score_article(
                        article.title,
                        article.description,
                    )
                    await session.execute(
                        update(NewsArticle)
                        .where(NewsArticle.id == article.id)
                        .values(
                            sentiment_score=Decimal(
                                str(round(score.normalized_score, 3)),
                            ),
                            sentiment_label=score.label.value,
                            sentiment_method=SentimentMethod.KEYWORD.value,
                        )
                    )
                    updated += 1

                await session.commit()

                logger.info(
                    "sentiment.keywords_updated",
                    symbol=symbol,
                    updated_count=updated,
                )
                return updated

        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"감성분석 DB 업데이트 실패: {exc}") from exc

    # -- Internal -----------------------------------------------------------

    def _analyze_article_list(
        self,
        articles: list[NewsArticle],
        symbol: str,
    ) -> SentimentResult:
        """기사 목록을 채점하고 종합 결과를 반환."""
        if not articles:
            return SentimentResult(
                symbol=symbol,
                overall_score=Decimal("0.000"),
                overall_label=SentimentLabel.NEUTRAL,
                method=SentimentMethod.KEYWORD,
                needs_llm_analysis=False,
                reasoning="분석 대상 뉴스 기사 없음",
            )

        scores = [self._score_article(a.title, a.description) for a in articles]
        return self._aggregate_scores(scores, symbol)

    def _score_article(self, title: str, description: str | None) -> _ArticleScore:
        """개별 기사 키워드 채점."""
        text = title + " " + (description or "")

        result = _ArticleScore()

        # 긍정 키워드 매칭 (기사당 동일 키워드 1회만)
        for entry in POSITIVE_KEYWORDS:
            if entry.keyword in text:
                result.positive_keywords.append(entry.keyword)
                result.positive_weight_sum += entry.weight

        # 부정 키워드 매칭
        for entry in NEGATIVE_KEYWORDS:
            if entry.keyword in text:
                result.negative_keywords.append(entry.keyword)
                result.negative_weight_sum += entry.weight

        # 중요 키워드 매칭
        for kw in IMPORTANT_KEYWORDS:
            if kw in text:
                result.important_keywords.append(kw)

        result.raw_score = result.positive_weight_sum - result.negative_weight_sum
        result.normalized_score = math.tanh(result.raw_score)

        # 레이블 결정
        if abs(result.normalized_score) < _NEUTRAL_THRESHOLD:
            result.label = SentimentLabel.NEUTRAL
        elif result.normalized_score >= _NEUTRAL_THRESHOLD:
            result.label = SentimentLabel.POSITIVE
        else:
            result.label = SentimentLabel.NEGATIVE

        return result

    def _aggregate_scores(
        self,
        scores: list[_ArticleScore],
        symbol: str,
    ) -> SentimentResult:
        """기사별 점수를 시간 가중 평균으로 종합."""
        # 시간 가중 평균 (인덱스 0=최신, 감쇠율 0.95)
        decay = 0.95
        weighted_sum = 0.0
        weight_total = 0.0

        for i, s in enumerate(scores):
            w = decay**i
            weighted_sum += s.normalized_score * w
            weight_total += w

        overall_float = weighted_sum / weight_total if weight_total > 0 else 0.0
        overall_score = Decimal(str(round(overall_float, 3)))

        # 종합 레이블
        abs_overall = abs(overall_float)
        if abs_overall < _NEUTRAL_THRESHOLD:
            overall_label = SentimentLabel.NEUTRAL
        elif overall_float >= _NEUTRAL_THRESHOLD:
            overall_label = SentimentLabel.POSITIVE
        else:
            overall_label = SentimentLabel.NEGATIVE

        # 카운트
        positive_count = sum(1 for s in scores if s.label == SentimentLabel.POSITIVE)
        negative_count = sum(1 for s in scores if s.label == SentimentLabel.NEGATIVE)
        neutral_count = sum(1 for s in scores if s.label == SentimentLabel.NEUTRAL)

        # key_topics: 중요 키워드 + 빈도 상위 긍정/부정 키워드
        all_important: list[str] = []
        pos_freq: dict[str, int] = {}
        neg_freq: dict[str, int] = {}

        for s in scores:
            all_important.extend(s.important_keywords)
            for kw in s.positive_keywords:
                pos_freq[kw] = pos_freq.get(kw, 0) + 1
            for kw in s.negative_keywords:
                neg_freq[kw] = neg_freq.get(kw, 0) + 1

        important_unique = list(dict.fromkeys(all_important))  # 순서 유지 dedupe
        top_pos = sorted(pos_freq, key=pos_freq.get, reverse=True)[:3]  # type: ignore[arg-type]
        top_neg = sorted(neg_freq, key=neg_freq.get, reverse=True)[:3]  # type: ignore[arg-type]
        key_topics = important_unique + top_pos + top_neg

        # LLM 분석 필요 여부
        needs_llm = self._needs_llm(overall_score, important_unique)

        # reasoning
        reasoning = (
            f"키워드 분석 {len(scores)}건: "
            f"긍정 {positive_count}, 부정 {negative_count}, 중립 {neutral_count}. "
            f"종합 점수 {overall_score}"
        )
        if important_unique:
            reasoning += f". 중요 키워드: {', '.join(important_unique)}"
        if needs_llm:
            reasoning += " → LLM 심층 분석 권장"

        logger.info(
            "sentiment.keyword_analyzed",
            symbol=symbol,
            overall_score=float(overall_score),
            label=overall_label.value,
            total_articles=len(scores),
            needs_llm=needs_llm,
        )

        return SentimentResult(
            symbol=symbol,
            overall_score=overall_score,
            overall_label=overall_label,
            method=SentimentMethod.KEYWORD,
            positive_count=positive_count,
            negative_count=negative_count,
            neutral_count=neutral_count,
            total_articles=len(scores),
            key_topics=key_topics,
            needs_llm_analysis=needs_llm,
            reasoning=reasoning,
        )

    @staticmethod
    def _needs_llm(
        overall_score: Decimal,
        important_matched: list[str],
    ) -> bool:
        """LLM 심층 분석 필요 여부 판단.

        True 조건:
        - 애매 구간: 0.15 <= |score| <= 0.50
        - 중요 키워드 1개 이상 매칭
        """
        abs_score = abs(float(overall_score))
        if important_matched:
            return True
        return _NEUTRAL_THRESHOLD <= abs_score <= _AMBIGUOUS_UPPER
