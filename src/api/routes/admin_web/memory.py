"""Backoffice 학습 메모리 뷰어 — agent_memory 관측/정리 (F-07/F-08).

에이전트별/종목별 학습 메모리를 비활성·만료 포함해 감사하고, 만료 메모리를
수동 정리(cleanup_expired)할 수 있다.
"""

import math

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import redirect_with, render
from src.api.templates import templates
from src.db.models.strategy import AgentMemory
from src.db.session import get_db_session, get_session_factory
from src.strategy.memory_manager import AgentMemoryManager

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/memory", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def memory_page(
    request: Request,
    agent_type: str | None = Query(None),
    symbol: str | None = Query(None),
    memory_type: str | None = Query(None),
    is_active: str | None = Query(None),
    cleanup_msg: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """GET /admin/memory — 학습 메모리 목록 (비활성·만료 포함)."""
    active_filter = None
    if is_active == "true":
        active_filter = True
    elif is_active == "false":
        active_filter = False

    manager = AgentMemoryManager(get_session_factory())
    rows, total = await manager.list_memories(
        agent_type=agent_type or None,
        symbol=symbol or None,
        memory_type=memory_type or None,
        is_active=active_filter,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    total_pages = max(1, math.ceil(total / per_page)) if total else 1
    page = min(page, total_pages)

    context = {
        "request": request,
        "rows": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "agent_type": agent_type,
        "symbol": symbol,
        "memory_type": memory_type,
        "is_active": is_active,
        "cleanup_msg": cleanup_msg,
    }
    return render(request, "memory.html", "partials/memory_rows.html", context)


@router.get("/memory/{memory_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def memory_detail(
    request: Request,
    memory_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/memory/{memory_id} — 메모리 전문 + context (HTMX 모달)."""
    item = await session.get(AgentMemory, memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return templates.TemplateResponse("partials/memory_detail.html", {
        "request": request,
        "item": item,
    })


@router.post("/memory/cleanup", dependencies=[Depends(require_admin)])
async def memory_cleanup():
    """POST /admin/memory/cleanup — 만료 메모리 비활성화 트리거."""
    manager = AgentMemoryManager(get_session_factory())
    count = await manager.cleanup_expired()
    logger.info("admin.memory_cleanup", deactivated=count)
    return redirect_with("/admin/memory", cleanup_msg=f"만료 메모리 {count}건 비활성화")
