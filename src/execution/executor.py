"""OrderExecutor — 중앙 주문 실행 엔진.

Phase 4 전략 시그널(TradeDecision)을 실제 브로커 주문으로 변환한다.
전체 주문 라이프사이클을 오케스트레이션:
    Web 검증 → 승인 → 주문 체결 → 포지션 관리 → Audit Trail.

진입(execute_entry)과 청산(execute_exit) 두 경로를 제공한다.

KIS 주문 접수는 동기 응답으로 체결 확정을 주지 않으므로(`OrderStatus.SUBMITTED`만 반환)
SUBMITTED 상태일 때는 `ExecutionStreamManager`(WS 체결통보 기반) 혹은
`OrderReconciler`(REST 폴링 기반)가 체결을 확정한다. 본 클래스는
체결 후 finalize 로직을 공용 헬퍼(`finalize_entry_fill`, `finalize_exit_fill`)로
노출하여 WS/reconciler 양쪽에서 동일한 경로를 사용하도록 한다.
"""

from __future__ import annotations

import asyncio
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
    OrderResult,
    PortfolioState,
    TradeDecision,
    WebVerification,
)
from src.db.models.execution import Execution, Order
from src.db.models.market_data import StockMaster
from src.notification.templates import MessageTemplates
from src.strategy.sizing import PositionSizer
from src.strategy.trailing import entry_trailing_params

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.agent.decision_recorder import DecisionRecorder
    from src.broker.base import BrokerInterface
    from src.config import Settings
    from src.db.models.strategy import PositionRecord
    from src.execution.approval import ApprovalManager
    from src.execution.execution_stream import ExecutionStreamManager
    from src.execution.web_verify import WebSearchVerifier
    from src.notification.telegram import TelegramBot
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager
    from src.strategy.risk_manager import AlgoRiskManager, BatchReservation

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

# broker.place_order 반환 상태 중 "명시적 실패"로 취급할 집합.
# SUBMITTED/PENDING은 정상 접수이므로 이 집합에 포함되지 않는다 —
# KIS는 주문 접수 시 SUBMITTED만 반환하고 체결은 WS(H0STCNI0/9) 또는 reconciler로 확정된다.
_HARD_FAIL_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.FAILED}
)


def _event_to_order_result(
    *,
    event: object,  # ExecutionEvent (TYPE_CHECKING 회피용 런타임 duck-typing)
    submitted: OrderResult,
    fallback_quantity: int,
) -> OrderResult:
    """WS ExecutionEvent + 원 접수응답을 체결된 OrderResult로 병합."""
    filled_qty = int(getattr(event, "filled_quantity", 0))
    filled_price = Decimal(str(getattr(event, "filled_price", 0)))

    # 체결수량이 0이면 아직 미체결 — SUBMITTED 상태로 반환
    if filled_qty <= 0:
        return OrderResult(
            account_id=submitted.account_id,
            order_id=submitted.order_id,
            symbol=submitted.symbol,
            side=submitted.side,
            order_type=submitted.order_type,
            quantity=submitted.quantity,
            price=submitted.price,
            status=OrderStatus.SUBMITTED,
            filled_quantity=0,
            filled_price=None,
            commission=submitted.commission,
            timestamp=getattr(event, "timestamp", submitted.timestamp),
        )

    return OrderResult(
        account_id=submitted.account_id,
        order_id=submitted.order_id,
        symbol=submitted.symbol,
        side=submitted.side,
        order_type=submitted.order_type,
        quantity=filled_qty,
        price=submitted.price,
        status=OrderStatus.FILLED,
        filled_quantity=filled_qty,
        filled_price=filled_price if filled_price > 0 else submitted.price,
        commission=submitted.commission,
        timestamp=getattr(event, "timestamp", submitted.timestamp),
    )


# ExitReason → DecisionAction
_EXIT_REASON_TO_ACTION: dict[str, DecisionAction] = {
    ExitReason.STOP_LOSS: DecisionAction.STOP_LOSS,
    ExitReason.TAKE_PROFIT: DecisionAction.TAKE_PROFIT,
    ExitReason.PARTIAL_TAKE_PROFIT: DecisionAction.TAKE_PROFIT,
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
        execution_stream: ExecutionStreamManager | None = None,
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
        self._execution_stream = execution_stream

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
        manual: bool = False,
        batch_reservation: BatchReservation | None = None,
        entry_analysis_snapshot: dict | None = None,
        reference_price: Decimal | None = None,
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
        manual: True면 웹검증/승인 플로우를 생략(관리자 수동 주문). Cash gate와 포지션 생성은 유지.
        batch_reservation: 배치 매수 시 같은 배치의 in-flight 진입을 누적하는 예약
            (F-04). 주어지면 승인 요청 전에 누적 한도 게이트를 적용하고, 접수(미체결)
            성공 시 예약에 누적한다. 수동/단건 주문은 None(게이트 스킵).
        reference_price: 전략이 손절/익절을 산출한 기준가(결정 큐 reference_price,
            08:30 결정가). 주어지면 갭게이트 통과 등으로 진입가가 기준가에서 벌어졌을 때
            손절/익절을 원래 비율로 재적용하고 수량을 재사이징해 의도 R:R·리스크를
            보존한다(F-16). 수동/단건 주문은 None(보정 스킵).
        """
        side = _ACTION_TO_SIDE.get(trade_decision.action, OrderSide.BUY)
        symbol = trade_decision.symbol
        price = trade_decision.price or Decimal("0")
        quantity = trade_decision.quantity
        decision_ids: list[UUID] = []
        order: Order | None = None
        effective_broker = broker or self._broker
        # 배치 예약 누적용 — 게이트에서 결정되고 접수 성공 시 reserve()에 사용
        sector = ""
        is_new_holding = False

        # 입력 검증 — 주문 생성 전에 조기 반환
        if quantity <= 0:
            logger.warning("executor.invalid_quantity", symbol=symbol, quantity=quantity)
            return self._fail_result(
                symbol=symbol, side=side, quantity=quantity,
                error=f"유효하지 않은 수량: {quantity}",
            )
        if price <= 0:
            logger.warning("executor.invalid_price", symbol=symbol, price=str(price))
            return self._fail_result(
                symbol=symbol, side=side, quantity=quantity,
                error=f"유효하지 않은 가격: {price}",
            )

        try:
            # 0. 회사명 조회 (웹 검증 프롬프트 정확도 향상)
            company_name = await self._lookup_company_name(symbol)

            # 1. 주문 생성 (PENDING)
            order = await self._create_order(
                symbol=symbol,
                side=side,
                order_type=trade_decision.order_type,
                quantity=quantity,
                price=price,
                session_id=session_id,
                account_id=account_id,
                entry_analysis_snapshot=entry_analysis_snapshot,
            )

            # 2. Web 검증 — manual=True면 생략하고 SAFE 결과로 간주
            if manual:
                verification = WebVerification(
                    symbol=symbol,
                    result=WebVerifyResult.SAFE,
                    summary="수동 주문: 웹검증 생략",
                )
                await self._update_order(
                    order.id,
                    web_verify_result=verification.result.value,
                    web_verify_summary=verification.summary,
                )
            else:
                verification = await self._web_verifier.verify(
                    symbol=symbol,
                    side=side,
                    session_id=session_id,
                    parent_decision_id=parent_decision_id,
                    is_stop_loss=False,
                    company_name=company_name,
                )
                # web_verify 결과 + BLOCKED 시 status를 한 번에 업데이트
                web_update: dict = {
                    "web_verify_result": verification.result.value,
                    "web_verify_summary": verification.summary,
                }
                if verification.result == WebVerifyResult.BLOCKED:
                    web_update["status"] = OrderStatus.CANCELLED.value
                    web_update["rejection_reason"] = f"Web 검증 차단: {verification.summary}"
                await self._update_order(order.id, **web_update)

            if verification.result == WebVerifyResult.BLOCKED:
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
                    terminal=True,
                )

            if verification.result == WebVerifyResult.WARNING:
                logger.warning("executor.web_verify_warning", symbol=symbol, summary=verification.summary)

            # 2-b. 배치 누적 한도 게이트 (F-04) — 승인 요청 전에 같은 배치의 in-flight
            # 예약을 반영해 MAX_HOLDINGS / MAX_DAILY_TRADES / 섹터 집중도를 검증한다.
            # 차단 시 승인을 요청하지 않고 즉시 거부(헛 승인 방지). 수동 주문은 스킵.
            if batch_reservation is not None and not manual and side == OrderSide.BUY:
                sector = await self._lookup_sector(symbol)
                state_snapshot = await self._portfolio_service.get_current_state()
                held = {
                    p.symbol for p in state_snapshot.positions
                } | batch_reservation.new_symbols
                is_new_holding = symbol not in held

                gate_result = await self._risk_manager.check(
                    symbol=symbol,
                    action=SignalAction.BUY,
                    quantity=quantity,
                    price=price,
                    stop_loss_price=trade_decision.stop_loss_price,
                    sector=sector,
                    account_id=account_id,
                    reservation=batch_reservation,
                )

                if not gate_result.passed or gate_result.adjusted_quantity <= 0:
                    reason = (
                        f"배치 누적 리스크 한도 차단: {', '.join(gate_result.violations)}"
                    )
                    await self._update_order(
                        order.id,
                        status=OrderStatus.CANCELLED,
                        rejection_reason=reason,
                    )
                    await self._notify_safe(MessageTemplates.rejection_notification(
                        account_label=account_label,
                        symbol=symbol, name=symbol, side=side,
                        reason=reason, stage="risk_blocked",
                    ))
                    did = await self._record_decision_safe(
                        session_id=session_id, stage=DecisionStage.EXECUTION,
                        decision=DecisionAction.REJECT, symbol=symbol,
                        reasoning=reason,
                        parent_id=parent_decision_id,
                        account_id=account_id,
                        data_snapshot={
                            "order_id": order.id,
                            "violations": gate_result.violations,
                            "batch_trade_count": batch_reservation.trade_count,
                        },
                    )
                    if did:
                        decision_ids.append(did)
                    return self._fail_result(
                        order=order, symbol=symbol, side=side, quantity=quantity,
                        web_verify_result=verification.result, decision_ids=decision_ids,
                        error=reason,
                        terminal=True,
                    )

                # 섹터 한도 등으로 수량이 축소된 경우 반영 후 진행
                if gate_result.adjusted_quantity < quantity:
                    logger.info(
                        "executor.batch_gate.shrink",
                        symbol=symbol,
                        original_qty=quantity,
                        adjusted_qty=gate_result.adjusted_quantity,
                        account_id=account_id,
                    )
                    quantity = gate_result.adjusted_quantity
                    trade_decision = trade_decision.model_copy(
                        update={"quantity": quantity}
                    )
                    await self._update_order(order.id, quantity=quantity)

            # 3-4-5. 포트폴리오 상태 + 승인 요청 + 수량 변경 — manual=True면 전체 생략
            if manual:
                approval_status = ApprovalStatus.AUTO_APPROVED
                effective_quantity = quantity
            else:
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
                    # F-06 방어: 정상 흐름에선 승인이 broker 접수보다 먼저라 broker_order_id가
                    # NULL이지만, 향후 경로 재배치로 이미 접수된 주문이 있으면 broker에 살아
                    # 있으므로 취소를 시도한다(best-effort). 취소 실패가 거부 처리를 막지 않는다.
                    refreshed = await self._get_order(order.id)
                    if refreshed and refreshed.broker_order_id:
                        logger.warning(
                            "executor.disapproved_order_has_broker_id",
                            order_id=order.id,
                            broker_order_id=refreshed.broker_order_id,
                            approval=approval_status.value,
                        )
                        try:
                            await effective_broker.cancel_order(
                                refreshed.broker_order_id
                            )
                        except Exception:
                            logger.exception(
                                "executor.disapproved_order_cancel_failed",
                                order_id=order.id,
                            )
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
                        # REJECTED(사용자 명시 거부)만 영구 차단. TIMEOUT은 다음 드레인에
                        # 사용자가 승인할 수 있어 재시도 유지(F-21).
                        terminal=approval_status == ApprovalStatus.REJECTED,
                    )

                # 5. 수량 변경 확인 (DB에서 재조회)
                effective_quantity = quantity
                refreshed_order = await self._get_order(order.id)
                if refreshed_order and refreshed_order.modified_quantity is not None:
                    modified_qty = refreshed_order.modified_quantity

                    # 포트폴리오 상태 갱신 (승인 대기 중 변경 반영)
                    portfolio_state = await self._portfolio_service.get_current_state()

                    # sector 조회 (리스크 재검증에 필요; 배치 게이트에서 이미 조회됐으면 재사용)
                    if not sector:
                        sector = await self._lookup_sector(symbol)

                    # 리스크 재검증 (배치 예약 누적 반영)
                    risk_result = await self._risk_manager.check(
                        symbol=symbol,
                        action=SignalAction.BUY if side == OrderSide.BUY else SignalAction.SELL,
                        quantity=modified_qty,
                        price=price,
                        stop_loss_price=trade_decision.stop_loss_price,
                        sector=sector,
                        reservation=batch_reservation,
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
                            terminal=True,
                        )

                    effective_quantity = risk_result.adjusted_quantity or modified_qty

            # 5-0. 갭/진입가 괴리 보정 — 발주 전 수량 재사이징 (F-16).
            # reference_price(전략 손절 산출 기준가) 대비 live 진입가(price)가 벌어지면
            # risk_per_share가 달라져 의도 risk budget을 초과할 수 있다. 손절가를 원래
            # 비율로 재적용한 값으로 PositionSizer를 재호출해 수량을 재산정하되, 항상
            # 축소(min)만 적용한다(상방 갭이면 리스크 기반 수량이 작아져 자연 채택,
            # 하방 갭이면 원수량 유지로 예산 초과 없음). 수동 주문은 스킵.
            if (
                side == OrderSide.BUY
                and not manual
                and reference_price is not None
                and reference_price > 0
            ):
                rescaled = self._rescale_exit_prices(
                    reference_price=reference_price,
                    stop0=trade_decision.stop_loss_price,
                    tp0=trade_decision.take_profit_price,
                    target_price=price,
                )
                if rescaled is not None:
                    scaled_stop, scaled_tp = rescaled
                    try:
                        size_state = await self._portfolio_service.get_current_state()
                        sizing = PositionSizer(self._settings).calculate(
                            symbol=symbol,
                            entry_price=price,
                            stop_loss_price=scaled_stop,
                            take_profit_price=scaled_tp,
                            total_portfolio_value=size_state.total_value,
                        )
                        if 0 < sizing.quantity < effective_quantity:
                            logger.info(
                                "executor.gap_resize",
                                symbol=symbol,
                                reference_price=str(reference_price),
                                live_price=str(price),
                                original_qty=effective_quantity,
                                resized_qty=sizing.quantity,
                                account_id=account_id,
                            )
                            effective_quantity = sizing.quantity
                    except Exception:
                        logger.warning(
                            "executor.gap_resize_failed", symbol=symbol, exc_info=True
                        )

            # 5-a. Cash Gate (미수 방지): place_order 직전 브로커 주문가능현금 확인.
            # dnca_tot_amt(예수금총액)는 D+2 정산 전이라 당일 매수분을 차감하지
            # 않음 → 실제 가용은 TTTC8908R의 nrcvb_buy_amt만 정확. 초과 시 모드별
            # 처리: reject=차단, shrink=수량 축소, off=비활성.
            if side == OrderSide.BUY:
                gate_mode = str(
                    self._settings.ORDER_CASH_GATE_MODE or "reject"
                ).lower()
                if gate_mode != "off":
                    buyable = await effective_broker.get_buyable_cash(symbol, price)
                    order_total = Decimal(effective_quantity) * price

                    if order_total > buyable:
                        max_qty_by_cash = int(buyable / price) if price > 0 else 0

                        if gate_mode == "shrink" and max_qty_by_cash >= 1:
                            logger.warning(
                                "executor.cash_gate.shrink",
                                symbol=symbol,
                                original_qty=effective_quantity,
                                adjusted_qty=max_qty_by_cash,
                                order_total=str(order_total),
                                buyable=str(buyable),
                                account_id=account_id,
                            )
                            did = await self._record_decision_safe(
                                session_id=session_id, stage=DecisionStage.EXECUTION,
                                decision=DecisionAction.APPROVE, symbol=symbol,
                                reasoning=(
                                    f"Cash gate 축소: {effective_quantity}주→"
                                    f"{max_qty_by_cash}주 (가용={buyable})"
                                ),
                                parent_id=parent_decision_id,
                                account_id=account_id,
                                data_snapshot={
                                    "order_id": order.id,
                                    "gate_mode": gate_mode,
                                    "original_qty": effective_quantity,
                                    "adjusted_qty": max_qty_by_cash,
                                    "order_total_krw": str(order_total),
                                    "buyable_krw": str(buyable),
                                },
                            )
                            if did:
                                decision_ids.append(did)
                            effective_quantity = max_qty_by_cash
                        else:
                            # reject 모드 또는 shrink인데 1주도 불가능
                            reason = (
                                f"주문가능현금 부족 — 필요={order_total:,} KRW, "
                                f"가용={buyable:,} KRW"
                            )
                            logger.warning(
                                "executor.cash_gate.reject",
                                symbol=symbol,
                                quantity=effective_quantity,
                                order_total=str(order_total),
                                buyable=str(buyable),
                                account_id=account_id,
                            )
                            await self._update_order(
                                order.id,
                                status=OrderStatus.CANCELLED,
                                rejection_reason=reason,
                            )
                            await self._notify_safe(
                                MessageTemplates.rejection_notification(
                                    account_label=account_label,
                                    symbol=symbol, name=symbol, side=side,
                                    reason=reason, stage="cash_gate",
                                )
                            )
                            did = await self._record_decision_safe(
                                session_id=session_id, stage=DecisionStage.EXECUTION,
                                decision=DecisionAction.REJECT, symbol=symbol,
                                reasoning=reason,
                                parent_id=parent_decision_id,
                                account_id=account_id,
                                data_snapshot={
                                    "order_id": order.id,
                                    "gate_mode": gate_mode,
                                    "order_total_krw": str(order_total),
                                    "buyable_krw": str(buyable),
                                },
                            )
                            if did:
                                decision_ids.append(did)
                            return self._fail_result(
                                order=order, symbol=symbol, side=side,
                                quantity=effective_quantity,
                                approval_status=approval_status,
                                web_verify_result=verification.result,
                                decision_ids=decision_ids,
                                error=reason,
                            )

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

            # 6-a. 명시적 실패 — REJECTED/CANCELLED/FAILED
            if order_result.status in _HARD_FAIL_STATUSES:
                await self._update_order(
                    order.id, status=OrderStatus.FAILED,
                    rejection_reason=f"브로커 주문 실패: {order_result.status.value}",
                    broker_order_id=order_result.order_id or None,
                )
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=effective_quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"브로커 주문 실패: {order_result.status.value}",
                )

            # 6-b. 즉시 체결 — Mock broker / 일부 실제 브로커
            if order_result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                return await self._finalize_entry_fill(
                    order=order, order_result=order_result,
                    symbol=symbol, side=side, quantity=effective_quantity, price=price,
                    strategy_type=strategy_type, trade_decision=trade_decision,
                    session_id=session_id, parent_decision_id=parent_decision_id,
                    account_id=account_id, account_label=account_label,
                    verification=verification, approval_status=approval_status,
                    decision_ids=decision_ids, reference_price=reference_price,
                )

            # 6-c. SUBMITTED — KIS 실제 주문의 정상 경로
            await self._update_order(
                order.id, status=OrderStatus.SUBMITTED,
                broker_order_id=order_result.order_id,
            )
            await self._notify_safe(MessageTemplates.submission_notification(
                account_label=account_label,
                symbol=symbol, name=symbol, side=side,
                quantity=effective_quantity, price=price,
                approval_status=approval_status,
                broker_order_id=order_result.order_id,
            ))
            submission_action = (
                DecisionAction.BUY if side == OrderSide.BUY else DecisionAction.SELL
            )
            did = await self._record_decision_safe(
                session_id=session_id, stage=DecisionStage.EXECUTION,
                decision=submission_action, symbol=symbol,
                reasoning=f"접수(미체결): {effective_quantity}주 @ {price}",
                parent_id=parent_decision_id,
                account_id=account_id,
                data_snapshot={
                    "order_id": order.id,
                    "broker_order_id": order_result.order_id,
                    "status": OrderStatus.SUBMITTED.value,
                },
            )
            if did:
                decision_ids.append(did)

            # 6-d. WS 체결통보 대기 — stream 연결 시 finalize까지 동기 대기
            if (
                self._execution_stream is not None
                and order_result.order_id
            ):
                try:
                    event = await self._execution_stream.wait_for_fill(
                        broker_order_id=order_result.order_id,
                        account_id=account_id,
                        timeout=self._settings.ORDER_FILL_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "executor.entry_fill_timeout",
                        order_id=order.id,
                        broker_order_id=order_result.order_id,
                        timeout_sec=self._settings.ORDER_FILL_TIMEOUT_SEC,
                    )
                    self._reserve_batch_entry(
                        batch_reservation, symbol=symbol, sector=sector,
                        quantity=effective_quantity, price=price,
                        is_new_holding=is_new_holding,
                    )
                    return self._pending_result(
                        order=order, order_result=order_result,
                        symbol=symbol, side=side, quantity=effective_quantity,
                        approval_status=approval_status,
                        web_verify_result=verification.result,
                        decision_ids=decision_ids,
                    )

                if event.is_rejected:
                    await self._update_order(
                        order.id, status=OrderStatus.REJECTED,
                        rejection_reason=event.rejected_reason or "KIS 거부",
                    )
                    await self._notify_safe(MessageTemplates.rejection_notification(
                        account_label=account_label,
                        symbol=symbol, name=symbol, side=side,
                        reason=event.rejected_reason or "KIS 거부",
                        stage="risk_blocked",
                    ))
                    return self._fail_result(
                        order=order, symbol=symbol, side=side, quantity=effective_quantity,
                        approval_status=approval_status,
                        web_verify_result=verification.result, decision_ids=decision_ids,
                        error=f"KIS 거부: {event.rejected_reason or '-'}",
                    )

                # 체결이 아닌 이벤트(접수/정정 통보)면 pending 처리
                if not event.is_filled:
                    logger.warning(
                        "executor.entry_event_not_filled",
                        order_id=order.id,
                        broker_order_id=order_result.order_id,
                    )
                    self._reserve_batch_entry(
                        batch_reservation, symbol=symbol, sector=sector,
                        quantity=effective_quantity, price=price,
                        is_new_holding=is_new_holding,
                    )
                    return self._pending_result(
                        order=order, order_result=order_result,
                        symbol=symbol, side=side, quantity=effective_quantity,
                        approval_status=approval_status,
                        web_verify_result=verification.result,
                        decision_ids=decision_ids,
                    )

                filled_result = _event_to_order_result(
                    event=event, submitted=order_result, fallback_quantity=effective_quantity,
                )
                return await self._finalize_entry_fill(
                    order=order, order_result=filled_result,
                    symbol=symbol, side=side, quantity=effective_quantity, price=price,
                    strategy_type=strategy_type, trade_decision=trade_decision,
                    session_id=session_id, parent_decision_id=parent_decision_id,
                    account_id=account_id, account_label=account_label,
                    verification=verification, approval_status=approval_status,
                    decision_ids=decision_ids, reference_price=reference_price,
                )

            # 6-e. WS 미연결 — 체결은 reconciler가 처리
            self._reserve_batch_entry(
                batch_reservation, symbol=symbol, sector=sector,
                quantity=effective_quantity, price=price,
                is_new_holding=is_new_holding,
            )
            return self._pending_result(
                order=order, order_result=order_result,
                symbol=symbol, side=side, quantity=effective_quantity,
                approval_status=approval_status,
                web_verify_result=verification.result,
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

    @staticmethod
    def _rescale_exit_prices(
        *,
        reference_price: Decimal,
        stop0: Decimal | None,
        tp0: Decimal | None,
        target_price: Decimal,
    ) -> tuple[Decimal, Decimal | None] | None:
        """진입가 괴리 시 손절·익절을 원래 비율로 재적용(F-16).

        reference_price(전략이 stop0/tp0를 산출한 기준가) 대비 손절/익절 비율을
        target_price(live 진입가 또는 체결가)에 그대로 곱해 의도한 R:R·리스크%를
        보존한다. 비율을 보존하므로 손절 *폭(%)* 은 변하지 않고(좁아지지 않고)
        절대가만 진입가에 맞춰 따라간다.

        입력이 비정상(기준가/체결가 비양수, 손절가 결측, 손절 비율이 0..1 밖,
        역전된 손절가)이면 ``None`` 을 반환해 호출자가 원본을 유지하게 한다.

        Returns
        -------
        (new_stop, new_tp) — new_tp는 익절가 결측/비정상이면 None.
        """
        if (
            reference_price <= 0
            or target_price <= 0
            or stop0 is None
            or stop0 <= 0
        ):
            return None
        p_stop = (reference_price - stop0) / reference_price
        # 정상 롱 진입은 0 < p_stop < 1 (손절가가 기준가보다 낮음). 벗어나면 보류.
        if not (Decimal("0") < p_stop < Decimal("1")):
            return None
        new_stop = target_price * (Decimal("1") - p_stop)

        new_tp: Decimal | None = None
        if tp0 is not None and tp0 > 0:
            p_tp = (tp0 - reference_price) / reference_price
            if p_tp > 0:
                new_tp = target_price * (Decimal("1") + p_tp)
        return new_stop, new_tp

    async def _finalize_entry_fill(
        self,
        *,
        order: Order,
        order_result: OrderResult,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        strategy_type: str,
        trade_decision: TradeDecision,
        session_id: UUID,
        parent_decision_id: UUID | None,
        account_id: str,
        account_label: str,
        verification: WebVerification,
        approval_status: ApprovalStatus,
        decision_ids: list[UUID],
        reference_price: Decimal | None = None,
    ) -> ExecutionResult:
        """진입 주문 체결 확정 후처리 — 체결기록·포지션생성·알림·audit."""
        now = datetime.now(UTC)
        fill_price = order_result.filled_price or price
        fill_quantity = order_result.filled_quantity or quantity
        commission = order_result.commission
        # F-03: 부분 체결이면 최종 상태를 PARTIALLY_FILLED로 (체결 수량 기준 포지션 생성)
        final_status = (
            OrderStatus.PARTIALLY_FILLED
            if order_result.status == OrderStatus.PARTIALLY_FILLED
            or fill_quantity < quantity
            else OrderStatus.FILLED
        )

        await self._record_execution(
            order_id=order.id,
            broker_order_id=order_result.order_id,
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            commission=commission,
            executed_at=now,
        )

        # F-28 방어: 진입 경로로 유입된 매도 체결은 절대 포지션을 만들지 않는다.
        # 수동 매도는 execute_exit(포지션 해석 + position_id 부착)로 통일됐으므로
        # 여기에 SELL이 도달하면 배선 결함이다. 브로커 체결은 되돌릴 수 없으니
        # executions/orders 기록은 남기고 포지션 생성만 차단한 뒤 실패로 반환해
        # 사람 개입을 유도한다(포지션 생성 실패 핸들러와 동일한 원칙).
        if side != OrderSide.BUY:
            logger.critical(
                "executor.entry_finalize_sell_side",
                order_id=order.id, symbol=symbol, account_id=account_id,
                fill_quantity=fill_quantity, fill_price=str(fill_price),
            )
            await self._notify_safe(
                f"<b>[긴급] 진입 경로에 매도 체결 유입(F-28)</b>\n"
                f"종목: {symbol}\n수량: {fill_quantity}주 @ {fill_price:,}원\n"
                f"포지션 생성을 차단했습니다. 브로커 체결은 완료 — 즉시 수동 확인 필요"
            )
            await self._update_order(
                order.id,
                status=final_status,
                broker_order_id=order_result.order_id,
                filled_quantity=fill_quantity,
                filled_price=fill_price,
                commission=commission,
                executed_at=now,
                rejection_reason="진입 경로 매도 체결 — 포지션 미생성(F-28), 수동 확인 필요",
            )
            did = await self._record_decision_safe(
                session_id=session_id,
                stage=DecisionStage.EXECUTION,
                decision=DecisionAction.REJECT,
                symbol=symbol,
                reasoning="진입 경로로 유입된 매도 체결 — 포지션 생성 차단(F-28)",
                parent_id=parent_decision_id,
                account_id=account_id,
                data_snapshot={
                    "order_id": order.id,
                    "broker_order_id": order_result.order_id,
                    "fill_quantity": fill_quantity,
                    "fill_price": str(fill_price),
                },
            )
            if did:
                decision_ids.append(did)
            return self._fail_result(
                order=order, symbol=symbol, side=side, quantity=fill_quantity,
                approval_status=approval_status,
                web_verify_result=verification.result, decision_ids=decision_ids,
                error="진입 경로 매도 체결 — 포지션 생성 차단(F-28), 수동 확인 필요",
            )

        # 손절/익절가: 체결가 기준 비율 재적용(F-16) > 명시적 값 > 설정 기반 기본값.
        # reference_price가 주어지면 실제 체결가(fill_price) 대비 원래 비율로 손절/익절을
        # 재산정해 갭/체결가 괴리에서도 의도한 R:R·리스크%를 보존한다.
        stop_loss = trade_decision.stop_loss_price
        take_profit = trade_decision.take_profit_price
        if reference_price is not None and reference_price > 0:
            rescaled = self._rescale_exit_prices(
                reference_price=reference_price,
                stop0=trade_decision.stop_loss_price,
                tp0=trade_decision.take_profit_price,
                target_price=fill_price,
            )
            if rescaled is not None:
                stop_loss, rescaled_tp = rescaled
                if rescaled_tp is not None:
                    take_profit = rescaled_tp
        if not stop_loss or stop_loss <= 0:
            default_sl_pct = Decimal(str(self._settings.STOP_LOSS_PERCENT))
            stop_loss = fill_price * (Decimal("1") - default_sl_pct / Decimal("100"))
            logger.info(
                "executor.default_stop_loss",
                symbol=symbol, stop_loss=str(stop_loss), pct=str(default_sl_pct),
            )

        # F-10: 전략 상수로 트레일링/시간 청산 파라미터를 포지션에 주입(라이브 배선).
        # 두 값이 비-NULL이어야 폴링·WS·exit_checker의 트레일링/시간 청산 게이트가
        # 실제로 동작한다(인프라는 이미 두 값을 대기 중).
        trailing_stop_pct, max_holding_days = entry_trailing_params(
            strategy_type, fill_price
        )

        position_id: int | None = None
        try:
            pos = await self._position_manager.create(
                symbol=symbol,
                strategy_type=strategy_type,
                quantity=fill_quantity,
                entry_price=fill_price,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                trailing_stop_pct=trailing_stop_pct,
                max_holding_days=max_holding_days,
                entry_session_id=session_id,
                account_id=account_id,
                entry_analysis_snapshot=order.entry_analysis_snapshot,
            )
            position_id = pos.id
        except Exception:
            logger.critical(
                "executor.position_create_failed",
                order_id=order.id, symbol=symbol, exc_info=True,
            )
            await self._notify_safe(
                f"<b>[긴급] 포지션 생성 실패</b>\n"
                f"종목: {symbol}\n수량: {fill_quantity}주 @ {fill_price:,}원\n"
                f"브로커 체결 완료, DB 포지션 미기록\n"
                f"즉시 수동 확인 필요"
            )
            await self._update_order(
                order.id,
                status=final_status,
                broker_order_id=order_result.order_id,
                filled_quantity=fill_quantity,
                filled_price=fill_price,
                commission=commission,
                executed_at=now,
                rejection_reason="포지션 생성 실패 — 수동 확인 필요",
            )
            return self._fail_result(
                order=order, symbol=symbol, side=side, quantity=fill_quantity,
                approval_status=approval_status,
                web_verify_result=verification.result, decision_ids=decision_ids,
                error="포지션 생성 실패 — 브로커 체결됨, DB 미기록",
            )

        await self._update_order(
            order.id,
            status=final_status,
            broker_order_id=order_result.order_id,
            filled_quantity=fill_quantity,
            filled_price=fill_price,
            commission=commission,
            executed_at=now,
            position_id=position_id,
        )

        await self._notify_safe(MessageTemplates.execution_notification(
            account_label=account_label,
            symbol=symbol, name=symbol, side=side,
            quantity=fill_quantity, fill_price=fill_price,
            commission=commission, approval_status=approval_status,
        ))

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

    @staticmethod
    def _resolve_sellable_quantity(
        quantity: int, sellable: int | None, *, is_stop_loss: bool
    ) -> int | None:
        """매도 preflight 결정 — 발주 수량을 매도가능수량으로 클램프(F-12).

        Returns:
            발주할 수량(클램프 가능). ``None``이면 매도 스킵.

        규칙(사용자 확정: 클램프 + 손절 예외):
        - ``sellable is None``(조회 불가/실패/모의) → 원수량 유지(클램프 안 함).
        - ``sellable >= quantity`` → 원수량 유지.
        - ``0 < sellable < quantity`` → ``sellable``로 축소(클램프).
        - ``sellable <= 0`` → 긴급 손절은 원수량 유지(스킵 안 함, 스트랜딩 방지),
          그 외는 ``None``(스킵).
        """
        if sellable is None or sellable >= quantity:
            return quantity
        if sellable > 0:
            return sellable
        return quantity if is_stop_loss else None

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
        exit_quantity: int | None = None,
        order_type_override: OrderType | None = None,
        manual: bool = False,
    ) -> ExecutionResult:
        """청산 주문 실행.

        ExitSignal → Web 검증 → 승인 → 브로커 주문 → 포지션 청산.
        manual=True면 웹검증/승인을 생략한다(백오피스 수동 청산).

        Parameters
        ----------
        exit_signal: 청산 시그널 (ExitConditionChecker 출력).
        position: 청산 대상 포지션 (DB ORM).
        session_id: 파이프라인 세션 ID.
        parent_decision_id: 부모 decision_log ID.
        order_type_override: 주문유형 강제(F-28 수동 매도의 시장가 지원). 손절+즉시의
            MARKET은 안전 하한이라 override로 낮추지 않는다.
        """
        symbol = exit_signal.symbol
        reason = exit_signal.reason
        action = _EXIT_REASON_TO_ACTION.get(reason, DecisionAction.SELL)
        side = OrderSide.SELL
        is_stop_loss = reason == ExitReason.STOP_LOSS

        # 손절+즉시 → MARKET(미체결 방치 방지용 안전 하한. override로 낮추지 않는다),
        # 그 외 → 호출자 override(F-28 REST 수동 시장가 매도) > LIMIT(기본).
        if is_stop_loss and exit_signal.urgency == "immediate":
            order_type = OrderType.MARKET
        else:
            order_type = order_type_override or OrderType.LIMIT
        price = exit_signal.current_price
        # 부분 청산 지원(B-01 수동 매도): 지정 수량이 없거나 잔량 초과면 전량.
        quantity = position.quantity
        if exit_quantity is not None and 0 < exit_quantity < position.quantity:
            quantity = exit_quantity
        decision_ids: list[UUID] = []
        order: Order | None = None
        effective_broker = broker or self._broker

        try:
            # 1. TradeDecision 구성 (ApprovalManager 인터페이스용)
            trade_decision = self._build_exit_trade_decision(exit_signal, position)

            # 2. 주문 생성 — position_id를 미리 설정(WS/reconciler가 청산 소스 참조)
            order = await self._create_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                session_id=session_id,
                account_id=account_id,
                position_id=position.id,
            )

            # 3. Web 검증 — manual=True면 생략(백오피스 수동 청산)
            if manual:
                verification = WebVerification(
                    symbol=symbol,
                    result=WebVerifyResult.SAFE,
                    summary="수동 청산: 웹검증 생략",
                )
            else:
                # 진입 경로와 동일하게 종목명을 넘겨 웹검색 정확도 확보(F-19).
                company_name = await self._lookup_company_name(symbol)
                verification = await self._web_verifier.verify(
                    symbol=symbol,
                    side=side,
                    session_id=session_id,
                    parent_decision_id=parent_decision_id,
                    is_stop_loss=is_stop_loss,
                    company_name=company_name,
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

            # 4. 포트폴리오 상태 + 승인 — manual=True면 생략하고 자동 승인
            if manual:
                approval_status = ApprovalStatus.AUTO_APPROVED
            else:
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

            # 5-pre. 매도 preflight (F-12): 매도가능수량(ord_psbl_qty) 확인 후 클램프.
            # 보유수량과 달리 미체결 매도주문·결제미수로 줄어든 실제 매도가능수량을
            # 초과하면 KIS가 거부하므로 사전 클램프. 조회 불가(None)면 미적용.
            # 긴급 손절은 절대 스킵하지 않는다(포지션 스트랜딩 방지).
            sellable = await effective_broker.get_sellable_quantity(symbol)
            # 계약은 int|None이나, 브로커 구현 이상 시에도 청산이 깨지지 않도록
            # 비-int는 None(=preflight 미적용)으로 폴백(fail-open).
            if not isinstance(sellable, int) or isinstance(sellable, bool):
                sellable = None
            resolved_qty = self._resolve_sellable_quantity(
                quantity, sellable, is_stop_loss=is_stop_loss
            )
            if resolved_qty is None:
                reason_msg = f"매도가능수량 부족 — 매도가능=0, 요청={quantity}주"
                logger.warning(
                    "executor.sell_preflight.skip",
                    symbol=symbol, quantity=quantity, sellable=sellable,
                    account_id=account_id,
                )
                await self._update_order(
                    order.id, status=OrderStatus.CANCELLED,
                    rejection_reason=reason_msg,
                )
                await self._notify_safe(MessageTemplates.rejection_notification(
                    account_label=account_label,
                    symbol=symbol, name=symbol, side=side,
                    reason=reason_msg, stage="sell_preflight",
                ))
                did = await self._record_decision_safe(
                    session_id=session_id, stage=DecisionStage.EXIT,
                    decision=DecisionAction.REJECT, symbol=symbol,
                    reasoning=reason_msg,
                    parent_id=parent_decision_id,
                    account_id=account_id,
                    data_snapshot={
                        "order_id": order.id,
                        "exit_reason": reason.value,
                        "sellable_qty": sellable,
                        "requested_qty": quantity,
                    },
                )
                if did:
                    decision_ids.append(did)
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result,
                    decision_ids=decision_ids,
                    error=reason_msg,
                )
            if resolved_qty != quantity:
                logger.warning(
                    "executor.sell_preflight.clamp",
                    symbol=symbol, original_qty=quantity,
                    adjusted_qty=resolved_qty, sellable=sellable,
                    account_id=account_id,
                )
                did = await self._record_decision_safe(
                    session_id=session_id, stage=DecisionStage.EXIT,
                    decision=action, symbol=symbol,
                    reasoning=(
                        f"매도가능수량 클램프: {quantity}주→{resolved_qty}주 "
                        f"(매도가능={sellable})"
                    ),
                    parent_id=parent_decision_id,
                    account_id=account_id,
                    data_snapshot={
                        "order_id": order.id,
                        "exit_reason": reason.value,
                        "original_qty": quantity,
                        "adjusted_qty": resolved_qty,
                        "sellable_qty": sellable,
                    },
                )
                if did:
                    decision_ids.append(did)
                quantity = resolved_qty

            # 5. 브로커 주문
            order_result = await effective_broker.place_order(OrderRequest(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                account_id=account_id,
            ))

            # 5-a. 명시적 실패
            if order_result.status in _HARD_FAIL_STATUSES:
                await self._update_order(
                    order.id, status=OrderStatus.FAILED,
                    rejection_reason=f"브로커 주문 실패: {order_result.status.value}",
                    broker_order_id=order_result.order_id or None,
                )
                return self._fail_result(
                    order=order, symbol=symbol, side=side, quantity=quantity,
                    approval_status=approval_status,
                    web_verify_result=verification.result, decision_ids=decision_ids,
                    error=f"브로커 주문 실패: {order_result.status.value}",
                )

            # 5-b. 즉시 체결 (Mock broker 등)
            if order_result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                return await self._finalize_exit_fill(
                    order=order, position=position, order_result=order_result,
                    symbol=symbol, side=side, quantity=quantity, price=price,
                    reason=reason, action=action, session_id=session_id,
                    parent_decision_id=parent_decision_id,
                    account_id=account_id, account_label=account_label,
                    verification=verification, approval_status=approval_status,
                    decision_ids=decision_ids,
                )

            # 5-c. SUBMITTED — 접수 통보 + WS/reconciler 대기
            await self._update_order(
                order.id, status=OrderStatus.SUBMITTED,
                broker_order_id=order_result.order_id,
            )
            await self._notify_safe(MessageTemplates.submission_notification(
                account_label=account_label,
                symbol=symbol, name=symbol, side=side,
                quantity=quantity, price=price,
                approval_status=approval_status,
                broker_order_id=order_result.order_id,
            ))
            did = await self._record_decision_safe(
                session_id=session_id, stage=DecisionStage.EXIT,
                decision=action, symbol=symbol,
                reasoning=f"청산 접수(미체결): {reason.value} {quantity}주 @ {price}",
                parent_id=parent_decision_id,
                account_id=account_id,
                data_snapshot={
                    "order_id": order.id,
                    "broker_order_id": order_result.order_id,
                    "exit_reason": reason.value,
                    "status": OrderStatus.SUBMITTED.value,
                },
            )
            if did:
                decision_ids.append(did)

            if (
                self._execution_stream is not None
                and order_result.order_id
            ):
                try:
                    event = await self._execution_stream.wait_for_fill(
                        broker_order_id=order_result.order_id,
                        account_id=account_id,
                        timeout=self._settings.ORDER_FILL_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "executor.exit_fill_timeout",
                        order_id=order.id,
                        broker_order_id=order_result.order_id,
                        timeout_sec=self._settings.ORDER_FILL_TIMEOUT_SEC,
                    )
                    return self._pending_result(
                        order=order, order_result=order_result,
                        symbol=symbol, side=side, quantity=quantity,
                        approval_status=approval_status,
                        web_verify_result=verification.result,
                        decision_ids=decision_ids,
                    )

                if event.is_rejected:
                    await self._update_order(
                        order.id, status=OrderStatus.REJECTED,
                        rejection_reason=event.rejected_reason or "KIS 거부",
                    )
                    await self._notify_safe(MessageTemplates.rejection_notification(
                        account_label=account_label,
                        symbol=symbol, name=symbol, side=side,
                        reason=event.rejected_reason or "KIS 거부",
                        stage="risk_blocked",
                    ))
                    return self._fail_result(
                        order=order, symbol=symbol, side=side, quantity=quantity,
                        approval_status=approval_status,
                        web_verify_result=verification.result, decision_ids=decision_ids,
                        error=f"KIS 거부: {event.rejected_reason or '-'}",
                    )

                # 체결이 아닌 이벤트(접수/정정 통보)면 pending 처리
                if not event.is_filled:
                    logger.warning(
                        "executor.exit_event_not_filled",
                        order_id=order.id,
                        broker_order_id=order_result.order_id,
                    )
                    return self._pending_result(
                        order=order, order_result=order_result,
                        symbol=symbol, side=side, quantity=quantity,
                        approval_status=approval_status,
                        web_verify_result=verification.result,
                        decision_ids=decision_ids,
                    )

                filled_result = _event_to_order_result(
                    event=event, submitted=order_result, fallback_quantity=quantity,
                )
                return await self._finalize_exit_fill(
                    order=order, position=position, order_result=filled_result,
                    symbol=symbol, side=side, quantity=quantity, price=price,
                    reason=reason, action=action, session_id=session_id,
                    parent_decision_id=parent_decision_id,
                    account_id=account_id, account_label=account_label,
                    verification=verification, approval_status=approval_status,
                    decision_ids=decision_ids,
                )

            return self._pending_result(
                order=order, order_result=order_result,
                symbol=symbol, side=side, quantity=quantity,
                approval_status=approval_status,
                web_verify_result=verification.result,
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

    async def _finalize_exit_fill(
        self,
        *,
        order: Order,
        position: PositionRecord,
        order_result: OrderResult,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        reason: ExitReason,
        action: DecisionAction,
        session_id: UUID,
        parent_decision_id: UUID | None,
        account_id: str,
        account_label: str,
        verification: WebVerification,
        approval_status: ApprovalStatus,
        decision_ids: list[UUID],
    ) -> ExecutionResult:
        """청산 주문 체결 확정 후처리 — 체결기록·포지션청산·알림·audit."""
        now = datetime.now(UTC)
        fill_price = order_result.filled_price or price
        fill_quantity = order_result.filled_quantity or quantity
        commission = order_result.commission
        # F-03: 주문 상태는 주문 수량 대비 체결 여부로, 포지션 청산은 포지션 잔량
        # 대비 체결 수량으로 판단한다(부분 매도 시 잔량 open 유지).
        final_status = (
            OrderStatus.PARTIALLY_FILLED
            if order_result.status == OrderStatus.PARTIALLY_FILLED
            or fill_quantity < quantity
            else OrderStatus.FILLED
        )
        position_partial = fill_quantity < position.quantity

        await self._record_execution(
            order_id=order.id,
            broker_order_id=order_result.order_id,
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            commission=commission,
            executed_at=now,
        )

        try:
            if position_partial:
                await self._position_manager.reduce(
                    position.id,
                    exit_quantity=fill_quantity,
                    exit_price=fill_price,
                    exit_reason=reason,
                    exit_session_id=session_id,
                )
            else:
                await self._position_manager.close(
                    position.id,
                    exit_price=fill_price,
                    exit_reason=reason,
                    exit_session_id=session_id,
                )
        except Exception:
            logger.critical(
                "executor.position_close_failed",
                order_id=order.id, position_id=position.id,
                symbol=symbol, exc_info=True,
            )
            await self._notify_safe(
                f"<b>[긴급] 포지션 청산 DB 실패</b>\n"
                f"종목: {symbol}\n수량: {fill_quantity}주 @ {fill_price:,}원\n"
                f"브로커 매도 체결 완료, DB 포지션 미청산\n"
                f"즉시 수동 확인 필요"
            )

        # F-10 Phase 2: 부분익절 체결 → 잔량을 트레일링 모드로 전환(TP 소거 +
        # 본전 플로어). reduce 성공과 분리(전환 실패가 부분매도 기록을 무효화하지
        # 않도록). 전환 실패 시 다음 사이클에 TP가 남아 재시도된다.
        if position_partial and reason == ExitReason.PARTIAL_TAKE_PROFIT:
            try:
                await self._position_manager.transition_to_trailing(
                    position.id, break_even_price=position.avg_cost,
                )
            except Exception:
                logger.warning(
                    "executor.partial_tp_transition_failed",
                    order_id=order.id, position_id=position.id,
                    symbol=symbol, exc_info=True,
                )

        await self._update_order(
            order.id,
            status=final_status,
            broker_order_id=order_result.order_id,
            filled_quantity=fill_quantity,
            filled_price=fill_price,
            commission=commission,
            executed_at=now,
            position_id=position.id,
        )

        await self._notify_safe(MessageTemplates.execution_notification(
            account_label=account_label,
            symbol=symbol, name=symbol, side=side,
            quantity=fill_quantity, fill_price=fill_price,
            commission=commission, approval_status=approval_status,
        ))

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

    # ── Private: DB Helpers ───────────────────────────────────────────────

    async def _lookup_company_name(self, symbol: str) -> str | None:
        """웹 검증 프롬프트 정확도용 종목명(StockMaster.name) 조회. 실패 시 None."""
        try:
            async with self._session_factory() as _sess:
                _row = await _sess.execute(
                    select(StockMaster.name).where(StockMaster.symbol == symbol)
                )
                return _row.scalar()
        except Exception:
            logger.warning("executor.company_name_lookup_failed", symbol=symbol)
            return None

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
        position_id: int | None = None,
        entry_analysis_snapshot: dict | None = None,
    ) -> Order:
        """orders 테이블에 PENDING 주문 생성.

        Parameters
        ----------
        position_id: 청산 주문일 때 원 포지션 ID. 진입 주문은 체결 후 별도 업데이트.
        entry_analysis_snapshot: 진입 분석 스냅샷(메모리 학습용). 체결 시 포지션으로 복사된다.
        """
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
            position_id=position_id,
            entry_analysis_snapshot=entry_analysis_snapshot,
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

    def _reserve_batch_entry(
        self,
        reservation: BatchReservation | None,
        *,
        symbol: str,
        sector: str,
        quantity: int,
        price: Decimal,
        is_new_holding: bool,
    ) -> None:
        """접수(미체결) 진입을 배치 예약에 누적한다 (F-04).

        체결 전 in-flight 상태에서만 호출한다. 동기 체결(즉시 FILLED)은 DB에
        ``PositionRecord``가 생성되어 다음 후보가 DB로 카운트하므로 예약하지 않는다
        (이중 카운트 방지). ``reservation``이 None이면 no-op.
        """
        if reservation is None:
            return
        reservation.reserve(
            symbol,
            sector,
            Decimal(quantity) * price,
            is_new_holding=is_new_holding,
        )

    async def _lookup_sector(self, symbol: str) -> str:
        """StockMaster에서 종목 섹터를 조회한다. 실패/미존재 시 빈 문자열."""
        try:
            async with self._session_factory() as session:
                row = await session.execute(
                    select(StockMaster.sector).where(StockMaster.symbol == symbol)
                )
                return row.scalar() or ""
        except Exception:
            logger.warning("executor.sector_lookup_failed", symbol=symbol)
            return ""

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
    def _pending_result(
        *,
        order: Order,
        order_result: OrderResult,
        symbol: str,
        side: OrderSide,
        quantity: int,
        approval_status: ApprovalStatus,
        web_verify_result: WebVerifyResult | None,
        decision_ids: list[UUID],
    ) -> ExecutionResult:
        """SUBMITTED 후 체결 미확정 — pending=True, success=True로 반환.

        체결 확정은 이후 ExecutionStreamManager(WS) 또는 OrderReconciler가 담당한다.
        호출자는 `result.pending`을 확인하여 후속 작업 분기를 결정할 수 있다.
        """
        return ExecutionResult(
            success=True,
            pending=True,
            order_id=order.id,
            broker_order_id=order_result.order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            approval_status=approval_status,
            web_verify_result=web_verify_result,
            decision_ids=decision_ids,
        )

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
        terminal: bool = False,
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
            terminal=terminal,
        )
