"""Pydantic domain models for the stock trading agent.

All financial fields use ``Decimal`` (DESIGN.md 원칙 #8).
Every model has ``from_attributes=True`` for ORM compatibility.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import (
    AgentType,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
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
