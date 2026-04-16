"""Report data fetcher — DB queries optimized for report generation.

기존 PositionManager/PortfolioStateService를 직접 사용하지 않고,
리포트 전용 쿼리를 캡슐화하여 불필요한 side-effect 방지.
모든 메서드는 read-only (commit 없음).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import Date, func, select

from src.core.enums import OrderStatus
from src.db.models.execution import Execution, Order
from src.db.models.strategy import PortfolioSnapshot, PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


class ReportDataFetcher:
    """리포트용 DB 쿼리 계층."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def get_closed_positions(
        self,
        *,
        start_date: date,
        end_date: date,
        strategy_type: str | None = None,
        account_id: str | None = None,
    ) -> list[PositionRecord]:
        """기간 내 청산된 포지션 (exit_date 기준, ASC 정렬).

        WHERE status='closed' AND exit_date BETWEEN start_date AND end_date
        """
        async with self._session_factory() as session:
            stmt = (
                select(PositionRecord)
                .where(
                    PositionRecord.status == "closed",
                    PositionRecord.exit_date >= start_date,
                    PositionRecord.exit_date <= end_date,
                )
                .order_by(PositionRecord.exit_date.asc())
            )
            if strategy_type is not None:
                stmt = stmt.where(PositionRecord.strategy_type == strategy_type)
            if account_id is not None:
                stmt = stmt.where(PositionRecord.account_id == account_id)

            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_portfolio_snapshots(
        self,
        *,
        start_date: date,
        end_date: date,
        account_id: str | None = None,
    ) -> list[PortfolioSnapshot]:
        """기간 내 포트폴리오 스냅샷 (snapshot_date ASC 정렬)."""
        async with self._session_factory() as session:
            stmt = (
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.snapshot_date >= start_date,
                    PortfolioSnapshot.snapshot_date <= end_date,
                )
                .order_by(PortfolioSnapshot.snapshot_date.asc())
            )
            if account_id is not None:
                stmt = stmt.where(PortfolioSnapshot.account_id == account_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_todays_orders(
        self, *, account_id: str | None = None,
    ) -> list[Order]:
        """오늘 생성된 주문 목록 (created_at::date = today)."""
        async with self._session_factory() as session:
            stmt = (
                select(Order)
                .where(func.cast(Order.created_at, Date) == date.today())
                .order_by(Order.created_at.asc())
            )
            if account_id is not None:
                stmt = stmt.where(Order.account_id == account_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_pending_orders(
        self, *, account_id: str | None = None,
    ) -> list[Order]:
        """현재 미체결 주문 (status='submitted')."""
        async with self._session_factory() as session:
            stmt = (
                select(Order)
                .where(Order.status == OrderStatus.SUBMITTED.value)
                .order_by(Order.created_at.asc())
            )
            if account_id is not None:
                stmt = stmt.where(Order.account_id == account_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_todays_executions(
        self, *, account_id: str | None = None,
    ) -> list[Execution]:
        """오늘 체결된 거래 (executed_at::date = today)."""
        async with self._session_factory() as session:
            stmt = (
                select(Execution)
                .where(func.cast(Execution.executed_at, Date) == date.today())
                .order_by(Execution.executed_at.asc())
            )
            if account_id is not None:
                stmt = stmt.join(Order, Execution.order_id == Order.id).where(
                    Order.account_id == account_id
                )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_open_positions(
        self, *, account_id: str | None = None,
    ) -> list[PositionRecord]:
        """현재 보유 포지션 (status='open')."""
        async with self._session_factory() as session:
            stmt = (
                select(PositionRecord)
                .where(PositionRecord.status == "open")
                .order_by(PositionRecord.entry_date.asc())
            )
            if account_id is not None:
                stmt = stmt.where(PositionRecord.account_id == account_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_latest_snapshot(
        self, *, account_id: str | None = None,
    ) -> PortfolioSnapshot | None:
        """가장 최근 포트폴리오 스냅샷 (snapshot_date DESC LIMIT 1)."""
        async with self._session_factory() as session:
            stmt = (
                select(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.snapshot_date.desc())
                .limit(1)
            )
            if account_id is not None:
                stmt = stmt.where(PortfolioSnapshot.account_id == account_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_position_with_orders(
        self,
        position_id: int,
    ) -> dict | None:
        """단일 포지션 + 관련 주문 조합.

        Returns: {"position": PositionRecord, "orders": [Order, ...]}
        Position.id → Order.position_id 관계 조회.
        없으면 None.
        """
        async with self._session_factory() as session:
            # 포지션 조회
            pos_result = await session.execute(
                select(PositionRecord).where(PositionRecord.id == position_id)
            )
            position = pos_result.scalar_one_or_none()
            if position is None:
                return None

            # 해당 포지션의 주문 조회
            orders_result = await session.execute(
                select(Order)
                .where(Order.position_id == position_id)
                .order_by(Order.created_at.asc())
            )
            orders = list(orders_result.scalars().all())

            return {"position": position, "orders": orders}
