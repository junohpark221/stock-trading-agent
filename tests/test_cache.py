"""Unit tests for src/data/cache — RedisCache + singleton lifecycle."""

import json
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

import src.data.cache as cache_mod
from src.core.exceptions import CacheError
from src.data.cache import RedisCache

# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_redis():
    """AsyncMock that mimics redis.asyncio.Redis."""
    client = AsyncMock()
    # scan default: return (0, []) → empty namespace
    client.scan = AsyncMock(return_value=(0, []))
    return client


@pytest.fixture
def cache(mock_redis):
    """RedisCache instance backed by mock_redis."""
    return RedisCache(mock_redis)


# ── TestMakeKey ──────────────────────────────────────────────────────


class TestMakeKey:
    def test_basic(self):
        assert RedisCache._make_key("kis:token", "access") == "kis:token:access"

    def test_symbol(self):
        assert RedisCache._make_key("kis:price", "005930") == "kis:price:005930"

    def test_nested_namespace(self):
        assert RedisCache._make_key("kis:ohlcv", "005930") == "kis:ohlcv:005930"


# ── TestGet ──────────────────────────────────────────────────────────


class TestGet:
    async def test_hit(self, cache, mock_redis):
        mock_redis.get = AsyncMock(return_value="hello")

        result = await cache.get("ns", "k")

        assert result == "hello"
        mock_redis.get.assert_awaited_once_with("ns:k")

    async def test_miss(self, cache, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)

        result = await cache.get("ns", "k")

        assert result is None

    async def test_redis_error(self, cache, mock_redis):
        mock_redis.get = AsyncMock(side_effect=RedisError("conn refused"))

        with pytest.raises(CacheError, match="cache get failed"):
            await cache.get("ns", "k")


# ── TestSet ──────────────────────────────────────────────────────────


class TestSet:
    async def test_with_ttl(self, cache, mock_redis):
        mock_redis.set = AsyncMock()

        await cache.set("ns", "k", "v", ttl=60)

        mock_redis.set.assert_awaited_once_with("ns:k", "v", ex=60)

    async def test_without_ttl(self, cache, mock_redis):
        mock_redis.set = AsyncMock()

        await cache.set("ns", "k", "v")

        mock_redis.set.assert_awaited_once_with("ns:k", "v")

    async def test_redis_error(self, cache, mock_redis):
        mock_redis.set = AsyncMock(side_effect=RedisError("conn refused"))

        with pytest.raises(CacheError, match="cache set failed"):
            await cache.set("ns", "k", "v")


# ── TestDelete ───────────────────────────────────────────────────────


class TestDelete:
    async def test_deleted(self, cache, mock_redis):
        mock_redis.delete = AsyncMock(return_value=1)

        result = await cache.delete("ns", "k")

        assert result is True
        mock_redis.delete.assert_awaited_once_with("ns:k")

    async def test_not_found(self, cache, mock_redis):
        mock_redis.delete = AsyncMock(return_value=0)

        result = await cache.delete("ns", "k")

        assert result is False

    async def test_redis_error(self, cache, mock_redis):
        mock_redis.delete = AsyncMock(side_effect=RedisError("conn refused"))

        with pytest.raises(CacheError, match="cache delete failed"):
            await cache.delete("ns", "k")


# ── TestExists ───────────────────────────────────────────────────────


class TestExists:
    async def test_exists(self, cache, mock_redis):
        mock_redis.exists = AsyncMock(return_value=1)

        result = await cache.exists("ns", "k")

        assert result is True

    async def test_not_exists(self, cache, mock_redis):
        mock_redis.exists = AsyncMock(return_value=0)

        result = await cache.exists("ns", "k")

        assert result is False

    async def test_redis_error(self, cache, mock_redis):
        mock_redis.exists = AsyncMock(side_effect=RedisError("conn refused"))

        with pytest.raises(CacheError, match="cache exists failed"):
            await cache.exists("ns", "k")


# ── TestGetJson ──────────────────────────────────────────────────────


class TestGetJson:
    async def test_dict(self, cache, mock_redis):
        mock_redis.get = AsyncMock(return_value='{"price": 72000}')

        result = await cache.get_json("ns", "k")

        assert result == {"price": 72000}

    async def test_list(self, cache, mock_redis):
        mock_redis.get = AsyncMock(return_value='[1, 2, 3]')

        result = await cache.get_json("ns", "k")

        assert result == [1, 2, 3]

    async def test_miss(self, cache, mock_redis):
        mock_redis.get = AsyncMock(return_value=None)

        result = await cache.get_json("ns", "k")

        assert result is None

    async def test_invalid_json(self, cache, mock_redis):
        mock_redis.get = AsyncMock(return_value="not-json{")

        with pytest.raises(CacheError, match="JSON decode failed"):
            await cache.get_json("ns", "k")


# ── TestSetJson ──────────────────────────────────────────────────────


class TestSetJson:
    async def test_dict(self, cache, mock_redis):
        mock_redis.set = AsyncMock()

        await cache.set_json("ns", "k", {"a": 1}, ttl=10)

        call_args = mock_redis.set.call_args
        stored = json.loads(call_args[0][1])
        assert stored == {"a": 1}
        assert call_args.kwargs == {"ex": 10}

    async def test_list(self, cache, mock_redis):
        mock_redis.set = AsyncMock()

        await cache.set_json("ns", "k", [1, 2])

        call_args = mock_redis.set.call_args
        stored = json.loads(call_args[0][1])
        assert stored == [1, 2]

    async def test_korean_preserved(self, cache, mock_redis):
        mock_redis.set = AsyncMock()

        await cache.set_json("ns", "k", {"name": "삼성전자"})

        call_args = mock_redis.set.call_args
        raw = call_args[0][1]
        # ensure_ascii=False → 한글이 그대로 포함
        assert "삼성전자" in raw
        stored = json.loads(raw)
        assert stored["name"] == "삼성전자"


# ── TestClearNamespace ───────────────────────────────────────────────


class TestClearNamespace:
    async def test_keys_deleted(self, cache, mock_redis):
        mock_redis.scan = AsyncMock(return_value=(0, ["ns:a", "ns:b"]))
        mock_redis.delete = AsyncMock(return_value=2)

        result = await cache.clear_namespace("ns")

        assert result == 2
        mock_redis.scan.assert_awaited_once()
        mock_redis.delete.assert_awaited_once_with("ns:a", "ns:b")

    async def test_empty_namespace(self, cache, mock_redis):
        mock_redis.scan = AsyncMock(return_value=(0, []))

        result = await cache.clear_namespace("ns")

        assert result == 0
        mock_redis.delete.assert_not_awaited()

    async def test_multi_page_scan(self, cache, mock_redis):
        """SCAN이 여러 페이지에 걸쳐 키를 반환하는 경우."""
        mock_redis.scan = AsyncMock(
            side_effect=[
                (42, ["ns:a", "ns:b"]),  # 첫 페이지, cursor=42
                (0, ["ns:c"]),  # 마지막 페이지, cursor=0
            ]
        )
        mock_redis.delete = AsyncMock(side_effect=[2, 1])

        result = await cache.clear_namespace("ns")

        assert result == 3
        assert mock_redis.scan.await_count == 2
        assert mock_redis.delete.await_count == 2

    async def test_redis_error(self, cache, mock_redis):
        mock_redis.scan = AsyncMock(side_effect=RedisError("conn refused"))

        with pytest.raises(CacheError, match="cache clear namespace failed"):
            await cache.clear_namespace("ns")


# ── TestModuleSingleton ──────────────────────────────────────────────


class TestModuleSingleton:
    def setup_method(self):
        """각 테스트 전 싱글톤 초기화."""
        cache_mod._cache = None

    def teardown_method(self):
        """각 테스트 후 싱글톤 정리."""
        cache_mod._cache = None

    def test_init_and_get(self):
        client = AsyncMock()
        result = cache_mod.init_cache(client)

        assert isinstance(result, RedisCache)
        assert cache_mod.get_cache() is result

    def test_get_before_init(self):
        with pytest.raises(RuntimeError, match="RedisCache not initialized"):
            cache_mod.get_cache()

    def test_close_and_reinit(self):
        client = AsyncMock()
        cache_mod.init_cache(client)
        cache_mod.close_cache()

        with pytest.raises(RuntimeError, match="RedisCache not initialized"):
            cache_mod.get_cache()

        # 재초기화 가능
        new_client = AsyncMock()
        result = cache_mod.init_cache(new_client)
        assert cache_mod.get_cache() is result
