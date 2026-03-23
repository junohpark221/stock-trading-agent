"""PositionManager — positions 테이블 CRUD 관리자.

Strategy.save_position/close_position/get_open_positions를 대체하여
포지션 생성, 청산, 조회, 스톱로스 업데이트 등 DB 오퍼레이션을 캡슐화한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import func, select

from src.core.enums import ExitReason, StrategyType
from src.core.exceptions import DatabaseError
from src.db.models.strategy import PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)


class PositionManager:
    """positions 테이블 CRUD 관리자.

    포지션 생성, 청산, 조회, 스톱로스 업데이트 등 DB 오퍼레이션을 캡슐화.
    Strategy.save_position/close_position/get_open_positions를 대체한다.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    # ── Create ────────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        symbol: str,
        strategy_type: str,
        quantity: int,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None = None,
        trailing_stop_pct: Decimal | None = None,
        max_holding_days: int | None = None,
        entry_session_id: UUID | None = None,
    ) -> PositionRecord:
        """포지션 생성 (avg_cost = entry_price, status = 'open').

        Parameters
        ----------
        symbol: 종목 코드
        strategy_type: 전략 유형 (StrategyType.value)
        quantity: 수량
        entry_price: 진입가
        stop_loss_price: 손절가
        take_profit_price: 익절가 (선택)
        trailing_stop_pct: 트레일링 스톱 비율 (선택)
        max_holding_days: 최대 보유 기간 (선택)
        entry_session_id: decision_log 연결 세션 ID (선택)
        """
        record = PositionRecord(
            symbol=symbol,
            strategy_type=strategy_type,
            quantity=quantity,
            avg_cost=entry_price,
            entry_price=entry_price,
            entry_date=date.today(),
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            trailing_stop_pct=trailing_stop_pct,
            max_holding_days=max_holding_days,
            status="open",
            entry_session_id=entry_session_id,
        )

        try:
            async with self._session_factory() as session:
                session.add(record)
                await session.commit()
                await session.refresh(record)
        except Exception as exc:
            raise DatabaseError(f"Position create failed: {exc}") from exc

        logger.info(
            "position.created",
            id=record.id,
            symbol=record.symbol,
            quantity=record.quantity,
            strategy=record.strategy_type,
        )
        return record

    # ── Close ─────────────────────────────────────────────────────────────

    async def close(
        self,
        position_id: int,
        *,
        exit_price: Decimal,
        exit_reason: ExitReason,
        exit_session_id: UUID | None = None,
    ) -> PositionRecord:
        """포지션 청산 — realized_pnl 자동 계산.

        Parameters
        ----------
        position_id: 포지션 ID
        exit_price: 청산가
        exit_reason: 청산 사유 (ExitReason)
        exit_session_id: decision_log 연결 세션 ID (선택)

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.id == position_id
                )
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(
                        f"Position not found: id={position_id}"
                    )

                if record.status == "closed":
                    raise DatabaseError(
                        f"Position already closed: id={position_id}"
                    )

                # 청산 처리
                # realized_pnl = (청산가 - 평균단가) × 수량
                record.status = "closed"
                record.exit_price = exit_price
                record.exit_date = date.today()
                record.exit_reason = exit_reason.value
                record.realized_pnl = (
                    exit_price - record.avg_cost
                ) * record.quantity
                record.exit_session_id = exit_session_id

                await session.commit()
                await session.refresh(record)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Position close failed: {exc}") from exc

        logger.info(
            "position.closed",
            id=record.id,
            symbol=record.symbol,
            reason=exit_reason.value,
            realized_pnl=str(record.realized_pnl),
        )
        return record

    # ── Read ──────────────────────────────────────────────────────────────

    async def get_open(
        self, strategy_type: StrategyType | None = None
    ) -> list[PositionRecord]:
        """열린 포지션 조회 (strategy_type 필터 옵션).

        Parameters
        ----------
        strategy_type: 전략 유형 필터 (None이면 전체)
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.status == "open"
                )

                if strategy_type is not None:
                    stmt = stmt.where(
                        PositionRecord.strategy_type == strategy_type.value
                    )

                stmt = stmt.order_by(PositionRecord.entry_date.asc())
                result = await session.execute(stmt)
                return list(result.scalars().all())
        except Exception as exc:
            raise DatabaseError(
                f"Open positions query failed: {exc}"
            ) from exc

    async def get_by_symbol(
        self, symbol: str, *, status: str = "open"
    ) -> PositionRecord | None:
        """종목별 포지션 조회.

        Parameters
        ----------
        symbol: 종목 코드
        status: 포지션 상태 필터 (기본: "open")
        """
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(PositionRecord)
                    .where(
                        PositionRecord.symbol == symbol,
                        PositionRecord.status == status,
                    )
                    .limit(1)
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except Exception as exc:
            raise DatabaseError(
                f"Position query by symbol failed: {exc}"
            ) from exc

    async def get_daily_entries(self, target_date: date) -> int:
        """당일 진입 건수.

        Parameters
        ----------
        target_date: 조회 대상 날짜
        """
        try:
            async with self._session_factory() as session:
                stmt = select(func.count()).where(
                    PositionRecord.entry_date == target_date
                )
                result = await session.execute(stmt)
                return result.scalar_one()
        except Exception as exc:
            raise DatabaseError(
                f"Daily entries count failed: {exc}"
            ) from exc

    # ── Update ────────────────────────────────────────────────────────────

    async def update_stop_loss(
        self, position_id: int, new_stop_loss: Decimal
    ) -> None:
        """트레일링 스톱 업데이트.

        Parameters
        ----------
        position_id: 포지션 ID
        new_stop_loss: 새 손절가

        Raises
        ------
        DatabaseError: 포지션을 찾을 수 없거나 이미 청산된 경우
        """
        try:
            async with self._session_factory() as session:
                stmt = select(PositionRecord).where(
                    PositionRecord.id == position_id
                )
                result = await session.execute(stmt)
                record = result.scalar_one_or_none()

                if record is None:
                    raise DatabaseError(
                        f"Position not found: id={position_id}"
                    )

                if record.status == "closed":
                    raise DatabaseError(
                        f"Cannot update closed position: id={position_id}"
                    )

                record.stop_loss_price = new_stop_loss
                await session.commit()
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Stop loss update failed: {exc}"
            ) from exc

        logger.info(
            "position.stop_loss_updated",
            id=position_id,
            new_stop_loss=str(new_stop_loss),
        )
