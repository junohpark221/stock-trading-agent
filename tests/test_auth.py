"""Unit tests for backoffice authentication module."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    _get_secret_key,
    create_session_token,
    login_handler,
    logout_handler,
    require_admin,
    verify_session_token,
)

SECRET = "test-secret-key-for-signing"


class TestSessionToken:
    """토큰 생성/검증 테스트."""

    def test_create_and_verify(self):
        token = create_session_token(SECRET)
        assert verify_session_token(token, SECRET)

    def test_invalid_token_rejected(self):
        assert verify_session_token("invalid.token.here", SECRET) is False

    def test_wrong_secret_rejected(self):
        token = create_session_token(SECRET)
        assert verify_session_token(token, "wrong-secret") is False

    def test_expired_token_rejected(self):
        token = create_session_token(SECRET)
        # max_age=-1 → 토큰 생성 직후라도 만료 판정
        assert verify_session_token(token, SECRET, max_age=-1) is False

    def test_empty_token_rejected(self):
        assert verify_session_token("", SECRET) is False

    def test_max_age_default(self):
        assert SESSION_MAX_AGE == 86400


class TestGetSecretKey:
    """시크릿 키 폴백 로직 테스트."""

    def test_session_secret_key_preferred(self):
        with patch("src.api.auth.get_settings") as mock:
            mock.return_value.SESSION_SECRET_KEY = "session-key"
            mock.return_value.ACCOUNT_ENCRYPTION_KEY = "account-key"
            assert _get_secret_key() == "session-key"

    def test_fallback_to_account_encryption_key(self):
        with patch("src.api.auth.get_settings") as mock:
            mock.return_value.SESSION_SECRET_KEY = ""
            mock.return_value.ACCOUNT_ENCRYPTION_KEY = "account-key"
            assert _get_secret_key() == "account-key"

    def test_no_key_raises_runtime_error(self):
        with patch("src.api.auth.get_settings") as mock:
            mock.return_value.SESSION_SECRET_KEY = ""
            mock.return_value.ACCOUNT_ENCRYPTION_KEY = ""
            with pytest.raises(RuntimeError):
                _get_secret_key()


class TestAuthHandlers:
    """HTTP 핸들러 테스트: require_admin, login_handler, logout_handler."""

    @pytest.mark.asyncio
    async def test_require_admin_missing_cookie(self):
        request = MagicMock()
        request.cookies = {}
        with pytest.raises(Exception) as exc_info:
            await require_admin(request)
        assert exc_info.value.status_code == 303

    @pytest.mark.asyncio
    async def test_require_admin_invalid_cookie(self):
        request = MagicMock()
        request.cookies = {SESSION_COOKIE_NAME: "invalid.token"}
        with patch("src.api.auth._get_secret_key", return_value=SECRET):
            with pytest.raises(Exception) as exc_info:
                await require_admin(request)
            assert exc_info.value.status_code == 303

    @pytest.mark.asyncio
    async def test_require_admin_valid_cookie(self):
        token = create_session_token(SECRET)
        request = MagicMock()
        request.cookies = {SESSION_COOKIE_NAME: token}
        with patch("src.api.auth._get_secret_key", return_value=SECRET):
            result = await require_admin(request)
        assert result is None

    @pytest.mark.asyncio
    async def test_login_handler_correct_password(self):
        request = AsyncMock()
        request.form.return_value = {"password": "correct-pw"}
        with patch("src.api.auth.get_settings") as mock_settings:
            mock_settings.return_value.ADMIN_PASSWORD = "correct-pw"
            mock_settings.return_value.SESSION_SECRET_KEY = SECRET
            mock_settings.return_value.ACCOUNT_ENCRYPTION_KEY = ""
            response = await login_handler(request)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/"
        cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in cookie_header

    @pytest.mark.asyncio
    async def test_login_handler_wrong_password(self):
        request = AsyncMock()
        request.form.return_value = {"password": "wrong"}
        with patch("src.api.auth.get_settings") as mock_settings:
            mock_settings.return_value.ADMIN_PASSWORD = "correct-pw"
            response = await login_handler(request)
        assert response.status_code == 303
        assert "error=1" in response.headers["location"]

    @pytest.mark.asyncio
    async def test_logout_handler(self):
        request = MagicMock()
        response = await logout_handler(request)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in cookie_header
