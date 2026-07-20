"""Trading calendar ORM model: TradingCalendarDay (F-23).

KIS 국내휴장일조회(CTCA0903R)를 일 1회 동기화해 영속화하는 거래 캘린더.
휴장일을 `.env` 수동 목록(KR_HOLIDAYS)으로 관리하다 제헌절 재지정을 놓쳐
신선도 게이트가 오작동한 F-23의 근본 수정 — 대체·임시공휴일이 증권사 원장
기준으로 자동 반영되므로 배포 없이 캘린더가 최신을 유지한다.

- ``date``가 PK(자연키) — 하루 1행·FK 참조 없음·연 365행 규모라 서로게이트
  id의 이점이 없고 date PK가 upsert(``index_elements=["date"]``)도 가장 단순.
- 개장 여부 판정은 ``is_open``(KIS ``opnd_yn``) 사용 — "주문 가능 여부는
  개장일여부를 사용" 공식 안내. 나머지 여부 3종은 원값 보존(결제일 계산 등
  향후 재사용 여지).
- ``updated_at``(TimestampMixin)이 마지막 동기화 시각 역할을 겸한다.
"""

from datetime import date

from sqlalchemy import Boolean, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin


class TradingCalendarDay(TimestampMixin, Base):
    """일자별 영업일/거래일/개장일/결제일 여부 — KIS CTCA0903R 동기화본."""

    __tablename__ = "trading_calendar"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    wday_dvsn_cd: Mapped[str | None] = mapped_column(String(2), nullable=True)
    is_business_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_trade_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_settlement_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="kis")
