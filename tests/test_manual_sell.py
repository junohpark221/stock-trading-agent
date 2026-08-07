"""F-28: 수동 매도 공용 포지션 해석기 단위 테스트.

resolve_manual_sell의 매칭·검증 정책(0건 거부 / 다중 시 최오래된 행 / 수량 초과 거부 /
명시 position_id 검증)과 build_manual_exit_signal의 ExitSignal 구성을 고정한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.enums import DecisionAction, ExitReason
from src.execution.manual_sell import (
    ManualSellPlan,
    ManualSellRejection,
    build_manual_exit_signal,
    resolve_manual_sell,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_position(
    *,
    pos_id: int = 1,
    symbol: str = "005930",
    quantity: int = 10,
    status: str = "open",
    account_id: str = "default",
    entry_date: date = date(2026, 8, 1),
    avg_cost: Decimal = Decimal("72000"),
):
    pos = MagicMock()
    pos.id = pos_id
    pos.symbol = symbol
    pos.quantity = quantity
    pos.status = status
    pos.account_id = account_id
    pos.entry_date = entry_date
    pos.avg_cost = avg_cost
    return pos


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """select()는 rows를 그대로 돌려주고, get()은 by_id 맵을 조회한다."""

    def __init__(self, rows=None, by_id=None):
        self._rows = rows or []
        self._by_id = by_id or {}
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult(self._rows)

    async def get(self, model, pk):
        return self._by_id.get(pk)


# ---------------------------------------------------------------------------
# 자동 매칭 경로
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_open_position_rejected():
    session = FakeSession(rows=[])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="005930", quantity=5,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "no_open_position"
    assert "포지션 동기화" in result.message


@pytest.mark.asyncio
async def test_single_position_resolved():
    pos = _make_position(pos_id=7, symbol="005930", quantity=10)
    session = FakeSession(rows=[pos])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="005930", quantity=4,
    )

    assert isinstance(result, ManualSellPlan)
    assert result.position is pos
    assert result.symbol == "005930"      # 포지션 기준으로 확정
    assert result.quantity == 4
    assert result.is_ambiguous is False


@pytest.mark.asyncio
async def test_symbol_is_normalized_for_lookup():
    pos = _make_position(symbol="005930")
    session = FakeSession(rows=[pos])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="  005930  ", quantity=1,
    )

    assert isinstance(result, ManualSellPlan)


@pytest.mark.asyncio
async def test_multiple_positions_picks_oldest():
    """F-27 중복 — 쿼리 정렬(entry_date, id) 결과의 첫 행을 대상으로 삼는다."""
    older = _make_position(pos_id=12, quantity=10, entry_date=date(2026, 8, 3))
    newer = _make_position(pos_id=19, quantity=20, entry_date=date(2026, 8, 5))
    session = FakeSession(rows=[older, newer])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="005930", quantity=10,
    )

    assert isinstance(result, ManualSellPlan)
    assert result.position is older
    assert result.candidates == [older, newer]
    assert result.is_ambiguous is True


@pytest.mark.asyncio
async def test_multiple_positions_order_by_includes_id_tiebreak():
    """같은 entry_date 중복에서도 결정론을 보장하도록 id 타이브레이크가 걸려야 한다."""
    same_day_a = _make_position(pos_id=12, entry_date=date(2026, 8, 5))
    same_day_b = _make_position(pos_id=19, entry_date=date(2026, 8, 5))
    session = FakeSession(rows=[same_day_a, same_day_b])

    await resolve_manual_sell(
        session=session, account_id="default", symbol="005930", quantity=1,
    )

    compiled = str(session.executed[0]).lower()
    assert "order by" in compiled
    assert "entry_date asc" in compiled
    assert "positions.id asc" in compiled


@pytest.mark.asyncio
async def test_quantity_exceeds_rejected_with_candidates():
    older = _make_position(pos_id=12, quantity=10, entry_date=date(2026, 8, 3))
    newer = _make_position(pos_id=19, quantity=20, entry_date=date(2026, 8, 5))
    session = FakeSession(rows=[older, newer])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="005930", quantity=30,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "quantity_exceeds"
    assert "#12" in result.message and "#19" in result.message
    assert result.candidates == [older, newer]


@pytest.mark.asyncio
async def test_invalid_quantity_rejected():
    session = FakeSession(rows=[_make_position()])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="005930", quantity=0,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "invalid_quantity"


@pytest.mark.asyncio
async def test_missing_symbol_rejected():
    session = FakeSession(rows=[])

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol=None, quantity=1,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "missing_symbol"


# ---------------------------------------------------------------------------
# 명시 position_id 경로
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_position_id_happy_path():
    pos = _make_position(pos_id=42, symbol="000660", quantity=8)
    session = FakeSession(by_id={42: pos})

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol=None, quantity=8, position_id=42,
    )

    assert isinstance(result, ManualSellPlan)
    assert result.position is pos
    assert result.symbol == "000660"
    assert result.quantity == 8


@pytest.mark.asyncio
async def test_explicit_position_id_not_found():
    session = FakeSession(by_id={})

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol=None, quantity=1, position_id=99,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "position_not_found"


@pytest.mark.asyncio
async def test_explicit_position_id_not_open():
    pos = _make_position(pos_id=42, status="closed")
    session = FakeSession(by_id={42: pos})

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol=None, quantity=1, position_id=42,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "position_not_open"


@pytest.mark.asyncio
async def test_explicit_position_id_account_mismatch():
    pos = _make_position(pos_id=42, account_id="other")
    session = FakeSession(by_id={42: pos})

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol=None, quantity=1, position_id=42,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "account_mismatch"


@pytest.mark.asyncio
async def test_explicit_position_id_symbol_mismatch():
    pos = _make_position(pos_id=42, symbol="005930")
    session = FakeSession(by_id={42: pos})

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol="000660", quantity=1,
        position_id=42,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "symbol_mismatch"


@pytest.mark.asyncio
async def test_explicit_position_id_quantity_exceeds():
    pos = _make_position(pos_id=42, quantity=5)
    session = FakeSession(by_id={42: pos})

    result = await resolve_manual_sell(
        session=session, account_id="default", symbol=None, quantity=6, position_id=42,
    )

    assert isinstance(result, ManualSellRejection)
    assert result.code == "quantity_exceeds"


# ---------------------------------------------------------------------------
# ExitSignal 구성
# ---------------------------------------------------------------------------


def test_build_manual_exit_signal():
    pos = _make_position(symbol="005930", avg_cost=Decimal("70000"))

    signal = build_manual_exit_signal(
        position=pos, price=Decimal("77000"), reasoning="manual",
    )

    assert signal.symbol == "005930"
    assert signal.reason == ExitReason.MANUAL
    assert signal.urgency == "immediate"
    assert signal.recommended_action == DecisionAction.SELL
    assert signal.unrealized_pnl_pct == Decimal("10")


def test_build_manual_exit_signal_zero_avg_cost():
    pos = _make_position(avg_cost=Decimal("0"))

    signal = build_manual_exit_signal(
        position=pos, price=Decimal("70000"), reasoning="manual",
    )

    # avg_cost가 0/None이면 현재가로 대체 → 손익률 0
    assert signal.unrealized_pnl_pct == Decimal("0")
