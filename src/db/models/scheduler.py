"""Scheduler DB models — job execution tracking.

Phase 6: 스케줄러 작업 실행 이력.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class JobExecution(TimestampMixin, Base):
    """스케줄러 작업 실행 이력.

    각 작업 실행마다 1행 생성. SchedulerEngine._wrap_job()에서 관리.
    job_name+started_at 인덱스로 이력 조회 최적화.
    """

    __tablename__ = "job_executions"
    __table_args__ = (
        Index("ix_job_exec_name_started", "job_name", "started_at"),
        Index("ix_job_exec_status", "status"),
        Index("ix_job_exec_started_at", "started_at"),
        Index("ix_job_exec_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("accounts.id"), nullable=True
    )
    job_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_sec: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True,
    )
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
