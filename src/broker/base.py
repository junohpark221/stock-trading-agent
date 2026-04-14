"""Abstract broker interface for stock trading operations.

All broker implementations (KIS, mock, etc.) must implement this ABC.
Return types use Pydantic domain models from ``src/core/models``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date
    from types import TracebackType

    from src.core.models import (
        AccountBalance,
        OHLCV,
        OrderRequest,
        OrderResult,
        Position,
        PriceInfo,
        StockInfo,
    )


class BrokerInterface(ABC):
    """Abstract base class for broker integrations.

    Usage::

        async with SomeBroker(settings) as broker:
            price = await broker.get_price("005930")
    """

    # ── Market Data ───────────────────────────────────────────────────

    @abstractmethod
    async def get_price(self, symbol: str) -> PriceInfo:
        """Fetch the current price snapshot for *symbol*."""

    @abstractmethod
    async def get_daily_ohlcv(
        self, symbol: str, *, period_days: int = 100
    ) -> list[OHLCV]:
        """Fetch daily OHLCV bars for the last *period_days* trading days."""

    @abstractmethod
    async def get_stock_master(self) -> list[StockInfo]:
        """Fetch the full list of listed stocks (종목 마스터)."""

    # ── Trading ───────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Submit a buy/sell order. Returns the execution result."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns ``True`` on success."""

    @abstractmethod
    async def get_order_status(
        self, broker_order_id: str, *, order_date: date | None = None
    ) -> OrderResult:
        """Fetch current status of a previously submitted order.

        Used by OrderReconciler to confirm KIS fills after the asynchronous
        WebSocket 체결통보 path fails or is disabled.

        ``order_date`` defaults to today in KST when ``None``.
        Returns an ``OrderResult`` whose ``status`` reflects the latest state:
        SUBMITTED (still pending), FILLED, PARTIALLY_FILLED, REJECTED, CANCELLED.
        """

    # ── Account ───────────────────────────────────────────────────────

    @abstractmethod
    async def get_balance(self) -> AccountBalance:
        """Fetch the account balance summary."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch all open positions."""

    async def get_balance_and_positions(self) -> tuple[AccountBalance, list[Position]]:
        """Fetch balance and positions in a single API round-trip.

        Default implementation calls get_balance() + get_positions() separately.
        Subclasses (e.g. KISClient) should override to avoid duplicate API calls.
        """
        balance = await self.get_balance()
        positions = await self.get_positions()
        return balance, positions

    # ── Lifecycle ─────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Initialize connections (HTTP session, auth tokens, etc.)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close connections and release resources."""

    # ── Context Manager ───────────────────────────────────────────────

    async def __aenter__(self) -> BrokerInterface:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.disconnect()
