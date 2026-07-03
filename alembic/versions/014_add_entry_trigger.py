"""Add entry_trigger text[] column to positions (F-14).

라이브 스윙 진입 시점의 기술 셋업 태그(RSI과매도반전/MACD골든크로스/BB하단반등)를
포지션에 구조화 영속해, 청산 후 realized_pnl과 조인한 트리거별 성과 귀인을 가능케 한다.
전송은 기존 entry_analysis_snapshot(JSONB) 배관을 재사용하고, 포지션 생성 시 이 전용
컬럼으로 승격한다. 관측/귀인용 주석이며 매매 게이트가 아니다. 기존 행은 NULL 허용(백필 불필요).

Revision ID: 014_entry_trigger
Revises: 013_decision_queue
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "014_entry_trigger"
down_revision: str | None = "013_decision_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("entry_trigger", postgresql.ARRAY(sa.String()), nullable=True),
    )
    # 태그 멤버십 조회(= ANY / @>)용 GIN 인덱스.
    op.create_index(
        "ix_positions_entry_trigger",
        "positions",
        ["entry_trigger"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_positions_entry_trigger", table_name="positions")
    op.drop_column("positions", "entry_trigger")
