"""OrderReconciler — REST 기반 SUBMITTED 주문 체결 안전망.

WS 체결통보(ExecutionStreamManager)가 놓친 체결을 두 시점에 정리한다:
- **12:00 KST** mid-day sweep: 오전 세션 접수건 중 미반영 주문 점검
- **15:40 KST** EOD sweep: 장 마감 후 미체결분 `CANCELLED(expired)` 정리

각 주문에 대해 ``broker.get_order_status`` 호출로 KIS 상태를 조회하고,
``FillFinalizer`` 공용 서비스를 통해 DB/포지션/알림을 반영한다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import structlog

from src.core.enums import OrderStatus

if TYPE_CHECKING:
    from src.broker.registry import BrokerRegistry
    from src.db.models.execution import Order
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


__all__ = ["OrderReconciler"]
