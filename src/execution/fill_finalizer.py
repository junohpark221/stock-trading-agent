"""FillFinalizer — WS/Reconciler 공용 DB-기반 체결 확정 서비스.

OrderExecutor가 대기 중일 때는 자체 in-memory 컨텍스트로 finalize하지만,
WS 이벤트가 대기자 없이 도착했거나 reconciler가 뒤늦게 확정하는 경우에는
원본 context(strategy_type, stop_loss, take_profit 등)가 소실되어 있다.

본 모듈은 Order ORM + Account defaults 만으로 finalize를 수행한다:
- 진입 주문: Account.strategy_type + Settings.STOP_LOSS_PERCENT 기본값으로 포지션 생성
- 청산 주문: Order.position_id 로 원 포지션 찾아 close
- 거부/취소: 상태만 업데이트
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select, update

from src.core.enums import ApprovalStatus, ExitReason, OrderSide, OrderStatus
from src.db.models.account import Account
from src.db.models.execution import Execution, Order
from src.db.models.strategy import PositionRecord
from src.execution.fees import estimate_commission

_TERMINAL_NON_FILL = frozenset({
    OrderStatus.CANCELLED.value,
    OrderStatus.REJECTED.value,
    OrderStatus.FAILED.value,
})
from src.notification.templates import MessageTemplates

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings
    from src.core.models import ExecutionEvent, OrderResult
    from src.notification.telegram import TelegramBot
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)


class FillFinalizer:
    """DB-based finalize for WS/reconciler fallback paths."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        position_manager: PositionManager,
        telegram_bot: TelegramBot,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._position_manager = position_manager
        self._bot = telegram_bot
        self._settings = settings

    # ── Public API ────────────────────────────────────────────────────

    async def finalize_from_event(self, event: ExecutionEvent) -> None:
        """Apply a WS ExecutionEvent to DB state.

        Idempotent: if the order is already FILLED/REJECTED/CANCELLED, returns
        silently. WS/Reconciler may both fire for the same order.
        """
        order = await self._get_order(event.broker_order_id)
        if order is None:
            logger.info(
                "fill_finalizer.unknown_order",
                broker_order_id=event.broker_order_id,
            )
            return

        if order.status != OrderStatus.SUBMITTED.value:
            # Already processed (FILLED/REJECTED/CANCELLED) or not ready
            return

        if event.is_rejected:
            await self._apply_rejection(order, reason=event.rejected_reason or "KIS 거부")
            return

        if not event.is_filled:
            return  # 접수/정정 등 — 상태 변화 없음

        fill_price = event.filled_price if event.filled_price > 0 else order.price
        fill_quantity = event.filled_quantity if event.filled_quantity > 0 else order.quantity
        executed_at = event.timestamp

        # F-01: ExecutionEvent에는 수수료 필드가 없으므로 거래대금 기반으로 추정.
        commission = estimate_commission(
            OrderSide(order.side),
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            buy_pct=self._settings.COMMISSION_BUY_PCT,
            sell_pct=self._settings.COMMISSION_SELL_PCT,
        )

        await self._apply_fill(
            order=order,
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            commission=commission,
            executed_at=executed_at,
            is_partial=fill_quantity < order.quantity,
        )

    async def finalize_from_order_result(
        self, *, order: Order, order_result: OrderResult
    ) -> None:
        """Apply a REST `get_order_status` result to DB state (reconciler path)."""
        if order.status != OrderStatus.SUBMITTED.value:
            return

        status = order_result.status
        if status == OrderStatus.CANCELLED:
            await self._apply_rejection(order, reason="KIS 취소", cancelled=True)
            return
        if status == OrderStatus.REJECTED:
            await self._apply_rejection(order, reason="KIS 거부")
            return
        if status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            return  # 아직 미체결

        fill_quantity = order_result.filled_quantity or order.quantity
        fill_price = order_result.filled_price or order.price
        await self._apply_fill(
            order=order,
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            commission=order_result.commission or Decimal(0),
            executed_at=datetime.now(UTC),
            is_partial=status == OrderStatus.PARTIALLY_FILLED
            or fill_quantity < order.quantity,
        )

    async def mark_expired(self, order: Order) -> None:
        """EOD sweep — 미체결 주문을 CANCELLED(expired)로 정리."""
        if order.status != OrderStatus.SUBMITTED.value:
            return

        # F-02: 원자적 선점. 만료 처리 직전 WS/reconciler가 체결을 확정했다면
        # claim이 실패(rowcount 0)하므로 만료/고아청산을 건너뛴다.
        if not await self._claim_order(
            order.id,
            target=OrderStatus.CANCELLED,
            rejection_reason="장 마감 미체결 — 자동 취소",
        ):
            return

        # 고아 포지션 정리: WS/reconciler가 조기 생성한 포지션이 있으면 닫기
        if order.position_id:
            try:
                await self._close_orphaned_position(order.position_id, order.symbol)
            except Exception:
                logger.critical(
                    "fill_finalizer.orphan_position_close_failed",
                    order_id=order.id,
                    position_id=order.position_id,
                    exc_info=True,
                )

        account_label = await self._get_account_label(order.account_id)
        await self._notify_safe(MessageTemplates.rejection_notification(
            account_label=account_label,
            symbol=order.symbol, name=order.symbol,
            side=OrderSide(order.side),
            reason="장 마감까지 미체결",
            stage="risk_blocked",
        ))

    async def mark_disapproved_filled(self, order: Order) -> None:
        """F-06: 승인 거부/만료됐는데 broker에서 체결된 주문 처리.

        되돌릴 수 없는 체결이므로 자동 포지션 생성은 하지 않고, 고위험 표식 +
        텔레그램 알림으로 사람 개입을 유도한다(Human-in-the-Loop). 정상 흐름에선
        승인 거부 시 broker 접수 전에 차단되므로 도달하지 않는다.
        """
        # F-02 선점: WS/reconciler가 먼저 확정했다면 claim 실패(rowcount 0) → 건너뜀.
        if not await self._claim_order(
            order.id,
            target=OrderStatus.CANCELLED,
            rejection_reason="disapproved_but_filled",
        ):
            return

        logger.critical(
            "fill_finalizer.disapproved_order_filled",
            order_id=order.id,
            broker_order_id=order.broker_order_id,
            approval_status=order.approval_status,
            symbol=order.symbol,
        )

        account_label = await self._get_account_label(order.account_id)
        await self._notify_safe(MessageTemplates.rejection_notification(
            account_label=account_label,
            symbol=order.symbol, name=order.symbol,
            side=OrderSide(order.side),
            reason="⚠️ 승인 거부/만료 주문이 체결됨 — 수동 점검 필요",
            stage="risk_blocked",
        ))

    # ── Internal: apply fill ──────────────────────────────────────────

    async def _apply_fill(
        self,
        *,
        order: Order,
        fill_price: Decimal,
        fill_quantity: int,
        commission: Decimal,
        executed_at: datetime,
        is_partial: bool = False,
    ) -> None:
        # F-03: 부분 체결은 FILLED와 분리. 최종 상태는 PARTIALLY_FILLED.
        final_status = (
            OrderStatus.PARTIALLY_FILLED if is_partial else OrderStatus.FILLED
        )
        # F-02: 행 잠금 기반 원자적 선점(claim). WS·reconciler가 거의 동시에
        # 같은 SUBMITTED 주문을 관측해도, SUBMITTED→(PARTIALLY_)FILLED 조건부
        # UPDATE에서 rowcount==1 을 얻은 단 하나의 caller만 체결 후처리를 수행한다.
        # (Execution/Position 이중 생성 방지)
        if not await self._claim_order(order.id, target=final_status):
            logger.info(
                "fill_finalizer.fill_already_claimed",
                order_id=order.id,
                broker_order_id=order.broker_order_id,
            )
            return

        await self._record_execution(
            order_id=order.id,
            broker_order_id=order.broker_order_id or "",
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            commission=commission,
            executed_at=executed_at,
        )

        side = OrderSide(order.side)
        account_label = await self._get_account_label(order.account_id)

        if side == OrderSide.BUY:
            await self._create_entry_position(
                order=order,
                fill_price=fill_price,
                fill_quantity=fill_quantity,
                commission=commission,
                executed_at=executed_at,
                account_label=account_label,
                final_status=final_status,
            )
        else:
            await self._close_exit_position(
                order=order,
                fill_price=fill_price,
                fill_quantity=fill_quantity,
                commission=commission,
                executed_at=executed_at,
                account_label=account_label,
                final_status=final_status,
            )

    async def _create_entry_position(
        self,
        *,
        order: Order,
        fill_price: Decimal,
        fill_quantity: int,
        commission: Decimal,
        executed_at: datetime,
        account_label: str,
        final_status: OrderStatus = OrderStatus.FILLED,
    ) -> None:
        account = await self._get_account(order.account_id)
        strategy_type = account.strategy_type if account else "swing"

        default_sl_pct = Decimal(str(self._settings.STOP_LOSS_PERCENT))
        stop_loss = fill_price * (Decimal("1") - default_sl_pct / Decimal("100"))

        position_id: int | None = None
        try:
            pos = await self._position_manager.create(
                symbol=order.symbol,
                strategy_type=strategy_type,
                quantity=fill_quantity,
                entry_price=fill_price,
                stop_loss_price=stop_loss,
                take_profit_price=None,
                entry_session_id=order.session_id,
                account_id=order.account_id,
            )
            position_id = pos.id
        except Exception:
            logger.critical(
                "fill_finalizer.position_create_failed",
                order_id=order.id, symbol=order.symbol, exc_info=True,
            )
            await self._notify_safe(
                f"<b>[긴급] 포지션 생성 실패 (fallback)</b>\n"
                f"종목: {order.symbol}\n수량: {fill_quantity}주 @ {fill_price:,}원\n"
                f"브로커 체결 완료, DB 포지션 미기록 — 수동 확인 필요"
            )

        await self._update_order(
            order.id,
            status=final_status,
            filled_quantity=fill_quantity,
            filled_price=fill_price,
            commission=commission,
            executed_at=executed_at,
            position_id=position_id,
        )
        await self._notify_safe(MessageTemplates.execution_notification(
            account_label=account_label,
            symbol=order.symbol, name=order.symbol,
            side=OrderSide.BUY,
            quantity=fill_quantity, fill_price=fill_price,
            commission=commission,
            approval_status=ApprovalStatus(order.approval_status),
        ))

    async def _close_exit_position(
        self,
        *,
        order: Order,
        fill_price: Decimal,
        fill_quantity: int,
        commission: Decimal,
        executed_at: datetime,
        account_label: str,
        final_status: OrderStatus = OrderStatus.FILLED,
    ) -> None:
        if order.position_id:
            try:
                # F-03: 포지션 잔량 대비 체결 수량으로 부분/전량을 판단한다.
                # 잔량이 남으면 부분 청산(open 유지), 아니면 전량 청산.
                pos_qty = await self._get_open_position_quantity(order.position_id)
                if pos_qty is not None and fill_quantity < pos_qty:
                    await self._position_manager.reduce(
                        order.position_id,
                        exit_quantity=fill_quantity,
                        exit_price=fill_price,
                        exit_reason=ExitReason.MANUAL,
                        exit_session_id=order.session_id,
                    )
                else:
                    await self._position_manager.close(
                        order.position_id,
                        exit_price=fill_price,
                        exit_reason=ExitReason.MANUAL,
                        exit_session_id=order.session_id,
                    )
            except Exception:
                logger.critical(
                    "fill_finalizer.position_close_failed",
                    order_id=order.id, position_id=order.position_id,
                    exc_info=True,
                )
                await self._notify_safe(
                    f"<b>[긴급] 포지션 청산 DB 실패 (fallback)</b>\n"
                    f"종목: {order.symbol}\n수량: {fill_quantity}주 @ {fill_price:,}원\n"
                    f"즉시 수동 확인 필요"
                )

        await self._update_order(
            order.id,
            status=final_status,
            filled_quantity=fill_quantity,
            filled_price=fill_price,
            commission=commission,
            executed_at=executed_at,
        )
        await self._notify_safe(MessageTemplates.execution_notification(
            account_label=account_label,
            symbol=order.symbol, name=order.symbol,
            side=OrderSide.SELL,
            quantity=fill_quantity, fill_price=fill_price,
            commission=commission,
            approval_status=ApprovalStatus(order.approval_status),
        ))

    async def _apply_rejection(
        self, order: Order, *, reason: str, cancelled: bool = False,
    ) -> None:
        status = OrderStatus.CANCELLED if cancelled else OrderStatus.REJECTED
        # F-02: 거부/취소도 원자적 선점으로 처리해 동시 fill 확정·중복 알림을 차단
        if not await self._claim_order(
            order.id, target=status, rejection_reason=reason
        ):
            return
        account_label = await self._get_account_label(order.account_id)
        await self._notify_safe(MessageTemplates.rejection_notification(
            account_label=account_label,
            symbol=order.symbol, name=order.symbol,
            side=OrderSide(order.side),
            reason=reason,
            stage="risk_blocked",
        ))

    # ── Orphaned position cleanup ────────────────────────────────────

    async def _close_orphaned_position(
        self, position_id: int, symbol: str,
    ) -> None:
        """미체결 주문에 조기 생성된 PositionRecord를 닫는다 (PnL=0)."""
        today = datetime.now(UTC).date()
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(
                    PositionRecord.id == position_id,
                    PositionRecord.status == "open",
                ),
            )
            record = result.scalar_one_or_none()
            if record is None:
                return
            record.status = "closed"
            record.exit_price = record.entry_price
            record.exit_date = today
            record.exit_reason = ExitReason.EXPIRED.value
            record.realized_pnl = Decimal("0")
            await session.commit()
        logger.warning(
            "fill_finalizer.orphan_position_closed",
            position_id=position_id, symbol=symbol,
        )

    async def cleanup_orphaned_positions(self) -> int:
        """1회성 안전망: Order가 모두 취소/거부/실패인 open 포지션을 닫는다."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord).where(PositionRecord.status == "open"),
            )
            open_positions = list(result.scalars().all())

        closed = 0
        for pos in open_positions:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(Order).where(Order.position_id == pos.id),
                )
                orders = list(result.scalars().all())

            if orders and all(o.status in _TERMINAL_NON_FILL for o in orders):
                await self._close_orphaned_position(pos.id, pos.symbol)
                closed += 1

        logger.info("fill_finalizer.cleanup_orphaned_positions", closed=closed)
        return closed

    # ── DB helpers ────────────────────────────────────────────────────

    async def _get_order(self, broker_order_id: str) -> Order | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Order).where(Order.broker_order_id == broker_order_id),
            )
            return result.scalar_one_or_none()

    async def _get_open_position_quantity(self, position_id: int) -> int | None:
        """open 포지션의 현재 수량 (없거나 closed면 None) — 부분/전량 청산 판단용."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(PositionRecord.quantity).where(
                    PositionRecord.id == position_id,
                    PositionRecord.status == "open",
                ),
            )
            return result.scalar_one_or_none()

    async def _get_submitted_orders_for_date(
        self, target_date: date,
    ) -> list[Order]:
        """Return all orders with status=SUBMITTED created on target_date."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Order).where(
                    Order.status == OrderStatus.SUBMITTED.value,
                    Order.broker_order_id.is_not(None),
                ),
            )
            rows = list(result.scalars().all())
        # filter by created_at::date in Python to stay DB-agnostic
        return [
            r for r in rows
            if r.created_at and r.created_at.date() == target_date
        ]

    async def _update_order(self, order_id: int, **fields: object) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(Order).where(Order.id == order_id).values(**fields),
            )
            await session.commit()

    async def _claim_order(
        self, order_id: int, *, target: OrderStatus, **fields: object
    ) -> bool:
        """SUBMITTED→target 으로의 원자적 상태 전이(선점).

        단일 조건부 UPDATE이므로 DB가 동시 호출을 직렬화한다. 첫 호출만
        rowcount==1을 얻고, 나머지는 WHERE 불일치로 0을 받아 후처리를 건너뛴다.
        WS↔reconciler↔EOD sweep 간 이중 확정(TOCTOU)을 차단한다.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                update(Order)
                .where(
                    Order.id == order_id,
                    Order.status == OrderStatus.SUBMITTED.value,
                )
                .values(status=target.value, **fields),
            )
            await session.commit()
        return (result.rowcount or 0) == 1

    async def _record_execution(
        self,
        *,
        order_id: int,
        broker_order_id: str,
        fill_price: Decimal,
        fill_quantity: int,
        commission: Decimal,
        executed_at: datetime,
    ) -> None:
        row = Execution(
            order_id=order_id,
            broker_order_id=broker_order_id,
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            commission=commission,
            executed_at=executed_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def _get_account(self, account_id: str) -> Account | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Account).where(Account.id == account_id),
            )
            return result.scalar_one_or_none()

    async def _get_account_label(self, account_id: str) -> str:
        account = await self._get_account(account_id)
        if account is None or not account.nickname:
            return ""
        suffix = account.kis_account_no[-4:] if account.kis_account_no else ""
        return f"{account.nickname} ({suffix})" if suffix else account.nickname

    async def _notify_safe(self, text: str) -> None:
        try:
            await self._bot.send_message(text)
        except Exception:
            logger.exception("fill_finalizer.notify_failed")


__all__ = ["FillFinalizer"]
