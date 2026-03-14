"""Domain enums for the stock trading agent.

All enums use Python 3.12 StrEnum for automatic string serialization
and direct comparison with string values (e.g., OrderStatus.PENDING == "pending").
"""

from enum import StrEnum


class Environment(StrEnum):
    """Application environment."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class MarketType(StrEnum):
    """Stock market type (maps to stock_master.market_type)."""

    KOSPI = "kospi"
    KOSDAQ = "kosdaq"


class OrderSide(StrEnum):
    """Order direction."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Order type."""

    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    """Order lifecycle status."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class PositionStatus(StrEnum):
    """Position status."""

    OPEN = "open"
    CLOSED = "closed"
    PARTIALLY_CLOSED = "partially_closed"


class SignalAction(StrEnum):
    """Analysis signal action."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class DecisionStage(StrEnum):
    """Decision pipeline stage (maps to decision_log.stage)."""

    MARKET_ANALYSIS = "market_analysis"
    STOCK_ANALYSIS = "stock_analysis"
    RISK_CHECK = "risk_check"
    TRADE_DECISION = "trade_decision"
    APPROVAL = "approval"
    EXECUTION = "execution"
    EXIT = "exit"


class AgentType(StrEnum):
    """AI agent type (maps to agent_model_config.agent_type)."""

    MARKET_ANALYST = "market_analyst"
    STOCK_ANALYST = "stock_analyst"
    RISK_MANAGER = "risk_manager"
    TRADER = "trader"
    SENTIMENT_ANALYZER = "sentiment_analyzer"
    REPORT_GENERATOR = "report_generator"


class LLMProviderType(StrEnum):
    """LLM provider (maps to decision_log.llm_provider)."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"


class ApprovalStatus(StrEnum):
    """Trade approval status."""

    AUTO_APPROVED = "auto_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class RoutingMode(StrEnum):
    """LLM routing mode (maps to agent_model_config.routing_mode)."""

    FIXED = "fixed"
    ESCALATION = "escalation"


class DecisionAction(StrEnum):
    """Decision action type (maps to decision_log.decision)."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    APPROVE = "approve"
    REJECT = "reject"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class DecisionOutcome(StrEnum):
    """Decision outcome (maps to decision_log.outcome)."""

    PROFIT = "profit"
    LOSS = "loss"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ReportType(StrEnum):
    """DART 재무제표 보고서 종류."""

    ANNUAL = "annual"
    SEMI_ANNUAL = "semi_annual"
    QUARTERLY = "quarterly"


class SentimentLabel(StrEnum):
    """뉴스 감성 분류 (Phase 3에서 사용, 미리 정의)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class SentimentMethod(StrEnum):
    """감성분석 방법 (하이브리드: 키워드 1차 + LLM 심층)."""

    KEYWORD = "keyword"
    LLM = "llm"


class MessageRole(StrEnum):
    """LLM 메시지 역할."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class DataSourceType(StrEnum):
    """외부 데이터 소스 식별자."""

    KIS = "kis"
    DART = "dart"
    ECOS = "ecos"
    FRED = "fred"
    NAVER = "naver"
    PYKRX = "pykrx"
