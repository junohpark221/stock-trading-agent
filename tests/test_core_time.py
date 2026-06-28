"""src.core.time — KST 타임존 유틸 단위 테스트 (B-07)."""

from datetime import UTC, date, datetime

from src.core.time import KST, kst_day_range, to_kst, today_kst


class TestToKst:
    def test_aware_utc_converts_to_kst_plus9(self):
        dt = datetime(2026, 6, 28, 0, 0, tzinfo=UTC)
        kst = to_kst(dt)
        assert kst.tzinfo == KST
        assert kst.hour == 9  # UTC 00:00 → KST 09:00
        assert kst.date() == date(2026, 6, 28)

    def test_naive_treated_as_utc(self):
        # naive 입력은 UTC로 간주
        naive = datetime(2026, 6, 28, 1, 0)
        assert to_kst(naive).hour == 10  # +9h

    def test_utc_late_evening_rolls_to_next_kst_day(self):
        # UTC 23:30 = KST 익일 08:30 — 날짜 경계가 KST에서 하루 넘어감
        dt = datetime(2026, 6, 27, 23, 30, tzinfo=UTC)
        kst = to_kst(dt)
        assert kst.date() == date(2026, 6, 28)
        assert (kst.hour, kst.minute) == (8, 30)


class TestKstDayRange:
    def test_explicit_day_bounds_are_kst_midnight(self):
        start, end = kst_day_range(date(2026, 6, 28))
        assert start == datetime(2026, 6, 28, 0, 0, tzinfo=KST)
        assert end == datetime(2026, 6, 29, 0, 0, tzinfo=KST)

    def test_utc_2330_prev_day_falls_inside_today_kst_range(self):
        # UTC 23:30(전일) 주문이 KST 당일 경계 [start, end) 안에 들어옴
        order_at = datetime(2026, 6, 27, 23, 30, tzinfo=UTC)
        start, end = kst_day_range(date(2026, 6, 28))
        assert start <= order_at < end

    def test_default_uses_today_kst(self):
        start, end = kst_day_range()
        assert start.date() == today_kst()
        assert (end - start).days == 1
