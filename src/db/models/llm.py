"""LLM agent ORM models: DecisionLog, AgentModelConfigDB, LLMUsage."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class DecisionLog(TimestampMixin, Base):
    """통합 의사결정 감사 추적 — 모든 에이전트 판단의 근거·결과를 기록.

    decision_id(UUID)로 개별 의사결정을 식별하고,
    parent_id로 체인 참조, session_id로 분석 사이클을 묶는다.
    """

    __tablename__ = "decision_log"
    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_decision_log_decision_id"),
        Index("ix_decision_log_session_id", "session_id"),
        Index("ix_decision_log_symbol", "symbol"),
        Index("ix_decision_log_stage", "stage"),
        Index("ix_decision_log_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=False, server_default="default"
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # 파이프라인 컨텍스트
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    agent_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # LLM 호출 정보
    llm_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    llm_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )

    # 의사결정
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    data_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 사후 결과
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    outcome_pnl: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentModelConfigDB(TimestampMixin, Base):
    """에이전트별 LLM 모델 할당 설정.

    agent_type 유니크 제약으로 에이전트당 하나의 설정만 존재한다.
    routing_mode=escalation일 때 confidence_threshold 미만이면 escalation_model로 전환.
    """

    __tablename__ = "agent_model_config"
    __table_args__ = (
        UniqueConstraint("agent_type", name="uq_agent_model_config_agent_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_type: Mapped[str] = mapped_column(String(50), nullable=False)
    routing_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fixed"
    )
    primary_model: Mapped[str] = mapped_column(String(100), nullable=False)
    escalation_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confidence_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str] = mapped_column(
        String(50), nullable=False, default="system"
    )


class LLMUsage(TimestampMixin, Base):
    """일별 LLM 사용량 집계.

    (date, provider, model, agent_type) 유니크 제약으로 일별 집계 행을 관리한다.
    agent_type이 NULL이면 시스템 호출을 의미한다.
    PostgreSQL에서 NULL은 unique constraint에서 distinct 취급되므로
    CostTracker(Step 5) upsert에서 COALESCE 처리가 필요하다.
    """

    __tablename__ = "llm_usage"
    __table_args__ = (
        UniqueConstraint(
            "date", "provider", "model", "agent_type", name="uq_llm_usage_daily"
        ),
        Index("ix_llm_usage_date", "date"),
        Index("ix_llm_usage_provider", "provider"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=0
    )
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
