"""RiskManager — 리스크 검증 및 매매 승인/거부 에이전트."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.agent.agents.base import BaseAgent
from src.agent.prompts.risk_assessment import SYSTEM_PROMPT, build_user_prompt
from src.core.enums import AgentType, DecisionStage, MessageRole
from src.core.models import LLMMessage, RiskAssessment


class RiskManager(BaseAgent):
    """포지션 크기, 섹터 집중도, 손실 한도 등을 검증하여 승인/거부."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.RISK_MANAGER

    @property
    def output_schema(self) -> type[BaseModel]:
        return RiskAssessment

    @property
    def tool_modules(self) -> list[str]:
        return ["market_data"]

    @property
    def decision_stage(self) -> DecisionStage:
        return DecisionStage.RISK_CHECK

    async def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        symbol = data.get("symbol", "")
        result = dict(data)

        result["current_price"] = await self._execute_tool(
            "get_current_price", {"symbol": symbol}
        )

        result["market_data_summary"] = await self._execute_tool(
            "get_market_data_summary", {"symbol": symbol, "days": 60}
        )

        return result

    def _build_messages(self, data: dict[str, Any]) -> list[LLMMessage]:
        return [
            LLMMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            LLMMessage(role=MessageRole.USER, content=build_user_prompt(data)),
        ]

    def _extract_decision(self, result: BaseModel) -> str:
        return "approve" if result.approved else "reject"  # type: ignore[attr-defined]
