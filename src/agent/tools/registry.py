"""ToolRegistry — maps tool names to implementations and generates LLM schemas."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.agent.tools.context import ToolContext
from src.core.models import Tool, ToolParameter

logger = structlog.get_logger(__name__)

ToolFunction = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class RegisteredTool:
    """A tool registered in the registry."""

    definition: Tool
    function: ToolFunction
    module: str


class ToolRegistry:
    """Central registry of agent tool functions.

    On initialization, all built-in tools are registered.  The registry
    provides tool definitions for LLM function-calling and dispatches
    execution by tool name.
    """

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self._tools: dict[str, RegisteredTool] = {}
        self._register_all()

    # -- Public API -----------------------------------------------------------

    def get_tools(self, modules: list[str] | None = None) -> list[Tool]:
        """Return tool definitions, optionally filtered by module names."""
        if modules is None:
            return [t.definition for t in self._tools.values()]
        return [
            t.definition
            for t in self._tools.values()
            if t.module in modules
        ]

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool by name with the given arguments.

        Returns an error dict if the tool is not found.
        """
        registered = self._tools.get(tool_name)
        if registered is None:
            logger.warning("tool.unknown", tool_name=tool_name)
            return {"error": f"Unknown tool: {tool_name}", "tool": tool_name}

        logger.info("tool.execute", tool_name=tool_name, arguments=arguments)
        return await registered.function(self._ctx, **arguments)

    # -- Registration ---------------------------------------------------------

    def _register(
        self,
        name: str,
        description: str,
        func: ToolFunction,
        module: str,
        parameters: list[ToolParameter] | None = None,
    ) -> None:
        self._tools[name] = RegisteredTool(
            definition=Tool(
                name=name,
                description=description,
                parameters=parameters or [],
            ),
            function=func,
            module=module,
        )

    def _register_all(self) -> None:
        """Register all built-in agent tools."""
        from src.agent.tools.fundamental import (
            get_financial_statements,
            get_fundamental_score,
        )
        from src.agent.tools.investor_flow import (
            get_investor_flow_summary,
            get_market_investor_flow_summary,
        )
        from src.agent.tools.macro import get_macro_indicators
        from src.agent.tools.market_data import (
            get_current_price,
            get_market_data_summary,
        )
        from src.agent.tools.news import (
            analyze_news_sentiment,
            get_disclosures,
            get_recent_news,
        )
        from src.agent.tools.technical import (
            get_technical_indicators,
            scan_chart_patterns,
        )

        _sym = ToolParameter(
            name="symbol", type="string", description="종목 코드 (e.g. '005930')", required=True
        )
        _days = ToolParameter(
            name="days", type="integer", description="조회 기간 (거래일 수)", required=False
        )
        _limit = ToolParameter(
            name="limit", type="integer", description="최대 조회 건수", required=False
        )

        # -- Technical --
        self._register(
            "get_technical_indicators",
            "종목의 기술적 지표(RSI, MACD, 볼린저 밴드, 이동평균 등)와 차트 패턴을 분석합니다.",
            get_technical_indicators,
            "technical",
            [_sym, ToolParameter(name="days", type="integer", description="OHLCV 조회 기간 (기본 200일)", required=False)],
        )
        self._register(
            "scan_chart_patterns",
            "차트 패턴(골든크로스, 더블탑 등)과 지지/저항 레벨을 스캔합니다.",
            scan_chart_patterns,
            "technical",
            [_sym, _days],
        )

        # -- Fundamental --
        self._register(
            "get_fundamental_score",
            "종목의 펀더멘털 종합 점수(밸류에이션/성장성/수익성)를 산출합니다.",
            get_fundamental_score,
            "fundamental",
            [_sym],
        )
        self._register(
            "get_financial_statements",
            "종목의 최근 3개년 연간 재무제표를 조회합니다.",
            get_financial_statements,
            "fundamental",
            [_sym],
        )

        # -- Market Data --
        self._register(
            "get_current_price",
            "종목의 최신 주가(종가, 거래량, 등락률 등)를 조회합니다.",
            get_current_price,
            "market_data",
            [_sym],
        )
        self._register(
            "get_market_data_summary",
            "종목의 기간별 시세 요약(고가/저가/평균/변동성/거래량 추이)을 산출합니다.",
            get_market_data_summary,
            "market_data",
            [_sym, ToolParameter(name="days", type="integer", description="요약 기간 (기본 60일)", required=False)],
        )

        # -- News --
        self._register(
            "get_recent_news",
            "종목 관련 최근 뉴스 기사를 조회합니다.",
            get_recent_news,
            "news",
            [_sym, _limit],
        )
        self._register(
            "analyze_news_sentiment",
            "종목의 뉴스 감성 분석(키워드 기반)을 수행합니다.",
            analyze_news_sentiment,
            "news",
            [_sym],
        )
        self._register(
            "get_disclosures",
            "종목의 최근 DART 공시를 조회합니다.",
            get_disclosures,
            "news",
            [_sym, _limit],
        )

        # -- Macro --
        self._register(
            "get_macro_indicators",
            "한국/미국 주요 매크로 경제 지표(금리, 환율, 물가, GDP 등)의 최신값을 조회합니다.",
            get_macro_indicators,
            "macro",
            [],
        )

        # -- Investor Flow (PRJ-03) --
        self._register(
            "get_investor_flow_summary",
            "종목의 투자자별(외국인/기관/개인/금융투자) 순매수 수급 요약"
            "(5/20/60일 누적·연속 스트릭·거래대금 대비 강도)을 조회합니다. "
            "주권·리츠 외 종목(ETF 등)은 소비 제외됩니다.",
            get_investor_flow_summary,
            "investor_flow",
            [_sym, ToolParameter(name="days", type="integer", description="조회 기간 (기본 60거래일, 최대 120)", required=False)],
        )
        self._register(
            "get_market_investor_flow_summary",
            "시장(KOSPI/KOSDAQ) 단위 투자자별 순매수 수급 요약과 지수 수익률 컨텍스트를 조회합니다. market 생략 시 두 시장 모두.",
            get_market_investor_flow_summary,
            "investor_flow",
            [
                ToolParameter(name="market", type="string", description="'kospi' 또는 'kosdaq' (생략 시 둘 다)", required=False, enum=["kospi", "kosdaq"]),
                ToolParameter(name="days", type="integer", description="조회 기간 (기본 60거래일, 최대 120)", required=False),
            ],
        )
