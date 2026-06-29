"""Backoffice scheduler routes: status + pause/resume + manual run."""

import math

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.templates import templates
from src.db.models.scheduler import JobExecution
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()


async def _build_scheduler_context(
    request: Request,
    session: AsyncSession,
    *,
    job_name: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """스케줄러 상태 + 작업 이력 컨텍스트 공통 빌더."""
    from src.main import get_scheduler

    try:
        scheduler_status = get_scheduler().get_status()
    except RuntimeError:
        scheduler_status = {"is_running": False, "is_paused": False, "jobs": [], "disabled": True}

    # 작업 이름 목록 (필터 드롭다운용)
    job_names = [j["name"] for j in scheduler_status.get("jobs", [])]

    # 작업 이력 쿼리
    stmt = select(JobExecution).order_by(JobExecution.started_at.desc())
    count_stmt = select(func.count(JobExecution.id))

    if job_name:
        stmt = stmt.where(JobExecution.job_name == job_name)
        count_stmt = count_stmt.where(JobExecution.job_name == job_name)
    if status:
        stmt = stmt.where(JobExecution.status == status)
        count_stmt = count_stmt.where(JobExecution.status == status)

    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)
    history = list((await session.execute(stmt)).scalars().all())

    return {
        "request": request,
        "scheduler": scheduler_status,
        "job_names": job_names,
        "history": history,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "job_name": job_name,
        "status": status,
    }


@router.get("/scheduler", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_page(
    request: Request,
    job_name: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/scheduler — 스케줄러 상태 + 작업 목록 + 이력."""
    context = await _build_scheduler_context(
        request, session, job_name=job_name, status=status, page=page, per_page=per_page,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/scheduler_status.html", context)
    return templates.TemplateResponse("scheduler.html", context)


@router.post("/scheduler/pause", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_pause(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/scheduler/pause — 스케줄러 일시정지."""
    from src.main import get_scheduler

    get_scheduler().pause_all()
    context = await _build_scheduler_context(request, session)
    return templates.TemplateResponse("partials/scheduler_status.html", context)


@router.post("/scheduler/resume", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_resume(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/scheduler/resume — 스케줄러 재개."""
    from src.main import get_scheduler

    get_scheduler().resume_all()
    context = await _build_scheduler_context(request, session)
    return templates.TemplateResponse("partials/scheduler_status.html", context)


@router.post("/scheduler/run/{job_name:path}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def scheduler_run_job(
    request: Request,
    job_name: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
):
    """POST /admin/scheduler/run/{job_name} — 작업 즉시 실행."""
    from src.main import get_scheduler

    scheduler = get_scheduler()
    background_tasks.add_task(scheduler.run_job_now, job_name)
    context = await _build_scheduler_context(request, session)
    return templates.TemplateResponse("partials/scheduler_status.html", context)
