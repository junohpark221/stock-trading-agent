"""Backoffice authentication — single-password cookie-based session.

Uses itsdangerous URLSafeTimedSerializer for signed session cookies.
ADMIN_PASSWORD env var controls access; empty value disables backoffice.
"""

import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import get_settings

SESSION_COOKIE_NAME = "admin_session"
SESSION_MAX_AGE = 86400  # 24시간


def _get_secret_key() -> str:
    """SESSION_SECRET_KEY 우선, 없으면 ACCOUNT_ENCRYPTION_KEY 폴백."""
    settings = get_settings()
    key = settings.SESSION_SECRET_KEY or settings.ACCOUNT_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "SESSION_SECRET_KEY 또는 ACCOUNT_ENCRYPTION_KEY 중 하나는 설정되어야 합니다"
        )
    return key


def create_session_token(secret_key: str) -> str:
    """서명된 세션 토큰 생성."""
    s = URLSafeTimedSerializer(secret_key)
    return s.dumps({"admin": True})


def verify_session_token(
    token: str, secret_key: str, max_age: int = SESSION_MAX_AGE
) -> bool:
    """세션 토큰 검증. 만료/변조 시 False."""
    try:
        s = URLSafeTimedSerializer(secret_key)
        s.loads(token, max_age=max_age)
        return True
    except (BadSignature, SignatureExpired):
        return False


async def require_admin(request: Request) -> None:
    """FastAPI Depends 의존성. 인증 실패 시 /admin/login으로 리다이렉트."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or not verify_session_token(token, _get_secret_key()):
        from fastapi import HTTPException

        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


async def login_handler(request: Request) -> RedirectResponse:
    """POST /admin/login — 비밀번호 검증 후 쿠키 설정."""
    form = await request.form()
    password = form.get("password", "")
    settings = get_settings()

    if not hmac.compare_digest(str(password), settings.ADMIN_PASSWORD):
        # Step 3에서 로그인 페이지 템플릿 렌더링 시 에러 메시지 전달 예정
        return RedirectResponse("/admin/login?error=1", status_code=303)

    secret_key = _get_secret_key()
    token = create_session_token(secret_key)
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


async def logout_handler(request: Request) -> RedirectResponse:
    """GET /admin/logout — 쿠키 삭제 후 로그인 페이지로 리다이렉트."""
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


