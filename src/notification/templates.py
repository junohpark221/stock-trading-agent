"""HTML message templates for Telegram notifications.

All methods are stateless static functions producing aiogram-compatible HTML.
"""

from __future__ import annotations

import html
from decimal import Decimal
from uuid import UUID

from src.core.enums import ApprovalStatus, ExitReason, OrderSide

# ---------------------------------------------------------------------------
# Module-level constant mappings
# ---------------------------------------------------------------------------

_SIDE_KR: dict[str, str] = {OrderSide.BUY: "매수", OrderSide.SELL: "매도"}
_SIDE_EMOJI: dict[str, str] = {OrderSide.BUY: "🔵", OrderSide.SELL: "🔴"}

_STAGE_ICON: dict[str, str] = {
    "web_verify": "🚫",
    "approval_rejected": "❌",
    "approval_timeout": "⏰",
    "risk_blocked": "⚠️",
}

_STAGE_LABEL: dict[str, str] = {
    "web_verify": "Web 검증 차단",
    "approval_rejected": "사용자 거부",
    "approval_timeout": "승인 시간 초과",
    "risk_blocked": "리스크 차단",
}

# "차단" stages vs "거부" stages
_BLOCK_STAGES: set[str] = {"web_verify", "risk_blocked"}

_EXIT_REASON_KR: dict[str, str] = {
    ExitReason.STOP_LOSS: "손절",
    ExitReason.TAKE_PROFIT: "익절",
    ExitReason.TRAILING_STOP: "트레일링 스탑",
    ExitReason.TIME_BASED: "보유기간 초과",
    ExitReason.FUNDAMENTAL: "펀더멘털 변화",
    ExitReason.LLM_SIGNAL: "AI 시그널",
    ExitReason.DRAWDOWN: "낙폭 제한",
    ExitReason.MANUAL: "수동 청산",
}

_URGENCY_KR: dict[str, str] = {
    "immediate": "즉시",
    "end_of_day": "당일 장 마감 전",
    "next_session": "다음 거래일",
}

_APPROVAL_STATUS_KR: dict[str, str] = {
    ApprovalStatus.AUTO_APPROVED: "자동 승인",
    ApprovalStatus.APPROVED: "수동 승인",
    ApprovalStatus.REJECTED: "거부",
    ApprovalStatus.TIMEOUT: "시간 초과",
}

_DIVIDER = "━━━━━━━━━━━━━━━━"


# ---------------------------------------------------------------------------
# MessageTemplates
# ---------------------------------------------------------------------------


class MessageTemplates:
    """Stateless HTML message generators for Telegram notifications."""

    # -- private helpers -----------------------------------------------------

    @staticmethod
    def _escape(text: str) -> str:
        """Escape HTML-special characters for Telegram HTML parse mode."""
        return html.escape(text, quote=False)

    @staticmethod
    def _fmt_krw(value: Decimal) -> str:
        """Format KRW amount with comma separator and 원 suffix."""
        return f"{int(value):,}원"

    @staticmethod
    def _fmt_pct(value: Decimal) -> str:
        """Format percentage with 2 decimal places."""
        return f"{value:.2f}%"

    @staticmethod
    def _fmt_price(value: Decimal) -> str:
        """Format price with comma separator (no suffix)."""
        return f"{int(value):,}"

    @staticmethod
    def _fmt_optional_price(value: Decimal | None) -> str:
        """Format price or return '미설정' if None."""
        if value is None:
            return "미설정"
        return f"{int(value):,}"

    @staticmethod
    def _fmt_optional_ratio(value: Decimal | None) -> str:
        """Format risk-reward ratio or return '미설정' if None."""
        if value is None:
            return "미설정"
        return f"1:{value:.1f}"

    @staticmethod
    def _truncate(text: str, max_len: int = 500) -> str:
        """Truncate text to *max_len* characters, appending '...' if needed."""
        if len(text) <= max_len:
            return text
        return text[:max_len] + "..."

    # -- public templates ----------------------------------------------------

    @staticmethod
    def approval_request(
        *,
        symbol: str,
        name: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        position_value_krw: Decimal,
        portfolio_pct: Decimal,
        stop_loss_price: Decimal | None,
        take_profit_price: Decimal | None,
        risk_reward_ratio: Decimal | None,
        analysis_summary: str,
        web_verify_summary: str,
        session_id: UUID | None,
    ) -> str:
        """승인 요청 메시지 — 종목정보 + 분석 요약 + Web 검증 결과."""
        esc = MessageTemplates._escape
        fmt = MessageTemplates

        emoji = _SIDE_EMOJI.get(side, "⚪")
        side_kr = _SIDE_KR.get(side, str(side))
        session_display = str(session_id)[:8] if session_id else "N/A"

        lines = [
            "📊 <b>매매 승인 요청</b>",
            "",
            f"{emoji} <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            "<b>주문 정보</b>",
            f"• 구분: {side_kr}",
            f"• 수량: {quantity:,}주",
            f"• 가격: {fmt._fmt_price(price)}원",
            f"• 금액: {fmt._fmt_krw(position_value_krw)}",
            f"• 포트폴리오 비중: {fmt._fmt_pct(portfolio_pct)}",
            "",
            "<b>리스크 관리</b>",
            f"• 손절가: {fmt._fmt_optional_price(stop_loss_price)}",
            f"• 익절가: {fmt._fmt_optional_price(take_profit_price)}",
            f"• R:R 비율: {fmt._fmt_optional_ratio(risk_reward_ratio)}",
            "",
            "<b>분석 요약</b>",
            esc(fmt._truncate(analysis_summary)),
            "",
            "<b>Web Search 검증</b>",
            esc(fmt._truncate(web_verify_summary)),
            "",
            f"<i>Session: {session_display}</i>",
        ]
        return "\n".join(lines)

    @staticmethod
    def execution_notification(
        *,
        symbol: str,
        name: str,
        side: OrderSide,
        quantity: int,
        fill_price: Decimal,
        commission: Decimal,
        approval_status: ApprovalStatus,
    ) -> str:
        """체결 통보 메시지."""
        esc = MessageTemplates._escape
        fmt = MessageTemplates

        emoji = _SIDE_EMOJI.get(side, "⚪")
        side_kr = _SIDE_KR.get(side, str(side))
        approval_kr = _APPROVAL_STATUS_KR.get(approval_status, str(approval_status))

        lines = [
            "✅ <b>주문 체결 완료</b>",
            "",
            f"{emoji} <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            f"• 구분: {side_kr}",
            f"• 수량: {quantity:,}주",
            f"• 체결가: {fmt._fmt_price(fill_price)}원",
            f"• 수수료: {fmt._fmt_krw(commission)}",
            f"• 승인: {approval_kr}",
        ]
        return "\n".join(lines)

    @staticmethod
    def rejection_notification(
        *,
        symbol: str,
        name: str,
        side: OrderSide,
        reason: str,
        stage: str,
    ) -> str:
        """주문 차단/거부 알림 — stage별 아이콘 및 라벨 구분."""
        esc = MessageTemplates._escape

        icon = _STAGE_ICON.get(stage, "❓")
        label = _STAGE_LABEL.get(stage, stage)
        action = "차단" if stage in _BLOCK_STAGES else "거부"
        emoji = _SIDE_EMOJI.get(side, "⚪")
        side_kr = _SIDE_KR.get(side, str(side))

        lines = [
            f"{icon} <b>주문 {action} — {esc(label)}</b>",
            "",
            f"{emoji} <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            f"• 구분: {side_kr}",
            f"• 사유: {esc(reason)}",
        ]
        return "\n".join(lines)

    @staticmethod
    def exit_signal_notification(
        *,
        symbol: str,
        name: str,
        reason: ExitReason,
        urgency: str,
        current_price: Decimal,
        unrealized_pnl_pct: Decimal,
        auto_executed: bool,
    ) -> str:
        """청산 시그널 알림."""
        esc = MessageTemplates._escape
        fmt = MessageTemplates

        urgency_kr = _URGENCY_KR.get(urgency, urgency)
        reason_kr = _EXIT_REASON_KR.get(reason, str(reason))
        auto_kr = "예" if auto_executed else "아니오"

        lines = [
            f"🔔 <b>청산 시그널</b> ({urgency_kr})",
            "",
            f"🔴 <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            f"• 사유: {reason_kr}",
            f"• 현재가: {fmt._fmt_price(current_price)}원",
            f"• 미실현 수익률: {fmt._fmt_pct(unrealized_pnl_pct)}",
            f"• 자동 실행: {auto_kr}",
        ]
        return "\n".join(lines)

    @staticmethod
    def quantity_modify_prompt(original_quantity: int) -> str:
        """수량 수정 프롬프트."""
        return f"📝 현재 수량: {original_quantity:,}주\n새 수량을 숫자로 입력해주세요."
