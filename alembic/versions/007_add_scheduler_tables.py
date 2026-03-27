"""Phase 6: scheduler tables (job_executions)

Revision ID: 007_scheduler
Revises: 006_execution
Create Date: 2026-03-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007_scheduler"
down_revision: str | None = "006_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- job_executions ---
    op.create_table(
        "job_executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_name", sa.String(length=50), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "result_summary",
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
    op.create_index(
        "ix_job_exec_name_started", "job_executions", ["job_name", "started_at"], unique=False
    )
    op.create_index("ix_job_exec_status", "job_executions", ["status"], unique=False)
    op.create_index("ix_job_exec_started_at", "job_executions", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_job_exec_started_at", table_name="job_executions")
    op.drop_index("ix_job_exec_status", table_name="job_executions")
    op.drop_index("ix_job_exec_name_started", table_name="job_executions")
    op.drop_table("job_executions")
