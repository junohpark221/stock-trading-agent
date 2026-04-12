"""ExitConditionChecker — 전략 공통 청산 조건 체크 유틸리티.

Strategy 서브클래스에서 check_exit_conditions() 구현 시 개별 조건 체크를
위임받는다. 각 메서드는 조건 충족 시 ExitSignal을, 미충족 시 None을 반환.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.core.enums import DecisionAction, ExitReason
from src.core.models import ExitSignal, PortfolioState
from src.db.models.strategy import PositionRecord


def _count_trading_days(start: date, end: date) -> int:
    """start ~ end 사이 거래일 수 (주말 제외, 공휴일 미반영)."""
    if start >= end:
        return 0
    total = 0
    current = start
    one_day = timedelta(days=1)
    while current < end:
        if current.weekday() < 5:  # 월~금
            total += 1
        current += one_day
    return total


class ExitConditionChecker:
    """전략 공통 청산 조건 체크 유틸리티.

    Strategy 서브클래스(PositionTrading, SwingTrading)에서 호출하여
    포지션별 청산 조건을 판단한다. 각 메서드는 stateless하며,
    호출자가 현재가와 미실현 손익률을 사전 계산하여 전달한다.
    """

    def __init__(self, *, max_drawdown_pct: Decimal) -> None:
        self._max_drawdown_pct = max_drawdown_pct

    # ── 개별 청산 조건 체크 ────────────────────────────────────────────────

    def check_stop_loss(
        self,
        position: PositionRecord,
        current_price: Decimal,
        unrealized_pnl_pct: Decimal,
    ) -> ExitSignal | None:
        """손절 체크 — 현재가 ≤ 손절가이면 즉시 청산 시그널.

        # 우선순위 최고: 추가 손실 방지를 위해 즉시 청산
        # urgency = "immediate"
        """
        if current_price <= position.stop_loss_price:
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.STOP_LOSS,
                urgency="immediate",
                current_price=current_price,
                trigger_price=position.stop_loss_price,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.STOP_LOSS,
                reasoning=(
                    f"손절가 도달: 현재가 {current_price:,} ≤ "
                    f"손절가 {position.stop_loss_price:,} "
                    f"(손실률 {unrealized_pnl_pct:.1f}%)"
                ),
            )
        return None

    def check_take_profit(
        self,
        position: PositionRecord,
        current_price: Decimal,
        unrealized_pnl_pct: Decimal,
    ) -> ExitSignal | None:
        """익절 체크 — 현재가 ≥ 익절가이면 장 마감 시 청산 시그널.

        # 익절가 미설정(None) 시 None 반환
        # urgency = "end_of_day" (급하지 않음)
        """
        if (
            position.take_profit_price is not None
            and current_price >= position.take_profit_price
        ):
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.TAKE_PROFIT,
                urgency="end_of_day",
                current_price=current_price,
                trigger_price=position.take_profit_price,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.TAKE_PROFIT,
                reasoning=(
                    f"익절가 도달: 현재가 {current_price:,} ≥ "
                    f"익절가 {position.take_profit_price:,} "
                    f"(수익률 {unrealized_pnl_pct:.1f}%)"
                ),
            )
        return None

    def check_trailing_stop(
        self,
        position: PositionRecord,
        current_price: Decimal,
        unrealized_pnl_pct: Decimal,
        trailing_stop_price: Decimal,
    ) -> ExitSignal | None:
        """트레일링 스톱 체크 — 현재가 ≤ 트레일링 스톱가이면 즉시 청산.

        # trailing_stop_price는 호출자가 사전 계산 (ATR 기반 or 고정 비율)
        # urgency = "immediate" (수익 보호 목적)

        Parameters
        ----------
        position: 포지션 레코드
        current_price: 현재가
        unrealized_pnl_pct: 미실현 손익률 (%)
        trailing_stop_price: 트레일링 스톱 가격 (호출자가 계산)
        """
        if current_price <= trailing_stop_price:
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.TRAILING_STOP,
                urgency="immediate",
                current_price=current_price,
                trigger_price=trailing_stop_price,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.SELL,
                reasoning=(
                    f"트레일링 스톱 발동: 현재가 {current_price:,} ≤ "
                    f"트레일링가 {trailing_stop_price:,} "
                    f"(수익률 {unrealized_pnl_pct:.1f}%)"
                ),
            )
        return None

    def check_time_based(
        self,
        position: PositionRecord,
        current_price: Decimal,
        unrealized_pnl_pct: Decimal,
        today: date,
        *,
        time_urgency: str = "end_of_day",
    ) -> ExitSignal | None:
        """시간 기반 청산 — 최대 보유 기간 초과 시 청산 시그널.

        # max_holding_days 미설정(None) 시 None 반환
        # time_urgency: Position 전략 = "next_session", Swing 전략 = "end_of_day"

        Parameters
        ----------
        position: 포지션 레코드
        current_price: 현재가
        unrealized_pnl_pct: 미실현 손익률 (%)
        today: 기준일 (테스트 용이성을 위해 명시적 전달)
        time_urgency: 청산 긴급도 (기본: "end_of_day")
        """
        if position.max_holding_days is None:
            return None

        days_held = _count_trading_days(position.entry_date, today)
        if days_held >= position.max_holding_days:
            return ExitSignal(
                symbol=position.symbol,
                reason=ExitReason.TIME_BASED,
                urgency=time_urgency,
                current_price=current_price,
                trigger_price=None,
                unrealized_pnl_pct=unrealized_pnl_pct,
                recommended_action=DecisionAction.SELL,
                reasoning=(
                    f"최대 보유 기간 초과: {days_held}일 보유 "
                    f"(한도 {position.max_holding_days}일, "
                    f"수익률 {unrealized_pnl_pct:.1f}%)"
                ),
            )
        return None

    # ── 포트폴리오 레벨 체크 ──────────────────────────────────────────────

    def check_drawdown_exit(self, portfolio_state: PortfolioState) -> bool:
        """포트폴리오 드로다운 체크 — 최대 드로다운 초과 시 전체 청산 신호.

        # drawdown_pct > max_drawdown_pct → True (모든 포지션 긴급 청산)
        # 개별 ExitSignal이 아닌 bool 반환 (포트폴리오 전체에 영향)
        """
        return portfolio_state.drawdown_pct > self._max_drawdown_pct
