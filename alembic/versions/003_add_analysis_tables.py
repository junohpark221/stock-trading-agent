"""Phase 2: analysis tables (financial_statement, economic_indicator, news_article, disclosure)

Revision ID: 003_analysis
Revises: e09b66424886
Create Date: 2026-03-07 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_analysis"
down_revision: str | None = "e09b66424886"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- financial_statement ---
    op.create_table(
        "financial_statement",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("corp_code", sa.String(length=20), nullable=False),
        sa.Column("report_type", sa.String(length=20), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column("revenue", sa.Numeric(precision=20, scale=0), nullable=True),
        sa.Column("operating_income", sa.Numeric(precision=20, scale=0), nullable=True),
        sa.Column("net_income", sa.Numeric(precision=20, scale=0), nullable=True),
        sa.Column("total_assets", sa.Numeric(precision=20, scale=0), nullable=True),
        sa.Column("total_equity", sa.Numeric(precision=20, scale=0), nullable=True),
        sa.Column("total_liabilities", sa.Numeric(precision=20, scale=0), nullable=True),
        sa.Column("per", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("pbr", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("roe", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("eps", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("bps", sa.Numeric(precision=15, scale=2), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
            "symbol",
            "fiscal_year",
            "fiscal_quarter",
            "report_type",
            name="uq_financial_statement",
        ),
    )
    op.create_index(
        "ix_financial_statement_symbol", "financial_statement", ["symbol"], unique=False
    )

    # --- economic_indicator ---
    op.create_table(
        "economic_indicator",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("indicator_code", sa.String(length=50), nullable=False),
        sa.Column("indicator_name", sa.String(length=200), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit", sa.String(length=50), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.UniqueConstraint("source", "indicator_code", "date", name="uq_economic_indicator"),
    )
    op.create_index(
        "ix_economic_indicator_code",
        "economic_indicator",
        ["source", "indicator_code"],
        unique=False,
    )

    # --- news_article ---
    op.create_table(
        "news_article",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("link", sa.String(length=500), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sentiment_score", sa.Numeric(precision=5, scale=3), nullable=True),
        sa.Column("sentiment_label", sa.String(length=20), nullable=True),
        sa.Column("sentiment_method", sa.String(length=50), nullable=True),
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
        sa.UniqueConstraint("link", name="uq_news_article_link"),
    )
    op.create_index("ix_news_article_symbol", "news_article", ["symbol"], unique=False)
    op.create_index("ix_news_article_published", "news_article", ["published_at"], unique=False)

    # --- disclosure ---
    op.create_table(
        "disclosure",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("corp_code", sa.String(length=20), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("report_name", sa.String(length=300), nullable=False),
        sa.Column("receipt_no", sa.String(length=20), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("filer_name", sa.String(length=100), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.UniqueConstraint("receipt_no", name="uq_disclosure_receipt_no"),
    )
    op.create_index("ix_disclosure_symbol", "disclosure", ["symbol"], unique=False)
    op.create_index("ix_disclosure_date", "disclosure", ["receipt_date"], unique=False)


def downgrade() -> None:
    # --- disclosure ---
    op.drop_index("ix_disclosure_date", table_name="disclosure")
    op.drop_index("ix_disclosure_symbol", table_name="disclosure")
    op.drop_table("disclosure")

    # --- news_article ---
    op.drop_index("ix_news_article_published", table_name="news_article")
    op.drop_index("ix_news_article_symbol", table_name="news_article")
    op.drop_table("news_article")

    # --- economic_indicator ---
    op.drop_index("ix_economic_indicator_code", table_name="economic_indicator")
    op.drop_table("economic_indicator")

    # --- financial_statement ---
    op.drop_index("ix_financial_statement_symbol", table_name="financial_statement")
    op.drop_table("financial_statement")
