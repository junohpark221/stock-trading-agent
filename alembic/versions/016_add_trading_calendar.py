"""Add trading_calendar table (F-23).

KIS 국내휴장일조회(CTCA0903R) 일 1회 동기화 영속화 테이블.
date가 PK(자연키) — 연 365행 소형 테이블, upsert는 index_elements=["date"].
개장 판정 기준 컬럼은 is_open(KIS opnd_yn).

Revision ID: 016_trading_calendar
Revises: 015_investor_flow
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016_trading_calendar"
down_revision: str | None = "015_investor_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trading_calendar",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("wday_dvsn_cd", sa.String(length=2), nullable=True),
        sa.Column("is_business_day", sa.Boolean(), nullable=False),
        sa.Column("is_trade_day", sa.Boolean(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("is_settlement_day", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("date"),
    )


def downgrade() -> None:
    op.drop_table("trading_calendar")
