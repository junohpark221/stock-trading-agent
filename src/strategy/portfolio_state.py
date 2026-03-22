"""PortfolioStateService — broker + DB 기반 실시간 포트폴리오 상태 집계.

BrokerInterface에서 잔고/포지션을 가져오고, DB에서 섹터 매핑/스냅샷 이력을
조합하여 PortfolioState를 구성한다. 일별 스냅샷 저장(upsert)도 담당한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.core.exceptions import DatabaseError
from src.core.models import PortfolioState
from src.db.models.market_data import StockMaster
from src.db.models.strategy import PortfolioSnapshot, PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.broker.base import BrokerInterface
    from src.core.models import Position
    from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)

_SECTOR_CACHE_TTL = 300  # 5분


class PortfolioStateService:
    """포트폴리오 현재 상태를 집계하고 일별 스냅샷을 관리한다."""

    def __init__(
        self,
        broker: BrokerInterface,
        session_factory: async_sessionmaker[AsyncSession],
        cache: RedisCache,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._cache = cache

    # -- 포트폴리오 상태 집계 --------------------------------------------------

    async def get_current_state(self) -> PortfolioState:
        """Broker 잔고 + DB 이력을 조합하여 현재 포트폴리오 상태를 반환한다."""
        balance = await self._broker.get_balance()
        positions = await self._broker.get_positions()

        sector_allocations = await self.get_sector_allocations(
            positions, balance.total_assets
        )

        historical_peak = await self.get_peak_value()
        # 현재가 신고점이면 갱신
        peak = max(historical_peak, balance.total_assets)

        if peak > Decimal(0):
            drawdown_pct = (peak - balance.total_assets) / peak * Decimal(100)
        else:
            drawdown_pct = Decimal(0)

        daily_trade_count = await self.get_daily_trade_count(date.today())

        state = PortfolioState(
            total_value=balance.total_assets,
            cash=balance.cash,
            invested=balance.invested,
            unrealized_pnl=balance.unrealized_pnl,
            daily_pnl=balance.daily_pnl,
            daily_pnl_pct=balance.daily_pnl_pct,
            drawdown_pct=drawdown_pct,
            peak_value=peak,
            positions=positions,
            sector_allocations=sector_allocations,
            daily_trade_count=daily_trade_count,
            timestamp=datetime.now(UTC),
        )

        logger.info(
            "portfolio_state.aggregated",
            total_value=str(balance.total_assets),
            positions_count=len(positions),
            drawdown_pct=str(drawdown_pct),
        )
        return state

    # -- 섹터 배분 계산 --------------------------------------------------------

    async def get_sector_allocations(
        self,
        positions: list[Position],
        total_value: Decimal,
    ) -> dict[str, Decimal]:
        """포지션별 StockMaster 섹터 매핑 → 섹터별 비중(%) 계산."""
        if not positions or total_value <= Decimal(0):
            return {}

        symbols = [p.symbol for p in positions]

        # StockMaster에서 sector 조회
        sector_map = await self._get_sector_map(symbols)

        # 섹터별 market_value 합산
        sector_values: dict[str, Decimal] = {}
        for pos in positions:
            sector = sector_map.get(pos.symbol, "기타")
            sector_values[sector] = sector_values.get(sector, Decimal(0)) + pos.market_value

        # 비중 계산
        return {
            sector: value / total_value * Decimal(100)
            for sector, value in sector_values.items()
        }

    async def _get_sector_map(self, symbols: list[str]) -> dict[str, str]:
        """종목 코드 → 섹터명 매핑. Redis 캐시 우선, 미스 시 DB 조회."""
        result: dict[str, str] = {}
        uncached: list[str] = []

        # 캐시 조회
        for sym in symbols:
            cached = await self._cache.get("sector", sym)
            if cached is not None:
                result[sym] = cached
            else:
                uncached.append(sym)

        if not uncached:
            return result

        # DB 조회
        try:
            async with self._session_factory() as session:
                stmt = select(StockMaster.symbol, StockMaster.sector).where(
                    StockMaster.symbol.in_(uncached)
                )
                rows = (await session.execute(stmt)).all()
        except Exception as exc:
            raise DatabaseError(f"StockMaster sector query failed: {exc}") from exc

        for sym, sector in rows:
            mapped = sector if sector else "기타"
            result[sym] = mapped
            # 캐시 저장
            await self._cache.set("sector", sym, mapped, ttl=_SECTOR_CACHE_TTL)

        # DB에도 없는 종목은 "기타"
        for sym in uncached:
            if sym not in result:
                result[sym] = "기타"

        return result

    # -- 스냅샷 저장 -----------------------------------------------------------

    async def save_snapshot(self, state: PortfolioState) -> None:
        """일별 포트폴리오 스냅샷을 upsert한다 (snapshot_date 기준)."""
        snapshot_date = state.timestamp.date()

        # sector_allocations의 Decimal → float 변환 (JSONB 호환)
        sector_json: dict[str, float] | None = None
        if state.sector_allocations:
            sector_json = {k: float(v) for k, v in state.sector_allocations.items()}

        values = {
            "snapshot_date": snapshot_date,
            "total_value": state.total_value,
            "cash": state.cash,
            "invested": state.invested,
            "unrealized_pnl": state.unrealized_pnl,
            "realized_pnl_daily": Decimal(0),
            "peak_value": state.peak_value,
            "drawdown_pct": state.drawdown_pct,
            "positions_count": len(state.positions),
            "sector_allocations": sector_json,
            "trade_count_daily": state.daily_trade_count,
        }

        stmt = pg_insert(PortfolioSnapshot).values(**values)
        update_cols = {k: v for k, v in values.items() if k != "snapshot_date"}
        stmt = stmt.on_conflict_do_update(
            constraint="uq_portfolio_snapshots_date",
            set_=update_cols,
        )

        try:
            async with self._session_factory() as session:
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            raise DatabaseError(f"Portfolio snapshot upsert failed: {exc}") from exc

        logger.info("portfolio_snapshot.saved", date=str(snapshot_date))

    # -- 역대 최고 자산 --------------------------------------------------------

    async def get_peak_value(self) -> Decimal:
        """portfolio_snapshots에서 역대 최고 total_value를 반환한다."""
        try:
            async with self._session_factory() as session:
                stmt = select(func.max(PortfolioSnapshot.total_value))
                result = await session.execute(stmt)
                peak = result.scalar()
        except Exception as exc:
            raise DatabaseError(f"Peak value query failed: {exc}") from exc

        return peak if peak is not None else Decimal(0)

    # -- 당일 매매 건수 --------------------------------------------------------

    async def get_daily_trade_count(self, target_date: date) -> int:
        """positions 테이블에서 해당 날짜의 신규 진입 건수를 반환한다."""
        try:
            async with self._session_factory() as session:
                stmt = select(func.count()).select_from(PositionRecord).where(
                    PositionRecord.entry_date == target_date
                )
                result = await session.execute(stmt)
                count = result.scalar()
        except Exception as exc:
            raise DatabaseError(f"Daily trade count query failed: {exc}") from exc

        return count if count is not None else 0
