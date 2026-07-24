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


def _extract_flow_signal(flow: dict[str, Any]) -> dict[str, Any] | None:
    """수급 요약 dump에서 스냅샷용 핵심 신호(frgn/orgn 20일 축)만 압축 추출.

    전체 dump(4축×3윈도)는 JSONB 낭비 — 판단 근거의 압축 스냅샷 원칙 준수.
    """
    if not flow:
        return None
    if flow.get("excluded"):
        return {"excluded": True}
    axes = flow.get("axes")
    if not axes:
        return None

    signal: dict[str, Any] = {"as_of": flow.get("as_of")}
    for axis in axes:
        if axis.get("axis") not in ("frgn", "orgn"):
            continue
        key = axis["axis"]
        signal[f"{key}_streak"] = axis.get("streak")
        w20 = next(
            (w for w in axis.get("windows", []) if w.get("window") == 20), None
        )
        signal[f"{key}_20d_net_amt"] = w20.get("net_amt") if w20 else None
    return signal


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
        return ["news", "fundamental", "investor_flow"]

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
        # 수급 요약 (주권·리츠만 — 비대상은 excluded dict → 프롬프트 자동 생략)
        result["investor_flow"] = await self._execute_tool(
            "get_investor_flow_summary", {"symbol": symbol}
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
                "investor_flow": _extract_flow_signal(data.get("investor_flow") or {}),
            },
        }
