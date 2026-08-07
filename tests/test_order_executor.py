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
    # Cash gate: 기본적으로 충분한 가용 현금 반환 (기존 테스트는 cash gate 통과 가정).
    # cash gate 자체를 검증하는 테스트는 override하여 낮은 값 반환.
    broker.get_buyable_cash = AsyncMock(return_value=Decimal("1_000_000_000"))
    # 매도 preflight(F-12): 기본 None = 조회 정보 없음 → 클램프 미적용(기존 동작).
    # preflight를 검증하는 테스트는 override한다.
    broker.get_sellable_quantity = AsyncMock(return_value=None)
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
    # PRJ-04: 진입 확정은 merge_or_create 경유 — (record, merged) 튜플 반환.
    mgr.merge_or_create = AsyncMock(return_value=(_make_fake_position(), False))
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
    settings.ORDER_CASH_GATE_MODE = "reject"
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
    assert result.terminal is False  # F-21: 성공 경로는 영구 차단 아님
    assert result.symbol == "005930"
    assert result.side == OrderSide.BUY
    assert result.fill_price == Decimal("72000")
    assert result.commission == Decimal("1500")
    assert result.broker_order_id == "KIS123"
    assert result.position_id == 1
    assert result.approval_status == ApprovalStatus.AUTO_APPROVED
    assert result.web_verify_result == WebVerifyResult.SAFE
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.merge_or_create.assert_awaited_once()
    mock_bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_execute_entry_threads_analysis_snapshot(
    executor, mock_position_manager
):
    """entry_analysis_snapshot이 주문→체결→포지션 생성까지 전파된다."""
    td = _make_trade_decision()
    snapshot = {
        "symbol": "005930",
        "action": "buy",
        "confidence": "0.85",
        "key_factors": ["골든크로스"],
    }

    await executor.execute_entry(
        trade_decision=td,
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
        entry_analysis_snapshot=snapshot,
    )

    create_kwargs = mock_position_manager.merge_or_create.call_args.kwargs
    assert create_kwargs["entry_analysis_snapshot"] == snapshot


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
async def test_execute_entry_sell_action_blocked_by_guard(
    executor, mock_broker, mock_position_manager, mock_bot, fake_session,
):
    """F-28: 진입 경로로 들어온 매도 체결은 포지션을 만들지 않고 실패로 반환한다."""
    td = _make_trade_decision(action=DecisionAction.SELL)

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
    )

    # 브로커 발주 자체는 정상 수행(체결은 되돌릴 수 없음)
    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.side == OrderSide.SELL
    # 체결 기록은 남는다
    assert any(isinstance(o, Execution) for o in fake_session.added)
    # phantom 포지션 생성 차단 + 실패 반환 + 긴급 알림
    mock_position_manager.merge_or_create.assert_not_awaited()
    assert result.success is False
    assert result.position_id is None
    assert "F-28" in result.error
    mock_bot.send_message.assert_awaited()


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
    assert result.terminal is True  # F-21: 영구 차단 → 드레인이 즉시 만료
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
    assert result.terminal is True  # F-21: 사용자 명시 거부 → 영구 차단
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
    assert result.terminal is False  # F-21: TIMEOUT은 재시도 유지(다음 드레인에 승인 가능)
    assert result.approval_status == ApprovalStatus.TIMEOUT
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_entry_disapproved_cancels_stray_broker_order(
    executor, mock_approval_manager, mock_broker
):
    """F-06: 거부/만료인데 broker_order_id가 있으면 broker.cancel_order 시도."""
    mock_approval_manager.request_approval = AsyncMock(
        return_value=ApprovalStatus.TIMEOUT
    )
    stray = MagicMock()
    stray.broker_order_id = "KIS999"
    executor._get_order = AsyncMock(return_value=stray)
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    mock_broker.place_order.assert_not_awaited()
    mock_broker.cancel_order.assert_awaited_once_with("KIS999")


@pytest.mark.asyncio
async def test_execute_entry_disapproved_no_broker_order_no_cancel(
    executor, mock_approval_manager, mock_broker
):
    """F-06 회귀: 정상 흐름(broker_order_id 없음)에선 cancel_order 미호출."""
    mock_approval_manager.request_approval = AsyncMock(
        return_value=ApprovalStatus.REJECTED
    )
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    mock_broker.cancel_order.assert_not_awaited()


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
        # PRJ-04: positions 조회(부분익절 러너 게이트)까지 삼키지 않도록 orders만 매칭.
        if "orders" in str(stmt):
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
        # PRJ-04: positions 조회(부분익절 러너 게이트)까지 삼키지 않도록 orders만 매칭.
        if "orders" in str(stmt):
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
    assert result.terminal is True  # F-21: 승인 후 재검증 실패 → 영구 차단
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
    mock_position_manager.merge_or_create.assert_not_awaited()


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
    """브로커 성공 + 포지션 생성 실패 → success=False, 긴급 알림."""
    mock_position_manager.merge_or_create = AsyncMock(side_effect=Exception("DB error"))
    td = _make_trade_decision()

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.position_id is None
    assert "포지션 생성 실패" in result.error


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


@pytest.mark.asyncio
async def test_execute_exit_order_type_override_market(executor, mock_broker):
    """F-28: order_type_override로 수동 시장가 매도를 낼 수 있다."""
    es = _make_exit_signal(reason=ExitReason.MANUAL, urgency="immediate")
    pos = _make_fake_position()

    await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
        order_type_override=OrderType.MARKET, manual=True,
    )

    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.order_type == OrderType.MARKET


@pytest.mark.asyncio
async def test_execute_exit_override_cannot_downgrade_stop_loss(executor, mock_broker):
    """손절+즉시의 MARKET은 안전 하한 — override로 LIMIT으로 낮출 수 없다."""
    es = _make_exit_signal(reason=ExitReason.STOP_LOSS, urgency="immediate")
    pos = _make_fake_position()

    await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
        order_type_override=OrderType.LIMIT,
    )

    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.order_type == OrderType.MARKET


@pytest.mark.asyncio
async def test_execute_exit_default_order_type_unchanged(executor, mock_broker):
    """override 미지정 시 기존 동작(LIMIT) 유지 — 회귀 고정."""
    es = _make_exit_signal(reason=ExitReason.MANUAL, urgency="immediate")
    pos = _make_fake_position()

    await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(), manual=True,
    )

    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.order_type == OrderType.LIMIT


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
# Exit: 매도가능수량 preflight (F-12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_exit_sellable_clamps_quantity(executor, mock_broker):
    """매도가능수량 < 요청수량 → 발주 수량을 sellable로 클램프(비손절)."""
    es = _make_exit_signal(reason=ExitReason.TAKE_PROFIT, urgency="end_of_day")
    pos = _make_fake_position()  # quantity=10
    mock_broker.get_sellable_quantity = AsyncMock(return_value=6)
    sell_result = _make_order_result(status=OrderStatus.FILLED, filled_quantity=6)
    sell_result.side = OrderSide.SELL
    mock_broker.place_order = AsyncMock(return_value=sell_result)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.quantity == 6  # 10 → 6으로 클램프


@pytest.mark.asyncio
async def test_execute_exit_stop_loss_zero_sellable_proceeds(executor, mock_broker):
    """매도가능=0이라도 긴급 손절은 스킵하지 않고 원수량으로 진행(스트랜딩 방지)."""
    es = _make_exit_signal(reason=ExitReason.STOP_LOSS, urgency="immediate")
    pos = _make_fake_position()  # quantity=10
    mock_broker.get_sellable_quantity = AsyncMock(return_value=0)
    sell_result = _make_order_result(status=OrderStatus.FILLED, filled_quantity=10)
    sell_result.side = OrderSide.SELL
    mock_broker.place_order = AsyncMock(return_value=sell_result)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    mock_broker.place_order.assert_awaited_once()
    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.quantity == 10  # 손절은 클램프/스킵 안 함


@pytest.mark.asyncio
async def test_execute_exit_nonstoploss_zero_sellable_skips(executor, mock_broker):
    """비손절 청산에서 매도가능=0이면 발주 스킵(과매도 방지)."""
    es = _make_exit_signal(reason=ExitReason.FUNDAMENTAL, urgency="end_of_day")
    pos = _make_fake_position()
    mock_broker.get_sellable_quantity = AsyncMock(return_value=0)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is False
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_exit_sellable_none_no_clamp(executor, mock_broker):
    """매도가능수량 조회 불가(None)면 클램프 없이 원수량 발주."""
    es = _make_exit_signal(reason=ExitReason.TAKE_PROFIT, urgency="end_of_day")
    pos = _make_fake_position()  # quantity=10
    mock_broker.get_sellable_quantity = AsyncMock(return_value=None)
    sell_result = _make_order_result(status=OrderStatus.FILLED, filled_quantity=10)
    sell_result.side = OrderSide.SELL
    mock_broker.place_order = AsyncMock(return_value=sell_result)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.quantity == 10


# ---------------------------------------------------------------------------
# Exit: 부분익절 사다리 (F-10 Phase 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_exit_partial_tp_reduces_and_transitions(
    executor, mock_broker, mock_position_manager
):
    """부분익절: reduce 경유(close 아님) + 잔량 트레일링 전환 호출."""
    es = _make_exit_signal(reason=ExitReason.PARTIAL_TAKE_PROFIT, urgency="end_of_day")
    pos = _make_fake_position()  # quantity=10, avg_cost=72000
    sell_result = _make_order_result(status=OrderStatus.FILLED, filled_quantity=3)
    sell_result.side = OrderSide.SELL
    mock_broker.place_order = AsyncMock(return_value=sell_result)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(), exit_quantity=3,
    )

    assert result.success is True
    # 부분 체결(3 < 10) → reduce, close 미호출
    mock_position_manager.reduce.assert_awaited_once()
    mock_position_manager.close.assert_not_awaited()
    # 잔량 트레일링 전환 — 본전가는 평단가
    mock_position_manager.transition_to_trailing.assert_awaited_once()
    t_kwargs = mock_position_manager.transition_to_trailing.call_args
    assert t_kwargs[0][0] == pos.id
    assert t_kwargs[1]["break_even_price"] == pos.avg_cost
    # 발주 수량은 부분익절 수량
    order_req = mock_broker.place_order.call_args[0][0]
    assert order_req.quantity == 3


@pytest.mark.asyncio
async def test_execute_exit_full_close_no_transition(
    executor, mock_broker, mock_position_manager
):
    """전량 청산(TAKE_PROFIT)은 close 경유 + 트레일링 전환 미호출(회귀 가드)."""
    es = _make_exit_signal(reason=ExitReason.TAKE_PROFIT, urgency="end_of_day")
    pos = _make_fake_position()  # quantity=10
    sell_result = _make_order_result(status=OrderStatus.FILLED, filled_quantity=10)
    sell_result.side = OrderSide.SELL
    mock_broker.place_order = AsyncMock(return_value=sell_result)

    result = await executor.execute_exit(
        exit_signal=es, position=pos, session_id=uuid.uuid4(),
    )

    assert result.success is True
    mock_position_manager.close.assert_awaited_once()
    mock_position_manager.transition_to_trailing.assert_not_awaited()


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
    """price=None → 입력 검증 실패로 조기 반환."""
    td = _make_trade_decision(price=None)

    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert "유효하지 않은 가격" in result.error


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
    pos_call = mock_position_manager.merge_or_create.call_args
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
    alt_broker.get_buyable_cash = AsyncMock(return_value=Decimal("1_000_000_000"))

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
    alt_broker.get_sellable_quantity = AsyncMock(return_value=None)

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


# ---------------------------------------------------------------------------
# Cash Gate: 미수 방지
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cash_gate_reject_blocks_order_exceeding_buyable(
    executor, mock_broker, mock_settings,
):
    """reject 모드: 주문 총액 > 주문가능현금 → 차단, place_order 미호출."""
    mock_settings.ORDER_CASH_GATE_MODE = "reject"
    # 주문 10주 * 72000 = 720,000 / 가용 현금 500,000 → 차단
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("500_000"))

    td = _make_trade_decision(quantity=10, price=Decimal("72000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert "주문가능현금 부족" in (result.error or "")
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cash_gate_shrink_adjusts_quantity_to_fit_buyable(
    executor, mock_broker, mock_settings,
):
    """shrink 모드: 초과 시 수량을 buyable 내로 자동 축소하여 주문 진행."""
    mock_settings.ORDER_CASH_GATE_MODE = "shrink"
    # 주문 10주 * 72000 = 720,000, 가용 500,000 → 6주로 축소 (500000/72000=6.94→6)
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("500_000"))
    mock_broker.place_order = AsyncMock(return_value=_make_order_result(
        filled_quantity=6,
    ))

    td = _make_trade_decision(quantity=10, price=Decimal("72000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    # place_order에 축소된 수량 전달되었는지 확인
    call_kwargs = mock_broker.place_order.call_args.args[0]
    assert call_kwargs.quantity == 6


@pytest.mark.asyncio
async def test_cash_gate_shrink_falls_back_to_reject_when_below_1_share(
    executor, mock_broker, mock_settings,
):
    """shrink 모드에서 가용이 1주 가격보다 작으면 차단 동작."""
    mock_settings.ORDER_CASH_GATE_MODE = "shrink"
    # 가용 1만원 / 단가 7.2만원 → 0주 → reject 경로로 폴백
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("10_000"))

    td = _make_trade_decision(quantity=10, price=Decimal("72000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    mock_broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cash_gate_off_mode_skips_check(
    executor, mock_broker, mock_settings,
):
    """off 모드: 가용 현금 0이어도 gate 미적용 → place_order 호출."""
    mock_settings.ORDER_CASH_GATE_MODE = "off"
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("0"))

    td = _make_trade_decision(quantity=10, price=Decimal("72000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    mock_broker.place_order.assert_awaited_once()
    # off 모드에서는 get_buyable_cash 자체를 호출하지 않아야 함
    mock_broker.get_buyable_cash.assert_not_awaited()


@pytest.mark.asyncio
async def test_cash_gate_sell_skips_check(
    executor, mock_broker, mock_settings,
):
    """SELL 주문은 현금 체크 생략 (리스크 감소 방향).

    체결 확정은 F-28 가드가 막지만(success=False), 현금 게이트 자체는 발주 전 단계라
    SELL에서 호출되지 않아야 한다.
    """
    mock_settings.ORDER_CASH_GATE_MODE = "reject"
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("0"))

    td = _make_trade_decision(action=DecisionAction.SELL, quantity=10,
                              price=Decimal("72000"))
    await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    mock_broker.place_order.assert_awaited_once()
    mock_broker.get_buyable_cash.assert_not_awaited()


@pytest.mark.asyncio
async def test_cash_gate_passes_when_sufficient(executor, mock_broker, mock_settings):
    """주문 총액 <= 가용 현금: gate 통과, 수량 유지."""
    mock_settings.ORDER_CASH_GATE_MODE = "reject"
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("10_000_000"))

    td = _make_trade_decision(quantity=10, price=Decimal("72000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    call_kwargs = mock_broker.place_order.call_args.args[0]
    assert call_kwargs.quantity == 10


# ---------------------------------------------------------------------------
# SUBMITTED path — WS/Reconciler integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_submitted_without_stream_returns_pending(
    executor, mock_broker, mock_position_manager, fake_session,
):
    """KIS 기본 경로(SUBMITTED) + WS 미연결 → pending=True, 포지션 미생성."""
    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.SUBMITTED),
    )

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.pending is True
    assert result.broker_order_id == "KIS123"
    assert result.position_id is None
    assert result.fill_price is None
    mock_position_manager.merge_or_create.assert_not_awaited()

    # orders 테이블에 SUBMITTED로 저장되었는지 확인 (rejection_reason 미기록)
    from src.db.models.execution import Order
    order_rows = [x for x in fake_session.added if isinstance(x, Order)]
    assert len(order_rows) == 1


@pytest.mark.asyncio
async def test_execute_entry_submitted_with_ws_filled_finalizes(
    executor, mock_broker, mock_position_manager, mock_bot,
):
    """SUBMITTED + WS fill event → finalize_entry_fill 경로 진입 → 포지션 생성."""
    from src.core.models import ExecutionEvent

    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.SUBMITTED),
    )

    # WS 매니저 mock: wait_for_fill에서 체결 이벤트 반환
    stream = AsyncMock()
    stream.wait_for_fill = AsyncMock(
        return_value=ExecutionEvent(
            account_id="default",
            broker_order_id="KIS123",
            symbol="005930",
            side=OrderSide.BUY,
            filled_quantity=10,
            filled_price=Decimal("72100"),
            is_filled=True,
            is_rejected=False,
            timestamp=datetime.now(UTC),
        )
    )
    executor._execution_stream = stream

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.pending is False
    assert result.fill_price == Decimal("72100")
    assert result.position_id == 1
    mock_position_manager.merge_or_create.assert_awaited_once()
    stream.wait_for_fill.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_entry_submitted_ws_timeout_returns_pending(
    executor, mock_broker, mock_position_manager,
):
    """SUBMITTED + WS timeout → pending=True, 포지션 미생성."""
    import asyncio as _asyncio

    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.SUBMITTED),
    )

    stream = AsyncMock()
    stream.wait_for_fill = AsyncMock(side_effect=_asyncio.TimeoutError)
    executor._execution_stream = stream
    executor._settings.ORDER_FILL_TIMEOUT_SEC = 1

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.pending is True
    assert result.position_id is None
    mock_position_manager.merge_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_entry_submitted_ws_rejected(
    executor, mock_broker, mock_position_manager,
):
    """SUBMITTED + WS rejection event → FAILED."""
    from src.core.models import ExecutionEvent

    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.SUBMITTED),
    )

    stream = AsyncMock()
    stream.wait_for_fill = AsyncMock(
        return_value=ExecutionEvent(
            account_id="default",
            broker_order_id="KIS123",
            symbol="005930",
            side=OrderSide.BUY,
            filled_quantity=0,
            filled_price=Decimal(0),
            is_filled=False,
            is_rejected=True,
            rejected_reason="잔고부족",
            timestamp=datetime.now(UTC),
        )
    )
    executor._execution_stream = stream

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.pending is False
    assert "잔고부족" in result.error
    mock_position_manager.merge_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_entry_broker_hard_fail_marks_failed(
    executor, mock_broker, mock_position_manager,
):
    """broker가 REJECTED 반환 → FAILED 처리 (SUBMITTED는 실패로 간주하지 않음)."""
    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.REJECTED),
    )

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.pending is False
    assert "rejected" in result.error
    mock_position_manager.merge_or_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Manual=True: 웹검증/승인 플로우 생략 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_entry_manual_skips_web_verify_and_approval(
    executor,
    mock_web_verifier,
    mock_approval_manager,
    mock_portfolio_service,
    mock_broker,
    mock_position_manager,
):
    """manual=True면 WebSearchVerifier.verify / ApprovalManager.request_approval /
    PortfolioStateService.get_current_state를 호출하지 않고 AUTO_APPROVED 경로로 진행."""
    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
        manual=True,
    )

    assert result.success is True
    assert result.approval_status == ApprovalStatus.AUTO_APPROVED
    assert result.web_verify_result == WebVerifyResult.SAFE
    mock_web_verifier.verify.assert_not_awaited()
    mock_approval_manager.request_approval.assert_not_awaited()
    mock_portfolio_service.get_current_state.assert_not_awaited()
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.merge_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_entry_manual_still_enforces_cash_gate(
    executor, mock_broker, mock_position_manager,
):
    """manual=True여도 Cash Gate는 동작해야 함 (미수 방지)."""
    # 주문가능현금을 주문금액보다 훨씬 낮게 설정 → reject 모드에서 차단
    mock_broker.get_buyable_cash = AsyncMock(return_value=Decimal("100"))

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),  # 10주 @ 72000 = 720000 KRW 필요
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
        manual=True,
    )

    assert result.success is False
    assert "현금" in result.error or "cash" in result.error.lower()
    mock_broker.place_order.assert_not_awaited()
    mock_position_manager.merge_or_create.assert_not_awaited()


# ---------------------------------------------------------------------------
# F-04: 배치 in-flight 예약(BatchReservation) 게이트 + 누적
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_gate_blocks_before_approval(
    executor, mock_risk_manager, mock_approval_manager, mock_broker,
):
    """배치 게이트가 차단하면 승인 요청/브로커 주문 없이 즉시 거부."""
    from src.strategy.risk_manager import BatchReservation

    # 게이트 단계의 리스크 체크가 차단을 반환
    mock_risk_manager.check = AsyncMock(
        return_value=_make_risk_check(passed=False, qty=0)
    )
    reservation = BatchReservation()

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
        batch_reservation=reservation,
    )

    assert result.success is False
    assert result.terminal is True  # F-21: 배치 리스크 게이트 차단 → 영구 차단
    # 승인 요청·브로커 주문 모두 미발생 (헛 승인 방지)
    mock_approval_manager.request_approval.assert_not_awaited()
    mock_broker.place_order.assert_not_awaited()
    # 차단됐으므로 예약은 누적되지 않음
    assert reservation.trade_count == 0


@pytest.mark.asyncio
async def test_batch_reservation_accumulates_on_pending(
    executor, mock_broker, mock_position_manager,
):
    """접수(SUBMITTED, 미체결) 성공 시 예약에 누적된다."""
    from src.strategy.risk_manager import BatchReservation

    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.SUBMITTED),
    )
    reservation = BatchReservation()

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(symbol="005930"),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
        batch_reservation=reservation,
    )

    assert result.success is True
    assert result.pending is True
    mock_position_manager.merge_or_create.assert_not_awaited()
    # in-flight 진입이 예약에 누적
    assert reservation.trade_count == 1
    assert "005930" in reservation.new_symbols


@pytest.mark.asyncio
async def test_batch_reservation_not_accumulated_on_immediate_fill(
    executor, mock_broker, mock_position_manager,
):
    """즉시 체결(FILLED)은 DB 포지션이 생성되므로 예약하지 않는다(이중 카운트 방지)."""
    from src.strategy.risk_manager import BatchReservation

    # 기본 _make_order_result는 FILLED
    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(status=OrderStatus.FILLED),
    )
    reservation = BatchReservation()

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(symbol="005930"),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
        batch_reservation=reservation,
    )

    assert result.success is True
    mock_position_manager.merge_or_create.assert_awaited()  # DB 포지션 생성됨
    # DB가 카운트를 이어받으므로 예약은 누적되지 않음
    assert reservation.trade_count == 0
    assert reservation.new_symbols == set()


@pytest.mark.asyncio
async def test_batch_reservation_none_skips_gate(
    executor, mock_risk_manager, mock_approval_manager, mock_broker,
):
    """batch_reservation=None이면 게이트를 건너뛰고 기존 승인 경로를 탄다."""
    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(),
        session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
        batch_reservation=None,
    )

    assert result.success is True
    # 게이트가 없으므로 승인은 정상 요청됨
    mock_approval_manager.request_approval.assert_awaited()


# ---------------------------------------------------------------------------
# F-16: 갭/진입가 괴리 보정 (손절·익절 비율 보존 + 수량 재사이징)
# ---------------------------------------------------------------------------


def _set_sizing_settings(settings) -> None:
    """PositionSizer 재호출에 필요한 사이징 설정값 주입(MagicMock 기본값 회피)."""
    settings.RISK_PER_TRADE_PCT = 2.0
    settings.MAX_POSITION_PCT = 100.0
    settings.MAX_POSITION_SIZE_KRW = 1_000_000_000
    settings.STOP_LOSS_PERCENT = 3.0


@pytest.mark.asyncio
async def test_gap_resize_shrinks_quantity_on_upward_gap(
    executor, mock_broker, mock_settings,
):
    """상방 갭 통과 시 risk_per_share 증가분만큼 수량을 축소(F-16 A1)."""
    _set_sizing_settings(mock_settings)
    # 기준가 72000, live 74000(상방 갭). 손절 68000(기준가 대비 -5.56%).
    # risk_amount=10M*2%=200000, scaled_stop=74000*(1-4000/72000)=69888.9,
    # risk_per_share≈4111 → risk_qty=48. 원수량 60 → min(60,48)=48.
    td = _make_trade_decision(quantity=60, price=Decimal("74000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
        reference_price=Decimal("72000"),
    )

    assert result.success is True
    order_req = mock_broker.place_order.call_args.args[0]
    assert order_req.quantity == 48


@pytest.mark.asyncio
async def test_gap_resize_only_shrinks_never_grows(
    executor, mock_broker, mock_settings,
):
    """하방 갭이어도 수량은 원수량 유지(축소만, 예산 초과 없음, F-16 A1)."""
    _set_sizing_settings(mock_settings)
    # live 70000(하방 갭) → risk_per_share 감소 → risk_qty 증가하지만 min으로 원수량 유지.
    td = _make_trade_decision(quantity=10, price=Decimal("70000"))
    result = await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
        reference_price=Decimal("72000"),
    )

    assert result.success is True
    order_req = mock_broker.place_order.call_args.args[0]
    assert order_req.quantity == 10


@pytest.mark.asyncio
async def test_finalize_rescales_stop_tp_preserving_ratio(
    executor, mock_broker, mock_position_manager, mock_settings,
):
    """체결가 기준으로 손절/익절을 원래 비율로 재적용(F-16 A2)."""
    _set_sizing_settings(mock_settings)
    mock_broker.place_order = AsyncMock(
        return_value=_make_order_result(filled_price=Decimal("74000"), filled_quantity=10),
    )
    td = _make_trade_decision(quantity=10, price=Decimal("74000"))
    await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
        reference_price=Decimal("72000"),
    )

    kwargs = mock_position_manager.merge_or_create.call_args.kwargs
    fill = Decimal("74000")
    p_stop = (Decimal("72000") - Decimal("68000")) / Decimal("72000")
    p_tp = (Decimal("80000") - Decimal("72000")) / Decimal("72000")
    assert kwargs["stop_loss_price"] == fill * (Decimal("1") - p_stop)
    assert kwargs["take_profit_price"] == fill * (Decimal("1") + p_tp)


@pytest.mark.asyncio
async def test_no_reference_price_keeps_decision_stop_tp(
    executor, mock_position_manager,
):
    """reference_price 미주입(수동/단건) → 손절/익절은 결정값 그대로(보정 스킵)."""
    td = _make_trade_decision()
    await executor.execute_entry(
        trade_decision=td, session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    kwargs = mock_position_manager.merge_or_create.call_args.kwargs
    assert kwargs["stop_loss_price"] == Decimal("68000")
    assert kwargs["take_profit_price"] == Decimal("80000")


# ---------------------------------------------------------------------------
# F-10 B1: 진입 시 트레일링/시간 청산 파라미터 비-NULL 주입
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_injects_trailing_params_position(
    executor, mock_position_manager,
):
    """POSITION 진입: trailing_stop_pct/max_holding_days가 비-NULL로 저장."""
    await executor.execute_entry(
        trade_decision=_make_trade_decision(), session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    kwargs = mock_position_manager.merge_or_create.call_args.kwargs
    assert kwargs["trailing_stop_pct"] is not None
    assert kwargs["max_holding_days"] == 60


@pytest.mark.asyncio
async def test_entry_injects_trailing_params_swing(
    executor, mock_position_manager,
):
    """SWING 진입: 고정 트레일 폭 + max 10일 주입."""
    await executor.execute_entry(
        trade_decision=_make_trade_decision(), session_id=uuid.uuid4(),
        strategy_type=StrategyType.SWING.value,
    )

    kwargs = mock_position_manager.merge_or_create.call_args.kwargs
    assert kwargs["trailing_stop_pct"] == Decimal("5.0")
    assert kwargs["max_holding_days"] == 10


# ---------------------------------------------------------------------------
# PRJ-04 — 포지션 병합 + 부분익절 러너 추가매수 게이트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_merges_into_existing_position(
    executor, mock_position_manager, mock_bot,
):
    """병합 시 기존 행 id가 주문에 붙고 체결 알림에 병합 문구가 붙는다."""
    merged_pos = _make_fake_position()
    merged_pos.id = 7
    merged_pos.quantity = 20
    merged_pos.avg_cost = Decimal("75000")
    mock_position_manager.merge_or_create = AsyncMock(return_value=(merged_pos, True))

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(), session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is True
    assert result.position_id == 7
    messages = [c.args[0] for c in mock_bot.send_message.await_args_list if c.args]
    assert any("추가매수 병합" in m for m in messages)


@pytest.mark.asyncio
async def test_partial_exit_runner_blocks_auto_buy(
    executor, mock_broker, mock_position_manager, mock_bot,
):
    """부분익절 러너(open + realized_pnl>0)가 있으면 자동 매수를 terminal 거부한다."""
    runner = _make_fake_position()
    runner.id = 3
    runner.realized_pnl = Decimal("120000")
    runner.quantity = 5
    executor._find_partial_exit_runner = AsyncMock(return_value=runner)

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(), session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.terminal is True
    assert "부분익절 러너" in (result.error or "")
    mock_broker.place_order.assert_not_awaited()
    mock_position_manager.merge_or_create.assert_not_awaited()
    mock_bot.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_partial_exit_runner_gate_skipped_for_manual_order(
    executor, mock_broker, mock_position_manager,
):
    """수동 주문은 사람의 명시적 판단 — 게이트를 타지 않는다."""
    executor._find_partial_exit_runner = AsyncMock(
        return_value=_make_fake_position()
    )

    result = await executor.execute_entry(
        trade_decision=_make_trade_decision(), session_id=uuid.uuid4(),
        strategy_type=StrategyType.POSITION.value, manual=True,
    )

    assert result.success is True
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.merge_or_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_exit_runner_lookup_failure_does_not_block(executor):
    """조회 실패는 매수를 막지 않는다(게이트는 과도기 안전장치)."""
    executor._session_factory = MagicMock(side_effect=RuntimeError("db down"))

    runner = await executor._find_partial_exit_runner("default", "005930")

    assert runner is None
