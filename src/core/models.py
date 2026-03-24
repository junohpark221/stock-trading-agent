"""Pydantic domain models for the stock trading agent.

All financial fields use ``Decimal`` (DESIGN.md 원칙 #8).
Every model has ``from_attributes=True`` for ORM compatibility.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import (
    AgentType,
    ApprovalStatus,
    DataSourceType,
    DecisionAction,
    ExitReason,
    LLMProviderType,
    MarketType,
    MessageRole,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    ReportType,
    RoutingMode,
    SentimentLabel,
    SentimentMethod,
    SignalAction,
    WebVerifyResult,
)


class HealthStatus(BaseModel):
    """/health 응답 모델."""

    model_config = ConfigDict(from_attributes=True)

    status: str
    environment: str
    database: str
    redis: str
    timestamp: datetime


class StockInfo(BaseModel):
    """종목 기본 정보."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str
    market_type: MarketType
    sector: str = ""
    listed_shares: int = 0
    market_cap_krw: Decimal = Decimal(0)


class PriceInfo(BaseModel):
    """현재가 스냅샷 — BrokerInterface.get_price() 반환."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    current_price: Decimal
    previous_close: Decimal
    change_price: Decimal = Decimal(0)
    change_percent: Decimal = Decimal(0)
    high: Decimal = Decimal(0)
    low: Decimal = Decimal(0)
    volume: int = 0
    timestamp: datetime


class OHLCV(BaseModel):
    """봉 데이터 — BrokerInterface.get_daily_ohlcv() 반환."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    value: Decimal = Decimal(0)


class Signal(BaseModel):
    """매매 시그널 — generate_signals() 반환."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    action: SignalAction
    confidence: Decimal
    target_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    quantity: int = 0
    position_value_krw: Decimal = Decimal(0)
    reasoning: str
    source_agent: AgentType
    timestamp: datetime


class OrderRequest(BaseModel):
    """주문 요청 — BrokerInterface.place_order() 파라미터."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(gt=0)
    price: Decimal | None = None
    reason: str = ""


class OrderResult(BaseModel):
    """주문 결과 — BrokerInterface.place_order() 반환."""

    model_config = ConfigDict(from_attributes=True)

    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Decimal
    status: OrderStatus
    filled_quantity: int = 0
    filled_price: Decimal | None = None
    commission: Decimal = Decimal(0)
    timestamp: datetime


class Position(BaseModel):
    """보유 포지션 — BrokerInterface.get_positions() 반환."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    quantity: int
    average_cost: Decimal
    current_price: Decimal
    market_value: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    unrealized_pnl_pct: Decimal = Decimal(0)
    status: PositionStatus
    entry_date: datetime


class AccountBalance(BaseModel):
    """계좌 잔고 — BrokerInterface.get_balance() 반환."""

    model_config = ConfigDict(from_attributes=True)

    total_assets: Decimal
    cash: Decimal
    invested: Decimal
    unrealized_pnl: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    daily_pnl: Decimal = Decimal(0)
    daily_pnl_pct: Decimal = Decimal(0)
    positions_count: int = 0
    timestamp: datetime


# ---------------------------------------------------------------------------
# Phase 2: Analysis domain models
# ---------------------------------------------------------------------------


class FinancialStatementInfo(BaseModel):
    """DART 재무제표 응답."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    corp_code: str
    report_type: ReportType
    fiscal_year: int
    fiscal_quarter: int | None = None
    revenue: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    total_assets: Decimal | None = None
    total_equity: Decimal | None = None
    total_liabilities: Decimal | None = None
    per: Decimal | None = None
    pbr: Decimal | None = None
    roe: Decimal | None = None
    eps: Decimal | None = None
    bps: Decimal | None = None


class EconomicIndicatorInfo(BaseModel):
    """경제지표 응답."""

    model_config = ConfigDict(from_attributes=True)

    source: DataSourceType
    indicator_code: str
    indicator_name: str
    date: date
    value: Decimal
    unit: str | None = None


class NewsArticleInfo(BaseModel):
    """뉴스 기사 응답."""

    model_config = ConfigDict(from_attributes=True)

    source: DataSourceType
    symbol: str | None = None
    title: str
    description: str | None = None
    link: str
    published_at: datetime
    sentiment_score: Decimal | None = None
    sentiment_label: SentimentLabel | None = None


class DisclosureInfo(BaseModel):
    """DART 공시 응답."""

    model_config = ConfigDict(from_attributes=True)

    corp_code: str
    symbol: str
    report_name: str
    receipt_no: str
    receipt_date: date
    filer_name: str | None = None


class TechnicalIndicators(BaseModel):
    """기술지표 계산 결과 (DB 미저장, on-the-fly 계산)."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str

    # 이동평균
    sma_5: list[float | None] = Field(default_factory=list)
    sma_20: list[float | None] = Field(default_factory=list)
    sma_60: list[float | None] = Field(default_factory=list)
    sma_120: list[float | None] = Field(default_factory=list)
    ema_12: list[float | None] = Field(default_factory=list)
    ema_26: list[float | None] = Field(default_factory=list)

    # MACD
    macd_line: list[float | None] = Field(default_factory=list)
    macd_signal: list[float | None] = Field(default_factory=list)
    macd_histogram: list[float | None] = Field(default_factory=list)

    # 오실레이터
    rsi_14: list[float | None] = Field(default_factory=list)
    stoch_k: list[float | None] = Field(default_factory=list)
    stoch_d: list[float | None] = Field(default_factory=list)

    # 볼린저 밴드
    bb_upper: list[float | None] = Field(default_factory=list)
    bb_middle: list[float | None] = Field(default_factory=list)
    bb_lower: list[float | None] = Field(default_factory=list)

    # 거래량
    volume_sma_20: list[float | None] = Field(default_factory=list)

    # 최신값 스냅샷
    latest_rsi: float | None = None
    latest_macd_histogram: float | None = None
    latest_stoch_k: float | None = None
    latest_bb_position: float | None = None


class FundamentalScore(BaseModel):
    """펀더멘털 종합 점수."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    overall_score: Decimal
    valuation_score: Decimal
    growth_score: Decimal
    profitability_score: Decimal
    reasoning: str


class PatternSignal(BaseModel):
    """차트 패턴 감지 시그널."""

    model_config = ConfigDict(from_attributes=True)

    pattern_name: str
    signal_type: str
    confidence: Decimal
    description: str


# ---------------------------------------------------------------------------
# Phase 3: LLM Agent models
# ---------------------------------------------------------------------------


class ToolParameter(BaseModel):
    """LLM tool function 파라미터 정의."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    type: str
    description: str
    required: bool = True
    enum: list[str] | None = None


class Tool(BaseModel):
    """LLM function calling 도구 정의."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    parameters: list[ToolParameter] = []


class ToolCall(BaseModel):
    """LLM이 요청한 tool 호출."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    arguments: dict[str, object] = {}


class LLMMessage(BaseModel):
    """LLMProvider.chat() 입력."""

    model_config = ConfigDict(from_attributes=True)

    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class LLMResponse(BaseModel):
    """LLMProvider.chat() 반환."""

    model_config = ConfigDict(from_attributes=True)

    content: str
    model: str
    provider: LLMProviderType
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    tool_calls: list[ToolCall] = []
    finish_reason: str = "stop"
    latency_ms: int = 0


class AgentModelConfig(BaseModel):
    """에이전트별 모델 할당 (Admin API 응답/요청)."""

    model_config = ConfigDict(from_attributes=True)

    agent_type: AgentType
    routing_mode: RoutingMode
    primary_model: str
    escalation_model: str | None = None
    confidence_threshold: Decimal | None = None
    is_active: bool = True
    updated_by: str = "system"


class SentimentResult(BaseModel):
    """키워드/LLM 하이브리드 감성분석 결과."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    overall_score: Decimal
    overall_label: SentimentLabel
    method: SentimentMethod
    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0
    total_articles: int = 0
    key_topics: list[str] = []
    needs_llm_analysis: bool = False
    reasoning: str = ""


class SectorOutlook(BaseModel):
    """섹터별 전망 (OpenAI strict mode 호환)."""

    sector: str
    outlook: str


class MarketCondition(BaseModel):
    """Market Analyst 출력."""

    model_config = ConfigDict(from_attributes=True)

    condition: str
    confidence: Decimal
    kospi_trend: str
    kosdaq_trend: str
    market_risk_level: str
    key_factors: list[str] = []
    sector_outlook: list[SectorOutlook] = []
    macro_summary: str = ""
    recommended_exposure: Decimal = Decimal("0.5")
    reasoning: str = ""


class StockAnalysis(BaseModel):
    """Stock Analyst 출력."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str = ""
    action: DecisionAction
    confidence: Decimal
    technical_score: Decimal = Decimal(0)
    technical_summary: str = ""
    fundamental_score: Decimal = Decimal(0)
    fundamental_summary: str = ""
    sentiment: SentimentResult | None = None
    target_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    current_price: Decimal | None = None
    key_factors: list[str] = []
    risks: list[str] = []
    reasoning: str = ""


class RiskAssessment(BaseModel):
    """Risk Manager 출력."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    approved: bool
    risk_level: str
    confidence: Decimal
    recommended_quantity: int = 0
    recommended_position_size_krw: Decimal = Decimal(0)
    max_loss_krw: Decimal = Decimal(0)
    portfolio_concentration_ok: bool = True
    sector_exposure_ok: bool = True
    daily_loss_limit_ok: bool = True
    position_size_ok: bool = True
    volatility_ok: bool = True
    risk_factors: list[str] = []
    conditions: list[str] = []
    reasoning: str = ""


class TradeDecision(BaseModel):
    """Trader 출력."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    action: DecisionAction
    confidence: Decimal
    order_type: OrderType = OrderType.LIMIT
    quantity: int = 0
    price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    risk_reward_ratio: Decimal | None = None
    expected_return_pct: Decimal | None = None
    max_loss_pct: Decimal | None = None
    reasoning: str = ""
    requires_approval: bool = True


class PipelineResult(BaseModel):
    """PipelineOrchestrator 출력."""

    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    started_at: datetime
    completed_at: datetime | None = None
    market_condition: MarketCondition | None = None
    stock_analyses: list[StockAnalysis] = []
    risk_assessments: list[RiskAssessment] = []
    trade_decisions: list[TradeDecision] = []
    symbols_requested: list[str] = []
    symbols_analyzed: list[str] = []
    symbols_skipped: list[str] = []
    total_llm_cost_usd: Decimal = Decimal(0)
    total_llm_calls: int = 0
    errors: list[str] = []
    success: bool = True


# ---------------------------------------------------------------------------
# Phase 4: Strategy Engine + Risk Management
# ---------------------------------------------------------------------------


class ExitSignal(BaseModel):
    """청산 신호 — ExitMonitor → Strategy 반환."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    reason: ExitReason
    urgency: str  # "immediate" | "end_of_day" | "next_session"
    current_price: Decimal
    trigger_price: Decimal | None = None
    unrealized_pnl_pct: Decimal
    recommended_action: DecisionAction
    reasoning: str


class PortfolioState(BaseModel):
    """포트폴리오 현재 상태 스냅샷."""

    model_config = ConfigDict(from_attributes=True)

    total_value: Decimal
    cash: Decimal
    invested: Decimal
    unrealized_pnl: Decimal
    daily_pnl: Decimal
    daily_pnl_pct: Decimal
    drawdown_pct: Decimal              # 고점 대비 낙폭
    peak_value: Decimal                # 역대 최고 자산
    positions: list[Position]
    sector_allocations: dict[str, Decimal]  # sector → 비중(%)
    daily_trade_count: int
    timestamp: datetime


class RiskCheckResult(BaseModel):
    """알고리즘 리스크 체크 결과 — AlgoRiskManager 출력."""

    model_config = ConfigDict(from_attributes=True)

    passed: bool
    symbol: str
    violations: list[str]              # "MAX_POSITION_PCT", "DAILY_LOSS_LIMIT" 등
    warnings: list[str]
    adjusted_quantity: int             # 리스크 제약 반영 수량
    adjusted_amount_krw: Decimal       # 조정된 금액
    max_allowed_quantity: int
    reasoning: str


class PositionSizing(BaseModel):
    """포지션 사이징 계산 결과 — PositionSizer 출력."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal | None = None
    risk_per_share: Decimal            # entry - stop_loss
    quantity: int
    position_value_krw: Decimal
    risk_amount_krw: Decimal           # 최대 손실액
    risk_pct_of_portfolio: Decimal
    position_pct_of_portfolio: Decimal
    risk_reward_ratio: Decimal | None = None


# ---------------------------------------------------------------------------
# Phase 5: Order Execution + User Approval
# ---------------------------------------------------------------------------


class WebVerification(BaseModel):
    """LLM Web Search 최종 검증 결과."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    symbol: str
    result: WebVerifyResult              # safe / warning / blocked
    summary: str                          # 검증 요약 (한국어)
    issues_found: list[str] = []          # 감지된 이슈 목록
    news_checked: int = 0                 # 확인한 뉴스 건수
    llm_cost_usd: Decimal = Decimal(0)
    reasoning: str = ""


class ApprovalRequestModel(BaseModel):
    """텔레그램 승인 요청 정보."""

    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    order_id: int                          # orders 테이블 PK
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    position_value_krw: Decimal
    portfolio_pct: Decimal                 # 포트폴리오 비중 (%)
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    risk_reward_ratio: Decimal | None = None
    analysis_summary: str = ""
    web_verify_summary: str = ""
    session_id: UUID | None = None
    status: ApprovalStatus = ApprovalStatus.AUTO_APPROVED
    requested_at: datetime
    responded_at: datetime | None = None
    modified_quantity: int | None = None    # 수정된 수량 (수정 승인 시)
    response_reason: str = ""


class OrderRecord(BaseModel):
    """주문 기록 — DB orders 테이블 대응."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Decimal
    status: OrderStatus
    approval_status: ApprovalStatus
    original_quantity: int
    modified_quantity: int | None = None
    session_id: UUID | None = None
    trade_decision_id: UUID | None = None
    broker_order_id: str | None = None
    filled_quantity: int = 0
    filled_price: Decimal | None = None
    commission: Decimal = Decimal(0)
    rejection_reason: str = ""
    web_verify_result: str | None = None
    created_at: datetime
    executed_at: datetime | None = None


class ExecutionRecord(BaseModel):
    """체결 기록 — DB executions 테이블 대응."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    broker_order_id: str
    fill_price: Decimal
    fill_quantity: int
    commission: Decimal
    executed_at: datetime


class ExecutionResult(BaseModel):
    """OrderExecutor.execute() 최종 결과."""

    model_config = ConfigDict(from_attributes=True)

    success: bool
    order_id: int | None = None
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    quantity: int
    fill_price: Decimal | None = None
    commission: Decimal = Decimal(0)
    approval_status: ApprovalStatus
    web_verify_result: WebVerifyResult | None = None
    position_id: int | None = None         # 생성/청산된 포지션 ID
    decision_ids: list[UUID] = []          # 기록된 decision_log ID들
    error: str = ""
