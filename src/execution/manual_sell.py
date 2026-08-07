"""수동 매도 공용 포지션 해석 — F-28.

수동 매도(텔레그램 ``/sell`` · REST ``POST /api/orders/execute`` · 어드민 백오피스)는
반드시 기존 open 포지션을 대상으로 ``OrderExecutor.execute_exit`` 경로를 타야 한다.
진입 경로(``execute_entry``)로 매도를 내면 두 가지가 동시에 깨진다.

1. 인라인 체결 확정(``_finalize_entry_fill``)이 매도 체결로 **phantom open 포지션**을 만든다.
2. 주문에 ``position_id``가 없어 비동기 확정(``FillFinalizer._close_exit_position``)이
   **아무 포지션도 닫지 못한다** — 주문만 FILLED로 찍히고 실보유 포지션은 open으로 남는다.

세 UI(텔레그램 HTML · REST 422 JSON · 어드민 redirect flash)는 렌더링만 각자 하고,
매칭·검증 정책은 이 모듈 한 곳에서만 정의한다.

동일 종목 open 포지션이 여러 건인 상황(F-27)에서는 **가장 오래된 행**을 대상으로 삼는다
(스케줄러 청산 ``ExitExecutionService._match_signal_to_position``과 같은 규칙).
PRJ-04(포지션 병합)가 반영되면 중복 자체가 사라져 이 규칙은 자연히 무의미해진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from src.core.enums import DecisionAction, ExitReason
from src.core.models import ExitSignal
from src.db.models.strategy import PositionRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

MANUAL_SYNC_HINT = "포지션 동기화(어드민 계좌 상세 → 포지션 동기화) 후 다시 시도하세요."


@dataclass(frozen=True)
class ManualSellPlan:
    """해석 성공 — execute_exit에 그대로 넘길 수 있는 청산 대상."""

    position: PositionRecord
    symbol: str                                        # 포지션 기준으로 확정된 종목
    quantity: int                                      # 검증된 매도 수량
    candidates: list[PositionRecord] = field(default_factory=list)  # 동일 종목 open 행 전체

    @property
    def is_ambiguous(self) -> bool:
        """동일 종목 open 포지션이 2건 이상(F-27) — 호출자가 경고를 덧붙일 근거."""
        return len(self.candidates) > 1


@dataclass(frozen=True)
class ManualSellRejection:
    """해석 실패 — 브로커 발주 전에 차단한다."""

    code: str      # no_open_position | quantity_exceeds | invalid_quantity | missing_symbol
                   # | position_not_found | position_not_open | account_mismatch | symbol_mismatch
    message: str   # 한국어 평문(HTML 이스케이프는 호출자 몫)
    candidates: list[PositionRecord] = field(default_factory=list)


def _describe(position: PositionRecord) -> str:
    """거부 메시지에 넣을 포지션 한 줄 요약."""
    return (
        f"#{position.id} {position.quantity:,}주 @{position.avg_cost:,.0f}원"
        f"({position.entry_date})"
    )


def _quantity_rejection(
    *, quantity: int, position: PositionRecord, candidates: list[PositionRecord],
) -> ManualSellRejection:
    msg = (
        f"매도 수량({quantity:,})이 대상 포지션 보유 수량({position.quantity:,})을 "
        f"초과합니다."
    )
    if len(candidates) > 1:
        listed = " / ".join(_describe(p) for p in candidates)
        msg += (
            f"\n동일 종목 open 포지션 {len(candidates)}건 — {listed}"
            f"\n가장 오래된 {_describe(position)}를 대상으로 선택했습니다. "
            f"포지션을 지정하려면 백오피스 매도 폼을 사용하세요."
        )
    return ManualSellRejection(code="quantity_exceeds", message=msg, candidates=candidates)


async def resolve_manual_sell(
    *,
    session: AsyncSession,
    account_id: str,
    symbol: str | None,
    quantity: int,
    position_id: int | None = None,
) -> ManualSellPlan | ManualSellRejection:
    """수동 매도 대상 포지션을 해석·검증한다.

    Parameters
    ----------
    session: 호출자 스코프의 AsyncSession (어드민은 요청 스코프, 그 외는 임시 세션).
    account_id: 매도를 실행할 계좌.
    symbol: 종목코드. position_id가 없으면 필수이며, 있으면 교차 검증용(불일치 시 거부).
    quantity: 요청 수량(>0).
    position_id: 지정 시 해당 포지션을 대상으로 강제(어드민·REST 명시 지정).

    Returns
    -------
    ManualSellPlan 또는 ManualSellRejection. 예외는 던지지 않는다.
    """
    if quantity <= 0:
        return ManualSellRejection(
            code="invalid_quantity", message="매도 수량은 1 이상이어야 합니다.",
        )

    normalized = symbol.strip().upper() if symbol else ""

    # ── 명시 지정 경로 ────────────────────────────────────────────────
    if position_id is not None:
        position = await session.get(PositionRecord, position_id)
        if position is None:
            return ManualSellRejection(
                code="position_not_found",
                message=f"포지션 #{position_id}을(를) 찾을 수 없습니다.",
            )
        if position.status != "open":
            return ManualSellRejection(
                code="position_not_open",
                message=f"포지션 #{position_id}은(는) open 상태가 아닙니다"
                        f"(현재: {position.status}).",
            )
        if position.account_id != account_id:
            return ManualSellRejection(
                code="account_mismatch",
                message=f"포지션 #{position_id}은(는) 이 계좌({account_id})의 포지션이 아닙니다.",
            )
        # REST는 symbol·position_id를 모두 명시하므로 불일치는 조작 오류로 본다(덮어쓰지 않음).
        if normalized and position.symbol != normalized:
            return ManualSellRejection(
                code="symbol_mismatch",
                message=f"포지션 #{position_id}의 종목({position.symbol})이 "
                        f"요청 종목({normalized})과 다릅니다.",
            )
        if quantity > position.quantity:
            return _quantity_rejection(
                quantity=quantity, position=position, candidates=[position],
            )
        return ManualSellPlan(
            position=position,
            symbol=position.symbol,
            quantity=quantity,
            candidates=[position],
        )

    # ── 자동 매칭 경로 ────────────────────────────────────────────────
    if not normalized:
        return ManualSellRejection(
            code="missing_symbol", message="종목코드 또는 포지션 ID가 필요합니다.",
        )

    # entry_date는 Date라 당일 중복(F-27)은 동률 → id 타이브레이크로 결정론을 보장한다.
    rows = list(
        (
            await session.execute(
                select(PositionRecord)
                .where(
                    PositionRecord.symbol == normalized,
                    PositionRecord.status == "open",
                    PositionRecord.account_id == account_id,
                )
                .order_by(PositionRecord.entry_date.asc(), PositionRecord.id.asc())
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return ManualSellRejection(
            code="no_open_position",
            message=(
                f"{normalized} 계좌 내 open 포지션이 없어 수동 매도를 실행할 수 없습니다. "
                f"브로커 보유분이 맞다면 {MANUAL_SYNC_HINT}"
            ),
        )

    position = rows[0]
    if len(rows) > 1:
        logger.warning(
            "manual_sell.multiple_open_positions",
            symbol=normalized,
            account_id=account_id,
            count=len(rows),
            selected_position_id=position.id,
            position_ids=[p.id for p in rows],
        )

    if quantity > position.quantity:
        return _quantity_rejection(quantity=quantity, position=position, candidates=rows)

    return ManualSellPlan(
        position=position, symbol=position.symbol, quantity=quantity, candidates=rows,
    )


def build_manual_exit_signal(
    *, position: PositionRecord, price: Decimal, reasoning: str,
) -> ExitSignal:
    """수동 청산용 ExitSignal 구성 (reason=MANUAL → execute_exit는 LIMIT 기본)."""
    avg_cost = position.avg_cost or price
    pnl_pct = (
        (price - avg_cost) / avg_cost * Decimal("100") if avg_cost > 0 else Decimal("0")
    )
    return ExitSignal(
        symbol=position.symbol,
        reason=ExitReason.MANUAL,
        urgency="immediate",
        current_price=price,
        unrealized_pnl_pct=pnl_pct,
        recommended_action=DecisionAction.SELL,
        reasoning=reasoning,
    )
