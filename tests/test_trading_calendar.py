"""F-23 거래 캘린더 단위 테스트.

- MarketCalendar: DB 우선 판정 / miss 시 주말+KR_HOLIDAYS 폴백 / fallback_only
- previous_trading_day: 제헌절(2026-07-17) 시나리오 — 07-20(월)의 직전 거래일이
  DB 캘린더로 07-16(목)으로 정정되는지 (F-23 결함 재현 케이스)
- load_market_calendar: stale 판정(오늘 행 유무), DB 예외 → fallback_only
- KISHolidayOutput.to_domain: Y/N·날짜 파싱
- KISClient.get_holidays: 페이징(tr_cont M→N, ctx 키 재전달)·horizon 컷·dedupe
- KISDataProvider.sync_trading_calendar: upsert 경로
- job_calendar_sync: 부트스트랩 앵커 분기, 실패 전파
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from conftest import make_settings, mock_aiohttp_response

from src.broker.kis.auth import KISAuth
from src.broker.kis.client import KISClient
from src.broker.kis.models import KISHolidayOutput
from src.core.time import today_kst
from src.data.cache import RedisCache
from src.data.market_calendar import (
    MarketCalendar,
    load_market_calendar,
)
from src.data.providers.kis_provider import KISDataProvider
from src.scheduler.jobs import (
    _CALENDAR_BOOTSTRAP_PAST_DAYS,
    _previous_trading_day,
    job_calendar_sync,
)

# ── Helpers ───────────────────────────────────────────────────────────


def _make_kis_client():
    """KISClient with mocked _session/_auth (skip connect())."""
    client = KISClient(settings=make_settings(), cache=MagicMock(spec=RedisCache))
    client._session = MagicMock(spec=aiohttp.ClientSession)
    client._session.closed = False
    client._auth = MagicMock(spec=KISAuth)
    client._auth.get_token = AsyncMock(return_value="test_token")
    client._auth.build_headers = MagicMock(return_value={"tr_id": "TEST"})
    return client


def _holiday_row(d: date, opnd: str = "Y") -> dict:
    return {
        "bass_dt": d.strftime("%Y%m%d"),
        "wday_dvsn_cd": f"{(d.weekday() + 2 - 1) % 7 + 1:02d}",
        "bzdy_yn": opnd,
        "tr_day_yn": "Y",
        "opnd_yn": opnd,
        "sttl_day_yn": opnd,
    }


def _holiday_response(rows: list[dict], tr_cont: str = "", ctx_fk: str = "", ctx_nk: str = ""):
    data = {
        "rt_cd": "0",
        "msg_cd": "0000",
        "msg1": "정상처리",
        "output": rows,
        "ctx_area_fk": ctx_fk,
        "ctx_area_nk": ctx_nk,
    }
    return mock_aiohttp_response(json_data=data, headers={"tr_cont": tr_cont})


class _SessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        pass


def _calendar_session_factory(*, rows: list | None = None, scalar=None, raises=False):
    """execute().all() 행 / scalar() 값을 반환하는 mock 세션 팩토리."""
    session = AsyncMock()
    if raises:
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        session.scalar = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        result = MagicMock()
        result.all.return_value = rows or []
        session.execute = AsyncMock(return_value=result)
        session.scalar = AsyncMock(return_value=scalar)
    return lambda: _SessionCtx(session), session


# ── MarketCalendar 판정 ───────────────────────────────────────────────


class TestMarketCalendar:
    def test_db_value_wins_over_weekday(self):
        # 2026-07-17은 금요일(평일)이지만 DB가 휴장(제헌절)이라고 하면 휴장
        cal = MarketCalendar(open_by_date={date(2026, 7, 17): False})
        assert cal.is_trading_day(date(2026, 7, 17)) is False

    def test_miss_falls_back_to_weekend(self):
        cal = MarketCalendar(open_by_date={})
        assert cal.is_trading_day(date(2026, 7, 18)) is False  # 토
        assert cal.is_trading_day(date(2026, 7, 20)) is True   # 월

    def test_miss_falls_back_to_kr_holidays(self):
        cal = MarketCalendar(
            open_by_date={}, fallback_holidays=frozenset({"2026-07-17"})
        )
        assert cal.is_trading_day(date(2026, 7, 17)) is False

    def test_fallback_only_is_stale(self):
        cal = MarketCalendar.fallback_only("2026-07-17,2026-08-15")
        assert cal.stale is True
        assert cal.fallback_holidays == frozenset({"2026-07-17", "2026-08-15"})

    def test_previous_trading_day_constitution_day(self):
        # F-23 결함 재현: 07-20(월)의 직전 거래일 — DB에 제헌절 휴장이 있으면 07-16
        cal = MarketCalendar(
            open_by_date={date(2026, 7, 17): False, date(2026, 7, 16): True}
        )
        assert cal.previous_trading_day(date(2026, 7, 20)) == date(2026, 7, 16)

    def test_previous_trading_day_without_db_misjudges(self):
        # 대조군: DB 정보가 없으면(구 동작) 07-17을 거래일로 오판 — 결함 그 자체
        cal = MarketCalendar.fallback_only("")
        assert cal.previous_trading_day(date(2026, 7, 20)) == date(2026, 7, 17)


# ── jobs.py 헬퍼 하위호환 + 캘린더 주입 ──────────────────────────────


class TestPreviousTradingDayHelper:
    def test_legacy_signature_unchanged(self):
        # 기존 테스트와 동일한 계약 — calendar 미주입 시 종전 동작
        assert _previous_trading_day(date(2026, 6, 29)) == date(2026, 6, 26)
        assert _previous_trading_day(
            date(2026, 6, 30), holidays="2026-06-29"
        ) == date(2026, 6, 26)

    def test_calendar_injection(self):
        cal = MarketCalendar(
            open_by_date={date(2026, 7, 17): False, date(2026, 7, 16): True}
        )
        assert _previous_trading_day(
            date(2026, 7, 20), calendar=cal
        ) == date(2026, 7, 16)


# ── load_market_calendar ─────────────────────────────────────────────


class TestLoadMarketCalendar:
    @pytest.mark.asyncio
    async def test_fresh_when_today_present(self):
        today = today_kst()
        factory, _ = _calendar_session_factory(
            rows=[(today, True), (today + timedelta(days=1), False)]
        )
        cal = await load_market_calendar(factory)
        assert cal.stale is False
        assert cal.open_by_date[today] is True

    @pytest.mark.asyncio
    async def test_stale_when_today_missing(self):
        factory, _ = _calendar_session_factory(
            rows=[(today_kst() - timedelta(days=3), True)]
        )
        cal = await load_market_calendar(factory, holidays_fallback="2026-08-15")
        assert cal.stale is True
        assert "2026-08-15" in cal.fallback_holidays

    @pytest.mark.asyncio
    async def test_db_error_returns_fallback_only(self):
        factory, _ = _calendar_session_factory(raises=True)
        cal = await load_market_calendar(factory, holidays_fallback="2026-08-15")
        assert cal.stale is True
        assert cal.open_by_date == {}


# ── KISHolidayOutput ─────────────────────────────────────────────────


class TestKISHolidayOutput:
    def test_to_domain_parses_flags_and_date(self):
        out = KISHolidayOutput.model_validate(
            {
                "bass_dt": "20260717",
                "wday_dvsn_cd": "06",
                "bzdy_yn": "Y",
                "tr_day_yn": "Y",
                "opnd_yn": "N",
                "sttl_day_yn": "N",
                "unknown_field": "x",  # extra="ignore"
            }
        )
        rec = out.to_domain()
        assert rec.date == date(2026, 7, 17)
        assert rec.is_business_day is True
        assert rec.is_open is False
        assert rec.is_settlement_day is False
        assert rec.wday_dvsn_cd == "06"


# ── KISClient.get_holidays ───────────────────────────────────────────


class TestKISClientGetHolidays:
    @pytest.mark.asyncio
    async def test_single_page_without_until(self):
        client = _make_kis_client()
        rows = [_holiday_row(date(2026, 7, 17) + timedelta(days=i)) for i in range(5)]
        client._session.get = AsyncMock(
            return_value=_holiday_response(rows, tr_cont="M")
        )
        records = await client.get_holidays(start_date=date(2026, 7, 17))
        # until_date 미지정 → 다음 페이지가 있어도 단발
        assert client._session.get.await_count == 1
        assert len(records) == 5
        assert records[0].date == date(2026, 7, 17)

    @pytest.mark.asyncio
    async def test_paging_forwards_ctx_keys(self):
        client = _make_kis_client()
        page1 = [_holiday_row(date(2026, 7, 1) + timedelta(days=i)) for i in range(10)]
        page2 = [_holiday_row(date(2026, 7, 11) + timedelta(days=i)) for i in range(10)]
        client._session.get = AsyncMock(
            side_effect=[
                _holiday_response(page1, tr_cont="M", ctx_fk="FK1", ctx_nk="NK1"),
                _holiday_response(page2, tr_cont=""),
            ]
        )
        records = await client.get_holidays(
            start_date=date(2026, 7, 1), until_date=date(2026, 7, 20)
        )
        assert client._session.get.await_count == 2
        params2 = client._session.get.call_args_list[1].kwargs["params"]
        assert params2["CTX_AREA_FK"] == "FK1"
        assert params2["CTX_AREA_NK"] == "NK1"
        assert len(records) == 20
        assert records[0].date < records[-1].date

    @pytest.mark.asyncio
    async def test_horizon_cut_stops_paging_and_trims(self):
        client = _make_kis_client()
        rows = [_holiday_row(date(2026, 7, 1) + timedelta(days=i)) for i in range(15)]
        client._session.get = AsyncMock(
            return_value=_holiday_response(rows, tr_cont="M")
        )
        records = await client.get_holidays(
            start_date=date(2026, 7, 1), until_date=date(2026, 7, 10)
        )
        # 첫 페이지에서 horizon 도달 → 연속조회 안 함 + 초과분 절단
        assert client._session.get.await_count == 1
        assert records[-1].date == date(2026, 7, 10)
        assert len(records) == 10

    @pytest.mark.asyncio
    async def test_dedupe_duplicate_dates(self):
        client = _make_kis_client()
        rows = [_holiday_row(date(2026, 7, 1)), _holiday_row(date(2026, 7, 1))]
        client._session.get = AsyncMock(return_value=_holiday_response(rows))
        records = await client.get_holidays(start_date=date(2026, 7, 1))
        assert len(records) == 1


# ── KISDataProvider.sync_trading_calendar ────────────────────────────


class TestSyncTradingCalendar:
    def _make_provider(self, records):
        client = MagicMock()
        client.get_holidays = AsyncMock(return_value=records)
        session = AsyncMock()
        result = MagicMock()
        result.rowcount = len(records)
        session.execute = AsyncMock(return_value=result)
        provider = KISDataProvider(
            client=client,
            cache=MagicMock(),
            session_factory=lambda: _SessionCtx(session),
            settings=MagicMock(),
        )
        return provider, client, session

    @pytest.mark.asyncio
    async def test_upserts_records(self):
        from src.core.models import TradingDayRecord

        records = [
            TradingDayRecord(
                date=date(2026, 7, 17),
                wday_dvsn_cd="06",
                is_business_day=True,
                is_trade_day=True,
                is_open=False,
                is_settlement_day=False,
            )
        ]
        provider, client, session = self._make_provider(records)
        upserted = await provider.sync_trading_calendar(
            start_date=date(2026, 7, 17), until_date=date(2026, 8, 16)
        )
        assert upserted == 1
        client.get_holidays.assert_awaited_once_with(
            start_date=date(2026, 7, 17), until_date=date(2026, 8, 16)
        )
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()
        stmt = session.execute.await_args.args[0]
        assert stmt.table.name == "trading_calendar"

    @pytest.mark.asyncio
    async def test_empty_returns_zero_without_db(self):
        provider, _, session = self._make_provider([])
        upserted = await provider.sync_trading_calendar(
            start_date=date(2026, 7, 17), until_date=date(2026, 8, 16)
        )
        assert upserted == 0
        session.execute.assert_not_awaited()


# ── job_calendar_sync ────────────────────────────────────────────────


class TestJobCalendarSync:
    def _settings(self):
        s = MagicMock()
        s.CALENDAR_FORWARD_HORIZON_DAYS = 30
        return s

    @pytest.mark.asyncio
    async def test_bootstrap_when_table_empty(self):
        factory, _ = _calendar_session_factory(scalar=None)
        provider = MagicMock()
        provider.sync_trading_calendar = AsyncMock(return_value=70)
        await job_calendar_sync(
            provider=provider, session_factory=factory, settings=self._settings()
        )
        today = today_kst()
        provider.sync_trading_calendar.assert_awaited_once_with(
            start_date=today - timedelta(days=_CALENDAR_BOOTSTRAP_PAST_DAYS),
            until_date=today + timedelta(days=30),
        )

    @pytest.mark.asyncio
    async def test_normal_anchor_when_past_covered(self):
        today = today_kst()
        factory, _ = _calendar_session_factory(
            scalar=today - timedelta(days=100)
        )
        provider = MagicMock()
        provider.sync_trading_calendar = AsyncMock(return_value=31)
        await job_calendar_sync(
            provider=provider, session_factory=factory, settings=self._settings()
        )
        provider.sync_trading_calendar.assert_awaited_once_with(
            start_date=today, until_date=today + timedelta(days=30)
        )

    @pytest.mark.asyncio
    async def test_shallow_past_rebootstraps(self):
        today = today_kst()
        factory, _ = _calendar_session_factory(scalar=today - timedelta(days=10))
        provider = MagicMock()
        provider.sync_trading_calendar = AsyncMock(return_value=70)
        await job_calendar_sync(
            provider=provider, session_factory=factory, settings=self._settings()
        )
        assert (
            provider.sync_trading_calendar.await_args.kwargs["start_date"]
            == today - timedelta(days=_CALENDAR_BOOTSTRAP_PAST_DAYS)
        )

    @pytest.mark.asyncio
    async def test_failure_propagates_to_engine(self):
        factory, _ = _calendar_session_factory(scalar=None)
        provider = MagicMock()
        provider.sync_trading_calendar = AsyncMock(
            side_effect=RuntimeError("KIS down")
        )
        with pytest.raises(RuntimeError):
            await job_calendar_sync(
                provider=provider, session_factory=factory, settings=self._settings()
            )


# ── _job_calendar_sync_prod (F-23 실전 도메인 래퍼) ──────────────────


class TestJobCalendarSyncProd:
    """CTCA0903R 모의 미지원 대응 — 실전 앱키 1회용 클라이언트 래퍼."""

    def _settings(self, key: str = "PRODKEY", secret: str = "PRODSECRET"):
        s = MagicMock()
        s.KIS_PROD_APP_KEY = key
        s.KIS_PROD_APP_SECRET = secret
        s.CALENDAR_FORWARD_HORIZON_DAYS = 30
        return s

    @pytest.mark.asyncio
    async def test_skip_without_prod_keys(self, monkeypatch):
        from src.scheduler.factory import SchedulerFactory

        from_credentials = MagicMock()
        monkeypatch.setattr(
            "src.broker.kis.client.KISClient.from_credentials", from_credentials
        )
        await SchedulerFactory._job_calendar_sync_prod(
            settings=self._settings(key="", secret=""),
            cache=MagicMock(),
            session_factory=MagicMock(),
        )
        from_credentials.assert_not_called()  # 스킵 — 클라이언트 미생성·무예외

    @pytest.mark.asyncio
    async def test_skip_without_cache(self, monkeypatch):
        from src.scheduler.factory import SchedulerFactory

        from_credentials = MagicMock()
        monkeypatch.setattr(
            "src.broker.kis.client.KISClient.from_credentials", from_credentials
        )
        await SchedulerFactory._job_calendar_sync_prod(
            settings=self._settings(), cache=None, session_factory=MagicMock()
        )
        from_credentials.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_sync_with_prod_credentials(self, monkeypatch):
        import src.scheduler.factory as factory_mod
        from src.scheduler.factory import SchedulerFactory

        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        from_credentials = MagicMock(return_value=client)
        monkeypatch.setattr(
            "src.broker.kis.client.KISClient.from_credentials", from_credentials
        )
        provider_cls = MagicMock()
        monkeypatch.setattr(
            "src.data.providers.kis_provider.KISDataProvider", provider_cls
        )
        job = AsyncMock()
        monkeypatch.setattr(factory_mod, "job_calendar_sync", job)

        await SchedulerFactory._job_calendar_sync_prod(
            settings=self._settings(), cache=MagicMock(), session_factory=MagicMock()
        )

        creds = from_credentials.call_args.args[0]
        assert creds.account_id == "calendar-prod"  # 토큰 캐시 키 분리
        assert creds.is_paper is False  # 실전 도메인 자동 결정
        assert creds.app_key == "PRODKEY"
        client.connect.assert_awaited_once()
        job.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_on_sync_failure(self, monkeypatch):
        import src.scheduler.factory as factory_mod
        from src.scheduler.factory import SchedulerFactory

        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        monkeypatch.setattr(
            "src.broker.kis.client.KISClient.from_credentials",
            MagicMock(return_value=client),
        )
        monkeypatch.setattr(
            "src.data.providers.kis_provider.KISDataProvider", MagicMock()
        )
        job = AsyncMock(side_effect=RuntimeError("KIS down"))
        monkeypatch.setattr(factory_mod, "job_calendar_sync", job)

        with pytest.raises(RuntimeError):  # 실패는 엔진 알림으로 전파
            await SchedulerFactory._job_calendar_sync_prod(
                settings=self._settings(),
                cache=MagicMock(),
                session_factory=MagicMock(),
            )
        client.disconnect.assert_awaited_once()  # 세션 누수 없음
