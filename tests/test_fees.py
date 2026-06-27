"""거래 수수료/세금 추정 테스트 (F-01)."""

from decimal import Decimal

from src.core.enums import OrderSide
from src.execution.fees import estimate_commission


def test_buy_commission_estimate() -> None:
    """매수: 거래대금 × 0.015%."""
    c = estimate_commission(
        OrderSide.BUY,
        fill_price=Decimal("70000"),
        fill_quantity=100,
        buy_pct=0.015,
        sell_pct=0.195,
    )
    # 7,000,000 × 0.00015 = 1,050
    assert c == Decimal("1050")


def test_sell_commission_includes_tax() -> None:
    """매도: 거래대금 × 0.195% (수수료+거래세)."""
    c = estimate_commission(
        OrderSide.SELL,
        fill_price=Decimal("70000"),
        fill_quantity=100,
        buy_pct=0.015,
        sell_pct=0.195,
    )
    # 7,000,000 × 0.00195 = 13,650
    assert c == Decimal("13650")


def test_commission_is_rounded_to_won() -> None:
    """원 단위 반올림."""
    c = estimate_commission(
        OrderSide.BUY,
        fill_price=Decimal("12345"),
        fill_quantity=7,
        buy_pct=0.015,
        sell_pct=0.195,
    )
    # 86,415 × 0.00015 = 12.96 → 13
    assert c == Decimal("13")
