"""Backoffice 실행 모니터 — WS 손절 상태 + 거부주문 경보 (F-05/F-06).

섹션 A: 실시간 WS 손절(StopLossStreamService) 구독/연결 상태 + in-flight 청산.
섹션 B: 승인 거부·만료인데 체결된 고위험 주문(disapproved_but_filled) 경보.
"""

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import symbol_names
from src.api.templates import templates
from src.db.models.execution import Order
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()


async def _ws_status() -> dict:
    """StopLossStreamService 읽기 상태. 미가동/예외 시 available=False."""
    from src.main import get_stoploss_stream

    stream = get_stoploss_stream()
    if stream is None:
        return {"available": False}
    try:
        status = await stream.get_status()
        status["available"] = True
        return status
    except Exception:
        logger.warning("exec_monitor.ws_status_failed", exc_info=True)
        return {"available": False}


async def _disapproved_orders(session: AsyncSession, limit: int = 50) -> list[Order]:
    """F-06 고위험: 승인 거부/만료인데 체결됐거나 그렇게 마킹된 주문."""
    stmt = (
        select(Order)
        .where(
            or_(
                Order.rejection_reason == "disapproved_but_filled",
                and_(
                    Order.approval_status.in_(["rejected", "timeout"]),
                    Order.status.in_(["filled", "partially_filled"]),
                ),
            )
        )
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/exec-monitor", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def exec_monitor(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/exec-monitor — WS 손절 상태 + 거부주문 경보."""
    ws = await _ws_status()
    alerts = await _disapproved_orders(session)
    names = await symbol_names(session, [o.symbol for o in alerts if o.symbol])
    return templates.TemplateResponse("exec_monitor.html", {
        "request": request,
        "ws": ws,
        "alerts": alerts,
        "names": names,
    })


@router.get("/exec-monitor/status", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def exec_monitor_status(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/exec-monitor/status — 실시간 상태 partial (HTMX 폴링)."""
    ws = await _ws_status()
    alerts = await _disapproved_orders(session)
    names = await symbol_names(session, [o.symbol for o in alerts if o.symbol])
    return templates.TemplateResponse("partials/exec_monitor_status.html", {
        "request": request,
        "ws": ws,
        "alerts": alerts,
        "names": names,
    })
