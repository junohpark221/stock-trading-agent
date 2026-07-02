"""Shared helpers for backoffice web routes.

Extracted from the per-screen duplication that accumulated in the monolith:
pagination, HTMX full-vs-partial response selection, query-string redirects,
the active-accounts dropdown query, and navigation context injection.
"""

import math
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.templates import templates
from src.data.stock_names import resolve_symbol_names
from src.db.models.account import Account


async def paginate(session: AsyncSession, stmt, count_stmt, page: int, per_page: int):
    """Run count + offset/limit query. Returns (rows, page, total, total_pages).

    ``page`` is clamped to the available range. Count runs before the row
    query, matching the original per-screen ordering.
    """
    total = (await session.execute(count_stmt)).scalar_one()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    rows = list(
        (await session.execute(stmt.offset(offset).limit(per_page))).scalars().all()
    )
    return rows, page, total, total_pages


def render(request: Request, full_template: str, partial_template: str, context: dict):
    """Return the partial template for HTMX requests, else the full page."""
    name = partial_template if request.headers.get("HX-Request") else full_template
    return templates.TemplateResponse(name, context)


def redirect_with(base: str, **params) -> RedirectResponse:
    """303 redirect to ``base`` with a urlencoded query string (None values dropped)."""
    clean = {k: v for k, v in params.items() if v is not None}
    url = f"{base}?{urlencode(clean)}" if clean else base
    return RedirectResponse(url, status_code=303)


async def active_accounts(session: AsyncSession) -> list[Account]:
    """Active accounts ordered by creation — shared filter-dropdown source."""
    result = await session.execute(
        select(Account).where(Account.is_active.is_(True)).order_by(Account.created_at)
    )
    return list(result.scalars().all())


async def symbol_names(session: AsyncSession, symbols: list[str]) -> dict[str, str]:
    """{종목코드: 종목명} 매핑 — 로그 화면의 종목명 병기용(N+1 회피).

    미존재 종목은 누락되므로 템플릿에서 ``names.get(x.symbol, x.symbol)``로 폴백.
    de-listed(is_active=False) 종목도 이름을 반환한다.
    """
    return await resolve_symbol_names(session, symbols)
