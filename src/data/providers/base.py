"""Abstract base class for data providers.

Every external data source (KIS, DART, ECOS, FRED, Naver) implements this ABC.

- ``initialize/shutdown/health_check`` are **abstract** (all providers must implement).
- Data methods raise ``NotImplementedError`` by default — each provider overrides
  only the methods it supports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.models import OHLCV, StockInfo


class DataProvider(ABC):
    """Abstract data provider with lifecycle and optional data methods."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique provider identifier (e.g. ``"kis"``, ``"dart"``)."""

    # ── Lifecycle (abstract — all providers must implement) ────────────

    @abstractmethod
    async def initialize(self) -> None:
        """Establish connections and authenticate."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Release resources and close connections."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return ``True`` if the provider is healthy (no API calls)."""

    # ── Data methods (optional — NotImplementedError default) ─────────

    async def fetch_stock_master(self) -> list[StockInfo]:
        """Return cached/DB stock master list."""
        raise NotImplementedError(
            f"{self.provider_name} does not support fetch_stock_master"
        )

    async def fetch_daily_ohlcv(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[OHLCV]:
        """Return cached/DB daily OHLCV bars for *symbol* in date range."""
        raise NotImplementedError(
            f"{self.provider_name} does not support fetch_daily_ohlcv"
        )

    async def sync_stock_master(self) -> int:
        """Fetch from upstream API and upsert to DB. Returns row count."""
        raise NotImplementedError(
            f"{self.provider_name} does not support sync_stock_master"
        )

    async def sync_daily_ohlcv(self, symbol: str, *, period_days: int = 100) -> int:
        """Fetch OHLCV from upstream API and upsert to DB. Returns row count."""
        raise NotImplementedError(
            f"{self.provider_name} does not support sync_daily_ohlcv"
        )
