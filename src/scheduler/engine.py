"""SchedulerEngine — APScheduler AsyncIOScheduler wrapper with DB execution tracking.

9개 작업을 크론/인터벌 트리거로 등록하고, 실행마다 job_executions 테이블에
이력을 기록한다. SCHEDULER_ENABLED=False이면 작업 등록을 건너뛴다.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.core.enums import JobStatus
from src.db.models.scheduler import JobExecution

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.config import Settings

logger = structlog.get_logger(__name__)

_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# async callable type alias
_AsyncFn = Callable[[], Coroutine[Any, Any, None]]


class SchedulerEngine:
    """APScheduler AsyncIOScheduler 래퍼.

    register_jobs()로 9개 작업을 등록하고, start()/stop()으로 생명주기를 관리.
    _wrap_job()이 모든 실행을 감싸서 job_executions 테이블에 이력을 기록한다.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._job_fns: dict[str, _AsyncFn] = {}
        self._paused: bool = False

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """스케줄러가 동작 중인지 반환."""
        return self._scheduler.running

    @property
    def is_paused(self) -> bool:
        """스케줄러가 일시정지 상태인지 반환."""
        return self._paused

    # ── Parse Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_time(time_str: str) -> tuple[int, int]:
        """``"HH:MM"`` 형식 문자열을 ``(hour, minute)`` 튜플로 변환.

        Raises:
            ValueError: 형식이 올바르지 않거나 범위를 벗어난 경우.
        """
        parts = time_str.split(":")
        if len(parts) != 2:
            msg = f"Invalid time format (expected HH:MM): {time_str}"
            raise ValueError(msg)

        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            msg = f"Invalid time format (non-numeric): {time_str}"
            raise ValueError(msg) from None

        if not (0 <= hour <= 23):
            msg = f"Hour out of range (0-23): {hour}"
            raise ValueError(msg)
        if not (0 <= minute <= 59):
            msg = f"Minute out of range (0-59): {minute}"
            raise ValueError(msg)

        return (hour, minute)

    @staticmethod
    def _parse_day_of_week(days_str: str) -> str:
        """``"wed,sat"`` 형식 요일 문자열을 검증하고 반환.

        APScheduler CronTrigger가 직접 사용할 수 있는 소문자 3글자 형식.

        Raises:
            ValueError: 유효하지 않은 요일이 포함된 경우.
        """
        tokens = [t.strip().lower() for t in days_str.split(",")]
        for token in tokens:
            if token not in _VALID_DAYS:
                msg = f"Invalid day of week: {token!r} (valid: {sorted(_VALID_DAYS)})"
                raise ValueError(msg)
        return ",".join(tokens)

    # ── Job Registration ────────────────────────────────────────────────

    def register_jobs(
        self,
        *,
        token_refresh_fn: _AsyncFn,
        market_data_collect_fn: _AsyncFn,
        swing_analysis_fn: _AsyncFn,
        position_analysis_fn: _AsyncFn,
        stop_loss_check_fn: _AsyncFn,
        daily_report_fn: _AsyncFn,
        weekly_report_fn: _AsyncFn,
        monthly_report_fn: _AsyncFn,
        llm_cost_report_fn: _AsyncFn,
    ) -> None:
        """9개 작업을 스케줄러에 등록.

        ``SCHEDULER_ENABLED=False``이면 함수 참조만 저장하고 스케줄러에는
        등록하지 않는다 (run_job_now는 여전히 사용 가능).
        """
        s = self._settings

        # 함수 참조 저장 (run_job_now에서 사용)
        self._job_fns = {
            "token_refresh": token_refresh_fn,
            "market_data_collect": market_data_collect_fn,
            "swing_analysis": swing_analysis_fn,
            "position_analysis": position_analysis_fn,
            "stop_loss_check": stop_loss_check_fn,
            "daily_report": daily_report_fn,
            "weekly_report": weekly_report_fn,
            "monthly_report": monthly_report_fn,
            "llm_cost_report": llm_cost_report_fn,
        }

        if not s.SCHEDULER_ENABLED:
            logger.info("scheduler.register_jobs.disabled", job_count=len(self._job_fns))
            return

        # 트리거 매핑
        tr_h, tr_m = self._parse_time(s.TOKEN_REFRESH_TIME)
        md_h, md_m = self._parse_time(s.MARKET_DATA_COLLECTION_TIME)
        sw_h, sw_m = self._parse_time(s.SWING_ANALYSIS_TIME)
        pa_h, pa_m = self._parse_time(s.POSITION_ANALYSIS_TIME)
        pa_days = self._parse_day_of_week(s.POSITION_ANALYSIS_DAYS)
        dr_h, dr_m = self._parse_time(s.DAILY_REPORT_TIME)
        wr_h, wr_m = self._parse_time(s.WEEKLY_REPORT_TIME)
        wr_day = self._parse_day_of_week(s.WEEKLY_REPORT_DAY)
        mr_h, mr_m = self._parse_time(s.MONTHLY_REPORT_TIME)
        lc_h, lc_m = self._parse_time(s.LLM_COST_REPORT_TIME)
        lc_day = self._parse_day_of_week(s.LLM_COST_REPORT_DAY)

        job_configs: list[tuple[str, _AsyncFn, CronTrigger | IntervalTrigger]] = [
            (
                "token_refresh",
                token_refresh_fn,
                CronTrigger(hour=tr_h, minute=tr_m, timezone="UTC"),
            ),
            (
                "market_data_collect",
                market_data_collect_fn,
                CronTrigger(hour=md_h, minute=md_m, timezone="UTC"),
            ),
            (
                "swing_analysis",
                swing_analysis_fn,
                CronTrigger(hour=sw_h, minute=sw_m, timezone="UTC"),
            ),
            (
                "position_analysis",
                position_analysis_fn,
                CronTrigger(
                    day_of_week=pa_days, hour=pa_h, minute=pa_m, timezone="UTC",
                ),
            ),
            (
                "stop_loss_check",
                stop_loss_check_fn,
                IntervalTrigger(minutes=s.STOP_LOSS_CHECK_INTERVAL_MIN),
            ),
            (
                "daily_report",
                daily_report_fn,
                CronTrigger(hour=dr_h, minute=dr_m, timezone="UTC"),
            ),
            (
                "weekly_report",
                weekly_report_fn,
                CronTrigger(
                    day_of_week=wr_day, hour=wr_h, minute=wr_m, timezone="UTC",
                ),
            ),
            (
                "monthly_report",
                monthly_report_fn,
                CronTrigger(day=s.MONTHLY_REPORT_DAY, hour=mr_h, minute=mr_m, timezone="UTC"),
            ),
            (
                "llm_cost_report",
                llm_cost_report_fn,
                CronTrigger(
                    day_of_week=lc_day, hour=lc_h, minute=lc_m, timezone="UTC",
                ),
            ),
        ]

        for job_name, fn, trigger in job_configs:
            self._scheduler.add_job(
                self._wrap_job,
                trigger=trigger,
                args=[job_name, fn],
                id=job_name,
                name=job_name,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        logger.info("scheduler.register_jobs.done", job_count=len(job_configs))

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """스케줄러를 시작한다.

        ``SCHEDULER_ENABLED=False``이거나 등록된 작업이 없으면 no-op.
        """
        if not self._settings.SCHEDULER_ENABLED:
            logger.info("scheduler.start.disabled")
            return

        if not self._scheduler.get_jobs():
            logger.info("scheduler.start.no_jobs")
            return

        self._scheduler.start()
        logger.info(
            "scheduler.started",
            job_count=len(self._scheduler.get_jobs()),
        )

    async def stop(self) -> None:
        """스케줄러를 중지한다 (진행 중 작업은 완료를 기다리지 않음)."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("scheduler.stopped")

    def pause_all(self) -> None:
        """모든 작업 트리거를 일시정지."""
        self._scheduler.pause()
        self._paused = True
        logger.info("scheduler.paused")

    def resume_all(self) -> None:
        """일시정지된 작업 트리거를 재개."""
        self._scheduler.resume()
        self._paused = False
        logger.info("scheduler.resumed")

    # ── On-Demand Execution ─────────────────────────────────────────────

    async def run_job_now(self, job_name: str) -> None:
        """지정된 작업을 즉시 실행 (pause 상태에서도 동작).

        Raises:
            ValueError: 등록되지 않은 job_name인 경우.
        """
        fn = self._job_fns.get(job_name)
        if fn is None:
            msg = f"Unknown job: {job_name}"
            raise ValueError(msg)

        logger.info("scheduler.run_job_now", job_name=job_name)
        await self._wrap_job(job_name, fn)

    # ── Status ──────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """스케줄러 상태 + 등록된 작업 목록 반환."""
        jobs_info: list[dict[str, Any]] = []
        for job in self._scheduler.get_jobs():
            jobs_info.append({
                "name": job.name,
                "next_run_time": (
                    job.next_run_time.isoformat()
                    if getattr(job, "next_run_time", None)
                    else None
                ),
                "trigger": str(job.trigger),
            })

        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "jobs": jobs_info,
        }

    # ── Execution Wrapper ───────────────────────────────────────────────

    async def _wrap_job(self, job_name: str, fn: _AsyncFn) -> None:
        """작업 실행을 감싸서 job_executions 테이블에 이력을 기록.

        1. INSERT status=RUNNING → commit
        2. fn() 실행
        3. UPDATE status=SUCCESS/FAILED, duration_sec, error_message → commit

        DB 에러는 로그만 남기고 작업 실행을 차단하지 않는다 (fail-open).
        """
        started_at = datetime.now(UTC)
        execution: JobExecution | None = None

        # Step 1: INSERT RUNNING record
        try:
            async with self._session_factory() as session:
                execution = JobExecution(
                    job_name=job_name,
                    status=JobStatus.RUNNING,
                    started_at=started_at,
                )
                session.add(execution)
                await session.commit()
                # refresh to get generated id
                await session.refresh(execution)
                execution_id = execution.id
        except Exception:
            logger.warning("scheduler.wrap_job.insert_failed", job_name=job_name, exc_info=True)
            execution_id = None

        # Step 2: Execute
        status = JobStatus.SUCCESS
        error_message = ""
        try:
            await fn()
        except Exception as exc:
            status = JobStatus.FAILED
            error_message = str(exc)
            logger.warning(
                "scheduler.job_failed",
                job_name=job_name,
                error=error_message,
                exc_info=True,
            )

        # Step 3: UPDATE record
        finished_at = datetime.now(UTC)
        duration_sec = Decimal(str((finished_at - started_at).total_seconds()))

        if execution_id is not None:
            try:
                async with self._session_factory() as session:
                    exec_record = await session.get(JobExecution, execution_id)
                    if exec_record is not None:
                        exec_record.status = status
                        exec_record.finished_at = finished_at
                        exec_record.duration_sec = duration_sec
                        exec_record.error_message = error_message
                        await session.commit()
            except Exception:
                logger.warning(
                    "scheduler.wrap_job.update_failed",
                    job_name=job_name,
                    execution_id=execution_id,
                    exc_info=True,
                )

        logger.info(
            "scheduler.job_completed",
            job_name=job_name,
            status=status,
            duration_sec=str(duration_sec),
        )
