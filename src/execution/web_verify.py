"""WebSearchVerifier — LLM web search final verification before order execution.

Checks for urgent disclosures, sudden adverse news, trading suspensions, etc.
Uses LLMRouter.route_structured() with OpenAI web search capability.

Fail-open design: if LLM call fails, defaults to SAFE (infrastructure failure
should not block legitimate trades).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from src.core.enums import (
    AgentType,
    DecisionStage,
    MessageRole,
    OrderSide,
    WebVerifyResult,
)
from src.core.models import LLMMessage, WebVerification

if TYPE_CHECKING:
    from src.agent.decision_recorder import DecisionRecorder
    from src.config import Settings
    from src.data.cache import RedisCache
    from src.llm.router import LLMRouter

logger = structlog.get_logger(__name__)


class _WebVerifyOutput(BaseModel):
    """LLM structured output 스키마.

    OpenAI strict mode 호환: ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    result: str  # "safe" / "warning" / "blocked"
    summary: str  # 검증 요약 (한국어, 100자 이내)
    issues: list[str]  # 감지된 이슈 목록
    news_count: int  # 확인한 뉴스/정보 건수
    reasoning: str  # 판단 근거


_SIDE_KR = {OrderSide.BUY: "매수", OrderSide.SELL: "매도"}

_SYSTEM_PROMPT = (
    "당신은 한국 주식 투자 전문가입니다. "
    "주문 실행 직전 최종 안전 검증 역할을 수행합니다.\n"
    "웹검색을 통해 해당 종목의 최신 뉴스와 이슈를 확인하고, "
    "주문 진행 여부를 판단하세요."
)

_USER_PROMPT_TEMPLATE = (
    "다음 종목의 최신 뉴스/이슈를 웹검색으로 확인해주세요.\n\n"
    "종목코드: {symbol}\n"
    "주문방향: {side}\n\n"
    "## 확인 항목\n"
    "1. 긴급 공시 (상장폐지, 관리종목 지정, 투자주의 환기종목 등)\n"
    "2. 돌발 악재/호재 (실적 쇼크, 대규모 소송, 대주주 변경 등)\n"
    "3. 블록딜/유상증자/전환사채 행사\n"
    "4. 거래정지/매매거래 제한\n"
    "5. 기타 주가에 큰 영향을 미칠 수 있는 사건\n\n"
    "## 판단 기준\n"
    "- safe: 특이사항 없음, 주문 진행 가능\n"
    "- warning: 주의 필요한 이슈 존재, 투자자 확인 필요\n"
    "- blocked: 위험 감지, 주문 차단 권고 (거래정지, 상장폐지 등)\n"
)


class WebSearchVerifier:
    """주문 실행 직전 LLM Web Search 최종 검증.

    LLM 내장 웹검색으로 해당 종목의 최신 뉴스/이슈를 확인한다.
    긴급 공시, 돌발 악재, 거래정지 등 급변 상황을 감지하여
    위험 주문을 사전에 차단한다.

    ``agent_model_config``에 ``web_verifier`` 에이전트 타입이 등록되어야 한다.
    """

    _WEB_VERIFY_CACHE_TTL = 300  # 5분

    def __init__(
        self,
        *,
        llm_router: LLMRouter,
        recorder: DecisionRecorder,
        settings: Settings,
        cache: RedisCache | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._recorder = recorder
        self._settings = settings
        self._cache = cache

    async def verify(
        self,
        *,
        symbol: str,
        side: OrderSide,
        session_id: uuid.UUID,
        parent_decision_id: uuid.UUID | None = None,
        is_stop_loss: bool = False,
    ) -> WebVerification:
        """종목에 대한 최종 웹 검증을 수행한다.

        Args:
            symbol: 종목코드.
            side: 주문 방향 (BUY / SELL).
            session_id: 파이프라인 세션 ID.
            parent_decision_id: 부모 결정 ID (audit chain).
            is_stop_loss: 손절 주문 여부.

        Returns:
            WebVerification with result, summary, issues, cost info.
        """
        # 1. 설정으로 비활성화된 경우
        if not self._settings.WEB_VERIFY_ENABLED:
            logger.info("web_verify.skipped", reason="disabled", symbol=symbol)
            return self._safe_default(symbol, summary="웹 검증 비활성화")

        # 2. 손절 주문 + 손절 시 건너뛰기 설정
        if is_stop_loss and self._settings.WEB_VERIFY_SKIP_ON_STOP_LOSS:
            logger.info("web_verify.skipped", reason="stop_loss", symbol=symbol)
            return self._safe_default(symbol, summary="손절 주문 — 웹 검증 생략")

        # 3. 캐시 체크 (같은 종목+방향의 최근 검증 결과 재사용)
        cache_key = f"{symbol}:{side.value}"
        if self._cache is not None:
            try:
                cached = await self._cache.get_json("web_verify", cache_key)
                if cached is not None:
                    logger.info("web_verify.cache_hit", symbol=symbol, side=side.value)
                    return WebVerification.model_validate(cached)
            except Exception:
                pass  # 캐시 실패 시 무시, LLM 호출로 진행

        # 4. LLM 웹검색 호출
        try:
            messages = self._build_messages(symbol, side)
            parsed, routing_result = await self._llm_router.route_structured(
                agent_type=AgentType.WEB_VERIFIER,
                messages=messages,
                schema=_WebVerifyOutput,
                temperature=0.3,
            )

            # 4. _WebVerifyOutput → WebVerification 변환
            result_enum = WebVerifyResult(parsed.result)
            resp = routing_result.response
            verification = WebVerification(
                symbol=symbol,
                result=result_enum,
                summary=parsed.summary,
                issues_found=parsed.issues,
                news_checked=parsed.news_count,
                llm_cost_usd=resp.cost_usd,
                reasoning=parsed.reasoning,
            )

            # 5. decision_log 기록
            await self._recorder.record(
                session_id=session_id,
                stage=DecisionStage.EXECUTION,
                decision=result_enum.value,
                reasoning=parsed.reasoning,
                parent_id=parent_decision_id,
                agent_type=AgentType.WEB_VERIFIER,
                symbol=symbol,
                llm_provider=resp.provider.value,
                llm_model=resp.model,
                llm_tokens_in=resp.tokens_in,
                llm_tokens_out=resp.tokens_out,
                llm_cost_usd=resp.cost_usd,
                data_snapshot={
                    "issues_found": parsed.issues,
                    "news_count": parsed.news_count,
                    "summary": parsed.summary,
                },
            )

            logger.info(
                "web_verify.completed",
                symbol=symbol,
                result=result_enum.value,
                issues_count=len(parsed.issues),
            )

            # 캐시 저장
            if self._cache is not None:
                try:
                    await self._cache.set_json(
                        "web_verify", cache_key,
                        verification.model_dump(mode="json"),
                        ttl=self._WEB_VERIFY_CACHE_TTL,
                    )
                except Exception:
                    pass  # 캐시 실패 시 무시

            return verification

        except Exception:
            # Fail-open: LLM 실패 시 SAFE 반환, 감사 기록은 남긴다
            logger.exception("web_verify.failed", symbol=symbol)
            try:
                await self._recorder.record(
                    session_id=session_id,
                    stage=DecisionStage.EXECUTION,
                    decision="safe",
                    reasoning="웹 검증 실패 — fail-open 기본값 적용 (인프라 장애)",
                    parent_id=parent_decision_id,
                    agent_type=AgentType.WEB_VERIFIER,
                    symbol=symbol,
                    data_snapshot={"error": "infrastructure_failure", "fail_open": True},
                )
            except Exception:
                logger.exception("web_verify.audit_record_failed", symbol=symbol)
            return self._safe_default(
                symbol, summary="웹 검증 실패 — fail-open 기본값 적용"
            )

    def _build_messages(self, symbol: str, side: OrderSide) -> list[LLMMessage]:
        """웹검색 프롬프트를 구성한다."""
        side_kr = _SIDE_KR.get(side, str(side))
        return [
            LLMMessage(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            LLMMessage(
                role=MessageRole.USER,
                content=_USER_PROMPT_TEMPLATE.format(symbol=symbol, side=side_kr),
            ),
        ]

    @staticmethod
    def _safe_default(symbol: str, *, summary: str) -> WebVerification:
        """SAFE 기본값을 생성한다."""
        return WebVerification(
            symbol=symbol,
            result=WebVerifyResult.SAFE,
            summary=summary,
            issues_found=[],
            news_checked=0,
            llm_cost_usd=Decimal(0),
            reasoning=summary,
        )
