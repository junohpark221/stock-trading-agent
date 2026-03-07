"""Naver news data provider -- DB persistence + Redis caching over Naver Search API.

Wraps ``aiohttp.ClientSession`` with:
- **sync_news**: fetch from Naver API -> upsert to PostgreSQL
- **fetch_news**: cache-through reads (Redis -> DB fallback)

Cache keys:
    naver:news:{symbol} -- news articles for a symbol
"""

from __future__ import annotations

import html
import re
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import aiohttp
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.enums import DataSourceType
from src.core.exceptions import CacheError, DatabaseError, ExternalAPIError
from src.core.models import NewsArticleInfo
from src.data.providers.base import DataProvider
from src.db.models.analysis import NewsArticle

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_NAVER_BASE_URL = "https://openapi.naver.com"
_NEWS_CACHE_NS = "naver:news"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape HTML entities."""
    if not text:
        return text
    cleaned = _HTML_TAG_RE.sub("", text)
    return html.unescape(cleaned)


class NaverProvider(DataProvider):
    """Naver news data provider with DB persistence and Redis caching.

    Args:
        cache: ``RedisCache`` singleton.
        session_factory: SQLAlchemy async session factory.
        settings: Application settings (NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NEWS_CACHE_TTL).
    """

    def __init__(
        self,
        *,
        cache: RedisCache,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._cache = cache
        self._session_factory = session_factory
        self._settings = settings
        self._session: aiohttp.ClientSession | None = None

    # -- Identity ----------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "naver"

    # -- Lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(base_url=_NAVER_BASE_URL)
        logger.info("naver_provider_initialized")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("naver_provider_shutdown")

    async def health_check(self) -> bool:
        if not self._settings.NAVER_CLIENT_ID:
            return False
        return self._session is not None and not self._session.closed

    # -- Internal: API call ------------------------------------------------

    async def _api_get(self, path: str, params: dict | None = None) -> dict:
        """Make a GET request to Naver API with client ID/secret headers.

        Raises:
            ExternalAPIError: session not initialized, HTTP error, or API error.
        """
        if self._session is None or self._session.closed:
            raise ExternalAPIError("Naver session not initialized")

        headers = {
            "X-Naver-Client-Id": self._settings.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": self._settings.NAVER_CLIENT_SECRET,
        }

        try:
            async with self._session.get(path, params=params, headers=headers) as resp:
                if resp.status != 200:
                    raise ExternalAPIError(
                        f"Naver API HTTP {resp.status} on {path}"
                    )
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ExternalAPIError(f"Naver API request failed: {exc}") from exc

        # Error response: {"errorMessage": "...", "errorCode": "..."}
        if "errorMessage" in data:
            msg = data.get("errorMessage", "unknown error")
            code = data.get("errorCode", "")
            raise ExternalAPIError(f"Naver API error [{code}]: {msg}")

        return data

    # -- sync_news: API -> DB upsert ---------------------------------------

    async def sync_news(self, symbol: str, query: str, display: int = 100) -> int:
        """Fetch news from Naver API and upsert into DB.

        Args:
            symbol: Stock symbol to tag articles with.
            query: Search query string.
            display: Number of results (max 100).

        Returns the number of upserted rows.
        """
        data = await self._api_get(
            "/v1/search/news.json",
            params={"query": query, "display": display, "sort": "date"},
        )

        items = data.get("items", [])
        if not items:
            return 0

        values_list = []
        for item in items:
            try:
                published_at = parsedate_to_datetime(item["pubDate"])
            except (ValueError, KeyError):
                continue

            values_list.append(
                {
                    "source": DataSourceType.NAVER.value,
                    "symbol": symbol,
                    "title": _strip_html(item.get("title", "")),
                    "description": _strip_html(item.get("description", "")),
                    "link": item.get("originallink") or item.get("link", ""),
                    "published_at": published_at,
                    "sentiment_score": None,
                    "sentiment_label": None,
                    "sentiment_method": None,
                }
            )

        if not values_list:
            return 0

        try:
            async with self._session_factory() as session:
                stmt = pg_insert(NewsArticle).values(values_list)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_news_article_link",
                    set_={
                        "title": stmt.excluded.title,
                        "description": stmt.excluded.description,
                        "symbol": stmt.excluded.symbol,
                        "published_at": stmt.excluded.published_at,
                    },
                )
                result = await session.execute(stmt)
                total_upserted = result.rowcount
                await session.commit()
        except ExternalAPIError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"sync_news DB error ({symbol}): {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        try:
            await self._cache.clear_namespace(f"{_NEWS_CACHE_NS}:{symbol}")
        except CacheError:
            logger.warning(
                "naver_sync_news_cache_invalidate_failed",
                symbol=symbol,
            )

        logger.info(
            "naver_sync_news_done",
            symbol=symbol,
            query=query,
            upserted=total_upserted,
        )
        return total_upserted

    # -- fetch_news: cache-through read ------------------------------------

    async def fetch_news(
        self,
        symbol: str,
        display: int = 100,
        sort: str = "date",
    ) -> list[NewsArticleInfo]:
        """Return news articles from cache or DB.

        Flow: Redis -> DB -> cache result.
        """
        cache_key = symbol

        # 1. Try cache
        try:
            cached = await self._cache.get_json(_NEWS_CACHE_NS, cache_key)
            if cached is not None:
                return [NewsArticleInfo.model_validate(item) for item in cached]
        except CacheError:
            logger.warning(
                "naver_fetch_news_cache_read_failed",
                symbol=symbol,
            )

        # 2. DB fallback
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(NewsArticle)
                    .where(
                        NewsArticle.source == DataSourceType.NAVER.value,
                        NewsArticle.symbol == symbol,
                    )
                    .order_by(NewsArticle.published_at.desc())
                    .limit(display)
                )

                result = await session.execute(stmt)
                rows = result.scalars().all()
        except Exception as exc:
            raise DatabaseError(
                f"fetch_news DB error ({symbol}): {exc}"
            ) from exc

        articles = [
            NewsArticleInfo(
                source=DataSourceType.NAVER,
                symbol=row.symbol,
                title=row.title,
                description=row.description,
                link=row.link,
                published_at=row.published_at,
                sentiment_score=row.sentiment_score,
                sentiment_label=row.sentiment_label,
            )
            for row in rows
        ]

        # 3. Cache the result (best-effort)
        if articles:
            try:
                await self._cache.set_json(
                    _NEWS_CACHE_NS,
                    cache_key,
                    [a.model_dump(mode="json") for a in articles],
                    ttl=self._settings.NEWS_CACHE_TTL,
                )
            except CacheError:
                logger.warning(
                    "naver_fetch_news_cache_write_failed",
                    symbol=symbol,
                )

        return articles
