"""Add risk_tolerance column to accounts table.

Revision ID: 010_risk_tolerance
Revises: 009_accounts
Create Date: 2026-04-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_risk_tolerance"
down_revision: str | None = "009_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "risk_tolerance",
            sa.String(20),
            nullable=False,
            server_default="moderate",
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "risk_tolerance")
