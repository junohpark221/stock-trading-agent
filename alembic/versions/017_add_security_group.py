"""Add stock_master.security_group (PRJ-03 단계 8).

mst part2 선두 2바이트 증권그룹구분코드(ST 주권/RT 리츠/EF ETF/EW ELW 등)를
영속화해 수급 지표 소비 게이트(ST·RT만 허용)의 판별 근거로 사용한다.
NULL = 동기화 전/mst 미수록 — 기동 시 sync_stock_master가 ON CONFLICT DO UPDATE로 채운다.
조회는 symbol PK 단건 lookup이라 별도 인덱스 불요.

Revision ID: 017_security_group
Revises: 016_trading_calendar
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "017_security_group"
down_revision: str | None = "016_trading_calendar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stock_master",
        sa.Column("security_group", sa.String(length=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stock_master", "security_group")
