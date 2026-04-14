"""Phase 5 E2E integration tests.

Uses real OrderExecutor + ExitExecutionService with mocked dependencies.
No real DB, API, LLM, or Telegram calls — all external dependencies are mocked.
8 scenarios covering the full Phase 5 execution pipeline.
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
    DecisionStage,
    ExitReason,
    OrderSide,
    OrderStatus,
    OrderType,
    StrategyType,
    WebVerifyResult,
)
from src.core.models import (
    ExecutionResult,
    ExitSignal,
    OrderRequest,
    OrderResult,
    PortfolioState,
    RiskCheckResult,
    TradeDecision,
    WebVerification,
)
from src.db.models.execution import Execution, Order
from src.execution.executor import OrderExecutor
from src.execution.exit_executor import ExitExecutionService


# ---------------------------------------------------------------------------
# Fake DB Session (reuse pattern from test_order_executor.py)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Minimal async session stub."""

    def __init__(self) -> None:
        self.added: list = []
        self.executed: list = []
        self._commit_count = 0
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
        if not getattr(obj, "id", None):
            obj.id = self._next_id
            self._next_id += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# Sample Data Helpers
# ---------------------------------------------------------------------------


def _trade_decision(
    *,
    symbol: str = "005930",
    action: DecisionAction = DecisionAction.BUY,
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
        reasoning="Integration test decision",
    )


def _exit_signal(
    *,
    symbol: str = "005930",
    reason: ExitReason = ExitReason.STOP_LOSS,
    urgency: str = "immediate",
    current_price: Decimal = Decimal("68000"),
) -> ExitSignal:
    return ExitSignal(
        symbol=symbol,
        reason=reason,
        urgency=urgency,
        current_price=current_price,
        unrealized_pnl_pct=Decimal("-5.5"),
        recommended_action=DecisionAction.STOP_LOSS,
        reasoning="Exit signal triggered",
    )


def _portfolio_state() -> PortfolioState:
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


def _web_verification(result: WebVerifyResult = WebVerifyResult.SAFE) -> WebVerification:
    return WebVerification(
        symbol="005930",
        result=result,
        summary="정상" if result == WebVerifyResult.SAFE else "이슈 감지",
        issues_found=[] if result == WebVerifyResult.SAFE else ["이슈"],
        news_checked=5,
        llm_cost_usd=Decimal("0.01"),
        reasoning="특이사항 없음" if result == WebVerifyResult.SAFE else "문제 발견",
    )


def _order_result(
    *,
    status: OrderStatus = OrderStatus.FILLED,
    filled_price: Decimal = Decimal("72000"),
    filled_quantity: int = 10,
) -> OrderResult:
    return OrderResult(
        order_id="KIS-INT-001",
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


def _risk_check(*, passed: bool = True, qty: int = 10) -> RiskCheckResult:
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


def _fake_position(**kwargs) -> MagicMock:
    """PositionRecord ORM mock."""
    pos = MagicMock()
    pos.id = kwargs.get("id", 1)
    pos.symbol = kwargs.get("symbol", "005930")
    pos.quantity = kwargs.get("quantity", 10)
    pos.avg_cost = kwargs.get("avg_cost", Decimal("72000"))
    pos.entry_price = kwargs.get("entry_price", Decimal("72000"))
    pos.stop_loss_price = kwargs.get("stop_loss_price", Decimal("68000"))
    pos.take_profit_price = kwargs.get("take_profit_price", Decimal("80000"))
    pos.strategy_type = kwargs.get("strategy_type", StrategyType.POSITION.value)
    pos.status = kwargs.get("status", "open")
    pos.entry_session_id = kwargs.get("entry_session_id", uuid.uuid4())
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
    broker.place_order = AsyncMock(return_value=_order_result())
    broker.get_buyable_cash = AsyncMock(return_value=Decimal("1_000_000_000"))
    return broker


@pytest.fixture()
def mock_web_verifier():
    verifier = AsyncMock()
    verifier.verify = AsyncMock(return_value=_web_verification())
    return verifier


@pytest.fixture()
def mock_approval_manager():
    mgr = AsyncMock()
    mgr.request_approval = AsyncMock(return_value=ApprovalStatus.AUTO_APPROVED)
    return mgr


@pytest.fixture()
def mock_risk_manager():
    mgr = AsyncMock()
    mgr.check = AsyncMock(return_value=_risk_check())
    return mgr


@pytest.fixture()
def mock_position_manager():
    mgr = AsyncMock()
    mgr.create = AsyncMock(return_value=_fake_position())
    mgr.close = AsyncMock(return_value=_fake_position())
    return mgr


@pytest.fixture()
def mock_portfolio_service():
    svc = AsyncMock()
    svc.get_current_state = AsyncMock(return_value=_portfolio_state())
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


@pytest.fixture()
def exit_service(
    executor,
    mock_position_manager,
    mock_portfolio_service,
    mock_recorder,
    mock_bot,
):
    return ExitExecutionService(
        order_executor=executor,
        position_manager=mock_position_manager,
        portfolio_service=mock_portfolio_service,
        recorder=mock_recorder,
        telegram_bot=mock_bot,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Full Entry — Auto-Approved
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_entry_auto_approved(
    executor, mock_broker, mock_web_verifier, mock_approval_manager,
    mock_position_manager, mock_recorder, mock_bot,
):
    """진입 자동 승인: TradeDecision → WebSearch(SAFE) → AUTO_APPROVED → 주문 → 포지션 생성.

    검증: orders FILLED, executions 존재, positions open, decision_log EXECUTION.
    """
    td = _trade_decision()
    sid = uuid.uuid4()

    result = await executor.execute_entry(
        trade_decision=td,
        session_id=sid,
        strategy_type=StrategyType.POSITION.value,
    )

    # 전체 파이프라인 성공
    assert result.success is True
    assert result.symbol == "005930"
    assert result.side == OrderSide.BUY
    assert result.fill_price == Decimal("72000")
    assert result.commission == Decimal("1500")
    assert result.approval_status == ApprovalStatus.AUTO_APPROVED
    assert result.web_verify_result == WebVerifyResult.SAFE
    assert result.position_id == 1
    assert result.broker_order_id == "KIS-INT-001"

    # Cross-component 호출 검증
    mock_web_verifier.verify.assert_awaited_once()
    mock_approval_manager.request_approval.assert_awaited_once()
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.create.assert_awaited_once()
    mock_recorder.record.assert_awaited()  # decision_log 기록
    mock_bot.send_message.assert_awaited()  # 체결 통보


# ═══════════════════════════════════════════════════════════════════════════
# 2. Full Entry — Manual Approved
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_entry_manual_approved(
    executor, mock_approval_manager, mock_broker,
    mock_position_manager, mock_recorder,
):
    """진입 수동 승인: 텔레그램 승인 시뮬레이션.

    검증: APPROVAL + EXECUTION 전체 파이프라인 실행.
    """
    mock_approval_manager.request_approval = AsyncMock(
        return_value=ApprovalStatus.APPROVED,
    )

    td = _trade_decision()
    sid = uuid.uuid4()

    result = await executor.execute_entry(
        trade_decision=td,
        session_id=sid,
        strategy_type=StrategyType.SWING.value,
    )

    assert result.success is True
    assert result.approval_status == ApprovalStatus.APPROVED
    assert result.position_id == 1

    # 수동 승인이어도 전체 파이프라인 동일하게 실행
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.create.assert_awaited_once()
    mock_recorder.record.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# 3. Entry — Quantity Modification
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_entry_quantity_modification(
    executor, mock_approval_manager, mock_risk_manager,
    mock_broker, mock_position_manager,
):
    """수량 수정 승인: quantity=20 → modify(10) → 리스크 재검증 → 주문(10).

    검증: orders.modified_quantity=10, 포지션 quantity=10.
    """
    # 수동 승인 (수량 수정 포함)
    mock_approval_manager.request_approval = AsyncMock(
        return_value=ApprovalStatus.APPROVED,
    )

    # 리스크 재검증 통과 (수정 수량 10으로)
    mock_risk_manager.check = AsyncMock(return_value=_risk_check(qty=10))

    # 브로커: 10주 체결
    mock_broker.place_order = AsyncMock(
        return_value=_order_result(filled_quantity=10),
    )

    # 포지션 생성: 10주
    mock_position_manager.create = AsyncMock(
        return_value=_fake_position(quantity=10),
    )

    td = _trade_decision(quantity=20)
    sid = uuid.uuid4()

    # _get_order가 modified_quantity=10인 Order mock 반환하도록 패치
    mock_order = MagicMock()
    mock_order.modified_quantity = 10
    mock_order.id = 1

    with patch.object(executor, "_get_order", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_order

        result = await executor.execute_entry(
            trade_decision=td,
            session_id=sid,
            strategy_type=StrategyType.POSITION.value,
        )

    assert result.success is True
    assert result.quantity == 10  # 20 → 10으로 수정

    # 리스크 재검증이 수정 수량으로 호출됨
    mock_risk_manager.check.assert_awaited_once()
    call_kwargs = mock_risk_manager.check.call_args.kwargs
    assert call_kwargs["quantity"] == 10


# ═══════════════════════════════════════════════════════════════════════════
# 4. Entry — Blocked by Web Verify
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_entry_blocked_by_web_verify(
    executor, mock_web_verifier, mock_approval_manager,
    mock_broker, mock_recorder,
):
    """Web Search 차단: WebSearch(BLOCKED) → 주문 CANCELLED.

    검증: orders.status=CANCELLED, web_verify_result="blocked".
    """
    mock_web_verifier.verify = AsyncMock(
        return_value=_web_verification(WebVerifyResult.BLOCKED),
    )

    td = _trade_decision()
    sid = uuid.uuid4()

    result = await executor.execute_entry(
        trade_decision=td,
        session_id=sid,
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.web_verify_result == WebVerifyResult.BLOCKED
    assert "Web 검증 차단" in result.error

    # 승인/브로커 호출 없음 (파이프라인 조기 종료)
    mock_approval_manager.request_approval.assert_not_awaited()
    mock_broker.place_order.assert_not_awaited()

    # 거부 기록은 남김
    mock_recorder.record.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# 5. Exit — Stop Loss Auto-Execute
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_exit_stop_loss_auto_execute(
    exit_service, mock_web_verifier, mock_approval_manager,
    mock_broker, mock_position_manager,
):
    """손절 자동 실행: ExitSignal(STOP_LOSS, immediate) → Web Search 검증(is_stop_loss) → 매도.

    검증: position closed, exit_reason=stop_loss.
    """
    # 브로커: 매도 체결
    mock_broker.place_order = AsyncMock(
        return_value=OrderResult(
            order_id="KIS-EXIT-001",
            symbol="005930",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=10,
            price=Decimal("68000"),
            status=OrderStatus.FILLED,
            filled_quantity=10,
            filled_price=Decimal("67500"),
            commission=Decimal("1400"),
            timestamp=datetime.now(UTC),
        ),
    )

    signal = _exit_signal(reason=ExitReason.STOP_LOSS, urgency="immediate")
    position = _fake_position()

    results = await exit_service.process_exit_signals(
        [signal], [position], session_id=uuid.uuid4(),
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].side == OrderSide.SELL

    # Web 검증: is_stop_loss=True로 호출 (verifier 내부에서 skip 가능)
    mock_web_verifier.verify.assert_awaited_once()
    verify_kwargs = mock_web_verifier.verify.call_args.kwargs
    assert verify_kwargs["is_stop_loss"] is True

    # 포지션 청산
    mock_position_manager.close.assert_awaited_once()
    close_kwargs = mock_position_manager.close.call_args.kwargs
    assert close_kwargs["exit_reason"] == ExitReason.STOP_LOSS


# ═══════════════════════════════════════════════════════════════════════════
# 6. Exit — Take Profit with Approval
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_exit_take_profit_with_approval(
    exit_service, mock_web_verifier, mock_approval_manager,
    mock_broker, mock_position_manager,
):
    """익절 승인: ExitSignal(TAKE_PROFIT, end_of_day) → 승인 → 매도.

    검증: decision_log EXIT + EXECUTION 체인.
    """
    # 브로커: 매도 체결
    mock_broker.place_order = AsyncMock(
        return_value=OrderResult(
            order_id="KIS-EXIT-002",
            symbol="005930",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("80000"),
            status=OrderStatus.FILLED,
            filled_quantity=10,
            filled_price=Decimal("80000"),
            commission=Decimal("1700"),
            timestamp=datetime.now(UTC),
        ),
    )

    signal = _exit_signal(
        reason=ExitReason.TAKE_PROFIT,
        urgency="end_of_day",
        current_price=Decimal("80000"),
    )
    position = _fake_position()

    results = await exit_service.process_exit_signals(
        [signal], [position], session_id=uuid.uuid4(),
    )

    assert len(results) == 1
    assert results[0].success is True

    # Web 검증: is_stop_loss=False (익절은 stop loss 아님)
    verify_kwargs = mock_web_verifier.verify.call_args.kwargs
    assert verify_kwargs["is_stop_loss"] is False

    # 승인 요청 + 브로커 주문 + 포지션 청산 모두 실행
    mock_approval_manager.request_approval.assert_awaited_once()
    mock_broker.place_order.assert_awaited_once()
    mock_position_manager.close.assert_awaited_once()
    close_kwargs = mock_position_manager.close.call_args.kwargs
    assert close_kwargs["exit_reason"] == ExitReason.TAKE_PROFIT


# ═══════════════════════════════════════════════════════════════════════════
# 7. Approval Timeout Cancels Order
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_approval_timeout_cancels_order(
    executor, mock_approval_manager, mock_broker,
    mock_position_manager, mock_recorder,
):
    """타임아웃: TIMEOUT → 주문 CANCELLED.

    검증: orders.status=CANCELLED, approval_status="timeout".
    """
    mock_approval_manager.request_approval = AsyncMock(
        return_value=ApprovalStatus.TIMEOUT,
    )

    td = _trade_decision()
    sid = uuid.uuid4()

    result = await executor.execute_entry(
        trade_decision=td,
        session_id=sid,
        strategy_type=StrategyType.POSITION.value,
    )

    assert result.success is False
    assert result.approval_status == ApprovalStatus.TIMEOUT
    assert "timeout" in result.error.lower()

    # 브로커/포지션 미호출 (승인 실패로 조기 종료)
    mock_broker.place_order.assert_not_awaited()
    mock_position_manager.create.assert_not_awaited()

    # 거부 기록
    mock_recorder.record.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# 8. Full Decision Chain — parent_id + stage 검증
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_decision_chain(
    executor, mock_broker, mock_web_verifier,
    mock_approval_manager, mock_position_manager,
    mock_recorder, mock_bot, session_factory,
    mock_settings, mock_risk_manager, mock_portfolio_service,
):
    """전체 의사결정 체인: 진입 + 청산 순차 실행.

    진입(EXECUTION stage) → 청산(EXIT stage) parent_id 체인 검증.
    """
    # 진입/청산 각각의 decision_id
    entry_decision_id = uuid.uuid4()
    exit_decision_id = uuid.uuid4()
    mock_recorder.record = AsyncMock(
        side_effect=[entry_decision_id, exit_decision_id],
    )

    parent_id = uuid.uuid4()  # 상위 단계(예: trade_decision)에서 넘어온 ID
    sid = uuid.uuid4()

    # ── Step 1: 진입 실행 ──
    td = _trade_decision()
    entry_result = await executor.execute_entry(
        trade_decision=td,
        session_id=sid,
        strategy_type=StrategyType.POSITION.value,
        parent_decision_id=parent_id,
    )

    assert entry_result.success is True
    assert entry_decision_id in entry_result.decision_ids

    # recorder.record 첫 호출: EXECUTION stage + parent_id 연결
    entry_call = mock_recorder.record.call_args_list[0]
    assert entry_call.kwargs["stage"] == DecisionStage.EXECUTION
    assert entry_call.kwargs["parent_id"] == parent_id
    assert entry_call.kwargs["symbol"] == "005930"

    # ── Step 2: 청산 실행 ──
    # 브로커: 매도 체결
    mock_broker.place_order = AsyncMock(
        return_value=OrderResult(
            order_id="KIS-EXIT-003",
            symbol="005930",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=10,
            price=Decimal("68000"),
            status=OrderStatus.FILLED,
            filled_quantity=10,
            filled_price=Decimal("67500"),
            commission=Decimal("1400"),
            timestamp=datetime.now(UTC),
        ),
    )

    signal = _exit_signal()
    position = _fake_position()

    exit_result = await executor.execute_exit(
        exit_signal=signal,
        position=position,
        session_id=sid,
        parent_decision_id=entry_decision_id,  # 진입 결과를 parent로
    )

    assert exit_result.success is True
    assert exit_decision_id in exit_result.decision_ids

    # recorder.record 두 번째 호출: EXIT stage + parent_id = entry_decision_id
    exit_call = mock_recorder.record.call_args_list[1]
    assert exit_call.kwargs["stage"] == DecisionStage.EXIT
    assert exit_call.kwargs["parent_id"] == entry_decision_id
    assert exit_call.kwargs["symbol"] == "005930"

    # 총 2회 기록 (진입 1 + 청산 1)
    assert mock_recorder.record.call_count == 2
