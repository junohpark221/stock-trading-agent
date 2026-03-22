"""ExitPriceCalculator — 손절/익절/트레일링 스톱 가격 계산 유틸리티.

Strategy 서브클래스(PositionTrading, SwingTrading)에서 호출하여
진입가 기반으로 exit 가격을 산출한다. 모든 메서드는 stateless static method.
"""

from decimal import ROUND_HALF_UP, Decimal

_HUNDRED = Decimal("100")
_ONE = Decimal("1")
_ZERO = Decimal("0")


class ExitPriceCalculator:
    """손절/익절/트레일링 스톱 가격 계산 유틸리티 (stateless)."""

    @staticmethod
    def fixed_percentage(
        entry_price: Decimal,
        stop_pct: Decimal,
        tp_pct: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """고정 비율 손절/익절 계산.

        Parameters
        ----------
        entry_price: 진입가 (> 0)
        stop_pct: 손절 비율 (%, ≥ 0)  예: Decimal("3.0") → 3%
        tp_pct: 익절 비율 (%, ≥ 0)  예: Decimal("5.0") → 5%

        Returns
        -------
        (stop_loss, take_profit) — 모두 Decimal
        """
        if entry_price <= _ZERO:
            msg = "entry_price must be positive"
            raise ValueError(msg)
        if stop_pct < _ZERO:
            msg = "stop_pct must be non-negative"
            raise ValueError(msg)
        if tp_pct < _ZERO:
            msg = "tp_pct must be non-negative"
            raise ValueError(msg)

        stop_loss = entry_price * (_ONE - stop_pct / _HUNDRED)
        take_profit = entry_price * (_ONE + tp_pct / _HUNDRED)
        return stop_loss, take_profit

    @staticmethod
    def atr_based(
        entry_price: Decimal,
        atr: Decimal,
        stop_mult: Decimal = Decimal("2.0"),
        tp_mult: Decimal = Decimal("3.0"),
    ) -> tuple[Decimal, Decimal]:
        """ATR 기반 손절/익절 계산.

        Parameters
        ----------
        entry_price: 진입가 (> 0)
        atr: Average True Range 값 (> 0)
        stop_mult: 손절 ATR 배수 (기본 2.0)
        tp_mult: 익절 ATR 배수 (기본 3.0)

        Returns
        -------
        (stop_loss, take_profit) — stop_loss는 최소 Decimal("1")
        """
        if entry_price <= _ZERO:
            msg = "entry_price must be positive"
            raise ValueError(msg)
        if atr <= _ZERO:
            msg = "atr must be positive"
            raise ValueError(msg)

        stop_loss = entry_price - atr * stop_mult
        # 주가는 0 이하가 될 수 없으므로 최소 1원
        if stop_loss <= _ZERO:
            stop_loss = _ONE

        take_profit = entry_price + atr * tp_mult
        return stop_loss, take_profit

    @staticmethod
    def trailing_stop_price(
        highest_since_entry: Decimal,
        trailing_pct: Decimal,
    ) -> Decimal:
        """트레일링 스톱 가격 계산.

        Parameters
        ----------
        highest_since_entry: 진입 후 최고가 (> 0)
        trailing_pct: 트레일링 비율 (%, > 0)

        Returns
        -------
        trailing_stop 가격
        """
        if highest_since_entry <= _ZERO:
            msg = "highest_since_entry must be positive"
            raise ValueError(msg)
        if trailing_pct <= _ZERO:
            msg = "trailing_pct must be positive"
            raise ValueError(msg)

        return highest_since_entry * (_ONE - trailing_pct / _HUNDRED)

    @staticmethod
    def risk_reward_ratio(
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
    ) -> Decimal:
        """리스크/리워드 비율 계산.

        Parameters
        ----------
        entry_price: 진입가
        stop_loss: 손절가 (≠ entry_price)
        take_profit: 익절가

        Returns
        -------
        ratio (소수점 2자리) — 예: Decimal("3.00") = 3:1
        """
        risk = abs(entry_price - stop_loss)
        if risk == _ZERO:
            msg = "entry_price and stop_loss must differ"
            raise ValueError(msg)

        reward = abs(take_profit - entry_price)
        ratio = reward / risk
        return ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
