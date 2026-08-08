"""보유 컨텍스트 렌더러 (PRJ-04 §8).

orchestrator가 계좌 open 포지션에서 만든 컨텍스트 dict를 Stock/Risk/Trade 프롬프트가
공유하는 마크다운 블록으로 변환한다. LLM이 "이 종목을 이미 들고 있다"는 사실과
추가매수(병합)의 부작용을 알고 판단하게 하는 것이 목적이다.

**3-state를 반드시 구분한다** — `known=False`(조회 못 함)를 "미보유"로 렌더링하면
과거의 상시 "신규 포트폴리오" 오정보를 다른 형태로 재생산하게 된다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_PCT_Q = Decimal("0.01")


def _to_decimal(value: Any) -> Decimal | None:
    """Decimal/int/str → Decimal (float 금지 원칙상 str 경유). 실패 시 None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _krw(value: Any) -> str:
    d = _to_decimal(value)
    return f"{d:,.0f}원" if d is not None else "-"


def _pct(value: Decimal) -> str:
    q = value.quantize(_PCT_Q, rounding=ROUND_HALF_UP)
    return f"{q:+.2f}%" if q != 0 else "0.00%"


def close_price(data: dict[str, Any]) -> Decimal | None:
    """프롬프트 data에서 평가손익 계산용 현재가 추출.

    Stock/Risk/Trade 모두 ``get_current_price`` 도구 결과를 ``current_price``에 담고
    종가 키는 ``close``다(에러 시 error 키만 있어 None).
    """
    price_data = data.get("current_price")
    if not isinstance(price_data, dict):
        return None
    return _to_decimal(price_data.get("close"))


def render_holding_section(
    ctx: dict[str, Any] | None,
    *,
    current_price: Any = None,
) -> str:
    """분석 대상 종목의 보유 현황 블록.

    Parameters
    ----------
    ctx: ``{"known": bool, "position": dict | None}``. None이면 미조회로 취급.
    current_price: 평가손익률 계산용 현재가(Decimal/int/str). 없으면 손익률 생략.
    """
    ctx = ctx or {}
    lines = ["### 이 종목의 보유 현황"]

    if not ctx.get("known"):
        lines.append(
            "보유 현황 미조회 — 포지션 데이터를 확인하지 못했습니다. "
            "보유 여부를 단정하지 말고, 판단의 확신도를 보수적으로 잡으세요.\n"
        )
        return "\n".join(lines)

    pos = ctx.get("position")
    if not pos:
        lines.append("**미보유** — 이 종목은 신규 진입 후보입니다.\n")
        return "\n".join(lines)

    qty = pos.get("quantity")
    avg_cost = _to_decimal(pos.get("avg_cost"))
    lines.append(f"**보유 중** — 수량 {qty:,}주 · 평균단가 {_krw(avg_cost)}")

    price = _to_decimal(current_price)
    if price is not None and avg_cost is not None and avg_cost > 0:
        pnl_pct = (price - avg_cost) / avg_cost * Decimal(100)
        pnl_krw = (price - avg_cost) * Decimal(str(qty or 0))
        state = "평가이익" if pnl_pct > 0 else ("평가손실" if pnl_pct < 0 else "본전")
        lines.append(
            f"- 평가손익: {_pct(pnl_pct)} ({_krw(pnl_krw)}) — {state} 구간"
        )

    holding_days = pos.get("holding_days")
    max_days = pos.get("max_holding_days")
    if holding_days is not None:
        limit = f" / 시간손절 한도 {max_days}일" if max_days else ""
        lines.append(
            f"- 진입일: {pos.get('entry_date')} (보유 {holding_days}일{limit})"
        )

    exit_bits = [f"손절가 {_krw(pos.get('stop_loss_price'))}"]
    if pos.get("take_profit_price"):
        exit_bits.append(f"익절가 {_krw(pos.get('take_profit_price'))}")
    if pos.get("trailing_stop_pct"):
        high = pos.get("highest_price")
        trail = f"트레일링 전환됨(폭 {pos['trailing_stop_pct']}%"
        trail += f", 고점 {_krw(high)})" if high else ")"
        exit_bits.append(trail)
    lines.append("- 청산 기준: " + " · ".join(exit_bits))

    if pos.get("entry_trigger"):
        lines.append(f"- 진입 트리거: {', '.join(pos['entry_trigger'])}")

    thesis = pos.get("entry_thesis")
    if thesis:
        factors = thesis.get("key_factors") or []
        lines.append(
            f"- 진입 가설: action={thesis.get('action')}, "
            f"confidence={thesis.get('confidence')}"
            + (f", 근거={list(factors)[:5]}" if factors else "")
        )

    if pos.get("is_runner"):
        lines.append(
            f"- ⚠️ **부분익절 이력 있음**(실현손익 {_krw(pos.get('realized_pnl'))}) — "
            "추가매수로 병합되면 트레일링 보호(본전 플로어·고점 기록)가 해제되고 "
            "일반 손절 상태로 되돌아갑니다."
        )

    lines.append("")
    lines.append(
        "> 이 종목에 매수하면 **신규 진입이 아니라 기존 포지션에 병합되는 추가매수**입니다. "
        "병합 시 ① 평균단가가 새 체결가와 가중평균으로 바뀌고 ② 진입일·시간손절 시계가 "
        "리셋되며 ③ 손절가·익절가가 새 평균단가 기준으로 재산정됩니다"
        "(트레일링 상태였다면 일반 상태로 되돌아갑니다).\n"
    )
    return "\n".join(lines)


def render_portfolio_section(ctx: dict[str, Any] | None) -> str:
    """계좌 전체 보유 요약 블록 (RiskManager 집중도 판단용)."""
    ctx = ctx or {}
    lines = ["### 현재 포트폴리오 (이 계좌의 보유 포지션 전량)"]

    if not ctx.get("known"):
        lines.append(
            "보유 현황 미조회 — 포지션 데이터를 확인하지 못했습니다. "
            "보유 종목이 없다고 단정하지 말고 집중도 판단의 확신도를 낮추세요.\n"
        )
        return "\n".join(lines)

    positions = ctx.get("positions") or []
    if not positions:
        lines.append("보유 포지션 없음 — 기존 보유 종목이 없어 집중도 리스크가 낮습니다.\n")
        return "\n".join(lines)

    total = _to_decimal(ctx.get("total_cost_krw")) or Decimal(0)
    lines.append(f"보유 {ctx.get('count', len(positions))}종목 · 총 매입금액 {_krw(total)}")
    for p in positions:
        cost = _to_decimal(p.get("cost_krw")) or Decimal(0)
        weight = (cost / total * Decimal(100)) if total > 0 else Decimal(0)
        label = p.get("name") or p.get("symbol")
        lines.append(
            f"- {label}({p.get('symbol')}): {p.get('quantity'):,}주 · "
            f"평단 {_krw(p.get('avg_cost'))} · 매입 {_krw(cost)} "
            f"(보유분 대비 {weight.quantize(_PCT_Q, rounding=ROUND_HALF_UP)}%)"
        )
    lines.append("")
    return "\n".join(lines)
