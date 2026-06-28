"""OrderReconciler — REST 기반 SUBMITTED 주문 체결 안전망.
PositionReconciler — 브로커-DB 포지션 양방향 정합성 동기화.

OrderReconciler:
WS 체결통보(ExecutionStreamManager)가 놓친 체결을 두 시점에 정리한다:
- **12:00 KST** mid-day sweep: 오전 세션 접수건 중 미반영 주문 점검
- **15:40 KST** EOD sweep: 장 마감 후 미체결분 `CANCELLED(expired)` 정리

각 주문에 대해 ``broker.get_order_status`` 호출로 KIS 상태를 조회하고,
``FillFinalizer`` 공용 서비스를 통해 DB/포지션/알림을 반영한다.

PositionReconciler:
브로커 실보유 vs DB open 포지션을 3-way 비교하여 완전 동기화한다:
- Case 1 (DB only)  : DB에 있지만 브로커에 없는 포지션 → closed 처리
- Case 2 (broker only): 브로커에 있지만 DB에 없는 포지션 → 신규 생성 (strategy=manual)
- Case 3 (mismatch) : 수량·평균단가가 다른 포지션 → DB 갱신

9:00–16:00 KST 매시 정각 + 15:50 KST EOD 실행.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from src.core.enums import ApprovalStatus, ExitReason, OrderStatus, StrategyType
from src.db.models.execution import Order
from src.db.models.strategy import PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.broker.registry import BrokerRegistry
    from src.execution.fill_finalizer import FillFinalizer
    from src.strategy.position_manager import PositionManager

# 브로커에서 신규 발견된 포지션의 기본 손절 비율 (평균단가 대비)
_DEFAULT_STOP_PCT = Decimal("0.05")

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

        # F-06 게이트: 승인이 거부/만료된 주문은 절대 포지션화하지 않는다. 정상 흐름에선
        # 승인 거부 시 broker 접수 전에 CANCELLED 처리되어 여기 도달하지 않지만(broker_order_id
        # NULL), 향후 경로 재배치로 "승인 거부/만료인데 broker에 SUBMITTED로 살아있는" 주문이
        # 생기면 finalize(포지션 생성)로 내려가기 전에 차단한다.
        if order.approval_status in (
            ApprovalStatus.REJECTED.value,
            ApprovalStatus.TIMEOUT.value,
        ):
            logger.error(
                "reconciler.disapproved_order_submitted",
                order_id=order.id,
                broker_order_id=order.broker_order_id,
                approval=order.approval_status,
                broker_status=status.value,
            )
            if status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                # 이미 체결 — broker에서 되돌릴 수 없으므로 자동 포지션 생성 대신
                # 고위험 알림 + 별도 표식으로 사람 개입을 유도한다(Human-in-the-Loop).
                await self._finalizer.mark_disapproved_filled(order)
            else:
                # 미체결 — 취소 시도 후 만료 처리.
                try:
                    await broker.cancel_order(order.broker_order_id)
                except Exception:
                    logger.exception(
                        "reconciler.disapproved_cancel_failed", order_id=order.id,
                    )
                await self._finalizer.mark_expired(order)
            return

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

    closed_count: int = 0           # 닫은 포지션 수 (DB only)
    order_corrected_count: int = 0  # 보정한 주문 수
    broker_symbol_count: int = 0    # 브로커 실제 보유 종목 수 (전 계좌 합산)
    db_open_count: int = 0          # 정리 전 DB open 포지션 수
    created_count: int = 0          # 브로커에만 있어서 신규 생성한 포지션 수
    qty_updated_count: int = 0      # 수량·단가 불일치로 갱신한 포지션 수


class PositionReconciler:
    """브로커 실보유 vs DB open 포지션 양방향 동기화.

    Parameters
    ----------
    broker_registry: 계좌별 BrokerInterface
    session_factory: DB 세션 팩토리
    position_manager: PositionRecord CRUD 관리자 (신규 생성·수량 갱신용)
    """

    def __init__(
        self,
        *,
        broker_registry: BrokerRegistry,
        session_factory: async_sessionmaker[AsyncSession],
        position_manager: PositionManager | None = None,
    ) -> None:
        self._registry = broker_registry
        self._session_factory = session_factory
        self._position_manager = position_manager

    async def reconcile(self) -> ReconcileResult:
        """전 계좌에 대해 브로커-DB 포지션 3-way 동기화.

        - Case 1 (DB only)  : closed 처리 (exit_reason=reconciled)
        - Case 2 (broker only): 신규 PositionRecord 생성 (strategy=manual)
        - Case 3 (mismatch) : quantity·avg_cost DB 갱신
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
            created=result.created_count,
            qty_updated=result.qty_updated_count,
            order_corrected=result.order_corrected_count,
            broker_symbols=result.broker_symbol_count,
            db_open=result.db_open_count,
        )
        return result

    async def reconcile_for_account(self, account_id: str) -> ReconcileResult:
        """단일 계좌만 브로커-DB 3-way 동기화 (백오피스 수동 트리거용)."""
        result = ReconcileResult()
        broker = self._registry.get_all().get(account_id)
        if broker is None:
            logger.warning("position_reconciler.account_not_found", account_id=account_id)
            return result
        try:
            await self._reconcile_account(account_id, broker, result)
        except Exception:
            logger.exception("position_reconciler.account_failed", account_id=account_id)
        return result

    async def _reconcile_account(
        self, account_id: str, broker: object, result: ReconcileResult,
    ) -> None:
        from src.broker.base import BrokerInterface

        assert isinstance(broker, BrokerInterface)
        broker_positions = await broker.get_positions()
        broker_by_symbol = {p.symbol: p for p in broker_positions}
        result.broker_symbol_count += len(broker_by_symbol)

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
        db_by_symbol = {pos.symbol: pos for pos in open_positions}

        # Case 1: DB에 있지만 브로커에 없는 포지션 → closed
        for pos in open_positions:
            if pos.symbol in broker_by_symbol:
                continue

            # F-07: 청산가를 진입가로 대용(PnL=0)하지 않고, 폐기 시점 시장가로
            # 실현손익을 계산한다. 시장가 조회 실패 시 진입가로 폴백한다.
            # exit_reason=RECONCILED 플래그로 추정 청산임을 표기해
            # 성과·세금·학습 집계에서 구분 가능하게 둔다.
            exit_price = await self._resolve_orphan_exit_price(broker, pos)

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
                record.exit_price = exit_price
                record.exit_date = today
                record.exit_reason = ExitReason.RECONCILED.value
                record.realized_pnl = (
                    exit_price - record.avg_cost
                ) * record.quantity
                await session.commit()
                realized_pnl = record.realized_pnl
            result.closed_count += 1
            logger.warning(
                "position_reconciler.closed_orphan",
                account_id=account_id,
                position_id=pos.id,
                symbol=pos.symbol,
                exit_price=str(exit_price),
                realized_pnl=str(realized_pnl),
            )

            corrected = await self._correct_linked_orders(pos.id)
            result.order_corrected_count += corrected

        # Case 2: 브로커에 있지만 DB에 없는 포지션 → 신규 생성
        if self._position_manager is not None:
            for symbol, bp in broker_by_symbol.items():
                if symbol in db_by_symbol:
                    continue

                entry_date = (
                    bp.entry_date.date()
                    if isinstance(bp.entry_date, datetime)
                    else today
                )
                stop_loss = bp.average_cost * (Decimal("1") - _DEFAULT_STOP_PCT)
                await self._position_manager.create(
                    symbol=symbol,
                    strategy_type=StrategyType.MANUAL.value,
                    quantity=bp.quantity,
                    entry_price=bp.average_cost,
                    stop_loss_price=stop_loss,
                    account_id=account_id,
                )
                result.created_count += 1
                logger.warning(
                    "position_reconciler.created_missing",
                    account_id=account_id,
                    symbol=symbol,
                    quantity=bp.quantity,
                    avg_cost=str(bp.average_cost),
                )

        # Case 3: 양쪽에 있지만 수량·단가가 다른 포지션 → DB 갱신
        if self._position_manager is not None:
            for symbol, bp in broker_by_symbol.items():
                db_pos = db_by_symbol.get(symbol)
                if db_pos is None:
                    continue
                if db_pos.quantity == bp.quantity and db_pos.avg_cost == bp.average_cost:
                    continue

                await self._position_manager.update_quantity(
                    db_pos.id,
                    quantity=bp.quantity,
                    avg_cost=bp.average_cost,
                )
                result.qty_updated_count += 1
                logger.info(
                    "position_reconciler.qty_updated",
                    account_id=account_id,
                    position_id=db_pos.id,
                    symbol=symbol,
                    old_qty=db_pos.quantity,
                    new_qty=bp.quantity,
                    old_avg_cost=str(db_pos.avg_cost),
                    new_avg_cost=str(bp.average_cost),
                )

    async def _resolve_orphan_exit_price(
        self, broker: object, pos: PositionRecord,
    ) -> Decimal:
        """고아 포지션 폐기용 청산가 — 시장가 우선, 실패 시 진입가 폴백 (F-07)."""
        from src.broker.base import BrokerInterface

        if isinstance(broker, BrokerInterface):
            try:
                price_info = await broker.get_price(pos.symbol)
                current = price_info.current_price
                if current and current > 0:
                    return current
            except Exception:
                logger.warning(
                    "position_reconciler.orphan_price_fetch_failed",
                    symbol=pos.symbol,
                    position_id=pos.id,
                )
        return pos.entry_price

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
