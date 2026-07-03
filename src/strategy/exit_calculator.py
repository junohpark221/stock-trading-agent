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
    def atr_clamped(
        entry_price: Decimal,
        atr: Decimal,
        stop_mult: Decimal = Decimal("1.5"),
        floor_pct: Decimal = Decimal("2.5"),
        cap_pct: Decimal = Decimal("6.0"),
        rr_ratio: Decimal = Decimal("1.67"),
    ) -> tuple[Decimal, Decimal]:
        """ATR 연동 손절폭(clamp) + R:R 보존 익절 계산 (F-13).

        변동성으로 선별한 종목의 청산을 변동성에 맞춘다. 손절 *폭(%)* 을
        ``clamp(stop_mult × ATR%, floor_pct, cap_pct)`` 로 산출해 저변동은 하한
        근처, 고변동은 상한까지 완충을 넓힌다(고변동 노이즈 손절 완화). 익절은
        실제 손절폭에 ``rr_ratio`` 를 곱해 산출하므로 clamp가 걸려도 R:R이 종목
        변동성과 무관하게 일정하게 보존된다. 리스크%는 사이저가 수량을 조정해
        일정하게 유지한다(사이저 무변경).

        Parameters
        ----------
        entry_price: 진입가 (> 0)
        atr: Average True Range 값 (> 0)
        stop_mult: 손절 ATR 배수 (기본 1.5)
        floor_pct: 손절폭 하한 % (기본 2.5)
        cap_pct: 손절폭 상한 % (기본 6.0)
        rr_ratio: 익절/손절 비 (기본 1.67 = 기존 5%/3% 계승)

        Returns
        -------
        (stop_loss, take_profit) — 모두 Decimal
        """
        if entry_price <= _ZERO:
            msg = "entry_price must be positive"
            raise ValueError(msg)
        if atr <= _ZERO:
            msg = "atr must be positive"
            raise ValueError(msg)

        atr_pct = atr / entry_price * _HUNDRED
        width_pct = stop_mult * atr_pct
        # clamp(width_pct, floor, cap) — 저변동 과소·고변동 과대 방지
        if width_pct < floor_pct:
            width_pct = floor_pct
        elif width_pct > cap_pct:
            width_pct = cap_pct

        stop_loss = entry_price * (_ONE - width_pct / _HUNDRED)
        # R:R 보존: 익절폭 = 실제 손절폭 × rr_ratio
        take_profit = entry_price + rr_ratio * (entry_price - stop_loss)
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
