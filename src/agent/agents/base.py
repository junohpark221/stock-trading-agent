"""BaseAgent ABC — Template Method 패턴 기반 에이전트 추상 클래스.

모든 에이전트는 analyze() 메서드를 통해 동일한 흐름으로 동작:
1. _prepare_data() — 도구 호출로 데이터 수집
2. _build_messages() — 프롬프트 생성
3. router.route_structured() — LLM 호출
4. _post_process() — 후처리
5. recorder.record() — 감사 기록
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from src.agent.decision_recorder import DecisionRecorder
from src.agent.tools.registry import ToolRegistry
from src.core.enums import AgentType, DecisionStage
from src.core.models import LLMMessage
from src.llm.router import LLMRouter

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """에이전트 파이프라인 단계를 구현하는 추상 기반 클래스."""

    def __init__(
        self,
        router: LLMRouter,
        recorder: DecisionRecorder,
        tool_registry: ToolRegistry,
    ) -> None:
        self._router = router
        self._recorder = recorder
        self._tools = tool_registry

    # ── 추상 속성/메서드 ──────────────────────────────────

    @property
    @abstractmethod
    def agent_type(self) -> AgentType:
        """에이전트 유형."""

    @property
    @abstractmethod
    def output_schema(self) -> type[BaseModel]:
        """LLM 구조화 출력 Pydantic 스키마."""

    @property
    @abstractmethod
    def tool_modules(self) -> list[str]:
        """사용할 도구 모듈 이름 리스트."""

    @property
    @abstractmethod
    def decision_stage(self) -> DecisionStage:
        """감사 기록용 DecisionStage."""

    @abstractmethod
    def _build_messages(self, data: dict[str, Any]) -> list[LLMMessage]:
        """프롬프트 메시지 리스트 생성."""

    # ── 오버라이드 가능 훅 ─────────────────────────────────

    async def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """도구 호출로 데이터 수집. 기본: data 그대로 반환."""
        return data

    async def _post_process(
        self, result: BaseModel, data: dict[str, Any]
    ) -> BaseModel:
        """후처리. 기본: result 그대로 반환."""
        return result

    def _extract_decision(self, result: BaseModel) -> str:
        """감사 로그용 decision 문자열 추출."""
        if hasattr(result, "action"):
            action = getattr(result, "action")
            return action.value if hasattr(action, "value") else str(action)
        if hasattr(result, "approved"):
            return "approve" if result.approved else "reject"
        if hasattr(result, "condition"):
            return str(result.condition)
        return "unknown"

    # ── 도구 실행 헬퍼 ─────────────────────────────────────

    async def _execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """도구 실행. 에러 시 빈 dict 반환하고 분석 계속."""
        try:
            return await self._tools.execute(tool_name, arguments)
        except Exception:
            logger.warning(
                "Tool execution failed: %s(%s)", tool_name, arguments,
                exc_info=True,
            )
            return {}

    # ── 핵심 메서드: analyze() ─────────────────────────────

    async def analyze(
        self,
        data: dict[str, Any],
        *,
        session_id: uuid.UUID,
        parent_id: uuid.UUID | None = None,
        symbol: str | None = None,
    ) -> tuple[BaseModel, uuid.UUID]:
        """Template Method — 에이전트 분석 실행.

        Returns:
            (result, decision_id) 튜플
        """
        # 1. 데이터 수집
        prepared = await self._prepare_data(data)

        # 2. 프롬프트 생성
        messages = self._build_messages(prepared)

        # 3. LLM 호출
        result, routing_result = await self._router.route_structured(
            agent_type=self.agent_type.value,
            messages=messages,
            schema=self.output_schema,
        )

        # 4. 후처리
        result = await self._post_process(result, prepared)

        # 5. 감사 기록
        confidence = None
        if hasattr(result, "confidence"):
            confidence = Decimal(str(result.confidence))

        resp = routing_result.response
        decision_id = await self._recorder.record(
            session_id=session_id,
            stage=self.decision_stage.value,
            decision=self._extract_decision(result),
            reasoning=getattr(result, "reasoning", ""),
            parent_id=parent_id,
            agent_type=self.agent_type.value,
            symbol=symbol,
            confidence=confidence,
            llm_provider=resp.provider.value if resp.provider else None,
            llm_model=resp.model,
            llm_tokens_in=resp.tokens_in,
            llm_tokens_out=resp.tokens_out,
            llm_cost_usd=Decimal(str(resp.cost_usd)) if resp.cost_usd else None,
        )

        return result, decision_id
