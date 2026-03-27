"""Phase 8: accounts table + account_id columns on 7 existing tables

Revision ID: 009_accounts
Revises: 008_backtest
Create Date: 2026-03-27 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009_accounts"
down_revision: str | None = "008_backtest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 기존 6개 NOT NULL 테이블 (job_executions 제외)
_NOT_NULL_TABLES = [
    "orders",
    "positions",
    "portfolio_snapshots",
    "approval_requests",
    "agent_memory",
    "decision_log",
]

# 모든 7개 테이블
_ALL_TABLES = [*_NOT_NULL_TABLES, "job_executions"]

# 테이블별 인덱스 이름
_INDEX_NAMES = {
    "orders": "ix_orders_account_id",
    "positions": "ix_positions_account_id",
    "portfolio_snapshots": "ix_portfolio_snapshots_account_id",
    "approval_requests": "ix_approval_requests_account_id",
    "agent_memory": "ix_agent_memory_account_id",
    "decision_log": "ix_decision_log_account_id",
    "job_executions": "ix_job_exec_account_id",
}

# 테이블별 FK 제약 이름
_FK_NAMES = {
    "orders": "fk_orders_account_id",
    "positions": "fk_positions_account_id",
    "portfolio_snapshots": "fk_portfolio_snapshots_account_id",
    "approval_requests": "fk_approval_requests_account_id",
    "agent_memory": "fk_agent_memory_account_id",
    "decision_log": "fk_decision_log_account_id",
    "job_executions": "fk_job_executions_account_id",
}


def upgrade() -> None:
    # 1. accounts 테이블 생성
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(50), nullable=False),
        sa.Column("nickname", sa.String(50), nullable=False, server_default=""),
        sa.Column("kis_app_key_enc", sa.Text(), nullable=False, server_default=""),
        sa.Column("kis_app_secret_enc", sa.Text(), nullable=False, server_default=""),
        sa.Column("kis_account_no", sa.String(20), nullable=False, server_default=""),
        sa.Column("kis_account_prod", sa.String(5), nullable=False, server_default="01"),
        sa.Column("kis_is_paper", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("kis_hts_id", sa.String(50), nullable=False, server_default=""),
        sa.Column("strategy_type", sa.String(20), nullable=False, server_default="position"),
        sa.Column("investment_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk_overrides", postgresql.JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounts_is_active", "accounts", ["is_active"])

    # 2. "default" 계정 INSERT
    op.execute(
        sa.text(
            "INSERT INTO accounts (id, nickname, strategy_type, investment_prompt, is_active, "
            "created_at, updated_at) "
            "VALUES ('default', 'Default', 'position', '', true, now(), now())"
        )
    )

    # 3. 7개 테이블에 account_id 컬럼 추가 (nullable=True로 시작)
    for table in _ALL_TABLES:
        op.add_column(
            table,
            sa.Column("account_id", sa.String(50), nullable=True),
        )

    # 4. 기존 데이터 backfill (6개 NOT NULL 테이블)
    for table in _NOT_NULL_TABLES:
        op.execute(
            sa.text(f"UPDATE {table} SET account_id = 'default' WHERE account_id IS NULL")
        )

    # 5. 6개 테이블 NOT NULL로 변경
    for table in _NOT_NULL_TABLES:
        op.alter_column(table, "account_id", nullable=False, server_default="default")

    # 6. FK 제약 생성
    for table in _ALL_TABLES:
        op.create_foreign_key(
            _FK_NAMES[table], table, "accounts", ["account_id"], ["id"]
        )

    # 7. 인덱스 생성
    for table, idx_name in _INDEX_NAMES.items():
        op.create_index(idx_name, table, ["account_id"])

    # 8. portfolio_snapshots: unique constraint 교체
    op.drop_constraint("uq_portfolio_snapshots_date", "portfolio_snapshots", type_="unique")
    op.create_unique_constraint(
        "uq_portfolio_snapshots_account_date",
        "portfolio_snapshots",
        ["account_id", "snapshot_date"],
    )


def downgrade() -> None:
    # 8. portfolio_snapshots: unique constraint 복원
    op.drop_constraint(
        "uq_portfolio_snapshots_account_date", "portfolio_snapshots", type_="unique"
    )
    op.create_unique_constraint(
        "uq_portfolio_snapshots_date", "portfolio_snapshots", ["snapshot_date"]
    )

    # 7. 인덱스 삭제
    for table, idx_name in _INDEX_NAMES.items():
        op.drop_index(idx_name, table_name=table)

    # 6. FK 제약 삭제
    for table in _ALL_TABLES:
        op.drop_constraint(_FK_NAMES[table], table, type_="foreignkey")

    # 5+4+3. account_id 컬럼 삭제
    for table in _ALL_TABLES:
        op.drop_column(table, "account_id")

    # 2+1. accounts 테이블 삭제
    op.drop_index("ix_accounts_is_active", table_name="accounts")
    op.drop_table("accounts")
