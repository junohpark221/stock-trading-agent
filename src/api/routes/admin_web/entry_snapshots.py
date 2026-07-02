"""Backoffice 진입 분석 스냅샷 — positions.entry_analysis_snapshot 관측 (F-07).

진입 시 LLM 분석 요약(action/confidence/key_factors)과 실제 청산 손익을 한 화면에서
대조해 메모리 학습 품질을 점검한다. 포지션 기준(진입→청산 라이프사이클).
"""

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import (
    active_accounts,
    paginate,
    render,
    symbol_names,
)
from src.db.models.strategy import PositionRecord
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/entry-snapshots", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def entry_snapshots(
    request: Request,
    account_id: str | None = Query(None),
    symbol: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/entry-snapshots — 진입 분석 스냅샷 보유 포지션 목록."""
    accounts = await active_accounts(session)

    stmt = (
        select(PositionRecord)
        .where(PositionRecord.entry_analysis_snapshot.isnot(None))
        .order_by(PositionRecord.entry_date.desc())
    )
    count_stmt = select(func.count(PositionRecord.id)).where(
        PositionRecord.entry_analysis_snapshot.isnot(None),
    )
    if account_id:
        stmt = stmt.where(PositionRecord.account_id == account_id)
        count_stmt = count_stmt.where(PositionRecord.account_id == account_id)
    if symbol:
        stmt = stmt.where(PositionRecord.symbol == symbol)
        count_stmt = count_stmt.where(PositionRecord.symbol == symbol)
    if status:
        stmt = stmt.where(PositionRecord.status == status)
        count_stmt = count_stmt.where(PositionRecord.status == status)

    rows, page, total, total_pages = await paginate(
        session, stmt, count_stmt, page, per_page,
    )
    names = await symbol_names(session, [r.symbol for r in rows if r.symbol])

    context = {
        "request": request,
        "rows": rows,
        "names": names,
        "accounts": accounts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "account_id": account_id,
        "symbol": symbol,
        "status": status,
    }
    return render(request, "entry_snapshots.html", "partials/entry_snapshot_rows.html", context)
