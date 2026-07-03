"""Backoffice 뉴스 조회 — news_article 수집 결과 관측 (B-12).

F-19 야간 수집 잡(job_news_collect)이 활성 유니버스 전 종목의 종목명 검색어로
적재한 뉴스를 운영자가 눈으로 검증하기 위한 조회 화면. 종목코드 자유 필터 +
날짜 범위 + 감성 라벨/점수 표시(미채점 기사는 '-' 공백).
"""

from datetime import date, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import require_admin
from src.api.routes.admin_web._common import (
    paginate,
    render,
    symbol_names,
)
from src.core.time import KST
from src.db.models.analysis import NewsArticle
from src.db.session import get_db_session

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/news", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def news_log(
    request: Request,
    symbol: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """GET /admin/news — 수집된 뉴스 기사 목록: 종목·날짜 필터 + 감성 라벨."""
    _from = date.fromisoformat(from_date) if from_date else None
    _to = date.fromisoformat(to_date) if to_date else None

    stmt = select(NewsArticle).order_by(NewsArticle.published_at.desc())
    count_stmt = select(func.count(NewsArticle.id))

    if symbol:
        stmt = stmt.where(NewsArticle.symbol == symbol)
        count_stmt = count_stmt.where(NewsArticle.symbol == symbol)
    if _from:
        dt_from = datetime.combine(_from, datetime.min.time(), tzinfo=KST)
        stmt = stmt.where(NewsArticle.published_at >= dt_from)
        count_stmt = count_stmt.where(NewsArticle.published_at >= dt_from)
    if _to:
        dt_to = datetime.combine(_to + timedelta(days=1), datetime.min.time(), tzinfo=KST)
        stmt = stmt.where(NewsArticle.published_at < dt_to)
        count_stmt = count_stmt.where(NewsArticle.published_at < dt_to)

    articles, page, total, total_pages = await paginate(
        session, stmt, count_stmt, page, per_page,
    )
    names = await symbol_names(session, [a.symbol for a in articles if a.symbol])

    context = {
        "request": request,
        "articles": articles,
        "names": names,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
    }

    return render(request, "news.html", "partials/news_rows.html", context)
