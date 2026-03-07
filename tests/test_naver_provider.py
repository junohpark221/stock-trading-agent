"""Unit tests for NaverProvider.

All Naver API, DB, and Redis calls are mocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import AsyncContextManagerMock, make_settings, mock_aiohttp_response

from src.core.enums import DataSourceType
from src.core.exceptions import CacheError, DatabaseError, ExternalAPIError
from src.data.cache import RedisCache
from src.data.providers.naver_provider import NaverProvider, _strip_html

# -- Helpers ---------------------------------------------------------------


def _mock_session_factory(session=None):
    """Create a mock async session factory (async with pattern)."""
    mock_session = session or AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return factory, mock_session


def _make_naver_provider(cache=None, session_factory=None, settings=None):
    if session_factory is None:
        sf, _ = _mock_session_factory()
    else:
        sf = session_factory
    return NaverProvider(
        cache=cache or AsyncMock(spec=RedisCache),
        session_factory=sf,
        settings=settings
        or make_settings(
            NAVER_CLIENT_ID="test_client_id",
            NAVER_CLIENT_SECRET="test_client_secret",
        ),
    )


def _make_db_result(rowcount=0, scalars_all=None):
    result = MagicMock()
    result.rowcount = rowcount
    result.scalars = MagicMock()
    result.scalars.return_value.all.return_value = scalars_all or []
    return result


def _make_news_row(**kwargs):
    """NewsArticle ORM row mock."""
    row = MagicMock()
    row.source = kwargs.get("source", "naver")
    row.symbol = kwargs.get("symbol", "005930")
    row.title = kwargs.get("title", "삼성전자 뉴스")
    row.description = kwargs.get("description", "삼성전자 관련 뉴스입니다.")
    row.link = kwargs.get("link", "https://news.example.com/1")
    row.published_at = kwargs.get(
        "published_at", datetime(2026, 3, 7, 9, 30, 0, tzinfo=timezone.utc)
    )
    row.sentiment_score = kwargs.get("sentiment_score", None)
    row.sentiment_label = kwargs.get("sentiment_label", None)
    return row


def _setup_provider_with_aiohttp(provider, json_data, status=200):
    resp = mock_aiohttp_response(status=status, json_data=json_data)
    mock_http_session = MagicMock()
    mock_http_session.closed = False
    mock_http_session.get = MagicMock(return_value=AsyncContextManagerMock(resp))
    mock_http_session.close = AsyncMock()
    provider._session = mock_http_session
    return mock_http_session


_SAMPLE_NAVER_RESPONSE = {
    "lastBuildDate": "Sat, 07 Mar 2026 10:00:00 +0900",
    "total": 2,
    "start": 1,
    "display": 2,
    "items": [
        {
            "title": "<b>삼성전자</b> 주가 상승",
            "originallink": "https://news.example.com/1",
            "link": "https://news.naver.com/1",
            "description": "<b>삼성전자</b>가 &quot;실적&quot; 호조로 주가 상승",
            "pubDate": "Fri, 07 Mar 2026 09:30:00 +0900",
        },
        {
            "title": "<b>삼성전자</b> 반도체 투자",
            "originallink": "https://news.example.com/2",
            "link": "https://news.naver.com/2",
            "description": "<b>삼성전자</b> 반도체 신규 투자 발표",
            "pubDate": "Fri, 07 Mar 2026 08:00:00 +0900",
        },
    ],
}


# =========================================================================
# Lifecycle Tests
# =========================================================================


class TestNaverProviderLifecycle:
    def test_provider_name(self):
        p = _make_naver_provider()
        assert p.provider_name == "naver"

    @pytest.mark.asyncio
    async def test_initialize_creates_session(self):
        p = _make_naver_provider()
        await p.initialize()
        assert p._session is not None
        await p.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_closes_session(self):
        p = _make_naver_provider()
        await p.initialize()
        await p.shutdown()
        assert p._session is None

    @pytest.mark.asyncio
    async def test_health_check_true(self):
        p = _make_naver_provider()
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_false_no_key(self):
        p = _make_naver_provider(settings=make_settings(NAVER_CLIENT_ID=""))
        mock_session = MagicMock()
        mock_session.closed = False
        p._session = mock_session
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_false_no_session(self):
        p = _make_naver_provider()
        assert await p.health_check() is False


# =========================================================================
# API Get Tests
# =========================================================================


class TestNaverApiGet:
    @pytest.mark.asyncio
    async def test_success(self):
        p = _make_naver_provider()
        _setup_provider_with_aiohttp(p, _SAMPLE_NAVER_RESPONSE)
        result = await p._api_get("/v1/search/news.json", {"query": "삼성전자"})
        assert "items" in result

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        p = _make_naver_provider()
        _setup_provider_with_aiohttp(p, {}, status=429)
        with pytest.raises(ExternalAPIError, match="HTTP 429"):
            await p._api_get("/v1/search/news.json", {"query": "삼성전자"})

    @pytest.mark.asyncio
    async def test_session_not_initialized_raises(self):
        p = _make_naver_provider()
        with pytest.raises(ExternalAPIError, match="not initialized"):
            await p._api_get("/v1/search/news.json", {"query": "삼성전자"})


# =========================================================================
# HTML Strip Tests
# =========================================================================


class TestNaverStripHtml:
    def test_bold_tags_removed(self):
        assert _strip_html("<b>삼성전자</b> 주가") == "삼성전자 주가"

    def test_html_entities_unescaped(self):
        assert _strip_html("&quot;실적&quot; 호조") == '"실적" 호조'

    def test_mixed_tags_and_entities(self):
        result = _strip_html("<b>삼성전자</b>가 &quot;실적&quot; 호조로 주가 상승")
        assert result == '삼성전자가 "실적" 호조로 주가 상승'

    def test_empty_string(self):
        assert _strip_html("") == ""


# =========================================================================
# Sync News Tests
# =========================================================================


class TestNaverSyncNews:
    @pytest.mark.asyncio
    async def test_normal_upsert(self):
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=2))
        session.commit = AsyncMock()

        p = _make_naver_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, _SAMPLE_NAVER_RESPONSE)

        result = await p.sync_news("005930", "삼성전자")
        assert result == 2
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_response_returns_zero(self):
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()

        p = _make_naver_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {"items": []})

        result = await p.sync_news("005930", "삼성전자")
        assert result == 0

    @pytest.mark.asyncio
    async def test_api_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        p = _make_naver_provider(cache=cache)
        _setup_provider_with_aiohttp(
            p, {"errorMessage": "Invalid display value", "errorCode": "SE02"}
        )

        with pytest.raises(ExternalAPIError, match="SE02"):
            await p.sync_news("005930", "삼성전자")

    @pytest.mark.asyncio
    async def test_db_error_raises(self):
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(side_effect=Exception("db error"))

        p = _make_naver_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, _SAMPLE_NAVER_RESPONSE)

        with pytest.raises(DatabaseError):
            await p.sync_news("005930", "삼성전자")

    @pytest.mark.asyncio
    async def test_html_stripped_before_save(self, monkeypatch):
        """Verify that HTML tags are stripped from title/description before DB insert."""
        cache = AsyncMock(spec=RedisCache)
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(rowcount=1))
        session.commit = AsyncMock()

        # Capture values passed to pg_insert().values()
        captured_values = []
        original_pg_insert = __import__(
            "sqlalchemy.dialects.postgresql", fromlist=["insert"]
        ).insert

        def patched_pg_insert(table):
            insert_obj = original_pg_insert(table)
            original_values = insert_obj.values

            def capture_values(values_list):
                captured_values.extend(values_list)
                return original_values(values_list)

            insert_obj.values = capture_values
            return insert_obj

        monkeypatch.setattr(
            "src.data.providers.naver_provider.pg_insert", patched_pg_insert
        )

        p = _make_naver_provider(cache=cache, session_factory=sf)
        _setup_provider_with_aiohttp(p, {
            "items": [
                {
                    "title": "<b>테스트</b>",
                    "originallink": "https://example.com/1",
                    "link": "https://naver.com/1",
                    "description": "&quot;설명&quot;",
                    "pubDate": "Fri, 07 Mar 2026 09:00:00 +0900",
                }
            ]
        })

        await p.sync_news("005930", "삼성전자")

        assert len(captured_values) == 1
        assert captured_values[0]["title"] == "테스트"
        assert captured_values[0]["description"] == '"설명"'


# =========================================================================
# Fetch News Tests
# =========================================================================


class TestNaverFetchNews:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(
            return_value=[
                {
                    "source": "naver",
                    "symbol": "005930",
                    "title": "삼성전자 뉴스",
                    "description": "설명",
                    "link": "https://example.com/1",
                    "published_at": "2026-03-07T09:30:00+00:00",
                    "sentiment_score": None,
                    "sentiment_label": None,
                },
            ]
        )
        sf, session = _mock_session_factory()
        p = _make_naver_provider(cache=cache, session_factory=sf)

        result = await p.fetch_news("005930")
        assert len(result) == 1
        assert result[0].source == DataSourceType.NAVER
        assert result[0].title == "삼성전자 뉴스"
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_miss_db_fallback(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        row = _make_news_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_naver_provider(cache=cache, session_factory=sf)
        result = await p.fetch_news("005930")
        assert len(result) == 1
        cache.set_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_result(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(return_value=None)
        cache.set_json = AsyncMock()

        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[]))

        p = _make_naver_provider(cache=cache, session_factory=sf)
        result = await p.fetch_news("005930")
        assert result == []
        cache.set_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_error_graceful(self):
        cache = AsyncMock(spec=RedisCache)
        cache.get_json = AsyncMock(side_effect=CacheError("redis down"))
        cache.set_json = AsyncMock()

        row = _make_news_row()
        sf, session = _mock_session_factory()
        session.execute = AsyncMock(return_value=_make_db_result(scalars_all=[row]))

        p = _make_naver_provider(cache=cache, session_factory=sf)
        result = await p.fetch_news("005930")
        assert len(result) == 1
