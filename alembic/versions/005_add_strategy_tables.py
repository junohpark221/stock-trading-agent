"""Phase 4: strategy tables (positions, portfolio_snapshots, agent_memory)

Revision ID: 005_strategy
Revises: 004_llm_agent
Create Date: 2026-03-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_strategy"
down_revision: str | None = "004_llm_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- positions ---
    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("strategy_type", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("avg_cost", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("stop_loss_price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("take_profit_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("trailing_stop_pct", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("max_holding_days", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("exit_price", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_reason", sa.String(length=20), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("entry_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("exit_session_id", postgresql.UUID(as_uuid=True), nullable=True),
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
    op.create_index("ix_positions_symbol_status", "positions", ["symbol", "status"], unique=False)
    op.create_index(
        "ix_positions_strategy_status",
        "positions",
        ["strategy_type", "status"],
        unique=False,
    )
    op.create_index("ix_positions_entry_date", "positions", ["entry_date"], unique=False)

    # --- portfolio_snapshots ---
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_value", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("cash", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("invested", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column(
            "realized_pnl_daily",
            sa.Numeric(precision=15, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("peak_value", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("drawdown_pct", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("positions_count", sa.Integer(), nullable=False),
        sa.Column(
            "sector_allocations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "trade_count_daily",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
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
        sa.UniqueConstraint("snapshot_date", name="uq_portfolio_snapshots_date"),
    )
    op.create_index(
        "ix_portfolio_snapshots_date",
        "portfolio_snapshots",
        ["snapshot_date"],
        unique=False,
    )

    # --- agent_memory ---
    op.create_table(
        "agent_memory",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("agent_type", sa.String(length=50), nullable=False),
        sa.Column("memory_type", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("source_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "relevance_score",
            sa.Numeric(precision=5, scale=4),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
    op.create_index(
        "ix_agent_memory_agent_active",
        "agent_memory",
        ["agent_type", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_agent_memory_symbol_active",
        "agent_memory",
        ["symbol", "is_active"],
        unique=False,
    )
    op.create_index("ix_agent_memory_expires", "agent_memory", ["expires_at"], unique=False)


def downgrade() -> None:
    # --- agent_memory ---
    op.drop_index("ix_agent_memory_expires", table_name="agent_memory")
    op.drop_index("ix_agent_memory_symbol_active", table_name="agent_memory")
    op.drop_index("ix_agent_memory_agent_active", table_name="agent_memory")
    op.drop_table("agent_memory")

    # --- portfolio_snapshots ---
    op.drop_index("ix_portfolio_snapshots_date", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")

    # --- positions ---
    op.drop_index("ix_positions_entry_date", table_name="positions")
    op.drop_index("ix_positions_strategy_status", table_name="positions")
    op.drop_index("ix_positions_symbol_status", table_name="positions")
    op.drop_table("positions")
