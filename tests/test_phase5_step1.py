"""Phase 5 Step 1: Enum, Pydantic model, Config 기반 테스트.

Tests:
- WebVerifyResult enum 직렬화/역직렬화
- AgentType.WEB_VERIFIER 추가 확인
- WebVerification, ApprovalRequestModel, OrderRecord, ExecutionRecord,
  ExecutionResult 모델 생성/검증
- Settings Phase 5 신규 기본값 검증
"""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.core.enums import (
    AgentType,
    ApprovalStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    WebVerifyResult,
)
from src.core.models import (
    ApprovalRequestModel,
    ExecutionRecord,
    ExecutionResult,
    OrderRecord,
    WebVerification,
)
from tests.conftest import make_settings

NOW = datetime(2026, 3, 24, 10, 0, 0)


# ---------------------------------------------------------------------------
# Enum Tests
# ---------------------------------------------------------------------------


class TestWebVerifyResult:
    """WebVerifyResult enum 테스트."""

    def test_values(self):
        assert WebVerifyResult.SAFE == "safe"
        assert WebVerifyResult.WARNING == "warning"
        assert WebVerifyResult.BLOCKED == "blocked"

    def test_from_string(self):
        assert WebVerifyResult("safe") is WebVerifyResult.SAFE
        assert WebVerifyResult("warning") is WebVerifyResult.WARNING
        assert WebVerifyResult("blocked") is WebVerifyResult.BLOCKED

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            WebVerifyResult("invalid")

    def test_all_members(self):
        assert len(WebVerifyResult) == 3


class TestAgentTypeWebVerifier:
    """AgentType에 WEB_VERIFIER 추가 확인."""

    def test_web_verifier_value(self):
        assert AgentType.WEB_VERIFIER == "web_verifier"

    def test_all_members(self):
        assert len(AgentType) == 7


# ---------------------------------------------------------------------------
# Model Tests
# ---------------------------------------------------------------------------


class TestWebVerification:
    """WebVerification 모델 테스트."""

    def test_create_minimal(self):
        v = WebVerification(
            symbol="005930",
            result=WebVerifyResult.SAFE,
            summary="특이사항 없음",
        )
        assert v.symbol == "005930"
        assert v.result == WebVerifyResult.SAFE
        assert v.issues_found == []
        assert v.news_checked == 0
        assert v.llm_cost_usd == Decimal(0)
        assert v.reasoning == ""

    def test_create_full(self):
        v = WebVerification(
            symbol="005930",
            result=WebVerifyResult.WARNING,
            summary="대규모 블록딜 감지",
            issues_found=["블록딜 1건", "공매도 증가"],
            news_checked=5,
            llm_cost_usd=Decimal("0.03"),
            reasoning="블록딜 규모가 크나 기업 펀더멘털에 영향 없음",
        )
        assert len(v.issues_found) == 2
        assert v.news_checked == 5

    def test_extra_field_rejected(self):
        """extra='forbid' — OpenAI strict mode 호환 검증."""
        with pytest.raises(ValidationError):
            WebVerification(
                symbol="005930",
                result=WebVerifyResult.SAFE,
                summary="OK",
                unknown_field="should fail",
            )

    def test_json_roundtrip(self):
        v = WebVerification(
            symbol="005930",
            result=WebVerifyResult.BLOCKED,
            summary="상장폐지 위험",
        )
        data = v.model_dump(mode="json")
        v2 = WebVerification.model_validate(data)
        assert v2.result == WebVerifyResult.BLOCKED
        assert v2.symbol == v.symbol


class TestApprovalRequestModel:
    """ApprovalRequestModel 모델 테스트."""

    def test_create(self):
        req_id = uuid4()
        m = ApprovalRequestModel(
            request_id=req_id,
            order_id=1,
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            price=Decimal("72000"),
            position_value_krw=Decimal("720000"),
            portfolio_pct=Decimal("7.2"),
            requested_at=NOW,
        )
        assert m.request_id == req_id
        assert m.status == ApprovalStatus.AUTO_APPROVED
        assert m.modified_quantity is None
        assert m.responded_at is None

    def test_json_roundtrip(self):
        m = ApprovalRequestModel(
            request_id=uuid4(),
            order_id=1,
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            price=Decimal("72000"),
            position_value_krw=Decimal("720000"),
            portfolio_pct=Decimal("7.2"),
            requested_at=NOW,
            status=ApprovalStatus.APPROVED,
            responded_at=NOW,
            modified_quantity=8,
            response_reason="수량 축소 승인",
        )
        data = m.model_dump(mode="json")
        m2 = ApprovalRequestModel.model_validate(data)
        assert m2.status == ApprovalStatus.APPROVED
        assert m2.modified_quantity == 8


class TestOrderRecord:
    """OrderRecord 모델 테스트."""

    def test_create(self):
        m = OrderRecord(
            id=1,
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("72000"),
            status=OrderStatus.PENDING,
            approval_status=ApprovalStatus.AUTO_APPROVED,
            original_quantity=10,
            created_at=NOW,
        )
        assert m.filled_quantity == 0
        assert m.commission == Decimal(0)
        assert m.rejection_reason == ""
        assert m.web_verify_result is None
        assert m.executed_at is None

    def test_json_roundtrip(self):
        m = OrderRecord(
            id=1,
            symbol="005930",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=5,
            price=Decimal("75000"),
            status=OrderStatus.FILLED,
            approval_status=ApprovalStatus.APPROVED,
            original_quantity=5,
            filled_quantity=5,
            filled_price=Decimal("75100"),
            commission=Decimal("375"),
            broker_order_id="KIS123",
            created_at=NOW,
            executed_at=NOW,
        )
        data = m.model_dump(mode="json")
        m2 = OrderRecord.model_validate(data)
        assert m2.status == OrderStatus.FILLED
        assert m2.filled_price == Decimal("75100")


class TestExecutionRecord:
    """ExecutionRecord 모델 테스트."""

    def test_create(self):
        m = ExecutionRecord(
            id=1,
            order_id=1,
            broker_order_id="KIS123",
            fill_price=Decimal("72000"),
            fill_quantity=10,
            commission=Decimal("360"),
            executed_at=NOW,
        )
        assert m.fill_price == Decimal("72000")
        assert m.commission == Decimal("360")

    def test_json_roundtrip(self):
        m = ExecutionRecord(
            id=1,
            order_id=1,
            broker_order_id="KIS123",
            fill_price=Decimal("72000"),
            fill_quantity=10,
            commission=Decimal("360"),
            executed_at=NOW,
        )
        data = m.model_dump(mode="json")
        m2 = ExecutionRecord.model_validate(data)
        assert m2.order_id == 1
        assert m2.fill_quantity == 10


class TestExecutionResult:
    """ExecutionResult 모델 테스트."""

    def test_create_failure(self):
        m = ExecutionResult(
            success=False,
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            approval_status=ApprovalStatus.REJECTED,
            error="리스크 한도 초과",
        )
        assert not m.success
        assert m.order_id is None
        assert m.position_id is None
        assert m.decision_ids == []

    def test_create_success(self):
        m = ExecutionResult(
            success=True,
            order_id=1,
            broker_order_id="KIS123",
            symbol="005930",
            side=OrderSide.BUY,
            quantity=10,
            fill_price=Decimal("72000"),
            commission=Decimal("360"),
            approval_status=ApprovalStatus.AUTO_APPROVED,
            web_verify_result=WebVerifyResult.SAFE,
            position_id=1,
            decision_ids=[uuid4(), uuid4()],
        )
        assert m.success
        assert m.web_verify_result == WebVerifyResult.SAFE
        assert len(m.decision_ids) == 2

    def test_json_roundtrip(self):
        m = ExecutionResult(
            success=True,
            symbol="005930",
            side=OrderSide.SELL,
            quantity=5,
            approval_status=ApprovalStatus.APPROVED,
            web_verify_result=WebVerifyResult.WARNING,
        )
        data = m.model_dump(mode="json")
        m2 = ExecutionResult.model_validate(data)
        assert m2.web_verify_result == WebVerifyResult.WARNING


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------


class TestPhase5Config:
    """Phase 5 신규 설정값 테스트."""

    def test_defaults(self):
        s = make_settings()
        assert s.WEB_VERIFY_ENABLED is True
        assert s.WEB_VERIFY_SKIP_ON_STOP_LOSS is True
        assert s.AUTO_EXECUTE_MAX_PORTFOLIO_PCT == 5.0

    def test_override(self):
        s = make_settings(
            WEB_VERIFY_ENABLED=False,
            WEB_VERIFY_SKIP_ON_STOP_LOSS=False,
            AUTO_EXECUTE_MAX_PORTFOLIO_PCT=10.0,
        )
        assert s.WEB_VERIFY_ENABLED is False
        assert s.WEB_VERIFY_SKIP_ON_STOP_LOSS is False
        assert s.AUTO_EXECUTE_MAX_PORTFOLIO_PCT == 10.0

    def test_types(self):
        s = make_settings()
        assert isinstance(s.WEB_VERIFY_ENABLED, bool)
        assert isinstance(s.WEB_VERIFY_SKIP_ON_STOP_LOSS, bool)
        assert isinstance(s.AUTO_EXECUTE_MAX_PORTFOLIO_PCT, float)

    def test_existing_phase5_settings(self):
        s = make_settings()
        assert s.HUMAN_APPROVAL_REQUIRED is True
        assert s.HUMAN_APPROVAL_TIMEOUT_SEC == 300
        assert isinstance(s.TELEGRAM_BOT_TOKEN, str)
        assert isinstance(s.TELEGRAM_CHAT_ID, str)
