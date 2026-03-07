"""FRED data provider -- DB persistence + Redis caching over FRED API.

Wraps ``aiohttp.ClientSession`` with:
- **sync_*** methods: fetch from FRED API -> upsert to PostgreSQL
- **fetch_*** methods: cache-through reads (Redis -> DB fallback)

Cache keys:
    fred:series:{series_id}:{start}:{end} -- series observations
    fred:series_info:{series_id} -- series metadata (title, units)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import aiohttp
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.enums import DataSourceType
from src.core.exceptions import CacheError, DatabaseError, ExternalAPIError
from src.core.models import EconomicIndicatorInfo
from src.data.providers.base import DataProvider
from src.db.models.analysis import EconomicIndicator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_FRED_BASE_URL = "https://api.stlouisfed.org/"
_SERIES_CACHE_NS = "fred:series"
_SERIES_INFO_CACHE_NS = "fred:series_info"
_SERIES_INFO_TTL = 604800  # 7 days

DEFAULT_FRED_SERIES: list[str] = ["FEDFUNDS", "CPIAUCSL", "UNRATE", "GS10", "VIXCLS"]


class FredProvider(DataProvider):
    """FRED data provider with DB persistence and Redis caching.

    Args:
        cache: ``RedisCache`` singleton.
        session_factory: SQLAlchemy async session factory.
        settings: Application settings (FRED_API_KEY, FRED_CACHE_TTL).
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
        return "fred"

    # -- Lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(base_url=_FRED_BASE_URL)
        logger.info("fred_provider_initialized")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("fred_provider_shutdown")

    async def health_check(self) -> bool:
        if not self._settings.FRED_API_KEY:
            return False
        return self._session is not None and not self._session.closed

    # -- Internal: API call ------------------------------------------------

    async def _api_get(self, path: str, params: dict) -> dict:
        """Make a GET request to FRED API with automatic api_key injection.

        Raises:
            ExternalAPIError: session not initialized, HTTP error, or FRED error.
        """
        if self._session is None or self._session.closed:
            raise ExternalAPIError("FRED session not initialized")

        params["api_key"] = self._settings.FRED_API_KEY
        params["file_type"] = "json"

        try:
            async with self._session.get(path, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ExternalAPIError(
                        f"FRED API HTTP {resp.status} on {path}: {body}"
                    )
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ExternalAPIError(f"FRED API request failed: {exc}") from exc

        # FRED error: {"error_message": "..."}
        if "error_message" in data:
            raise ExternalAPIError(
                f"FRED API error: {data['error_message']}"
            )

        return data

    # -- Series info (title, units) ----------------------------------------

    async def _fetch_series_info(self, series_id: str) -> tuple[str, str | None]:
        """Return ``(title, units)`` for a FRED series.

        Uses Redis cache (7-day TTL). Falls back to ``(series_id, None)`` on failure.
        """
        # 1. Try cache
        try:
            cached = await self._cache.get_json(_SERIES_INFO_CACHE_NS, series_id)
            if cached is not None:
                return cached.get("title", series_id), cached.get("units")
        except CacheError:
            logger.warning("fred_series_info_cache_read_failed", series_id=series_id)

        # 2. API call
        try:
            data = await self._api_get(
                "fred/series", {"series_id": series_id}
            )
            seriess = data.get("seriess", [])
            if seriess:
                info = seriess[0]
                title = info.get("title", series_id)
                units = info.get("units")

                # Cache (best-effort)
                try:
                    await self._cache.set_json(
                        _SERIES_INFO_CACHE_NS,
                        series_id,
                        {"title": title, "units": units},
                        ttl=_SERIES_INFO_TTL,
                    )
                except CacheError:
                    pass

                return title, units
        except ExternalAPIError:
            logger.warning("fred_series_info_api_failed", series_id=series_id)

        return series_id, None

    # -- sync_series: API -> DB upsert -------------------------------------

    async def sync_series(
        self,
        series_id: str,
        start_date: date,
        end_date: date,
    ) -> int:
        """Fetch observations for a FRED series and upsert into DB.

        Returns the number of upserted rows.
        """
        data = await self._api_get(
            "fred/series/observations",
            {
                "series_id": series_id,
                "observation_start": start_date.isoformat(),
                "observation_end": end_date.isoformat(),
            },
        )

        observations = data.get("observations", [])
        if not observations:
            return 0

        title, units = await self._fetch_series_info(series_id)

        values_list = []
        for obs in observations:
            raw_value = obs.get("value", ".")
            if raw_value == ".":
                continue
            try:
                value = Decimal(raw_value)
            except InvalidOperation:
                continue

            try:
                obs_date = date.fromisoformat(obs["date"])
            except (ValueError, KeyError):
                continue

            values_list.append(
                {
                    "source": DataSourceType.FRED.value,
                    "indicator_code": series_id,
                    "indicator_name": title,
                    "date": obs_date,
                    "value": value,
                    "unit": units,
                    "raw_data": obs,
                }
            )

        if not values_list:
            return 0

        total_upserted = 0

        try:
            async with self._session_factory() as session:
                stmt = pg_insert(EconomicIndicator).values(values_list)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_economic_indicator",
                    set_={
                        "indicator_name": stmt.excluded.indicator_name,
                        "value": stmt.excluded.value,
                        "unit": stmt.excluded.unit,
                        "raw_data": stmt.excluded.raw_data,
                    },
                )
                result = await session.execute(stmt)
                total_upserted = result.rowcount
                await session.commit()
        except ExternalAPIError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"sync_series DB error ({series_id}): {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        try:
            await self._cache.clear_namespace(f"{_SERIES_CACHE_NS}:{series_id}")
        except CacheError:
            logger.warning(
                "fred_sync_series_cache_invalidate_failed", series_id=series_id
            )

        logger.info(
            "fred_sync_series_done", series_id=series_id, upserted=total_upserted
        )
        return total_upserted

    async def sync_all(
        self,
        start_date: date,
        end_date: date,
        series_ids: list[str] | None = None,
    ) -> int:
        """Sync all (or given) FRED series. Returns total upserted rows."""
        targets = series_ids or DEFAULT_FRED_SERIES
        total = 0
        for series_id in targets:
            total += await self.sync_series(series_id, start_date, end_date)
        return total

    # -- fetch_series: cache-through read ----------------------------------

    async def fetch_series(
        self,
        series_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[EconomicIndicatorInfo]:
        """Return series data from cache or DB.

        Flow: Redis -> DB -> cache result.
        """
        cache_key = f"{series_id}:{start_date or 'none'}:{end_date or 'none'}"

        # 1. Try cache
        try:
            cached = await self._cache.get_json(_SERIES_CACHE_NS, cache_key)
            if cached is not None:
                return [EconomicIndicatorInfo.model_validate(item) for item in cached]
        except CacheError:
            logger.warning(
                "fred_fetch_series_cache_read_failed", series_id=series_id
            )

        # 2. DB fallback
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(EconomicIndicator)
                    .where(
                        EconomicIndicator.source == DataSourceType.FRED.value,
                        EconomicIndicator.indicator_code == series_id,
                    )
                    .order_by(EconomicIndicator.date.desc())
                )
                if start_date:
                    stmt = stmt.where(EconomicIndicator.date >= start_date)
                if end_date:
                    stmt = stmt.where(EconomicIndicator.date <= end_date)

                result = await session.execute(stmt)
                rows = result.scalars().all()
        except Exception as exc:
            raise DatabaseError(
                f"fetch_series DB error ({series_id}): {exc}"
            ) from exc

        series_list = [
            EconomicIndicatorInfo(
                source=DataSourceType.FRED,
                indicator_code=row.indicator_code,
                indicator_name=row.indicator_name,
                date=row.date,
                value=row.value,
                unit=row.unit,
            )
            for row in rows
        ]

        # 3. Cache the result (best-effort)
        if series_list:
            try:
                await self._cache.set_json(
                    _SERIES_CACHE_NS,
                    cache_key,
                    [i.model_dump(mode="json") for i in series_list],
                    ttl=self._settings.FRED_CACHE_TTL,
                )
            except CacheError:
                logger.warning(
                    "fred_fetch_series_cache_write_failed", series_id=series_id
                )

        return series_list
