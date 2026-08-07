"""PRJ-04 1단계 — PositionManager.merge_or_create (MTS식 포지션 병합) 단위 테스트.

동일 종목 추가매수가 새 open 행을 만들지 않고 기존 행에 가중평균으로 병합되는지,
손절·익절 재산정(§4)·보유 시계 리셋(§7)·트레일링 해제(§5)·가설 교체(§6)·
entry_trigger union(§1)이 규칙대로 적용되는지 검증한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.exceptions import DatabaseError
from src.db.models.strategy import PositionRecord
from src.strategy.position_manager import PositionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    """merge_or_create가 쓰는 최소 async session 스텁."""

    def __init__(self, existing: PositionRecord | None = None) -> None:
        self.existing = existing
        self.added: list = []
        self.commits = 0
        self.commit_error: Exception | None = None

    async def execute(self, stmt):  # noqa: ANN001
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.existing
        return result

    def add(self, obj):  # noqa: ANN001
        self.added.append(obj)
        if not getattr(obj, "id", None):
            obj.id = 999

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    async def refresh(self, obj):  # noqa: ANN001
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _factory(*sessions: _FakeSession):
    """호출 순서대로 세션을 내주는 session_factory 스텁."""
    queue = list(sessions)

    def make():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return make


def _existing_position(**overrides) -> PositionRecord:
    """기존 open 포지션 — 10주 @ 70,000 (손절 3% / 익절 5%)."""
    pos = PositionRecord(
        symbol="005930",
        strategy_type="position",
        quantity=10,
        avg_cost=Decimal("70000"),
        entry_price=Decimal("70000"),
        entry_date=date(2026, 1, 2),
        stop_loss_price=Decimal("67900"),
        take_profit_price=Decimal("73500"),
        trailing_stop_pct=Decimal("5.0"),
        highest_price=Decimal("78000"),
        max_holding_days=20,
        status="open",
        account_id="default",
        realized_pnl=None,
        entry_trigger=["rsi_oversold"],
        entry_analysis_snapshot={"confidence": "0.7"},
    )
    pos.id = 1
    for k, v in overrides.items():
        setattr(pos, k, v)
    return pos


async def _merge(manager: PositionManager, **overrides):
    """10주 @ 80,000 추가매수(손절 3% / 익절 5%)."""
    kwargs = {
        "symbol": "005930",
        "strategy_type": "position",
        "quantity": 10,
        "entry_price": Decimal("80000"),
        "stop_loss_price": Decimal("77600"),
        "take_profit_price": Decimal("84000"),
        "trailing_stop_pct": Decimal("4.0"),
        "max_holding_days": 30,
        "entry_session_id": None,
        "account_id": "default",
        "entry_analysis_snapshot": None,
    }
    kwargs.update(overrides)
    return await manager.merge_or_create(**kwargs)


# ---------------------------------------------------------------------------
# 신규 생성 (기존 open 행 없음)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_existing_position_inserts_new_row():
    """기존 open 행이 없으면 기존과 동일하게 INSERT하고 merged=False."""
    session = _FakeSession(existing=None)
    manager = PositionManager(_factory(session))

    record, merged = await _merge(manager)

    assert merged is False
    assert len(session.added) == 1
    assert record.avg_cost == Decimal("80000")
    assert record.entry_price == Decimal("80000")
    assert record.quantity == 10
    assert record.status == "open"


@pytest.mark.asyncio
async def test_entry_trigger_promoted_from_snapshot_on_insert():
    """F-14 회귀 — 신규 생성 시 스냅샷의 entry_trigger가 전용 컬럼으로 승격."""
    session = _FakeSession(existing=None)
    manager = PositionManager(_factory(session))

    record, _ = await _merge(
        manager, entry_analysis_snapshot={"entry_trigger": ["macd_cross"]}
    )

    assert record.entry_trigger == ["macd_cross"]


# ---------------------------------------------------------------------------
# 병합 (기존 open 행 있음)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_uses_weighted_average_cost():
    """수량 합산 + Decimal 가중평균 평단. entry_price(최초 체결가)는 불변."""
    existing = _existing_position()
    session = _FakeSession(existing=existing)
    manager = PositionManager(_factory(session))

    record, merged = await _merge(manager)

    assert merged is True
    assert record is existing
    assert session.added == []  # 새 행을 만들지 않는다
    assert record.quantity == 20
    assert record.avg_cost == Decimal("75000.00")  # (70000×10 + 80000×10) / 20
    assert record.entry_price == Decimal("70000")  # §3: 최초 체결가 보존


@pytest.mark.asyncio
async def test_merge_rebases_stop_and_tp_with_incoming_width():
    """§4 — 이번 추가매수의 손절/익절 폭(3%/5%)을 새 평단(75,000)에 재적용."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager)

    assert record.stop_loss_price == Decimal("72750.00")  # 75000 × 0.97
    assert record.take_profit_price == Decimal("78750.00")  # 75000 × 1.05


@pytest.mark.asyncio
async def test_merge_falls_back_to_existing_width_when_incoming_missing():
    """폭 정보가 없는 경로(리컨실러·fill_finalizer 폴백)는 기존 포지션 폭을 유지."""
    existing = _existing_position(
        stop_loss_price=Decimal("63000"),  # 10% 폭
        take_profit_price=Decimal("77000"),  # 10% 폭
    )
    session = _FakeSession(existing=existing)
    manager = PositionManager(_factory(session))

    record, _ = await _merge(
        manager, stop_loss_price=Decimal("0"), take_profit_price=None
    )

    assert record.stop_loss_price == Decimal("67500.00")  # 75000 × 0.90
    assert record.take_profit_price == Decimal("82500.00")  # 75000 × 1.10


@pytest.mark.asyncio
async def test_merge_keeps_existing_tp_width_when_incoming_has_no_tp():
    """이번 주문에 익절가가 없으면 기존 익절 폭을 새 평단에 재적용(손절은 이번 폭)."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager, take_profit_price=None)

    assert record.stop_loss_price == Decimal("72750.00")  # 이번 폭 3%
    assert record.take_profit_price == Decimal("78750.00")  # 기존 폭 5%


@pytest.mark.asyncio
async def test_merge_restores_take_profit_on_trailing_runner():
    """§5 — 트레일링 전환 포지션(TP=NULL)도 이번 주문의 익절 폭으로 복원된다."""
    existing = _existing_position(
        take_profit_price=None,  # transition_to_trailing 흔적
        stop_loss_price=Decimal("70000"),  # 본전 플로어
    )
    session = _FakeSession(existing=existing)
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager)

    assert record.take_profit_price == Decimal("78750.00")
    assert record.highest_price is None  # 고점 추적 리셋


@pytest.mark.asyncio
async def test_merge_resets_holding_clock_and_trailing_state():
    """§7 보유 시계 리셋 + §5 트레일링 상태 리셋."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager)

    assert record.entry_date == date.today()
    assert record.max_holding_days == 30
    assert record.trailing_stop_pct == Decimal("4.0")
    assert record.highest_price is None


@pytest.mark.asyncio
async def test_merge_keeps_existing_optional_values_when_incoming_none():
    """trailing_stop_pct·max_holding_days가 없으면 기존 값을 덮지 않는다."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager, trailing_stop_pct=None, max_holding_days=None)

    assert record.trailing_stop_pct == Decimal("5.0")
    assert record.max_holding_days == 20


@pytest.mark.asyncio
async def test_merge_unions_entry_trigger():
    """§1 — entry_trigger는 union(중복 제거, 기존 순서 유지). F-14 귀인 보존."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(
        manager,
        entry_analysis_snapshot={"entry_trigger": ["rsi_oversold", "bb_bounce"]},
    )

    assert record.entry_trigger == ["rsi_oversold", "bb_bounce"]


@pytest.mark.asyncio
async def test_merge_replaces_snapshot_and_session_id():
    """§6 — 가설은 최신 분석으로 교체하고 entry_session_id도 함께 옮긴다."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))
    new_sid = uuid4()

    record, _ = await _merge(
        manager,
        entry_analysis_snapshot={"confidence": "0.9"},
        entry_session_id=new_sid,
    )

    assert record.entry_analysis_snapshot == {"confidence": "0.9"}
    assert record.entry_session_id == new_sid


@pytest.mark.asyncio
async def test_merge_keeps_snapshot_when_incoming_is_none():
    """스냅샷이 없는 경로(리컨실러 등)는 기존 가설을 지우지 않는다."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager, entry_analysis_snapshot=None)

    assert record.entry_analysis_snapshot == {"confidence": "0.7"}


@pytest.mark.asyncio
async def test_merge_preserves_realized_pnl():
    """§5 — 기실현 부분익절 누적분은 병합해도 보존."""
    session = _FakeSession(
        existing=_existing_position(realized_pnl=Decimal("120000"))
    )
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager)

    assert record.realized_pnl == Decimal("120000")


@pytest.mark.asyncio
async def test_merge_keeps_existing_strategy_type_on_mismatch():
    """전략 유형이 다르면 기존 값을 유지(경고 로그만)."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record, _ = await _merge(manager, strategy_type="swing")

    assert record.strategy_type == "position"


# ---------------------------------------------------------------------------
# 조회 조건 / 동시성
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_filters_account_symbol_open_and_locks_row():
    """조회는 (account_id, symbol, status='open') + FOR UPDATE + 결정론적 정렬."""
    session = _FakeSession(existing=None)
    captured: list = []

    async def capture(stmt):
        captured.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    session.execute = capture
    manager = PositionManager(_factory(session))

    await _merge(manager, account_id="acct-1")

    sql = captured[0]
    assert "positions.account_id = 'acct-1'" in sql
    assert "positions.symbol = '005930'" in sql
    assert "positions.status = 'open'" in sql
    assert "FOR UPDATE" in sql
    # entry_date 동률(같은 날 중복 행)에서도 결정론적이도록 id 타이브레이크 필수.
    assert "ORDER BY positions.entry_date ASC, positions.id ASC" in sql


@pytest.mark.asyncio
async def test_insert_conflict_retries_into_merge():
    """§2 — INSERT 유니크 충돌(동시 체결 경합)이면 재조회해 병합으로 착지."""
    losing = _FakeSession(existing=None)
    losing.commit_error = IntegrityError("insert", {}, Exception("duplicate key"))
    winning = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(losing, winning))

    record, merged = await _merge(manager)

    assert merged is True
    assert record.quantity == 20
    assert record.avg_cost == Decimal("75000.00")


@pytest.mark.asyncio
async def test_retry_failure_raises_database_error():
    """재시도까지 실패하면 DatabaseError로 변환(호출자 CRITICAL 경로)."""
    first = _FakeSession(existing=None)
    first.commit_error = IntegrityError("insert", {}, Exception("duplicate key"))
    second = _FakeSession(existing=None)
    second.commit_error = RuntimeError("connection lost")
    manager = PositionManager(_factory(first, second))

    with pytest.raises(DatabaseError, match="Position merge retry failed"):
        await _merge(manager)


# ---------------------------------------------------------------------------
# create() 호환 래퍼
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_wrapper_delegates_and_merges():
    """미전환 호출자가 쓰는 create()도 병합 경유 — 유니크 인덱스와 충돌하지 않는다."""
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session))

    record = await manager.create(
        symbol="005930",
        strategy_type="position",
        quantity=10,
        entry_price=Decimal("80000"),
        stop_loss_price=Decimal("77600"),
    )

    assert isinstance(record, PositionRecord)
    assert record.quantity == 20
    assert session.added == []


@pytest.mark.asyncio
async def test_merge_does_not_record_trade_outcome():
    """병합은 청산이 아니므로 학습 메모리 기록이 발생하지 않는다."""
    memory = AsyncMock()
    session = _FakeSession(existing=_existing_position())
    manager = PositionManager(_factory(session), memory_manager=memory)

    await _merge(manager)

    memory.record_trade_outcome.assert_not_awaited()
