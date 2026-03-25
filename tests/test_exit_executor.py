"""Phase 5 Step 8: ExitExecutionService 단위 테스트.

Tests:
- immediate 시그널: OrderExecutor.execute_exit 호출
- end_of_day 시그널: OrderExecutor.execute_exit 호출
- next_session 시그널: 실행 안 됨, 알림만 전송
- 혼합 시그널: urgency 순 처리
- 시그널-포지션 매칭 실패: 스킵 + 로깅
- 복수 시그널 처리 + 장애 격리
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.core.enums import (
    ApprovalStatus,
    DecisionAction,
    DecisionStage,
    ExitReason,
    OrderSide,
    OrderStatus,
    StrategyType,
    WebVerifyResult,
)
from src.core.models import ExecutionResult, ExitSignal
from src.execution.exit_executor import ExitExecutionService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_exit_signal(
    *,
    reason: ExitReason = ExitReason.STOP_LOSS,
    urgency: str = "immediate",
    symbol: str = "005930",
) -> ExitSignal:
    return ExitSignal(
        symbol=symbol,
        reason=reason,
        urgency=urgency,
        current_price=Decimal("68000"),
        unrealized_pnl_pct=Decimal("-5.5"),
        recommended_action=DecisionAction.STOP_LOSS,
        reasoning="Stop loss triggered",
    )


def _make_fake_position(
    *,
    symbol: str = "005930",
    position_id: int = 1,
) -> MagicMock:
    pos = MagicMock()
    pos.id = position_id
    pos.symbol = symbol
    pos.quantity = 10
    pos.avg_cost = Decimal("72000")
    pos.entry_price = Decimal("72000")
    pos.stop_loss_price = Decimal("68000")
    pos.take_profit_price = Decimal("80000")
    pos.strategy_type = StrategyType.POSITION.value
    pos.status = "open"
    return pos


def _make_execution_result(
    *,
    success: bool = True,
    symbol: str = "005930",
) -> ExecutionResult:
    return ExecutionResult(
        success=success,
        order_id=1,
        broker_order_id="KIS456",
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=10,
        fill_price=Decimal("68000"),
        commission=Decimal("1500"),
        approval_status=ApprovalStatus.AUTO_APPROVED,
        web_verify_result=WebVerifyResult.SAFE,
        position_id=1,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_order_executor():
    executor = AsyncMock()
    executor.execute_exit = AsyncMock(return_value=_make_execution_result())
    return executor


@pytest.fixture()
def mock_position_manager():
    return AsyncMock()


@pytest.fixture()
def mock_portfolio_service():
    return AsyncMock()


@pytest.fixture()
def mock_recorder():
    recorder = AsyncMock()
    recorder.record = AsyncMock(return_value=uuid.uuid4())
    return recorder


@pytest.fixture()
def mock_bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=100)
    return bot


@pytest.fixture()
def exit_service(
    mock_order_executor,
    mock_position_manager,
    mock_portfolio_service,
    mock_recorder,
    mock_bot,
):
    return ExitExecutionService(
        order_executor=mock_order_executor,
        position_manager=mock_position_manager,
        portfolio_service=mock_portfolio_service,
        recorder=mock_recorder,
        telegram_bot=mock_bot,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestImmediateSignal:
    """immediate 시그널 → execute_exit() 호출."""

    @pytest.mark.asyncio()
    async def test_immediate_signal_executes(self, exit_service, mock_order_executor):
        signal = _make_exit_signal(urgency="immediate", reason=ExitReason.STOP_LOSS)
        position = _make_fake_position()
        session_id = uuid.uuid4()

        results = await exit_service.process_exit_signals(
            [signal], [position], session_id=session_id,
        )

        assert len(results) == 1
        assert results[0].success is True
        mock_order_executor.execute_exit.assert_awaited_once_with(
            exit_signal=signal,
            position=position,
            session_id=session_id,
        )


class TestEndOfDaySignal:
    """end_of_day 시그널 → execute_exit() 호출."""

    @pytest.mark.asyncio()
    async def test_end_of_day_signal_executes(self, exit_service, mock_order_executor):
        signal = _make_exit_signal(urgency="end_of_day", reason=ExitReason.TAKE_PROFIT)
        position = _make_fake_position()
        session_id = uuid.uuid4()

        results = await exit_service.process_exit_signals(
            [signal], [position], session_id=session_id,
        )

        assert len(results) == 1
        assert results[0].success is True
        mock_order_executor.execute_exit.assert_awaited_once()


class TestNextSessionSignal:
    """next_session 시그널 → 실행 안 됨, 알림만 전송."""

    @pytest.mark.asyncio()
    async def test_next_session_signal_alert_only(
        self, exit_service, mock_order_executor, mock_recorder, mock_bot,
    ):
        signal = _make_exit_signal(
            urgency="next_session", reason=ExitReason.FUNDAMENTAL,
        )
        position = _make_fake_position()
        session_id = uuid.uuid4()

        results = await exit_service.process_exit_signals(
            [signal], [position], session_id=session_id,
        )

        # execute_exit 미호출
        mock_order_executor.execute_exit.assert_not_awaited()

        # decision_log 기록
        mock_recorder.record.assert_awaited_once()
        record_kwargs = mock_recorder.record.call_args.kwargs
        assert record_kwargs["stage"] == DecisionStage.EXIT
        assert record_kwargs["symbol"] == "005930"
        assert "next_session" in record_kwargs["reasoning"]

        # 텔레그램 알림
        mock_bot.send_message.assert_awaited_once()

        # 결과: success=False (실행 안 됨)
        assert len(results) == 1
        assert results[0].success is False
        assert "next_session" in results[0].error


class TestMixedUrgencies:
    """혼합 시그널 → urgency 순 처리 (immediate → end_of_day → next_session)."""

    @pytest.mark.asyncio()
    async def test_mixed_urgencies_sorted_correctly(
        self, exit_service, mock_order_executor, mock_recorder,
    ):
        # 의도적으로 역순으로 생성
        sig_next = _make_exit_signal(
            urgency="next_session", reason=ExitReason.FUNDAMENTAL, symbol="000660",
        )
        sig_immediate = _make_exit_signal(
            urgency="immediate", reason=ExitReason.STOP_LOSS, symbol="005930",
        )
        sig_eod = _make_exit_signal(
            urgency="end_of_day", reason=ExitReason.TAKE_PROFIT, symbol="035420",
        )

        pos1 = _make_fake_position(symbol="005930", position_id=1)
        pos2 = _make_fake_position(symbol="035420", position_id=2)
        pos3 = _make_fake_position(symbol="000660", position_id=3)

        # execute_exit 호출 시 symbol에 따라 결과 반환
        mock_order_executor.execute_exit = AsyncMock(
            side_effect=[
                _make_execution_result(symbol="005930"),
                _make_execution_result(symbol="035420"),
            ],
        )

        session_id = uuid.uuid4()
        results = await exit_service.process_exit_signals(
            [sig_next, sig_immediate, sig_eod],
            [pos1, pos2, pos3],
            session_id=session_id,
        )

        assert len(results) == 3

        # execute_exit는 2번 호출 (immediate, end_of_day)
        assert mock_order_executor.execute_exit.await_count == 2

        # 첫 번째 호출: immediate (005930)
        first_call = mock_order_executor.execute_exit.call_args_list[0]
        assert first_call.kwargs["exit_signal"].symbol == "005930"
        assert first_call.kwargs["exit_signal"].urgency == "immediate"

        # 두 번째 호출: end_of_day (035420)
        second_call = mock_order_executor.execute_exit.call_args_list[1]
        assert second_call.kwargs["exit_signal"].symbol == "035420"
        assert second_call.kwargs["exit_signal"].urgency == "end_of_day"

        # next_session은 recorder.record 호출
        mock_recorder.record.assert_awaited_once()


class TestSignalPositionMismatch:
    """시그널-포지션 매칭 실패 → 스킵."""

    @pytest.mark.asyncio()
    async def test_signal_position_mismatch_skipped(
        self, exit_service, mock_order_executor,
    ):
        signal = _make_exit_signal(symbol="999999")
        position = _make_fake_position(symbol="005930")
        session_id = uuid.uuid4()

        results = await exit_service.process_exit_signals(
            [signal], [position], session_id=session_id,
        )

        assert len(results) == 0
        mock_order_executor.execute_exit.assert_not_awaited()


class TestBatchWithFaultIsolation:
    """복수 시그널 처리 + 1건 실패해도 나머지 처리."""

    @pytest.mark.asyncio()
    async def test_multiple_signals_batch_with_fault_isolation(
        self, exit_service, mock_order_executor,
    ):
        sig1 = _make_exit_signal(
            urgency="immediate", reason=ExitReason.STOP_LOSS, symbol="005930",
        )
        sig2 = _make_exit_signal(
            urgency="immediate", reason=ExitReason.TRAILING_STOP, symbol="035420",
        )

        pos1 = _make_fake_position(symbol="005930", position_id=1)
        pos2 = _make_fake_position(symbol="035420", position_id=2)

        # 첫 번째 호출 예외, 두 번째 성공
        mock_order_executor.execute_exit = AsyncMock(
            side_effect=[
                RuntimeError("broker connection lost"),
                _make_execution_result(symbol="035420"),
            ],
        )

        session_id = uuid.uuid4()
        results = await exit_service.process_exit_signals(
            [sig1, sig2], [pos1, pos2], session_id=session_id,
        )

        # 2건 모두 결과에 포함
        assert len(results) == 2

        # 첫 번째: 실패
        assert results[0].success is False
        assert "broker connection lost" in results[0].error

        # 두 번째: 성공
        assert results[1].success is True
        assert results[1].symbol == "035420"

        # execute_exit 2번 호출됨 (장애 격리)
        assert mock_order_executor.execute_exit.await_count == 2
