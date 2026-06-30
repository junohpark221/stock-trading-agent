"""트레일링 스톱 공통 계산 — 라이브 폴링·WS·백테스트가 공유하는 단일 출처.

F-10(트레일링·시간 청산 라이브 배선) 완전 이식의 핵심. 전략별 트레일링
파라미터(활성화 임계 수익률, 트레일링 폭)와 활성화 게이트("수익 N% 후 활성화"),
ATR 기반 동적 폭 산출을 한 곳에 모아 **라이브 청산과 백테스트의 트레일링 동작이
갈리지 않게** 한다.

전략 상수(TRAILING_ACTIVATE_PCT 등)는 각 전략 클래스가 진실의 원천이며, 여기서는
지연(함수-로컬) 임포트로 읽어 순환 참조를 피한다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from src.core.enums import StrategyType
from src.strategy.exit_calculator import ExitPriceCalculator

if TYPE_CHECKING:
    from src.core.models import OHLCV

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")

# POSITION 전략 트레일링 폭의 폴백(%). POSITION은 ATR×배수/진입가로 동적 산출이
# 원칙이나, 진입 시점/ATR 결측 시 저장·사용할 기본 폭. 백테스트 기본값(5.0)과 정합.
POSITION_TRAILING_FALLBACK_PCT = Decimal("5.0")

# 부분익절 비율(F-10 Phase 2). POSITION 전략이 +3ATR(익절가) 도달 시 이 비율만
# 부분익절하고 잔량은 트레일링으로 전환한다(추세 추종의 오른쪽 꼬리 수익 확보).
PARTIAL_TP_RATIO = Decimal("0.33")


def partial_tp_quantity(strategy_type: str, position_quantity: int) -> int | None:
    """부분익절 수량을 산출(F-10 Phase 2). 사다리 미적용이면 None.

    POSITION 전략만 부분익절 사다리를 적용한다(SWING 등은 기존 전량/스킵 유지).
    수량이 1주 이하이거나 비율 적용 결과가 잔량 전량이면 None을 반환해 호출부가
    기존 전량 익절 경로로 빠지게 한다.

    Returns:
        부분익절 수량(주, 1 ≤ q < position_quantity). 사다리 미적용이면 None.
    """
    if strategy_type != StrategyType.POSITION.value:
        return None
    if position_quantity <= 1:
        return None
    q = max(1, int(Decimal(position_quantity) * PARTIAL_TP_RATIO))
    if q >= position_quantity:
        return None
    return q


def _params(strategy_type: str) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """전략별 (활성화 임계%, ATR 배수, 고정 트레일 폭%)를 반환.

    - POSITION: (TRAILING_ACTIVATE_PCT, TRAILING_ATR_MULT, None) — ATR 동적 폭.
    - SWING: (TRAILING_ACTIVATE_PCT, None, TRAILING_TRAIL_PCT) — 고정 폭.
    - 그 외: (None, None, None).
    """
    if strategy_type == StrategyType.POSITION.value:
        from src.strategy.position_trading import PositionTradingStrategy

        return (
            PositionTradingStrategy.TRAILING_ACTIVATE_PCT,
            PositionTradingStrategy.TRAILING_ATR_MULT,
            None,
        )
    if strategy_type == StrategyType.SWING.value:
        from src.strategy.swing_trading import SwingTradingStrategy

        return (
            SwingTradingStrategy.TRAILING_ACTIVATE_PCT,
            None,
            SwingTradingStrategy.TRAILING_TRAIL_PCT,
        )
    return None, None, None


def entry_trailing_params(
    strategy_type: str, entry_price: Decimal
) -> tuple[Decimal | None, int | None]:
    """진입 시 포지션에 저장할 (trailing_stop_pct, max_holding_days)를 산출(F-10 B1).

    두 값이 비-NULL이어야 라이브 청산 게이트(폴링/WS/exit_checker)가 동작한다.
    POSITION은 ATR 동적이 원칙이라 폭은 폴백 상수로 두고, 라이브 폴링이 매 사이클
    ATR로 재계산한다(B3). SWING은 고정 폭을 그대로 저장한다.
    """
    if strategy_type == StrategyType.POSITION.value:
        from src.strategy.position_trading import PositionTradingStrategy

        return POSITION_TRAILING_FALLBACK_PCT, PositionTradingStrategy.MAX_HOLDING_DAYS
    if strategy_type == StrategyType.SWING.value:
        from src.strategy.swing_trading import SwingTradingStrategy

        return SwingTradingStrategy.TRAILING_TRAIL_PCT, SwingTradingStrategy.MAX_HOLDING_DAYS
    return None, None


def trailing_activate_pct(strategy_type: str) -> Decimal | None:
    """전략별 트레일링 활성화 임계 수익률(%). 알 수 없으면 None."""
    return _params(strategy_type)[0]


def is_trailing_active(strategy_type: str, unrealized_pnl_pct: Decimal) -> bool:
    """활성화 게이트 — 미실현 수익률이 전략 임계 이상이면 트레일링 활성(F-10 B2).

    임계가 정의되지 않은 전략은 항상 활성으로 간주(기존 동작 보존).
    """
    activate = trailing_activate_pct(strategy_type)
    if activate is None:
        return True
    return unrealized_pnl_pct >= activate


def resolve_trailing_pct(
    strategy_type: str,
    *,
    entry_price: Decimal,
    stored_pct: Decimal | None,
    atr: Decimal | None,
) -> Decimal | None:
    """전략별 트레일링 폭(%)을 산출.

    POSITION: ATR×TRAILING_ATR_MULT/진입가×100 (ATR 결측 시 stored_pct 폴백).
    SWING/기타: stored_pct(고정 폭).
    """
    _, atr_mult, _ = _params(strategy_type)
    if (
        atr_mult is not None
        and atr is not None
        and atr > _ZERO
        and entry_price > _ZERO
    ):
        return (atr * atr_mult / entry_price) * _HUNDRED
    return stored_pct


def trailing_stop_price(
    strategy_type: str,
    *,
    entry_price: Decimal,
    baseline_high: Decimal,
    stored_pct: Decimal | None,
    atr: Decimal | None = None,
) -> Decimal | None:
    """트레일링 스톱 가격 산출(폭 결정 + 고점 대비 적용).

    Returns None — 트레일링 폭을 정할 수 없거나 baseline_high가 비양수일 때.
    """
    pct = resolve_trailing_pct(
        strategy_type, entry_price=entry_price, stored_pct=stored_pct, atr=atr
    )
    if pct is None or pct <= _ZERO or baseline_high <= _ZERO:
        return None
    return ExitPriceCalculator.trailing_stop_price(
        highest_since_entry=baseline_high, trailing_pct=pct
    )


def calculate_atr(ohlcv_list: list[OHLCV], period: int = 14) -> Decimal:
    """ATR(Average True Range) — SMA 방식. 데이터 부족 시 Decimal("0").

    True Range = max(고가-저가, |고가-전일종가|, |저가-전일종가|)의 최근 period일 SMA.
    PositionTradingStrategy._calculate_atr의 단일 출처(전략은 이 함수에 위임).
    """
    if len(ohlcv_list) < period + 1:
        return _ZERO

    true_ranges: list[Decimal] = []
    for i in range(1, len(ohlcv_list)):
        high = ohlcv_list[i].high
        low = ohlcv_list[i].low
        prev_close = ohlcv_list[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    recent_trs = true_ranges[-period:]
    return sum(recent_trs) / Decimal(str(period))
