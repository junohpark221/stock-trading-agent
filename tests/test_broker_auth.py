"""Unit tests for KISAuth token management.

All HTTP calls and Redis operations are mocked — no external dependencies.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.broker.kis.auth import KISAuth, _KIS_PAPER_BASE_URL, _KIS_PROD_BASE_URL
from src.config import Settings
from src.core.exceptions import AuthError, ConfigurationError
from src.data.cache import RedisCache

from conftest import AsyncContextManagerMock, make_settings, mock_aiohttp_response


# ── Helpers ───────────────────────────────────────────────────────────


def _make_auth(
    settings: Settings | None = None,
    cache: RedisCache | None = None,
    session: aiohttp.ClientSession | None = None,
) -> KISAuth:
    """Create a KISAuth with mocked dependencies."""
    return KISAuth(
        settings=settings or make_settings(),
        cache=cache or MagicMock(spec=RedisCache),
        session=session or MagicMock(spec=aiohttp.ClientSession),
    )


# ── TestInit ──────────────────────────────────────────────────────────


class TestInit:
    """KISAuth.__init__() validation and configuration."""

    def test_missing_app_key_raises(self) -> None:
        settings = make_settings(KIS_APP_KEY="")
        with pytest.raises(ConfigurationError, match="KIS_APP_KEY"):
            _make_auth(settings=settings)

    def test_missing_app_secret_raises(self) -> None:
        settings = make_settings(KIS_APP_SECRET="")
        with pytest.raises(ConfigurationError, match="KIS_APP_SECRET"):
            _make_auth(settings=settings)

    def test_paper_base_url_default(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=True, KIS_BASE_URL=""))
        assert auth._base_url == _KIS_PAPER_BASE_URL

    def test_prod_base_url_default(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=False, KIS_BASE_URL=""))
        assert auth._base_url == _KIS_PROD_BASE_URL

    def test_explicit_base_url_takes_priority(self) -> None:
        auth = _make_auth(
            settings=make_settings(KIS_BASE_URL="https://custom.example.com")
        )
        assert auth._base_url == "https://custom.example.com"

    def test_trailing_slash_stripped(self) -> None:
        auth = _make_auth(
            settings=make_settings(KIS_BASE_URL="https://custom.example.com/")
        )
        assert auth._base_url == "https://custom.example.com"


# ── TestGetToken ──────────────────────────────────────────────────────


class TestGetToken:
    """KISAuth.get_token() — cache hit, cache miss, error scenarios."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached(self) -> None:
        cache = MagicMock(spec=RedisCache)
        cache.get = AsyncMock(return_value="cached_token_abc")
        auth = _make_auth(cache=cache)

        result = await auth.get_token()

        assert result == "cached_token_abc"
        cache.get.assert_awaited_once_with("kis", "token")

    @pytest.mark.asyncio
    async def test_cache_miss_issues_new_token(self) -> None:
        cache = MagicMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()

        resp = mock_aiohttp_response(json_data={"access_token": "new_token_xyz"})
        session = MagicMock(spec=aiohttp.ClientSession)
        session.post = MagicMock(return_value=AsyncContextManagerMock(resp))

        auth = _make_auth(cache=cache, session=session)
        result = await auth.get_token()

        assert result == "new_token_xyz"
        cache.set.assert_awaited_once_with("kis", "token", "new_token_xyz", ttl=82800)

    @pytest.mark.asyncio
    async def test_http_error_raises_auth_error(self) -> None:
        cache = MagicMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        resp = mock_aiohttp_response(status=401, text="Unauthorized")
        session = MagicMock(spec=aiohttp.ClientSession)
        session.post = MagicMock(return_value=AsyncContextManagerMock(resp))

        auth = _make_auth(cache=cache, session=session)
        with pytest.raises(AuthError, match="HTTP 401"):
            await auth.get_token()

    @pytest.mark.asyncio
    async def test_missing_access_token_raises_auth_error(self) -> None:
        cache = MagicMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        resp = mock_aiohttp_response(json_data={"token_type": "Bearer"})
        session = MagicMock(spec=aiohttp.ClientSession)
        session.post = MagicMock(return_value=AsyncContextManagerMock(resp))

        auth = _make_auth(cache=cache, session=session)
        with pytest.raises(AuthError, match="missing access_token"):
            await auth.get_token()

    @pytest.mark.asyncio
    async def test_network_error_raises_auth_error(self) -> None:
        cache = MagicMock(spec=RedisCache)
        cache.get = AsyncMock(return_value=None)

        session = MagicMock(spec=aiohttp.ClientSession)
        session.post = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

        auth = _make_auth(cache=cache, session=session)
        with pytest.raises(AuthError, match="network error"):
            await auth.get_token()


# ── TestRefreshToken ──────────────────────────────────────────────────


class TestRefreshToken:
    """KISAuth.refresh_token() — invalidate + re-issue."""

    @pytest.mark.asyncio
    async def test_deletes_cache_and_reissues(self) -> None:
        cache = MagicMock(spec=RedisCache)
        cache.delete = AsyncMock(return_value=True)
        cache.set = AsyncMock()

        resp = mock_aiohttp_response(json_data={"access_token": "refreshed_token"})
        session = MagicMock(spec=aiohttp.ClientSession)
        session.post = MagicMock(return_value=AsyncContextManagerMock(resp))

        auth = _make_auth(cache=cache, session=session)
        result = await auth.refresh_token()

        assert result == "refreshed_token"
        cache.delete.assert_awaited_once_with("kis", "token")
        cache.set.assert_awaited_once()


# ── TestBuildHeaders ──────────────────────────────────────────────────


class TestBuildHeaders:
    """KISAuth.build_headers() — header construction + TR ID conversion."""

    def test_contains_all_required_keys(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("tok", "FHKST01010100")
        required_keys = {
            "Content-Type",
            "Accept",
            "charset",
            "User-Agent",
            "authorization",
            "appkey",
            "appsecret",
            "tr_id",
            "custtype",
            "tr_cont",
        }
        assert required_keys == set(headers.keys())

    def test_authorization_bearer_format(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("my_token", "FHKST01010100")
        assert headers["authorization"] == "Bearer my_token"

    def test_appkey_in_headers(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("tok", "FHKST01010100")
        assert headers["appkey"] == "test_app_key"
        assert headers["appsecret"] == "test_app_secret"

    def test_user_agent(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("tok", "FHKST01010100")
        assert headers["User-Agent"] == "stock-trading-agent"

    def test_custtype_is_p(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("tok", "FHKST01010100")
        assert headers["custtype"] == "P"

    def test_tr_cont_default_empty(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("tok", "FHKST01010100")
        assert headers["tr_cont"] == ""

    def test_tr_cont_custom_value(self) -> None:
        auth = _make_auth()
        headers = auth.build_headers("tok", "FHKST01010100", tr_cont="N")
        assert headers["tr_cont"] == "N"

    # ── TR ID Conversion (Paper) ──────────────────────────────────

    def test_paper_t_prefix_converts_to_v(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=True))
        headers = auth.build_headers("tok", "TTTC0012U")
        assert headers["tr_id"] == "VTTC0012U"

    def test_paper_j_prefix_converts_to_v(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=True))
        headers = auth.build_headers("tok", "JTCE1001U")
        assert headers["tr_id"] == "VTCE1001U"

    def test_paper_c_prefix_converts_to_v(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=True))
        headers = auth.build_headers("tok", "CTOS5011R")
        assert headers["tr_id"] == "VTOS5011R"

    def test_paper_f_prefix_no_conversion(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=True))
        headers = auth.build_headers("tok", "FHKST01010100")
        assert headers["tr_id"] == "FHKST01010100"

    # ── TR ID Conversion (Production) ─────────────────────────────

    def test_prod_t_prefix_no_conversion(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=False))
        headers = auth.build_headers("tok", "TTTC0012U")
        assert headers["tr_id"] == "TTTC0012U"

    def test_prod_j_prefix_no_conversion(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=False))
        headers = auth.build_headers("tok", "JTCE1001U")
        assert headers["tr_id"] == "JTCE1001U"

    def test_prod_f_prefix_no_conversion(self) -> None:
        auth = _make_auth(settings=make_settings(KIS_IS_PAPER=False))
        headers = auth.build_headers("tok", "FHKST01010100")
        assert headers["tr_id"] == "FHKST01010100"
