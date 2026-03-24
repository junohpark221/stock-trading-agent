"""Phase 5 execution ORM models: Order, Execution, ApprovalRequestDB."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
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
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
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
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
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
