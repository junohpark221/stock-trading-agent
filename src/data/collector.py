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
from src.core.time import today_kst

if TYPE_CHECKING:
    from src.data.providers.base import DataProvider
    from src.data.providers.dart_provider import DartProvider
    from src.data.providers.ecos_provider import EcosProvider
    from src.data.providers.fred_provider import FredProvider
    from src.data.providers.naver_provider import NaverProvider

logger = structlog.get_logger(__name__)

# PRJ-03 수급 수집 종목 간 페이싱(초). KIS 클라이언트 자체 인터벌
# (KIS_RATE_LIMIT_INTERVAL) 위에 얹는 추가 간격 — 게이트 ④ 실측에서
# EGW00201 재시도가 매 스냅샷 발생해(전부 흡수되긴 함) 백오프 진입
# 빈도를 낮추기 위한 완충이다.
_FLOW_CALL_PACE_SEC = 0.2


@dataclass
class CollectionSummary:
    """Generic result summary for collection orchestration."""

    total_symbols: int = 0
    succeeded: int = 0
    failed: int = 0
    total_rows: int = 0
    failed_symbols: list[str] = field(default_factory=list)

    # PRJ-03 단계 5 크로스체크 집계(수급 수집 전용 — 그 외 수집은 항상 0).
    revision_rows: int = 0
    cross_source_rows: int = 0
    mismatched_cells: int = 0


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


async def collect_investor_flow(
    provider: DataProvider,
    symbols: list[str],
) -> CollectionSummary:
    """PRJ-03: 종목별 수급을 순차 수집(종목당 단발 1콜, 최근 ~30거래일 upsert).

    실패 종목은 기록만 하고 계속 진행. 순차 순회 + ``_FLOW_CALL_PACE_SEC``
    페이싱으로 KIS 레이트리밋(EGW00201) 백오프 진입을 완화한다.
    """
    summary = CollectionSummary(total_symbols=len(symbols))

    for symbol in symbols:
        try:
            res = await provider.sync_investor_flow(symbol)
            summary.succeeded += 1
            summary.total_rows += res.upserted
            summary.revision_rows += res.revision_rows
            summary.cross_source_rows += res.cross_source_rows
            summary.mismatched_cells += res.mismatched_cells
            logger.debug(
                "collect_investor_flow_symbol_done",
                provider=provider.provider_name,
                symbol=symbol,
                rows=res.upserted,
            )
        except Exception:
            summary.failed += 1
            summary.failed_symbols.append(symbol)
            logger.exception(
                "collect_investor_flow_symbol_failed",
                provider=provider.provider_name,
                symbol=symbol,
            )
        await asyncio.sleep(_FLOW_CALL_PACE_SEC)

    logger.info(
        "collect_investor_flow_done",
        provider=provider.provider_name,
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        total_rows=summary.total_rows,
        revision_rows=summary.revision_rows,
        cross_source_rows=summary.cross_source_rows,
        mismatched_cells=summary.mismatched_cells,
    )
    return summary


async def collect_market_investor_flow(
    provider: DataProvider,
    markets: tuple[str, ...] = ("kospi", "kosdaq"),
) -> CollectionSummary:
    """PRJ-03: 시장 단위 수급 수집(시장당 단발 1콜, ~300행 upsert).

    ``failed_symbols``에는 시장명('kospi'/'kosdaq')이 담긴다.
    """
    summary = CollectionSummary(total_symbols=len(markets))

    for market in markets:
        try:
            res = await provider.sync_market_investor_flow(market)
            summary.succeeded += 1
            summary.total_rows += res.upserted
            summary.revision_rows += res.revision_rows
            summary.cross_source_rows += res.cross_source_rows
            summary.mismatched_cells += res.mismatched_cells
            logger.debug(
                "collect_market_investor_flow_market_done",
                provider=provider.provider_name,
                market=market,
                rows=res.upserted,
            )
        except Exception:
            summary.failed += 1
            summary.failed_symbols.append(market)
            logger.exception(
                "collect_market_investor_flow_market_failed",
                provider=provider.provider_name,
                market=market,
            )

    logger.info(
        "collect_market_investor_flow_done",
        provider=provider.provider_name,
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        total_rows=summary.total_rows,
        revision_rows=summary.revision_rows,
        cross_source_rows=summary.cross_source_rows,
        mismatched_cells=summary.mismatched_cells,
    )
    return summary


async def collect_short_interest(
    provider: DataProvider,
    symbols: list[str],
    *,
    window_days: int = 14,
) -> CollectionSummary:
    """PRJ-03: 종목별 공매도+대차를 트레일링 창으로 순차 수집.

    KRX 공표 지연(공매도 T+1·대차 T+2)을 흡수하기 위해 매일
    ``[today_kst()-window_days, today_kst()]`` 창을 재조회한다(idempotent upsert).
    종목당 공매도 → 대차 순차 호출, 둘 중 하나라도 실패하면 종목 failed.
    공매도 성공 후 대차 실패 시 공매도 행은 이미 커밋된 상태로 남지만(세션 분리),
    다음 날 창 재조회가 자가 치유하므로 의도된 동작이다.
    """
    end = today_kst()
    start = end - timedelta(days=window_days)
    summary = CollectionSummary(total_symbols=len(symbols))

    for symbol in symbols:
        try:
            symbol_rows = await provider.sync_short_sale(
                symbol, start_date=start, end_date=end
            )
            symbol_rows += await provider.sync_loan_trans(
                symbol, start_date=start, end_date=end
            )
            summary.succeeded += 1
            summary.total_rows += symbol_rows
            logger.debug(
                "collect_short_interest_symbol_done",
                provider=provider.provider_name,
                symbol=symbol,
                rows=symbol_rows,
            )
        except Exception:
            summary.failed += 1
            summary.failed_symbols.append(symbol)
            logger.exception(
                "collect_short_interest_symbol_failed",
                provider=provider.provider_name,
                symbol=symbol,
            )
        await asyncio.sleep(_FLOW_CALL_PACE_SEC)

    logger.info(
        "collect_short_interest_done",
        provider=provider.provider_name,
        total=summary.total_symbols,
        succeeded=summary.succeeded,
        failed=summary.failed,
        total_rows=summary.total_rows,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
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
