"""거래 수수료/세금 추정 (F-01).

KIS WebSocket 체결통보(`ExecutionEvent`)에는 수수료 필드가 없으므로, WS 경로로
확정되는 체결의 수수료를 거래대금 기반으로 추정한다. REST `get_order_status`
경로는 `OrderResult.commission` 실값을 쓰므로 이 추정을 사용하지 않는다.

요율 기본값은 백테스트 모델(`src/backtest/simulator.py`)과 동일:
- 매수: 0.015% (증권사 수수료)
- 매도: 0.195% (증권사 수수료 0.015% + 증권거래세 0.18%)
운영 환경별 조정은 `Settings.COMMISSION_BUY_PCT` / `COMMISSION_SELL_PCT`로.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from src.core.enums import OrderSide


def estimate_commission(
    side: OrderSide,
    *,
    fill_price: Decimal,
    fill_quantity: int,
    buy_pct: float | Decimal,
    sell_pct: float | Decimal,
) -> Decimal:
    """거래대금(체결가 × 수량) 기반 수수료/세금 추정. 원 단위 반올림."""
    amount = Decimal(fill_price) * Decimal(fill_quantity)
    pct = buy_pct if side == OrderSide.BUY else sell_pct
    rate = Decimal(str(pct)) / Decimal("100")
    return (amount * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


__all__ = ["estimate_commission"]
