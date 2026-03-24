"""Phase 5: execution tables (orders, executions, approval_requests)

Revision ID: 006_execution
Revises: 005_strategy
Create Date: 2026-03-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_execution"
down_revision: str | None = "005_strategy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- orders ---
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("order_type", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "approval_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'auto_approved'"),
        ),
        sa.Column("original_quantity", sa.Integer(), nullable=False),
        sa.Column("modified_quantity", sa.Integer(), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trade_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("broker_order_id", sa.String(length=50), nullable=True),
        sa.Column(
            "filled_quantity",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("filled_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column(
            "commission",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rejection_reason",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("web_verify_result", sa.String(length=20), nullable=True),
        sa.Column(
            "web_verify_summary",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_symbol_status", "orders", ["symbol", "status"], unique=False)
    op.create_index("ix_orders_session_id", "orders", ["session_id"], unique=False)
    op.create_index("ix_orders_created_at", "orders", ["created_at"], unique=False)

    # --- executions ---
    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_order_id", sa.String(length=50), nullable=False),
        sa.Column("fill_price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("fill_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "commission",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executions_order_id", "executions", ["order_id"], unique=False)
    op.create_index("ix_executions_executed_at", "executions", ["executed_at"], unique=False)

    # --- approval_requests ---
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("modified_quantity", sa.Integer(), nullable=True),
        sa.Column(
            "response_reason",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_approval_requests_request_id"),
    )
    op.create_index(
        "ix_approval_requests_order_id", "approval_requests", ["order_id"], unique=False
    )
    op.create_index(
        "ix_approval_requests_status", "approval_requests", ["status"], unique=False
    )
    op.create_index(
        "ix_approval_requests_request_id", "approval_requests", ["request_id"], unique=False
    )


def downgrade() -> None:
    # --- approval_requests ---
    op.drop_index("ix_approval_requests_request_id", table_name="approval_requests")
    op.drop_index("ix_approval_requests_status", table_name="approval_requests")
    op.drop_index("ix_approval_requests_order_id", table_name="approval_requests")
    op.drop_table("approval_requests")

    # --- executions ---
    op.drop_index("ix_executions_executed_at", table_name="executions")
    op.drop_index("ix_executions_order_id", table_name="executions")
    op.drop_table("executions")

    # --- orders ---
    op.drop_index("ix_orders_created_at", table_name="orders")
    op.drop_index("ix_orders_session_id", table_name="orders")
    op.drop_index("ix_orders_symbol_status", table_name="orders")
    op.drop_table("orders")
