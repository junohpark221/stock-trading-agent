"""Backoffice decision-log routes: list + session detail."""

import math
from datetime import date, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.templates import templates
from src.core.time import KST
from src.db.models.account import Account
from src.db.models.llm import DecisionLog
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/decisions", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def decisions_log(
    request: Request,
    account_id: str | None = Query(None),
    symbol: str | None = Query(None),
    stage: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/decisions — 의사결정 로그: 에이전트 판단 이력 조회."""
    _from = date.fromisoformat(from_date) if from_date else None
    _to = date.fromisoformat(to_date) if to_date else None

    stmt = select(DecisionLog).order_by(DecisionLog.created_at.desc())
    count_stmt = select(func.count(DecisionLog.id))

    if account_id:
        stmt = stmt.where(DecisionLog.account_id == account_id)
        count_stmt = count_stmt.where(DecisionLog.account_id == account_id)
    if symbol:
        stmt = stmt.where(DecisionLog.symbol == symbol)
        count_stmt = count_stmt.where(DecisionLog.symbol == symbol)
    if stage:
        stmt = stmt.where(DecisionLog.stage == stage)
        count_stmt = count_stmt.where(DecisionLog.stage == stage)
    if _from:
        dt_from = datetime.combine(_from, datetime.min.time(), tzinfo=KST)
        stmt = stmt.where(DecisionLog.created_at >= dt_from)
        count_stmt = count_stmt.where(DecisionLog.created_at >= dt_from)
    if _to:
        dt_to = datetime.combine(_to + timedelta(days=1), datetime.min.time(), tzinfo=KST)
        stmt = stmt.where(DecisionLog.created_at < dt_to)
        count_stmt = count_stmt.where(DecisionLog.created_at < dt_to)

    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)

    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)
    decisions = list((await session.execute(stmt)).scalars().all())

    acct_result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    accounts = list(acct_result.scalars().all())

    context = {
        "request": request,
        "decisions": decisions,
        "accounts": accounts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "account_id": account_id,
        "symbol": symbol,
        "stage": stage,
        "from_date": from_date,
        "to_date": to_date,
    }

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse("partials/decision_rows.html", context)
    return templates.TemplateResponse("decisions.html", context)


@router.get(
    "/decisions/{session_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def decision_session_detail(
    request: Request,
    session_id: UUID,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/decisions/{session_id} — 세션 상세: 해당 세션의 전체 의사결정 체인."""
    stmt = (
        select(DecisionLog)
        .where(DecisionLog.session_id == session_id)
        .order_by(DecisionLog.created_at)
    )
    chain = list((await session.execute(stmt)).scalars().all())

    if not chain:
        raise HTTPException(status_code=404, detail="Session not found")

    return templates.TemplateResponse("partials/decision_detail.html", {
        "request": request,
        "chain": chain,
        "session_id": str(session_id),
    })
