"""Scheduler control API — manual scheduler management.

Endpoints:
    GET  /api/control/scheduler/status          — 스케줄러 상태 + 작업 목록
    POST /api/control/scheduler/pause           — 전체 작업 일시정지
    POST /api/control/scheduler/resume          — 전체 작업 재개
    POST /api/control/scheduler/run/{job_name}  — 특정 작업 즉시 실행
    GET  /api/control/jobs/history              — 작업 실행 이력
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.db.models.scheduler import JobExecution
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/control", tags=["control"])


# ── Helpers ──────────────────────────────────────────────────────────


def _get_scheduler():
    """SchedulerEngine 싱글톤 반환. 비활성 시 None."""
    from src.main import get_scheduler

    try:
        return get_scheduler()
    except RuntimeError:
        return None


# ── Scheduler Status ─────────────────────────────────────────────────


@router.get("/scheduler/status")
async def scheduler_status(
    account_id: str | None = Query(None, description="계좌 ID (미지정 시 전체)"),
) -> JSONResponse:
    """스케줄러 상태 + 전체 작업 다음 실행 시간.

    SCHEDULER_ENABLED=False → {"status": "disabled"} 200 응답.
    account_id 지정 시 해당 계좌 관련 작업만 표시.
    """
    settings = get_settings()
    if not settings.SCHEDULER_ENABLED:
        return JSONResponse({"status": "disabled"})

    engine = _get_scheduler()
    if engine is None:
        return JSONResponse({"status": "disabled"})

    status = engine.get_status()
    jobs = status["jobs"]

    if account_id is not None:
        jobs = [j for j in jobs if account_id in j.get("name", "")]

    return JSONResponse({
        "status": "ok",
        "is_running": status["is_running"],
        "is_paused": status["is_paused"],
        "jobs": jobs,
    })


# ── Pause / Resume ──────────────────────────────────────────────────


@router.post("/scheduler/pause")
async def scheduler_pause() -> JSONResponse:
    """전체 작업 일시정지.

    이미 정지 상태이면 그대로 200 응답.
    """
    settings = get_settings()
    if not settings.SCHEDULER_ENABLED:
        return JSONResponse({"status": "disabled"})

    engine = _get_scheduler()
    if engine is None:
        return JSONResponse({"status": "disabled"})

    engine.pause_all()
    return JSONResponse({
        "status": "paused",
        "paused_at": datetime.now(UTC).isoformat(),
    })


@router.post("/scheduler/resume")
async def scheduler_resume() -> JSONResponse:
    """전체 작업 재개.

    이미 실행 상태이면 그대로 200 응답.
    """
    settings = get_settings()
    if not settings.SCHEDULER_ENABLED:
        return JSONResponse({"status": "disabled"})

    engine = _get_scheduler()
    if engine is None:
        return JSONResponse({"status": "disabled"})

    engine.resume_all()
    return JSONResponse({
        "status": "resumed",
        "resumed_at": datetime.now(UTC).isoformat(),
    })


# ── Run Job Now ──────────────────────────────────────────────────────


@router.post("/scheduler/run/{job_name}")
async def scheduler_run_job(job_name: str) -> JSONResponse:
    """특정 작업 즉시 실행.

    유효한 job_name:
    token_refresh, market_data_collect, swing_analysis, position_analysis,
    stop_loss_check, daily_report, weekly_report, monthly_report, llm_cost_report

    200: {"status": "triggered", "job_name": str}
    400: {"error": "invalid_job_name", "valid_names": [...]}
    """
    settings = get_settings()
    if not settings.SCHEDULER_ENABLED:
        return JSONResponse({"status": "disabled"})

    engine = _get_scheduler()
    if engine is None:
        return JSONResponse({"status": "disabled"})

    try:
        await engine.run_job_now(job_name)
    except ValueError:
        valid_names = sorted(engine._job_fns.keys())
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_job_name",
                "valid_names": valid_names,
            },
        )

    return JSONResponse({
        "status": "triggered",
        "job_name": job_name,
    })


# ── Job History ──────────────────────────────────────────────────────


@router.get("/jobs/history")
async def job_history(
    job_name: str | None = Query(None),
    status: str | None = Query(None),
    account_id: str | None = Query(None, description="계좌 ID"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """최근 작업 실행 이력 (job_executions 테이블).

    필터: job_name, status.
    정렬: started_at DESC.
    Returns: {"items": [...], "total": int}
    """
    # Base query
    stmt = select(JobExecution).order_by(JobExecution.started_at.desc())
    count_stmt = select(func.count(JobExecution.id))

    if job_name is not None:
        stmt = stmt.where(JobExecution.job_name == job_name)
        count_stmt = count_stmt.where(JobExecution.job_name == job_name)
    if status is not None:
        stmt = stmt.where(JobExecution.status == status)
        count_stmt = count_stmt.where(JobExecution.status == status)
    if account_id is not None:
        stmt = stmt.where(JobExecution.account_id == account_id)
        count_stmt = count_stmt.where(JobExecution.account_id == account_id)

    # Total count
    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    # Paginated items
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        {
            "id": row.id,
            "job_name": row.job_name,
            "status": row.status,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "duration_sec": str(row.duration_sec) if row.duration_sec is not None else None,
            "error_message": row.error_message,
            "result_summary": row.result_summary,
        }
        for row in rows
    ]

    return JSONResponse({"items": items, "total": total})
