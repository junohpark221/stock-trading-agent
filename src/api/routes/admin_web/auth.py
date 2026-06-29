"""Backoffice auth routes (login/logout) — no require_admin guard."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.api.auth import login_handler, logout_handler
from src.api.templates import templates

router = APIRouter()


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
