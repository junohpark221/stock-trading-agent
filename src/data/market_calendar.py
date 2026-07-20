"""거래 캘린더 읽기 서비스 (F-23).

trading_calendar 테이블(KIS CTCA0903R 일 1회 동기화본)을 잡 진입 시 1회
로드해, jobs.py의 sync 판정 헬퍼(_is_market_open/_previous_trading_day/
_check_data_freshness)에 주입하는 얇은 조회 계층.

DB 모델(src/db/models/calendar.py)과 파일명 충돌을 피해 market_calendar로 명명.

폴백 정책 (조용한 실패 금지):
- 로드 창에 없는 날짜 → 그 날짜만 기존 주말+KR_HOLIDAYS 판정으로 폴백.
- 오늘 행 자체가 없음(빈 테이블·동기화 장기 실패) → ``stale=True`` 전면 폴백.
  경고 알림은 pre_open_prep이 1일 1건으로 발송(드레인/손절 잡은 로그만 —
  5분 주기 스팸 방지).
- DB 예외 → ``fallback_only`` 반환 + warning 로그. 매매 잡을 멈추지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from src.core.time import today_kst
from src.db.models.calendar import TradingCalendarDay

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger(__name__)

# 로드 창 반경(일) — _previous_trading_day 과거 탐색(최장 연휴 +α)과
# 미래 horizon(CALENDAR_FORWARD_HORIZON_DAYS=30)을 모두 덮는다.
_DEFAULT_WINDOW_DAYS = 45


def _parse_holidays(holidays: str) -> frozenset[str]:
    """KR_HOLIDAYS 쉼표 구분 문자열 → 'YYYY-MM-DD' frozenset."""
    if not holidays:
        return frozenset()
    return frozenset(d.strip() for d in holidays.split(",") if d.strip())


@dataclass(frozen=True)
class MarketCalendar:
    """거래일 판정 스냅샷 — 잡 진입 시 1회 로드해 사용(불변).

    ``open_by_date``: 로드 창 내 DB 행(date → is_open). 창 밖/미적재 날짜는
    주말 + ``fallback_holidays``(KR_HOLIDAYS) 판정으로 폴백한다.
    """

    open_by_date: Mapping[date, bool] = field(default_factory=dict)
    fallback_holidays: frozenset[str] = frozenset()
    stale: bool = False

    def is_trading_day(self, d: date) -> bool:
        known = self.open_by_date.get(d)
        if known is not None:
            return known
        if d.weekday() >= 5:
            return False
        return d.strftime("%Y-%m-%d") not in self.fallback_holidays

    def previous_trading_day(self, ref: date) -> date:
        """ref 직전(이전)의 거래일 — 휴장일(DB is_open=False + 폴백) 스킵."""
        d = ref - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    @classmethod
    def fallback_only(cls, holidays: str = "") -> MarketCalendar:
        """DB 없이 기존 주말+KR_HOLIDAYS 판정만 수행하는 캘린더."""
        return cls(
            open_by_date={},
            fallback_holidays=_parse_holidays(holidays),
            stale=True,
        )


async def load_market_calendar(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    holidays_fallback: str = "",
    window_days: int = _DEFAULT_WINDOW_DAYS,
) -> MarketCalendar:
    """trading_calendar에서 [오늘−N, 오늘+N] 창을 로드(~90행, PK 인덱스).

    DB 예외 시 fallback_only 반환 — 호출한 매매/수집 잡은 기존 KR_HOLIDAYS
    판정으로 계속 동작한다.
    """
    today = today_kst()
    try:
        async with session_factory() as session:
            rows = await session.execute(
                select(TradingCalendarDay.date, TradingCalendarDay.is_open).where(
                    TradingCalendarDay.date >= today - timedelta(days=window_days),
                    TradingCalendarDay.date <= today + timedelta(days=window_days),
                )
            )
            open_by_date: dict[date, bool] = {r[0]: r[1] for r in rows.all()}
    except Exception:
        logger.warning("market_calendar.load_failed", exc_info=True)
        return MarketCalendar.fallback_only(holidays_fallback)

    stale = today not in open_by_date
    if stale:
        logger.warning(
            "market_calendar.stale",
            today=today.isoformat(),
            rows=len(open_by_date),
        )
    return MarketCalendar(
        open_by_date=open_by_date,
        fallback_holidays=_parse_holidays(holidays_fallback),
        stale=stale,
    )
