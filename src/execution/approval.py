"""매매 승인 워크플로우 관리자.

주문별 자동 실행/수동 승인 결정 → 텔레그램 승인 요청 → 콜백 처리.
모든 과정을 decision_log에 기록.

상태 머신:
    PENDING → AUTO_APPROVED  (자동 실행 조건 충족)
    PENDING → WAITING        (텔레그램 승인 요청 전송)
    WAITING → APPROVED       (사용자 승인)
    WAITING → APPROVED + modified_quantity  (사용자 수정 승인)
    WAITING → REJECTED       (사용자 거부)
    WAITING → TIMEOUT        (HUMAN_APPROVAL_TIMEOUT_SEC 초과)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.decision_recorder import DecisionRecorder
from src.config import Settings
from src.core.enums import (
    ApprovalStatus,
    DecisionAction,
    DecisionStage,
    OrderSide,
)
from src.core.models import PortfolioState, TradeDecision, WebVerification
from src.data.cache import RedisCache
from src.db.models.execution import ApprovalRequestDB, Order
from src.notification.telegram import TelegramBot
from src.notification.templates import MessageTemplates

logger = structlog.get_logger(__name__)

# DecisionAction → OrderSide 매핑
_ACTION_TO_SIDE: dict[str, OrderSide] = {
    DecisionAction.BUY: OrderSide.BUY,
    DecisionAction.SELL: OrderSide.SELL,
    DecisionAction.STOP_LOSS: OrderSide.SELL,
    DecisionAction.TAKE_PROFIT: OrderSide.SELL,
}

_REDIS_NAMESPACE = "approval"
_REDIS_TTL_BUFFER = 60  # 타임아웃 이후 Redis 키 유지 여유 (초)


class ApprovalManager:
    """매매 승인 워크플로우 관리자.

    자동 실행 조건 판단 → 텔레그램 승인 요청 → 콜백 처리 → 타임아웃.
    """

    def __init__(
        self,
        *,
        telegram_bot: TelegramBot,
        recorder: DecisionRecorder,
        session_factory: async_sessionmaker[AsyncSession],
        cache: RedisCache,
        settings: Settings,
    ) -> None:
        self._bot = telegram_bot
        self._recorder = recorder
        self._session_factory = session_factory
        self._cache = cache
        self._settings = settings

        # 요청별 대기 동기화
        self._pending_events: dict[UUID, asyncio.Event] = {}
        self._pending_results: dict[UUID, ApprovalStatus] = {}
        self._pending_message_ids: dict[UUID, int] = {}
        self._modified_quantities: dict[UUID, int] = {}
        self._pending_account_ids: dict[UUID, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """텔레그램 봇에 콜백 핸들러 등록 + 만료된 승인 요청 정리."""
        self._bot.register_callback_handler(self._handle_callback)

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            # 만료된 pending 요청 조회
            stmt = select(ApprovalRequestDB).where(
                ApprovalRequestDB.status == "pending",
                ApprovalRequestDB.expires_at < now,
            )
            result = await session.execute(stmt)
            expired = result.scalars().all()

            if not expired:
                return

            # ApprovalRequestDB → timeout
            expired_ids = [r.id for r in expired]
            expired_order_ids = [r.order_id for r in expired]

            await session.execute(
                update(ApprovalRequestDB)
                .where(ApprovalRequestDB.id.in_(expired_ids))
                .values(status="timeout", responded_at=now)
            )

            # Order → timeout
            await session.execute(
                update(Order)
                .where(Order.id.in_(expired_order_ids))
                .values(approval_status="timeout")
            )

            await session.commit()
            logger.info(
                "approval_expired_cleanup",
                count=len(expired_ids),
            )

    # ── Public API ────────────────────────────────────────────────────

    async def request_approval(
        self,
        *,
        trade_decision: TradeDecision,
        order_id: int,
        session_id: UUID,
        portfolio_state: PortfolioState,
        web_verification: WebVerification | None = None,
        analysis_summary: str = "",
        name: str = "",
        account_id: str = "default",
        account_nickname: str = "",
        account_label: str = "",
    ) -> ApprovalStatus:
        """승인 요청 처리 — 자동 실행 또는 텔레그램 승인 요청.

        Returns:
            최종 ApprovalStatus.
        """
        request_id = uuid4()
        now = datetime.now(UTC)
        display_name = name or trade_decision.symbol

        # 포지션 금액/비중 계산
        price = trade_decision.price or Decimal("0")
        position_value = Decimal(trade_decision.quantity) * price
        portfolio_pct = (
            (position_value / portfolio_state.total_value * 100)
            if portfolio_state.total_value > 0
            else Decimal("0")
        )

        # ── 자동 실행 판단 ──
        auto_approve, reason = self._check_auto_approve(
            trade_decision, portfolio_state, position_value, portfolio_pct
        )

        if auto_approve:
            return await self._handle_auto_approve(
                request_id=request_id,
                order_id=order_id,
                session_id=session_id,
                trade_decision=trade_decision,
                display_name=display_name,
                position_value=position_value,
                portfolio_pct=portfolio_pct,
                reason=reason,
                now=now,
                account_id=account_id,
                account_label=account_label,
            )

        # ── 수동 승인 ──
        return await self._handle_manual_approve(
            request_id=request_id,
            order_id=order_id,
            session_id=session_id,
            trade_decision=trade_decision,
            portfolio_state=portfolio_state,
            web_verification=web_verification,
            analysis_summary=analysis_summary,
            display_name=display_name,
            position_value=position_value,
            portfolio_pct=portfolio_pct,
            now=now,
            account_id=account_id,
            account_label=account_label,
        )

    async def get_modified_quantity(self, request_id: UUID) -> int | None:
        """수정 승인 시 변경된 수량 조회. None if not modified."""
        return self._modified_quantities.get(request_id)

    # ── Auto-approve Path ─────────────────────────────────────────────

    def _check_auto_approve(
        self,
        trade_decision: TradeDecision,
        portfolio_state: PortfolioState,
        position_value: Decimal,
        portfolio_pct: Decimal,
    ) -> tuple[bool, str]:
        """자동 실행 조건 판단. (auto_approve, reason) 반환."""
        # 조건 1: 수동 승인 비활성화
        if not self._settings.HUMAN_APPROVAL_REQUIRED:
            return True, "human_approval_disabled"

        # 조건 2: 손절/익절
        if trade_decision.action in (
            DecisionAction.STOP_LOSS,
            DecisionAction.TAKE_PROFIT,
        ):
            return True, f"exit_order_{trade_decision.action.value}"

        # 조건 3: 소규모 매도
        if (
            trade_decision.action == DecisionAction.SELL
            and trade_decision.price is not None
            and portfolio_pct <= Decimal(str(self._settings.AUTO_EXECUTE_MAX_PORTFOLIO_PCT))
        ):
            return True, f"small_sell_{portfolio_pct:.1f}pct"

        return False, ""

    async def _handle_auto_approve(
        self,
        *,
        request_id: UUID,
        order_id: int,
        session_id: UUID,
        trade_decision: TradeDecision,
        display_name: str,
        position_value: Decimal,
        portfolio_pct: Decimal,
        reason: str,
        now: datetime,
        account_id: str = "default",
        account_label: str = "",
    ) -> ApprovalStatus:
        """자동 승인 경로: DB 저장 → decision_log → 텔레그램 통보."""
        async with self._session_factory() as session:
            # ApprovalRequestDB 저장
            approval_row = ApprovalRequestDB(
                request_id=request_id,
                order_id=order_id,
                account_id=account_id,
                status="auto_approved",
                requested_at=now,
                responded_at=now,
                expires_at=now,
            )
            session.add(approval_row)

            # Order 상태 업데이트
            await session.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(approval_status="auto_approved")
            )

            await session.commit()

        # decision_log 기록
        await self._recorder.record(
            session_id=session_id,
            stage=DecisionStage.APPROVAL,
            decision=DecisionAction.APPROVE,
            reasoning=f"Auto-approved: {reason}",
            symbol=trade_decision.symbol,
            confidence=trade_decision.confidence,
            account_id=account_id,
            data_snapshot={
                "order_id": order_id,
                "request_id": str(request_id),
                "auto_approve_reason": reason,
                "account_id": account_id,
            },
        )

        # 텔레그램 통보 (승인 요청 아닌 자동 실행 알림)
        side = _ACTION_TO_SIDE.get(trade_decision.action, OrderSide.BUY)
        side_kr = "매수" if side == OrderSide.BUY else "매도"
        label_prefix = f"<b>[{account_label}]</b> " if account_label else ""
        await self._bot.send_message(
            f"{label_prefix}⚡ <b>자동 실행</b> — {display_name} {side_kr} "
            f"{trade_decision.quantity:,}주 ({reason})"
        )

        logger.info(
            "approval_auto_approved",
            request_id=str(request_id),
            order_id=order_id,
            reason=reason,
        )
        return ApprovalStatus.AUTO_APPROVED

    # ── Manual Approve Path ───────────────────────────────────────────

    async def _handle_manual_approve(
        self,
        *,
        request_id: UUID,
        order_id: int,
        session_id: UUID,
        trade_decision: TradeDecision,
        portfolio_state: PortfolioState,
        web_verification: WebVerification | None,
        analysis_summary: str,
        display_name: str,
        position_value: Decimal,
        portfolio_pct: Decimal,
        now: datetime,
        account_id: str = "default",
        account_label: str = "",
    ) -> ApprovalStatus:
        """수동 승인 경로: DB 저장 → Redis → 텔레그램 승인 요청 → 대기."""
        timeout_sec = self._settings.HUMAN_APPROVAL_TIMEOUT_SEC
        expires_at = now + timedelta(seconds=timeout_sec)

        # DB 저장
        async with self._session_factory() as session:
            approval_row = ApprovalRequestDB(
                request_id=request_id,
                order_id=order_id,
                account_id=account_id,
                status="pending",
                requested_at=now,
                expires_at=expires_at,
            )
            session.add(approval_row)
            await session.commit()
            # commit 후 id가 할당됨

        # Redis 상태 저장
        await self._cache.set_json(
            _REDIS_NAMESPACE,
            f"{account_id}:{request_id}",
            {
                "status": "pending",
                "order_id": order_id,
                "account_id": account_id,
                "requested_at": now.isoformat(),
            },
            ttl=timeout_sec + _REDIS_TTL_BUFFER,
        )

        # 텔레그램 승인 요청 메시지
        side = _ACTION_TO_SIDE.get(trade_decision.action, OrderSide.BUY)
        web_summary = ""
        if web_verification:
            web_summary = web_verification.summary if hasattr(web_verification, "summary") else ""

        msg_text = MessageTemplates.approval_request(
            account_label=account_label,
            symbol=trade_decision.symbol,
            name=display_name,
            side=side,
            quantity=trade_decision.quantity,
            price=trade_decision.price or Decimal("0"),
            position_value_krw=position_value,
            portfolio_pct=portfolio_pct,
            stop_loss_price=trade_decision.stop_loss_price,
            take_profit_price=trade_decision.take_profit_price,
            risk_reward_ratio=trade_decision.risk_reward_ratio,
            analysis_summary=analysis_summary,
            web_verify_summary=web_summary,
            session_id=session_id,
        )

        message_id = await self._bot.send_approval_request(msg_text, request_id)

        # 텔레그램 메시지 ID 저장
        if message_id is not None:
            async with self._session_factory() as session:
                await session.execute(
                    update(ApprovalRequestDB)
                    .where(ApprovalRequestDB.request_id == request_id)
                    .values(telegram_message_id=message_id)
                )
                await session.commit()

        # asyncio.Event 대기 설정
        event = asyncio.Event()
        self._pending_events[request_id] = event
        if message_id is not None:
            self._pending_message_ids[request_id] = message_id
        self._pending_account_ids[request_id] = account_id

        # 응답 대기
        status = await self._wait_for_response(request_id, timeout_sec)

        # 타임아웃 처리
        if status == ApprovalStatus.TIMEOUT:
            await self._handle_timeout(
                request_id=request_id,
                order_id=order_id,
                session_id=session_id,
                trade_decision=trade_decision,
                display_name=display_name,
                side=side,
                account_id=account_id,
                account_label=account_label,
            )

        # 정리 (modified_quantities는 유지 — OrderExecutor가 조회)
        self._pending_events.pop(request_id, None)
        self._pending_results.pop(request_id, None)
        self._pending_message_ids.pop(request_id, None)
        self._pending_account_ids.pop(request_id, None)

        logger.info(
            "approval_result",
            request_id=str(request_id),
            order_id=order_id,
            status=status.value,
        )
        return status

    async def _handle_timeout(
        self,
        *,
        request_id: UUID,
        order_id: int,
        session_id: UUID,
        trade_decision: TradeDecision,
        display_name: str,
        side: OrderSide,
        account_id: str = "default",
        account_label: str = "",
    ) -> None:
        """타임아웃 시 DB/텔레그램/decision_log 업데이트."""
        now = datetime.now(UTC)

        # DB 업데이트
        async with self._session_factory() as session:
            await session.execute(
                update(ApprovalRequestDB)
                .where(ApprovalRequestDB.request_id == request_id)
                .values(status="timeout", responded_at=now)
            )
            await session.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(approval_status="timeout")
            )
            await session.commit()

        # Redis 업데이트
        await self._cache.set_json(
            _REDIS_NAMESPACE,
            f"{account_id}:{request_id}",
            {"status": "timeout", "order_id": order_id, "account_id": account_id},
            ttl=_REDIS_TTL_BUFFER,
        )

        # 텔레그램 메시지 업데이트 (버튼 제거)
        message_id = self._pending_message_ids.get(request_id)
        if message_id is not None:
            await self._bot.update_message(
                message_id,
                f"⏰ <b>승인 시간 초과</b> — {display_name}",
                remove_buttons=True,
            )

        # 거부 알림
        await self._bot.send_message(
            MessageTemplates.rejection_notification(
                account_label=account_label,
                symbol=trade_decision.symbol,
                name=display_name,
                side=side,
                reason=f"승인 대기 시간 초과 ({self._settings.HUMAN_APPROVAL_TIMEOUT_SEC}초)",
                stage="approval_timeout",
            )
        )

        # decision_log 기록
        await self._recorder.record(
            session_id=session_id,
            stage=DecisionStage.APPROVAL,
            decision=DecisionAction.REJECT,
            reasoning="Approval timeout",
            symbol=trade_decision.symbol,
            confidence=trade_decision.confidence,
            account_id=account_id,
            data_snapshot={
                "order_id": order_id,
                "request_id": str(request_id),
                "timeout_sec": self._settings.HUMAN_APPROVAL_TIMEOUT_SEC,
                "account_id": account_id,
            },
        )

    # ── Callback Handler ──────────────────────────────────────────────

    async def _handle_callback(self, action: str, request_id: UUID) -> None:
        """텔레그램 콜백 핸들러.

        action: "approve", "reject", "modify:{qty}"
        """
        if request_id not in self._pending_events:
            logger.warning(
                "approval_callback_ignored",
                request_id=str(request_id),
                reason="not_pending",
            )
            return

        # action 파싱
        if action == "approve":
            status = ApprovalStatus.APPROVED
            modified_qty = None
            response_reason = "user_approved"
        elif action == "reject":
            status = ApprovalStatus.REJECTED
            modified_qty = None
            response_reason = "user_rejected"
        elif action.startswith("modify:"):
            try:
                modified_qty = int(action.split(":", 1)[1])
                if modified_qty <= 0:
                    raise ValueError("non-positive")
            except (ValueError, IndexError):
                logger.warning(
                    "approval_invalid_modify",
                    request_id=str(request_id),
                    action=action,
                )
                # 파싱 실패 → REJECTED
                status = ApprovalStatus.REJECTED
                modified_qty = None
                response_reason = f"invalid_modify_action: {action}"
            else:
                status = ApprovalStatus.APPROVED
                response_reason = f"user_modified_quantity:{modified_qty}"
        else:
            logger.warning(
                "approval_unknown_action",
                request_id=str(request_id),
                action=action,
            )
            return

        now = datetime.now(UTC)

        # DB 업데이트
        try:
            async with self._session_factory() as session:
                # ApprovalRequestDB
                update_values: dict = {
                    "status": status.value,
                    "responded_at": now,
                    "response_reason": response_reason,
                }
                if modified_qty is not None:
                    update_values["modified_quantity"] = modified_qty

                await session.execute(
                    update(ApprovalRequestDB)
                    .where(ApprovalRequestDB.request_id == request_id)
                    .values(**update_values)
                )

                # Order
                order_values: dict = {"approval_status": status.value}
                if modified_qty is not None:
                    order_values["modified_quantity"] = modified_qty

                # order_id 조회
                stmt = select(ApprovalRequestDB.order_id).where(
                    ApprovalRequestDB.request_id == request_id
                )
                result = await session.execute(stmt)
                order_id = result.scalar_one_or_none()

                if order_id is not None:
                    await session.execute(
                        update(Order)
                        .where(Order.id == order_id)
                        .values(**order_values)
                    )

                await session.commit()
        except Exception:
            logger.exception(
                "approval_callback_db_error",
                request_id=str(request_id),
            )

        # Redis 업데이트
        try:
            cb_account_id = self._pending_account_ids.get(request_id, "default")
            await self._cache.set_json(
                _REDIS_NAMESPACE,
                f"{cb_account_id}:{request_id}",
                {"status": status.value, "responded_at": now.isoformat()},
                ttl=_REDIS_TTL_BUFFER,
            )
        except Exception:
            logger.exception(
                "approval_callback_redis_error",
                request_id=str(request_id),
            )

        # 텔레그램 메시지 업데이트 (버튼 제거 + 결과 표시)
        message_id = self._pending_message_ids.get(request_id)
        if message_id is not None:
            status_text = {
                ApprovalStatus.APPROVED: "✅ 승인됨",
                ApprovalStatus.REJECTED: "❌ 거부됨",
            }.get(status, str(status.value))
            if modified_qty is not None:
                status_text += f" (수량 변경: {modified_qty:,}주)"
            try:
                await self._bot.update_message(
                    message_id,
                    f"<b>{status_text}</b>",
                    remove_buttons=True,
                )
            except Exception:
                logger.exception(
                    "approval_callback_telegram_error",
                    request_id=str(request_id),
                )

        # 결과 저장 + 이벤트 시그널
        self._pending_results[request_id] = status
        if modified_qty is not None:
            self._modified_quantities[request_id] = modified_qty
        self._pending_events[request_id].set()

        logger.info(
            "approval_callback_processed",
            request_id=str(request_id),
            action=action,
            status=status.value,
        )

    # ── Wait ──────────────────────────────────────────────────────────

    async def _wait_for_response(
        self, request_id: UUID, timeout_sec: int
    ) -> ApprovalStatus:
        """asyncio.Event 기반 응답 대기. 타임아웃 시 TIMEOUT 반환."""
        event = self._pending_events[request_id]
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_sec)
            return self._pending_results.get(request_id, ApprovalStatus.TIMEOUT)
        except TimeoutError:
            return ApprovalStatus.TIMEOUT
