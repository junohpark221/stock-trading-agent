"""ECOS data provider -- DB persistence + Redis caching over Bank of Korea ECOS API.

Wraps ``aiohttp.ClientSession`` with:
- **sync_*** methods: fetch from ECOS API -> upsert to PostgreSQL
- **fetch_*** methods: cache-through reads (Redis -> DB fallback)

Cache keys:
    ecos:indicators:{indicator_code}:{start}:{end} -- indicator time-series
"""

from __future__ import annotations

from dataclasses import dataclass
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

_ECOS_BASE_URL = "https://ecos.bok.or.kr/api/"
_INDICATORS_CACHE_NS = "ecos:indicators"


@dataclass(frozen=True)
class EcosIndicatorConfig:
    """Configuration for a single ECOS indicator to sync."""

    stat_code: str  # 통계표코드
    item_code: str  # 항목코드
    period: str  # "D" | "M" | "Q"
    name: str  # 한글 지표명


DEFAULT_ECOS_INDICATORS: list[EcosIndicatorConfig] = [
    EcosIndicatorConfig("722Y001", "0101000", "D", "기준금리"),
    EcosIndicatorConfig("731Y003", "0000001", "D", "원/달러 환율"),
    EcosIndicatorConfig("901Y009", "0", "M", "소비자물가지수"),
    EcosIndicatorConfig("200Y002", "10111", "Q", "GDP 성장률"),
    EcosIndicatorConfig("101Y003", "BBGA00", "M", "M2 통화량"),
]


def _parse_ecos_time(time_str: str, period: str) -> date:
    """Parse ECOS TIME field to a date based on the period type.

    - ``D``: ``"20260301"`` -> ``date(2026, 3, 1)``
    - ``M``: ``"202603"`` -> ``date(2026, 3, 1)`` (first day of month)
    - ``Q``: ``"2026Q1"`` -> ``date(2026, 1, 1)`` (first day of quarter)
    """
    if period == "D":
        return date(int(time_str[:4]), int(time_str[4:6]), int(time_str[6:8]))
    if period == "M":
        return date(int(time_str[:4]), int(time_str[4:6]), 1)
    if period == "Q":
        year = int(time_str[:4])
        quarter = int(time_str[5])  # "2026Q1" -> '1'
        month = (quarter - 1) * 3 + 1
        return date(year, month, 1)
    raise ValueError(f"Unknown ECOS period: {period}")


def _format_ecos_date(d: date, period: str) -> str:
    """Format a date for ECOS API path based on period type."""
    if period == "D":
        return d.strftime("%Y%m%d")
    # M and Q both use YYYYMM
    return d.strftime("%Y%m")


class EcosProvider(DataProvider):
    """ECOS data provider with DB persistence and Redis caching.

    Args:
        cache: ``RedisCache`` singleton.
        session_factory: SQLAlchemy async session factory.
        settings: Application settings (ECOS_API_KEY, ECOS_CACHE_TTL).
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
        return "ecos"

    # -- Lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(base_url=_ECOS_BASE_URL)
        logger.info("ecos_provider_initialized")

    async def shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("ecos_provider_shutdown")

    async def health_check(self) -> bool:
        if not self._settings.ECOS_API_KEY:
            return False
        return self._session is not None and not self._session.closed

    # -- Internal: API call ------------------------------------------------

    def _build_path(
        self,
        stat_code: str,
        period: str,
        start_date: str,
        end_date: str,
        item_code: str,
        start_idx: int = 1,
        end_idx: int = 100000,
    ) -> str:
        """Build ECOS REST URL path."""
        api_key = self._settings.ECOS_API_KEY
        return (
            f"StatisticSearch/{api_key}/json/kr/"
            f"{start_idx}/{end_idx}/{stat_code}/{period}/"
            f"{start_date}/{end_date}/{item_code}"
        )

    async def _api_get(self, path: str) -> dict:
        """Make a GET request to ECOS API (path-based, no query params).

        Raises:
            ExternalAPIError: session not initialized, HTTP error, or ECOS error.
        """
        if self._session is None or self._session.closed:
            raise ExternalAPIError("ECOS session not initialized")

        try:
            async with self._session.get(path) as resp:
                if resp.status != 200:
                    raise ExternalAPIError(
                        f"ECOS API HTTP {resp.status} on {path}"
                    )
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ExternalAPIError(f"ECOS API request failed: {exc}") from exc

        # Error response: {"RESULT": {"CODE": "...", "MESSAGE": "..."}}
        if "RESULT" in data:
            code = data["RESULT"].get("CODE", "")
            if code == "INFO-200":
                # No data -- not an error
                return data
            msg = data["RESULT"].get("MESSAGE", "unknown error")
            raise ExternalAPIError(f"ECOS API error [{code}]: {msg}")

        return data

    # -- sync_indicator: API -> DB upsert ----------------------------------

    async def sync_indicator(
        self,
        config: EcosIndicatorConfig,
        start_date: date,
        end_date: date,
    ) -> int:
        """Fetch a single indicator from ECOS API and upsert into DB.

        Returns the number of upserted rows.
        """
        path = self._build_path(
            stat_code=config.stat_code,
            period=config.period,
            start_date=_format_ecos_date(start_date, config.period),
            end_date=_format_ecos_date(end_date, config.period),
            item_code=config.item_code,
        )

        data = await self._api_get(path)

        # Success: {"StatisticSearch": {"list_total_count": N, "row": [...]}}
        stat = data.get("StatisticSearch", {})
        rows = stat.get("row", [])
        if not rows:
            return 0

        indicator_code = f"{config.stat_code}/{config.item_code}"
        values_list = []

        for row in rows:
            raw_value = row.get("DATA_VALUE", "")
            if not raw_value:
                continue
            try:
                value = Decimal(raw_value.replace(",", ""))
            except InvalidOperation:
                continue

            try:
                row_date = _parse_ecos_time(row.get("TIME", ""), config.period)
            except (ValueError, IndexError):
                continue

            values_list.append(
                {
                    "source": DataSourceType.ECOS.value,
                    "indicator_code": indicator_code,
                    "indicator_name": config.name,
                    "date": row_date,
                    "value": value,
                    "unit": row.get("UNIT_NAME"),
                    "raw_data": row,
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
                f"sync_indicator DB error ({indicator_code}): {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        try:
            await self._cache.clear_namespace(
                f"{_INDICATORS_CACHE_NS}:{indicator_code}"
            )
        except CacheError:
            logger.warning(
                "ecos_sync_indicator_cache_invalidate_failed",
                indicator_code=indicator_code,
            )

        logger.info(
            "ecos_sync_indicator_done",
            indicator_code=indicator_code,
            upserted=total_upserted,
        )
        return total_upserted

    async def sync_all(
        self,
        start_date: date,
        end_date: date,
        indicators: list[EcosIndicatorConfig] | None = None,
    ) -> int:
        """Sync all (or given) indicators. Returns total upserted rows."""
        targets = indicators or DEFAULT_ECOS_INDICATORS
        total = 0
        for config in targets:
            total += await self.sync_indicator(config, start_date, end_date)
        return total

    # -- fetch_indicators: cache-through read ------------------------------

    async def fetch_indicators(
        self,
        indicator_code: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[EconomicIndicatorInfo]:
        """Return indicators from cache or DB.

        Flow: Redis -> DB -> cache result.
        """
        cache_key = f"{indicator_code}:{start_date or 'none'}:{end_date or 'none'}"

        # 1. Try cache
        try:
            cached = await self._cache.get_json(_INDICATORS_CACHE_NS, cache_key)
            if cached is not None:
                return [EconomicIndicatorInfo.model_validate(item) for item in cached]
        except CacheError:
            logger.warning(
                "ecos_fetch_indicators_cache_read_failed",
                indicator_code=indicator_code,
            )

        # 2. DB fallback
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(EconomicIndicator)
                    .where(
                        EconomicIndicator.source == DataSourceType.ECOS.value,
                        EconomicIndicator.indicator_code == indicator_code,
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
                f"fetch_indicators DB error ({indicator_code}): {exc}"
            ) from exc

        indicators_list = [
            EconomicIndicatorInfo(
                source=DataSourceType.ECOS,
                indicator_code=row.indicator_code,
                indicator_name=row.indicator_name,
                date=row.date,
                value=row.value,
                unit=row.unit,
            )
            for row in rows
        ]

        # 3. Cache the result (best-effort)
        if indicators_list:
            try:
                await self._cache.set_json(
                    _INDICATORS_CACHE_NS,
                    cache_key,
                    [i.model_dump(mode="json") for i in indicators_list],
                    ttl=self._settings.ECOS_CACHE_TTL,
                )
            except CacheError:
                logger.warning(
                    "ecos_fetch_indicators_cache_write_failed",
                    indicator_code=indicator_code,
                )

        return indicators_list
