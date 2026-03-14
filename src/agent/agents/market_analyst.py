"""MarketAnalyst — 한국 주식시장 전체 상황 분석 에이전트."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.agent.agents.base import BaseAgent
from src.agent.prompts.market_analysis import SYSTEM_PROMPT, build_user_prompt
from src.core.enums import AgentType, DecisionStage, MessageRole
from src.core.models import LLMMessage, MarketCondition


class MarketAnalyst(BaseAgent):
    """매크로 지표 + 시장 데이터를 종합하여 MarketCondition을 출력."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.MARKET_ANALYST

    @property
    def output_schema(self) -> type[BaseModel]:
        return MarketCondition

    @property
    def tool_modules(self) -> list[str]:
        return ["macro", "market_data"]

    @property
    def decision_stage(self) -> DecisionStage:
        return DecisionStage.MARKET_ANALYSIS

    async def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        result = dict(data)

        macro_data = await self._execute_tool("get_macro_indicators", {})
        result["macro_data"] = macro_data

        market_summary = await self._execute_tool(
            "get_market_data_summary", {"symbol": "005930", "days": 60}
        )
        result["market_summary"] = market_summary

        return result

    def _build_messages(self, data: dict[str, Any]) -> list[LLMMessage]:
        return [
            LLMMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            LLMMessage(role=MessageRole.USER, content=build_user_prompt(data)),
        ]

    def _extract_decision(self, result: BaseModel) -> str:
        return str(result.condition)  # type: ignore[attr-defined]
