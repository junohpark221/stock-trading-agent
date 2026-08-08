"""PRJ-04 2단계 — 오픈 포지션 스냅샷 인덱싱(§10) 단위 테스트.

알렘빅 018 부분 유니크 인덱스가 `(account_id, symbol) WHERE status='open'` 을 강제하므로
"계좌·종목당 open 1행"은 불변식이다. 이 전제로 스냅샷을 인덱싱하는 헬퍼와 그 적용 지점
(exit_executor 매칭 · stoploss_stream 스냅샷)이 다음을 지키는지 검증한다.

- 정상 입력은 그대로 통과
- `(account_id, symbol)` 중복은 **최고령(먼저 온) 행 승** + warning 로그 (예외·알림 없음)
- 같은 종목이라도 **계좌가 다르면 둘 다 보존** (WS 다계좌 회귀 방지)
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import DecisionAction, ExitReason, StrategyType
from src.core.models import ExitSignal
from src.execution.exit_executor import ExitExecutionService
from src.execution.stoploss_stream import StopLossStreamService
from src.strategy import position_manager as pm_module
from src.strategy.exit_checker import ExitConditionChecker
from src.strategy.position_manager import open_by_symbol, unique_open_positions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pos(pid: int, symbol: str, account_id: str = "default") -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.symbol = symbol
    p.account_id = account_id
    p.status = "open"
    p.quantity = 10
    p.avg_cost = Decimal("70000")
    p.entry_price = Decimal("70000")
    p.stop_loss_price = Decimal("67000")
    p.take_profit_price = None
    p.trailing_stop_pct = None
    p.highest_price = None
    p.strategy_type = StrategyType.POSITION.value
    return p


@pytest.fixture()
def captured_logger(monkeypatch):
    """position_manager 모듈 로거를 가로채 warning 호출을 관찰한다."""
    fake = MagicMock()
    monkeypatch.setattr(pm_module, "logger", fake)
    return fake


# ---------------------------------------------------------------------------
# unique_open_positions
# ---------------------------------------------------------------------------


def test_unique_open_positions_passthrough(captured_logger):
    positions = [_pos(1, "005930"), _pos(2, "000660")]

    result = unique_open_positions(positions, context="t")

    assert [p.id for p in result] == [1, 2]
    captured_logger.warning.assert_not_called()


def test_unique_open_positions_keeps_first_and_warns(captured_logger):
    # get_open()이 entry_date/id 오름차순을 보장하므로 "먼저 온 행" == 최고령 행.
    positions = [_pos(1, "005930"), _pos(7, "005930"), _pos(9, "005930")]

    result = unique_open_positions(positions, context="t")

    assert [p.id for p in result] == [1]
    captured_logger.warning.assert_called_once()
    event, kwargs = (
        captured_logger.warning.call_args.args[0],
        captured_logger.warning.call_args.kwargs,
    )
    assert event == "position.duplicate_open_snapshot"
    assert kwargs["context"] == "t"
    assert kwargs["symbol"] == "005930"
    assert kwargs["kept_id"] == 1
    assert kwargs["dropped_ids"] == [7, 9]


def test_unique_open_positions_keeps_same_symbol_across_accounts(captured_logger):
    positions = [_pos(1, "005930", "a"), _pos(2, "005930", "b")]

    result = unique_open_positions(positions, context="t")

    assert [(p.account_id, p.id) for p in result] == [("a", 1), ("b", 2)]
    captured_logger.warning.assert_not_called()


def test_open_by_symbol_indexes_account_scoped_list(captured_logger):
    positions = [_pos(1, "005930"), _pos(2, "000660"), _pos(3, "005930")]

    index = open_by_symbol(positions, context="t")

    assert set(index) == {"005930", "000660"}
    assert index["005930"].id == 1  # 중복은 최고령 행 승
    captured_logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# exit_executor — 인덱스 경로 매칭 회귀
# ---------------------------------------------------------------------------


def _exit_service() -> tuple[ExitExecutionService, AsyncMock]:
    order_executor = AsyncMock()
    order_executor.execute_exit = AsyncMock(
        return_value=MagicMock(success=True, symbol="005930")
    )
    service = ExitExecutionService(
        order_executor=order_executor,
        position_manager=AsyncMock(),
        portfolio_service=AsyncMock(),
        recorder=AsyncMock(),
        telegram_bot=AsyncMock(),
    )
    return service, order_executor


def _signal(symbol: str) -> ExitSignal:
    return ExitSignal(
        symbol=symbol,
        reason=ExitReason.STOP_LOSS,
        urgency="immediate",
        current_price=Decimal("68000"),
        unrealized_pnl_pct=Decimal("-5.5"),
        recommended_action=DecisionAction.STOP_LOSS,
        reasoning="stop",
    )


@pytest.mark.asyncio
async def test_exit_executor_matches_by_symbol_index():
    service, order_executor = _exit_service()
    positions = [_pos(1, "000660"), _pos(2, "005930")]

    await service.process_exit_signals(
        [_signal("005930")], positions, session_id=uuid.uuid4(),
    )

    order_executor.execute_exit.assert_awaited_once()
    assert order_executor.execute_exit.await_args.kwargs["position"].id == 2


@pytest.mark.asyncio
async def test_exit_executor_skips_closed_and_unmatched():
    service, order_executor = _exit_service()
    closed = _pos(1, "005930")
    closed.status = "closed"

    results = await service.process_exit_signals(
        [_signal("005930")], [closed], session_id=uuid.uuid4(),
    )

    assert results == []
    order_executor.execute_exit.assert_not_awaited()


# ---------------------------------------------------------------------------
# stoploss_stream — 스냅샷 유입 경계 dedupe
# ---------------------------------------------------------------------------


def _stream_service(open_positions: list) -> StopLossStreamService:
    settings = MagicMock()
    settings.STOP_LOSS_WS_ENABLED = True
    settings.PRICE_STREAM_SYNC_INTERVAL_SEC = 30
    settings.EXIT_INFLIGHT_TTL_SEC = 120

    position_manager = AsyncMock()
    position_manager.get_open = AsyncMock(return_value=open_positions)

    svc = StopLossStreamService(
        settings=settings,
        position_manager=position_manager,
        coordinator=MagicMock(),
    )
    for account_id in ("default", "sub"):
        svc.register_account(
            account_id,
            exit_checker=ExitConditionChecker(max_drawdown_pct=Decimal("10")),
            exit_service=AsyncMock(),
            position_manager=AsyncMock(),
            account_label=f"{account_id} (1234)",
        )
    return svc


@pytest.mark.asyncio
async def test_sync_symbols_dedupes_same_account_symbol():
    svc = _stream_service([_pos(1, "005930"), _pos(5, "005930")])

    await svc._sync_symbols()

    assert [p.id for p in svc._positions] == [1]


@pytest.mark.asyncio
async def test_sync_symbols_keeps_multi_account_same_symbol():
    svc = _stream_service([_pos(1, "005930", "default"), _pos(2, "005930", "sub")])

    await svc._sync_symbols()

    assert {(p.account_id, p.id) for p in svc._positions} == {
        ("default", 1), ("sub", 2),
    }


@pytest.mark.asyncio
async def test_sync_symbols_drops_unregistered_accounts():
    svc = _stream_service([_pos(1, "005930", "default"), _pos(2, "000660", "ghost")])

    await svc._sync_symbols()

    assert [p.id for p in svc._positions] == [1]
