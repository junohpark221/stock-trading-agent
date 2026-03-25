"""Phase 5 Step 6: WebSearchVerifier 단위 테스트.

Tests:
- WEB_VERIFY_ENABLED=False → 즉시 SAFE
- 손절 + WEB_VERIFY_SKIP_ON_STOP_LOSS=True → 즉시 SAFE
- LLM 응답 safe → WebVerification(result=SAFE)
- LLM 응답 warning → WebVerification(result=WARNING, issues_found 포함)
- LLM 응답 blocked → WebVerification(result=BLOCKED)
- LLM 호출 실패 → 기본 SAFE (fail-open)
- decision_log 기록 검증
- 프롬프트에 symbol/side 포함 확인
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import (
    AgentType,
    DecisionStage,
    LLMProviderType,
    MessageRole,
    OrderSide,
    RoutingMode,
    WebVerifyResult,
)
from src.core.models import AgentModelConfig, LLMResponse
from src.execution.web_verify import WebSearchVerifier, _WebVerifyOutput
from src.llm.router import RoutingResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = AgentModelConfig(
    agent_type=AgentType.WEB_VERIFIER,
    routing_mode=RoutingMode.FIXED,
    primary_model="openai/gpt-4o",
)


def _make_routing_result(
    parsed: _WebVerifyOutput,
    *,
    provider: str = "openai",
    model: str = "gpt-4o",
    tokens_in: int = 100,
    tokens_out: int = 50,
    cost_usd: Decimal = Decimal("0.005"),
) -> RoutingResult:
    """route_structured 반환값 생성 헬퍼."""
    response = LLMResponse(
        content=parsed.model_dump_json(),
        model=model,
        provider=LLMProviderType(provider),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )
    return RoutingResult(
        response=response,
        escalated=False,
        primary_response=None,
        config_used=_DEFAULT_CONFIG,
    )


@pytest.fixture()
def mock_settings():
    settings = MagicMock()
    settings.WEB_VERIFY_ENABLED = True
    settings.WEB_VERIFY_SKIP_ON_STOP_LOSS = True
    return settings


@pytest.fixture()
def mock_recorder():
    recorder = AsyncMock()
    recorder.record.return_value = uuid.uuid4()
    return recorder


@pytest.fixture()
def mock_router():
    return AsyncMock()


@pytest.fixture()
def verifier(mock_router, mock_recorder, mock_settings):
    return WebSearchVerifier(
        llm_router=mock_router,
        recorder=mock_recorder,
        settings=mock_settings,
    )


@pytest.fixture()
def session_id():
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# 1. WEB_VERIFY_ENABLED=False → 즉시 SAFE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_disabled_returns_safe(verifier, mock_settings, mock_router, session_id):
    mock_settings.WEB_VERIFY_ENABLED = False

    result = await verifier.verify(
        symbol="005930", side=OrderSide.BUY, session_id=session_id
    )

    assert result.result == WebVerifyResult.SAFE
    assert "비활성화" in result.summary
    mock_router.route_structured.assert_not_called()


# ---------------------------------------------------------------------------
# 2. 손절 + SKIP_ON_STOP_LOSS → 즉시 SAFE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_stop_loss_skip_returns_safe(
    verifier, mock_router, session_id
):
    result = await verifier.verify(
        symbol="005930",
        side=OrderSide.SELL,
        session_id=session_id,
        is_stop_loss=True,
    )

    assert result.result == WebVerifyResult.SAFE
    assert "손절" in result.summary
    mock_router.route_structured.assert_not_called()


@pytest.mark.asyncio()
async def test_stop_loss_no_skip_when_disabled(
    verifier, mock_settings, mock_router, session_id
):
    """WEB_VERIFY_SKIP_ON_STOP_LOSS=False면 손절이어도 검증 실행."""
    mock_settings.WEB_VERIFY_SKIP_ON_STOP_LOSS = False

    parsed = _WebVerifyOutput(
        result="safe",
        summary="특이사항 없음",
        issues=[],
        news_count=3,
        reasoning="최근 뉴스 확인 결과 이상 없음",
    )
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed),
    )

    result = await verifier.verify(
        symbol="005930",
        side=OrderSide.SELL,
        session_id=session_id,
        is_stop_loss=True,
    )

    assert result.result == WebVerifyResult.SAFE
    mock_router.route_structured.assert_called_once()


# ---------------------------------------------------------------------------
# 3. LLM 응답 safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_llm_safe_response(verifier, mock_router, session_id):
    parsed = _WebVerifyOutput(
        result="safe",
        summary="특이사항 없음",
        issues=[],
        news_count=5,
        reasoning="최근 뉴스 확인 결과 이상 없음",
    )
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed),
    )

    result = await verifier.verify(
        symbol="005930", side=OrderSide.BUY, session_id=session_id
    )

    assert result.result == WebVerifyResult.SAFE
    assert result.symbol == "005930"
    assert result.summary == "특이사항 없음"
    assert result.issues_found == []
    assert result.news_checked == 5
    assert result.llm_cost_usd == Decimal("0.005")


# ---------------------------------------------------------------------------
# 4. LLM 응답 warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_llm_warning_response(verifier, mock_router, session_id):
    parsed = _WebVerifyOutput(
        result="warning",
        summary="유상증자 공시 확인",
        issues=["유상증자 결정 공시 (2026-03-24)"],
        news_count=8,
        reasoning="유상증자 결정 공시가 확인되어 주의 필요",
    )
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed),
    )

    result = await verifier.verify(
        symbol="000660", side=OrderSide.BUY, session_id=session_id
    )

    assert result.result == WebVerifyResult.WARNING
    assert result.issues_found == ["유상증자 결정 공시 (2026-03-24)"]
    assert result.news_checked == 8


# ---------------------------------------------------------------------------
# 5. LLM 응답 blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_llm_blocked_response(verifier, mock_router, session_id):
    parsed = _WebVerifyOutput(
        result="blocked",
        summary="거래정지 확인",
        issues=["거래정지 — 상장폐지 사유 발생"],
        news_count=3,
        reasoning="해당 종목은 현재 거래정지 상태",
    )
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed),
    )

    result = await verifier.verify(
        symbol="123456", side=OrderSide.BUY, session_id=session_id
    )

    assert result.result == WebVerifyResult.BLOCKED
    assert "거래정지" in result.issues_found[0]


# ---------------------------------------------------------------------------
# 6. LLM 호출 실패 → fail-open SAFE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_llm_failure_returns_safe(verifier, mock_router, session_id):
    mock_router.route_structured.side_effect = RuntimeError("LLM timeout")

    result = await verifier.verify(
        symbol="005930", side=OrderSide.BUY, session_id=session_id
    )

    assert result.result == WebVerifyResult.SAFE
    assert "fail-open" in result.summary


# ---------------------------------------------------------------------------
# 7. decision_log 기록 검증
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_decision_log_recorded(
    verifier, mock_router, mock_recorder, session_id
):
    parent_id = uuid.uuid4()
    parsed = _WebVerifyOutput(
        result="warning",
        summary="뉴스 확인 필요",
        issues=["실적 쇼크 가능성"],
        news_count=4,
        reasoning="실적 발표 일정이 임박하여 주의 필요",
    )
    cost = Decimal("0.008")
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed, cost_usd=cost, tokens_in=200, tokens_out=80),
    )

    await verifier.verify(
        symbol="035720",
        side=OrderSide.SELL,
        session_id=session_id,
        parent_decision_id=parent_id,
    )

    mock_recorder.record.assert_called_once()
    call_kwargs = mock_recorder.record.call_args.kwargs
    assert call_kwargs["session_id"] == session_id
    assert call_kwargs["stage"] == DecisionStage.EXECUTION
    assert call_kwargs["decision"] == "warning"
    assert call_kwargs["parent_id"] == parent_id
    assert call_kwargs["agent_type"] == AgentType.WEB_VERIFIER
    assert call_kwargs["symbol"] == "035720"
    assert call_kwargs["llm_provider"] == "openai"
    assert call_kwargs["llm_model"] == "gpt-4o"
    assert call_kwargs["llm_tokens_in"] == 200
    assert call_kwargs["llm_tokens_out"] == 80
    assert call_kwargs["llm_cost_usd"] == cost
    assert "issues_found" in call_kwargs["data_snapshot"]


@pytest.mark.asyncio()
async def test_no_decision_log_when_disabled(
    verifier, mock_settings, mock_recorder, session_id
):
    """비활성화 시 decision_log 기록하지 않음."""
    mock_settings.WEB_VERIFY_ENABLED = False

    await verifier.verify(
        symbol="005930", side=OrderSide.BUY, session_id=session_id
    )

    mock_recorder.record.assert_not_called()


# ---------------------------------------------------------------------------
# 8. 프롬프트에 symbol/side 포함 확인
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_prompt_contains_symbol_and_side(verifier, mock_router, session_id):
    parsed = _WebVerifyOutput(
        result="safe",
        summary="정상",
        issues=[],
        news_count=2,
        reasoning="이상 없음",
    )
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed),
    )

    await verifier.verify(
        symbol="005930", side=OrderSide.BUY, session_id=session_id
    )

    call_kwargs = mock_router.route_structured.call_args.kwargs
    messages = call_kwargs["messages"]

    # SYSTEM + USER 메시지 2개
    assert len(messages) == 2
    assert messages[0].role == MessageRole.SYSTEM
    assert messages[1].role == MessageRole.USER

    user_content = messages[1].content
    assert "005930" in user_content
    assert "매수" in user_content


@pytest.mark.asyncio()
async def test_prompt_sell_side(verifier, mock_router, session_id):
    parsed = _WebVerifyOutput(
        result="safe",
        summary="정상",
        issues=[],
        news_count=1,
        reasoning="이상 없음",
    )
    mock_router.route_structured.return_value = (
        parsed,
        _make_routing_result(parsed),
    )

    await verifier.verify(
        symbol="000660", side=OrderSide.SELL, session_id=session_id
    )

    user_content = mock_router.route_structured.call_args.kwargs["messages"][1].content
    assert "000660" in user_content
    assert "매도" in user_content
