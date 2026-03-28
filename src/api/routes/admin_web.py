"""Backoffice web UI routes.

Step 3: login/logout + placeholder dashboard.
Step 4+: dashboard and other pages added incrementally.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.api.auth import login_handler, logout_handler, require_admin
from src.api.templates import templates

router = APIRouter(prefix="/admin", tags=["admin-web"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """GET /admin/login — 로그인 폼 렌더링."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login(request: Request):
    """POST /admin/login — 비밀번호 검증 후 쿠키 설정."""
    return await login_handler(request)


@router.get("/logout")
async def logout(request: Request):
    """GET /admin/logout — 쿠키 삭제 후 리다이렉트."""
    return await logout_handler(request)


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def dashboard_placeholder(request: Request):
    """GET /admin/ — 임시 placeholder (Step 4에서 대시보드로 교체)."""
    return HTMLResponse(
        "<html><body><h1>Dashboard — Step 4에서 구현 예정</h1>"
        '<p><a href="/admin/logout">로그아웃</a></p></body></html>'
    )
