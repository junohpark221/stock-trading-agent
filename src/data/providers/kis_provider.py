"""KIS data provider — DB persistence + Redis caching over KISClient.

Wraps ``KISClient`` (Step 5) with:
- **sync_*** methods: fetch from KIS API → upsert to PostgreSQL (batched)
- **fetch_*** methods: cache-through reads (Redis → DB fallback)

Cache keys:
    kis:master:all   — full stock master list (TTL 24h)
    kis:ohlcv:{sym}  — daily OHLCV per symbol (TTL = KIS_OHLCV_CACHE_TTL)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.exceptions import CacheError, DatabaseError
from src.core.models import OHLCV, StockInfo
from src.data.providers.base import DataProvider
from src.db.models.market_data import DailyOHLCV, StockMaster

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.broker.kis.client import KISClient
    from src.config import Settings
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_UPSERT_BATCH_SIZE = 500
_MASTER_CACHE_NS = "kis:master"
_OHLCV_CACHE_NS = "kis:ohlcv"
_MASTER_CACHE_TTL = 86400  # 24 hours


class KISDataProvider(DataProvider):
    """KIS data provider with DB persistence and Redis caching.

    Args:
        client: Initialized ``KISClient`` instance.
        cache: ``RedisCache`` singleton.
        session_factory: SQLAlchemy async session factory.
        settings: Application settings (cache TTL values).
    """

    def __init__(
        self,
        *,
        client: KISClient,
        cache: RedisCache,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._client = client
        self._cache = cache
        self._session_factory = session_factory
        self._settings = settings

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "kis"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Connect the underlying KISClient."""
        await self._client.connect()
        logger.info("kis_data_provider_initialized")

    async def shutdown(self) -> None:
        """Disconnect the underlying KISClient."""
        await self._client.disconnect()
        logger.info("kis_data_provider_shutdown")

    async def health_check(self) -> bool:
        """Check if the KIS HTTP session is alive (no API call)."""
        session = self._client._session  # noqa: SLF001
        return session is not None and not session.closed

    # ── sync_stock_master: API → DB upsert ────────────────────────────

    async def sync_stock_master(self) -> int:
        """Download KIS stock master and upsert into ``stock_master`` table.

        1. Fetch ~2000 stocks via ``KISClient.get_stock_master()``
        2. Batch upsert (INSERT ... ON CONFLICT DO UPDATE) 500건씩
        3. Soft-delete stocks not in the API result (is_active=False)
        4. Invalidate Redis ``kis:master`` namespace
        """
        stocks = await self._client.get_stock_master()
        if not stocks:
            logger.warning("kis_sync_master_empty")
            return 0

        api_symbols: set[str] = set()
        total_upserted = 0

        try:
            async with self._session_factory() as session:
                # Batch upsert
                for i in range(0, len(stocks), _UPSERT_BATCH_SIZE):
                    batch = stocks[i : i + _UPSERT_BATCH_SIZE]
                    values = [
                        {
                            "symbol": s.symbol,
                            "name": s.name,
                            "market_type": s.market_type.value,
                            "is_active": True,
                        }
                        for s in batch
                    ]
                    api_symbols.update(v["symbol"] for v in values)

                    stmt = pg_insert(StockMaster).values(values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["symbol"],
                        set_={
                            "name": stmt.excluded.name,
                            "market_type": stmt.excluded.market_type,
                            "is_active": stmt.excluded.is_active,
                            "updated_at": func.now(),
                        },
                    )
                    result = await session.execute(stmt)
                    total_upserted += result.rowcount

                # Soft-delete stocks missing from API
                if api_symbols:
                    deactivate_stmt = (
                        update(StockMaster)
                        .where(
                            StockMaster.is_active.is_(True),
                            StockMaster.symbol.not_in(api_symbols),
                        )
                        .values(is_active=False, updated_at=func.now())
                    )
                    deactivated = await session.execute(deactivate_stmt)
                    if deactivated.rowcount:
                        logger.info(
                            "kis_sync_master_deactivated",
                            count=deactivated.rowcount,
                        )

                await session.commit()
        except Exception as exc:
            raise DatabaseError(
                f"sync_stock_master DB error: {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        try:
            await self._cache.clear_namespace(_MASTER_CACHE_NS)
        except CacheError:
            logger.warning("kis_sync_master_cache_invalidate_failed")

        logger.info("kis_sync_master_done", upserted=total_upserted)
        return total_upserted

    # ── sync_daily_ohlcv: API → DB upsert ─────────────────────────────

    async def sync_daily_ohlcv(self, symbol: str, *, period_days: int = 100) -> int:
        """Fetch daily OHLCV from KIS API and upsert into ``daily_ohlcv`` table.

        1. Call ``KISClient.get_daily_ohlcv(symbol, period_days=...)``
        2. Batch upsert (ON CONFLICT on unique constraint) 500건씩
        3. Invalidate Redis ``kis:ohlcv:{symbol}`` cache
        """
        bars = await self._client.get_daily_ohlcv(symbol, period_days=period_days)
        if not bars:
            logger.warning("kis_sync_ohlcv_empty", symbol=symbol)
            return 0

        total_upserted = 0

        try:
            async with self._session_factory() as session:
                for i in range(0, len(bars), _UPSERT_BATCH_SIZE):
                    batch = bars[i : i + _UPSERT_BATCH_SIZE]
                    values = [
                        {
                            "symbol": bar.symbol,
                            "date": bar.date,
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "trading_value": bar.value,
                        }
                        for bar in batch
                    ]

                    stmt = pg_insert(DailyOHLCV).values(values)
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_daily_ohlcv_symbol_date",
                        set_={
                            "open": stmt.excluded.open,
                            "high": stmt.excluded.high,
                            "low": stmt.excluded.low,
                            "close": stmt.excluded.close,
                            "volume": stmt.excluded.volume,
                            "trading_value": stmt.excluded.trading_value,
                            "updated_at": func.now(),
                        },
                    )
                    result = await session.execute(stmt)
                    total_upserted += result.rowcount

                await session.commit()
        except Exception as exc:
            raise DatabaseError(
                f"sync_daily_ohlcv DB error ({symbol}): {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        try:
            await self._cache.delete(_OHLCV_CACHE_NS, symbol)
        except CacheError:
            logger.warning("kis_sync_ohlcv_cache_invalidate_failed", symbol=symbol)

        logger.info("kis_sync_ohlcv_done", symbol=symbol, upserted=total_upserted)
        return total_upserted

    # ── fetch_stock_master: cache-through read ────────────────────────

    async def fetch_stock_master(self) -> list[StockInfo]:
        """Return active stocks from cache or DB.

        Flow: Redis ``kis:master:all`` → DB ``stock_master`` → cache result.
        """
        # 1. Try cache
        try:
            cached = await self._cache.get_json(_MASTER_CACHE_NS, "all")
            if cached is not None:
                return [StockInfo.model_validate(item) for item in cached]
        except CacheError:
            logger.warning("kis_fetch_master_cache_read_failed")

        # 2. DB fallback
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(StockMaster)
                    .where(StockMaster.is_active.is_(True))
                    .order_by(StockMaster.symbol)
                )
                rows = result.scalars().all()
        except Exception as exc:
            raise DatabaseError(f"fetch_stock_master DB error: {exc}") from exc

        stocks = [
            StockInfo(
                symbol=row.symbol,
                name=row.name,
                market_type=row.market_type,
                sector=row.sector or "",
                listed_shares=row.listed_shares or 0,
                market_cap_krw=row.market_cap_krw or 0,
            )
            for row in rows
        ]

        # 3. Cache the result (best-effort)
        if stocks:
            try:
                await self._cache.set_json(
                    _MASTER_CACHE_NS,
                    "all",
                    [s.model_dump(mode="json") for s in stocks],
                    ttl=_MASTER_CACHE_TTL,
                )
            except CacheError:
                logger.warning("kis_fetch_master_cache_write_failed")

        return stocks

    # ── fetch_daily_ohlcv: cache-through read ─────────────────────────

    async def fetch_daily_ohlcv(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[OHLCV]:
        """Return daily OHLCV from cache or DB, filtered by date range.

        Flow: Redis ``kis:ohlcv:{symbol}`` → DB ``daily_ohlcv`` → cache result.
        Date filtering is applied *after* cache/DB fetch.
        """
        ohlcv_ttl = self._settings.KIS_OHLCV_CACHE_TTL
        bars: list[OHLCV] | None = None

        # 1. Try cache
        try:
            cached = await self._cache.get_json(_OHLCV_CACHE_NS, symbol)
            if cached is not None:
                bars = [OHLCV.model_validate(item) for item in cached]
        except CacheError:
            logger.warning("kis_fetch_ohlcv_cache_read_failed", symbol=symbol)

        # 2. DB fallback
        if bars is None:
            try:
                async with self._session_factory() as session:
                    stmt = (
                        select(DailyOHLCV)
                        .where(DailyOHLCV.symbol == symbol)
                        .order_by(DailyOHLCV.date)
                    )
                    result = await session.execute(stmt)
                    rows = result.scalars().all()
            except Exception as exc:
                raise DatabaseError(
                    f"fetch_daily_ohlcv DB error ({symbol}): {exc}"
                ) from exc

            bars = [
                OHLCV(
                    symbol=row.symbol,
                    date=row.date,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    value=row.trading_value or 0,
                )
                for row in rows
            ]

            # Cache the full result (best-effort)
            if bars:
                try:
                    await self._cache.set_json(
                        _OHLCV_CACHE_NS,
                        symbol,
                        [b.model_dump(mode="json") for b in bars],
                        ttl=ohlcv_ttl,
                    )
                except CacheError:
                    logger.warning(
                        "kis_fetch_ohlcv_cache_write_failed", symbol=symbol
                    )

        # 3. Filter by date range
        if start_date or end_date:
            _start = start_date or date.min
            _end = end_date or date.max
            bars = [b for b in bars if _start <= b.date <= _end]

        return bars
