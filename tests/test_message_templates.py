"""Tests for MessageTemplates — Phase 5 Step 4."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from src.core.enums import ApprovalStatus, ExitReason, OrderSide
from src.notification.templates import MessageTemplates


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def approval_kwargs() -> dict:
    """Minimal kwargs for approval_request."""
    return {
        "symbol": "005930",
        "name": "삼성전자",
        "side": OrderSide.BUY,
        "quantity": 10,
        "price": Decimal("72000"),
        "position_value_krw": Decimal("720000"),
        "portfolio_pct": Decimal("3.60"),
        "stop_loss_price": Decimal("68000"),
        "take_profit_price": Decimal("80000"),
        "risk_reward_ratio": Decimal("2.0"),
        "analysis_summary": "기술적 지표 양호, 이동평균선 골든크로스 발생",
        "web_verify_summary": "최근 뉴스에 특이사항 없음",
        "session_id": UUID("12345678-1234-1234-1234-123456789abc"),
    }


@pytest.fixture()
def execution_kwargs() -> dict:
    """Minimal kwargs for execution_notification."""
    return {
        "symbol": "005930",
        "name": "삼성전자",
        "side": OrderSide.BUY,
        "quantity": 10,
        "fill_price": Decimal("72000"),
        "commission": Decimal("360"),
        "approval_status": ApprovalStatus.AUTO_APPROVED,
    }


@pytest.fixture()
def rejection_kwargs() -> dict:
    """Minimal kwargs for rejection_notification."""
    return {
        "symbol": "005930",
        "name": "삼성전자",
        "side": OrderSide.BUY,
        "reason": "포트폴리오 비중 초과",
        "stage": "risk_blocked",
    }


@pytest.fixture()
def exit_kwargs() -> dict:
    """Minimal kwargs for exit_signal_notification."""
    return {
        "symbol": "005930",
        "name": "삼성전자",
        "reason": ExitReason.STOP_LOSS,
        "urgency": "immediate",
        "current_price": Decimal("68000"),
        "unrealized_pnl_pct": Decimal("-5.56"),
        "auto_executed": False,
    }


# ===========================================================================
# Helper method tests
# ===========================================================================


class TestHelpers:
    """Private helper method tests (accessed via class for completeness)."""

    def test_escape_angle_brackets(self):
        assert MessageTemplates._escape("<script>") == "&lt;script&gt;"

    def test_escape_ampersand(self):
        assert MessageTemplates._escape("A&B") == "A&amp;B"

    def test_escape_safe_text(self):
        assert MessageTemplates._escape("삼성전자") == "삼성전자"

    def test_fmt_krw_normal(self):
        assert MessageTemplates._fmt_krw(Decimal("1500000")) == "1,500,000원"

    def test_fmt_krw_zero(self):
        assert MessageTemplates._fmt_krw(Decimal("0")) == "0원"

    def test_fmt_pct_positive(self):
        assert MessageTemplates._fmt_pct(Decimal("3.456")) == "3.46%"

    def test_fmt_pct_negative(self):
        assert MessageTemplates._fmt_pct(Decimal("-5.56")) == "-5.56%"

    def test_fmt_pct_zero(self):
        assert MessageTemplates._fmt_pct(Decimal("0")) == "0.00%"

    def test_fmt_price(self):
        assert MessageTemplates._fmt_price(Decimal("85000")) == "85,000"

    def test_fmt_optional_price_value(self):
        assert MessageTemplates._fmt_optional_price(Decimal("68000")) == "68,000"

    def test_fmt_optional_price_none(self):
        assert MessageTemplates._fmt_optional_price(None) == "미설정"

    def test_fmt_optional_ratio_value(self):
        assert MessageTemplates._fmt_optional_ratio(Decimal("2.0")) == "1:2.0"

    def test_fmt_optional_ratio_none(self):
        assert MessageTemplates._fmt_optional_ratio(None) == "미설정"

    def test_truncate_short(self):
        assert MessageTemplates._truncate("abc", 10) == "abc"

    def test_truncate_exact(self):
        assert MessageTemplates._truncate("abcde", 5) == "abcde"

    def test_truncate_long(self):
        result = MessageTemplates._truncate("a" * 600)
        assert len(result) == 503  # 500 + "..."
        assert result.endswith("...")


# ===========================================================================
# approval_request tests
# ===========================================================================


class TestApprovalRequest:
    def test_contains_header(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "매매 승인 요청" in msg

    def test_contains_bold_header(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "<b>매매 승인 요청</b>" in msg

    def test_contains_symbol(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "005930" in msg

    def test_contains_name(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "삼성전자" in msg

    def test_buy_side(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "매수" in msg

    def test_sell_side(self, approval_kwargs):
        approval_kwargs["side"] = OrderSide.SELL
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "매도" in msg

    def test_quantity_formatted(self, approval_kwargs):
        approval_kwargs["quantity"] = 1500
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "1,500주" in msg

    def test_price_formatted(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "72,000원" in msg

    def test_position_value_formatted(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "720,000원" in msg

    def test_portfolio_pct(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "3.60%" in msg

    def test_stop_loss_present(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "68,000" in msg

    def test_stop_loss_none(self, approval_kwargs):
        approval_kwargs["stop_loss_price"] = None
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "손절가: 미설정" in msg

    def test_take_profit_present(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "80,000" in msg

    def test_take_profit_none(self, approval_kwargs):
        approval_kwargs["take_profit_price"] = None
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "익절가: 미설정" in msg

    def test_risk_reward_present(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "1:2.0" in msg

    def test_risk_reward_none(self, approval_kwargs):
        approval_kwargs["risk_reward_ratio"] = None
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "R:R 비율: 미설정" in msg

    def test_analysis_summary(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "골든크로스" in msg

    def test_web_verify_summary(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "특이사항 없음" in msg

    def test_session_id_displayed(self, approval_kwargs):
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "12345678" in msg

    def test_session_id_none(self, approval_kwargs):
        approval_kwargs["session_id"] = None
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "N/A" in msg

    def test_analysis_summary_escaped(self, approval_kwargs):
        approval_kwargs["analysis_summary"] = "<b>XSS</b> & test"
        msg = MessageTemplates.approval_request(**approval_kwargs)
        assert "&lt;b&gt;XSS&lt;/b&gt;" in msg
        assert "&amp; test" in msg

    def test_long_summary_truncated(self, approval_kwargs):
        approval_kwargs["analysis_summary"] = "A" * 600
        msg = MessageTemplates.approval_request(**approval_kwargs)
        # 500 A's + "..." = 503 chars for that line
        assert "A" * 500 + "..." in msg
        assert "A" * 501 not in msg


# ===========================================================================
# execution_notification tests
# ===========================================================================


class TestExecutionNotification:
    def test_contains_header(self, execution_kwargs):
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "주문 체결 완료" in msg

    def test_bold_header(self, execution_kwargs):
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "<b>주문 체결 완료</b>" in msg

    def test_buy_side(self, execution_kwargs):
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "매수" in msg

    def test_sell_side(self, execution_kwargs):
        execution_kwargs["side"] = OrderSide.SELL
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "매도" in msg

    def test_fill_price_formatted(self, execution_kwargs):
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "72,000원" in msg

    def test_commission_formatted(self, execution_kwargs):
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "360원" in msg

    def test_zero_commission(self, execution_kwargs):
        execution_kwargs["commission"] = Decimal("0")
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "0원" in msg

    def test_approval_auto(self, execution_kwargs):
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "자동 승인" in msg

    def test_approval_manual(self, execution_kwargs):
        execution_kwargs["approval_status"] = ApprovalStatus.APPROVED
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "수동 승인" in msg

    def test_approval_timeout(self, execution_kwargs):
        execution_kwargs["approval_status"] = ApprovalStatus.TIMEOUT
        msg = MessageTemplates.execution_notification(**execution_kwargs)
        assert "시간 초과" in msg


# ===========================================================================
# rejection_notification tests
# ===========================================================================


class TestRejectionNotification:
    def test_web_verify_icon(self, rejection_kwargs):
        rejection_kwargs["stage"] = "web_verify"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "🚫" in msg

    def test_approval_rejected_icon(self, rejection_kwargs):
        rejection_kwargs["stage"] = "approval_rejected"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "❌" in msg

    def test_approval_timeout_icon(self, rejection_kwargs):
        rejection_kwargs["stage"] = "approval_timeout"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "⏰" in msg

    def test_risk_blocked_icon(self, rejection_kwargs):
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "⚠️" in msg

    def test_unknown_stage_fallback(self, rejection_kwargs):
        rejection_kwargs["stage"] = "unknown_stage"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "❓" in msg
        assert "unknown_stage" in msg

    def test_block_action_word(self, rejection_kwargs):
        """web_verify and risk_blocked use '차단'."""
        rejection_kwargs["stage"] = "web_verify"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "차단" in msg

    def test_reject_action_word(self, rejection_kwargs):
        """approval_rejected uses '거부'."""
        rejection_kwargs["stage"] = "approval_rejected"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "거부" in msg

    def test_reason_escaped(self, rejection_kwargs):
        rejection_kwargs["reason"] = "<script>alert('xss')</script>"
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "&lt;script&gt;" in msg

    def test_stage_label_korean(self, rejection_kwargs):
        msg = MessageTemplates.rejection_notification(**rejection_kwargs)
        assert "리스크 차단" in msg


# ===========================================================================
# exit_signal_notification tests
# ===========================================================================


class TestExitSignalNotification:
    def test_contains_header(self, exit_kwargs):
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "청산 시그널" in msg

    def test_stop_loss_reason(self, exit_kwargs):
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "손절" in msg

    def test_take_profit_reason(self, exit_kwargs):
        exit_kwargs["reason"] = ExitReason.TAKE_PROFIT
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "익절" in msg

    @pytest.mark.parametrize("reason", list(ExitReason))
    def test_all_exit_reasons(self, exit_kwargs, reason):
        exit_kwargs["reason"] = reason
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "청산 시그널" in msg

    def test_urgency_immediate(self, exit_kwargs):
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "즉시" in msg

    def test_urgency_end_of_day(self, exit_kwargs):
        exit_kwargs["urgency"] = "end_of_day"
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "당일 장 마감 전" in msg

    def test_urgency_next_session(self, exit_kwargs):
        exit_kwargs["urgency"] = "next_session"
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "다음 거래일" in msg

    def test_unknown_urgency_fallback(self, exit_kwargs):
        exit_kwargs["urgency"] = "custom_urgency"
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "custom_urgency" in msg

    def test_auto_executed_true(self, exit_kwargs):
        exit_kwargs["auto_executed"] = True
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "자동 실행: 예" in msg

    def test_auto_executed_false(self, exit_kwargs):
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "자동 실행: 아니오" in msg

    def test_negative_pnl(self, exit_kwargs):
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "-5.56%" in msg

    def test_positive_pnl(self, exit_kwargs):
        exit_kwargs["unrealized_pnl_pct"] = Decimal("12.34")
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "12.34%" in msg

    def test_current_price_formatted(self, exit_kwargs):
        msg = MessageTemplates.exit_signal_notification(**exit_kwargs)
        assert "68,000원" in msg


# ===========================================================================
# quantity_modify_prompt tests
# ===========================================================================


class TestQuantityModifyPrompt:
    def test_contains_quantity(self):
        msg = MessageTemplates.quantity_modify_prompt(100)
        assert "100주" in msg

    def test_formatted_quantity(self):
        msg = MessageTemplates.quantity_modify_prompt(1500)
        assert "1,500주" in msg

    def test_instruction_text(self):
        msg = MessageTemplates.quantity_modify_prompt(10)
        assert "새 수량을 숫자로 입력해주세요" in msg
