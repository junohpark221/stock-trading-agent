"""Market data ORM models: StockMaster and DailyOHLCV."""

from datetime import date

from sqlalchemy import BigInteger, Boolean, Date, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class StockMaster(TimestampMixin, Base):
    """종목 마스터 — KIS .mst 파일 기반 종목 정보.

    symbol(종목코드)을 PK로 사용하며, is_active로 상장폐지 종목을 소프트 삭제한다.
    """

    __tablename__ = "stock_master"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    market_type: Mapped[str] = mapped_column(String(20), nullable=False)
    standard_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # mst 증권그룹구분코드(ST 주권/RT 리츠/EF ETF 등). NULL = 동기화 전/mst 미수록.
    # 수급 지표 소비 게이트는 ST·RT만 허용, NULL은 보수적 제외 (PRJ-03 단계 8).
    security_group: Mapped[str | None] = mapped_column(String(2), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    listed_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_cap_krw: Mapped[int | None] = mapped_column(Numeric(20, 0), nullable=True)
    face_value: Mapped[int | None] = mapped_column(Numeric(15, 0), nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DailyOHLCV(TimestampMixin, Base):
    """일봉 OHLCV — 종목별 일간 시세 데이터.

    (symbol, date) 유니크 제약으로 upsert를 보장한다.
    symbol에 FK를 걸지 않아 수집 순서 의존성을 제거한다.
    """

    __tablename__ = "daily_ohlcv"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_daily_ohlcv_symbol_date"),
        Index("ix_daily_ohlcv_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[int] = mapped_column(Numeric(15, 2), nullable=False)
    high: Mapped[int] = mapped_column(Numeric(15, 2), nullable=False)
    low: Mapped[int] = mapped_column(Numeric(15, 2), nullable=False)
    close: Mapped[int] = mapped_column(Numeric(15, 2), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trading_value: Mapped[int] = mapped_column(Numeric(20, 0), nullable=True)
    change_rate: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
