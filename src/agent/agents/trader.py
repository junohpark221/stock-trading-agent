"""Trader — 최종 매매 결정 에이전트."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.agent.agents.base import BaseAgent
from src.agent.prompts.trade_decision import SYSTEM_PROMPT, build_user_prompt
from src.core.enums import AgentType, DecisionStage, MessageRole
from src.core.models import LLMMessage, TradeDecision


class Trader(BaseAgent):
    """Risk Manager 승인 후 구체적인 주문 파라미터를 결정."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.TRADER

    @property
    def output_schema(self) -> type[BaseModel]:
        return TradeDecision

    @property
    def tool_modules(self) -> list[str]:
        return ["market_data"]

    @property
    def decision_stage(self) -> DecisionStage:
        return DecisionStage.TRADE_DECISION

    async def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        symbol = data.get("symbol", "")
        result = dict(data)

        result["current_price"] = await self._execute_tool(
            "get_current_price", {"symbol": symbol}
        )

        return result

    def _build_messages(self, data: dict[str, Any]) -> list[LLMMessage]:
        return [
            LLMMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            LLMMessage(role=MessageRole.USER, content=build_user_prompt(data)),
        ]

    def _extract_decision(self, result: BaseModel) -> str:
        action = getattr(result, "action", None)
        symbol = getattr(result, "symbol", "")
        action_str = action.value if hasattr(action, "value") else str(action)
        return f"{action_str}:{symbol}" if symbol else action_str
