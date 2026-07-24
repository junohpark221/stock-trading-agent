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
from src.core.models import (
    OHLCV,
    InvestorFlowRecord,
    LoanTransRecord,
    MarketInvestorFlowRecord,
    ShortSaleRecord,
    StockInfo,
    TradingDayRecord,
)
from src.core.time import today_kst
from src.data.flow_crosscheck import (
    REVISION_FIELDS_MARKET,
    REVISION_FIELDS_SYMBOL,
    CrosscheckStats,
    FlowSyncResult,
    crosscheck_rows,
)
from src.data.providers.base import DataProvider
from src.db.models.calendar import TradingCalendarDay
from src.db.models.investor_flow import (
    InvestorFlowDaily,
    MarketInvestorFlowDaily,
    ShortInterestDaily,
)
from src.db.models.market_data import DailyOHLCV, StockMaster

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.broker.kis.client import KISClient
    from src.config import Settings
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_UPSERT_BATCH_SIZE = 500

# PRJ-03 수급 upsert — 충돌 시 갱신할 컬럼을 도메인 레코드 필드에서 유도한다.
# 레코드에 없는 컬럼(예: 시장 테이블의 pykrx 전용 index_volume/trading_value/
# market_cap)은 INSERT·SET 양쪽에서 자동 제외되어 기존 값이 보존된다.
# short_interest_daily는 공매도/대차 두 TR이 같은 (symbol,date) 행의 서로 다른
# 컬럼 절반을 채우는 부분 upsert — set_에 상대편 컬럼이 섞이면 EXCLUDED가 NULL로
# 평가되어 상대편 데이터를 지우므로, 반드시 이 상수로만 set_을 구성한다.
_FLOW_SOURCE = "kis"
_FLOW_UPDATE_COLS = tuple(
    k for k in InvestorFlowRecord.model_fields if k not in ("symbol", "date")
)
_MARKET_FLOW_UPDATE_COLS = tuple(
    k for k in MarketInvestorFlowRecord.model_fields if k not in ("market", "date")
)
_SHORT_SALE_UPDATE_COLS = tuple(
    k for k in ShortSaleRecord.model_fields if k not in ("symbol", "date")
)
_LOAN_UPDATE_COLS = tuple(
    k for k in LoanTransRecord.model_fields if k not in ("symbol", "date")
)

# PRJ-03 단계 5 리비전 감지 — 로그 홍수 방지 캡(카운트는 stats에 정확 유지).
_CROSSCHECK_LOG_MAX_ROWS = 5  # 콜당 kis_flow_revision 상세 라인 수
_CROSSCHECK_LOG_MAX_FIELDS = 5  # 라인당 changes 필드 수


def _group_diffs(stats: "CrosscheckStats") -> list[dict]:
    """FieldDiff 목록을 행(key,day,kind) 단위 로그 페이로드로 묶는다."""
    grouped: dict[tuple, dict] = {}
    for d in stats.diffs:
        entry = grouped.setdefault(
            (d.key, d.day, d.kind),
            {
                "key": d.key,
                "date": d.day.isoformat(),
                "kind": d.kind,
                "source_before": d.source_before,
                "changed_fields": 0,
                "changes": [],
            },
        )
        entry["changed_fields"] += 1
        if len(entry["changes"]) < _CROSSCHECK_LOG_MAX_FIELDS:
            entry["changes"].append(
                {"field": d.field, "old": str(d.old), "new": str(d.new)}
            )
    return list(grouped.values())

_MASTER_CACHE_NS = "kis:master"
_OHLCV_CACHE_NS = "kis:ohlcv"
# portfolio_state가 종목→섹터를 캐시하는 네임스페이스. 재sync로 sector가 갱신되면
# stale "기타"(NULL 폴백) 항목이 남지 않도록 함께 무효화한다.
_SECTOR_CACHE_NS = "sector"
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
                            "sector": s.sector or None,
                            "security_group": s.security_group,
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
                            "sector": stmt.excluded.sector,
                            "security_group": stmt.excluded.security_group,
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

        # Invalidate cache (best-effort) — 종목 마스터 + 종목→섹터 매핑 캐시
        try:
            await self._cache.clear_namespace(_MASTER_CACHE_NS)
            await self._cache.clear_namespace(_SECTOR_CACHE_NS)
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

    # ── PRJ-03: investor flow sync (API → DB upsert) ──────────────────

    async def _upsert_flow_rows(
        self,
        *,
        model: type,
        constraint: str,
        rows: list[dict],
        update_cols: tuple[str, ...],
        err_label: str,
    ) -> int:
        """수급 계열 공통 배치 upsert. ``source='kis'`` 주입은 여기 1곳 책임.

        캐시 무효화 없음 — 수급 테이블은 아직 fetch 캐시가 없는 쓰기 전용 경로.
        """
        total_upserted = 0
        try:
            async with self._session_factory() as session:
                for i in range(0, len(rows), _UPSERT_BATCH_SIZE):
                    batch = [
                        {**row, "source": _FLOW_SOURCE}
                        for row in rows[i : i + _UPSERT_BATCH_SIZE]
                    ]
                    stmt = pg_insert(model).values(batch)
                    set_ = {k: stmt.excluded[k] for k in update_cols}
                    set_["source"] = stmt.excluded.source
                    set_["updated_at"] = func.now()
                    stmt = stmt.on_conflict_do_update(constraint=constraint, set_=set_)
                    result = await session.execute(stmt)
                    total_upserted += result.rowcount
                await session.commit()
        except Exception as exc:
            raise DatabaseError(f"{err_label} DB error: {exc}") from exc
        return total_upserted

    async def _crosscheck_existing(
        self,
        *,
        model: type,
        key_col: str,
        key_value: str,
        rows: list[dict],
        table: str,
        fields: tuple[str, ...],
    ) -> CrosscheckStats | None:
        """PRJ-03 단계 5: upsert 직전 기존 행과 diff(리비전·경계 크로스체크).

        오늘 미만 날짜만 비교(당일 행 최초 적재는 리비전 아님). 어떤 실패도
        수집을 막지 않는다 — 예외는 경고 로그 후 None.
        """
        try:
            today = today_kst()
            dates = [row["date"] for row in rows if row["date"] < today]
            if not dates:
                return CrosscheckStats()
            key_attr = getattr(model, key_col)
            stmt = select(
                model.date,
                model.source,
                *[getattr(model, f) for f in fields],
            ).where(key_attr == key_value, model.date.in_(dates))
            async with self._session_factory() as session:
                result = await session.execute(stmt)
                existing_by_date = {row["date"]: row for row in result.mappings()}

            stats = crosscheck_rows(
                existing_by_date,
                rows,
                key_value=key_value,
                table=table,  # type: ignore[arg-type]
                incoming_source=_FLOW_SOURCE,
                before=today,
            )
        except Exception as exc:
            logger.warning(
                "kis_flow_crosscheck_failed", table=table, key=key_value, error=str(exc)
            )
            return None

        for diff_group in _group_diffs(stats)[:_CROSSCHECK_LOG_MAX_ROWS]:
            logger.warning("kis_flow_revision", table=table, **diff_group)
        if stats.mismatched_cells:
            logger.info(
                "kis_flow_crosscheck_summary",
                table=table,
                key=key_value,
                compared_rows=stats.compared_rows,
                revision_rows=stats.revision_rows,
                cross_source_rows=stats.cross_source_rows,
                mismatched_cells=stats.mismatched_cells,
            )
        return stats

    async def sync_investor_flow(self, symbol: str) -> FlowSyncResult:
        """종목 수급 단발 조회(최근 ~30거래일) → ``investor_flow_daily`` upsert.

        30행 전체를 upsert하므로 최근 한 달 내 결손일이 다음 실행에서 자동 복구된다.
        upsert 직전 기존 행과 diff(단계 5 리비전 감지 — ④ 새벽 보정 상시 감시).
        """
        records = await self._client.get_investor_flow(symbol)
        if not records:
            logger.warning("kis_sync_investor_flow_empty", symbol=symbol)
            return FlowSyncResult(0)

        rows = [r.model_dump() for r in records]
        stats = await self._crosscheck_existing(
            model=InvestorFlowDaily,
            key_col="symbol",
            key_value=symbol,
            rows=rows,
            table="flow",
            fields=REVISION_FIELDS_SYMBOL,
        )
        upserted = await self._upsert_flow_rows(
            model=InvestorFlowDaily,
            constraint="uq_investor_flow_daily_symbol_date",
            rows=rows,
            update_cols=_FLOW_UPDATE_COLS,
            err_label=f"sync_investor_flow ({symbol})",
        )
        logger.info("kis_sync_investor_flow_done", symbol=symbol, upserted=upserted)
        return FlowSyncResult(upserted, stats)

    async def sync_market_investor_flow(self, market: str) -> FlowSyncResult:
        """시장 단위 수급 단발 조회(~300행) → ``market_investor_flow_daily`` upsert.

        pykrx 전용 컬럼(index_volume/trading_value/market_cap)은 레코드에 없어
        INSERT·SET에서 제외 — 백필 값이 보존된다.
        """
        records = await self._client.get_market_investor_flow(market)
        if not records:
            logger.warning("kis_sync_market_investor_flow_empty", market=market)
            return FlowSyncResult(0)

        # 클라이언트에 dedupe 없음 — 동일 (market,date) 2행이면 pg가
        # "cannot affect row a second time" 에러를 내므로 날짜 기준 후승 dedupe.
        deduped = list({r.date: r for r in records}.values())

        rows = [r.model_dump() for r in deduped]
        stats = await self._crosscheck_existing(
            model=MarketInvestorFlowDaily,
            key_col="market",
            key_value=market,
            rows=rows,
            table="market_flow",
            fields=REVISION_FIELDS_MARKET,
        )
        upserted = await self._upsert_flow_rows(
            model=MarketInvestorFlowDaily,
            constraint="uq_market_investor_flow_daily_market_date",
            rows=rows,
            update_cols=_MARKET_FLOW_UPDATE_COLS,
            err_label=f"sync_market_investor_flow ({market})",
        )
        logger.info(
            "kis_sync_market_investor_flow_done", market=market, upserted=upserted
        )
        return FlowSyncResult(upserted, stats)

    async def sync_short_sale(
        self, symbol: str, *, start_date: date, end_date: date
    ) -> int:
        """일별 공매도 조회 → ``short_interest_daily`` 공매도 절반 부분 upsert."""
        records = await self._client.get_daily_short_sale(
            symbol, start_date=start_date, end_date=end_date
        )
        if not records:
            logger.warning("kis_sync_short_sale_empty", symbol=symbol)
            return 0

        upserted = await self._upsert_flow_rows(
            model=ShortInterestDaily,
            constraint="uq_short_interest_daily_symbol_date",
            rows=[r.model_dump() for r in records],
            update_cols=_SHORT_SALE_UPDATE_COLS,
            err_label=f"sync_short_sale ({symbol})",
        )
        logger.info("kis_sync_short_sale_done", symbol=symbol, upserted=upserted)
        return upserted

    async def sync_loan_trans(
        self, symbol: str, *, start_date: date, end_date: date
    ) -> int:
        """일별 대차거래 조회 → ``short_interest_daily`` 대차 절반 부분 upsert."""
        records = await self._client.get_daily_loan_trans(
            symbol, start_date=start_date, end_date=end_date
        )
        if not records:
            logger.warning("kis_sync_loan_trans_empty", symbol=symbol)
            return 0

        upserted = await self._upsert_flow_rows(
            model=ShortInterestDaily,
            constraint="uq_short_interest_daily_symbol_date",
            rows=[r.model_dump() for r in records],
            update_cols=_LOAN_UPDATE_COLS,
            err_label=f"sync_loan_trans ({symbol})",
        )
        logger.info("kis_sync_loan_trans_done", symbol=symbol, upserted=upserted)
        return upserted

    # ── F-23: trading calendar sync (API → DB upsert) ─────────────────

    async def sync_trading_calendar(
        self, *, start_date: date, until_date: date
    ) -> int:
        """국내휴장일조회(``CTCA0903R``) → ``trading_calendar`` upsert.

        원장 연관 TR(가급적 1일 1회 호출)이므로 호출부는 calendar_sync 잡
        1곳으로 제한한다. date PK 단순 upsert — 연 365행 소형 테이블이라
        배치 분할 없이 단일 statement.
        """
        records = await self._client.get_holidays(
            start_date=start_date, until_date=until_date
        )
        if not records:
            logger.warning("kis_sync_trading_calendar_empty", start=str(start_date))
            return 0

        update_cols = tuple(
            k for k in TradingDayRecord.model_fields if k != "date"
        )
        try:
            async with self._session_factory() as session:
                batch = [
                    {**r.model_dump(), "source": _FLOW_SOURCE} for r in records
                ]
                stmt = pg_insert(TradingCalendarDay).values(batch)
                set_ = {k: stmt.excluded[k] for k in update_cols}
                set_["source"] = stmt.excluded.source
                set_["updated_at"] = func.now()
                stmt = stmt.on_conflict_do_update(
                    index_elements=[TradingCalendarDay.date], set_=set_
                )
                result = await session.execute(stmt)
                upserted = result.rowcount
                await session.commit()
        except Exception as exc:
            raise DatabaseError(f"sync_trading_calendar DB error: {exc}") from exc

        logger.info(
            "kis_sync_trading_calendar_done",
            upserted=upserted,
            start=str(records[0].date),
            end=str(records[-1].date),
        )
        return upserted

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
                security_group=row.security_group,
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
