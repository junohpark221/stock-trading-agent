"""SchedulerEngine + job functions 단위 테스트.

engine.py: 라이프사이클, _wrap_job DB 이력, parse 헬퍼, register_job
jobs.py: stop_loss_check (조기 리턴 / 시그널 / 계좌별), daily_report (호출 순서 / 계좌별)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.core.enums import JobStatus
from src.scheduler.engine import SchedulerEngine
from src.scheduler.jobs import job_daily_report, job_stop_loss_check
from tests.conftest import make_settings

# ── Fixtures ────────────────────────────────────────────────────────────


def _make_mock_session():
    """DB session mock: add, commit, refresh, get."""
    session = AsyncMock()
    session.add = MagicMock()  # sync method
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def mock_session():
    return _make_mock_session()


@pytest.fixture
def mock_session_factory(mock_session):
    factory = MagicMock(return_value=mock_session)
    return factory


@pytest.fixture
def settings_enabled():
    return make_settings(SCHEDULER_ENABLED=True)


@pytest.fixture
def settings_disabled():
    return make_settings(SCHEDULER_ENABLED=False)


@pytest.fixture
def engine(mock_session_factory, settings_enabled):
    return SchedulerEngine(
        session_factory=mock_session_factory,
        settings=settings_enabled,
    )


@pytest.fixture
def engine_disabled(mock_session_factory, settings_disabled):
    return SchedulerEngine(
        session_factory=mock_session_factory,
        settings=settings_disabled,
    )


def _register_sample_jobs(engine: SchedulerEngine) -> dict[str, AsyncMock]:
    """9개 샘플 작업을 register_job()으로 등록."""
    fns: dict[str, AsyncMock] = {}
    job_configs = [
        ("token_refresh", CronTrigger(hour=6, minute=0, timezone="UTC")),
        ("market_data_collect", CronTrigger(hour=15, minute=40, timezone="UTC")),
        ("swing_analysis", CronTrigger(hour=16, minute=0, timezone="UTC")),
        (
            "position_analysis",
            CronTrigger(
                day_of_week="wed,sat",
                hour=16,
                minute=30,
                timezone="UTC",
            ),
        ),
        ("stop_loss_check", IntervalTrigger(minutes=5)),
        ("daily_report", CronTrigger(hour=20, minute=0, timezone="UTC")),
        ("weekly_report", CronTrigger(day_of_week="sat", hour=10, minute=0, timezone="UTC")),
        ("monthly_report", CronTrigger(day=1, hour=10, minute=0, timezone="UTC")),
        ("llm_cost_report", CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="UTC")),
    ]
    for name, trigger in job_configs:
        fn = AsyncMock()
        fns[name] = fn
        engine.register_job(name, fn, trigger)
    return fns


# ── Parse Helpers ───────────────────────────────────────────────────────


class TestParseTime:
    def test_valid_time(self):
        assert SchedulerEngine._parse_time("15:40") == (15, 40)

    def test_midnight(self):
        assert SchedulerEngine._parse_time("00:00") == (0, 0)

    def test_end_of_day(self):
        assert SchedulerEngine._parse_time("23:59") == (23, 59)

    def test_hour_out_of_range(self):
        with pytest.raises(ValueError, match="Hour out of range"):
            SchedulerEngine._parse_time("25:00")

    def test_minute_out_of_range(self):
        with pytest.raises(ValueError, match="Minute out of range"):
            SchedulerEngine._parse_time("12:60")

    def test_bad_format(self):
        with pytest.raises(ValueError, match="Invalid time format"):
            SchedulerEngine._parse_time("1234")

    def test_non_numeric(self):
        with pytest.raises(ValueError, match="non-numeric"):
            SchedulerEngine._parse_time("ab:cd")


class TestParseDayOfWeek:
    def test_single_day(self):
        assert SchedulerEngine._parse_day_of_week("mon") == "mon"

    def test_multiple_days(self):
        assert SchedulerEngine._parse_day_of_week("wed,sat") == "wed,sat"

    def test_invalid_day(self):
        with pytest.raises(ValueError, match="Invalid day of week"):
            SchedulerEngine._parse_day_of_week("xyz")

    def test_mixed_valid_invalid(self):
        with pytest.raises(ValueError, match="Invalid day of week"):
            SchedulerEngine._parse_day_of_week("mon,xyz")


# ── Engine Lifecycle ────────────────────────────────────────────────────


class TestSchedulerDisabled:
    @pytest.mark.asyncio
    async def test_start_skips_when_disabled(self, engine_disabled):
        """SCHEDULER_ENABLED=False → start()는 스케줄러를 시작하지 않는다."""
        _register_sample_jobs(engine_disabled)
        await engine_disabled.start()
        assert not engine_disabled.is_running

    def test_register_job_stores_fn_but_no_scheduler_job(self, engine_disabled):
        """disabled여도 _job_fns에는 저장 (run_job_now 용)."""
        fn = AsyncMock()
        engine_disabled.register_job("test_job", fn, CronTrigger(hour=0, timezone="UTC"))
        assert "test_job" in engine_disabled._job_fns
        assert len(engine_disabled._scheduler.get_jobs()) == 0


class TestRegisterJob:
    def test_registers_single_job(self, engine):
        """register_job() → 스케줄러에 1개 작업 등록."""
        fn = AsyncMock()
        engine.register_job("my_job", fn, CronTrigger(hour=10, timezone="UTC"))
        assert len(engine._scheduler.get_jobs()) == 1
        assert engine._job_fns["my_job"] is fn

    def test_registers_multiple_jobs(self, engine):
        """register_job() 반복 → 여러 작업 등록."""
        _register_sample_jobs(engine)
        assert len(engine._scheduler.get_jobs()) == 9
        assert len(engine._job_fns) == 9

    def test_account_scoped_job_names(self, engine):
        """계좌별 작업 이름 (예: swing_analysis:acct-1)."""
        fn1 = AsyncMock()
        fn2 = AsyncMock()
        engine.register_job("swing_analysis:acct-1", fn1, CronTrigger(hour=16, timezone="UTC"))
        engine.register_job("swing_analysis:acct-2", fn2, CronTrigger(hour=16, timezone="UTC"))
        assert "swing_analysis:acct-1" in engine._job_fns
        assert "swing_analysis:acct-2" in engine._job_fns
        assert len(engine._scheduler.get_jobs()) == 2

    def test_job_names(self, engine):
        """등록된 작업 이름이 올바른지."""
        _register_sample_jobs(engine)
        job_names = {j.name for j in engine._scheduler.get_jobs()}
        expected = {
            "token_refresh",
            "market_data_collect",
            "swing_analysis",
            "position_analysis",
            "stop_loss_check",
            "daily_report",
            "weekly_report",
            "monthly_report",
            "llm_cost_report",
        }
        assert job_names == expected


class TestGetStatus:
    def test_status_structure(self, engine):
        """get_status()가 올바른 구조를 반환."""
        _register_sample_jobs(engine)
        status = engine.get_status()
        assert "is_running" in status
        assert "is_paused" in status
        assert "jobs" in status
        assert len(status["jobs"]) == 9

    def test_job_info_fields(self, engine):
        """각 job info에 name, next_run_time, trigger가 있다."""
        _register_sample_jobs(engine)
        status = engine.get_status()
        for job_info in status["jobs"]:
            assert "name" in job_info
            assert "next_run_time" in job_info
            assert "trigger" in job_info


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_resume(self, engine):
        """pause_all → is_paused=True, resume_all → is_paused=False."""
        _register_sample_jobs(engine)
        await engine.start()

        assert not engine.is_paused
        engine.pause_all()
        assert engine.is_paused
        engine.resume_all()
        assert not engine.is_paused

        await engine.stop()


# ── run_job_now ─────────────────────────────────────────────────────────


class TestRunJobNow:
    @pytest.mark.asyncio
    async def test_success(self, engine, mock_session):
        """run_job_now → 함수 실행 + JobExecution DB 기록."""
        fns = _register_sample_jobs(engine)

        await engine.run_job_now("daily_report")
        fns["daily_report"].assert_awaited_once()

        # session.add 호출 확인 (JobExecution INSERT)
        mock_session.add.assert_called_once()
        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.job_name == "daily_report"
        assert added_obj.status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_account_scoped_job(self, engine, mock_session):
        """계좌별 작업 이름으로 run_job_now 실행."""
        fn = AsyncMock()
        engine.register_job("swing_analysis:acct-1", fn, CronTrigger(hour=16, timezone="UTC"))

        await engine.run_job_now("swing_analysis:acct-1")
        fn.assert_awaited_once()

        added_obj = mock_session.add.call_args[0][0]
        assert added_obj.job_name == "swing_analysis:acct-1"

    @pytest.mark.asyncio
    async def test_invalid_raises(self, engine):
        """등록되지 않은 job → ValueError."""
        _register_sample_jobs(engine)

        with pytest.raises(ValueError, match="Unknown job: nonexistent"):
            await engine.run_job_now("nonexistent")


# ── _wrap_job ───────────────────────────────────────────────────────────


class TestWrapJob:
    @pytest.mark.asyncio
    async def test_success_records(self, engine, mock_session):
        """성공 실행 → status=SUCCESS, finished_at, duration_sec 기록."""
        fn = AsyncMock()

        # refresh가 execution.id를 설정하도록 mock
        async def _set_id(obj):
            obj.id = 42

        mock_session.refresh = AsyncMock(side_effect=_set_id)

        # session.get()이 반환할 execution 레코드 mock
        exec_record = MagicMock()
        mock_session.get = AsyncMock(return_value=exec_record)

        await engine._wrap_job("test_job", fn)

        fn.assert_awaited_once()

        # INSERT 확인
        mock_session.add.assert_called_once()
        inserted = mock_session.add.call_args[0][0]
        assert inserted.job_name == "test_job"
        assert inserted.status == JobStatus.RUNNING

        # UPDATE 확인 (session.get으로 조회 후 속성 변경)
        assert exec_record.status == JobStatus.SUCCESS
        assert exec_record.finished_at is not None
        assert exec_record.duration_sec >= Decimal("0")
        assert exec_record.error_message == ""

    @pytest.mark.asyncio
    async def test_failure_records(self, engine, mock_session):
        """실패 실행 → status=FAILED, error_message 기록."""
        fn = AsyncMock(side_effect=RuntimeError("test error"))

        async def _set_id(obj):
            obj.id = 43

        mock_session.refresh = AsyncMock(side_effect=_set_id)

        exec_record = MagicMock()
        mock_session.get = AsyncMock(return_value=exec_record)

        await engine._wrap_job("test_job", fn)

        fn.assert_awaited_once()

        # UPDATE 확인
        assert exec_record.status == JobStatus.FAILED
        assert "test error" in exec_record.error_message

    @pytest.mark.asyncio
    async def test_failure_sends_telegram(self, mock_session_factory, settings_enabled):
        """실패 실행 → 텔레그램 에러 알림 발송."""
        telegram_bot = AsyncMock()
        engine = SchedulerEngine(
            session_factory=mock_session_factory,
            settings=settings_enabled,
            telegram_bot=telegram_bot,
        )

        mock_session = _make_mock_session()

        async def _set_id(obj):
            obj.id = 44

        mock_session.refresh = AsyncMock(side_effect=_set_id)
        mock_session.get = AsyncMock(return_value=MagicMock())
        mock_session_factory.return_value = mock_session

        fn = AsyncMock(side_effect=RuntimeError("connection timeout"))
        await engine._wrap_job("market_data_collect", fn)

        telegram_bot.send_message.assert_awaited_once()
        msg = telegram_bot.send_message.call_args[0][0]
        assert "배치 작업 실패" in msg
        assert "market_data_collect" in msg
        assert "connection timeout" in msg

    @pytest.mark.asyncio
    async def test_failure_telegram_error_ignored(self, mock_session_factory, settings_enabled):
        """텔레그램 발송 실패 시 예외가 전파되지 않음."""
        telegram_bot = AsyncMock()
        telegram_bot.send_message = AsyncMock(side_effect=Exception("Telegram down"))
        engine = SchedulerEngine(
            session_factory=mock_session_factory,
            settings=settings_enabled,
            telegram_bot=telegram_bot,
        )

        mock_session = _make_mock_session()

        async def _set_id(obj):
            obj.id = 45

        mock_session.refresh = AsyncMock(side_effect=_set_id)
        mock_session.get = AsyncMock(return_value=MagicMock())
        mock_session_factory.return_value = mock_session

        fn = AsyncMock(side_effect=RuntimeError("some error"))
        # 예외 없이 정상 완료되어야 함
        await engine._wrap_job("test_job", fn)

    @pytest.mark.asyncio
    async def test_db_insert_failure_still_runs_job(self, engine, mock_session_factory):
        """DB INSERT 실패해도 작업은 실행된다 (fail-open)."""
        # session factory가 에러를 일으키도록
        bad_session = _make_mock_session()
        bad_session.commit = AsyncMock(side_effect=Exception("DB down"))
        mock_session_factory.return_value = bad_session

        fn = AsyncMock()
        await engine._wrap_job("test_job", fn)
        fn.assert_awaited_once()


# ── Job Functions ───────────────────────────────────────────────────────


class TestJobStopLossCheck:
    @pytest.mark.asyncio
    async def test_no_positions_skip(self):
        """오픈 포지션 없음 → 조기 리턴, exit_service 미호출."""
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])
        exit_service = AsyncMock()
        exit_checker = MagicMock()
        portfolio_service = AsyncMock()
        broker = AsyncMock()
        monitor = AsyncMock()
        monitor.check_all = AsyncMock(return_value=[])

        await job_stop_loss_check(
            exit_checker=exit_checker,
            exit_service=exit_service,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            broker=broker,
            monitor=monitor,
            market_open="00:00",
            market_close="23:59",
        )

        exit_service.process_exit_signals.assert_not_awaited()
        monitor.check_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_id_passed_to_get_open(self):
        """account_id가 position_manager.get_open에 전달된다."""
        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[])
        exit_service = AsyncMock()
        exit_checker = MagicMock()
        portfolio_service = AsyncMock()
        broker = AsyncMock()
        monitor = AsyncMock()

        await job_stop_loss_check(
            exit_checker=exit_checker,
            exit_service=exit_service,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            broker=broker,
            monitor=monitor,
            account_id="acct-1",
            account_label="공격형 (1234)",
            market_open="00:00",
            market_close="23:59",
        )

        position_manager.get_open.assert_awaited_once_with(account_id="acct-1")

    @pytest.mark.asyncio
    async def test_with_exit_signals(self):
        """손절 시그널 발생 → process_exit_signals 호출."""
        from datetime import UTC, datetime

        from src.core.enums import DecisionAction, ExitReason
        from src.core.models import ExitSignal, PriceInfo

        # Mock position
        position = MagicMock()
        position.symbol = "005930"
        position.entry_price = Decimal("50000")
        position.stop_loss_price = Decimal("45000")
        position.trailing_stop_pct = None
        position.max_holding_days = None

        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[position])

        # 현재가 = 44000 (손절가 이하)
        broker = AsyncMock()
        broker.get_price = AsyncMock(
            return_value=PriceInfo(
                symbol="005930",
                current_price=Decimal("44000"),
                previous_close=Decimal("50000"),
                timestamp=datetime.now(UTC),
            )
        )

        # exit_checker가 stop_loss 시그널 반환
        stop_signal = ExitSignal(
            symbol="005930",
            reason=ExitReason.STOP_LOSS,
            urgency="immediate",
            current_price=Decimal("44000"),
            trigger_price=Decimal("45000"),
            unrealized_pnl_pct=Decimal("-12.00"),
            recommended_action=DecisionAction.STOP_LOSS,
            reasoning="손절가 도달",
        )
        exit_checker = MagicMock()
        exit_checker.check_stop_loss = MagicMock(return_value=stop_signal)

        exit_service = AsyncMock()
        exit_service.process_exit_signals = AsyncMock(return_value=[])

        portfolio_service = AsyncMock()
        monitor = AsyncMock()
        monitor.check_all = AsyncMock(return_value=[])

        await job_stop_loss_check(
            exit_checker=exit_checker,
            exit_service=exit_service,
            position_manager=position_manager,
            portfolio_service=portfolio_service,
            broker=broker,
            monitor=monitor,
            account_id="acct-1",
            account_label="공격형 (1234)",
            market_open="00:00",
            market_close="23:59",
        )

        exit_service.process_exit_signals.assert_awaited_once()
        args = exit_service.process_exit_signals.call_args
        assert len(args[0][0]) == 1  # 1 signal
        assert args[1]["account_id"] == "acct-1"
        assert args[1]["account_label"] == "공격형 (1234)"
        monitor.check_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_coordinator_skips_inflight_position(self):
        """coordinator가 이미 선점한 포지션은 청산에서 스킵된다 (F-05 이중 청산 방지)."""
        from datetime import UTC, datetime

        from src.core.enums import DecisionAction, ExitReason
        from src.core.models import ExitSignal, PriceInfo
        from src.execution.exit_coordinator import ExitCoordinator

        position = MagicMock()
        position.id = 1
        position.symbol = "005930"
        position.entry_price = Decimal("50000")
        position.stop_loss_price = Decimal("45000")
        position.trailing_stop_pct = None
        position.max_holding_days = None

        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[position])
        broker = AsyncMock()
        broker.get_price = AsyncMock(return_value=PriceInfo(
            symbol="005930", current_price=Decimal("44000"),
            previous_close=Decimal("50000"), timestamp=datetime.now(UTC),
        ))
        exit_checker = MagicMock()
        exit_checker.check_stop_loss = MagicMock(return_value=ExitSignal(
            symbol="005930", reason=ExitReason.STOP_LOSS, urgency="immediate",
            current_price=Decimal("44000"), trigger_price=Decimal("45000"),
            unrealized_pnl_pct=Decimal("-12.00"),
            recommended_action=DecisionAction.STOP_LOSS, reasoning="손절가 도달",
        ))
        exit_service = AsyncMock()
        exit_service.process_exit_signals = AsyncMock(return_value=[])
        monitor = AsyncMock()
        monitor.check_all = AsyncMock(return_value=[])

        coordinator = ExitCoordinator(ttl_sec=120)
        await coordinator.try_claim(1)  # WS 등 다른 경로가 이미 선점

        with patch("src.scheduler.jobs._is_market_open", return_value=True):
            await job_stop_loss_check(
                exit_checker=exit_checker, exit_service=exit_service,
                position_manager=position_manager, portfolio_service=AsyncMock(),
                broker=broker, monitor=monitor,
                market_open="00:00", market_close="23:59",
                coordinator=coordinator,
            )

        # 선점된 포지션 → 발주 스킵
        exit_service.process_exit_signals.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coordinator_claims_free_position(self):
        """coordinator 미점유 포지션은 선점 후 청산 발주된다."""
        from datetime import UTC, datetime

        from src.core.enums import DecisionAction, ExitReason
        from src.core.models import ExitSignal, PriceInfo
        from src.execution.exit_coordinator import ExitCoordinator

        position = MagicMock()
        position.id = 2
        position.symbol = "005930"
        position.entry_price = Decimal("50000")
        position.stop_loss_price = Decimal("45000")
        position.trailing_stop_pct = None
        position.max_holding_days = None

        position_manager = AsyncMock()
        position_manager.get_open = AsyncMock(return_value=[position])
        broker = AsyncMock()
        broker.get_price = AsyncMock(return_value=PriceInfo(
            symbol="005930", current_price=Decimal("44000"),
            previous_close=Decimal("50000"), timestamp=datetime.now(UTC),
        ))
        exit_checker = MagicMock()
        exit_checker.check_stop_loss = MagicMock(return_value=ExitSignal(
            symbol="005930", reason=ExitReason.STOP_LOSS, urgency="immediate",
            current_price=Decimal("44000"), trigger_price=Decimal("45000"),
            unrealized_pnl_pct=Decimal("-12.00"),
            recommended_action=DecisionAction.STOP_LOSS, reasoning="손절가 도달",
        ))
        exit_service = AsyncMock()
        exit_service.process_exit_signals = AsyncMock(
            return_value=[MagicMock(success=True, symbol="005930")]
        )
        monitor = AsyncMock()
        monitor.check_all = AsyncMock(return_value=[])

        coordinator = ExitCoordinator(ttl_sec=120)

        with patch("src.scheduler.jobs._is_market_open", return_value=True):
            await job_stop_loss_check(
                exit_checker=exit_checker, exit_service=exit_service,
                position_manager=position_manager, portfolio_service=AsyncMock(),
                broker=broker, monitor=monitor,
                market_open="00:00", market_close="23:59",
                coordinator=coordinator,
            )

        exit_service.process_exit_signals.assert_awaited_once()
        # 성공 청산 클레임은 TTL까지 보유 (즉시 해제 안 함)
        assert await coordinator.is_claimed(2) is True


class TestJobDailyReport:
    @pytest.mark.asyncio
    async def test_call_order(self):
        """save_snapshot → generate → send_message 순서."""
        generator = AsyncMock()
        generator.generate_daily_report = AsyncMock(return_value=MagicMock())

        telegram_bot = AsyncMock()
        telegram_bot.send_message = AsyncMock()

        portfolio_service = AsyncMock()
        state = MagicMock()
        portfolio_service.get_current_state = AsyncMock(return_value=state)
        portfolio_service.save_snapshot = AsyncMock()

        with patch(
            "src.scheduler.jobs.MessageTemplates.daily_report",
            return_value="<html>report</html>",
        ):
            await job_daily_report(
                generator=generator,
                telegram_bot=telegram_bot,
                portfolio_service=portfolio_service,
            )

        # 순서 확인: get_current_state → save_snapshot → generate → send
        portfolio_service.get_current_state.assert_awaited_once()
        portfolio_service.save_snapshot.assert_awaited_once_with(state)
        generator.generate_daily_report.assert_awaited_once()
        telegram_bot.send_message.assert_awaited_once_with("<html>report</html>")

    @pytest.mark.asyncio
    async def test_account_params_passed(self):
        """account_id와 account_label이 generator/template에 전달된다."""
        generator = AsyncMock()
        generator.generate_daily_report = AsyncMock(return_value=MagicMock())

        telegram_bot = AsyncMock()
        telegram_bot.send_message = AsyncMock()

        portfolio_service = AsyncMock()
        portfolio_service.get_current_state = AsyncMock(return_value=MagicMock())
        portfolio_service.save_snapshot = AsyncMock()

        with patch(
            "src.scheduler.jobs.MessageTemplates.daily_report",
            return_value="<html>",
        ) as mock_template:
            await job_daily_report(
                generator=generator,
                telegram_bot=telegram_bot,
                portfolio_service=portfolio_service,
                account_id="acct-1",
                account_label="공격형 (1234)",
            )

        generator.generate_daily_report.assert_awaited_once_with(account_id="acct-1")
        mock_template.assert_called_once()
        assert mock_template.call_args[1]["account_label"] == "공격형 (1234)"
