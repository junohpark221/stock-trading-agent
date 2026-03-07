"""Pydantic domain models for the stock trading agent.

All financial fields use ``Decimal`` (DESIGN.md 원칙 #8).
Every model has ``from_attributes=True`` for ORM compatibility.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import (
    AgentType,
    DataSourceType,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    ReportType,
    SentimentLabel,
    SignalAction,
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
