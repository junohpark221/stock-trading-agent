"""OrderReconciler — REST 기반 SUBMITTED 주문 체결 안전망.
PositionReconciler — 브로커-DB 포지션 정합성 검증.

OrderReconciler:
WS 체결통보(ExecutionStreamManager)가 놓친 체결을 두 시점에 정리한다:
- **12:00 KST** mid-day sweep: 오전 세션 접수건 중 미반영 주문 점검
- **15:40 KST** EOD sweep: 장 마감 후 미체결분 `CANCELLED(expired)` 정리

각 주문에 대해 ``broker.get_order_status`` 호출로 KIS 상태를 조회하고,
``FillFinalizer`` 공용 서비스를 통해 DB/포지션/알림을 반영한다.

PositionReconciler:
브로커 실제 보유 종목과 DB open 포지션을 비교하여, 브로커에 없는 포지션을
닫고 연결된 주문을 보정한다. 15:50 KST 정기 작업 또는 백오피스 버튼으로 실행.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from src.core.enums import ExitReason, OrderStatus
from src.db.models.execution import Order
from src.db.models.strategy import PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.broker.registry import BrokerRegistry
    from src.execution.fill_finalizer import FillFinalizer

logger = structlog.get_logger(__name__)


class OrderReconciler:
    """SUBMITTED 주문을 KIS REST 조회로 정리하는 배치 서비스.

    Parameters
    ----------
    broker_registry: 계좌별 BrokerInterface 가져오기
    fill_finalizer: DB/포지션/알림 공용 반영 서비스
    """

    def __init__(
        self,
        *,
        broker_registry: BrokerRegistry,
        fill_finalizer: FillFinalizer,
    ) -> None:
        self._registry = broker_registry
        self._finalizer = fill_finalizer

    async def run(self, *, eod: bool = False, target_date: date | None = None) -> int:
        """오늘자 SUBMITTED 주문 전부 스위프.

        Parameters
        ----------
        eod: True면 장 마감 sweep. 미체결 주문을 CANCELLED(expired)로 정리.
        target_date: 기본값은 오늘(KST 상 동일하므로 UTC date 사용).

        Returns:
            처리된 주문 건수 (상태 변경과 무관).
        """
        scope_date = target_date or datetime.now(UTC).date()
        orders = await self._finalizer._get_submitted_orders_for_date(scope_date)  # noqa: SLF001
        logger.info(
            "reconciler.run.started",
            order_count=len(orders), eod=eod, target_date=str(scope_date),
        )

        processed = 0
        for order in orders:
            try:
                await self._reconcile_one(order, eod=eod, target_date=scope_date)
                processed += 1
            except Exception:
                logger.exception(
                    "reconciler.one_order_failed",
                    order_id=order.id, broker_order_id=order.broker_order_id,
                )

        logger.info(
            "reconciler.run.completed", processed=processed, eod=eod,
        )
        return processed

    async def _reconcile_one(
        self, order: Order, *, eod: bool, target_date: date,
    ) -> None:
        if not order.broker_order_id:
            # SUBMITTED이지만 broker_order_id 없음 — 접수 실패로 간주, EOD에 취소
            if eod:
                await self._finalizer.mark_expired(order)
            return

        try:
            broker = self._registry.get(order.account_id)
        except KeyError:
            logger.warning(
                "reconciler.no_broker_for_account",
                account_id=order.account_id, order_id=order.id,
            )
            return

        order_result = await broker.get_order_status(
            order.broker_order_id, order_date=target_date,
        )
        status = order_result.status

        # Terminal status → finalize via shared finalizer (idempotent)
        if status in (
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        ):
            await self._finalizer.finalize_from_order_result(
                order=order, order_result=order_result,
            )
            return

        # Still pending → EOD sweep expires it
        if eod:
            await self._finalizer.mark_expired(order)


@dataclass
class ReconcileResult:
    """PositionReconciler.reconcile() 결과."""

    closed_count: int = 0           # 닫은 포지션 수
    order_corrected_count: int = 0  # 보정한 주문 수
    broker_symbol_count: int = 0    # 브로커 실제 보유 종목 수 (전 계좌 합산)
    db_open_count: int = 0          # 정리 전 DB open 포지션 수


class PositionReconciler:
    """브로커 실보유 vs DB open 포지션 정합성 검증 + 주문 보정.

    Parameters
    ----------
    broker_registry: 계좌별 BrokerInterface
    session_factory: DB 세션 팩토리
    """

    def __init__(
        self,
        *,
        broker_registry: BrokerRegistry,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._registry = broker_registry
        self._session_factory = session_factory

    async def reconcile(self) -> ReconcileResult:
        """전 계좌에 대해 브로커-DB 포지션 정합성 검증.

        브로커에 없는 DB open 포지션 → closed (exit_reason=reconciled, pnl=0).
        해당 포지션에 연결된 FILLED 주문 → CANCELLED로 보정.
        """
        result = ReconcileResult()

        for account_id, broker in self._registry.get_all().items():
            try:
                await self._reconcile_account(account_id, broker, result)
            except Exception:
                logger.exception(
                    "position_reconciler.account_failed",
                    account_id=account_id,
                )

        logger.info(
            "position_reconciler.done",
            closed=result.closed_count,
            order_corrected=result.order_corrected_count,
            broker_symbols=result.broker_symbol_count,
            db_open=result.db_open_count,
        )
        return result

    async def _reconcile_account(
        self, account_id: str, broker: object, result: ReconcileResult,
    ) -> None:
        from src.broker.base import BrokerInterface

        assert isinstance(broker, BrokerInterface)
        broker_positions = await broker.get_positions()
        broker_symbols = {p.symbol for p in broker_positions}
        result.broker_symbol_count += len(broker_symbols)

        async with self._session_factory() as session:
            rows = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.status == "open",
                    PositionRecord.account_id == account_id,
                ),
            )
            open_positions = list(rows.scalars().all())

        result.db_open_count += len(open_positions)
        today = datetime.now(UTC).date()

        for pos in open_positions:
            if pos.symbol in broker_symbols:
                continue

            # 브로커에 없는 포지션 → 닫기
            async with self._session_factory() as session:
                record = (
                    await session.execute(
                        select(PositionRecord).where(
                            PositionRecord.id == pos.id,
                            PositionRecord.status == "open",
                        ),
                    )
                ).scalar_one_or_none()
                if record is None:
                    continue
                record.status = "closed"
                record.exit_price = record.entry_price
                record.exit_date = today
                record.exit_reason = ExitReason.RECONCILED.value
                record.realized_pnl = Decimal("0")
                await session.commit()
            result.closed_count += 1
            logger.warning(
                "position_reconciler.closed_orphan",
                account_id=account_id,
                position_id=pos.id,
                symbol=pos.symbol,
            )

            # 연결된 FILLED 주문 → CANCELLED로 보정
            corrected = await self._correct_linked_orders(pos.id)
            result.order_corrected_count += corrected

    async def _correct_linked_orders(self, position_id: int) -> int:
        """position_id에 연결된 FILLED 주문을 CANCELLED로 보정."""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(Order).where(
                    Order.position_id == position_id,
                    Order.status == OrderStatus.FILLED.value,
                ),
            )
            orders = list(rows.scalars().all())
            if not orders:
                return 0

            await session.execute(
                update(Order)
                .where(
                    Order.position_id == position_id,
                    Order.status == OrderStatus.FILLED.value,
                )
                .values(
                    status=OrderStatus.CANCELLED.value,
                    rejection_reason="브로커 미보유 — 정합성 보정",
                ),
            )
            await session.commit()

        for o in orders:
            logger.warning(
                "position_reconciler.order_corrected",
                order_id=o.id,
                position_id=position_id,
                symbol=o.symbol,
            )
        return len(orders)


__all__ = ["OrderReconciler", "PositionReconciler", "ReconcileResult"]
