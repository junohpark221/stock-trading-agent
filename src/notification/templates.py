"""HTML message templates for Telegram notifications.

All methods are stateless static functions producing aiogram-compatible HTML.
"""

from __future__ import annotations

import html
from decimal import Decimal
from uuid import UUID

from src.core.enums import ApprovalStatus, ExitReason, MonitoringAlertType, OrderSide
from src.core.models import DailyReportData, PerformanceMetrics, WeeklyReportData

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

_REPORT_TYPE_KR: dict[str, str] = {
    "daily": "일간",
    "weekly": "주간",
    "monthly": "월간",
    "llm_cost": "LLM 비용",
}

_ALERT_TYPE_ICON: dict[str, str] = {
    MonitoringAlertType.STOP_LOSS_PROXIMITY: "🔻",
    MonitoringAlertType.SECTOR_CONCENTRATION: "📊",
    MonitoringAlertType.LLM_BUDGET: "💳",
    MonitoringAlertType.PORTFOLIO_DRAWDOWN: "📉",
}

_ALERT_TYPE_KR: dict[str, str] = {
    MonitoringAlertType.STOP_LOSS_PROXIMITY: "손절 근접 경고",
    MonitoringAlertType.SECTOR_CONCENTRATION: "섹터 비중 경고",
    MonitoringAlertType.LLM_BUDGET: "LLM 예산 경고",
    MonitoringAlertType.PORTFOLIO_DRAWDOWN: "포트폴리오 낙폭 경고",
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

    @staticmethod
    def _fmt_usd(value: Decimal) -> str:
        """Format USD amount with 2 decimal places."""
        return f"${value:,.2f}"

    @staticmethod
    def _progress_bar(ratio: Decimal, width: int = 10) -> str:
        """Render a text progress bar, e.g. '████░░░░░░ 40%'."""
        clamped = max(Decimal(0), min(Decimal(1), ratio))
        filled = int(clamped * width)
        empty = width - filled
        pct = int(clamped * 100)
        return f"{'█' * filled}{'░' * empty} {pct}%"

    @staticmethod
    def _pnl_sign(value: Decimal) -> str:
        """Return '+' for positive values, '' otherwise."""
        return "+" if value > 0 else ""

    @staticmethod
    def _account_header(account_label: str) -> list[str]:
        """Return account label header lines, or empty list if no label."""
        if not account_label:
            return []
        return [f"<b>[{html.escape(account_label, quote=False)}]</b>", ""]

    # -- public templates ----------------------------------------------------

    @staticmethod
    def approval_request(
        *,
        account_label: str = "",
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

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
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
        ])
        return "\n".join(lines)

    @staticmethod
    def execution_notification(
        *,
        account_label: str = "",
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

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
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
        ])
        return "\n".join(lines)

    @staticmethod
    def submission_notification(
        *,
        account_label: str = "",
        symbol: str,
        name: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        approval_status: ApprovalStatus,
        broker_order_id: str = "",
    ) -> str:
        """주문 접수(미체결) 통보 — KIS 접수 직후. 체결 확정 시 execution_notification."""
        esc = MessageTemplates._escape
        fmt = MessageTemplates

        emoji = _SIDE_EMOJI.get(side, "⚪")
        side_kr = _SIDE_KR.get(side, str(side))
        approval_kr = _APPROVAL_STATUS_KR.get(approval_status, str(approval_status))

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            "📥 <b>주문 접수 · 체결 대기</b>",
            "",
            f"{emoji} <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            f"• 구분: {side_kr}",
            f"• 수량: {quantity:,}주",
            f"• 주문가: {fmt._fmt_price(price)}원",
            f"• 승인: {approval_kr}",
        ])
        if broker_order_id:
            lines.append(f"• 주문번호: {esc(broker_order_id)}")
        lines.append("")
        lines.append("<i>체결 완료 시 별도 통보됩니다.</i>")
        return "\n".join(lines)

    @staticmethod
    def rejection_notification(
        *,
        account_label: str = "",
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

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            f"{icon} <b>주문 {action} — {esc(label)}</b>",
            "",
            f"{emoji} <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            f"• 구분: {side_kr}",
            f"• 사유: {esc(reason)}",
        ])
        return "\n".join(lines)

    @staticmethod
    def exit_signal_notification(
        *,
        account_label: str = "",
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

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            f"🔔 <b>청산 시그널</b> ({urgency_kr})",
            "",
            f"🔴 <b>{esc(name)}</b> ({esc(symbol)})",
            _DIVIDER,
            "",
            f"• 사유: {reason_kr}",
            f"• 현재가: {fmt._fmt_price(current_price)}원",
            f"• 미실현 수익률: {fmt._fmt_pct(unrealized_pnl_pct)}",
            f"• 자동 실행: {auto_kr}",
        ])
        return "\n".join(lines)

    @staticmethod
    def quantity_modify_prompt(original_quantity: int) -> str:
        """수량 수정 프롬프트."""
        return f"📝 현재 수량: {original_quantity:,}주\n새 수량을 숫자로 입력해주세요."

    # -- report templates (Phase 6 Step 5) -----------------------------------

    @staticmethod
    def daily_report(data: DailyReportData, *, account_label: str = "") -> str:
        """일간 리포트 메시지 — 6개 섹션."""
        fmt = MessageTemplates

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            f"📋 <b>일간 리포트</b> — {data.report_date}",
            _DIVIDER,
            "",
            # 섹션 2: 오늘의 손익
            "<b>💰 오늘의 손익</b>",
        ])

        # PnL lines (local vars to stay within line length)
        r_sign = fmt._pnl_sign(data.realized_pnl)
        r_krw = fmt._fmt_krw(data.realized_pnl)
        r_pct = fmt._pnl_sign(data.realized_pnl_pct) + fmt._fmt_pct(data.realized_pnl_pct)
        u_sign = fmt._pnl_sign(data.unrealized_pnl)
        u_krw = fmt._fmt_krw(data.unrealized_pnl)
        u_pct = fmt._pnl_sign(data.unrealized_pnl_pct) + fmt._fmt_pct(data.unrealized_pnl_pct)
        c_ret = fmt._pnl_sign(data.cumulative_return_pct) + fmt._fmt_pct(data.cumulative_return_pct)

        lines.extend([
            f"• 실현 손익: {r_sign}{r_krw} ({r_pct})",
            f"• 미실현 손익: {u_sign}{u_krw} ({u_pct})",
            f"• 누적 수익률: {c_ret}",
        ])

        # 섹션 3: 오늘의 거래
        lines.append("")
        if data.trades_today:
            lines.append(f"<b>📈 오늘의 거래</b> ({len(data.trades_today)}건)")
            for t in data.trades_today:
                side = t.get("side", "")
                emoji = _SIDE_EMOJI.get(side, "⚪")
                side_kr = _SIDE_KR.get(side, str(side))
                symbol = t.get("symbol", "")
                qty = t.get("quantity", 0)
                price = t.get("price", "0")
                lines.append(f"{emoji} {symbol} {side_kr} {qty:,}주 @ {int(Decimal(str(price))):,}")
        else:
            lines.append("<b>📈 오늘의 거래</b>")
            lines.append("• 오늘 거래 없음")

        # 섹션 4: 포트폴리오 현황
        lines.extend([
            "",
            "<b>📋 포트폴리오 현황</b>",
            f"• 총 자산: {fmt._fmt_krw(data.total_value)}",
            f"• 현금: {fmt._fmt_krw(data.cash)} ({fmt._fmt_pct(data.cash_pct)})",
            f"• 보유 종목: {data.positions_count}개",
        ])
        if data.sector_allocations:
            sectors = ", ".join(
                f"{s} {fmt._fmt_pct(w)}" for s, w in data.sector_allocations.items()
            )
            lines.append(f"• 섹터: {sectors}")

        # 섹션 5: LLM 비용
        if data.llm_budget_usd > 0:
            budget_ratio = (
                data.llm_cost_monthly_usd / data.llm_budget_usd
                if data.llm_budget_usd > 0
                else Decimal(0)
            )
            lines.extend([
                "",
                "<b>🤖 LLM 비용</b>",
                f"• 오늘: {fmt._fmt_usd(data.llm_cost_today_usd)}",
                f"• 이번 달: {fmt._fmt_usd(data.llm_cost_monthly_usd)}"
                f" / {fmt._fmt_usd(data.llm_budget_usd)}",
                f"  {fmt._progress_bar(budget_ratio)}",
            ])

        # 섹션 6: 경고
        if data.warnings:
            lines.extend(["", "⚠️ <b>주의사항</b>"])
            for w in data.warnings:
                lines.append(f"• {fmt._escape(w)}")

        return "\n".join(lines)

    @staticmethod
    def weekly_report(data: WeeklyReportData, *, account_label: str = "") -> str:
        """주간 리포트 메시지 — 4개 섹션."""
        fmt = MessageTemplates
        perf = data.performance

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            f"📋 <b>주간 리포트</b> — {data.week_start} ~ {data.week_end}",
            _DIVIDER,
            "",
            # 섹션 2: 주간 성과
            "<b>📊 주간 성과</b>",
        ])

        ret_str = fmt._pnl_sign(perf.total_return_pct) + fmt._fmt_pct(perf.total_return_pct)
        wr_str = f"{perf.winning_trades}승 {perf.losing_trades}패"
        lines.extend([
            f"• 주간 수익률: {ret_str}",
            f"• MDD: {fmt._fmt_pct(perf.max_drawdown_pct)}",
            f"• 승률: {fmt._fmt_pct(perf.win_rate_pct)} ({wr_str})",
            f"• 총 거래: {perf.total_trades}건",
        ])
        if perf.sharpe_ratio is not None:
            lines.append(f"• Sharpe Ratio: {perf.sharpe_ratio:.2f}")
        if perf.profit_factor is not None:
            lines.append(f"• Profit Factor: {perf.profit_factor:.2f}")

        # 섹션 3: 최고/최악 거래
        if data.best_trade is not None or data.worst_trade is not None:
            lines.extend(["", "<b>🏆 최고 / 💀 최악 거래</b>"])
            if data.best_trade is not None:
                bt = data.best_trade
                pnl_pct = bt.get("pnl_pct", Decimal(0))
                pnl = bt.get("pnl", Decimal(0))
                lines.append(
                    f"🏆 {bt.get('symbol', '')} "
                    f"{fmt._pnl_sign(pnl_pct)}{fmt._fmt_pct(pnl_pct)} "
                    f"({fmt._pnl_sign(pnl)}{fmt._fmt_krw(pnl)})"
                )
            if data.worst_trade is not None:
                wt = data.worst_trade
                pnl_pct = wt.get("pnl_pct", Decimal(0))
                pnl = wt.get("pnl", Decimal(0))
                lines.append(
                    f"💀 {wt.get('symbol', '')} "
                    f"{fmt._pnl_sign(pnl_pct)}{fmt._fmt_pct(pnl_pct)} "
                    f"({fmt._pnl_sign(pnl)}{fmt._fmt_krw(pnl)})"
                )

        # 섹션 4: 전략별 비교
        if data.strategy_comparison:
            lines.extend(["", "<b>📋 전략별 비교</b>"])
            for strategy, stats in data.strategy_comparison.items():
                ret = stats.get("avg_pnl_pct", Decimal(0))
                count = stats.get("trade_count", 0)
                wr = stats.get("win_rate_pct", Decimal(0))
                lines.append(
                    f"• {strategy}: 수익률 {fmt._pnl_sign(ret)}{fmt._fmt_pct(ret)}, "
                    f"승률 {fmt._fmt_pct(wr)}, {count}건"
                )

        return "\n".join(lines)

    @staticmethod
    def monthly_report(
        *,
        account_label: str = "",
        metrics: PerformanceMetrics,
        summary: DailyReportData,
    ) -> str:
        """월간 리포트 메시지 — 5개 섹션."""
        fmt = MessageTemplates

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            f"📋 <b>월간 리포트</b> — {metrics.period_start} ~ {metrics.period_end}",
            _DIVIDER,
            "",
            # 섹션 2: 월간 성과 상세
            "<b>📊 월간 성과</b>",
            f"• 총 수익률: "
            f"{fmt._pnl_sign(metrics.total_return_pct)}"
            f"{fmt._fmt_pct(metrics.total_return_pct)}",
            f"• MDD: {fmt._fmt_pct(metrics.max_drawdown_pct)}",
        ])
        if metrics.annualized_return_pct is not None:
            lines.append(
                f"• 연환산 수익률: {fmt._pnl_sign(metrics.annualized_return_pct)}"
                f"{fmt._fmt_pct(metrics.annualized_return_pct)}"
            )
        if metrics.sharpe_ratio is not None:
            lines.append(f"• Sharpe Ratio: {metrics.sharpe_ratio:.2f}")
        if metrics.sortino_ratio is not None:
            lines.append(f"• Sortino Ratio: {metrics.sortino_ratio:.2f}")
        if metrics.profit_factor is not None:
            lines.append(f"• Profit Factor: {metrics.profit_factor:.2f}")

        # 섹션 3: 거래 통계
        lines.extend([
            "",
            "<b>📈 거래 통계</b>",
            f"• 총 거래: {metrics.total_trades}건",
            f"• 승률: {fmt._fmt_pct(metrics.win_rate_pct)}"
            f" ({metrics.winning_trades}승 {metrics.losing_trades}패)",
            f"• 평균 수익: "
            f"{fmt._pnl_sign(metrics.avg_win_pct)}"
            f"{fmt._fmt_pct(metrics.avg_win_pct)}",
            f"• 평균 손실: "
            f"{fmt._pnl_sign(metrics.avg_loss_pct)}"
            f"{fmt._fmt_pct(metrics.avg_loss_pct)}",
        ])

        # 섹션 4: 포트폴리오 현황
        lines.extend([
            "",
            "<b>📋 포트폴리오 현황</b>",
            f"• 총 자산: {fmt._fmt_krw(summary.total_value)}",
            f"• 현금: {fmt._fmt_krw(summary.cash)} ({fmt._fmt_pct(summary.cash_pct)})",
            f"• 보유 종목: {summary.positions_count}개",
        ])

        # 섹션 5: LLM 비용
        if summary.llm_budget_usd > 0:
            lines.extend([
                "",
                "<b>🤖 LLM 비용</b>",
                f"• 이번 달: "
                f"{fmt._fmt_usd(summary.llm_cost_monthly_usd)}"
                f" / {fmt._fmt_usd(summary.llm_budget_usd)}",
            ])

        return "\n".join(lines)

    @staticmethod
    def llm_cost_report(data: dict) -> str:
        """LLM 비용 리포트 메시지 — 4개 섹션."""
        fmt = MessageTemplates

        lines = [
            "💳 <b>LLM 비용 리포트</b>",
            _DIVIDER,
        ]

        # 섹션 2: 예산 현황
        budget_status = data.get("budget_status")
        if budget_status is not None:
            cost = getattr(budget_status, "current_month_cost_usd", Decimal(0))
            budget = getattr(budget_status, "budget_usd", Decimal(0))
            lines.append("")
            lines.append("<b>💰 예산 현황</b>")
            if budget > 0:
                ratio = cost / budget
                lines.append(f"• {fmt._fmt_usd(cost)} / {fmt._fmt_usd(budget)}")
                lines.append(f"  {fmt._progress_bar(ratio)}")
            else:
                lines.append("• 예산 미설정")

        # 섹션 3: 프로바이더별 비용
        monthly = data.get("monthly_summary", {})
        by_provider = monthly.get("by_provider", {})
        lines.extend(["", "<b>📊 프로바이더별 비용</b>"])
        if by_provider:
            sorted_providers = sorted(
                by_provider.items(),
                key=lambda x: Decimal(str(x[1].get("cost_usd", "0"))),
                reverse=True,
            )
            for provider, info in sorted_providers:
                cost = Decimal(str(info.get("cost_usd", "0")))
                calls = info.get("call_count", 0)
                lines.append(f"• {provider}: {fmt._fmt_usd(cost)} ({calls}건)")
        else:
            lines.append("• 데이터 없음")

        # 섹션 4: 7일 추이
        recent = data.get("recent_usage", [])
        if recent:
            lines.extend(["", "<b>📈 최근 7일</b>"])
            # 날짜별 합산 (entry는 dict 또는 object 가능)
            daily_totals: dict[str, Decimal] = {}
            for entry in recent:
                is_dict = isinstance(entry, dict)
                d = entry.get("date") if is_dict else getattr(entry, "date", None)
                if d is None:
                    continue
                day_key = d.strftime("%m-%d") if hasattr(d, "strftime") else str(d)[-5:]
                raw_cost = (
                    entry.get("cost_usd", "0") if is_dict
                    else getattr(entry, "cost_usd", "0")
                )
                cost = Decimal(str(raw_cost))
                daily_totals[day_key] = daily_totals.get(day_key, Decimal(0)) + cost
            for day_key, cost in daily_totals.items():
                lines.append(f"• {day_key}: {fmt._fmt_usd(cost)}")

        return "\n".join(lines)

    @staticmethod
    def monitoring_alert(
        *,
        account_label: str = "",
        alert_type: MonitoringAlertType,
        symbol: str | None,
        message: str,
        current_value: Decimal,
        threshold_value: Decimal,
    ) -> str:
        """모니터링 경고 메시지."""
        fmt = MessageTemplates

        icon = _ALERT_TYPE_ICON.get(alert_type, "⚠️")
        label = _ALERT_TYPE_KR.get(alert_type, str(alert_type))

        lines = MessageTemplates._account_header(account_label)
        lines.extend([
            f"{icon} <b>{label}</b>",
            _DIVIDER,
        ])
        if symbol is not None:
            lines.append(f"• 종목: {fmt._escape(symbol)}")
        lines.extend([
            f"• 현재: {fmt._fmt_pct(current_value)}",
            f"• 기준: {fmt._fmt_pct(threshold_value)}",
            f"• 상세: {fmt._escape(message)}",
        ])

        return "\n".join(lines)

    # -- telegram command templates ------------------------------------------

    @staticmethod
    def portfolio_summary_command(
        snapshot: object,
        positions_count: int,
        account_label: str = "",
    ) -> str:
        """텔레그램 /portfolio 커맨드 응답 포맷.

        Args:
            snapshot: PortfolioSnapshot ORM 객체 (total_value, cash, invested,
                      unrealized_pnl, realized_pnl_daily 필드 사용).
            positions_count: 보유 종목 수.
            account_label: 계좌 표시명.
        """
        fmt = MessageTemplates

        lines = fmt._account_header(account_label)
        lines.append(_DIVIDER)

        unrealized_pct = (
            snapshot.unrealized_pnl / snapshot.invested * 100
            if snapshot.invested
            else Decimal(0)
        )

        lines.extend([
            f"💰 총 자산: {fmt._fmt_krw(snapshot.total_value)}",
            f"💵 현금: {fmt._fmt_krw(snapshot.cash)}",
            f"📈 투자금: {fmt._fmt_krw(snapshot.invested)}",
            f"📊 미실현 손익: {fmt._pnl_sign(snapshot.unrealized_pnl)}"
            f"{fmt._fmt_krw(snapshot.unrealized_pnl)}"
            f" ({fmt._pnl_sign(unrealized_pct)}{fmt._fmt_pct(unrealized_pct)})",
            f"📉 일일 손익: {fmt._pnl_sign(snapshot.realized_pnl_daily)}"
            f"{fmt._fmt_krw(snapshot.realized_pnl_daily)}",
            f"🔢 보유 종목: {positions_count}개",
        ])

        return "\n".join(lines)

    @staticmethod
    def positions_list_command(
        positions: list,
        account_label: str = "",
    ) -> str:
        """텔레그램 /positions 커맨드 응답 포맷.

        Args:
            positions: PositionRecord ORM 객체 리스트
                       (symbol, quantity, avg_cost, stop_loss_price,
                        take_profit_price 필드 사용).
            account_label: 계좌 표시명.
        """
        fmt = MessageTemplates

        lines = fmt._account_header(account_label)
        lines.append(_DIVIDER)

        for pos in positions:
            lines.extend([
                f"📌 <b>{fmt._escape(pos.symbol)}</b>",
                f"  수량: {pos.quantity}주 | 평균: {fmt._fmt_price(pos.avg_cost)}원",
                f"  손절: {fmt._fmt_optional_price(pos.stop_loss_price)}"
                f" | 익절: {fmt._fmt_optional_price(pos.take_profit_price)}",
                "",
            ])

        # 마지막 빈 줄 제거
        if lines and lines[-1] == "":
            lines.pop()

        return "\n".join(lines)

    @staticmethod
    def trade_history_command(
        positions: list,
        count: int,
        account_label: str = "",
    ) -> str:
        """텔레그램 /history 커맨드 응답 포맷.

        Args:
            positions: 청산된 PositionRecord ORM 객체 리스트 (최신순).
            count: 표시 건수 (헤더용).
            account_label: 계좌 표시명.
        """
        fmt = MessageTemplates

        lines = fmt._account_header(account_label)
        lines.append(f"최근 {count}건")
        lines.append(_DIVIDER)

        for pos in positions:
            is_profit = pos.realized_pnl is not None and pos.realized_pnl > 0
            icon = "✅" if is_profit else "❌"
            exit_dt = pos.exit_date.strftime("%m/%d") if pos.exit_date else "?"
            reason = pos.exit_reason or ""

            pnl = pos.realized_pnl or Decimal(0)
            cost_basis = pos.avg_cost * pos.quantity
            pnl_pct = pnl / cost_basis * 100 if cost_basis else Decimal(0)

            lines.extend([
                f"{icon} {fmt._escape(pos.symbol)} 매도 ({exit_dt})",
                f"  {fmt._pnl_sign(pnl)}{fmt._fmt_krw(pnl)}"
                f" ({fmt._pnl_sign(pnl_pct)}{fmt._fmt_pct(pnl_pct)})"
                + (f" | {reason}" if reason else ""),
                "",
            ])

        if lines and lines[-1] == "":
            lines.pop()

        return "\n".join(lines)

    @staticmethod
    def performance_summary_command(
        metrics: object,
        account_label: str = "",
    ) -> str:
        """텔레그램 /performance 커맨드 응답 포맷.

        Args:
            metrics: PerformanceMetrics 객체.
            account_label: 계좌 표시명.
        """
        fmt = MessageTemplates

        def _val(v: Decimal | None, formatter=fmt._fmt_pct) -> str:
            return formatter(v) if v is not None else "N/A"

        def _ratio(v: Decimal | None) -> str:
            return f"{v:.2f}" if v is not None else "N/A"

        lines = fmt._account_header(account_label)
        lines.append(f"최근 {metrics.period_start} ~ {metrics.period_end}")
        lines.append(_DIVIDER)

        lines.extend([
            f"📊 총 수익률: {fmt._pnl_sign(metrics.total_return_pct)}"
            f"{fmt._fmt_pct(metrics.total_return_pct)}",
            f"📈 연환산 수익률: {_val(metrics.annualized_return_pct)}",
            f"⚡ Sharpe Ratio: {_ratio(metrics.sharpe_ratio)}",
            f"📉 Max Drawdown: {fmt._fmt_pct(metrics.max_drawdown_pct)}",
            f"🎯 승률: {fmt._fmt_pct(metrics.win_rate_pct)}"
            f" ({metrics.winning_trades}승 {metrics.losing_trades}패)",
            f"💰 Profit Factor: {_ratio(metrics.profit_factor)}",
        ])

        return "\n".join(lines)
