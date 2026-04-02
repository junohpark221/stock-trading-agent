"""ExitExecutionService — 청산 시그널 일괄 실행 서비스.

Phase 4 ExitConditionChecker의 결과(ExitSignal 목록)를 OrderExecutor.execute_exit()로
연결하는 오케스트레이션 레이어. urgency별 라우팅:
    immediate  → 즉시 실행 (손절, 트레일링 스톱)
    end_of_day → 장중 실행 (익절, 시간 기반)
    next_session → 실행 안 함, 알림 + decision_log 기록만
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from src.core.enums import (
    ApprovalStatus,
    DecisionAction,
    DecisionStage,
    OrderSide,
)
from src.core.models import ExecutionResult, ExitSignal
from src.notification.templates import MessageTemplates

if TYPE_CHECKING:
    from src.agent.decision_recorder import DecisionRecorder
    from src.broker.base import BrokerInterface
    from src.db.models.strategy import PositionRecord
    from src.execution.executor import OrderExecutor
    from src.notification.telegram import TelegramBot
    from src.strategy.portfolio_state import PortfolioStateService
    from src.strategy.position_manager import PositionManager

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 긴급도 정렬 상수
# ---------------------------------------------------------------------------

_URGENCY_ORDER: dict[str, int] = {
    "immediate": 0,
    "end_of_day": 1,
    "next_session": 2,
}


class ExitExecutionService:
    """청산 시그널 일괄 실행 서비스.

    Phase 4 ExitConditionChecker의 결과를 OrderExecutor.execute_exit()로 연결.
    urgency별 처리 경로를 분기하고, 개별 시그널 장애를 격리한다.
    """

    def __init__(
        self,
        *,
        order_executor: OrderExecutor,
        position_manager: PositionManager,
        portfolio_service: PortfolioStateService,
        recorder: DecisionRecorder,
        telegram_bot: TelegramBot,
    ) -> None:
        self._order_executor = order_executor
        self._position_manager = position_manager
        self._portfolio_service = portfolio_service
        self._recorder = recorder
        self._bot = telegram_bot

    # ── Public API ────────────────────────────────────────────────────────

    async def process_exit_signals(
        self,
        exit_signals: list[ExitSignal],
        positions: list[PositionRecord],
        *,
        session_id: UUID,
        account_id: str = "default",
        account_label: str = "",
        broker: BrokerInterface | None = None,
    ) -> list[ExecutionResult]:
        """청산 시그널 목록을 받아 urgency순 실행.

        1. exit_signals와 positions를 symbol로 매칭
        2. urgency 순 정렬: immediate > end_of_day > next_session
        3. immediate/end_of_day: OrderExecutor.execute_exit() 호출
        4. next_session: decision_log 기록 + 텔레그램 알림만
        """
        if not exit_signals:
            return []

        # urgency 순 정렬 (immediate → end_of_day → next_session)
        sorted_signals = sorted(
            exit_signals,
            key=lambda s: _URGENCY_ORDER.get(s.urgency, 99),
        )

        results: list[ExecutionResult] = []
        executed_count = 0
        alert_count = 0
        skipped_count = 0

        for signal in sorted_signals:
            # 시그널-포지션 매칭
            position = await self._match_signal_to_position(signal, positions)
            if position is None:
                skipped_count += 1
                logger.warning(
                    "exit_executor.no_matching_position",
                    symbol=signal.symbol,
                    reason=signal.reason.value,
                    urgency=signal.urgency,
                )
                continue

            if signal.urgency in ("immediate", "end_of_day"):
                result = await self._execute_signal(
                    signal, position,
                    session_id=session_id,
                    account_id=account_id,
                    account_label=account_label,
                    broker=broker,
                )
                results.append(result)
                executed_count += 1
            else:
                # next_session: 알림만
                result = await self._handle_next_session_signal(
                    signal, position,
                    session_id=session_id,
                    account_id=account_id,
                    account_label=account_label,
                )
                results.append(result)
                alert_count += 1

        logger.info(
            "exit_executor.batch_complete",
            total=len(exit_signals),
            executed=executed_count,
            alert_only=alert_count,
            skipped=skipped_count,
        )
        return results

    # ── Private Helpers ───────────────────────────────────────────────────

    async def _match_signal_to_position(
        self,
        signal: ExitSignal,
        positions: list[PositionRecord],
    ) -> PositionRecord | None:
        """시그널 symbol과 매칭되는 열린 포지션 찾기."""
        for pos in positions:
            if pos.symbol == signal.symbol and pos.status == "open":
                return pos
        return None

    async def _execute_signal(
        self,
        signal: ExitSignal,
        position: PositionRecord,
        *,
        session_id: UUID,
        account_id: str = "default",
        account_label: str = "",
        broker: BrokerInterface | None = None,
    ) -> ExecutionResult:
        """immediate/end_of_day 시그널을 OrderExecutor로 실행. 장애 격리."""
        try:
            return await self._order_executor.execute_exit(
                exit_signal=signal,
                position=position,
                session_id=session_id,
                account_id=account_id,
                account_label=account_label,
                broker=broker,
            )
        except Exception as exc:
            logger.exception(
                "exit_executor.execute_failed",
                symbol=signal.symbol,
                reason=signal.reason.value,
                urgency=signal.urgency,
            )
            # 텔레그램 에러 알림
            try:
                await self._bot.send_message(
                    f"<b>청산 실행 실패</b>\n"
                    f"종목: {signal.symbol}\n"
                    f"사유: {signal.reason.value}\n"
                    f"긴급도: {signal.urgency}\n"
                    f"에러: {exc!s}"
                )
            except Exception:
                logger.warning(
                    "exit_executor.execute_failed_telegram_send_failed",
                    symbol=signal.symbol,
                )
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                side=OrderSide.SELL,
                quantity=position.quantity,
                approval_status=ApprovalStatus.AUTO_APPROVED,
                error=f"실행 중 예외: {exc!s}",
            )

    async def _handle_next_session_signal(
        self,
        signal: ExitSignal,
        position: PositionRecord,
        *,
        session_id: UUID,
        account_id: str = "default",
        account_label: str = "",
    ) -> ExecutionResult:
        """next_session 시그널: decision_log 기록 + 텔레그램 알림만 전송."""
        decision_ids: list[UUID] = []

        # 1. decision_log 기록
        try:
            did = await self._recorder.record(
                session_id=session_id,
                stage=DecisionStage.EXIT,
                decision=signal.recommended_action,
                symbol=signal.symbol,
                reasoning=f"next_session 시그널: {signal.reasoning}",
                account_id=account_id,
                data_snapshot={
                    "urgency": "next_session",
                    "exit_reason": signal.reason.value,
                    "unrealized_pnl_pct": str(signal.unrealized_pnl_pct),
                    "current_price": str(signal.current_price),
                    "position_id": position.id,
                },
            )
            decision_ids.append(did)
        except Exception:
            logger.exception(
                "exit_executor.record_failed",
                symbol=signal.symbol,
            )

        # 2. 텔레그램 알림
        try:
            msg = MessageTemplates.exit_signal_notification(
                account_label=account_label,
                symbol=signal.symbol,
                name=signal.symbol,
                reason=signal.reason,
                urgency=signal.urgency,
                current_price=signal.current_price,
                unrealized_pnl_pct=signal.unrealized_pnl_pct,
                auto_executed=False,
            )
            await self._bot.send_message(msg)
        except Exception:
            logger.exception(
                "exit_executor.telegram_failed",
                symbol=signal.symbol,
            )

        return ExecutionResult(
            success=False,
            symbol=signal.symbol,
            side=OrderSide.SELL,
            quantity=position.quantity,
            approval_status=ApprovalStatus.AUTO_APPROVED,
            decision_ids=decision_ids,
            error="next_session: 알림 전송만 실행 (자동 체결 없음)",
        )
