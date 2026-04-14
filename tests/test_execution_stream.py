"""Tests for ExecutionStreamManager waiter/dispatcher mechanics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import OrderSide
from src.core.models import ExecutionEvent
from src.execution.execution_stream import ExecutionStreamManager


def _make_event(
    *,
    broker_order_id: str = "KIS123",
    is_filled: bool = True,
    is_rejected: bool = False,
) -> ExecutionEvent:
    return ExecutionEvent(
        account_id="default",
        broker_order_id=broker_order_id,
        symbol="005930",
        side=OrderSide.BUY,
        filled_quantity=10 if is_filled else 0,
        filled_price=Decimal("72000") if is_filled else Decimal(0),
        is_filled=is_filled,
        is_rejected=is_rejected,
        rejected_reason="",
        timestamp=datetime.now(UTC),
    )


def _make_mgr() -> ExecutionStreamManager:
    settings = MagicMock()
    settings.EXECUTION_STREAM_ENABLED = True
    settings.ORDER_FILL_TIMEOUT_SEC = 30
    session_factory = MagicMock()
    fill_finalizer = MagicMock()
    fill_finalizer.finalize_from_event = AsyncMock()
    return ExecutionStreamManager(
        settings=settings,
        session_factory=session_factory,
        fill_finalizer=fill_finalizer,
    )


@pytest.mark.asyncio
async def test_waiter_resolves_when_event_arrives_first():
    mgr = _make_mgr()

    # 먼저 이벤트 도착 (waiter 없음) → 버퍼링
    await mgr._on_event(_make_event())

    # 이후 wait_for_fill → 버퍼된 이벤트 즉시 반환
    event = await mgr.wait_for_fill(
        broker_order_id="KIS123", account_id="default", timeout=5,
    )
    assert event.is_filled is True
    assert event.broker_order_id == "KIS123"


@pytest.mark.asyncio
async def test_waiter_resolves_when_event_arrives_later():
    mgr = _make_mgr()

    # wait_for_fill 먼저 호출
    async def consumer():
        return await mgr.wait_for_fill(
            broker_order_id="KIS123", account_id="default", timeout=5,
        )

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)  # 대기자 등록 대기

    # 그 다음 이벤트 도착
    await mgr._on_event(_make_event())

    event = await asyncio.wait_for(task, timeout=3)
    assert event.is_filled is True


@pytest.mark.asyncio
async def test_wait_for_fill_timeout_raises():
    mgr = _make_mgr()

    with pytest.raises(asyncio.TimeoutError):
        await mgr.wait_for_fill(
            broker_order_id="NEVER", account_id="default", timeout=0.2,
        )

    # 타임아웃 후 waiter 정리됐는지 확인
    assert "NEVER" not in mgr._waiters


@pytest.mark.asyncio
async def test_orphan_event_falls_back_to_finalizer():
    mgr = _make_mgr()

    # 이벤트 도착하지만 아무도 대기하지 않음 → late_finalize가 finalizer 호출
    await mgr._on_event(_make_event(broker_order_id="ORPHAN"))

    # late_finalize는 2초 후 실행 — 대기
    await asyncio.sleep(2.3)

    mgr._fill_finalizer.finalize_from_event.assert_awaited_once()
    # waiter는 청소됨
    assert "ORPHAN" not in mgr._waiters


@pytest.mark.asyncio
async def test_disabled_stream_start_is_noop():
    settings = MagicMock()
    settings.EXECUTION_STREAM_ENABLED = False
    mgr = ExecutionStreamManager(
        settings=settings,
        session_factory=MagicMock(),
        fill_finalizer=MagicMock(),
    )
    await mgr.start([])  # should not raise
    assert len(mgr._streams) == 0
