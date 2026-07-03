"""ThesisMonitorAgent — 진입 가설 훼손 판단 에이전트 (F-11, 경보형).

중장기 포지션의 진입 가설(entry_analysis_snapshot)을 현재 시점의 뉴스·공시·
펀더멘털과 대조해 훼손 여부를 판단한다. 자동 청산을 만들지 않으며, 판단 결과는
decision_log(stage=HYPOTHESIS_ALERT)에 영속되고, 저빈도 잡이 보수 게이트를 통과한
경우에만 텔레그램 경보를 발송한다(실제 매도는 사용자 수동).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.agent.agents.base import BaseAgent
from src.agent.prompts.thesis_monitor import SYSTEM_PROMPT, build_user_prompt
from src.core.enums import AgentType, DecisionStage, MessageRole
from src.core.models import LLMMessage, ThesisMonitorResult


class ThesisMonitorAgent(BaseAgent):
    """진입 가설 ↔ 현재 뉴스/공시/펀더멘털을 대조해 훼손 여부를 판단."""

    @property
    def agent_type(self) -> AgentType:
        return AgentType.THESIS_MONITOR

    @property
    def output_schema(self) -> type[BaseModel]:
        return ThesisMonitorResult

    @property
    def tool_modules(self) -> list[str]:
        return ["news", "fundamental"]

    @property
    def decision_stage(self) -> DecisionStage:
        return DecisionStage.HYPOTHESIS_ALERT

    async def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        symbol = data.get("symbol", "")
        result = dict(data)

        result["recent_news"] = await self._execute_tool(
            "get_recent_news", {"symbol": symbol, "limit": 15}
        )
        result["disclosures"] = await self._execute_tool(
            "get_disclosures", {"symbol": symbol, "limit": 10}
        )
        result["fundamental"] = await self._execute_tool(
            "get_fundamental_score", {"symbol": symbol}
        )
        result["financials"] = await self._execute_tool(
            "get_financial_statements", {"symbol": symbol}
        )
        return result

    def _build_messages(self, data: dict[str, Any]) -> list[LLMMessage]:
        return [
            LLMMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            LLMMessage(role=MessageRole.USER, content=build_user_prompt(data)),
        ]

    def _extract_decision(self, result: BaseModel) -> str:
        return "hypothesis_broken" if getattr(result, "thesis_broken", False) else "thesis_intact"

    def _build_data_snapshot(
        self, result: BaseModel, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """진입 가설 ↔ 현재 신호 요약을 JSONB로 영속(어드민 표시·사후 추적용).

        원문 뉴스/공시 전체가 아니라 판단 근거의 압축 스냅샷만 남긴다.
        """
        news = data.get("recent_news") or {}
        disc = data.get("disclosures") or {}
        fund = data.get("fundamental") or {}
        return {
            "entry_thesis": data.get("entry_snapshot"),
            "thesis_broken": getattr(result, "thesis_broken", False),
            "severity": getattr(result, "severity", "low"),
            "key_changes": list(getattr(result, "key_changes", []) or []),
            "current_signals": {
                "news_count": news.get("count"),
                "disclosure_count": disc.get("count"),
                "fundamental_score": fund.get("overall_score"),
            },
        }
