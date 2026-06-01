"""PositionReconciler 단위 테스트.

3-way 동기화 케이스:
- Case 1: DB only  → closed (기존 동작 회귀)
- Case 2: broker only → 신규 PositionRecord 생성
- Case 3: 수량·단가 불일치 → DB 갱신
- position_manager=None 시 Case 2/3 스킵
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import ExitReason, StrategyType
from src.execution.reconciler import PositionReconciler, ReconcileResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_broker_position(
    symbol: str,
    quantity: int = 10,
    average_cost: Decimal = Decimal("50000"),
    entry_date: datetime | None = None,
) -> MagicMock:
    p = MagicMock()
    p.symbol = symbol
    p.quantity = quantity
    p.average_cost = average_cost
    p.entry_date = entry_date or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return p


def _make_db_position(
    symbol: str,
    position_id: int = 1,
    quantity: int = 10,
    avg_cost: Decimal = Decimal("50000"),
    entry_price: Decimal = Decimal("50000"),
) -> MagicMock:
    p = MagicMock()
    p.id = position_id
    p.symbol = symbol
    p.quantity = quantity
    p.avg_cost = avg_cost
    p.entry_price = entry_price
    p.status = "open"
    return p


def _make_session_factory(open_positions: list) -> MagicMock:
    """DB 세션에서 open_positions를 반환하는 mock factory."""
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = open_positions

    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    execute_result.scalar_one_or_none.return_value = (
        open_positions[0] if open_positions else None
    )

    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _make_registry(broker: MagicMock, account_id: str = "acc1") -> MagicMock:
    registry = MagicMock()
    registry.get_all.return_value = {account_id: broker}
    return registry


def _make_broker(positions: list) -> MagicMock:
    from src.broker.base import BrokerInterface

    broker = MagicMock(spec=BrokerInterface)
    broker.get_positions = AsyncMock(return_value=positions)
    return broker


def _make_position_manager() -> MagicMock:
    pm = AsyncMock()
    pm.create = AsyncMock(return_value=MagicMock(id=99))
    pm.update_quantity = AsyncMock(return_value=MagicMock())
    return pm


# ---------------------------------------------------------------------------
# Case 1: DB only → closed (기존 동작 회귀)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_closes_orphan():
    """브로커에 없는 DB 포지션은 RECONCILED 사유로 closed 처리된다."""
    db_pos = _make_db_position("005930", position_id=1)
    broker = _make_broker([])  # 브로커에 아무것도 없음
    registry = _make_registry(broker)

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [db_pos]
    # scalar_one_or_none → 닫을 때 record 반환
    execute_result.scalar_one_or_none.return_value = db_pos
    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    reconciler = PositionReconciler(
        broker_registry=registry,
        session_factory=factory,
        position_manager=None,
    )

    # _correct_linked_orders 내부 session도 처리
    with patch.object(reconciler, "_correct_linked_orders", AsyncMock(return_value=0)):
        result = await reconciler.reconcile()

    assert result.closed_count == 1
    assert result.created_count == 0
    assert result.qty_updated_count == 0
    # 닫힌 DB 레코드에 상태/사유 설정 확인
    assert db_pos.status == "closed"
    assert db_pos.exit_reason == ExitReason.RECONCILED.value
    assert db_pos.realized_pnl == Decimal("0")


# ---------------------------------------------------------------------------
# Case 2: broker only → 신규 생성
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_creates_missing_position():
    """브로커에만 있는 포지션은 strategy=manual로 DB에 신규 생성된다."""
    bp = _make_broker_position("005930", quantity=5, average_cost=Decimal("60000"))
    broker = _make_broker([bp])
    registry = _make_registry(broker)

    # DB에는 open 포지션 없음
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    pm = _make_position_manager()
    reconciler = PositionReconciler(
        broker_registry=registry,
        session_factory=factory,
        position_manager=pm,
    )

    result = await reconciler.reconcile()

    assert result.created_count == 1
    assert result.closed_count == 0
    assert result.qty_updated_count == 0

    pm.create.assert_awaited_once()
    call_kwargs = pm.create.call_args.kwargs
    assert call_kwargs["symbol"] == "005930"
    assert call_kwargs["strategy_type"] == StrategyType.MANUAL.value
    assert call_kwargs["quantity"] == 5
    assert call_kwargs["entry_price"] == Decimal("60000")
    # 기본 손절가 = 평균단가 * (1 - 0.05)
    assert call_kwargs["stop_loss_price"] == Decimal("60000") * Decimal("0.95")
    assert call_kwargs["account_id"] == "acc1"


@pytest.mark.asyncio
async def test_reconcile_no_create_without_position_manager():
    """position_manager=None이면 broker-only 포지션을 생성하지 않는다."""
    bp = _make_broker_position("005930")
    broker = _make_broker([bp])
    registry = _make_registry(broker)

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=execute_result)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    reconciler = PositionReconciler(
        broker_registry=registry,
        session_factory=factory,
        position_manager=None,
    )

    result = await reconciler.reconcile()
    assert result.created_count == 0


# ---------------------------------------------------------------------------
# Case 3: 수량·단가 불일치 → DB 갱신
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_updates_quantity():
    """브로커 수량이 DB와 다를 때 update_quantity가 호출된다."""
    db_pos = _make_db_position("005930", position_id=7, quantity=10, avg_cost=Decimal("50000"))
    bp = _make_broker_position("005930", quantity=8, average_cost=Decimal("51000"))
    broker = _make_broker([bp])
    registry = _make_registry(broker)

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [db_pos]
    execute_result.scalar_one_or_none.return_value = db_pos
    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    pm = _make_position_manager()
    reconciler = PositionReconciler(
        broker_registry=registry,
        session_factory=factory,
        position_manager=pm,
    )

    result = await reconciler.reconcile()

    assert result.qty_updated_count == 1
    assert result.closed_count == 0
    assert result.created_count == 0

    pm.update_quantity.assert_awaited_once_with(
        7, quantity=8, avg_cost=Decimal("51000")
    )


@pytest.mark.asyncio
async def test_reconcile_no_update_when_quantities_match():
    """수량과 단가가 동일하면 update_quantity가 호출되지 않는다."""
    db_pos = _make_db_position("005930", position_id=3, quantity=10, avg_cost=Decimal("50000"))
    bp = _make_broker_position("005930", quantity=10, average_cost=Decimal("50000"))
    broker = _make_broker([bp])
    registry = _make_registry(broker)

    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [db_pos]
    session.execute = AsyncMock(return_value=execute_result)
    session.commit = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    pm = _make_position_manager()
    reconciler = PositionReconciler(
        broker_registry=registry,
        session_factory=factory,
        position_manager=pm,
    )

    result = await reconciler.reconcile()

    assert result.qty_updated_count == 0
    pm.update_quantity.assert_not_awaited()


# ---------------------------------------------------------------------------
# ReconcileResult 기본값 확인
# ---------------------------------------------------------------------------


def test_reconcile_result_defaults():
    r = ReconcileResult()
    assert r.closed_count == 0
    assert r.created_count == 0
    assert r.qty_updated_count == 0
    assert r.order_corrected_count == 0
    assert r.broker_symbol_count == 0
    assert r.db_open_count == 0
