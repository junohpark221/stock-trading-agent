"""Phase 5 execution ORM models: Order, Execution, ApprovalRequestDB."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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


class Order(TimestampMixin, Base):
    """주문 기록 — 시그널부터 체결/취소까지의 주문 라이프사이클.

    symbol+status 인덱스로 미체결 주문 조회 최적화.
    session_id, trade_decision_id로 decision_log 체인에 연결.
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_symbol_status", "symbol", "status"),
        Index("ix_orders_session_id", "session_id"),
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)  # OrderSide.value
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)  # OrderType.value
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # 승인 관련
    approval_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="auto_approved"
    )
    original_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    modified_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 연결 정보
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trade_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 체결 정보
    broker_order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    commission: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 메타
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    web_verify_result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    web_verify_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 진입 분석 스냅샷 (메모리 학습용) — 진입 시 LLM 분석 요약을 JSONB로 보관.
    # 체결 시 PositionRecord로 복사되어, 청산 후 record_trade_outcome이 참조한다.
    entry_analysis_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TradeDecisionQueue(TimestampMixin, Base):
    """진입 결정 큐 — 개장 전 결정 잡이 적재하고, 개장 후 실행 드레인 잡이 소비.

    "분석=발주" 결합을 끊기 위한 가변 실행 상태 테이블. decision_log(감사·불변)와
    별도다. 결정 잡(08:30)이 BUY 결정을 status=pending으로 적재하면, 실행 드레인
    잡이 당일가/갭 게이트를 통과한 건만 order_executor로 발주하고 executed/expired로
    전이한다. entry_analysis_snapshot은 메모리 학습용으로 결정 시점에 보관한다.
    """

    __tablename__ = "trade_decision_queue"
    __table_args__ = (
        Index("ix_trade_decision_queue_account_status", "account_id", "status"),
        Index("ix_trade_decision_queue_session_id", "session_id"),
        Index("ix_trade_decision_queue_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # DecisionAction.value
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # 결정 시점 기준가 — 실행 드레인의 당일가/갭 게이트 비교 기준.
    reference_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)  # OrderType.value
    strategy_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # 결정 본문 — TradeDecision.model_dump(mode="json") 전체. 실행 드레인이 이걸로
    # TradeDecision을 복원해 발주한다(손절/익절가 등 모든 필드 보존). 위 컬럼들은
    # 조회/관측용 비정규화 사본.
    decision_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # 진입 분석 스냅샷 (메모리 학습용) — 결정 시점에 build_entry_snapshot 결과 보관.
    entry_analysis_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 라이프사이클: pending → executed | expired | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    gate_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # → orders.id
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Execution(TimestampMixin, Base):
    """체결 기록 — 주문별 실제 체결 상세 (부분 체결 시 복수 row)."""

    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_order_id", "order_id"),
        Index("ix_executions_executed_at", "executed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # → orders.id
    broker_order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    fill_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    commission: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRequestDB(TimestampMixin, Base):
    """승인 요청 — 텔레그램 승인 워크플로우 상태 추적.

    request_id(UUID)로 텔레그램 콜백 매핑, order_id로 주문에 연결.
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_order_id", "order_id"),
        Index("ix_approval_requests_status", "status"),
        Index("ix_approval_requests_request_id", "request_id"),
        Index("ix_approval_requests_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # → orders.id
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    modified_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
