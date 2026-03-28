"""Phase 5 Step 7: OrderExecutor 단위 테스트.

Tests:
- Entry 정상 경로: 전체 플로우, 수동 승인, 매도
- Entry Web 검증: BLOCKED, WARNING, disabled
- Entry 승인: 거부, 타임아웃, 수량 변경(통과/실패)
- Entry 브로커: 실패, rejected 상태
- Entry 에러 핸들링: 예외, 포지션 생성 실패, 텔레그램/recorder 실패
- Exit 경로: 손절, 익절, 일반, Web 차단, 승인 거부, 포지션 청산
- Audit Trail: decision_ids
- Edge cases
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import (
    ApprovalStatus,
    DecisionAction,
    ExitReason,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalAction,
    StrategyType,
    WebVerifyResult,
)
from src.core.models import (
    ExecutionResult,
    ExitSignal,
    OrderRequest,
    OrderResult,
    PortfolioState,
    Position,
    RiskCheckResult,
    TradeDecision,
    WebVerification,
)
from src.db.models.execution import Execution, Order
from src.execution.executor import OrderExecutor


# ---------------------------------------------------------------------------
# Fake DB Session
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal async session stub."""

    def __init__(self) -> None:
        self.added: list = []
        self.executed: list = []
        self._commit_count = 0
        # 주문 생성 시 refresh에서 id를 할당하기 위한 카운터
        self._next_id = 1

    def add(self, obj):
        self.added.append(obj)
        if not getattr(obj, "id", None):
            obj.id = self._next_id
            self._next_id += 1

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult([])

    async def commit(self):
        self._commit_count += 1

    async def refresh(self, obj):
        # id가 없으면 할당
        if not getattr(obj, "id", None):
            obj.id = self._next_id
            self._next_id += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trade_decision(
    *,
    action: DecisionAction = DecisionAction.BUY,
    symbol: str = "005930",
    quantity: int = 10,
    price: Decimal = Decimal("72000"),
) -> TradeDecision:
    return TradeDecision(
        symbol=symbol,
        action=action,
        confidence=Decimal("0.8"),
        order_type=OrderType.LIMIT,
        quantity=quantity,
        price=price,
        stop_loss_price=Decimal("68000"),
        take_profit_price=Decimal("80000"),
        reasoning="Test decision",
    )


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


def _make_portfolio_state() -> PortfolioState:
    return PortfolioState(
        total_value=Decimal("10000000"),
        cash=Decimal("5000000"),
        invested=Decimal("5000000"),
        unrealized_pnl=Decimal("100000"),
        daily_pnl=Decimal("50000"),
        daily_pnl_pct=Decimal("0.5"),
        drawdown_pct=Decimal("2.0"),
        peak_value=Decimal("10200000"),
        positions=[],
        sector_allocations={},
        daily_trade_count=1,
        timestamp=datetime.now(UTC),
    )


def _make_order_result(
    *,
    status: OrderStatus = OrderStatus.FILLED,
    filled_price: Decimal = Decimal("72000"),
    filled_quantity: int = 10,
) -> OrderResult:
    return OrderResult(
        order_id="KIS123",
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=Decimal("72000"),
        status=status,
        filled_quantity=filled_quantity,
        filled_price=filled_price,
        commission=Decimal("1500"),
        timestamp=datetime.now(UTC),
    )


def _make_web_verification(
    result: WebVerifyResult = WebVerifyResult.SAFE,
) -> WebVerification:
    return WebVerification(
        symbol="005930",
        result=result,
        summary="정상",
        issues_found=[],
        news_checked=5,
        llm_cost_usd=Decimal("0.01"),
        reasoning="특이사항 없음",
    )


def _make_risk_check(*, passed: bool = True, qty: int = 10) -> RiskCheckResult:
    return RiskCheckResult(
        passed=passed,
        symbol="005930",
        violations=[] if passed else ["MAX_POSITION_PCT"],
        warnings=[],
        adjusted_quantity=qty,
        adjusted_amount_krw=Decimal(qty) * Decimal("72000"),
        max_allowed_quantity=qty,
        reasoning="OK" if passed else "위반",
    )


def _make_fake_position():
    """PositionRecord 모의 객체."""
    pos = MagicMock()
    pos.id = 1
    pos.symbol = "005930"
    pos.quantity = 10
    pos.avg_cost = Decimal("72000")
    pos.entry_price = Decimal("72000")
    pos.stop_loss_price = Decimal("68000")
    pos.take_profit_price = Decimal("80000")
    pos.strategy_type = StrategyType.POSITION.value
    pos.status = "open"
    return pos


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_session():
    return FakeSession()


@pytest.fixture()
def session_factory(fake_session):
    def factory():
        return fake_session

    factory._session = fake_session
    return factory


@pytest.fixture()
def mock_broker():
    broker = AsyncMock()
    broker.place_order = AsyncMock(return_value=_make_order_result())
    return broker


@pytest.fixture()
def mock_web_verifier():
    verifier = AsyncMock()
    verifier.verify = AsyncMock(return_value=_make_web_verification())
    return verifier


@pytest.fixture()
def mock_approval_manager():
    mgr = AsyncMock()
    mgr.request_approval = AsyncMock(return_value=ApprovalStatus.AUTO_APPROVED)
    return mgr


@pytest.fixture()
def mock_risk_manager():
    mgr = AsyncMock()
    mgr.check = AsyncMock(return_value=_make_risk_check())
    return mgr


@pytest.fixture()
def mock_position_manager():
    mgr = AsyncMock()
    mgr.create = AsyncMock(return_value=_make_fake_position())
    mgr.close = AsyncMock(return_value=_make_fake_position())
    return mgr


@pytest.fixture()
def mock_portfolio_service():
    svc = AsyncMock()
    svc.get_current_state = AsyncMock(return_value=_make_portfolio_state())
    return svc


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
def mock_settings():
    settings = MagicMock()
    settings.HUMAN_APPROVAL_REQUIRED = True
    settings.HUMAN_APPROVAL_TIMEOUT_SEC = 300
    settings.AUTO_EXECUTE_MAX_PORTFOLIO_PCT = 5.0
    settings.WEB_VERIFY_ENABLED = True
    settings.WEB_VERIFY_SKIP_ON_STOP_LOSS = True
    settings.ALERT_TELEGRAM_ENABLED = True
    return settings


@pytest.fixture()
def executor(
    mock_broker,
    mock_web_verifier,
    mock_approval_manager,
    mock_risk_manager,
    mock_position_manager,
    mock_portfolio_service,
    mock_recorder,
    mock_bot,
    session_factory,
    mock_settings,
):
    return OrderExecutor(
        broker=mock_broker,
        web_verifier=mock_web_verifier,
        approval_manager=mock_approval_manager,
        risk_manager=mock_risk_manager,
        position_manager=mock_position_manager,
        portfolio_service=mock_portfolio_service,
        recorder=mock_recorder,
        telegram_bot=mock_bot,
        session_factory=session_factory,
        settings=mock_settings,
    )


# ---------------------------------------------------------------------------
# Entry: Happy Path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_full_success(executor, mock_broker, mock_position_manager, mock_bot):
    """정상 진입: SAFE → AUTO_APPROVED → FILLED → position created."""
    td = _make_trade_decision()
    sid = uuid.uuid4()

    result = await executor.execute_entry(
        trade_decision=td, session_id=sid, strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.symbol == "005930"
    assert result.side == OrderSide.BUY
    assert result.fill_price == Decimal("72000")
    assert result.commission == Decimal("1500")
    assert result.broker_order_id == "KIS123"
    assert result.position_id == 1
    assert result.approval_status == ApprovalStatus.AUTO_APPROVED
    assert result.web_verify_result == WebVerifyResult.SAFE
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.create.assert_awaited_once()
    mock_bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_execute_entry_manual_approved(executor, mock_approval_manager):
    """수동 승인 경로."""
    mock_approval_manager.request_approval = AsyncMock(return_value=ApprovalStatus.APPROVED)
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.approval_status == ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_execute_entry_sell_action(executor, mock_broker):
    """매도 진입 주문."""
    td = _make_trade_decision(action=DecisionAction.SELL)

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
    )

    assert result.success is True
    assert result.side == OrderSide.SELL
    # place_order에 전달된 OrderRequest 확인
    call_args = mock_broker.place_order.call_args
    order_req = call_args[0][0]
    assert order_req.side == OrderSide.SELL


# ---------------------------------------------------------------------------
# Entry: Web Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_web_blocked(executor, mock_web_verifier, mock_broker, mock_bot):
    """Web 검증 BLOCKED → 주문 취소."""
    mock_web_verifier.verify = AsyncMock(return_value=_make_web_verification(WebVerifyResult.BLOCKED))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.web_verify_result == WebVerifyResult.BLOCKED
    assert "Web 검증 차단" in result.error
    mock_broker.place_order.assert_not_awaited()
    mock_bot.send_message.assert_awaited()  # rejection notification


@pytest.mark.asyncio
async def test_execute_entry_web_warning_proceeds(executor, mock_web_verifier, mock_broker):
    """Web 검증 WARNING → 진행."""
    mock_web_verifier.verify = AsyncMock(return_value=_make_web_verification(WebVerifyResult.WARNING))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.web_verify_result == WebVerifyResult.WARNING
    mock_broker.place_order.assert_awaited_once()


# ---------------------------------------------------------------------------
# Entry: Approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_approval_rejected(executor, mock_approval_manager, mock_broker):
    """승인 거부 → 주문 취소."""
    mock_approval_manager.request_approval = AsyncMock(return_value=ApprovalStatus.REJECTED)
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.approval_status == ApprovalStatus.REJECTED
    assert "승인 rejected" in result.error
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_entry_approval_timeout(executor, mock_approval_manager, mock_broker):
    """승인 타임아웃 → 주문 취소."""
    mock_approval_manager.request_approval = AsyncMock(return_value=ApprovalStatus.TIMEOUT)
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.approval_status == ApprovalStatus.TIMEOUT
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_entry_modified_quantity_passes(
    executor, mock_risk_manager, mock_broker, fake_session
):
    """수량 변경 + 리스크 통과 → 변경된 수량으로 체결."""
    # _get_order가 modified_quantity 포함된 Order 반환하도록 설정
    modified_order = MagicMock()
    modified_order.modified_quantity = 5
    modified_order.id = 1

    original_execute = fake_session.execute

    async def mock_execute(stmt):
        # select(Order) 쿼리일 때만 modified_order 반환
        if hasattr(stmt, "whereclause") or "orders" in str(stmt):
            return _FakeResult([modified_order])
        return await original_execute(stmt)

    fake_session.execute = mock_execute

    mock_risk_manager.check = AsyncMock(return_value=_make_risk_check(passed=True, qty=5))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    mock_risk_manager.check.assert_awaited_once()
    # place_order에 수정된 수량 전달 확인
    call_args = mock_broker.place_order.call_args[0][0]
    assert call_args.quantity == 5


@pytest.mark.asyncio
async def test_execute_entry_modified_quantity_risk_fails(
    executor, mock_risk_manager, mock_broker, mock_bot, fake_session
):
    """수량 변경 + 리스크 실패 → 주문 취소."""
    modified_order = MagicMock()
    modified_order.modified_quantity = 5
    modified_order.id = 1

    async def mock_execute(stmt):
        if hasattr(stmt, "whereclause") or "orders" in str(stmt):
            return _FakeResult([modified_order])
        return _FakeResult([])

    fake_session.execute = mock_execute

    mock_risk_manager.check = AsyncMock(return_value=_make_risk_check(passed=False, qty=0))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert "리스크 재검증 실패" in result.error
    mock_broker.place_order.assert_not_awaited()
    mock_bot.send_message.assert_awaited()  # rejection notification


# ---------------------------------------------------------------------------
# Entry: Broker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_broker_failure(executor, mock_broker):
    """place_order 예외 → FAILED."""
    mock_broker.place_order = AsyncMock(side_effect=Exception("Connection timeout"))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert "Connection timeout" in result.error


@pytest.mark.asyncio
async def test_execute_entry_broker_rejected(executor, mock_broker, mock_position_manager):
    """OrderResult status=REJECTED → FAILED."""
    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.REJECTED)
    )
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert "브로커 주문 실패" in result.error
    mock_position_manager.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Entry: Error Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_unexpected_exception(executor, mock_web_verifier):
    """예상치 못한 예외 → FAILED result."""
    mock_web_verifier.verify = AsyncMock(side_effect=RuntimeError("Unexpected"))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert "Unexpected" in result.error


@pytest.mark.asyncio
async def test_execute_entry_position_create_fails(executor, mock_position_manager):
    """브로커 성공 + 포지션 생성 실패 → success=True, position_id=None."""
    mock_position_manager.create = AsyncMock(side_effect=Exception("DB error"))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.position_id is None
    assert result.broker_order_id == "KIS123"


@pytest.mark.asyncio
async def test_execute_entry_telegram_failure_non_blocking(executor, mock_bot):
    """텔레그램 실패 → 체결은 계속."""
    mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram down"))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_execute_entry_recorder_failure_non_blocking(executor, mock_recorder):
    """decision_log 기록 실패 → 체결은 계속."""
    mock_recorder.record = AsyncMock(side_effect=Exception("DB write failed"))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.decision_ids == []  # 기록 실패하여 비어있음


# ---------------------------------------------------------------------------
# Exit: Happy Path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_exit_stop_loss(
    executor, mock_web_verifier, mock_broker, mock_position_manager
):
    """손절: MARKET 주문, auto-approved, web verify skipped."""
    es = _make_exit_signal(reason=ExitReason.STOP_LOSS, urgency="immediate")
    pos = _make_fake_position()
    sell_result = _make_order_result(
        status=OrderStatus.FILLED, filled_price=Decimal("68000"),
    )
    sell_result.side = OrderSide.SELL
    mock_broker.place_order = AsyncMock(return_value=sell_result)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    assert result.side == OrderSide.SELL
    # web_verifier는 호출되지만 is_stop_loss=True로 (내부에서 skip)
    mock_web_verifier.verify.assert_awaited_once()
    call_kwargs = mock_web_verifier.verify.call_args.kwargs
    assert call_kwargs["is_stop_loss"] is True
    # place_order 호출 확인
    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.order_type == OrderType.MARKET
    mock_position_manager.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_exit_take_profit(executor, mock_approval_manager):
    """익절: auto-approved (ApprovalManager 내부 로직)."""
    es = _make_exit_signal(reason=ExitReason.TAKE_PROFIT, urgency="end_of_day")
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    # ApprovalManager에 전달된 trade_decision의 action 확인
    call_kwargs = mock_approval_manager.request_approval.call_args.kwargs
    td = call_kwargs["trade_decision"]
    assert td.action == DecisionAction.TAKE_PROFIT


@pytest.mark.asyncio
async def test_execute_exit_normal(executor, mock_approval_manager):
    """일반 청산 (FUNDAMENTAL 등)."""
    es = _make_exit_signal(reason=ExitReason.FUNDAMENTAL, urgency="next_session")
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    call_kwargs = mock_approval_manager.request_approval.call_args.kwargs
    td = call_kwargs["trade_decision"]
    assert td.action == DecisionAction.SELL
    # LIMIT 주문 (non-stop-loss)
    assert td.order_type == OrderType.LIMIT


# ---------------------------------------------------------------------------
# Exit: Web Verification & Approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_exit_web_blocked(executor, mock_web_verifier, mock_broker):
    """non-stop-loss exit Web 차단 → 취소."""
    mock_web_verifier.verify = AsyncMock(
        return_value=_make_web_verification(WebVerifyResult.BLOCKED)
    )
    es = _make_exit_signal(reason=ExitReason.FUNDAMENTAL, urgency="next_session")
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is False
    assert result.web_verify_result == WebVerifyResult.BLOCKED
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_exit_approval_rejected(executor, mock_approval_manager, mock_broker):
    """청산 승인 거부."""
    mock_approval_manager.request_approval = AsyncMock(return_value=ApprovalStatus.REJECTED)
    es = _make_exit_signal(reason=ExitReason.FUNDAMENTAL, urgency="next_session")
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is False
    assert result.approval_status == ApprovalStatus.REJECTED
    mock_broker.place_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# Exit: Position Close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_exit_position_closed(executor, mock_position_manager):
    """포지션 청산 — close()에 올바른 파라미터 전달."""
    es = _make_exit_signal(reason=ExitReason.STOP_LOSS)
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    mock_position_manager.close.assert_awaited_once()
    call_args = mock_position_manager.close.call_args
    assert call_args[0][0] == pos.id  # position_id
    assert call_args[1]["exit_reason"] == ExitReason.STOP_LOSS


@pytest.mark.asyncio
async def test_execute_exit_position_close_fails(executor, mock_position_manager):
    """포지션 청산 실패 → success=True (브로커 주문은 이미 체결)."""
    mock_position_manager.close = AsyncMock(side_effect=Exception("DB error"))
    es = _make_exit_signal(reason=ExitReason.STOP_LOSS)
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    # position_id는 원본 position.id (close 실패해도)
    assert result.position_id == pos.id


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_decision_ids(executor, mock_recorder):
    """진입 시 decision_ids에 ID 수집."""
    fixed_id = uuid.uuid4()
    mock_recorder.record = AsyncMock(return_value=fixed_id)
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert fixed_id in result.decision_ids
    mock_recorder.record.assert_awaited()


@pytest.mark.asyncio
async def test_execute_exit_decision_ids(executor, mock_recorder):
    """청산 시 decision_ids에 ID 수집."""
    fixed_id = uuid.uuid4()
    mock_recorder.record = AsyncMock(return_value=fixed_id)
    es = _make_exit_signal()
    pos = _make_fake_position()

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    assert fixed_id in result.decision_ids


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_no_price(executor):
    """price=None → Decimal(0) 사용."""
    td = _make_trade_decision(price=None)

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    # 브로커가 성공 반환하므로 체결
    assert result.success is True


@pytest.mark.asyncio
async def test_execute_entry_db_creation_fails(executor, fake_session):
    """초기 Order 생성 실패 → 즉시 에러."""
    original_add = fake_session.add
    def failing_add(obj):
        raise Exception("DB connection lost")
    fake_session.add = failing_add

    td = _make_trade_decision()
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.order_id is None
    assert "DB connection lost" in result.error


# ---------------------------------------------------------------------------
# Phase 8 Step 6: account_id + broker override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_account_id_propagation(
    executor, fake_session, mock_approval_manager, mock_position_manager, mock_recorder,
):
    """account_id가 Order, approval, position, recorder에 모두 전파되는지 확인."""
    td = _make_trade_decision()
    sid = uuid.uuid4()
    acct = "acct-growth"

    result = await executor.execute_entry(
        trade_decision=td, session_id=sid,
        strategy_type=StrategyType.POSITION.value,
        account_id=acct,
    )

    assert result.success is True

    # Order에 account_id 설정 확인
    order_row = fake_session.added[0]
    assert order_row.account_id == acct

    # approval_manager에 account_id 전달 확인
    approval_call = mock_approval_manager.request_approval.call_args
    assert approval_call.kwargs["account_id"] == acct

    # position_manager에 account_id 전달 확인
    pos_call = mock_position_manager.create.call_args
    assert pos_call.kwargs["account_id"] == acct

    # recorder에 account_id 전달 확인 (최소 1회 호출)
    assert mock_recorder.record.await_count >= 1
    for call in mock_recorder.record.call_args_list:
        assert call.kwargs.get("account_id") == acct


@pytest.mark.asyncio
async def test_execute_entry_broker_override(executor, mock_broker):
    """broker 파라미터 전달 시 self._broker 대신 전달된 broker 사용."""
    alt_broker = AsyncMock()
    alt_broker.place_order = AsyncMock(return_value=_make_order_result())

    td = _make_trade_decision()
    sid = uuid.uuid4()

    result = await executor.execute_entry(
        trade_decision=td, session_id=sid,
        strategy_type=StrategyType.POSITION.value,
        broker=alt_broker,
    )

    assert result.success is True
    alt_broker.place_order.assert_awaited_once()
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_exit_account_id(executor, fake_session, mock_recorder):
    """execute_exit에도 account_id가 전파되는지 확인."""
    sig = _make_exit_signal()
    pos = _make_fake_position()
    sid = uuid.uuid4()
    acct = "acct-safe"

    result = await executor.execute_exit(
        exit_signal=sig, position=pos, session_id=sid, account_id=acct,
    )

    assert result.success is True
    # Order에 account_id 설정 확인
    order_row = fake_session.added[0]
    assert order_row.account_id == acct

    # recorder에 account_id 전달 확인
    for call in mock_recorder.record.call_args_list:
        assert call.kwargs.get("account_id") == acct


@pytest.mark.asyncio
async def test_execute_exit_broker_override(executor, mock_broker):
    """execute_exit에서 broker override 동작 확인."""
    alt_broker = AsyncMock()
    alt_broker.place_order = AsyncMock(return_value=_make_order_result())

    sig = _make_exit_signal()
    pos = _make_fake_position()
    sid = uuid.uuid4()

    result = await executor.execute_exit(
        exit_signal=sig, position=pos, session_id=sid, broker=alt_broker,
    )

    assert result.success is True
    alt_broker.place_order.assert_awaited_once()
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_entry_default_account_id(executor, fake_session):
    """account_id 미지정 시 'default' 사용."""
    td = _make_trade_decision()
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    order_row = fake_session.added[0]
    assert order_row.account_id == "default"
