"""OrderExecutor — 중앙 주문 실행 엔진.

Phase 4 전략 시그널(TradeDecision)을 실제 브로커 주문으로 변환한다.
전체 주문 라이프사이클을 오케스트레이션:
    Web 검증 → 승인 → 주문 체결 → 포지션 관리 → Audit Trail.

진입(execute_entry)과 청산(execute_exit) 두 경로를 제공한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import select, update

from src.core.enums import (
    ApprovalStatus,
    DecisionAction,
    DecisionStage,
    ExitReason,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalAction,
    WebVerifyResult,
)
from src.core.models import (
    ExecutionResult,
    ExitSignal,
    OrderRequest,
    PortfolioState,
    TradeDecision,
    WebVerification,
)
from src.db.models.execution import Execution, Order
from src.notification.templates import MessageTemplates

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.agent.decision_recorder import DecisionRecorder
    from src.broker.base import BrokerInterface
    from src.config import Settings
    from src.db.models.strategy import PositionRecord
    from src.execution.approval import ApprovalManager
    from src.execution.web_verify import WebSearchVerifier
    from src.notification.telegram import TelegramBot
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager
    from src.strategy.risk_manager import AlgoRiskManager

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 매핑 상수
# ---------------------------------------------------------------------------

# DecisionAction → OrderSide
_ACTION_TO_SIDE: dict[str, OrderSide] = {
    DecisionAction.BUY: OrderSide.BUY,
    DecisionAction.SELL: OrderSide.SELL,
    DecisionAction.STOP_LOSS: OrderSide.SELL,
    DecisionAction.TAKE_PROFIT: OrderSide.SELL,
}

# ExitReason → DecisionAction
_EXIT_REASON_TO_ACTION: dict[str, DecisionAction] = {
    ExitReason.STOP_LOSS: DecisionAction.STOP_LOSS,
    ExitReason.TAKE_PROFIT: DecisionAction.TAKE_PROFIT,
    ExitReason.TRAILING_STOP: DecisionAction.SELL,
    ExitReason.TIME_BASED: DecisionAction.SELL,
    ExitReason.FUNDAMENTAL: DecisionAction.SELL,
    ExitReason.LLM_SIGNAL: DecisionAction.SELL,
    ExitReason.DRAWDOWN: DecisionAction.SELL,
    ExitReason.MANUAL: DecisionAction.SELL,
}


class OrderExecutor:
    """중앙 주문 실행 엔진.

    전략 시그널 → Web 검증 → 승인 → 브로커 주문 → 체결 기록 → 포지션 관리.
    모든 과정을 decision_log에 기록하여 audit trail을 보장한다.
    """

    def __init__(
        self,
        *,
        broker: BrokerInterface,
        web_verifier: WebSearchVerifier,
        approval_manager: ApprovalManager,
        risk_manager: AlgoRiskManager,
        position_manager: PositionManager,
        portfolio_service: PortfolioStateService,
        recorder: DecisionRecorder,
        telegram_bot: TelegramBot,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._broker = broker
        self._web_verifier = web_verifier
        self._approval_manager = approval_manager
        self._risk_manager = risk_manager
        self._position_manager = position_manager
        self._portfolio_service = portfolio_service
        self._recorder = recorder
        self._bot = telegram_bot
        self._session_factory = session_factory
        self._settings = settings

    # ── Public API ────────────────────────────────────────────────────────

    async def execute_entry(
        self,
        *,
        trade_decision: TradeDecision,
        session_id: UUID,
        strategy_type: str,
        parent_decision_id: UUID | None = None,
        analysis_summary: str = "",
        account_id: str = "default",
        account_label: str = "",
        broker: BrokerInterface | None = None,
    ) -> ExecutionResult:
        """진입 주문 실행.

        TradeDecision → Web 검증 → 승인 → 브로커 주문 → 포지션 생성.

        Parameters
        ----------
        trade_decision: 전략 엔진의 매매 결정.
        session_id: 파이프라인 세션 ID.
        strategy_type: 전략 유형 (StrategyType.value).
        parent_decision_id: 부모 decision_log ID.
        analysis_summary: 분석 요약 (승인 메시지에 표시).
        """
        side = _ACTION_TO_SIDE.get(trade_decision.action, OrderSide.BUY)
        symbol = trade_decision.symbol
        price = trade_decision.price or Decimal("0")
        quantity = trade_decision.quantity
        decision_ids: list[UUID] = []
        order: Order | None = None
        effective_broker = broker or self._broker

        try:
            # 1. 주문 생성 (PENDING)
            order = await self._create_order(
                symbol=symbol,
                side=side,
                order_type=trade_decision.order_type,
                quantity=quantity,
                price=price,
                session_id=session_id,
                account_id=account_id,
            )

            # 2. Web 검증
            verification = await self._web_verifier.verify(
                symbol=symbol,
                side=side,
                session_id=session_id,
                parent_decision_id=parent_decision_id,
                is_stop_loss=False,
            )
            await self._update_order(
                order.id,
                web_verify_result=verification.result.value,
                web_verify_summary=verification.summary,
            )

            if verification.result == WebVerifyResult.BLOCKED:
                await self._update_order(
                    order.id,
                    status=OrderStatus.CANCELLED,
                    rejection_reason=f"Web 검증 차단: {verification.summary}",
                )
                await self._notify_safe(MessageTemplates.rejection_notification(
                    account_label=account_label,
                    symbol=symbol, name=symbol, side=side,
                    reason=verification.summary, stage="web_verify",
                ))
                did = await self._record_decision_safe(
                    session_id=session_id, stage=DecisionStage.EXECUTION,
                    decision=DecisionAction.REJECT, symbol=symbol,
                    reasoning=f"Web 검증 차단: {verification.summary}",
                    parent_id=parent_decision_id,
                    account_id=account_id,
                    data_snapshot={"order_id": order.id, "web_verify": verification.result.value},
                )
                if did:
                    decision_ids.append(did)
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"Web 검증 차단: {verification.summary}",
                )

            if verification.result == WebVerifyResult.WARNING:
                logger.warning("executor.web_verify_warning", symbol=symbol, summary=verification.summary)

            # 3. 포트폴리오 상태 조회
            portfolio_state = await self._portfolio_service.get_current_state()

            # 4. 승인 요청
            approval_status = await self._approval_manager.request_approval(
                trade_decision=trade_decision,
                order_id=order.id,
                session_id=session_id,
                portfolio_state=portfolio_state,
                web_verification=verification,
                analysis_summary=analysis_summary,
                account_id=account_id,
                account_label=account_label,
            )

            if approval_status in (ApprovalStatus.REJECTED, ApprovalStatus.TIMEOUT):
                # ApprovalManager가 이미 DB 업데이트 + 텔레그램 알림 처리
                await self._update_order(order.id, status=OrderStatus.CANCELLED)
                did = await self._record_decision_safe(
                    session_id=session_id, stage=DecisionStage.EXECUTION,
                    decision=DecisionAction.REJECT, symbol=symbol,
                    reasoning=f"승인 {approval_status.value}",
                    parent_id=parent_decision_id,
                    account_id=account_id,
                    data_snapshot={"order_id": order.id, "approval": approval_status.value},
                )
                if did:
                    decision_ids.append(did)
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"승인 {approval_status.value}",
                )

            # 5. 수량 변경 확인 (DB에서 재조회)
            effective_quantity = quantity
            refreshed_order = await self._get_order(order.id)
            if refreshed_order and refreshed_order.modified_quantity is not None:
                modified_qty = refreshed_order.modified_quantity

                # 리스크 재검증
                risk_result = await self._risk_manager.check(
                    symbol=symbol,
                    action=SignalAction.BUY if side == OrderSide.BUY else SignalAction.SELL,
                    quantity=modified_qty,
                    price=price,
                    stop_loss_price=trade_decision.stop_loss_price,
                    sector="",
                )

                if not risk_result.passed:
                    await self._update_order(
                        order.id,
                        status=OrderStatus.CANCELLED,
                        rejection_reason=f"리스크 재검증 실패: {', '.join(risk_result.violations)}",
                    )
                    await self._notify_safe(MessageTemplates.rejection_notification(
                        account_label=account_label,
                        symbol=symbol, name=symbol, side=side,
                        reason=f"리스크 재검증 실패 (수정 수량 {modified_qty:,}주)",
                        stage="risk_blocked",
                    ))
                    did = await self._record_decision_safe(
                        session_id=session_id, stage=DecisionStage.EXECUTION,
                        decision=DecisionAction.REJECT, symbol=symbol,
                        reasoning=f"수정 수량 리스크 위반: {risk_result.violations}",
                        parent_id=parent_decision_id,
                        account_id=account_id,
                        data_snapshot={
                            "order_id": order.id,
                            "modified_qty": modified_qty,
                            "violations": risk_result.violations,
                        },
                    )
                    if did:
                        decision_ids.append(did)
                    return self._fail_result(
                        order=order, symbol=symbol, side=side, quantity=modified_qty,
                        approval_status=approval_status,
                        web_verify_result=verification.result, decision_ids=decision_ids,
                        error=f"리스크 재검증 실패: {risk_result.violations}",
                    )

                effective_quantity = risk_result.adjusted_quantity or modified_qty

            # 6. 브로커 주문
            order_result = await effective_broker.place_order(OrderRequest(
                symbol=symbol,
                side=side,
                order_type=trade_decision.order_type,
                quantity=effective_quantity,
                price=price,
                reason=trade_decision.reasoning[:200],
                account_id=account_id,
            ))

            if order_result.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                await self._update_order(
                    order.id, status=OrderStatus.FAILED,
                    rejection_reason=f"브로커 주문 실패: {order_result.status.value}",
                )
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=effective_quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"브로커 주문 실패: {order_result.status.value}",
                )

            now = datetime.now(UTC)
            fill_price = order_result.filled_price or price
            fill_quantity = order_result.filled_quantity or effective_quantity
            commission = order_result.commission

            # 7. 체결 기록
            await self._record_execution(
                order_id=order.id,
                broker_order_id=order_result.order_id,
                fill_price=fill_price,
                fill_quantity=fill_quantity,
                commission=commission,
                executed_at=now,
            )

            # 8. 포지션 생성
            position_id: int | None = None
            try:
                pos = await self._position_manager.create(
                    symbol=symbol,
                    strategy_type=strategy_type,
                    quantity=fill_quantity,
                    entry_price=fill_price,
                    stop_loss_price=trade_decision.stop_loss_price or Decimal("0"),
                    take_profit_price=trade_decision.take_profit_price,
                    entry_session_id=session_id,
                    account_id=account_id,
                )
                position_id = pos.id
            except Exception:
                logger.critical(
                    "executor.position_create_failed",
                    order_id=order.id,
                    symbol=symbol,
                    exc_info=True,
                )

            # 9. 주문 상태 업데이트
            await self._update_order(
                order.id,
                status=OrderStatus.FILLED,
                broker_order_id=order_result.order_id,
                filled_quantity=fill_quantity,
                filled_price=fill_price,
                commission=commission,
                executed_at=now,
                position_id=position_id,
            )

            # 10. 텔레그램 체결 통보
            await self._notify_safe(MessageTemplates.execution_notification(
                account_label=account_label,
                symbol=symbol, name=symbol, side=side,
                quantity=fill_quantity, fill_price=fill_price,
                commission=commission, approval_status=approval_status,
            ))

            # 11. decision_log 기록
            action = DecisionAction.BUY if side == OrderSide.BUY else DecisionAction.SELL
            did = await self._record_decision_safe(
                session_id=session_id, stage=DecisionStage.EXECUTION,
                decision=action, symbol=symbol,
                reasoning=f"체결 완료: {fill_quantity}주 @ {fill_price}",
                parent_id=parent_decision_id,
                account_id=account_id,
                data_snapshot={
                    "order_id": order.id,
                    "broker_order_id": order_result.order_id,
                    "fill_price": str(fill_price),
                    "fill_quantity": fill_quantity,
                    "commission": str(commission),
                    "position_id": position_id,
                },
            )
            if did:
                decision_ids.append(did)

            return ExecutionResult(
                success=True,
                order_id=order.id,
                broker_order_id=order_result.order_id,
                symbol=symbol,
                side=side,
                quantity=fill_quantity,
                fill_price=fill_price,
                commission=commission,
                approval_status=approval_status,
                web_verify_result=verification.result,
                position_id=position_id,
                decision_ids=decision_ids,
            )

        except Exception as exc:
            logger.exception("executor.execute_entry_failed", symbol=symbol)
            if order is not None:
                await self._update_order_safe(
                    order.id, status=OrderStatus.FAILED,
                    rejection_reason=f"예외 발생: {exc!s}",
                )
            return ExecutionResult(
                success=False,
                order_id=order.id if order else None,
                symbol=symbol,
                side=side,
                quantity=quantity,
                approval_status=ApprovalStatus.AUTO_APPROVED,
                decision_ids=decision_ids,
                error=str(exc),
            )

    async def execute_exit(
        self,
        *,
        exit_signal: ExitSignal,
        position: PositionRecord,
        session_id: UUID,
        parent_decision_id: UUID | None = None,
        account_id: str = "default",
        account_label: str = "",
        broker: BrokerInterface | None = None,
    ) -> ExecutionResult:
        """청산 주문 실행.

        ExitSignal → Web 검증 → 승인 → 브로커 주문 → 포지션 청산.

        Parameters
        ----------
        exit_signal: 청산 시그널 (ExitConditionChecker 출력).
        position: 청산 대상 포지션 (DB ORM).
        session_id: 파이프라인 세션 ID.
        parent_decision_id: 부모 decision_log ID.
        """
        symbol = exit_signal.symbol
        reason = exit_signal.reason
        action = _EXIT_REASON_TO_ACTION.get(reason, DecisionAction.SELL)
        side = OrderSide.SELL
        is_stop_loss = reason == ExitReason.STOP_LOSS

        # 손절+즉시 → MARKET, 나머지 → LIMIT
        order_type = (
            OrderType.MARKET
            if is_stop_loss and exit_signal.urgency == "immediate"
            else OrderType.LIMIT
        )
        price = exit_signal.current_price
        quantity = position.quantity
        decision_ids: list[UUID] = []
        order: Order | None = None
        effective_broker = broker or self._broker

        try:
            # 1. TradeDecision 구성 (ApprovalManager 인터페이스용)
            trade_decision = self._build_exit_trade_decision(exit_signal, position)

            # 2. 주문 생성
            order = await self._create_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                session_id=session_id,
                account_id=account_id,
            )

            # 3. Web 검증
            verification = await self._web_verifier.verify(
                symbol=symbol,
                side=side,
                session_id=session_id,
                parent_decision_id=parent_decision_id,
                is_stop_loss=is_stop_loss,
            )
            await self._update_order(
                order.id,
                web_verify_result=verification.result.value,
                web_verify_summary=verification.summary,
            )

            if verification.result == WebVerifyResult.BLOCKED:
                await self._update_order(
                    order.id,
                    status=OrderStatus.CANCELLED,
                    rejection_reason=f"Web 검증 차단: {verification.summary}",
                )
                await self._notify_safe(MessageTemplates.rejection_notification(
                    account_label=account_label,
                    symbol=symbol, name=symbol, side=side,
                    reason=verification.summary, stage="web_verify",
                ))
                did = await self._record_decision_safe(
                    session_id=session_id, stage=DecisionStage.EXIT,
                    decision=DecisionAction.REJECT, symbol=symbol,
                    reasoning=f"Web 검증 차단: {verification.summary}",
                    parent_id=parent_decision_id,
                    account_id=account_id,
                    data_snapshot={"order_id": order.id, "exit_reason": reason.value},
                )
                if did:
                    decision_ids.append(did)
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"Web 검증 차단: {verification.summary}",
                )

            # 4. 포트폴리오 상태 + 승인
            portfolio_state = await self._portfolio_service.get_current_state()
            approval_status = await self._approval_manager.request_approval(
                trade_decision=trade_decision,
                order_id=order.id,
                session_id=session_id,
                portfolio_state=portfolio_state,
                web_verification=verification,
                account_id=account_id,
                account_label=account_label,
            )

            if approval_status in (ApprovalStatus.REJECTED, ApprovalStatus.TIMEOUT):
                await self._update_order(order.id, status=OrderStatus.CANCELLED)
                did = await self._record_decision_safe(
                    session_id=session_id, stage=DecisionStage.EXIT,
                    decision=DecisionAction.REJECT, symbol=symbol,
                    reasoning=f"청산 승인 {approval_status.value}",
                    parent_id=parent_decision_id,
                    account_id=account_id,
                    data_snapshot={"order_id": order.id, "exit_reason": reason.value},
                )
                if did:
                    decision_ids.append(did)
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"청산 승인 {approval_status.value}",
                )

            # 5. 브로커 주문
            order_result = await effective_broker.place_order(OrderRequest(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                account_id=account_id,
            ))

            if order_result.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                await self._update_order(
                    order.id, status=OrderStatus.FAILED,
                    rejection_reason=f"브로커 주문 실패: {order_result.status.value}",
                )
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"브로커 주문 실패: {order_result.status.value}",
                )

            now = datetime.now(UTC)
            fill_price = order_result.filled_price or price
            fill_quantity = order_result.filled_quantity or quantity
            commission = order_result.commission

            # 6. 체결 기록
            await self._record_execution(
                order_id=order.id,
                broker_order_id=order_result.order_id,
                fill_price=fill_price,
                fill_quantity=fill_quantity,
                commission=commission,
                executed_at=now,
            )

            # 7. 포지션 청산
            position_id: int | None = position.id
            try:
                await self._position_manager.close(
                    position.id,
                    exit_price=fill_price,
                    exit_reason=reason,
                    exit_session_id=session_id,
                )
            except Exception:
                logger.critical(
                    "executor.position_close_failed",
                    order_id=order.id,
                    position_id=position.id,
                    symbol=symbol,
                    exc_info=True,
                )
                position_id = None

            # 8. 주문 상태 업데이트
            await self._update_order(
                order.id,
                status=OrderStatus.FILLED,
                broker_order_id=order_result.order_id,
                filled_quantity=fill_quantity,
                filled_price=fill_price,
                commission=commission,
                executed_at=now,
                position_id=position.id,
            )

            # 9. 텔레그램 체결 통보
            await self._notify_safe(MessageTemplates.execution_notification(
                account_label=account_label,
                symbol=symbol, name=symbol, side=side,
                quantity=fill_quantity, fill_price=fill_price,
                commission=commission, approval_status=approval_status,
            ))

            # 10. decision_log
            did = await self._record_decision_safe(
                session_id=session_id, stage=DecisionStage.EXIT,
                decision=action, symbol=symbol,
                reasoning=f"청산 체결: {reason.value} {fill_quantity}주 @ {fill_price}",
                parent_id=parent_decision_id,
                account_id=account_id,
                data_snapshot={
                    "order_id": order.id,
                    "exit_reason": reason.value,
                    "broker_order_id": order_result.order_id,
                    "fill_price": str(fill_price),
                    "fill_quantity": fill_quantity,
                    "position_id": position.id,
                },
            )
            if did:
                decision_ids.append(did)

            return ExecutionResult(
                success=True,
                order_id=order.id,
                broker_order_id=order_result.order_id,
                symbol=symbol,
                side=side,
                quantity=fill_quantity,
                fill_price=fill_price,
                commission=commission,
                approval_status=approval_status,
                web_verify_result=verification.result,
                position_id=position.id,
                decision_ids=decision_ids,
            )

        except Exception as exc:
            logger.exception("executor.execute_exit_failed", symbol=symbol)
            if order is not None:
                await self._update_order_safe(
                    order.id, status=OrderStatus.FAILED,
                    rejection_reason=f"예외 발생: {exc!s}",
                )
            return ExecutionResult(
                success=False,
                order_id=order.id if order else None,
                symbol=symbol,
                side=side,
                quantity=quantity,
                approval_status=ApprovalStatus.AUTO_APPROVED,
                decision_ids=decision_ids,
                error=str(exc),
            )

    # ── Private: DB Helpers ───────────────────────────────────────────────

    async def _create_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Decimal,
        session_id: UUID,
        account_id: str = "default",
    ) -> Order:
        """orders 테이블에 PENDING 주문 생성."""
        row = Order(
            symbol=symbol,
            side=side.value,
            order_type=order_type.value,
            quantity=quantity,
            price=price,
            status=OrderStatus.PENDING.value,
            approval_status=ApprovalStatus.AUTO_APPROVED.value,
            original_quantity=quantity,
            session_id=session_id,
            account_id=account_id,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        logger.info("executor.order_created", order_id=row.id, symbol=symbol, side=side.value)
        return row

    async def _update_order(self, order_id: int, **fields: object) -> None:
        """orders 테이블 필드 업데이트."""
        async with self._session_factory() as session:
            await session.execute(
                update(Order).where(Order.id == order_id).values(**fields)
            )
            await session.commit()

    async def _update_order_safe(self, order_id: int, **fields: object) -> None:
        """_update_order의 예외 흡수 버전 (cleanup용)."""
        try:
            await self._update_order(order_id, **fields)
        except Exception:
            logger.exception("executor.update_order_safe_failed", order_id=order_id)

    async def _get_order(self, order_id: int) -> Order | None:
        """orders 테이블에서 주문 조회."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(Order).where(Order.id == order_id)
            )
            return result.scalar_one_or_none()

    async def _record_execution(
        self,
        *,
        order_id: int,
        broker_order_id: str,
        fill_price: Decimal,
        fill_quantity: int,
        commission: Decimal,
        executed_at: datetime,
    ) -> Execution:
        """executions 테이블에 체결 기록 저장."""
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
            await session.refresh(row)
        return row

    # ── Private: Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _build_exit_trade_decision(
        exit_signal: ExitSignal, position: PositionRecord
    ) -> TradeDecision:
        """ExitSignal + PositionRecord → TradeDecision 변환 (ApprovalManager용)."""
        action = _EXIT_REASON_TO_ACTION.get(
            exit_signal.reason, DecisionAction.SELL
        )
        return TradeDecision(
            symbol=exit_signal.symbol,
            action=action,
            confidence=Decimal("0.9"),  # 청산 시그널은 높은 확신도
            order_type=(
                OrderType.MARKET
                if exit_signal.reason == ExitReason.STOP_LOSS
                and exit_signal.urgency == "immediate"
                else OrderType.LIMIT
            ),
            quantity=position.quantity,
            price=exit_signal.current_price,
            stop_loss_price=None,
            take_profit_price=None,
            reasoning=exit_signal.reasoning,
        )

    async def _notify_safe(self, text: str) -> None:
        """텔레그램 알림 전송 — 실패 시 예외 흡수."""
        try:
            await self._bot.send_message(text)
        except Exception:
            logger.exception("executor.notify_failed")

    async def _record_decision_safe(
        self,
        *,
        session_id: UUID,
        stage: str,
        decision: str,
        symbol: str,
        reasoning: str,
        parent_id: UUID | None = None,
        account_id: str = "default",
        data_snapshot: dict | None = None,
    ) -> UUID | None:
        """decision_log 기록 — 실패 시 예외 흡수, None 반환."""
        try:
            return await self._recorder.record(
                session_id=session_id,
                stage=stage,
                decision=decision,
                reasoning=reasoning,
                parent_id=parent_id,
                symbol=symbol,
                account_id=account_id,
                data_snapshot=data_snapshot,
            )
        except Exception:
            logger.exception("executor.record_decision_failed", symbol=symbol)
            return None

    @staticmethod
    def _fail_result(
        *,
        order: Order | None = None,
        symbol: str,
        side: OrderSide,
        quantity: int,
        approval_status: ApprovalStatus = ApprovalStatus.AUTO_APPROVED,
        web_verify_result: WebVerifyResult | None = None,
        decision_ids: list[UUID] | None = None,
        error: str = "",
    ) -> ExecutionResult:
        """실패 ExecutionResult 생성 헬퍼."""
        return ExecutionResult(
            success=False,
            order_id=order.id if order else None,
            symbol=symbol,
            side=side,
            quantity=quantity,
            approval_status=approval_status,
            web_verify_result=web_verify_result,
            decision_ids=decision_ids or [],
            error=error,
        )
