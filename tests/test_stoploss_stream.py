"""Tests for F-05 실시간 손절 — KISPriceStream 동적 구독 + StopLossStreamService 틱 평가."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.broker.kis.ws_codec import PriceTick
from src.broker.kis.ws_price import KISPriceStream
from src.core.enums import ExitReason
from src.execution.exit_coordinator import PHASE_PARTIAL_TP, ExitCoordinator
from src.execution.stoploss_stream import StopLossStreamService
from src.strategy.exit_checker import ExitConditionChecker

# ── Helpers ───────────────────────────────────────────────────────────


def _settings() -> MagicMock:
    s = MagicMock()
    s.STOP_LOSS_WS_ENABLED = True
    s.PRICE_STREAM_SYNC_INTERVAL_SEC = 30
    s.EXIT_INFLIGHT_TTL_SEC = 120
    s.WS_RECONNECT_BACKOFF_MAX_SEC = 60
    s.KIS_WS_URL_LIVE = "ws://live"
    s.KIS_WS_URL_PAPER = "ws://paper"
    s.KIS_WS_APPROVAL_URL_LIVE = "https://live/approval"
    s.KIS_WS_APPROVAL_URL_PAPER = "https://paper/approval"
    return s


def _make_position(
    *,
    pid: int = 1,
    symbol: str = "005930",
    account_id: str = "default",
    entry: Decimal = Decimal("70000"),
    stop: Decimal = Decimal("67000"),
    trailing: Decimal | None = None,
    highest: Decimal | None = None,
) -> MagicMock:
    pos = MagicMock()
    pos.id = pid
    pos.symbol = symbol
    pos.account_id = account_id
    pos.entry_price = entry
    pos.stop_loss_price = stop
    pos.take_profit_price = None
    pos.trailing_stop_pct = trailing
    pos.highest_price = highest
    pos.quantity = 10
    return pos


def _make_service(coordinator: ExitCoordinator | None = None):
    coord = coordinator or ExitCoordinator(ttl_sec=120)
    svc = StopLossStreamService(
        settings=_settings(),
        position_manager=AsyncMock(),  # service-level (get_open) — bypassed in tick tests
        coordinator=coord,
    )
    exit_service = AsyncMock()
    exit_service.process_exit_signals = AsyncMock(return_value=[MagicMock(success=True)])
    deps_pm = AsyncMock()  # per-account update_highest_price
    svc.register_account(
        "default",
        exit_checker=ExitConditionChecker(max_drawdown_pct=Decimal("10")),
        exit_service=exit_service,
        position_manager=deps_pm,
        account_label="테스트 (1234)",
    )
    return svc, exit_service, deps_pm, coord


# ── KISPriceStream dynamic subscription ───────────────────────────────


def _price_stream() -> KISPriceStream:
    return KISPriceStream(
        account_id="default",
        app_key="k", app_secret="s", is_paper=True,
        on_tick=AsyncMock(), settings=_settings(),
    )


@pytest.mark.asyncio
async def test_set_symbols_sends_subscribe_diff():
    stream = _price_stream()
    ws = MagicMock()
    ws.send = AsyncMock()
    stream._ws = ws
    stream._approval_key = "appkey"
    stream._subscribed = {"005930"}

    await stream.set_symbols({"005930", "000660"})

    # 신규(000660)만 구독 프레임 전송 (기존 005930은 재전송 안 함)
    assert ws.send.await_count == 1
    sent = json.loads(ws.send.await_args.args[0])
    assert sent["body"]["input"]["tr_key"] == "000660"
    assert sent["body"]["input"]["tr_id"] == "H0STCNT0"
    assert sent["header"]["tr_type"] == "1"
    assert stream._subscribed == {"005930", "000660"}


@pytest.mark.asyncio
async def test_set_symbols_sends_unsubscribe_for_removed():
    stream = _price_stream()
    ws = MagicMock()
    ws.send = AsyncMock()
    stream._ws = ws
    stream._approval_key = "appkey"
    stream._subscribed = {"005930", "000660"}

    await stream.set_symbols({"005930"})

    assert ws.send.await_count == 1
    sent = json.loads(ws.send.await_args.args[0])
    assert sent["body"]["input"]["tr_key"] == "000660"
    assert sent["header"]["tr_type"] == "0"  # 해제
    assert stream._subscribed == {"005930"}


@pytest.mark.asyncio
async def test_set_symbols_deferred_when_disconnected():
    stream = _price_stream()
    # _ws is None (미접속) → 전송 없이 desired만 갱신
    await stream.set_symbols({"005930"})
    assert stream._desired == {"005930"}
    assert stream._subscribed == set()


@pytest.mark.asyncio
async def test_handle_realtime_dispatches_tick():
    on_tick = AsyncMock()
    stream = _price_stream()
    stream._on_tick = on_tick
    record = ["005930", "093045", "71000"] + [str(i) for i in range(43)]
    frame = "0|H0STCNT0|1|" + "^".join(record)

    await stream._handle_realtime(frame)

    on_tick.assert_awaited_once()
    tick = on_tick.await_args.args[0]
    assert tick.symbol == "005930"
    assert tick.price == Decimal("71000")


# ── StopLossStreamService tick evaluation ─────────────────────────────


@pytest.mark.asyncio
async def test_tick_below_stop_triggers_exit_once():
    svc, exit_service, _pm, _coord = _make_service()
    pos = _make_position(stop=Decimal("67000"))
    svc._positions = [pos]

    # 손절가 하회 → 청산 1회 디스패치
    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("66000"), time="093045"))
    assert exit_service.process_exit_signals.await_count == 1

    # 연속 틱 → 스냅샷에서 제거되어 재디스패치 없음
    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("65000"), time="093046"))
    assert exit_service.process_exit_signals.await_count == 1


@pytest.mark.asyncio
async def test_tick_above_stop_no_exit():
    svc, exit_service, _pm, _coord = _make_service()
    svc._positions = [_make_position(stop=Decimal("67000"))]

    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("68000"), time="093045"))
    exit_service.process_exit_signals.assert_not_awaited()


@pytest.mark.asyncio
async def test_inflight_claim_blocks_exit():
    coord = ExitCoordinator(ttl_sec=120)
    await coord.try_claim(1)  # 폴링/다른 경로가 이미 선점
    svc, exit_service, _pm, _ = _make_service(coordinator=coord)
    svc._positions = [_make_position(pid=1, stop=Decimal("67000"))]

    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("66000"), time="093045"))
    exit_service.process_exit_signals.assert_not_awaited()  # 이중 청산 차단


@pytest.mark.asyncio
async def test_trailing_new_high_updates_highest_no_exit():
    svc, exit_service, deps_pm, _ = _make_service()
    pos = _make_position(
        stop=Decimal("67000"), trailing=Decimal("5.0"),
        highest=Decimal("72000"),
    )
    svc._positions = [pos]

    # 신고가(73000) → highest_price 갱신, 청산 없음 (트레일가 69350 > 73000 아님)
    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("73000"), time="093045"))
    deps_pm.update_highest_price.assert_awaited_once_with(1, Decimal("73000"))
    exit_service.process_exit_signals.assert_not_awaited()


@pytest.mark.asyncio
async def test_trailing_breach_triggers_exit():
    svc, exit_service, _pm, _ = _make_service()
    pos = _make_position(
        stop=Decimal("60000"), trailing=Decimal("5.0"),
        highest=Decimal("80000"),
    )
    svc._positions = [pos]

    # 트레일가 = 80000 * 0.95 = 76000. 현재가 75000 ≤ 76000 → 청산.
    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("75000"), time="093045"))
    assert exit_service.process_exit_signals.await_count == 1


@pytest.mark.asyncio
async def test_trailing_skipped_below_activation_threshold():
    """활성화 게이트(F-10 B2): 미실현 수익이 임계 미만이면 트레일링 미발동."""
    svc, exit_service, _pm, _ = _make_service()
    pos = _make_position(
        entry=Decimal("70000"), stop=Decimal("60000"),
        trailing=Decimal("5.0"), highest=Decimal("80000"),
    )
    pos.strategy_type = "swing"  # 활성화 임계 3%
    svc._positions = [pos]

    # 현재가 71000 → 미실현 +1.43% < 3% → 트레일가(76000) 하회여도 청산 안 함.
    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("71000"), time="093045"))
    exit_service.process_exit_signals.assert_not_awaited()


@pytest.mark.asyncio
async def test_trailing_fires_above_activation_threshold():
    """활성화 임계 이상이면 트레일링 정상 발동(F-10 B2)."""
    svc, exit_service, _pm, _ = _make_service()
    pos = _make_position(
        entry=Decimal("70000"), stop=Decimal("60000"),
        trailing=Decimal("5.0"), highest=Decimal("80000"),
    )
    pos.strategy_type = "swing"
    svc._positions = [pos]

    # 현재가 75000 → 미실현 +7.1% ≥ 3% 활성, 트레일가 76000 ≥ 75000 → 청산.
    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("75000"), time="093045"))
    assert exit_service.process_exit_signals.await_count == 1


@pytest.mark.asyncio
async def test_dispatch_failure_releases_claim():
    svc, exit_service, _pm, coord = _make_service()
    exit_service.process_exit_signals = AsyncMock(return_value=[MagicMock(success=False)])
    svc._positions = [_make_position(pid=1, stop=Decimal("67000"))]

    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("66000"), time="093045"))
    # 발주 실패 → 클레임 해제되어 재시도 가능
    assert await coord.is_claimed(1) is False


@pytest.mark.asyncio
async def test_unregistered_account_skipped():
    svc, exit_service, _pm, _ = _make_service()
    svc._positions = [_make_position(account_id="other", stop=Decimal("67000"))]

    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("66000"), time="093045"))
    exit_service.process_exit_signals.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_noop_when_disabled():
    svc, _es, _pm, _ = _make_service()
    svc._settings.STOP_LOSS_WS_ENABLED = False
    await svc.start([])
    assert svc._stream is None


# ── F-10 Phase 2: 부분익절 사다리 (POSITION) ──────────────────────────


@pytest.mark.asyncio
async def test_position_take_profit_triggers_partial():
    """POSITION이 익절가(+3ATR) 도달 → 33% 부분익절 신호(PARTIAL_TAKE_PROFIT)."""
    svc, exit_service, _pm, coord = _make_service()
    pos = _make_position(entry=Decimal("70000"), stop=Decimal("66000"))
    pos.strategy_type = "position"
    pos.take_profit_price = Decimal("76000")
    pos.quantity = 10
    svc._positions = [pos]

    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("77000"), time="093045"))

    exit_service.process_exit_signals.assert_awaited_once()
    signal = exit_service.process_exit_signals.call_args[0][0][0]
    assert signal.reason == ExitReason.PARTIAL_TAKE_PROFIT
    assert signal.exit_quantity == 3  # 33% × 10
    # partial_tp 위상으로 선점(보호 레그와 독립)
    assert await coord.is_claimed(pos.id, PHASE_PARTIAL_TP) is True


@pytest.mark.asyncio
async def test_swing_take_profit_full_exit_unchanged():
    """SWING은 사다리 미적용 — 익절가 도달 시 전량 익절(회귀 가드)."""
    svc, exit_service, _pm, _ = _make_service()
    pos = _make_position(entry=Decimal("70000"), stop=Decimal("66000"))
    pos.strategy_type = "swing"
    pos.take_profit_price = Decimal("73000")
    pos.trailing_stop_pct = None  # 트레일링 비활성 → 익절가 도달 시 매도
    pos.quantity = 10
    svc._positions = [pos]

    await svc._on_tick(PriceTick(symbol="005930", price=Decimal("74000"), time="093045"))

    exit_service.process_exit_signals.assert_awaited_once()
    signal = exit_service.process_exit_signals.call_args[0][0][0]
    assert signal.reason == ExitReason.TAKE_PROFIT
    assert signal.exit_quantity is None  # 전량
