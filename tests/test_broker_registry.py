"""Unit tests for BrokerRegistry, AccountCredentials, KISClient.from_credentials,
and KISAuth per-account cache key isolation.

All HTTP calls and Redis operations are mocked — no external dependencies.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.broker.credentials import AccountCredentials
from src.broker.kis.auth import KISAuth, _KIS_PAPER_BASE_URL, _KIS_PROD_BASE_URL
from src.broker.kis.client import KISClient
from src.broker.mock.client import InMemoryBroker
from src.broker.registry import BrokerRegistry
from src.core.exceptions import ConfigurationError
from src.data.cache import RedisCache

from conftest import make_settings


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_credentials(**overrides: object) -> AccountCredentials:
    """Create AccountCredentials with sensible defaults."""
    defaults = {
        "account_id": "test-acct",
        "app_key": "test_key",
        "app_secret": "test_secret",
        "account_no": "1234567801",
        "account_prod": "01",
        "is_paper": True,
        "hts_id": "test_hts",
    }
    defaults.update(overrides)
    return AccountCredentials(**defaults)  # type: ignore[arg-type]


def _make_mock_cache() -> MagicMock:
    """Create a MagicMock RedisCache with async methods."""
    cache = MagicMock(spec=RedisCache)
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    cache.delete = AsyncMock()
    return cache


# ── TestAccountCredentials ───────────────────────────────────────────────


class TestAccountCredentials:
    """AccountCredentials dataclass tests."""

    def test_frozen_dataclass(self) -> None:
        creds = _make_credentials()
        with pytest.raises(AttributeError):
            creds.account_id = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        creds = AccountCredentials(
            account_id="x", app_key="k", app_secret="s", account_no="12345678"
        )
        assert creds.account_prod == "01"
        assert creds.is_paper is True
        assert creds.hts_id == ""

    def test_all_fields_set(self) -> None:
        creds = _make_credentials(
            account_id="acct-1",
            app_key="KEY",
            app_secret="SECRET",
            account_no="9876543201",
            account_prod="02",
            is_paper=False,
            hts_id="myhts",
        )
        assert creds.account_id == "acct-1"
        assert creds.app_key == "KEY"
        assert creds.account_prod == "02"
        assert creds.is_paper is False
        assert creds.hts_id == "myhts"


# ── TestBrokerRegistry ──────────────────────────────────────────────────


class TestBrokerRegistryRegister:
    """BrokerRegistry.register() tests."""

    @pytest.mark.asyncio
    async def test_register_mock_broker(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        creds = _make_credentials()

        broker = await registry.register(creds)

        assert isinstance(broker, InMemoryBroker)
        assert broker._connected is True

    @pytest.mark.asyncio
    async def test_register_stores_broker(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        creds = _make_credentials(account_id="acct-1")

        broker = await registry.register(creds)

        assert registry.get("acct-1") is broker

    @pytest.mark.asyncio
    async def test_register_kis_broker(self) -> None:
        """use_mock=False creates KISClient via from_credentials."""
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=False)
        creds = _make_credentials()

        # Mock connect() to avoid real HTTP
        with patch.object(KISClient, "connect", new_callable=AsyncMock) as mock_connect:
            broker = await registry.register(creds)

        assert isinstance(broker, KISClient)
        mock_connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_re_register_disconnects_old(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        creds = _make_credentials(account_id="acct-1")

        old_broker = await registry.register(creds)
        assert old_broker._connected is True

        new_broker = await registry.register(creds)

        assert old_broker._connected is False  # disconnected
        assert new_broker._connected is True
        assert registry.get("acct-1") is new_broker


class TestBrokerRegistryGet:
    """BrokerRegistry.get() tests."""

    @pytest.mark.asyncio
    async def test_get_existing(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        creds = _make_credentials(account_id="acct-1")
        broker = await registry.register(creds)

        assert registry.get("acct-1") is broker

    def test_get_missing_raises_key_error(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)

        with pytest.raises(KeyError, match="acct-missing"):
            registry.get("acct-missing")


class TestBrokerRegistryGetAll:
    """BrokerRegistry.get_all() tests."""

    def test_empty(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        assert registry.get_all() == {}

    @pytest.mark.asyncio
    async def test_multiple(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        b1 = await registry.register(_make_credentials(account_id="a1"))
        b2 = await registry.register(_make_credentials(account_id="a2"))

        result = registry.get_all()
        assert result == {"a1": b1, "a2": b2}

    @pytest.mark.asyncio
    async def test_returns_copy(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        await registry.register(_make_credentials(account_id="a1"))

        copy = registry.get_all()
        copy.clear()  # modify the copy

        assert len(registry.get_all()) == 1  # original unaffected


class TestBrokerRegistryDisconnectAll:
    """BrokerRegistry.disconnect_all() tests."""

    @pytest.mark.asyncio
    async def test_disconnects_all_brokers(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        b1 = await registry.register(_make_credentials(account_id="a1"))
        b2 = await registry.register(_make_credentials(account_id="a2"))

        await registry.disconnect_all()

        assert b1._connected is False
        assert b2._connected is False

    @pytest.mark.asyncio
    async def test_clears_registry(self) -> None:
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        await registry.register(_make_credentials(account_id="a1"))

        await registry.disconnect_all()

        with pytest.raises(KeyError):
            registry.get("a1")
        assert registry.get_all() == {}

    @pytest.mark.asyncio
    async def test_continues_on_error(self) -> None:
        """One broker disconnect error should not prevent others from disconnecting."""
        cache = _make_mock_cache()
        registry = BrokerRegistry(cache, use_mock=True)
        b1 = await registry.register(_make_credentials(account_id="a1"))
        b2 = await registry.register(_make_credentials(account_id="a2"))

        # Make first broker's disconnect raise
        b1.disconnect = AsyncMock(side_effect=RuntimeError("network error"))  # type: ignore[method-assign]

        await registry.disconnect_all()  # should not raise

        b1.disconnect.assert_awaited_once()
        assert b2._connected is False
        assert registry.get_all() == {}


# ── TestKISClientFromCredentials ─────────────────────────────────────────


class TestKISClientFromCredentials:
    """KISClient.from_credentials() classmethod tests."""

    def test_paper_base_url(self) -> None:
        creds = _make_credentials(is_paper=True)
        client = KISClient.from_credentials(creds, _make_mock_cache())
        assert client._base_url == _KIS_PAPER_BASE_URL

    def test_prod_base_url(self) -> None:
        creds = _make_credentials(is_paper=False)
        client = KISClient.from_credentials(creds, _make_mock_cache())
        assert client._base_url == _KIS_PROD_BASE_URL

    def test_cano_and_acnt_prdt_cd(self) -> None:
        creds = _make_credentials(account_no="9876543201", account_prod="02")
        client = KISClient.from_credentials(creds, _make_mock_cache())
        assert client._cano == "98765432"
        assert client._acnt_prdt_cd == "02"

    def test_empty_account_no(self) -> None:
        creds = _make_credentials(account_no="")
        client = KISClient.from_credentials(creds, _make_mock_cache())
        assert client._cano == ""

    def test_rate_limit_auto_paper(self) -> None:
        creds = _make_credentials(is_paper=True)
        KISClient.from_credentials(creds, _make_mock_cache())
        assert KISClient._global_rate_interval == 0.5

    def test_rate_limit_auto_prod(self) -> None:
        creds = _make_credentials(is_paper=False)
        KISClient.from_credentials(creds, _make_mock_cache())
        assert KISClient._global_rate_interval == 0.05

    def test_rate_limit_explicit(self) -> None:
        creds = _make_credentials()
        KISClient.from_credentials(creds, _make_mock_cache(), rate_limit_interval=0.1)
        assert KISClient._global_rate_interval == 0.1

    def test_settings_is_none(self) -> None:
        creds = _make_credentials()
        client = KISClient.from_credentials(creds, _make_mock_cache())
        assert client._settings is None
        assert client._credentials is creds

    @pytest.mark.asyncio
    async def test_connect_creates_auth_with_account_id(self) -> None:
        """After connect(), auth should use the account_id from credentials."""
        cache = _make_mock_cache()
        # Return a fake token to avoid real HTTP
        cache.get = AsyncMock(return_value="fake_token")

        creds = _make_credentials(account_id="acct-42")
        client = KISClient.from_credentials(creds, cache)

        await client.connect()

        assert client._auth is not None
        assert client._auth._account_id == "acct-42"

        await client.disconnect()


# ── TestKISAuthAccountId ─────────────────────────────────────────────────


class TestKISAuthAccountId:
    """KISAuth per-account cache key isolation tests."""

    def test_default_account_id(self) -> None:
        auth = KISAuth(
            settings=make_settings(),
            cache=_make_mock_cache(),
            session=MagicMock(spec=aiohttp.ClientSession),
        )
        assert auth._account_id == "default"

    def test_custom_account_id(self) -> None:
        auth = KISAuth(
            settings=make_settings(),
            cache=_make_mock_cache(),
            session=MagicMock(spec=aiohttp.ClientSession),
            account_id="acct-123",
        )
        assert auth._account_id == "acct-123"

    def test_cache_key_default(self) -> None:
        auth = KISAuth(
            settings=make_settings(),
            cache=_make_mock_cache(),
            session=MagicMock(spec=aiohttp.ClientSession),
        )
        assert auth._cache_key_token == "default:token"

    def test_cache_key_custom(self) -> None:
        auth = KISAuth(
            settings=make_settings(),
            cache=_make_mock_cache(),
            session=MagicMock(spec=aiohttp.ClientSession),
            account_id="acct-1",
        )
        assert auth._cache_key_token == "acct-1:token"

    @pytest.mark.asyncio
    async def test_get_token_uses_account_key(self) -> None:
        cache = _make_mock_cache()
        cache.get = AsyncMock(return_value="cached_token")
        auth = KISAuth(
            settings=make_settings(),
            cache=cache,
            session=MagicMock(spec=aiohttp.ClientSession),
            account_id="acct-1",
        )

        token = await auth.get_token()

        assert token == "cached_token"
        cache.get.assert_awaited_once_with("kis", "acct-1:token")

    @pytest.mark.asyncio
    async def test_refresh_deletes_correct_key(self) -> None:
        cache = _make_mock_cache()
        # set up: get returns None (cache miss after delete), then mock _issue_token
        cache.get = AsyncMock(return_value=None)

        auth = KISAuth(
            settings=make_settings(),
            cache=cache,
            session=MagicMock(spec=aiohttp.ClientSession),
            account_id="acct-2",
        )

        # Mock _issue_token to avoid HTTP
        auth._issue_token = AsyncMock(return_value="new_token")  # type: ignore[method-assign]

        await auth.refresh_token()

        cache.delete.assert_awaited_once_with("kis", "acct-2:token")

    def test_settings_path_backward_compatible(self) -> None:
        """Legacy Settings-based construction still works without account_id."""
        settings = make_settings()
        auth = KISAuth(
            settings=settings,
            cache=_make_mock_cache(),
            session=MagicMock(spec=aiohttp.ClientSession),
        )
        assert auth._app_key == "test_app_key"
        assert auth._app_secret == "test_app_secret"
        assert auth._account_id == "default"

    def test_direct_credentials_path(self) -> None:
        """Direct credentials path (settings=None) works."""
        auth = KISAuth(
            cache=_make_mock_cache(),
            session=MagicMock(spec=aiohttp.ClientSession),
            account_id="acct-direct",
            app_key="DIRECT_KEY",
            app_secret="DIRECT_SECRET",
            is_paper=False,
            token_ttl=3600,
        )
        assert auth._app_key == "DIRECT_KEY"
        assert auth._app_secret == "DIRECT_SECRET"
        assert auth._is_paper is False
        assert auth._token_ttl == 3600
        assert auth._account_id == "acct-direct"
        assert auth._base_url == _KIS_PROD_BASE_URL

    def test_direct_path_missing_key_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="KIS_APP_KEY"):
            KISAuth(
                cache=_make_mock_cache(),
                session=MagicMock(spec=aiohttp.ClientSession),
                app_key="",
                app_secret="secret",
            )

    def test_direct_path_missing_secret_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="KIS_APP_SECRET"):
            KISAuth(
                cache=_make_mock_cache(),
                session=MagicMock(spec=aiohttp.ClientSession),
                app_key="key",
                app_secret="",
            )
