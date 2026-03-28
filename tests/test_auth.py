"""Unit tests for backoffice authentication module."""

import time
from unittest.mock import patch

import pytest

from src.api.auth import (
    SESSION_MAX_AGE,
    _get_secret_key,
    create_session_token,
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
