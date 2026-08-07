"""FillFinalizer 단위 테스트 — F-02 원자적 선점(claim) 멱등성."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import OrderStatus
from src.execution.fill_finalizer import FillFinalizer


def _make_factory(rowcount: int) -> tuple[MagicMock, AsyncMock]:
    """_claim_order의 조건부 UPDATE 결과(rowcount)를 제어하는 세션 팩토리."""
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = rowcount
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _make_finalizer(factory: MagicMock) -> FillFinalizer:
    return FillFinalizer(
        session_factory=factory,
        position_manager=AsyncMock(),
        telegram_bot=AsyncMock(),
        settings=MagicMock(),
    )


@pytest.mark.asyncio
async def test_claim_wins_when_rowcount_one() -> None:
    """SUBMITTED→target 전이가 1행 갱신되면 선점 성공(True)."""
    factory, _ = _make_factory(rowcount=1)
    finalizer = _make_finalizer(factory)
    won = await finalizer._claim_order(1, target=OrderStatus.FILLED)
    assert won is True


@pytest.mark.asyncio
async def test_claim_loses_when_rowcount_zero() -> None:
    """이미 다른 caller가 선점(상태가 SUBMITTED 아님) → rowcount 0 → False."""
    factory, _ = _make_factory(rowcount=0)
    finalizer = _make_finalizer(factory)
    won = await finalizer._claim_order(1, target=OrderStatus.FILLED)
    assert won is False


@pytest.mark.asyncio
async def test_apply_fill_skips_when_not_claimed() -> None:
    """선점 실패 시 체결 후처리(_record_execution 등) 자체를 건너뛴다."""
    factory, _ = _make_factory(rowcount=0)
    finalizer = _make_finalizer(factory)
    finalizer._record_execution = AsyncMock()
    order = MagicMock(id=1, side="buy", broker_order_id="X", account_id="a")

    from decimal import Decimal

    await finalizer._apply_fill(
        order=order,
        fill_price=Decimal("100"),
        fill_quantity=10,
        commission=Decimal("0"),
        executed_at=MagicMock(),
    )
    finalizer._record_execution.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_entry_position_threads_snapshot() -> None:
    """진입 체결 시 order의 entry_analysis_snapshot이 포지션 생성으로 전파된다."""
    from datetime import UTC, datetime
    from decimal import Decimal

    factory, _ = _make_factory(rowcount=1)
    pm = AsyncMock()
    pm.merge_or_create = AsyncMock(return_value=(MagicMock(id=10), False))
    settings = MagicMock()
    settings.STOP_LOSS_PERCENT = 5.0
    finalizer = FillFinalizer(
        session_factory=factory,
        position_manager=pm,
        telegram_bot=AsyncMock(),
        settings=settings,
    )
    finalizer._get_account = AsyncMock(return_value=MagicMock(strategy_type="swing"))
    finalizer._update_order = AsyncMock()
    finalizer._notify_safe = AsyncMock()

    snapshot = {
        "symbol": "005930",
        "action": "buy",
        "confidence": "0.8",
        "key_factors": [],
    }
    order = MagicMock(
        id=1,
        symbol="005930",
        session_id=None,
        account_id="default",
        approval_status="auto_approved",
        entry_analysis_snapshot=snapshot,
    )

    await finalizer._create_entry_position(
        order=order,
        fill_price=Decimal("72000"),
        fill_quantity=10,
        commission=Decimal("0"),
        executed_at=datetime.now(UTC),
        account_label="",
    )

    assert pm.merge_or_create.call_args.kwargs["entry_analysis_snapshot"] == snapshot


@pytest.mark.asyncio
async def test_mark_expired_closes_orphan_for_buy_order() -> None:
    """진입(BUY) 미체결 만료 — 조기 생성된 고아 포지션은 닫는다(기존 동작)."""
    factory, _ = _make_factory(rowcount=1)
    finalizer = _make_finalizer(factory)
    finalizer._close_orphaned_position = AsyncMock()
    finalizer._notify_safe = AsyncMock()
    finalizer._get_account_label = AsyncMock(return_value="테스트")

    order = MagicMock(
        id=1, side="buy", symbol="005930", account_id="default", position_id=42,
        status=OrderStatus.SUBMITTED.value,
    )
    await finalizer.mark_expired(order)

    finalizer._close_orphaned_position.assert_awaited_once_with(42, "005930")


@pytest.mark.asyncio
async def test_mark_expired_sell_order_does_not_close_position() -> None:
    """F-31: 청산(SELL) 주문의 position_id는 실보유 포지션 — 만료로 닫으면 안 된다."""
    factory, _ = _make_factory(rowcount=1)
    finalizer = _make_finalizer(factory)
    finalizer._close_orphaned_position = AsyncMock()
    finalizer._notify_safe = AsyncMock()
    finalizer._get_account_label = AsyncMock(return_value="테스트")

    order = MagicMock(
        id=1, side="sell", symbol="005930", account_id="default", position_id=42,
        status=OrderStatus.SUBMITTED.value,
    )
    await finalizer.mark_expired(order)

    finalizer._close_orphaned_position.assert_not_awaited()
    # 만료 알림 자체는 정상 발송
    finalizer._notify_safe.assert_awaited_once()
