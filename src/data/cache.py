"""Redis cache wrapper with namespace-based key management.

Provides a thin abstraction over ``redis.asyncio.Redis`` with:

- **Namespace isolation**: all keys prefixed as ``{namespace}:{key}``
- **JSON helpers**: ``get_json`` / ``set_json`` with UTF-8 safe serialization
- **Error mapping**: ``redis.exceptions.RedisError`` → ``CacheError``

Singleton lifecycle follows ``src/db/session.py`` pattern::

    # startup
    init_cache(redis_client)

    # usage
    cache = get_cache()
    await cache.set_json("kis:price", "005930", {"price": 72000}, ttl=10)

    # shutdown
    close_cache()
"""

from __future__ import annotations

import json

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.exceptions import CacheError

logger = structlog.get_logger(__name__)

# ── Singleton ────────────────────────────────────────────────────────

_cache: RedisCache | None = None


def init_cache(client: Redis) -> RedisCache:
    """Initialize the module-level cache singleton.

    Called once during ``lifespan`` startup after ``Redis.from_url()``.
    """
    global _cache
    _cache = RedisCache(client)
    return _cache


def get_cache() -> RedisCache:
    """Return the initialized cache instance.

    Raises:
        RuntimeError: If ``init_cache`` has not been called.
    """
    if _cache is None:
        raise RuntimeError("RedisCache not initialized. Call init_cache() first.")
    return _cache


def close_cache() -> None:
    """Release the singleton reference.

    Redis connection close is handled by the caller (``main.py`` lifespan).
    """
    global _cache
    _cache = None


# ── RedisCache ───────────────────────────────────────────────────────


class RedisCache:
    """Namespace-aware Redis cache with JSON serialization."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    # ── Key helper ───────────────────────────────────────────────────

    @staticmethod
    def _make_key(namespace: str, key: str) -> str:
        """Build a namespaced Redis key → ``{namespace}:{key}``."""
        return f"{namespace}:{key}"

    # ── Basic CRUD ───────────────────────────────────────────────────

    async def get(self, namespace: str, key: str) -> str | None:
        """Get a string value by namespace + key. Returns ``None`` on miss."""
        full_key = self._make_key(namespace, key)
        try:
            value = await self._client.get(full_key)
            logger.debug("cache_get", key=full_key, hit=value is not None)
            return value
        except RedisError as exc:
            raise CacheError(f"cache get failed: {full_key}") from exc

    async def set(
        self, namespace: str, key: str, value: str, *, ttl: int | None = None
    ) -> None:
        """Set a string value with optional TTL (seconds)."""
        full_key = self._make_key(namespace, key)
        try:
            if ttl is not None:
                await self._client.set(full_key, value, ex=ttl)
            else:
                await self._client.set(full_key, value)
            logger.debug("cache_set", key=full_key, ttl=ttl)
        except RedisError as exc:
            raise CacheError(f"cache set failed: {full_key}") from exc

    async def delete(self, namespace: str, key: str) -> bool:
        """Delete a key. Returns ``True`` if the key existed."""
        full_key = self._make_key(namespace, key)
        try:
            result = await self._client.delete(full_key)
            deleted = result > 0
            logger.debug("cache_delete", key=full_key, deleted=deleted)
            return deleted
        except RedisError as exc:
            raise CacheError(f"cache delete failed: {full_key}") from exc

    async def exists(self, namespace: str, key: str) -> bool:
        """Check if a key exists."""
        full_key = self._make_key(namespace, key)
        try:
            result = await self._client.exists(full_key)
            found = result > 0
            logger.debug("cache_exists", key=full_key, found=found)
            return found
        except RedisError as exc:
            raise CacheError(f"cache exists failed: {full_key}") from exc

    # ── JSON CRUD ────────────────────────────────────────────────────

    async def get_json(self, namespace: str, key: str) -> dict | list | None:
        """Get and deserialize a JSON value. Returns ``None`` on miss."""
        raw = await self.get(namespace, key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            full_key = self._make_key(namespace, key)
            raise CacheError(f"cache JSON decode failed: {full_key}") from exc

    async def set_json(
        self,
        namespace: str,
        key: str,
        value: dict | list,
        *,
        ttl: int | None = None,
    ) -> None:
        """Serialize a dict/list to JSON and store it.

        Uses ``ensure_ascii=False`` to preserve Korean stock names,
        and ``default=str`` as a safety net for Decimal/datetime values.
        """
        try:
            raw = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            full_key = self._make_key(namespace, key)
            raise CacheError(f"cache JSON encode failed: {full_key}") from exc
        await self.set(namespace, key, raw, ttl=ttl)

    # ── Namespace management ─────────────────────────────────────────

    async def clear_namespace(self, namespace: str) -> int:
        """Delete all keys under a namespace using SCAN + DELETE.

        Returns the number of deleted keys.
        """
        pattern = f"{namespace}:*"
        deleted = 0
        try:
            cursor: int | bytes = 0
            while True:
                cursor, keys = await self._client.scan(
                    cursor=cursor, match=pattern, count=100
                )
                if keys:
                    deleted += await self._client.delete(*keys)
                if cursor == 0:
                    break
            logger.info("cache_clear_namespace", namespace=namespace, deleted=deleted)
            return deleted
        except RedisError as exc:
            raise CacheError(f"cache clear namespace failed: {namespace}") from exc
