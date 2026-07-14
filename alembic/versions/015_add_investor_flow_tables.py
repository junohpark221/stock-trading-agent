"""Add investor flow tables (PRJ-03).

수급 데이터 서브시스템 테이블 3종:
- investor_flow_daily: 종목별 투자자 수급 (15축 × net/sell/buy × qty/amt = 90컬럼)
- short_interest_daily: 공매도·대차 (두 TR partial upsert)
- market_investor_flow_daily: 시장 단위 수급 + 지수 OHLC (시스템 내 유일 소스)

대금(*_amt)은 원(KRW) 단위 통일 — KIS 백만원 필드의 ×1e6 변환은 수집 계층 책임.
데이터 컬럼 전부 nullable — pykrx 백필 행은 KRX 부재 축이 NULL.

Revision ID: 015_investor_flow
Revises: 014_entry_trigger
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "015_investor_flow"
down_revision: str | None = "014_entry_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 투자자 주체 15축 (KIS 토큰 — 모델 docstring 참조)
_AXES = (
    "frgn",
    "frgn_reg",
    "frgn_nreg",
    "prsn",
    "orgn",
    "scrt",
    "ivtr",
    "pe_fund",
    "bank",
    "insu",
    "mrbn",
    "fund",
    "etc",
    "etc_corp",
    "etc_orgt",
)


def _flow_columns(slots: tuple[str, ...]) -> list[sa.Column]:
    """축×슬롯 수급 컬럼 생성 — *_qty는 BigInteger, *_amt는 Numeric(20,0) 원 단위."""
    columns: list[sa.Column] = []
    for axis in _AXES:
        for slot in slots:
            col_type = sa.BigInteger() if slot.endswith("_qty") else sa.Numeric(20, 0)
            columns.append(sa.Column(f"{axis}_{slot}", col_type, nullable=True))
    return columns


def _timestamp_columns() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "investor_flow_daily",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        *_flow_columns(("net_qty", "net_amt", "sell_qty", "buy_qty", "sell_amt", "buy_amt")),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "date", name="uq_investor_flow_daily_symbol_date"),
    )
    op.create_index("ix_investor_flow_daily_date", "investor_flow_daily", ["date"])

    op.create_table(
        "short_interest_daily",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("short_sale_qty", sa.BigInteger(), nullable=True),
        sa.Column("short_sale_vol_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("short_sale_amt", sa.Numeric(20, 0), nullable=True),
        sa.Column("short_sale_amt_ratio", sa.Numeric(10, 4), nullable=True),
        sa.Column("avg_price", sa.Numeric(15, 2), nullable=True),
        sa.Column("loan_new_qty", sa.BigInteger(), nullable=True),
        sa.Column("loan_redemption_qty", sa.BigInteger(), nullable=True),
        sa.Column("loan_balance_diff", sa.BigInteger(), nullable=True),
        sa.Column("loan_balance_qty", sa.BigInteger(), nullable=True),
        sa.Column("loan_balance_amt", sa.Numeric(20, 0), nullable=True),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "date", name="uq_short_interest_daily_symbol_date"),
    )
    op.create_index("ix_short_interest_daily_date", "short_interest_daily", ["date"])

    op.create_table(
        "market_investor_flow_daily",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=10), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("index_open", sa.Numeric(15, 2), nullable=True),
        sa.Column("index_high", sa.Numeric(15, 2), nullable=True),
        sa.Column("index_low", sa.Numeric(15, 2), nullable=True),
        sa.Column("index_close", sa.Numeric(15, 2), nullable=True),
        sa.Column("index_prev_close", sa.Numeric(15, 2), nullable=True),
        sa.Column("index_change", sa.Numeric(15, 2), nullable=True),
        sa.Column("index_change_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("index_volume", sa.BigInteger(), nullable=True),
        sa.Column("index_trading_value", sa.Numeric(20, 0), nullable=True),
        sa.Column("index_market_cap", sa.Numeric(20, 0), nullable=True),
        *_flow_columns(("net_qty", "net_amt")),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market", "date", name="uq_market_investor_flow_daily_market_date"),
    )


def downgrade() -> None:
    op.drop_table("market_investor_flow_daily")
    op.drop_index("ix_short_interest_daily_date", table_name="short_interest_daily")
    op.drop_table("short_interest_daily")
    op.drop_index("ix_investor_flow_daily_date", table_name="investor_flow_daily")
    op.drop_table("investor_flow_daily")
