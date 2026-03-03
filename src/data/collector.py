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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.data.providers.base import DataProvider

logger = structlog.get_logger(__name__)


@dataclass
class OHLCVCollectionSummary:
    """Result summary for ``collect_daily_ohlcv``."""

    total_symbols: int = 0
    succeeded: int = 0
    failed: int = 0
    total_rows: int = 0
    failed_symbols: list[str] = field(default_factory=list)


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
) -> OHLCVCollectionSummary:
    """Sequentially sync daily OHLCV for each symbol.

    Failed symbols are logged and recorded but do **not** abort the run.
    Sequential iteration respects upstream rate limits.
    """
    summary = OHLCVCollectionSummary(total_symbols=len(symbols))

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
