"""src.strategy.trailing 단위 테스트 (F-10 공유 트레일링 헬퍼).

라이브 폴링·WS·백테스트·전략이 공유하는 트레일링 계산의 단일 출처를 검증한다.
"""

from __future__ import annotations

from decimal import Decimal

from src.core.models import OHLCV
from src.strategy.position_trading import PositionTradingStrategy
from src.strategy.swing_trading import SwingTradingStrategy
from src.strategy.trailing import (
    POSITION_TRAILING_FALLBACK_PCT,
    calculate_atr,
    entry_trailing_params,
    is_trailing_active,
    resolve_trailing_pct,
    trailing_activate_pct,
    trailing_stop_price,
)

_POSITION = "position"
_SWING = "swing"


def _ohlcv(close: Decimal, high: Decimal, low: Decimal) -> OHLCV:
    from datetime import date

    return OHLCV(
        symbol="005930",
        date=date(2026, 1, 1),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1000,
    )


# ── entry_trailing_params ────────────────────────────────────────────────


def test_entry_params_position():
    pct, days = entry_trailing_params(_POSITION, Decimal("70000"))
    assert pct == POSITION_TRAILING_FALLBACK_PCT
    assert days == PositionTradingStrategy.MAX_HOLDING_DAYS


def test_entry_params_swing():
    pct, days = entry_trailing_params(_SWING, Decimal("70000"))
    assert pct == SwingTradingStrategy.TRAILING_TRAIL_PCT
    assert days == SwingTradingStrategy.MAX_HOLDING_DAYS


def test_entry_params_unknown_strategy_returns_none():
    assert entry_trailing_params("unknown", Decimal("70000")) == (None, None)


# ── 활성화 게이트 ──────────────────────────────────────────────────────────


def test_trailing_activate_pct_per_strategy():
    assert trailing_activate_pct(_POSITION) == PositionTradingStrategy.TRAILING_ACTIVATE_PCT
    assert trailing_activate_pct(_SWING) == SwingTradingStrategy.TRAILING_ACTIVATE_PCT
    assert trailing_activate_pct("unknown") is None


def test_is_trailing_active_below_threshold_false():
    # POSITION 활성화 5% — 4%는 미활성
    assert is_trailing_active(_POSITION, Decimal("4.0")) is False


def test_is_trailing_active_at_threshold_true():
    assert is_trailing_active(_POSITION, Decimal("5.0")) is True
    assert is_trailing_active(_SWING, Decimal("3.0")) is True


def test_is_trailing_active_unknown_strategy_always_true():
    # 임계 미정 전략은 기존 동작 보존(항상 활성)
    assert is_trailing_active("unknown", Decimal("0.0")) is True


# ── 트레일링 폭 산출 ───────────────────────────────────────────────────────


def test_resolve_pct_position_atr_dynamic():
    # POSITION: ATR×1.5/진입가×100
    pct = resolve_trailing_pct(
        _POSITION, entry_price=Decimal("100"), stored_pct=Decimal("5"), atr=Decimal("2")
    )
    # 2 * 1.5 / 100 * 100 = 3.0
    assert pct == Decimal("3.0") * PositionTradingStrategy.TRAILING_ATR_MULT / Decimal("1.5")
    assert pct == Decimal("3.0")


def test_resolve_pct_position_atr_missing_falls_back_to_stored():
    pct = resolve_trailing_pct(
        _POSITION, entry_price=Decimal("100"), stored_pct=Decimal("5"), atr=None
    )
    assert pct == Decimal("5")


def test_resolve_pct_swing_uses_stored_fixed():
    # SWING은 ATR 무시(고정 폭)
    pct = resolve_trailing_pct(
        _SWING, entry_price=Decimal("100"), stored_pct=Decimal("5"), atr=Decimal("9")
    )
    assert pct == Decimal("5")


# ── 트레일링 스톱 가격 ─────────────────────────────────────────────────────


def test_trailing_stop_price_position_atr():
    # 폭 3% (ATR 2 × 1.5 / 100), 고점 110 → 110 * 0.97 = 106.7
    price = trailing_stop_price(
        _POSITION,
        entry_price=Decimal("100"),
        baseline_high=Decimal("110"),
        stored_pct=Decimal("5"),
        atr=Decimal("2"),
    )
    assert price == Decimal("110") * (Decimal("1") - Decimal("3.0") / Decimal("100"))


def test_trailing_stop_price_none_when_no_width():
    assert (
        trailing_stop_price(
            "unknown",
            entry_price=Decimal("100"),
            baseline_high=Decimal("110"),
            stored_pct=None,
            atr=None,
        )
        is None
    )


def test_trailing_stop_price_none_when_baseline_nonpositive():
    assert (
        trailing_stop_price(
            _SWING,
            entry_price=Decimal("100"),
            baseline_high=Decimal("0"),
            stored_pct=Decimal("5"),
        )
        is None
    )


# ── ATR ────────────────────────────────────────────────────────────────────


def test_calculate_atr_insufficient_data_returns_zero():
    bars = [_ohlcv(Decimal("100"), Decimal("101"), Decimal("99"))] * 3
    assert calculate_atr(bars, period=14) == Decimal("0")


def test_calculate_atr_simple():
    # 종가 100 고정, 매일 고가 102 / 저가 98 → TR=4 일정 → ATR=4
    bars = [_ohlcv(Decimal("100"), Decimal("102"), Decimal("98")) for _ in range(20)]
    atr = calculate_atr(bars, period=14)
    assert atr == Decimal("4")
