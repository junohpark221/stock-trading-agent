"""4개 에이전트 + 프롬프트 단위 테스트.

BaseAgent ABC, MarketAnalyst, StockAnalyst(하이브리드 감성분석),
RiskManager, Trader, 프롬프트 모듈을 테스트한다.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.agents.base import BaseAgent
from src.agent.agents.market_analyst import MarketAnalyst
from src.agent.agents.risk_manager import RiskManager
from src.agent.agents.stock_analyst import StockAnalyst
from src.agent.agents.trader import Trader
from src.agent.prompts import market_analysis, risk_assessment, stock_analysis, trade_decision
from src.core.enums import (
    AgentType,
    DecisionAction,
    DecisionStage,
    LLMProviderType,
    MessageRole,
    SentimentLabel,
    SentimentMethod,
)
from src.core.models import (
    AgentModelConfig,
    LLMMessage,
    LLMResponse,
    MarketCondition,
    RiskAssessment,
    SectorOutlook,
    SentimentResult,
    StockAnalysis,
    TradeDecision,
)
from src.llm.router import RoutingResult


# ── 샘플 데이터 ────────────────────────────────────────────

def _sample_market_condition() -> MarketCondition:
    return MarketCondition(
        condition="bullish",
        confidence=Decimal("0.75"),
        kospi_trend="상승",
        kosdaq_trend="횡보",
        market_risk_level="medium",
        key_factors=["금리 동결", "외국인 순매수"],
        sector_outlook=[
            SectorOutlook(sector="반도체", outlook="긍정"),
            SectorOutlook(sector="바이오", outlook="중립"),
        ],
        macro_summary="안정적 성장 국면",
        recommended_exposure=Decimal("0.7"),
        reasoning="매크로 안정, 외국인 유입 지속",
    )


def _sample_stock_analysis() -> StockAnalysis:
    return StockAnalysis(
        symbol="005930",
        name="삼성전자",
        action=DecisionAction.BUY,
        confidence=Decimal("0.8"),
        technical_score=Decimal("72"),
        technical_summary="MACD 골든크로스, RSI 55",
        fundamental_score=Decimal("68"),
        fundamental_summary="PER 12.5, ROE 15%",
        sentiment=None,
        target_price=Decimal("85000"),
        stop_loss_price=Decimal("72000"),
        current_price=Decimal("78000"),
        key_factors=["골든크로스", "외국인 매수"],
        risks=["환율 변동", "반도체 업황 둔화"],
        reasoning="기술적 상승 신호와 양호한 펀더멘털",
    )


def _sample_risk_assessment() -> RiskAssessment:
    return RiskAssessment(
        symbol="005930",
        approved=True,
        risk_level="medium",
        confidence=Decimal("0.85"),
        recommended_quantity=10,
        recommended_position_size_krw=Decimal("780000"),
        max_loss_krw=Decimal("60000"),
        portfolio_concentration_ok=True,
        sector_exposure_ok=True,
        daily_loss_limit_ok=True,
        position_size_ok=True,
        volatility_ok=True,
        risk_factors=["반도체 섹터 집중"],
        conditions=["10주 이하 분할 매수"],
        reasoning="리스크 지표 정상 범위",
    )


def _sample_trade_decision() -> TradeDecision:
    return TradeDecision(
        symbol="005930",
        action=DecisionAction.BUY,
        confidence=Decimal("0.82"),
        order_type="limit",
        quantity=10,
        price=Decimal("78000"),
        stop_loss_price=Decimal("72000"),
        take_profit_price=Decimal("84000"),
        risk_reward_ratio=Decimal("1.75"),
        expected_return_pct=Decimal("7.69"),
        max_loss_pct=Decimal("7.69"),
        reasoning="리스크/수익 비율 양호",
        requires_approval=False,
    )


def _sample_llm_response() -> LLMResponse:
    return LLMResponse(
        content="{}",
        model="gpt-4o",
        provider=LLMProviderType.OPENAI,
        tokens_in=500,
        tokens_out=200,
        cost_usd=Decimal("0.005"),
    )


def _sample_routing_result(config_agent: str = "market_analyst") -> RoutingResult:
    return RoutingResult(
        response=_sample_llm_response(),
        escalated=False,
        config_used=AgentModelConfig(
            agent_type=config_agent,
            routing_mode="fixed",
            primary_model="openai/gpt-4o",
        ),
    )


def _sample_sentiment_data(*, needs_llm: bool = False) -> dict[str, Any]:
    return {
        "symbol": "005930",
        "overall_score": "0.35",
        "overall_label": "positive",
        "method": "keyword",
        "positive_count": 5,
        "negative_count": 2,
        "neutral_count": 3,
        "total_articles": 10,
        "key_topics": ["반도체", "실적"],
        "needs_llm_analysis": needs_llm,
        "reasoning": "긍정 키워드 우세",
    }


# ── 픽스처 ──────────────────────────────────────────────────

@pytest.fixture
def mock_router():
    router = AsyncMock()
    router.route_structured = AsyncMock()
    return router


@pytest.fixture
def mock_recorder():
    recorder = AsyncMock()
    recorder.record = AsyncMock(return_value=uuid.uuid4())
    return recorder


@pytest.fixture
def mock_tool_registry():
    registry = AsyncMock()
    registry.execute = AsyncMock(return_value={})
    registry.get_tools = MagicMock(return_value=[])
    return registry


@pytest.fixture
def session_id():
    return uuid.uuid4()


# ── 1. BaseAgent ABC 테스트 ─────────────────────────────────

class TestBaseAgentABC:
    """BaseAgent 추상 클래스 테스트."""

    def test_cannot_instantiate_directly(self, mock_router, mock_recorder, mock_tool_registry):
        """BaseAgent를 직접 인스턴스화할 수 없다."""
        with pytest.raises(TypeError):
            BaseAgent(mock_router, mock_recorder, mock_tool_registry)

    def test_must_implement_all_abstract_methods(self):
        """모든 추상 메서드를 구현해야 한다."""
        class PartialAgent(BaseAgent):
            @property
            def agent_type(self): return AgentType.MARKET_ANALYST
            # Missing: output_schema, tool_modules, decision_stage, _build_messages

        with pytest.raises(TypeError):
            PartialAgent(AsyncMock(), AsyncMock(), AsyncMock())

    @pytest.mark.asyncio
    async def test_analyze_calls_in_order(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """analyze()가 올바른 순서로 호출된다."""
        call_order = []

        class TestAgent(BaseAgent):
            @property
            def agent_type(self): return AgentType.MARKET_ANALYST
            @property
            def output_schema(self): return MarketCondition
            @property
            def tool_modules(self): return []
            @property
            def decision_stage(self): return DecisionStage.MARKET_ANALYSIS

            async def _prepare_data(self, data):
                call_order.append("prepare")
                return data

            def _build_messages(self, data):
                call_order.append("build")
                return []

            async def _post_process(self, result, data):
                call_order.append("post")
                return result

        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        agent = TestAgent(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze({}, session_id=session_id)

        assert call_order == ["prepare", "build", "post"]
        mock_router.route_structured.assert_called_once()
        mock_recorder.record.assert_called_once()

    @pytest.mark.asyncio
    async def test_analyze_returns_tuple(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """analyze()는 (BaseModel, UUID) 튜플을 반환한다."""
        class TestAgent(BaseAgent):
            @property
            def agent_type(self): return AgentType.MARKET_ANALYST
            @property
            def output_schema(self): return MarketCondition
            @property
            def tool_modules(self): return []
            @property
            def decision_stage(self): return DecisionStage.MARKET_ANALYSIS
            def _build_messages(self, data): return []

        mc = _sample_market_condition()
        decision_id = uuid.uuid4()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())
        mock_recorder.record.return_value = decision_id

        agent = TestAgent(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze({}, session_id=session_id)

        assert isinstance(result, MarketCondition)
        assert did == decision_id


# ── 2. MarketAnalyst 테스트 ─────────────────────────────────

class TestMarketAnalyst:
    """MarketAnalyst 에이전트 테스트."""

    def test_properties(self, mock_router, mock_recorder, mock_tool_registry):
        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)
        assert agent.agent_type == AgentType.MARKET_ANALYST
        assert agent.output_schema is MarketCondition
        assert agent.tool_modules == ["macro", "market_data"]
        assert agent.decision_stage == DecisionStage.MARKET_ANALYSIS

    @pytest.mark.asyncio
    async def test_prepare_data_calls_tools(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """_prepare_data에서 get_macro_indicators, get_market_data_summary 호출."""
        mock_tool_registry.execute = AsyncMock(side_effect=[
            {"kr_rate": "3.5", "us_rate": "5.25"},  # macro
            {"close": 78000, "change": 1.2},  # market summary
        ])

        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)
        result = await agent._prepare_data({})

        assert mock_tool_registry.execute.call_count == 2
        calls = mock_tool_registry.execute.call_args_list
        assert calls[0].args[0] == "get_macro_indicators"
        assert calls[1].args[0] == "get_market_data_summary"
        assert result["macro_data"] == {"kr_rate": "3.5", "us_rate": "5.25"}
        assert result["market_summary"] == {"close": 78000, "change": 1.2}

    @pytest.mark.asyncio
    async def test_analyze_returns_market_condition(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze({}, session_id=session_id)

        assert isinstance(result, MarketCondition)
        assert result.condition == "bullish"

    @pytest.mark.asyncio
    async def test_extract_decision(self, mock_router, mock_recorder, mock_tool_registry):
        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)
        mc = _sample_market_condition()
        assert agent._extract_decision(mc) == "bullish"

    @pytest.mark.asyncio
    async def test_decision_record_params(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """감사 기록에 올바른 stage, decision 전달."""
        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)
        await agent.analyze({}, session_id=session_id)

        record_kwargs = mock_recorder.record.call_args.kwargs
        assert record_kwargs["stage"] == "market_analysis"
        assert record_kwargs["decision"] == "bullish"
        assert record_kwargs["agent_type"] == "market_analyst"


# ── 3. StockAnalyst 테스트 ──────────────────────────────────

class TestStockAnalyst:
    """StockAnalyst 에이전트 테스트 (하이브리드 감성분석)."""

    def test_properties(self, mock_router, mock_recorder, mock_tool_registry):
        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        assert agent.agent_type == AgentType.STOCK_ANALYST
        assert agent.output_schema is StockAnalysis
        assert agent.tool_modules == ["technical", "fundamental", "market_data", "news"]
        assert agent.decision_stage == DecisionStage.STOCK_ANALYSIS

    @pytest.mark.asyncio
    async def test_prepare_data_keyword_only(self, mock_router, mock_recorder, mock_tool_registry):
        """needs_llm_analysis=False → 5개 도구 호출 (get_recent_news 미호출)."""
        sentiment = _sample_sentiment_data(needs_llm=False)
        mock_tool_registry.execute = AsyncMock(side_effect=[
            {"rsi": 55, "macd": 0.5},   # technical
            {"patterns": []},             # chart_patterns
            {"score": 68},                # fundamental
            {"price": 78000},             # current_price
            sentiment,                    # sentiment
        ])

        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        result = await agent._prepare_data({"symbol": "005930"})

        assert mock_tool_registry.execute.call_count == 5
        assert result["_needs_llm_analysis"] is False
        assert "news_articles" not in result

    @pytest.mark.asyncio
    async def test_prepare_data_hybrid_llm(self, mock_router, mock_recorder, mock_tool_registry):
        """needs_llm_analysis=True → 6개 도구 호출 (get_recent_news 추가)."""
        sentiment = _sample_sentiment_data(needs_llm=True)
        mock_tool_registry.execute = AsyncMock(side_effect=[
            {"rsi": 55, "macd": 0.5},
            {"patterns": []},
            {"score": 68},
            {"price": 78000},
            sentiment,
            {"articles": [{"title": "삼성 실적 호조", "source": "경제신문"}]},  # news
        ])

        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        result = await agent._prepare_data({"symbol": "005930"})

        assert mock_tool_registry.execute.call_count == 6
        assert result["_needs_llm_analysis"] is True
        assert len(result["news_articles"]) == 1

    @pytest.mark.asyncio
    async def test_post_process_keyword_method(self, mock_router, mock_recorder, mock_tool_registry):
        """needs_llm_analysis=False → sentiment.method == KEYWORD."""
        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        sa = _sample_stock_analysis()
        data = {"sentiment": _sample_sentiment_data(needs_llm=False), "_needs_llm_analysis": False}

        result = await agent._post_process(sa, data)
        assert result.sentiment is not None
        assert result.sentiment.method == SentimentMethod.KEYWORD

    @pytest.mark.asyncio
    async def test_post_process_llm_method(self, mock_router, mock_recorder, mock_tool_registry):
        """needs_llm_analysis=True → sentiment.method == LLM."""
        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        sa = _sample_stock_analysis()
        data = {"sentiment": _sample_sentiment_data(needs_llm=True), "_needs_llm_analysis": True}

        result = await agent._post_process(sa, data)
        assert result.sentiment is not None
        assert result.sentiment.method == SentimentMethod.LLM

    @pytest.mark.asyncio
    async def test_analyze_returns_stock_analysis(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        sa = _sample_stock_analysis()
        mock_router.route_structured.return_value = (sa, _sample_routing_result("stock_analyst"))
        mock_tool_registry.execute = AsyncMock(side_effect=[
            {"rsi": 55}, {}, {"score": 68}, {"price": 78000},
            _sample_sentiment_data(needs_llm=False),
        ])

        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze({"symbol": "005930"}, session_id=session_id, symbol="005930")

        assert isinstance(result, StockAnalysis)

    @pytest.mark.asyncio
    async def test_extract_decision(self, mock_router, mock_recorder, mock_tool_registry):
        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        sa = _sample_stock_analysis()
        assert agent._extract_decision(sa) == "buy"

    @pytest.mark.asyncio
    async def test_decision_record_with_symbol(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """감사 기록에 symbol 필드 설정."""
        sa = _sample_stock_analysis()
        mock_router.route_structured.return_value = (sa, _sample_routing_result("stock_analyst"))
        mock_tool_registry.execute = AsyncMock(side_effect=[
            {}, {}, {}, {}, _sample_sentiment_data(needs_llm=False),
        ])

        agent = StockAnalyst(mock_router, mock_recorder, mock_tool_registry)
        await agent.analyze({"symbol": "005930"}, session_id=session_id, symbol="005930")

        record_kwargs = mock_recorder.record.call_args.kwargs
        assert record_kwargs["symbol"] == "005930"
        assert record_kwargs["stage"] == "stock_analysis"


# ── 4. RiskManager 테스트 ───────────────────────────────────

class TestRiskManager:
    """RiskManager 에이전트 테스트."""

    def test_properties(self, mock_router, mock_recorder, mock_tool_registry):
        agent = RiskManager(mock_router, mock_recorder, mock_tool_registry)
        assert agent.agent_type == AgentType.RISK_MANAGER
        assert agent.output_schema is RiskAssessment
        assert agent.tool_modules == ["market_data"]
        assert agent.decision_stage == DecisionStage.RISK_CHECK

    @pytest.mark.asyncio
    async def test_prepare_data_calls_tools(self, mock_router, mock_recorder, mock_tool_registry):
        mock_tool_registry.execute = AsyncMock(side_effect=[
            {"price": 78000},
            {"volatility": 0.25, "avg_volume": 1000000},
        ])

        agent = RiskManager(mock_router, mock_recorder, mock_tool_registry)
        result = await agent._prepare_data({"symbol": "005930"})

        assert mock_tool_registry.execute.call_count == 2
        assert result["current_price"] == {"price": 78000}
        assert result["market_data_summary"]["volatility"] == 0.25

    @pytest.mark.asyncio
    async def test_analyze_approved(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        ra = _sample_risk_assessment()
        mock_router.route_structured.return_value = (ra, _sample_routing_result("risk_manager"))

        agent = RiskManager(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze(
            {"symbol": "005930", "stock_analysis": _sample_stock_analysis()},
            session_id=session_id,
            symbol="005930",
        )

        assert isinstance(result, RiskAssessment)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_extract_decision_approve(self, mock_router, mock_recorder, mock_tool_registry):
        agent = RiskManager(mock_router, mock_recorder, mock_tool_registry)
        ra = _sample_risk_assessment()
        assert agent._extract_decision(ra) == "approve"

    @pytest.mark.asyncio
    async def test_extract_decision_reject(self, mock_router, mock_recorder, mock_tool_registry):
        agent = RiskManager(mock_router, mock_recorder, mock_tool_registry)
        ra = RiskAssessment(
            symbol="005930", approved=False, risk_level="high",
            confidence=Decimal("0.9"), reasoning="리스크 초과",
        )
        assert agent._extract_decision(ra) == "reject"


# ── 5. Trader 테스트 ────────────────────────────────────────

class TestTrader:
    """Trader 에이전트 테스트."""

    def test_properties(self, mock_router, mock_recorder, mock_tool_registry):
        agent = Trader(mock_router, mock_recorder, mock_tool_registry)
        assert agent.agent_type == AgentType.TRADER
        assert agent.output_schema is TradeDecision
        assert agent.tool_modules == ["market_data"]
        assert agent.decision_stage == DecisionStage.TRADE_DECISION

    @pytest.mark.asyncio
    async def test_analyze_returns_trade_decision(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        td = _sample_trade_decision()
        mock_router.route_structured.return_value = (td, _sample_routing_result("trader"))
        mock_tool_registry.execute = AsyncMock(return_value={"price": 78000})

        agent = Trader(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze(
            {"symbol": "005930", "stock_analysis": _sample_stock_analysis(),
             "risk_assessment": _sample_risk_assessment()},
            session_id=session_id,
            symbol="005930",
        )

        assert isinstance(result, TradeDecision)
        assert result.action == DecisionAction.BUY

    @pytest.mark.asyncio
    async def test_extract_decision_with_symbol(self, mock_router, mock_recorder, mock_tool_registry):
        agent = Trader(mock_router, mock_recorder, mock_tool_registry)
        td = _sample_trade_decision()
        assert agent._extract_decision(td) == "buy:005930"

    @pytest.mark.asyncio
    async def test_decision_record_params(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        td = _sample_trade_decision()
        mock_router.route_structured.return_value = (td, _sample_routing_result("trader"))
        mock_tool_registry.execute = AsyncMock(return_value={"price": 78000})

        agent = Trader(mock_router, mock_recorder, mock_tool_registry)
        await agent.analyze(
            {"symbol": "005930"}, session_id=session_id, symbol="005930",
        )

        record_kwargs = mock_recorder.record.call_args.kwargs
        assert record_kwargs["stage"] == "trade_decision"
        assert record_kwargs["symbol"] == "005930"
        assert record_kwargs["llm_model"] == "gpt-4o"


# ── 6. 프롬프트 모듈 테스트 ────────────────────────────────

class TestPromptModules:
    """프롬프트 모듈 export 및 기본 검증."""

    @pytest.mark.parametrize("mod", [
        market_analysis, stock_analysis, risk_assessment, trade_decision,
    ])
    def test_exports(self, mod):
        """SYSTEM_PROMPT(str) + build_user_prompt(callable) export 확인."""
        assert hasattr(mod, "SYSTEM_PROMPT")
        assert isinstance(mod.SYSTEM_PROMPT, str)
        assert hasattr(mod, "build_user_prompt")
        assert callable(mod.build_user_prompt)

    @pytest.mark.parametrize("mod", [
        market_analysis, stock_analysis, risk_assessment, trade_decision,
    ])
    def test_system_prompt_length(self, mod):
        """시스템 프롬프트 < 8,000자 (≈ 2,000 토큰)."""
        assert len(mod.SYSTEM_PROMPT) < 8000

    @pytest.mark.parametrize("mod", [
        market_analysis, stock_analysis, risk_assessment, trade_decision,
    ])
    def test_system_prompt_korean(self, mod):
        """시스템 프롬프트에 한국어 포함."""
        # 한글 유니코드 범위 체크
        has_korean = any("\uac00" <= c <= "\ud7a3" for c in mod.SYSTEM_PROMPT)
        assert has_korean, "시스템 프롬프트에 한국어가 포함되어야 합니다"

    def test_market_analysis_prompt(self):
        data = {
            "macro_data": {"kr_rate": "3.5"},
            "market_summary": {"close": 78000},
        }
        prompt = market_analysis.build_user_prompt(data)
        assert len(prompt) > 0
        assert "3.5" in prompt

    def test_stock_analysis_prompt_basic(self):
        data = {
            "symbol": "005930",
            "technical_indicators": {"rsi": 55},
            "current_price": {"price": 78000},
        }
        prompt = stock_analysis.build_user_prompt(data)
        assert "005930" in prompt
        assert len(prompt) > 0

    def test_stock_analysis_prompt_with_news(self):
        """needs_llm_analysis=True → 뉴스 섹션 포함."""
        data = {
            "symbol": "005930",
            "news_articles": [
                {"title": "삼성 실적 서프라이즈", "source": "경제신문", "date": "2026-03-14"},
            ],
        }
        prompt = stock_analysis.build_user_prompt(data)
        assert "심층 분석" in prompt
        assert "삼성 실적 서프라이즈" in prompt

    def test_risk_assessment_prompt(self):
        data = {
            "symbol": "005930",
            "stock_analysis": _sample_stock_analysis(),
            "portfolio": {"total_value": 10000000, "cash": 5000000},
        }
        prompt = risk_assessment.build_user_prompt(data)
        assert "005930" in prompt
        assert "포트폴리오" in prompt

    def test_trade_decision_prompt(self):
        data = {
            "symbol": "005930",
            "stock_analysis": _sample_stock_analysis(),
            "risk_assessment": _sample_risk_assessment(),
        }
        prompt = trade_decision.build_user_prompt(data)
        assert "005930" in prompt
        assert len(prompt) > 0


# ── 7. 에러 처리 테스트 ─────────────────────────────────────

class TestErrorHandling:
    """에러 처리 테스트."""

    @pytest.mark.asyncio
    async def test_router_error_propagates(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """router 에러 → 전파."""
        from src.core.exceptions import ProviderError

        mock_router.route_structured.side_effect = ProviderError("API timeout")

        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)

        with pytest.raises(ProviderError, match="API timeout"):
            await agent.analyze({}, session_id=session_id)

    @pytest.mark.asyncio
    async def test_tool_error_continues_analysis(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """도구 실행 에러 → 빈 데이터로 대체, 분석 계속."""
        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        # 첫 번째 도구 에러, 두 번째 정상
        mock_tool_registry.execute = AsyncMock(side_effect=[
            Exception("DART API down"),
            {"close": 78000},
        ])

        agent = MarketAnalyst(mock_router, mock_recorder, mock_tool_registry)
        result, did = await agent.analyze({}, session_id=session_id)

        # 에러에도 불구하고 분석 완료
        assert isinstance(result, MarketCondition)
        mock_router.route_structured.assert_called_once()


# ── 7. 투자 철학 프롬프트 주입 테스트 ─────────────────────────

class _ConcreteAgent(BaseAgent):
    """테스트용 구체 에이전트."""

    @property
    def agent_type(self):
        return AgentType.MARKET_ANALYST

    @property
    def output_schema(self):
        return MarketCondition

    @property
    def tool_modules(self):
        return []

    @property
    def decision_stage(self):
        return DecisionStage.MARKET_ANALYSIS

    def _build_messages(self, data):
        return [
            LLMMessage(role=MessageRole.SYSTEM, content="You are an analyst."),
            LLMMessage(role=MessageRole.USER, content="Analyze the market."),
        ]


class TestInvestmentPromptInjection:
    """_inject_investment_prompt() 및 analyze() 투자 철학 주입 테스트."""

    def test_inject_appends_to_system_message(self, mock_router, mock_recorder, mock_tool_registry):
        """system 메시지에 투자 철학 블록이 append된다."""
        agent = _ConcreteAgent(mock_router, mock_recorder, mock_tool_registry)
        messages = [
            LLMMessage(role=MessageRole.SYSTEM, content="Base prompt."),
            LLMMessage(role=MessageRole.USER, content="Hello"),
        ]

        result = agent._inject_investment_prompt(messages, "가치투자 원칙으로 운용")

        assert "## 투자 철학 (이 계좌의 운용 방침)" in result[0].content
        assert "가치투자 원칙으로 운용" in result[0].content
        assert result[0].content.startswith("Base prompt.")
        # user 메시지는 변경 없음
        assert result[1].content == "Hello"

    def test_inject_only_first_system_message(self, mock_router, mock_recorder, mock_tool_registry):
        """system 메시지가 2개일 때 첫 번째만 주입."""
        agent = _ConcreteAgent(mock_router, mock_recorder, mock_tool_registry)
        messages = [
            LLMMessage(role=MessageRole.SYSTEM, content="First system."),
            LLMMessage(role=MessageRole.USER, content="Hello"),
            LLMMessage(role=MessageRole.SYSTEM, content="Second system."),
        ]

        result = agent._inject_investment_prompt(messages, "모멘텀 투자")

        assert "모멘텀 투자" in result[0].content
        assert result[2].content == "Second system."  # 변경 없음

    def test_inject_no_system_message(self, mock_router, mock_recorder, mock_tool_registry):
        """system 메시지 없으면 에러 없이 원본 반환."""
        agent = _ConcreteAgent(mock_router, mock_recorder, mock_tool_registry)
        messages = [
            LLMMessage(role=MessageRole.USER, content="Hello"),
        ]

        result = agent._inject_investment_prompt(messages, "가치투자")

        assert len(result) == 1
        assert result[0].content == "Hello"

    @pytest.mark.asyncio
    async def test_analyze_with_investment_prompt(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """analyze(investment_prompt=...) → LLM에 주입된 메시지 전달."""
        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        agent = _ConcreteAgent(mock_router, mock_recorder, mock_tool_registry)
        await agent.analyze({}, session_id=session_id, investment_prompt="배당주 중심 안정적 투자")

        # router에 전달된 messages 확인
        call_kwargs = mock_router.route_structured.call_args.kwargs
        messages = call_kwargs["messages"]
        system_msg = next(m for m in messages if m.role == MessageRole.SYSTEM)
        assert "배당주 중심 안정적 투자" in system_msg.content
        assert "## 투자 철학" in system_msg.content

    @pytest.mark.asyncio
    async def test_analyze_without_investment_prompt(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """analyze(investment_prompt=None) → 기존 동작 동일."""
        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        agent = _ConcreteAgent(mock_router, mock_recorder, mock_tool_registry)
        await agent.analyze({}, session_id=session_id, investment_prompt=None)

        call_kwargs = mock_router.route_structured.call_args.kwargs
        messages = call_kwargs["messages"]
        system_msg = next(m for m in messages if m.role == MessageRole.SYSTEM)
        assert "투자 철학" not in system_msg.content
        assert system_msg.content == "You are an analyst."

    @pytest.mark.asyncio
    async def test_analyze_passes_account_id_to_recorder(self, mock_router, mock_recorder, mock_tool_registry, session_id):
        """analyze(account_id=...) → recorder.record()에 account_id 전달."""
        mc = _sample_market_condition()
        mock_router.route_structured.return_value = (mc, _sample_routing_result())

        agent = _ConcreteAgent(mock_router, mock_recorder, mock_tool_registry)
        await agent.analyze({}, session_id=session_id, account_id="acct-aggressive")

        record_kwargs = mock_recorder.record.call_args.kwargs
        assert record_kwargs["account_id"] == "acct-aggressive"
