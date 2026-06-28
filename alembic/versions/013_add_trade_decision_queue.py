"""Add trade_decision_queue table.

개장 전 결정 잡(08:30)이 BUY 결정을 적재하고, 개장 후 실행 드레인 잡이
당일가/갭 게이트를 통과한 건만 발주하기 위한 가변 실행 큐. decision_log
(감사·불변)와 별도다.

Revision ID: 013_decision_queue
Revises: 012_entry_snapshot
Create Date: 2026-06-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "013_decision_queue"
down_revision: str | None = "012_entry_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_decision_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "account_id",
            sa.String(length=50),
            server_default="default",
            nullable=False,
        ),
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reference_price", sa.Numeric(15, 2), nullable=False),
        sa.Column("order_type", sa.String(length=10), nullable=False),
        sa.Column("strategy_type", sa.String(length=20), nullable=False),
        sa.Column("decision_payload", JSONB(), nullable=False),
        sa.Column("entry_analysis_snapshot", JSONB(), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("gate_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_trade_decision_queue_account_status",
        "trade_decision_queue",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_trade_decision_queue_session_id",
        "trade_decision_queue",
        ["session_id"],
    )
    op.create_index(
        "ix_trade_decision_queue_created_at",
        "trade_decision_queue",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trade_decision_queue_created_at", table_name="trade_decision_queue"
    )
    op.drop_index(
        "ix_trade_decision_queue_session_id", table_name="trade_decision_queue"
    )
    op.drop_index(
        "ix_trade_decision_queue_account_status", table_name="trade_decision_queue"
    )
    op.drop_table("trade_decision_queue")
