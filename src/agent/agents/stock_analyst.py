"""StockAnalyst — 개별 종목 분석 에이전트 (하이브리드 감성분석 통합)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from src.agent.agents.base import BaseAgent
from src.agent.prompts.stock_analysis import SYSTEM_PROMPT, build_user_prompt
from src.core.enums import AgentType, DecisionStage, MessageRole, SentimentMethod
from src.core.models import LLMMessage, SentimentResult, StockAnalysis


class StockAnalyst(BaseAgent):
    """기술적/펀더멘털/감성 분석을 종합하여 StockAnalysis를 출력.

    하이브리드 감성분석:
    - 1차: 키워드 감성분석 (비용 $0)
    - needs_llm_analysis=True → 뉴스 본문을 LLM 프롬프트에 포함
    - sentiment 필드는 LLM이 아닌 _post_process에서 프로그래밍적 첨부
    """

    @property
    def agent_type(self) -> AgentType:
        return AgentType.STOCK_ANALYST

    @property
    def output_schema(self) -> type[BaseModel]:
        return StockAnalysis

    @property
    def tool_modules(self) -> list[str]:
        return ["technical", "fundamental", "market_data", "news"]

    @property
    def decision_stage(self) -> DecisionStage:
        return DecisionStage.STOCK_ANALYSIS

    async def _prepare_data(self, data: dict[str, Any]) -> dict[str, Any]:
        symbol = data.get("symbol", "")
        result = dict(data)

        # 기술적 지표
        result["technical_indicators"] = await self._execute_tool(
            "get_technical_indicators", {"symbol": symbol}
        )

        # 차트 패턴
        result["chart_patterns"] = await self._execute_tool(
            "scan_chart_patterns", {"symbol": symbol}
        )

        # 펀더멘털
        result["fundamental_score"] = await self._execute_tool(
            "get_fundamental_score", {"symbol": symbol}
        )

        # 현재가
        result["current_price"] = await self._execute_tool(
            "get_current_price", {"symbol": symbol}
        )

        # 키워드 감성분석 (1차)
        sentiment_result = await self._execute_tool(
            "analyze_news_sentiment", {"symbol": symbol}
        )
        result["sentiment"] = sentiment_result

        # 하이브리드: 키워드 분석이 LLM 심층 분석 필요 판정 시 뉴스 본문 추가
        needs_llm = sentiment_result.get("needs_llm_analysis", False)
        result["_needs_llm_analysis"] = needs_llm
        if needs_llm:
            news = await self._execute_tool(
                "get_recent_news", {"symbol": symbol, "limit": 10}
            )
            result["news_articles"] = news.get("articles", []) if isinstance(news, dict) else []

        return result

    def _build_messages(self, data: dict[str, Any]) -> list[LLMMessage]:
        return [
            LLMMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            LLMMessage(role=MessageRole.USER, content=build_user_prompt(data)),
        ]

    async def _post_process(
        self, result: BaseModel, data: dict[str, Any]
    ) -> BaseModel:
        """키워드/LLM 감성분석 결과를 sentiment 필드에 프로그래밍적 첨부."""
        sentiment_data = data.get("sentiment", {})
        if not sentiment_data:
            return result

        needs_llm = data.get("_needs_llm_analysis", False)
        method = SentimentMethod.LLM if needs_llm else SentimentMethod.KEYWORD

        # sentiment_data가 이미 SentimentResult dict이므로 method만 오버라이드
        sentiment_data_copy = dict(sentiment_data)
        sentiment_data_copy["method"] = method.value

        try:
            sentiment = SentimentResult(**sentiment_data_copy)
        except Exception:
            return result

        # StockAnalysis의 sentiment 필드에 첨부
        if hasattr(result, "model_copy"):
            result = result.model_copy(update={"sentiment": sentiment})

        return result

    def _extract_decision(self, result: BaseModel) -> str:
        action = getattr(result, "action", None)
        return action.value if hasattr(action, "value") else str(action)
