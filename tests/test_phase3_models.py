"""Phase 3 Step 1: enum, Pydantic model, config 테스트."""

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from src.core.enums import (
    AgentType,
    DecisionAction,
    LLMProviderType,
    MessageRole,
    OrderType,
    RoutingMode,
    SentimentLabel,
    SentimentMethod,
)
from src.core.models import (
    AgentModelConfig,
    LLMMessage,
    LLMResponse,
    MarketCondition,
    PipelineResult,
    RiskAssessment,
    SentimentResult,
    StockAnalysis,
    Tool,
    ToolCall,
    ToolParameter,
    TradeDecision,
)

# ---------------------------------------------------------------------------
# A. Enum 테스트
# ---------------------------------------------------------------------------

class TestSentimentMethod:
    def test_values(self):
        assert SentimentMethod.KEYWORD == "keyword"
        assert SentimentMethod.LLM == "llm"
        assert len(SentimentMethod) == 2


class TestMessageRole:
    def test_values(self):
        assert MessageRole.SYSTEM == "system"
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"
        assert MessageRole.TOOL == "tool"
        assert len(MessageRole) == 4


# ---------------------------------------------------------------------------
# B. 모델 직렬화/역직렬화 (round-trip)
# ---------------------------------------------------------------------------

class TestToolParameter:
    def test_round_trip(self):
        tp = ToolParameter(name="symbol", type="string", description="종목코드")
        data = tp.model_dump()
        restored = ToolParameter.model_validate(data)
        assert restored.name == "symbol"
        assert restored.required is True
        assert restored.enum is None


class TestTool:
    def test_round_trip(self):
        tool = Tool(
            name="get_price",
            description="현재가 조회",
            parameters=[ToolParameter(name="symbol", type="string", description="종목코드")],
        )
        data = tool.model_dump()
        restored = Tool.model_validate(data)
        assert restored.name == "get_price"
        assert len(restored.parameters) == 1

    def test_defaults(self):
        tool = Tool(name="noop", description="no-op")
        assert tool.parameters == []


class TestToolCall:
    def test_round_trip(self):
        tc = ToolCall(id="call_1", name="get_price", arguments={"symbol": "005930"})
        data = tc.model_dump()
        restored = ToolCall.model_validate(data)
        assert restored.arguments["symbol"] == "005930"

    def test_defaults(self):
        tc = ToolCall(id="call_2", name="noop")
        assert tc.arguments == {}


class TestLLMMessage:
    def test_round_trip(self):
        msg = LLMMessage(role=MessageRole.USER, content="분석해줘")
        data = msg.model_dump()
        restored = LLMMessage.model_validate(data)
        assert restored.role == MessageRole.USER
        assert restored.name is None
        assert restored.tool_call_id is None

    def test_tool_result(self):
        msg = LLMMessage(
            role=MessageRole.TOOL,
            content='{"price": 70000}',
            name="get_price",
            tool_call_id="call_1",
        )
        assert msg.role == MessageRole.TOOL
        assert msg.name == "get_price"


class TestLLMResponse:
    def test_round_trip(self):
        resp = LLMResponse(
            content="매수 추천",
            model="gpt-4o",
            provider=LLMProviderType.OPENAI,
            tokens_in=100,
            tokens_out=50,
            cost_usd=Decimal("0.0025"),
        )
        data = resp.model_dump()
        restored = LLMResponse.model_validate(data)
        assert restored.provider == LLMProviderType.OPENAI
        assert restored.finish_reason == "stop"
        assert restored.tool_calls == []
        assert restored.latency_ms == 0

    def test_with_tool_calls(self):
        resp = LLMResponse(
            content="",
            model="gpt-4o",
            provider=LLMProviderType.OPENAI,
            tokens_in=80,
            tokens_out=30,
            cost_usd=Decimal("0.002"),
            tool_calls=[ToolCall(id="call_1", name="get_price", arguments={"symbol": "005930"})],
            finish_reason="tool_calls",
        )
        assert len(resp.tool_calls) == 1
        assert resp.finish_reason == "tool_calls"


class TestAgentModelConfig:
    def test_round_trip(self):
        cfg = AgentModelConfig(
            agent_type=AgentType.MARKET_ANALYST,
            routing_mode=RoutingMode.ESCALATION,
            primary_model="openai/gpt-4o",
            escalation_model="openai/o3",
            confidence_threshold=Decimal("0.7"),
        )
        data = cfg.model_dump()
        restored = AgentModelConfig.model_validate(data)
        assert restored.agent_type == AgentType.MARKET_ANALYST
        assert restored.is_active is True
        assert restored.updated_by == "system"


class TestSentimentResult:
    def test_round_trip(self):
        sr = SentimentResult(
            symbol="005930",
            overall_score=Decimal("0.65"),
            overall_label=SentimentLabel.POSITIVE,
            method=SentimentMethod.KEYWORD,
            positive_count=10,
            negative_count=2,
            total_articles=15,
        )
        data = sr.model_dump()
        restored = SentimentResult.model_validate(data)
        assert restored.overall_score == Decimal("0.65")
        assert restored.needs_llm_analysis is False
        assert restored.key_topics == []


class TestMarketCondition:
    def test_round_trip(self):
        mc = MarketCondition(
            condition="bullish",
            confidence=Decimal("0.8"),
            kospi_trend="up",
            kosdaq_trend="sideways",
            market_risk_level="low",
        )
        data = mc.model_dump()
        restored = MarketCondition.model_validate(data)
        assert restored.recommended_exposure == Decimal("0.5")
        assert restored.sector_outlook == {}


class TestStockAnalysis:
    def test_round_trip(self):
        sa = StockAnalysis(
            symbol="005930",
            action=DecisionAction.BUY,
            confidence=Decimal("0.85"),
            target_price=Decimal("80000"),
            stop_loss_price=Decimal("65000"),
            current_price=Decimal("70000"),
        )
        data = sa.model_dump()
        restored = StockAnalysis.model_validate(data)
        assert restored.technical_score == Decimal(0)
        assert restored.sentiment is None


class TestRiskAssessment:
    def test_round_trip(self):
        ra = RiskAssessment(
            symbol="005930",
            approved=True,
            risk_level="medium",
            confidence=Decimal("0.75"),
            recommended_quantity=10,
        )
        data = ra.model_dump()
        restored = RiskAssessment.model_validate(data)
        assert restored.portfolio_concentration_ok is True
        assert restored.risk_factors == []


class TestTradeDecision:
    def test_round_trip(self):
        td = TradeDecision(
            symbol="005930",
            action=DecisionAction.BUY,
            confidence=Decimal("0.9"),
            quantity=10,
            price=Decimal("70000"),
        )
        data = td.model_dump()
        restored = TradeDecision.model_validate(data)
        assert restored.order_type == OrderType.LIMIT
        assert restored.requires_approval is True


class TestPipelineResult:
    def test_round_trip(self):
        now = datetime.now()
        pr = PipelineResult(
            session_id=uuid4(),
            started_at=now,
            symbols_requested=["005930", "000660"],
        )
        data = pr.model_dump()
        restored = PipelineResult.model_validate(data)
        assert restored.success is True
        assert restored.total_llm_cost_usd == Decimal(0)
        assert restored.stock_analyses == []


# ---------------------------------------------------------------------------
# C. 모델 중첩 테스트
# ---------------------------------------------------------------------------

class TestNestedModels:
    def test_stock_analysis_with_sentiment(self):
        sentiment = SentimentResult(
            symbol="005930",
            overall_score=Decimal("0.7"),
            overall_label=SentimentLabel.POSITIVE,
            method=SentimentMethod.LLM,
            key_topics=["반도체", "AI"],
        )
        sa = StockAnalysis(
            symbol="005930",
            action=DecisionAction.BUY,
            confidence=Decimal("0.85"),
            sentiment=sentiment,
            target_price=Decimal("80000"),
            stop_loss_price=Decimal("65000"),
        )
        data = sa.model_dump()
        restored = StockAnalysis.model_validate(data)
        assert restored.sentiment is not None
        assert restored.sentiment.method == SentimentMethod.LLM
        assert restored.sentiment.key_topics == ["반도체", "AI"]

    def test_pipeline_result_full(self):
        now = datetime.now()
        pr = PipelineResult(
            session_id=uuid4(),
            started_at=now,
            completed_at=now,
            market_condition=MarketCondition(
                condition="mixed",
                confidence=Decimal("0.6"),
                kospi_trend="down",
                kosdaq_trend="up",
                market_risk_level="medium",
            ),
            stock_analyses=[
                StockAnalysis(
                    symbol="005930",
                    action=DecisionAction.HOLD,
                    confidence=Decimal("0.5"),
                    target_price=None,
                    stop_loss_price=None,
                ),
            ],
            risk_assessments=[
                RiskAssessment(
                    symbol="005930",
                    approved=False,
                    risk_level="high",
                    confidence=Decimal("0.8"),
                ),
            ],
            trade_decisions=[
                TradeDecision(
                    symbol="005930",
                    action=DecisionAction.HOLD,
                    confidence=Decimal("0.5"),
                ),
            ],
            total_llm_cost_usd=Decimal("0.15"),
            total_llm_calls=5,
        )
        data = pr.model_dump()
        restored = PipelineResult.model_validate(data)
        assert len(restored.stock_analyses) == 1
        assert len(restored.risk_assessments) == 1
        assert len(restored.trade_decisions) == 1
        assert restored.market_condition is not None
        assert restored.total_llm_calls == 5

    def test_llm_response_with_tool_calls(self):
        resp = LLMResponse(
            content="",
            model="gpt-4o",
            provider=LLMProviderType.OPENAI,
            tokens_in=100,
            tokens_out=40,
            cost_usd=Decimal("0.003"),
            tool_calls=[
                ToolCall(id="call_1", name="get_price", arguments={"symbol": "005930"}),
                ToolCall(id="call_2", name="get_financials", arguments={"symbol": "005930"}),
            ],
            finish_reason="tool_calls",
        )
        data = resp.model_dump()
        restored = LLMResponse.model_validate(data)
        assert len(restored.tool_calls) == 2
        assert restored.tool_calls[0].name == "get_price"
        assert restored.tool_calls[1].name == "get_financials"


# ---------------------------------------------------------------------------
# D. Settings 기본값 테스트
# ---------------------------------------------------------------------------

class TestSettingsDefaults:
    def test_phase3_config_defaults(self, _clear_settings_cache):
        from conftest import make_settings

        settings = make_settings()
        assert Decimal("0.60") == settings.LLM_ESCALATION_CONFIDENCE_THRESHOLD
        assert settings.LLM_RESPONSE_CACHE_TTL == 1800
        assert settings.LLM_CONFIG_CACHE_TTL == 300
        assert settings.LLM_BUDGET_WARNING_PCT == 80
        assert settings.LLM_MAX_RETRIES == 3
        assert settings.LLM_REQUEST_TIMEOUT == 120


# ---------------------------------------------------------------------------
# E. Decimal 정밀도 테스트
# ---------------------------------------------------------------------------

class TestDecimalPrecision:
    def test_cost_precision(self):
        resp = LLMResponse(
            content="ok",
            model="gpt-4o",
            provider=LLMProviderType.OPENAI,
            tokens_in=1,
            tokens_out=1,
            cost_usd=Decimal("0.00000123"),
        )
        assert resp.cost_usd == Decimal("0.00000123")

    def test_confidence_precision(self):
        sa = StockAnalysis(
            symbol="005930",
            action=DecisionAction.BUY,
            confidence=Decimal("0.123456789"),
            target_price=None,
            stop_loss_price=None,
        )
        assert sa.confidence == Decimal("0.123456789")

    def test_sentiment_score_boundaries(self):
        for score in [Decimal("-1.0"), Decimal("0.0"), Decimal("1.0")]:
            sr = SentimentResult(
                symbol="005930",
                overall_score=score,
                overall_label=SentimentLabel.NEUTRAL,
                method=SentimentMethod.KEYWORD,
            )
            assert sr.overall_score == score
