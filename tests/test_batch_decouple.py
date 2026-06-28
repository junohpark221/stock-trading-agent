"""배치 재구성(결정/실행 분리 + 게이트) 단위 테스트.

- _previous_trading_day: 주말·공휴일 스킵
- job_execution_drain: 당일가/갭 게이트(통과 발주 / 초과 만료) + 전일 이월 만료
- StopLossStreamService._evaluate: WS 익절 평가(트레일링 무/유에 따른 분기)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import DecisionAction, OrderType
from src.core.models import TradeDecision
from src.execution.decision_queue import PendingDecision
from src.scheduler.jobs import (
    _previous_trading_day,
    job_execution_drain,
)

# ── _previous_trading_day ────────────────────────────────────────────────


def test_previous_trading_day_skips_weekend():
    # 2026-06-29는 월요일 → 직전 거래일은 금요일(06-26)
    assert _previous_trading_day(date(2026, 6, 29)) == date(2026, 6, 26)


def test_previous_trading_day_skips_holiday():
    # 화요일(06-30) 기준, 직전 영업일 월요일(06-29)이 공휴일이면 금요일(06-26)
    assert _previous_trading_day(
        date(2026, 6, 30), holidays="2026-06-29"
    ) == date(2026, 6, 26)


# ── job_execution_drain: 당일가/갭 게이트 ─────────────────────────────────


def _pending(symbol="005930", ref="70000", qty=10, pid=1, expires_at=None):
    decision = TradeDecision(
        symbol=symbol,
        action=DecisionAction.BUY,
        confidence=Decimal("0.8"),
        order_type=OrderType.LIMIT,
        quantity=qty,
        price=Decimal(ref),
    )
    return PendingDecision(
        id=pid,
        account_id="acct-1",
        symbol=symbol,
        reference_price=Decimal(ref),
        strategy_type="swing",
        session_id=None,
        decision=decision,
        entry_analysis_snapshot=None,
        expires_at=expires_at,
    )


def _drain_kwargs(queue, broker, order_executor, **over):
    base = dict(
        queue=queue,
        order_executor=order_executor,
        broker=broker,
        strategy_type="swing",
        allocator=None,
        account_id="acct-1",
        account_label="공격형",
        market_open="09:00",
        market_close="15:30",
        holidays="",
        gap_guard_pct=3.0,
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_drain_executes_within_gap(monkeypatch):
    """갭이 한도 내면 라이브가로 발주하고 executed 전이."""
    monkeypatch.setattr("src.scheduler.jobs._is_market_open", lambda *a, **k: True)
    queue = AsyncMock()
    queue.get_pending = AsyncMock(return_value=[_pending(ref="70000", pid=1)])
    broker = AsyncMock()
    broker.get_price = AsyncMock(
        return_value=MagicMock(current_price=Decimal("70500"))  # ~0.7% 갭
    )
    order_executor = AsyncMock()
    order_executor.execute_entry = AsyncMock(
        return_value=MagicMock(success=True, order_id=123)
    )

    await job_execution_drain(**_drain_kwargs(queue, broker, order_executor))

    order_executor.execute_entry.assert_awaited_once()
    # 라이브가(70500)로 진입가 갱신되어 발주되었는지 확인
    sent = order_executor.execute_entry.await_args.kwargs["trade_decision"]
    assert sent.price == Decimal("70500")
    queue.mark_executed.assert_awaited_once_with(1, 123)


@pytest.mark.asyncio
async def test_drain_gap_guard_expires(monkeypatch):
    """갭이 한도 초과면 발주하지 않고 gap_guard로 만료."""
    monkeypatch.setattr("src.scheduler.jobs._is_market_open", lambda *a, **k: True)
    queue = AsyncMock()
    queue.get_pending = AsyncMock(return_value=[_pending(ref="70000", pid=7)])
    broker = AsyncMock()
    broker.get_price = AsyncMock(
        return_value=MagicMock(current_price=Decimal("75000"))  # ~7.1% 갭
    )
    order_executor = AsyncMock()

    await job_execution_drain(**_drain_kwargs(queue, broker, order_executor))

    order_executor.execute_entry.assert_not_awaited()
    queue.mark_expired.assert_awaited_once_with([7], "gap_guard")


@pytest.mark.asyncio
async def test_drain_expires_stale_pending(monkeypatch):
    """expires_at 지난(전일 이월) pending은 먼저 만료 처리."""
    monkeypatch.setattr("src.scheduler.jobs._is_market_open", lambda *a, **k: True)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    queue = AsyncMock()
    queue.get_pending = AsyncMock(
        return_value=[_pending(pid=9, expires_at=yesterday)]
    )
    broker = AsyncMock()
    order_executor = AsyncMock()

    await job_execution_drain(**_drain_kwargs(queue, broker, order_executor))

    queue.mark_expired.assert_awaited_once_with([9], "expired_eod")
    broker.get_price.assert_not_awaited()
    order_executor.execute_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_skips_when_market_closed(monkeypatch):
    """장 마감 시 아무것도 하지 않음."""
    monkeypatch.setattr("src.scheduler.jobs._is_market_open", lambda *a, **k: False)
    queue = AsyncMock()
    await job_execution_drain(
        **_drain_kwargs(queue, AsyncMock(), AsyncMock())
    )
    queue.get_pending.assert_not_awaited()


# ── StopLossStreamService._evaluate: WS 익절 ─────────────────────────────


def _ws_service(exit_checker, exit_service, position_manager):
    from src.execution.exit_coordinator import ExitCoordinator
    from src.execution.stoploss_stream import StopLossStreamService

    svc = StopLossStreamService(
        settings=MagicMock(),
        position_manager=position_manager,
        coordinator=ExitCoordinator(ttl_sec=120.0),
    )
    svc.register_account(
        "acct-1",
        exit_checker=exit_checker,
        exit_service=exit_service,
        position_manager=position_manager,
        account_label="공격형",
    )
    return svc


def _ws_position(trailing=None):
    pos = MagicMock()
    pos.id = 1
    pos.account_id = "acct-1"
    pos.symbol = "005930"
    pos.entry_price = Decimal("70000")
    pos.highest_price = None
    pos.trailing_stop_pct = trailing
    return pos


@pytest.mark.asyncio
async def test_ws_take_profit_sells_without_trailing():
    """트레일링 미설정 + 익절 도달 → WS가 익절 매도 신호로 청산."""
    tp_signal = MagicMock()
    exit_checker = MagicMock()
    exit_checker.check_stop_loss = MagicMock(return_value=None)
    exit_checker.check_take_profit = MagicMock(return_value=tp_signal)
    exit_service = AsyncMock()
    exit_service.process_exit_signals = AsyncMock(return_value=[MagicMock(success=True)])
    position_manager = AsyncMock()
    svc = _ws_service(exit_checker, exit_service, position_manager)
    svc._positions = [_ws_position(trailing=None)]

    await svc._evaluate(_ws_position(trailing=None), Decimal("80000"))

    exit_checker.check_take_profit.assert_called_once()
    exit_service.process_exit_signals.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_take_profit_with_trailing_does_not_sell():
    """트레일링 설정 + 익절 도달 → 즉시 매도 안 함(트레일링 무장, 트레일링가 미도달)."""
    exit_checker = MagicMock()
    exit_checker.check_stop_loss = MagicMock(return_value=None)
    exit_checker.check_take_profit = MagicMock(return_value=MagicMock())
    exit_checker.check_trailing_stop = MagicMock(return_value=None)  # 고점 대비 미도달
    exit_service = AsyncMock()
    position_manager = AsyncMock()
    svc = _ws_service(exit_checker, exit_service, position_manager)
    pos = _ws_position(trailing=Decimal("5"))
    svc._positions = [pos]

    await svc._evaluate(pos, Decimal("80000"))

    exit_checker.check_trailing_stop.assert_called_once()
    exit_service.process_exit_signals.assert_not_awaited()
