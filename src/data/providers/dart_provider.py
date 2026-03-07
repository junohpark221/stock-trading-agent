"""DART data provider — DB persistence + Redis caching over DART Open API.

Wraps ``aiohttp.ClientSession`` with:
- **sync_*** methods: fetch from DART API -> upsert to PostgreSQL
- **fetch_*** methods: cache-through reads (Redis -> DB fallback)

Cache keys:
    dart:corp_code:{symbol}  — symbol->corp_code mapping (TTL 7 days)
    dart:disclosures:{symbol}:{start}:{end} — disclosure list (TTL = DART_CACHE_TTL)
    dart:financials:{symbol}:{year}:{report_type} — financial statement (TTL = DART_CACHE_TTL)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import aiohttp
import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.enums import ReportType
from src.core.exceptions import CacheError, DatabaseError, ExternalAPIError
from src.core.models import DisclosureInfo, FinancialStatementInfo
from src.data.providers.base import DataProvider
from src.db.models.analysis import Disclosure, FinancialStatement

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_DART_BASE_URL = "https://opendart.fss.or.kr/api/"
_CORP_CODE_CACHE_NS = "dart:corp_code"
_DISCLOSURES_CACHE_NS = "dart:disclosures"
_FINANCIALS_CACHE_NS = "dart:financials"
_CORP_CODE_TTL = 604800  # 7 days

# report_type -> DART reprt_code mapping
_REPORT_CODE_MAP: dict[ReportType, str] = {
    ReportType.ANNUAL: "11011",
    ReportType.SEMI_ANNUAL: "11012",
    ReportType.QUARTERLY: "11013",
}


class DartProvider(DataProvider):
    """DART data provider with DB persistence and Redis caching.

    Args:
        cache: ``RedisCache`` singleton.
        session_factory: SQLAlchemy async session factory.
        settings: Application settings (DART_API_KEY, DART_CACHE_TTL).
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
        return "dart"

    # -- Lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Create aiohttp session for DART API calls."""
        self._session = aiohttp.ClientSession(base_url=_DART_BASE_URL)
        logger.info("dart_provider_initialized")

    async def shutdown(self) -> None:
        """Close aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("dart_provider_shutdown")

    async def health_check(self) -> bool:
        """Check if session is alive and API key is configured."""
        if not self._settings.DART_API_KEY:
            return False
        return self._session is not None and not self._session.closed

    # -- Internal: API call ------------------------------------------------

    async def _api_get(self, path: str, params: dict) -> dict:
        """Make a GET request to DART API and return JSON response.

        Raises:
            ExternalAPIError: If session is not initialized, HTTP error, or
                DART status != "000".
        """
        if self._session is None or self._session.closed:
            raise ExternalAPIError("DART session not initialized")

        params["crtfc_key"] = self._settings.DART_API_KEY

        try:
            async with self._session.get(path, params=params) as resp:
                if resp.status != 200:
                    raise ExternalAPIError(
                        f"DART API HTTP {resp.status} on {path}"
                    )
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ExternalAPIError(f"DART API request failed: {exc}") from exc

        status = data.get("status", "")
        # "000" = success, "013" = no data
        if status not in ("000", "013"):
            msg = data.get("message", "unknown error")
            raise ExternalAPIError(f"DART API error [{status}]: {msg}")

        return data

    # -- corp_code resolution ----------------------------------------------

    async def _resolve_corp_code(self, symbol: str) -> str | None:
        """Resolve stock symbol to DART corp_code.

        Flow: Redis cache -> DART company.json API -> cache result.
        Returns None if the symbol is not found.
        """
        # 1. Try cache
        try:
            cached = await self._cache.get(_CORP_CODE_CACHE_NS, symbol)
            if cached is not None:
                return cached
        except CacheError:
            logger.warning("dart_corp_code_cache_read_failed", symbol=symbol)

        # 2. API call
        try:
            data = await self._api_get("/company.json", {"stock_code": symbol})
        except ExternalAPIError:
            logger.warning("dart_corp_code_api_failed", symbol=symbol)
            return None

        corp_code = data.get("corp_code")
        if not corp_code or data.get("status") == "013":
            return None

        # 3. Cache the result (best-effort)
        try:
            await self._cache.set(
                _CORP_CODE_CACHE_NS, symbol, corp_code, ttl=_CORP_CODE_TTL
            )
        except CacheError:
            logger.warning("dart_corp_code_cache_write_failed", symbol=symbol)

        return corp_code

    # -- sync_disclosures: API -> DB upsert --------------------------------

    async def sync_disclosures(
        self,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        """Fetch disclosures from DART API and upsert into DB.

        Returns the number of upserted rows.
        """
        corp_code = await self._resolve_corp_code(symbol)
        if not corp_code:
            logger.warning("dart_sync_disclosures_no_corp_code", symbol=symbol)
            return 0

        params: dict[str, str | int] = {
            "corp_code": corp_code,
            "page_count": 100,
        }
        if start_date:
            params["bgn_de"] = start_date.strftime("%Y%m%d")
        if end_date:
            params["end_de"] = end_date.strftime("%Y%m%d")

        data = await self._api_get("/list.json", params)

        items = data.get("list", [])
        if not items:
            return 0

        total_upserted = 0

        try:
            async with self._session_factory() as session:
                values = [
                    {
                        "corp_code": corp_code,
                        "symbol": symbol,
                        "report_name": item.get("report_nm", ""),
                        "receipt_no": item["rcept_no"],
                        "receipt_date": datetime.strptime(
                            item["rcept_dt"], "%Y%m%d"
                        ).date(),
                        "filer_name": item.get("flr_nm"),
                        "raw_data": item,
                    }
                    for item in items
                    if item.get("rcept_no")
                ]

                if values:
                    stmt = pg_insert(Disclosure).values(values)
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_disclosure_receipt_no",
                        set_={
                            "report_name": stmt.excluded.report_name,
                            "filer_name": stmt.excluded.filer_name,
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
                f"sync_disclosures DB error ({symbol}): {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        try:
            await self._cache.clear_namespace(f"{_DISCLOSURES_CACHE_NS}:{symbol}")
        except CacheError:
            logger.warning("dart_sync_disclosures_cache_invalidate_failed", symbol=symbol)

        logger.info("dart_sync_disclosures_done", symbol=symbol, upserted=total_upserted)
        return total_upserted

    # -- sync_financials: API -> DB upsert ---------------------------------

    async def sync_financials(
        self,
        symbol: str,
        fiscal_year: int,
        report_type: ReportType,
    ) -> int:
        """Fetch financial statement from DART API and upsert into DB.

        Returns the number of upserted rows.
        """
        corp_code = await self._resolve_corp_code(symbol)
        if not corp_code:
            logger.warning("dart_sync_financials_no_corp_code", symbol=symbol)
            return 0

        reprt_code = _REPORT_CODE_MAP[report_type]
        data = await self._api_get(
            "/fnlttSinglAcnt.json",
            {
                "corp_code": corp_code,
                "bsns_year": str(fiscal_year),
                "reprt_code": reprt_code,
            },
        )

        items = data.get("list", [])
        if not items:
            return 0

        # DART returns multiple account items; aggregate into one row
        financials = _parse_financial_items(items)

        # Determine fiscal_quarter from report_type
        fiscal_quarter: int | None = None
        if report_type == ReportType.QUARTERLY:
            fiscal_quarter = 1
        elif report_type == ReportType.SEMI_ANNUAL:
            fiscal_quarter = 2

        total_upserted = 0

        try:
            async with self._session_factory() as session:
                values = {
                    "symbol": symbol,
                    "corp_code": corp_code,
                    "report_type": report_type.value,
                    "fiscal_year": fiscal_year,
                    "fiscal_quarter": fiscal_quarter,
                    "revenue": financials.get("revenue"),
                    "operating_income": financials.get("operating_income"),
                    "net_income": financials.get("net_income"),
                    "total_assets": financials.get("total_assets"),
                    "total_equity": financials.get("total_equity"),
                    "total_liabilities": financials.get("total_liabilities"),
                    "raw_data": items,
                }

                stmt = pg_insert(FinancialStatement).values([values])
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_financial_statement",
                    set_={
                        "revenue": stmt.excluded.revenue,
                        "operating_income": stmt.excluded.operating_income,
                        "net_income": stmt.excluded.net_income,
                        "total_assets": stmt.excluded.total_assets,
                        "total_equity": stmt.excluded.total_equity,
                        "total_liabilities": stmt.excluded.total_liabilities,
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
                f"sync_financials DB error ({symbol}): {exc}"
            ) from exc

        # Invalidate cache (best-effort)
        cache_key = f"{symbol}:{fiscal_year}:{report_type.value}"
        try:
            await self._cache.delete(_FINANCIALS_CACHE_NS, cache_key)
        except CacheError:
            logger.warning("dart_sync_financials_cache_invalidate_failed", symbol=symbol)

        logger.info(
            "dart_sync_financials_done",
            symbol=symbol,
            fiscal_year=fiscal_year,
            report_type=report_type.value,
            upserted=total_upserted,
        )
        return total_upserted

    # -- fetch_disclosures: cache-through read -----------------------------

    async def fetch_disclosures(
        self,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[DisclosureInfo]:
        """Return disclosures from cache or DB.

        Flow: Redis -> DB -> cache result.
        """
        cache_key = f"{symbol}:{start_date or 'none'}:{end_date or 'none'}"

        # 1. Try cache
        try:
            cached = await self._cache.get_json(_DISCLOSURES_CACHE_NS, cache_key)
            if cached is not None:
                return [DisclosureInfo.model_validate(item) for item in cached]
        except CacheError:
            logger.warning("dart_fetch_disclosures_cache_read_failed", symbol=symbol)

        # 2. DB fallback
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(Disclosure)
                    .where(Disclosure.symbol == symbol)
                    .order_by(Disclosure.receipt_date.desc())
                )
                if start_date:
                    stmt = stmt.where(Disclosure.receipt_date >= start_date)
                if end_date:
                    stmt = stmt.where(Disclosure.receipt_date <= end_date)

                result = await session.execute(stmt)
                rows = result.scalars().all()
        except Exception as exc:
            raise DatabaseError(
                f"fetch_disclosures DB error ({symbol}): {exc}"
            ) from exc

        disclosures = [
            DisclosureInfo(
                corp_code=row.corp_code,
                symbol=row.symbol,
                report_name=row.report_name,
                receipt_no=row.receipt_no,
                receipt_date=row.receipt_date,
                filer_name=row.filer_name,
            )
            for row in rows
        ]

        # 3. Cache the result (best-effort)
        if disclosures:
            try:
                await self._cache.set_json(
                    _DISCLOSURES_CACHE_NS,
                    cache_key,
                    [d.model_dump(mode="json") for d in disclosures],
                    ttl=self._settings.DART_CACHE_TTL,
                )
            except CacheError:
                logger.warning("dart_fetch_disclosures_cache_write_failed", symbol=symbol)

        return disclosures

    # -- fetch_financial_statement: cache-through read ---------------------

    async def fetch_financial_statement(
        self,
        symbol: str,
        fiscal_year: int,
        report_type: ReportType,
    ) -> FinancialStatementInfo | None:
        """Return a financial statement from cache or DB.

        Flow: Redis -> DB -> cache result.
        """
        cache_key = f"{symbol}:{fiscal_year}:{report_type.value}"

        # 1. Try cache
        try:
            cached = await self._cache.get_json(_FINANCIALS_CACHE_NS, cache_key)
            if cached is not None:
                return FinancialStatementInfo.model_validate(cached)
        except CacheError:
            logger.warning("dart_fetch_financials_cache_read_failed", symbol=symbol)

        # 2. DB fallback
        fiscal_quarter: int | None = None
        if report_type == ReportType.QUARTERLY:
            fiscal_quarter = 1
        elif report_type == ReportType.SEMI_ANNUAL:
            fiscal_quarter = 2

        try:
            async with self._session_factory() as session:
                stmt = select(FinancialStatement).where(
                    FinancialStatement.symbol == symbol,
                    FinancialStatement.fiscal_year == fiscal_year,
                    FinancialStatement.report_type == report_type.value,
                    FinancialStatement.fiscal_quarter == fiscal_quarter,
                )
                result = await session.execute(stmt)
                row = result.scalars().first()
        except Exception as exc:
            raise DatabaseError(
                f"fetch_financial_statement DB error ({symbol}): {exc}"
            ) from exc

        if row is None:
            return None

        info = FinancialStatementInfo(
            symbol=row.symbol,
            corp_code=row.corp_code,
            report_type=ReportType(row.report_type),
            fiscal_year=row.fiscal_year,
            fiscal_quarter=row.fiscal_quarter,
            revenue=row.revenue,
            operating_income=row.operating_income,
            net_income=row.net_income,
            total_assets=row.total_assets,
            total_equity=row.total_equity,
            total_liabilities=row.total_liabilities,
            per=row.per,
            pbr=row.pbr,
            roe=row.roe,
            eps=row.eps,
            bps=row.bps,
        )

        # 3. Cache the result (best-effort)
        try:
            await self._cache.set_json(
                _FINANCIALS_CACHE_NS,
                cache_key,
                info.model_dump(mode="json"),
                ttl=self._settings.DART_CACHE_TTL,
            )
        except CacheError:
            logger.warning("dart_fetch_financials_cache_write_failed", symbol=symbol)

        return info


# -- Helpers ---------------------------------------------------------------


def _parse_financial_items(items: list[dict]) -> dict:
    """Parse DART fnlttSinglAcnt response items into a flat dict.

    DART returns one row per account name (매출액, 영업이익, ...).
    We extract the 당기금액 (thstrm_amount) for each known account.
    """
    # account_nm -> field mapping
    account_map: dict[str, str] = {
        "매출액": "revenue",
        "영업이익": "operating_income",
        "당기순이익": "net_income",
        "자산총계": "total_assets",
        "자본총계": "total_equity",
        "부채총계": "total_liabilities",
    }

    result: dict[str, int | None] = {}

    for item in items:
        account_nm = item.get("account_nm", "")
        field = account_map.get(account_nm)
        if field is None:
            continue

        raw_amount = item.get("thstrm_amount", "")
        if raw_amount:
            # Remove commas and convert
            cleaned = raw_amount.replace(",", "")
            try:
                result[field] = int(cleaned)
            except ValueError:
                result[field] = None

    return result
