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
