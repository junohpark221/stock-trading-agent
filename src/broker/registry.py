"""BrokerRegistry — manages per-account BrokerInterface instances.

Creates, caches, and disconnects broker clients for each trading account.
Supports both KISClient (real API) and InMemoryBroker (mock).

Usage::

    registry = BrokerRegistry(cache=cache, use_mock=True)
    broker = await registry.register(credentials)
    broker = registry.get("acct-1")
    await registry.disconnect_all()
"""

from __future__ import annotations

import structlog

from src.broker.base import BrokerInterface
from src.broker.credentials import AccountCredentials
from src.broker.kis.client import KISClient
from src.broker.mock.client import InMemoryBroker
from src.data.cache import RedisCache

logger = structlog.get_logger(__name__)


class BrokerRegistry:
    """Manages per-account BrokerInterface instances.

    Args:
        cache: RedisCache for token caching (shared across all KIS clients).
        use_mock: If True, register() creates InMemoryBroker instead of KISClient.
    """

    def __init__(self, cache: RedisCache, *, use_mock: bool = False) -> None:
        self._cache = cache
        self._use_mock = use_mock
        self._brokers: dict[str, BrokerInterface] = {}

    async def register(self, credentials: AccountCredentials) -> BrokerInterface:
        """Create a broker for the given account, connect it, and store it.

        If the account_id is already registered, disconnect the existing
        broker first (allows re-registration with new credentials).

        Args:
            credentials: Decrypted account credentials.

        Returns:
            Connected BrokerInterface instance.
        """
        account_id = credentials.account_id

        # Disconnect existing broker if re-registering
        if account_id in self._brokers:
            try:
                await self._brokers[account_id].disconnect()
            except Exception:
                logger.exception("broker_disconnect_error_on_replace", account_id=account_id)
            logger.info("broker_replaced", account_id=account_id)

        if self._use_mock:
            broker: BrokerInterface = InMemoryBroker()
        else:
            broker = KISClient.from_credentials(credentials, self._cache)

        await broker.connect()
        self._brokers[account_id] = broker
        logger.info(
            "broker_registered",
            account_id=account_id,
            broker_type=type(broker).__name__,
        )
        return broker

    def get(self, account_id: str) -> BrokerInterface:
        """Return the broker for the given account.

        Raises:
            KeyError: If account_id is not registered.
        """
        if account_id not in self._brokers:
            raise KeyError(f"No broker registered for account: {account_id}")
        return self._brokers[account_id]

    def get_all(self) -> dict[str, BrokerInterface]:
        """Return a copy of all registered brokers."""
        return dict(self._brokers)

    async def disconnect_all(self) -> None:
        """Disconnect all registered brokers and clear the registry."""
        for account_id, broker in self._brokers.items():
            try:
                await broker.disconnect()
                logger.info("broker_disconnected", account_id=account_id)
            except Exception:
                logger.exception("broker_disconnect_error", account_id=account_id)
        self._brokers.clear()
