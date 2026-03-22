"""PositionSizer — Fixed Fractional 포지션 사이징.

진입가, 손절가, 포트폴리오 총 가치를 기반으로 적정 수량을 계산한다.
AlgoRiskManager(Step 4)와 상호보완 관계:
- PositionSizer: 초기 수량 계산 (사전)
- AlgoRiskManager: 최종 리스크 검증 (사후)
"""

from decimal import ROUND_HALF_UP, Decimal

from src.config import Settings
from src.core.models import PositionSizing
from src.strategy.exit_calculator import ExitPriceCalculator

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


class PositionSizer:
    """Fixed Fractional 포지션 사이징."""

    def __init__(self, settings: Settings) -> None:
        self._risk_pct = Decimal(str(settings.RISK_PER_TRADE_PCT))
        self._max_position_pct = Decimal(str(settings.MAX_POSITION_PCT))
        self._max_position_krw = Decimal(str(settings.MAX_POSITION_SIZE_KRW))

    def calculate(
        self,
        symbol: str,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        take_profit_price: Decimal | None,
        total_portfolio_value: Decimal,
        existing_position_value: Decimal = _ZERO,
    ) -> PositionSizing:
        """고정비율법으로 적정 매수 수량을 계산한다.

        Parameters
        ----------
        symbol: 종목 코드
        entry_price: 진입 예상가 (> 0)
        stop_loss_price: 손절가 (> 0, ≠ entry_price)
        take_profit_price: 익절가 (None이면 risk_reward_ratio 미계산)
        total_portfolio_value: 포트폴리오 총 가치 (> 0)
        existing_position_value: 해당 종목 기존 보유 금액 (기본 0)

        Returns
        -------
        PositionSizing — quantity=0이면 진입 불가 (캡 초과)
        """
        # ── 1. 입력 검증 ──────────────────────────────────────────
        if entry_price <= _ZERO:
            msg = "entry_price must be positive"
            raise ValueError(msg)
        if stop_loss_price <= _ZERO:
            msg = "stop_loss_price must be positive"
            raise ValueError(msg)
        if entry_price == stop_loss_price:
            msg = "stop_loss_price must differ from entry_price"
            raise ValueError(msg)
        if total_portfolio_value <= _ZERO:
            msg = "total_portfolio_value must be positive"
            raise ValueError(msg)

        # ── 2. 리스크 기반 수량 (Fixed Fractional) ────────────────
        risk_amount = total_portfolio_value * self._risk_pct / _HUNDRED
        risk_per_share = abs(entry_price - stop_loss_price)
        raw_qty = int(risk_amount / risk_per_share)
        quantity = max(1, raw_qty)  # 최소 1주 floor

        # ── 3. MAX_POSITION_PCT 캡 ────────────────────────────────
        max_by_pct = total_portfolio_value * self._max_position_pct / _HUNDRED
        max_qty_pct = int(max_by_pct / entry_price)

        # ── 4. MAX_POSITION_SIZE_KRW 캡 ──────────────────────────
        max_qty_krw = int(self._max_position_krw / entry_price)

        # ── 5. 기존 포지션 차감 ──────────────────────────────────
        existing_equiv = int(existing_position_value / entry_price)
        cap_from_pct = max(0, max_qty_pct - existing_equiv)
        cap_from_krw = max(0, max_qty_krw - existing_equiv)

        # ── 6. 최소값 적용 ───────────────────────────────────────
        quantity = min(quantity, cap_from_pct, cap_from_krw)
        quantity = max(0, quantity)  # 캡에 의해 0이 될 수 있음

        # ── 7. 출력 필드 계산 ────────────────────────────────────
        qty_dec = Decimal(quantity)
        position_value_krw = qty_dec * entry_price
        actual_risk_amount = qty_dec * risk_per_share

        risk_pct_of_portfolio = (
            (actual_risk_amount / total_portfolio_value * _HUNDRED).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if quantity > 0
            else _ZERO
        )
        position_pct_of_portfolio = (
            (position_value_krw / total_portfolio_value * _HUNDRED).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if quantity > 0
            else _ZERO
        )

        # ── 8. risk_reward_ratio ─────────────────────────────────
        rr_ratio: Decimal | None = None
        if take_profit_price is not None:
            rr_ratio = ExitPriceCalculator.risk_reward_ratio(
                entry_price, stop_loss_price, take_profit_price
            )

        # ── 9. PositionSizing 반환 ───────────────────────────────
        return PositionSizing(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            risk_per_share=risk_per_share,
            quantity=quantity,
            position_value_krw=position_value_krw,
            risk_amount_krw=actual_risk_amount,
            risk_pct_of_portfolio=risk_pct_of_portfolio,
            position_pct_of_portfolio=position_pct_of_portfolio,
            risk_reward_ratio=rr_ratio,
        )
