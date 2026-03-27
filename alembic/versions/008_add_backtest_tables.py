"""Phase 7: backtest tables (backtest_runs, backtest_trades)

Revision ID: 008_backtest
Revises: 007_scheduler
Create Date: 2026-03-27 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "008_backtest"
down_revision: str | None = "007_scheduler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- backtest_runs ---
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
        ),
        sa.Column("strategy_type", sa.String(length=20), nullable=False),
        sa.Column(
            "mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'technical'"),
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("initial_capital", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column(
            "slippage_bps",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
        sa.Column(
            "commission_buy_pct",
            sa.Numeric(precision=7, scale=4),
            nullable=False,
            server_default=sa.text("0.015"),
        ),
        sa.Column(
            "commission_sell_pct",
            sa.Numeric(precision=7, scale=4),
            nullable=False,
            server_default=sa.text("0.195"),
        ),
        sa.Column("symbols", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "result_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "total_trades",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"], unique=False)
    op.create_index(
        "ix_backtest_runs_strategy_type",
        "backtest_runs",
        ["strategy_type"],
        unique=False,
    )

    # --- backtest_trades ---
    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column(
            "commission",
            sa.Numeric(precision=15, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "slippage",
            sa.Numeric(precision=15, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("pnl", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("exit_reason", sa.String(length=20), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.run_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_backtest_trades_run_id", "backtest_trades", ["run_id"], unique=False
    )
    op.create_index(
        "ix_backtest_trades_symbol", "backtest_trades", ["symbol"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_trades_symbol", table_name="backtest_trades")
    op.drop_index("ix_backtest_trades_run_id", table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_index("ix_backtest_runs_strategy_type", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_status", table_name="backtest_runs")
    op.drop_table("backtest_runs")
