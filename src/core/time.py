"""중앙 타임존 유틸 — 저장은 UTC(aware), 판정·표시는 KST 기준으로 통일.

원칙:
- DB 저장: 모든 timestamp는 UTC timezone-aware (`DateTime(timezone=True)` + `func.now()`).
- 판정/표시: "오늘/전일" 같은 날짜 판정과 운영자 화면 표기는 항상 KST(Asia/Seoul) 기준.

이 모듈은 그 KST 기준을 한 곳에 모아 코드 전반의 타임존 드리프트를 막는다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    """현재 시각을 KST aware datetime으로 반환."""
    return datetime.now(KST)


def today_kst() -> date:
    """현재 KST 기준 날짜."""
    return datetime.now(KST).date()


def to_kst(dt: datetime) -> datetime:
    """datetime을 KST로 변환. naive는 UTC로 간주한 뒤 변환한다.

    DB의 UTC aware 컬럼을 화면 표기/날짜 판정용으로 옮길 때 사용.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KST)


def kst_day_range(day: date | None = None) -> tuple[datetime, datetime]:
    """주어진 KST 날짜(기본: 오늘 KST)의 [시작, 끝) 경계를 KST-aware로 반환.

    UTC aware 컬럼과 `>= start AND < end` 로 직접 비교하면 DB 세션 타임존에
    의존하지 않고 KST 하루 경계를 정확히 판정할 수 있다(cast 방식의 tz 취약점 회피).
    """
    if day is None:
        day = today_kst()
    start = datetime.combine(day, time.min, tzinfo=KST)
    return start, start + timedelta(days=1)
