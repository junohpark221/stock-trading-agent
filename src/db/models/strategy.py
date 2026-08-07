"""Phase 4 strategy ORM models: PositionRecord, PortfolioSnapshot, AgentMemory."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class PositionRecord(TimestampMixin, Base):
    """포지션 추적 — 진입부터 청산까지의 전체 라이프사이클을 기록.

    symbol+status 복합 인덱스로 활성 포지션 조회를 최적화하고,
    entry_session_id/exit_session_id로 decision_log와 연결한다.
    """

    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_symbol_status", "symbol", "status"),
        Index("ix_positions_strategy_status", "strategy_type", "status"),
        Index("ix_positions_entry_date", "entry_date"),
        Index("ix_positions_account_id", "account_id"),
        # F-14: 진입 트리거 태그(text[]) 멤버십 조회(= ANY)용 GIN 인덱스.
        Index("ix_positions_entry_trigger", "entry_trigger", postgresql_using="gin"),
        # PRJ-04 §2: 계좌·종목당 open 포지션은 1행(MTS식 병합). 추가매수는 새 행이
        # 아니라 기존 행 UPDATE로 처리되며, 이 부분 유니크 인덱스가 불변식을 강제한다.
        Index(
            "uq_positions_account_symbol_open",
            "account_id",
            "symbol",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    stop_loss_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    take_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    trailing_stop_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    highest_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    max_holding_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open")
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)

    entry_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    exit_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # 진입 분석 스냅샷 (메모리 학습용) — 진입 시 LLM 분석 요약(action/confidence/key_factors).
    # 체결 시 Order로부터 복사되며, 청산 후 record_trade_outcome이 교훈 생성에 사용한다.
    entry_analysis_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # F-14: 진입 트리거 태그 (스윙 기술 셋업: RSI과매도반전/MACD골든크로스/BB하단반등).
    # 라이브 결정 시점에 계산해 스냅샷 배관으로 전달, 포지션 생성 시 이 전용 컬럼으로 승격.
    # 관측/성과귀인용 주석이며 매매 게이트가 아니다. 청산 시 realized_pnl과 조인해 성과 산출.
    entry_trigger: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)


class PortfolioSnapshot(TimestampMixin, Base):
    """일별 포트폴리오 스냅샷 — 자산 현황, 수익률, 드로다운을 일 단위로 기록.

    snapshot_date 유니크 제약으로 하루에 하나의 스냅샷만 존재한다.
    sector_allocations는 JSONB로 섹터별 비중(%)을 저장한다.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_date", name="uq_portfolio_snapshots_account_date"),
        Index("ix_portfolio_snapshots_date", "snapshot_date"),
        Index("ix_portfolio_snapshots_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    invested: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    realized_pnl_daily: Mapped[Decimal] = mapped_column(
        Numeric(15, 2), nullable=False, default=Decimal("0")
    )
    peak_value: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    positions_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sector_allocations: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    trade_count_daily: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentMemory(TimestampMixin, Base):
    """에이전트 학습 메모리 — 에이전트가 과거 경험에서 학습한 패턴/교훈을 저장.

    agent_type+is_active 복합 인덱스로 활성 메모리 조회를 최적화하고,
    expires_at으로 시간 제한 메모리를 관리한다.
    symbol이 None이면 범용 메모리, 값이 있으면 종목별 메모리이다.
    """

    __tablename__ = "agent_memory"
    __table_args__ = (
        Index("ix_agent_memory_agent_active", "agent_type", "is_active"),
        Index("ix_agent_memory_symbol_active", "symbol", "is_active"),
        Index("ix_agent_memory_expires", "expires_at"),
        Index("ix_agent_memory_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    agent_type: Mapped[str] = mapped_column(String(50), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    relevance_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("1.0")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
