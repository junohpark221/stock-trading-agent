"""Backtest DB models — run history and virtual trades.

Phase 7: 백테스트 실행 기록 + 가상 거래 내역.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class BacktestRun(TimestampMixin, Base):
    """백테스트 실행 기록.

    각 백테스트 실행마다 1행 생성. run_id UUID로 외부 식별.
    result_metrics JSONB에 PerformanceMetrics 직렬화 저장.
    """

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_backtest_runs_status", "status"),
        Index("ix_backtest_runs_strategy_type", "strategy_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False,
    )
    strategy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="technical")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    slippage_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    commission_buy_pct: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=Decimal("0.015"),
    )
    commission_sell_pct: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=Decimal("0.195"),
    )  # 0.015% 수수료 + 0.18% 거래세
    symbols: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    parameters: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    result_metrics: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class BacktestTrade(TimestampMixin, Base):
    """백테스트 가상 거래 내역.

    BacktestRun에 종속. run_id FK로 연결.
    side: 'buy' | 'sell'. pnl은 청산 시에만 기록.
    """

    __tablename__ = "backtest_trades"
    __table_args__ = (
        Index("ix_backtest_trades_run_id", "run_id"),
        Index("ix_backtest_trades_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("backtest_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    commission: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=Decimal("0"),
    )
    slippage: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=Decimal("0"),
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
