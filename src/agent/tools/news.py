"""News, sentiment, and disclosure tool functions."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select

from src.agent.tools.context import ToolContext
from src.db.models.analysis import Disclosure, NewsArticle

logger = structlog.get_logger(__name__)


async def get_recent_news(
    ctx: ToolContext, symbol: str, limit: int = 20
) -> dict[str, Any]:
    """Fetch recent news articles for *symbol* from DB.

    Returns:
        {symbol, articles: [{title, link, published_at, sentiment_label, sentiment_score}], count}
    """
    try:
        async with ctx.session_factory() as session:
            stmt = (
                select(NewsArticle)
                .where(NewsArticle.symbol == symbol)
                .order_by(NewsArticle.published_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        articles = [
            {
                "title": r.title,
                "link": r.link,
                "published_at": str(r.published_at) if r.published_at else None,
                "sentiment_label": r.sentiment_label,
                "sentiment_score": float(r.sentiment_score) if r.sentiment_score is not None else None,
            }
            for r in rows
        ]

        return {"symbol": symbol, "articles": articles, "count": len(articles)}
    except Exception as exc:
        logger.error("tool.get_recent_news.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_recent_news"}


async def analyze_news_sentiment(
    ctx: ToolContext, symbol: str
) -> dict[str, Any]:
    """Run keyword sentiment analysis on *symbol*'s news articles.

    Returns:
        {symbol, overall_score, overall_label, method, positive_count,
         negative_count, neutral_count, total_articles, key_topics,
         needs_llm_analysis, reasoning}
    """
    try:
        result = await ctx.sentiment_analyzer.analyze(symbol)
        return result.model_dump(mode="json")
    except Exception as exc:
        logger.error("tool.analyze_news_sentiment.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "analyze_news_sentiment"}


async def get_disclosures(
    ctx: ToolContext, symbol: str, limit: int = 10
) -> dict[str, Any]:
    """Fetch recent DART disclosures for *symbol* from DB.

    Returns:
        {symbol, disclosures: [{report_name, receipt_date, filer_name}], count}
    """
    try:
        async with ctx.session_factory() as session:
            stmt = (
                select(Disclosure)
                .where(Disclosure.symbol == symbol)
                .order_by(Disclosure.receipt_date.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        disclosures = [
            {
                "report_name": r.report_name,
                "receipt_date": str(r.receipt_date) if r.receipt_date else None,
                "filer_name": r.filer_name,
            }
            for r in rows
        ]

        return {"symbol": symbol, "disclosures": disclosures, "count": len(disclosures)}
    except Exception as exc:
        logger.error("tool.get_disclosures.error", symbol=symbol, error=str(exc))
        return {"error": str(exc), "symbol": symbol, "tool": "get_disclosures"}
