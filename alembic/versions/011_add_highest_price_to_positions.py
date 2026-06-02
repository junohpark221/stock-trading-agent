"""Add highest_price column to positions table for trailing stop high water mark tracking.

Revision ID: 011_highest_price
Revises: 010_risk_tolerance
Create Date: 2026-06-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011_highest_price"
down_revision: str | None = "010_risk_tolerance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("highest_price", sa.Numeric(precision=15, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "highest_price")
