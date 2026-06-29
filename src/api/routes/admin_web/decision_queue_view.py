"""Backoffice 결정 큐 모니터 — trade_decision_queue 관측 (F-09).

08:30 결정 잡이 적재하고 장중 실행 드레인이 소비하는 진입 결정 큐를 상태별로
보여준다. pending → executed | expired | rejected 전이와 gate_reason, 발주된
order_id 연계를 추적한다.
"""

from datetime import date, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import active_accounts, paginate, render
from src.api.templates import templates
from src.core.time import KST
from src.db.models.execution import TradeDecisionQueue
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()

_STATUSES = ["pending", "executed", "expired", "rejected"]


@router.get("/decision-queue", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def decision_queue(
    request: Request,
    account_id: str | None = Query(None),
    status: str | None = Query(None),
    symbol: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/decision-queue — 결정 큐 목록 + 상태 카운터 + 필터."""
    _from = date.fromisoformat(from_date) if from_date else None
    _to = date.fromisoformat(to_date) if to_date else None

    accounts = await active_accounts(session)

    # 상태별 전역 카운터 (필터 무관 — 큐 전체 현황)
    counts = {}
    for st in _STATUSES:
        counts[st] = (
            await session.execute(
                select(func.count(TradeDecisionQueue.id)).where(
                    TradeDecisionQueue.status == st,
                )
            )
        ).scalar_one()

    stmt = select(TradeDecisionQueue).order_by(TradeDecisionQueue.created_at.desc())
    count_stmt = select(func.count(TradeDecisionQueue.id))
    if account_id:
        stmt = stmt.where(TradeDecisionQueue.account_id == account_id)
        count_stmt = count_stmt.where(TradeDecisionQueue.account_id == account_id)
    if status:
        stmt = stmt.where(TradeDecisionQueue.status == status)
        count_stmt = count_stmt.where(TradeDecisionQueue.status == status)
    if symbol:
        stmt = stmt.where(TradeDecisionQueue.symbol == symbol)
        count_stmt = count_stmt.where(TradeDecisionQueue.symbol == symbol)
    if _from:
        dt_from = datetime.combine(_from, datetime.min.time(), tzinfo=KST)
        stmt = stmt.where(TradeDecisionQueue.created_at >= dt_from)
        count_stmt = count_stmt.where(TradeDecisionQueue.created_at >= dt_from)
    if _to:
        dt_to = datetime.combine(_to + timedelta(days=1), datetime.min.time(), tzinfo=KST)
        stmt = stmt.where(TradeDecisionQueue.created_at < dt_to)
        count_stmt = count_stmt.where(TradeDecisionQueue.created_at < dt_to)

    rows, page, total, total_pages = await paginate(
        session, stmt, count_stmt, page, per_page,
    )

    context = {
        "request": request,
        "rows": rows,
        "counts": counts,
        "statuses": _STATUSES,
        "accounts": accounts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "account_id": account_id,
        "status": status,
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
    }
    return render(request, "decision_queue.html", "partials/decision_queue_rows.html", context)


@router.get(
    "/decision-queue/{queue_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def decision_queue_detail(
    request: Request,
    queue_id: int,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/decision-queue/{queue_id} — 결정 큐 항목 상세 (HTMX 모달)."""
    item = await session.get(TradeDecisionQueue, queue_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Decision queue item not found")

    return templates.TemplateResponse("partials/decision_queue_detail.html", {
        "request": request,
        "item": item,
    })
