"""Data collection orchestration functions.

Thin wrappers around ``DataProvider.sync_*`` methods that add:
- Multi-symbol sequential iteration with failure tolerance
- Structured logging with collection summaries

Usage::

    provider = KISDataProvider(client=client, cache=cache, ...)
    count = await collect_stock_master(provider)
    summary = await collect_daily_ohlcv(provider, ["005930", "000660"])
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import structlog

from src.core.enums import ReportType

if TYPE_CHECKING:
    from src.data.providers.base import DataProvider
    from src.data.providers.dart_provider import DartProvider
    from src.data.providers.ecos_provider import EcosProvider
    from src.data.providers.fred_provider import FredProvider
    from src.data.providers.naver_provider import NaverProvider

logger = structlog.get_logger(__name__)


@dataclass
class CollectionSummary:
    """Generic result summary for collection orchestration."""

    total_symbols: int = 0
    succeeded: int = 0
    failed: int = 0
    total_rows: int = 0
    failed_symbols: list[str] = field(default_factory=list)


# 기존 코드 하위호환 (tests/test_data_provider.py:19에서 import)
OHLCVCollectionSummary = CollectionSummary


async def collect_stock_master(provider: DataProvider) -> int:
    """Sync stock master via *provider* and return upserted row count.

    Exceptions propagate to the caller (scheduler, API handler) for
    policy-level error handling.
    """
    count = await provider.sync_stock_master()
    logger.info(
        "collect_stock_master_done",
        provider=provider.provider_name,
        upserted=count,
    )
    return count


async def collect_daily_ohlcv(
    provider: DataProvider,
    symbols: list[str],
    *,
    period_days: int = 100,
) -> CollectionSummary:
    """Sequentially sync daily OHLCV for each symbol.

    Failed symbols are logged and recorded but do **not** abort the run.
    Sequential iteration respects upstream rate limits.
    """
    summary = CollectionSummary(total_symbols=len(symbols))

    for symbol in symbols:
        try:
            rows = await provider.sync_daily_ohlcv(symbol, period_days=period_days)
            summary.succeeded += 1
            summary.total_rows += rows
            logger.debug(
                "collect_ohlcv_symbol_done",
                provider=provider.provider_name,
                symbol=symbol,
                rows=rows,
            )
        except Exception:
            summary.failed += 1
            summary.failed_symbols.append(symbol)
            logger.exception(
                "collect_ohlcv_symbol_failed",
                provider=provider.provider_name,
                symbol=symbol,
            )

    logger.info(
        "collect_daily_ohlcv_done",
        provider=provider.provider_name,
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        total_rows=summary.total_rows,
    )
    return summary


async def collect_financials(
    provider: DartProvider,
    symbols: list[str],
    fiscal_year: int,
) -> CollectionSummary:
    """Sequentially sync financial statements for each symbol.

    Iterates over 3 report types (ANNUAL, SEMI_ANNUAL, QUARTERLY) per symbol.
    DART rate limits are respected via sequential execution.
    """
    summary = CollectionSummary(total_symbols=len(symbols))

    for symbol in symbols:
        try:
            symbol_rows = 0
            for report_type in ReportType:
                rows = await provider.sync_financials(symbol, fiscal_year, report_type)
                symbol_rows += rows
            summary.succeeded += 1
            summary.total_rows += symbol_rows
            logger.debug(
                "collect_financials_symbol_done",
                symbol=symbol,
                fiscal_year=fiscal_year,
                rows=symbol_rows,
            )
        except Exception:
            summary.failed += 1
            summary.failed_symbols.append(symbol)
            logger.exception(
                "collect_financials_symbol_failed",
                symbol=symbol,
                fiscal_year=fiscal_year,
            )

    logger.info(
        "collect_financials_done",
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        total_rows=summary.total_rows,
    )
    return summary


async def collect_macro_indicators(
    ecos: EcosProvider,
    fred: FredProvider,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, int]:
    """Concurrently sync macro indicators from ECOS and FRED.

    Defaults to the last 365 days if dates are not provided.
    Returns ``{"ecos": <count>, "fred": <count>}``.
    """
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=365)

    results = await asyncio.gather(
        ecos.sync_all(start_date=start_date, end_date=end_date),
        fred.sync_all(start_date=start_date, end_date=end_date),
        return_exceptions=True,
    )

    counts: dict[str, int] = {}
    for key, result in zip(["ecos", "fred"], results, strict=True):
        if isinstance(result, Exception):
            logger.exception(
                "collect_macro_indicator_failed",
                source=key,
                error=str(result),
            )
            counts[key] = 0
        else:
            counts[key] = result

    logger.info("collect_macro_indicators_done", **counts)
    return counts


async def collect_news(
    provider: NaverProvider,
    symbols: list[str],
    *,
    query_map: dict[str, str] | None = None,
) -> CollectionSummary:
    """Sequentially sync news articles for each symbol.

    Naver rate limits are respected via sequential execution.
    ``query_map`` allows overriding the search query per symbol;
    defaults to the symbol itself.
    """
    summary = CollectionSummary(total_symbols=len(symbols))

    for symbol in symbols:
        try:
            query = (query_map or {}).get(symbol, symbol)
            rows = await provider.sync_news(symbol, query=query)
            summary.succeeded += 1
            summary.total_rows += rows
            logger.debug(
                "collect_news_symbol_done",
                symbol=symbol,
                query=query,
                rows=rows,
            )
        except Exception:
            summary.failed += 1
            summary.failed_symbols.append(symbol)
            logger.exception(
                "collect_news_symbol_failed",
                symbol=symbol,
            )

    logger.info(
        "collect_news_done",
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        total_rows=summary.total_rows,
    )
    return summary
