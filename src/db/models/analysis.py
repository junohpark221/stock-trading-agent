"""Analysis ORM models: FinancialStatement, EconomicIndicator, NewsArticle, Disclosure."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class FinancialStatement(TimestampMixin, Base):
    """DART 재무제표 — 연간/반기/분기 재무데이터.

    (symbol, fiscal_year, fiscal_quarter, report_type) 유니크 제약으로 upsert를 보장한다.
    fiscal_quarter가 NULL인 annual 보고서의 중복 방지는 application-level upsert에서 처리.
    """

    __tablename__ = "financial_statement"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "fiscal_year",
            "fiscal_quarter",
            "report_type",
            name="uq_financial_statement",
        ),
        Index("ix_financial_statement_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    corp_code: Mapped[str] = mapped_column(String(20), nullable=False)
    report_type: Mapped[str] = mapped_column(String(20), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 재무 항목
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    operating_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(20, 0), nullable=True)

    # 투자지표
    per: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    eps: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    bps: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)

    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class EconomicIndicator(TimestampMixin, Base):
    """ECOS/FRED 매크로 경제지표.

    (source, indicator_code, date) 유니크 제약으로 시계열 중복을 방지한다.
    """

    __tablename__ = "economic_indicator"
    __table_args__ = (
        UniqueConstraint("source", "indicator_code", "date", name="uq_economic_indicator"),
        Index("ix_economic_indicator_code", "source", "indicator_code"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    indicator_code: Mapped[str] = mapped_column(String(50), nullable=False)
    indicator_name: Mapped[str] = mapped_column(String(200), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class NewsArticle(TimestampMixin, Base):
    """뉴스 기사 — 네이버 검색 API 등에서 수집.

    link 유니크 제약으로 중복 기사를 방지한다.
    감성분석 필드(sentiment_*)는 Phase 3에서 LLM으로 채운다.
    """

    __tablename__ = "news_article"
    __table_args__ = (
        UniqueConstraint("link", name="uq_news_article_link"),
        Index("ix_news_article_symbol", "symbol"),
        Index("ix_news_article_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str] = mapped_column(String(500), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # 감성분석 (Phase 3)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 3), nullable=True)
    sentiment_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sentiment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)


class Disclosure(TimestampMixin, Base):
    """DART 공시 — 기업 공시 정보.

    receipt_no(접수번호) 유니크 제약으로 중복 공시를 방지한다.
    """

    __tablename__ = "disclosure"
    __table_args__ = (
        UniqueConstraint("receipt_no", name="uq_disclosure_receipt_no"),
        Index("ix_disclosure_symbol", "symbol"),
        Index("ix_disclosure_date", "receipt_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    corp_code: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    report_name: Mapped[str] = mapped_column(String(300), nullable=False)
    receipt_no: Mapped[str] = mapped_column(String(20), nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    filer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
