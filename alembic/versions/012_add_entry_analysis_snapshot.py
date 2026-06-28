"""Add entry_analysis_snapshot JSONB column to orders and positions.

진입 시 LLM 분석 요약을 주문→포지션으로 전파해, 청산 후 학습 메모리
(record_trade_outcome) 생성에 사용한다. 기존 행은 NULL 허용(백필 불필요).

Revision ID: 012_entry_snapshot
Revises: 011_highest_price
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "012_entry_snapshot"
down_revision: str | None = "011_highest_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("entry_analysis_snapshot", JSONB(), nullable=True),
    )
    op.add_column(
        "positions",
        sa.Column("entry_analysis_snapshot", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "entry_analysis_snapshot")
    op.drop_column("orders", "entry_analysis_snapshot")
