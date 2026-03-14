"""Phase 3: LLM agent tables (decision_log, agent_model_config, llm_usage)

Revision ID: 004_llm_agent
Revises: 003_analysis
Create Date: 2026-03-14 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_llm_agent"
down_revision: str | None = "003_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- decision_log ---
    op.create_table(
        "decision_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("agent_type", sa.String(length=50), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("llm_provider", sa.String(length=20), nullable=True),
        sa.Column("llm_model", sa.String(length=100), nullable=True),
        sa.Column("llm_prompt", sa.Text(), nullable=True),
        sa.Column("llm_response", sa.Text(), nullable=True),
        sa.Column("llm_tokens_in", sa.Integer(), nullable=True),
        sa.Column("llm_tokens_out", sa.Integer(), nullable=True),
        sa.Column("llm_cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column(
            "data_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("outcome_pnl", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("decision_id", name="uq_decision_log_decision_id"),
    )
    op.create_index(
        "ix_decision_log_session_id", "decision_log", ["session_id"], unique=False
    )
    op.create_index(
        "ix_decision_log_symbol", "decision_log", ["symbol"], unique=False
    )
    op.create_index(
        "ix_decision_log_stage", "decision_log", ["stage"], unique=False
    )

    # --- agent_model_config ---
    op.create_table(
        "agent_model_config",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_type", sa.String(length=50), nullable=False),
        sa.Column(
            "routing_mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'fixed'"),
        ),
        sa.Column("primary_model", sa.String(length=100), nullable=False),
        sa.Column("escalation_model", sa.String(length=100), nullable=True),
        sa.Column(
            "confidence_threshold",
            sa.Numeric(precision=5, scale=4),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "updated_by",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text("'system'"),
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
        sa.UniqueConstraint("agent_type", name="uq_agent_model_config_agent_type"),
    )

    # --- llm_usage ---
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("agent_type", sa.String(length=50), nullable=True),
        sa.Column(
            "tokens_in", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "tokens_out", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "call_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "escalation_count",
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
        sa.UniqueConstraint(
            "date", "provider", "model", "agent_type", name="uq_llm_usage_daily"
        ),
    )
    op.create_index("ix_llm_usage_date", "llm_usage", ["date"], unique=False)
    op.create_index("ix_llm_usage_provider", "llm_usage", ["provider"], unique=False)


def downgrade() -> None:
    # --- llm_usage ---
    op.drop_index("ix_llm_usage_provider", table_name="llm_usage")
    op.drop_index("ix_llm_usage_date", table_name="llm_usage")
    op.drop_table("llm_usage")

    # --- agent_model_config ---
    op.drop_table("agent_model_config")

    # --- decision_log ---
    op.drop_index("ix_decision_log_stage", table_name="decision_log")
    op.drop_index("ix_decision_log_symbol", table_name="decision_log")
    op.drop_index("ix_decision_log_session_id", table_name="decision_log")
    op.drop_table("decision_log")
