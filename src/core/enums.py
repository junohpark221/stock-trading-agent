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
    """Position status.

    PRJ-04 §10: ``partially_closed`` 는 폐기했다 — 부분 매도는 별도 상태가 아니라
    ``open`` + 수량 감소로 표현한다(DB `positions.status` 도 open/closed 2값).
    """

    OPEN = "open"
    CLOSED = "closed"


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
    BATCH_ALLOCATION = "batch_allocation"
    APPROVAL = "approval"
    EXECUTION = "execution"
    EXIT = "exit"
    HYPOTHESIS_ALERT = "hypothesis_alert"  # F-11: 가설훼손 경보(경보형 1차, 자동청산 아님)


class AgentType(StrEnum):
    """AI agent type (maps to agent_model_config.agent_type)."""

    MARKET_ANALYST = "market_analyst"
    STOCK_ANALYST = "stock_analyst"
    RISK_MANAGER = "risk_manager"
    TRADER = "trader"
    SENTIMENT_ANALYZER = "sentiment_analyzer"
    REPORT_GENERATOR = "report_generator"
    WEB_VERIFIER = "web_verifier"
    THESIS_MONITOR = "thesis_monitor"  # F-11: 가설훼손 판단 에이전트(경보형)


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


# ---------------------------------------------------------------------------
# Phase 4: Strategy Engine + Risk Management
# ---------------------------------------------------------------------------


class StrategyType(StrEnum):
    """매매 전략 유형."""

    POSITION = "position"  # 주~월 단위 (중장기)
    SWING = "swing"        # 일~주 단위 (단기~중기)
    MANUAL = "manual"      # 브로커 직접 체결 등 전략 외 보유 종목


class ExitReason(StrEnum):
    """포지션 청산 사유."""

    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    PARTIAL_TAKE_PROFIT = "partial_take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_BASED = "time_based"
    FUNDAMENTAL = "fundamental"
    LLM_SIGNAL = "llm_signal"
    DRAWDOWN = "drawdown"
    MANUAL = "manual"
    EXPIRED = "expired"
    RECONCILED = "reconciled"


# ---------------------------------------------------------------------------
# Phase 5: Order Execution + User Approval
# ---------------------------------------------------------------------------


class WebVerifyResult(StrEnum):
    """LLM Web Search 최종 검증 결과."""

    SAFE = "safe"           # 특이사항 없음 → 주문 진행
    WARNING = "warning"     # 주의 필요 → 승인 메시지에 경고 포함
    BLOCKED = "blocked"     # 위험 감지 → 주문 차단


# ---------------------------------------------------------------------------
# Phase 6: Scheduler + Report + Monitoring
# ---------------------------------------------------------------------------


class PerformanceReportType(StrEnum):
    """성과 리포트 유형."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    LLM_COST = "llm_cost"


class JobStatus(StrEnum):
    """스케줄러 작업 실행 상태."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class MonitoringAlertType(StrEnum):
    """트레이딩 모니터링 경고 유형."""

    STOP_LOSS_PROXIMITY = "stop_loss_proximity"
    SECTOR_CONCENTRATION = "sector_concentration"
    LLM_BUDGET = "llm_budget"
    PORTFOLIO_DRAWDOWN = "portfolio_drawdown"


# ---------------------------------------------------------------------------
# Phase 7: Backtesting
# ---------------------------------------------------------------------------


class BacktestStatus(StrEnum):
    """백테스트 실행 상태."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BacktestMode(StrEnum):
    """백테스트 시그널 생성 모드."""

    TECHNICAL = "technical"      # Mode 1: 순수 기술 지표
    LLM_REPLAY = "llm_replay"   # Mode 2: decision_log 재생
    LLM_LIVE = "llm_live"       # Mode 3: 실제 LLM 호출 (향후)


# ---------------------------------------------------------------------------
# Phase 8: Multi-Account
# ---------------------------------------------------------------------------


class AccountStatus(StrEnum):
    """계정 상태."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class RiskTolerance(StrEnum):
    """계좌별 LLM 리스크 허용 수준."""

    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"
